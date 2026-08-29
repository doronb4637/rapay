/**
 * The payload, as bytes.
 *
 * A client-side mirror of what IRS's encoder does, in the same spirit as
 * `defaultFor`/`defaultPayload` in `schema.js`: not a trust boundary, and never
 * what actually goes on the wire -- `core` re-encodes every payload from the
 * dict the form submits, exactly as it did before this file existed. This
 * exists so the Inspector can SHOW the bytes while you build a message, which
 * is the one thing a simulator for a binary protocol was not doing.
 *
 * Why it can be a mirror at all: `Structure.to_bytes` composes one
 * `struct.Struct` per field and concatenates, with no padding between them, so
 * a field's wire position is just the sum of the widths before it. Everything
 * else needed is already in the schema -- `byte_size` and `numeric` come from
 * the field's own packer, and `endian` is read off that packer's format string
 * rather than assumed (see `core_gateway/schema.py`).
 *
 * If the two ever disagree, `core` is right and this is the bug.
 */
import { leafOffsets } from './schema';

/** Enums arrive as a NUMBER while composing and as the member NAME when
 *  inspecting a received message (`EnumField.to_dict` returns `value.name`), or
 *  as null when unset -- an enum with no 0 member, which `EnumField.to_bytes`
 *  writes as 0. All three have to reach `struct` as the number, which is why the
 *  `Number(value)` fallback below is load-bearing rather than incidental:
 *  `Number(null)` is 0, exactly what goes on the wire. */
function enumValue(value, options = []) {
  if (typeof value === 'number') return value;
  const byName = options.find((option) => option.name === value);
  if (byName) return byName.value;
  const asNumber = Number(value);
  return Number.isFinite(asNumber) ? asNumber : 0;
}

/** A bitfield is ONE packed integer on the wire, not one value per bit: each
 *  entry contributes `value << shift`. Multiplication rather than `<<` because
 *  JS bitwise operators truncate to 32 bits, and a `UInt32` bitfield with a
 *  high shift would silently wrap. */
function packBits(node, value) {
  const current = value ?? {};
  let packed = 0;
  for (const bit of node.bits ?? []) {
    const raw = bit.kind === 'enum' ? enumValue(current[bit.name], bit.options) : current[bit.name];
    const numeric = Number(raw);
    if (!Number.isFinite(numeric)) continue;
    const mask = Math.pow(2, bit.bits) - 1;
    packed += (Math.trunc(numeric) & mask) * Math.pow(2, bit.shift);
  }
  return packed;
}

function writeLeaf(view, offset, leaf) {
  const { node, value, size } = leaf;
  const little = (node.endian ?? 'little') !== 'big';

  if (node.kind === 'bitfield') {
    const packed = packBits(node, value);
    if (size === 1) view.setUint8(offset, packed & 0xff);
    else if (size === 2) view.setUint16(offset, packed % 0x10000, little);
    else if (size === 4) view.setUint32(offset, packed % 0x100000000, little);
    else if (size === 8) view.setBigUint64(offset, BigInt(Math.trunc(packed)), little);
    return;
  }

  const raw = node.kind === 'enum' ? enumValue(value, node.options) : value;
  const numeric = Number(raw);
  // Blank means zero, which is what the server fills in on send -- so the ruler
  // shows the bytes that would actually go out, not a gap.
  const safe = Number.isFinite(numeric) ? numeric : 0;

  if (node.numeric === 'float') {
    if (size === 4) view.setFloat32(offset, safe, little);
    else view.setFloat64(offset, safe, little);
    return;
  }

  const signed = typeof node.min === 'number' && node.min < 0;
  const whole = Math.trunc(safe);
  if (size === 1) signed ? view.setInt8(offset, whole) : view.setUint8(offset, whole & 0xff);
  else if (size === 2) signed ? view.setInt16(offset, whole, little) : view.setUint16(offset, whole & 0xffff, little);
  else if (size === 4) signed ? view.setInt32(offset, whole, little) : view.setUint32(offset, whole >>> 0, little);
  else if (size === 8) {
    const big = BigInt(whole);
    signed ? view.setBigInt64(offset, big, little) : view.setBigUint64(offset, big < 0n ? 0n : big, little);
  }
}

/**
 * `{ bytes, leaves }` for one message: the encoded payload and, for each leaf,
 * where it landed. Both halves come from one walk so a field's highlight can
 * never point at the wrong bytes.
 *
 * A field this module cannot pack (an unsupported IRS kind, a width `struct`
 * has no code for) leaves its bytes zero rather than throwing -- the ruler is a
 * read-out, and a read-out that disappears because one field is unusual is
 * worse than one showing zeros where it cannot know better.
 */
export function encodePayload(fields, payload) {
  const leaves = leafOffsets(fields, payload);
  const length = leaves.reduce((sum, leaf) => Math.max(sum, leaf.offset + leaf.size), 0);
  const buffer = new ArrayBuffer(length);
  const view = new DataView(buffer);

  for (const leaf of leaves) {
    if (leaf.size <= 0 || leaf.offset + leaf.size > length) continue;
    try {
      writeLeaf(view, leaf.offset, leaf);
    } catch {
      /* leave this field's bytes zero; see the note above */
    }
  }

  return { bytes: new Uint8Array(buffer), leaves };
}

/** `2A` -- one byte, always two digits, always upper case. */
export function hexByte(byte) {
  return byte.toString(16).toUpperCase().padStart(2, '0');
}
