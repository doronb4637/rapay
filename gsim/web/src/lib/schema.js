/**
 * Client-side mirror of gsim/core_gateway/payloads.py.
 *
 * The server re-normalises every payload before encoding, so this is not a
 * trust boundary -- it exists so the form starts out fully populated (every
 * input controlled from first render) and so the user can see exactly what
 * will be sent. The two must agree on defaults; if they ever drift, the
 * server wins.
 */

/** The zero value for one schema node. */
export function defaultFor(node) {
  switch (node.kind) {
    case 'scalar':
      return node.numeric === 'float' ? 0.0 : 0;
    case 'enum': {
      const options = node.options ?? [];
      // Prefer the member whose value is 0 (the conventional UNKNOWN/unset
      // member); EnumField.from_bytes rejects any value with no member.
      const zero = options.find((option) => option.value === 0);
      return zero ? zero.value : options[0]?.value ?? 0;
    }
    case 'struct':
      return Object.fromEntries(node.fields.map((f) => [f.name, defaultFor(f)]));
    case 'bitfield':
      return Object.fromEntries(node.bits.map((b) => [b.name, defaultFor(b)]));
    case 'array':
      return node.length === 'fixed'
        ? Array.from({ length: node.size ?? 0 }, () => defaultFor(node.item))
        : [];
    default:
      return 0;
  }
}

/** Initial form state for a whole message schema. */
export function defaultPayload(schema) {
  return Object.fromEntries(schema.fields.map((f) => [f.name, defaultFor(f)]));
}

/**
 * Names of scalar fields that are really an array's length counter.
 *
 * IRS does NOT write these itself -- `ArrayField.to_bytes` just iterates the
 * list, while `from_bytes` reads exactly `getattr(instance, count_field)`
 * items. So the count is derived from the list, and the UI renders it
 * read-only rather than letting the user desync the wire format by hand.
 */
export function derivedCounters(fields) {
  return new Set(
    fields
      .filter((f) => f.kind === 'array' && f.length === 'counted' && f.count_field)
      .map((f) => f.count_field),
  );
}

/** The array a given counter field belongs to, for the read-only display. */
export function counterSource(fields, counterName) {
  return fields.find(
    (f) => f.kind === 'array' && f.length === 'counted' && f.count_field === counterName,
  );
}

//: The framing header's DataLength is uint16 (`struct.Struct("<BHH")` in
//: core/connections/framing.py), so no payload can exceed this many bytes --
//: the ceiling on a dynamic array that has no counter to bound it.
const MAX_PAYLOAD_BYTES = 0xffff;

/**
 * How many items a growable array may hold, or `null` when nothing bounds it.
 *
 * Two different ceilings, because the two array kinds are limited by different
 * things:
 *
 *   counted  its length travels in a SIBLING scalar, so the array can never be
 *            longer than that field can count -- a UInt8 counter caps it at
 *            255 no matter how much frame is left. Going past that does not
 *            error, it silently truncates on the receiving side, which is the
 *            worst possible failure mode.
 *   dynamic  no counter exists (it fills the rest of the frame), so the only
 *            real bound is what the uint16 DataLength can describe.
 *
 * `fields` is the array's SIBLING list -- the counter is a sibling, not a
 * child, so this cannot be derived from the array node alone.
 */
export function maxArrayItems(node, fields) {
  if (node.kind !== 'array' || node.length === 'fixed') return null;

  if (node.length === 'counted') {
    const counter = (fields ?? []).find((f) => f.name === node.count_field);
    if (typeof counter?.max === 'number') return counter.max;
  }

  const itemBytes = sizeOf(node.item, defaultFor(node.item));
  if (itemBytes > 0) return Math.floor(MAX_PAYLOAD_BYTES / itemBytes);
  return null;
}

