/**
 * The recursive heart of the Inspector.
 *
 * One component renders every IRS field kind and renders *itself* for nested
 * structs and array items -- so struct-in-array-in-struct needs no special
 * case, however deep the layout goes.
 *
 * State flows by composition, not by paths: each level takes `value` plus an
 * `onChange(next)` and hands its children a narrowed pair. A leaf edit rebuilds
 * only its own branch, and the root's state *is* the payload dict the API
 * expects -- submitting is `JSON.stringify(value)`, no serialisation step.
 */
import React from 'react';
import { Braces, Hash, List, Plus, ToggleLeft, Trash2 } from 'lucide-react';
import { Badge, Field, IconButton, Input, Select, cx } from './ui';
import { counterSource, defaultFor, derivedCounters, maxArrayItems } from '../lib/schema';

export default function FieldRenderer({
  node, value, onChange, readOnly = false, depth = 0, maxItems = null,
}) {
  switch (node.kind) {
    case 'enum':
      return <EnumField node={node} value={value} onChange={onChange} readOnly={readOnly} />;
    case 'struct':
      return <StructField node={node} value={value} onChange={onChange} readOnly={readOnly} depth={depth} />;
    case 'bitfield':
      return <BitFieldGroup node={node} value={value} onChange={onChange} readOnly={readOnly} depth={depth} />;
    case 'array':
      return (
        <ArrayField
          node={node} value={value} onChange={onChange} readOnly={readOnly}
          depth={depth} maxItems={maxItems}
        />
      );
    case 'scalar':
      return <ScalarField node={node} value={value} onChange={onChange} readOnly={readOnly} />;
    default:
      return (
        <Field label={node.name} type="unsupported">
          <div className="rounded-md border border-amber-900/60 bg-amber-950/30 px-2.5 py-1.5 text-[11px] text-amber-300">
            No widget for this IRS field type ({node.python_type}).
          </div>
        </Field>
      );
  }
}

/* ------------------------------------------------------------------ */
/* Nesting container                                                   */
/* ------------------------------------------------------------------ */
/**
 * A struct/bitfield/array field renders as ONE of these. At depth 0 (a direct
 * child of the message card) it is a full bordered card, worth marking as its
 * own thing. Any deeper and it renders flat instead -- a header row plus a
 * left indent guide, no border/background/padding of its own -- so it fills
 * the space inside whichever card it already lives in rather than stacking a
 * new box on top of it. Without this, three levels of nesting was three
 * levels of border+padding, and the actual fields drowned in chrome.
 */
function Group({ icon: Icon, name, type, meta, depth, tone = 'slate', children, actions, badge }) {
  const iconTone = tone === 'sky' ? 'text-sky-500' : 'text-slate-500';

  if (depth > 0) {
    return (
      <div className="flex min-w-0 flex-col gap-2">
        <div
          className={cx(
            'flex items-center gap-2 border-b pb-1.5',
            tone === 'sky' ? 'border-sky-900/40' : 'border-slate-800/70',
          )}
        >
          {badge}
          {Icon && <Icon size={11} className={cx('shrink-0', iconTone)} />}
          {name && <span className="truncate text-[11px] font-semibold text-slate-300">{name}</span>}
          {type && <Badge tone="slate">{type}</Badge>}
          {meta && <span className="truncate text-[10px] text-slate-500">{meta}</span>}
          <div className="ml-auto flex shrink-0 items-center gap-1">{actions}</div>
        </div>
        <div
          className={cx(
            'flex flex-col gap-2.5 border-l pl-3',
            tone === 'sky' ? 'border-sky-900/30' : 'border-slate-800/60',
          )}
        >
          {children}
        </div>
      </div>
    );
  }

  return (
    <div
      className={cx(
        'rounded-lg border',
        tone === 'sky' ? 'border-sky-900/50 bg-sky-950/20' : 'border-slate-700/70 bg-slate-900/40',
      )}
    >
      <div className="flex items-center gap-2 border-b border-slate-800/80 px-2.5 py-1.5">
        {badge}
        {Icon && <Icon size={12} className={cx('shrink-0', iconTone)} />}
        {name && <span className="truncate text-[11px] font-semibold text-slate-200">{name}</span>}
        {type && <Badge tone="slate">{type}</Badge>}
        {meta && <span className="truncate text-[10px] text-slate-500">{meta}</span>}
        <div className="ml-auto flex shrink-0 items-center gap-1">{actions}</div>
      </div>
      <div className="flex flex-col gap-2.5 p-2.5">{children}</div>
    </div>
  );
}

