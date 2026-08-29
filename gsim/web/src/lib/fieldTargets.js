/**
 * One message's schema -> the fields a rule may address.
 *
 * Four pickers are built from this, and they are all the same question asked of
 * the same shape: a received filter's keep/drop rule, a behaviour's trigger
 * condition, and both ends of a behaviour's value forwarding.
 *
 * The client-side mirror of `core_gateway/schema.py`'s `field_targets`, kept
 * only so a dialog can populate its pickers without a second round trip. The
 * server re-validates every path against its own copy before storing it (see
 * `api/routes/filters.py` and `api/routes/behaviours.py`), so this is not a
 * trust boundary -- the two must simply agree, with the server as tiebreaker.
 * Same arrangement, and the same reasoning, as `lib/schema.js`'s mirror of
 * IRS's `fill()`.
 *
 * Two questions per field, because the halves of a rule address different
 * things:
 *
 *   ruleOk    can a keep/drop rule compare this? Only a single decoded value:
 *             a scalar, an enum, or one bit of a bitfield.
 *   (all)     can the change trigger watch this? Anything listed here --
 *             including a whole struct, a whole bitfield, or an array, because
 *             the trigger compares by value and `==` on a list is exactly the
 *             right question.
 *
 * **The walk never enters an array's item.** A dotted path cannot name one of
 * 35 elements, so an array is offered as a change subject (compare the whole
 * list) and nothing inside it is offered at all.
 *
 * Paths use the dotted grammar `leafOffsets` already established
 * (`Position.Latitude`), so a field is spelt one way across the whole UI. This
 * is deliberately a second walk rather than a parameterisation of that one:
 * `leafOffsets` treats a bitfield as a single leaf because it occupies one span
 * of bytes, while a filter has to descend into its bits, since `to_dict` emits
 * one key per bit.
 */

/** Field kinds that carry a single value an operator can be applied to. */
const COMPARABLE = new Set(['scalar', 'enum']);

export function fieldTargets(schema) {
  const targets = [];

  const emit = (path, node, ruleOk) => {
    targets.push({
      path: path.join('.'),
      name: path[path.length - 1],
      depth: path.length - 1,
      kind: node.kind ?? 'scalar',
      ruleOk,
      options: node.options,
      dtype: node.dtype,
      enumName: node.enum,
      min: node.min,
      max: node.max,
      itemLabel: node.item_label,
      length: node.length,
    });
  };

  const walk = (node, path) => {
    if (node.kind === 'struct') {
      emit(path, node, false);
      (node.fields ?? []).forEach((child) => walk(child, [...path, child.name]));
      return;
    }
    if (node.kind === 'bitfield') {
      emit(path, node, false);
      // A bit IS comparable: `BitField.to_dict` emits one key per bit, with
      // enum bits as member names, so `Area.fr` resolves against the payload.
      (node.bits ?? []).forEach((bit) => emit([...path, bit.name], bit, true));
      return;
    }
    if (node.kind === 'array') {
      emit(path, node, false);      // watchable, never rule-addressable
      return;
    }
    emit(path, node, COMPARABLE.has(node.kind));
  };

  (schema?.fields ?? []).forEach((field) => walk(field, [field.name]));
  return targets;
}

/**
 * What a rule's value control should be, for one target.
 *
 * An enum decodes to its MEMBER NAME, so its control is a list of names and
 * never a number box -- typing `2` for `ON` produces a rule that silently never
 * matches, which is the single easiest way to make this feature look broken.
 */
export function valueControl(target) {
  if (!target) return { type: 'number' };
  if (target.kind === 'enum') {
    return { type: 'enum', options: target.options ?? [] };
  }
  return { type: 'number', min: target.min, max: target.max };
}

/**
 * The operators that mean anything for one target.
 *
 * Ordering an enum would compare member names alphabetically, which is not what
 * anyone means by `<`. Mirrors the server's own refusal so the control never
 * offers what the PUT would reject.
 */
export const EQUALITY_OPS = [
  { value: '==', label: 'is' },
  { value: '!=', label: 'is not' },
];
export const NUMERIC_OPS = [
  ...EQUALITY_OPS,
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
];

export function operatorsFor(target) {
  return target?.kind === 'enum' ? EQUALITY_OPS : NUMERIC_OPS;
}