/**
 * Wire size in bytes of one field given its current value.
 *
 * Computed from the payload rather than the schema alone because a message's
 * length is not fixed: dynamic and counted arrays contribute
 * `items x item size`, so the total only exists once you know how many items
 * there are. Scalars/enums carry `byte_size` from the struct format char, and
 * a bitfield carries the width of its packed integer (its @baseType), which is
 * not the sum of its bit widths.
 */
function sizeOf(node, value) {
  switch (node.kind) {
    case 'scalar':
    case 'enum':
      return node.byte_size ?? 0;
    case 'bitfield':
      return node.byte_size ?? 0;
    case 'struct':
      return node.fields.reduce((sum, f) => sum + sizeOf(f, value?.[f.name]), 0);
    case 'array': {
      const items = Array.isArray(value) ? value : [];
      return items.reduce((sum, item) => sum + sizeOf(node.item, item), 0);
    }
    default:
      return 0;
  }
}

/** Total payload size for a message, excluding the framing header. */
export function payloadSize(fields, payload) {
  return fields.reduce((sum, field) => sum + sizeOf(field, payload?.[field.name]), 0);
}

/**
 * How many characters wide a field's input needs to be.
 *
 * Every scalar used to render full-width, one per row, whatever it could hold:
 * a `UInt8` bounded at 255 got the same ~900px box as a `Double`, so a
 * seven-field message scrolled while holding seven zeros. The schema already
 * says exactly how much room each value needs -- `max` gives the digit count
 * and `numeric` says whether a decimal point and exponent are possible -- so
 * the control can simply be that wide.
 *
 * Returned in `ch`, and deliberately generous: a value being typed can be
 * momentarily longer than the field's range allows (a leading minus, a pasted
 * number about to be trimmed), and a control that clips mid-edit is worse than
 * one with a little slack.
 */
export function controlChars(node) {
  if (node.kind === 'enum') return 16;
  if (node.numeric === 'float') return 16;
  if (typeof node.max === 'number') {
    const digits = String(node.max).length;
    const sign = typeof node.min === 'number' && node.min < 0 ? 1 : 0;
    return Math.min(16, Math.max(6, digits + sign + 2));
  }
  if (typeof node.bits === 'number') return Math.max(6, String((1 << node.bits) - 1).length + 2);
  return 10;
}

/**
 * Byte offsets for every leaf in a message, flattened in wire order.
 *
 * The byte ruler needs to map a field to a byte range and back, and that map is
 * pure arithmetic over the schema plus the current payload -- `sizeOf` above
 * already computes each node's width, so walking the tree in `_fields_` order
 * and accumulating gives the offset of every leaf. It depends on the payload
 * because a message's length is not fixed: a counted or dynamic array
 * contributes `items x item size`, so nothing after one has a knowable offset
 * until you know how many items there are.
 *
 * `path` is the dotted name shown to the user (`Position.Latitude`,
 * `Emitters[0].PowerDbm`); `key` is the same path as an array, which is what
 * the renderer compares against to know whether a given field is the one lit.
 */
export function leafOffsets(fields, payload) {
  const out = [];
  let offset = 0;

  const walk = (node, value, path) => {
    switch (node.kind) {
      case 'struct':
        for (const child of node.fields) {
          walk(child, value?.[child.name], [...path, child.name]);
        }
        return;
      case 'array': {
        const items = Array.isArray(value) ? value : [];
        items.forEach((item, index) => {
          // The index belongs to the LAST segment, not a segment of its own --
          // `Emitters[0]`, so the label reads the way the field is written.
          const head = path.slice(0, -1);
          const tail = `${path[path.length - 1]}[${index}]`;
          walk(node.item, item, [...head, tail]);
        });
        return;
      }
      case 'bitfield':
      case 'scalar':
      case 'enum': {
        const size = sizeOf(node, value);
        out.push({
          path: path.join('.'),
          key: path.join('.'),
          kind: node.kind,
          node,
          value,
          offset,
          size,
        });
        offset += size;
        return;
      }
      default:
        return;
    }
  };

  for (const field of fields) walk(field, payload?.[field.name], [field.name]);
  return out;
}