/** Small "#N" chip -- an array item's position, shown in its merged header. */
function IndexBadge({ index }) {
  return (
    <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-slate-800 px-1 py-px font-mono text-[10px] text-slate-400">
      <Hash size={9} className="text-slate-600" />
      {index}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Scalars                                                             */
/* ------------------------------------------------------------------ */
function ScalarField({ node, value, onChange, readOnly, hint }) {
  const isFloat = node.numeric === 'float';
  const type = node.dtype ?? (node.bits ? `${node.bits} bit` : undefined);

  return (
    <Field label={node.name} type={type} hint={hint} htmlFor={`f-${node.name}`}>
      <Input
        id={`f-${node.name}`}
        type="number"
        value={value ?? ''}
        min={node.min}
        max={node.max}
        step={isFloat ? 'any' : 1}
        disabled={readOnly}
        placeholder="0"
        onChange={(event) => {
          const raw = event.target.value;
          // Blank stays blank so a field can be cleared; the server fills
          // absent/blank values with 0 at send time.
          if (raw === '') return onChange('');
          onChange(isFloat ? parseFloat(raw) : parseInt(raw, 10));
        }}
      />
    </Field>
  );
}

/**
 * An enum reaches us as a NUMBER when composing (our own default/edit) but as
 * the member NAME when inspecting a received message -- IRS's
 * `EnumField.to_dict` returns `value.name`, and `BitField.to_dict` does the
 * same for its enum bits. A name string matches none of the numeric <option>
 * values, so the browser would silently fall back to showing the first option
 * (typically the 0/UNKNOWN member) and misreport the message. Normalise both
 * forms to the option's numeric value -- shared by the full `EnumField` and
 * the compact array-item select, which face the same two input shapes.
 */
function resolveEnumValue(value, options) {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'number') return value;
  const byName = options.find((option) => option.name === value);
  if (byName) return byName.value;
  const asNumber = Number(value);
  return Number.isFinite(asNumber) ? asNumber : '';
}

/* ------------------------------------------------------------------ */
/* Enums -> styled <select>                                            */
/* ------------------------------------------------------------------ */
function EnumField({ node, value, onChange, readOnly }) {
  const resolved = React.useMemo(
    () => resolveEnumValue(value, node.options),
    [value, node.options],
  );

  // Emits the numeric value: the canonical wire form, matching the defaults on
  // both sides. (IRS also accepts the member NAME as a string.)
  return (
    <Field label={node.name} type={node.enum} htmlFor={`f-${node.name}`}>
      <Select
        id={`f-${node.name}`}
        value={resolved}
        disabled={readOnly}
        onChange={(event) => onChange(parseInt(event.target.value, 10))}
      >
        {node.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.name} · {option.value}
          </option>
        ))}
      </Select>
    </Field>
  );
}

/* ------------------------------------------------------------------ */
/* Nested structs                                                      */
/* ------------------------------------------------------------------ */
function StructField({ node, value, onChange, readOnly, depth, indexBadge, extraActions }) {
  return (
    <Group
      icon={Braces}
      name={node.name}
      type={node.struct}
      depth={depth}
      badge={indexBadge}
      actions={extraActions}
    >
      <FieldList
        fields={node.fields}
        value={value ?? {}}
        onChange={onChange}
        readOnly={readOnly}
        depth={depth + 1}
      />
    </Group>
  );
}

/**
 * A list of sibling fields -- shared by the struct renderer and the form root.
 *
 * This is the level that knows about counted arrays, because an array's length
 * field is its SIBLING, not its child.
 */
export function FieldList({ fields, value, onChange, readOnly, depth = 0 }) {
  const counters = derivedCounters(fields);

  return (
    <>
      {fields.map((field) => {
        const setField = (next) => onChange({ ...value, [field.name]: next });

        if (counters.has(field.name) && field.kind === 'scalar') {
          // Derived: show the live list length, read-only. The server recomputes
          // it on send regardless, so a typed value would only silently lose.
          const source = counterSource(fields, field.name);
          return (
            <ScalarField
              key={field.name}
              node={field}
              value={(value[source.name] ?? []).length}
              onChange={() => {}}
              readOnly
              hint={`auto — counts ${source.name}`}
            />
          );
        }

        return (
          <FieldRenderer
            key={field.name}
            node={field}
            value={value[field.name]}
            onChange={setField}
            readOnly={readOnly}
            depth={depth}
            // Derived HERE because a counted array's limit lives in a sibling
            // field -- the array node alone cannot see it.
            maxItems={maxArrayItems(field, fields)}
          />
        );
      })}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Bitfields                                                           */
/* ------------------------------------------------------------------ */
function BitFieldGroup({ node, value, onChange, readOnly, depth, indexBadge, extraActions }) {
  const current = value ?? {};

  return (
    <Group
      icon={ToggleLeft}
      name={node.name}
      type={node.struct}
      depth={depth}
      badge={indexBadge}
      actions={extraActions}
    >
      <div className="grid grid-cols-2 gap-2.5">
        {node.bits.map((bit) => (
          <FieldRenderer
            key={bit.name}
            node={bit}
            value={current[bit.name]}
            onChange={(next) => onChange({ ...current, [bit.name]: next })}
            readOnly={readOnly}
            depth={depth + 1}
          />
        ))}
      </div>
    </Group>
  );
}

/* ------------------------------------------------------------------ */
/* Arrays -- the "+ Add <Struct>" case                                 */
/* ------------------------------------------------------------------ */
function ArrayField({ node, value, onChange, readOnly, depth, maxItems = null }) {
  const items = value ?? [];
  const growable = node.length !== 'fixed' && !readOnly;
  // At the ceiling the array cannot take another item -- for a counted array
  // that is its counter's width (a UInt8 count stops at 255), and going past it
  // does not raise, it silently truncates on the receiving side. Blocking the
  // button is the only place that failure is visible.
  const full = maxItems !== null && items.length >= maxItems;

  // "Increments the internal array size variable by 1 and dynamically adds a
  // new set of fields": appending a fully-defaulted item does both -- the
  // rendered fields follow from the item's presence, and a counted array's
  // sibling counter is derived from this length.
  const addItem = () => {
    if (full) return;
    onChange([...items, defaultFor(node.item)]);
  };
  const removeItem = (index) => onChange(items.filter((_, i) => i !== index));
  const setItem = (index, next) => onChange(items.map((item, i) => (i === index ? next : item)));

  const meta =
    node.length === 'counted'
      ? `counted by ${node.count_field}${maxItems !== null ? ` · max ${maxItems}` : ''}`
      : node.length === 'dynamic'
        ? `fills remaining frame${maxItems !== null ? ` · max ${maxItems}` : ''}`
        : `fixed ${node.size}`;

  return (
    <Group
      icon={List}
      name={node.name}
      type={`${node.item_label}[${items.length}]`}
      meta={meta}
      depth={depth}
      tone="sky"
      actions={
        growable && (
          <button
            type="button"
            onClick={addItem}
            disabled={full}
            title={
              full
                ? node.length === 'counted'
                  ? `Limit reached — ${node.count_field} can only count ${maxItems} items`
                  : `Limit reached — ${maxItems} items fill the maximum frame`
                : undefined
            }
            className={cx(
              'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10px] font-medium transition-colors',
              'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500',
              full
                ? 'cursor-not-allowed border-slate-700 bg-slate-800/60 text-slate-500'
                : 'border-sky-800/70 bg-sky-950/50 text-sky-300 hover:border-sky-600 hover:bg-sky-900/60 hover:text-sky-200',
            )}
          >
            <Plus size={11} />
            {full ? `Max ${maxItems}` : `Add ${node.item_label}`}
          </button>
        )
      }
    >
      {items.length === 0 ? (
        <p className="px-1 py-2 text-center text-[11px] text-slate-600">
          {growable ? `Empty — use “Add ${node.item_label}”.` : 'Empty.'}
        </p>
      ) : node.item.kind === 'struct' || node.item.kind === 'bitfield' ? (
        // A struct/bitfield item already renders its own header+indent (see
        // `Group`) -- wrapping it in a second bordered box would just stack a
        // duplicate header on top of it. Instead its index and remove button
        // are handed to that same header via `indexBadge`/`extraActions`, so
        // one item is one section, not two.
        <div className="flex flex-col gap-3">
          {items.map((item, index) => {
            const ItemComponent = node.item.kind === 'struct' ? StructField : BitFieldGroup;
            return (
              <ItemComponent
                key={index}
                node={{ ...node.item, name: null }}
                value={item}
                onChange={(next) => setItem(index, next)}
                readOnly={readOnly}
                depth={depth + 1}
                indexBadge={<IndexBadge index={index} />}
                extraActions={
                  growable && (
                    <IconButton
                      icon={Trash2}
                      title={`Remove ${node.item_label} ${index}`}
                      variant="danger"
                      onClick={() => removeItem(index)}
                      className="!h-5 !w-5"
                    />
                  )
                }
              />
            );
          })}
        </div>
      ) : node.item.kind === 'scalar' || node.item.kind === 'enum' ? (
        // A plain number/enum needs no box of its own -- a whole bordered
        // card per byte was most of what made a 9-item array feel heavy. A
        // wrapped row of small chips shows the same values in a fraction of
        // the height.
        <div className="flex flex-wrap gap-1.5">
          {items.map((item, index) => (
            <CompactArrayItem
              key={index}
              node={node.item}
              value={item}
              onChange={(next) => setItem(index, next)}
              onRemove={growable ? () => removeItem(index) : null}
              readOnly={readOnly}
              index={index}
            />
          ))}
        </div>
      ) : (
        items.map((item, index) => (
          <div
            key={index}
            className="animate-row-in rounded-md border border-slate-800 bg-slate-950/40"
          >
            <div className="flex items-center gap-2 border-b border-slate-800/70 px-2 py-1">
              <Hash size={10} className="text-slate-600" />
              <span className="font-mono text-[10px] text-slate-500">
                {node.item_label} [{index}]
              </span>
              {growable && (
                <IconButton
                  icon={Trash2}
                  title={`Remove ${node.item_label} ${index}`}
                  variant="danger"
                  onClick={() => removeItem(index)}
                  className="ml-auto !h-5 !w-5"
                />
              )}
            </div>
            <div className="flex flex-col gap-2.5 p-2">
              <FieldRenderer
                node={{ ...node.item, name: `${node.name}[${index}]` }}
                value={item}
                onChange={(next) => setItem(index, next)}
                readOnly={readOnly}
                depth={depth + 1}
              />
            </div>
          </div>
        ))
      )}
    </Group>
  );
}

/**
 * One item of an array of plain scalars/enums -- a small chip instead of a
 * full field card, so an N-byte array reads as one dense row instead of N
 * stacked boxes. Mirrors `ScalarField`/`EnumField`'s value handling, just
 * without the label/hint chrome those carry (the array's own header already
 * says what these are; only the index is per-item information).
 */
function CompactArrayItem({ node, value, onChange, onRemove, readOnly, index }) {
  const isEnum = node.kind === 'enum';
  const isFloat = node.numeric === 'float';
  const resolved = React.useMemo(
    () => (isEnum ? resolveEnumValue(value, node.options) : value),
    [isEnum, value, node.options],
  );

  return (
    <div className="flex items-center gap-1 rounded-md border border-slate-800 bg-slate-950/40 py-1 pl-1.5 pr-1">
      <span className="font-mono text-[9px] text-slate-600">{index}</span>
      {isEnum ? (
        <select
          value={resolved}
          disabled={readOnly}
          title={node.enum}
          onChange={(event) => onChange(parseInt(event.target.value, 10))}
          className={cx(
            'rounded border border-slate-700 bg-slate-950/60 py-0.5 pl-1 pr-5 text-[10px] text-slate-100',
            'cursor-pointer appearance-none transition-colors hover:border-slate-600',
            'focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500/40',
            'disabled:cursor-not-allowed disabled:text-slate-500',
          )}
        >
          {node.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.name}
            </option>
          ))}
        </select>
      ) : (
        <input
          type="number"
          value={resolved ?? ''}
          min={node.min}
          max={node.max}
          step={isFloat ? 'any' : 1}
          disabled={readOnly}
          placeholder="0"
          title={node.dtype}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === '') return onChange('');
            onChange(isFloat ? parseFloat(raw) : parseInt(raw, 10));
          }}
          className={cx(
            'w-12 rounded border border-slate-700 bg-slate-950/60 px-1 py-0.5 text-center font-mono text-[10px] text-slate-100',
            'transition-colors hover:border-slate-600',
            'focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500/40',
            'disabled:cursor-not-allowed disabled:text-slate-500',
          )}
        />
      )}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          title={`Remove item ${index}`}
          className="grid h-4 w-4 shrink-0 place-items-center rounded text-slate-600 transition-colors hover:bg-rose-950/60 hover:text-rose-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
        >
          <Trash2 size={9} />
        </button>
      )}
    </div>
  );
}
