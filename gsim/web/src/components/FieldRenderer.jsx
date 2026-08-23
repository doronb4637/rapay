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
 *
 * Two things travel alongside that state, and both are read-only passengers:
 *
 *   path   the dotted name of the field being rendered, assembled on the way
 *          down so a leaf can identify itself to the byte ruler. It is built
 *          with exactly the rules `leafOffsets` uses (`Emitters[0].PowerDbm`),
 *          because the two have to agree for a highlight to point at the right
 *          bytes.
 *   focus  which path is currently lit, in context rather than in props --
 *          it changes on every pointer move and every input focus, and
 *          threading it through six components by hand would put a prop on
 *          every one of them for the benefit of two.
 */
import React from 'react';
import { Braces, Hash, List, Plus, ToggleLeft, Trash2 } from 'lucide-react';
import { Badge, Field, FieldRow, IconButton, Input, Select, ValueCell, cx } from './ui';
import { counterSource, controlChars, defaultFor, derivedCounters, maxArrayItems } from '../lib/schema';

/**
 * Which field the byte ruler is currently lighting, and how to change it.
 *
 * Defaulted to an inert pair so `FieldRenderer` still works anywhere outside a
 * provider -- the highlight simply does nothing, which is the correct
 * behaviour for a form with no ruler above it.
 */
export const FieldFocus = React.createContext({ activePath: null, setActivePath: () => {} });

/** The child path for an array item, matching `leafOffsets`: the index belongs
 *  to the last segment (`Emitters[0]`), not to a segment of its own. */
export function itemPath(path, index) {
  if (path.length === 0) return [`[${index}]`];
  return [...path.slice(0, -1), `${path[path.length - 1]}[${index}]`];
}

export default function FieldRenderer({
  node, value, onChange, readOnly = false, depth = 0, maxItems = null, path = [],
}) {
  switch (node.kind) {
    case 'enum':
      return <EnumField node={node} value={value} onChange={onChange} readOnly={readOnly} path={path} />;
    case 'struct':
      return <StructField node={node} value={value} onChange={onChange} readOnly={readOnly} depth={depth} path={path} />;
    case 'bitfield':
      return <BitFieldGroup node={node} value={value} onChange={onChange} readOnly={readOnly} depth={depth} path={path} />;
    case 'array':
      return (
        <ArrayField
          node={node} value={value} onChange={onChange} readOnly={readOnly}
          depth={depth} maxItems={maxItems} path={path}
        />
      );
    case 'scalar':
      return <ScalarField node={node} value={value} onChange={onChange} readOnly={readOnly} path={path} />;
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

/**
 * Wire one leaf to the byte ruler.
 *
 * Hover and focus both light it, because the two ways of reaching a field --
 * pointing at it and tabbing to it -- should read the same. Returns the props a
 * `FieldRow` needs plus whether it is currently the lit one.
 */
function useFieldFocus(path) {
  const { activePath, setActivePath } = React.useContext(FieldFocus);
  const key = path.join('.');
  return {
    active: key !== '' && activePath === key,
    rowProps: {
      onPointerEnter: () => setActivePath(key || null),
      onPointerLeave: () => setActivePath(null),
    },
    focusProps: {
      onFocus: () => setActivePath(key || null),
      onBlur: () => setActivePath(null),
    },
  };
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
      <div className="flex min-w-0 flex-col gap-1">
        <div
          className={cx(
            'flex items-center gap-2 border-b pb-1',
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
            'flex flex-col border-l pl-2',
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
      <div className="flex items-center gap-2 border-b border-slate-800/80 px-2.5 py-1">
        {badge}
        {Icon && <Icon size={12} className={cx('shrink-0', iconTone)} />}
        {name && <span className="truncate text-[11px] font-semibold text-slate-200">{name}</span>}
        {type && <Badge tone="slate">{type}</Badge>}
        {meta && <span className="truncate text-[10px] text-slate-500">{meta}</span>}
        <div className="ml-auto flex shrink-0 items-center gap-1">{actions}</div>
      </div>
      <div className="flex flex-col p-1.5">{children}</div>
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
/** An integer's hex form, at its own width -- `0x2A` for a byte, `0x002A` for
 *  a uint16 -- so the number of digits says how wide the field is. */
function hexFor(node, value) {
  if (node.numeric === 'float') return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  const digits = Math.max(2, (node.byte_size ?? 1) * 2);
  const unsigned = numeric < 0 ? numeric >>> 0 : numeric;
  return `0x${Math.trunc(unsigned).toString(16).toUpperCase().padStart(digits, '0')}`;
}

function ScalarField({ node, value, onChange, readOnly, hint, path = [] }) {
  const isFloat = node.numeric === 'float';
  const type = node.dtype ?? (node.bits ? `${node.bits} bit` : undefined);
  const { active, rowProps, focusProps } = useFieldFocus(path);
  const id = `f-${path.join('-') || node.name}`;

  return (
    <FieldRow
      label={node.name}
      type={type}
      hint={hint}
      htmlFor={readOnly ? undefined : id}
      chars={controlChars(node)}
      active={active}
      {...rowProps}
    >
      {readOnly ? (
        <ValueCell
          text={value === '' || value === null || value === undefined ? '—' : String(value)}
          hex={hexFor(node, value)}
          title={type}
        />
      ) : (
        <Input
          id={id}
          type="number"
          value={value ?? ''}
          min={node.min}
          max={node.max}
          step={isFloat ? 'any' : 1}
          placeholder="0"
          className="!px-2 !py-1 text-right"
          {...focusProps}
          onChange={(event) => {
            const raw = event.target.value;
            // Blank stays blank so a field can be cleared; the server fills
            // absent/blank values with 0 at send time.
            if (raw === '') return onChange('');
            onChange(isFloat ? parseFloat(raw) : parseInt(raw, 10));
          }}
        />
      )}
    </FieldRow>
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
function EnumField({ node, value, onChange, readOnly, path = [] }) {
  const resolved = React.useMemo(
    () => resolveEnumValue(value, node.options),
    [value, node.options],
  );
  const { active, rowProps, focusProps } = useFieldFocus(path);
  const id = `f-${path.join('-') || node.name}`;
  const member = node.options.find((option) => option.value === resolved);

  // Emits the numeric value: the canonical wire form, matching the defaults on
  // both sides. (IRS also accepts the member NAME as a string.)
  return (
    <FieldRow
      label={node.name}
      type={node.enum}
      htmlFor={readOnly ? undefined : id}
      chars={controlChars(node)}
      active={active}
      {...rowProps}
    >
      {readOnly ? (
        // A decoded enum is a name and a number, not a dropdown you cannot
        // open -- the chevron on a disabled <select> promised an interaction
        // that could never happen.
        <ValueCell
          text={member ? `${member.name} · ${member.value}` : String(value ?? '—')}
          title={node.enum}
        />
      ) : (
        <Select
          id={id}
          value={resolved}
          className="!px-2 !py-1"
          {...focusProps}
          onChange={(event) => onChange(parseInt(event.target.value, 10))}
        >
          {node.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.name} · {option.value}
            </option>
          ))}
        </Select>
      )}
    </FieldRow>
  );
}

/* ------------------------------------------------------------------ */
/* Nested structs                                                      */
/* ------------------------------------------------------------------ */
function StructField({ node, value, onChange, readOnly, depth, indexBadge, extraActions, path = [] }) {
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
        path={path}
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
/** A field that renders as one label-and-control row, rather than as a section
 *  with a header and children of its own. Only these pair up two-across. */
const isLeaf = (field) => field.kind === 'scalar' || field.kind === 'enum';

export function FieldList({ fields, value, onChange, readOnly, depth = 0, path = [] }) {
  const counters = derivedCounters(fields);

  const renderOne = (field) => {
    const setField = (next) => onChange({ ...value, [field.name]: next });
    const childPath = [...path, field.name];

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
          path={childPath}
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
        path={childPath}
        // Derived HERE because a counted array's limit lives in a sibling
        // field -- the array node alone cannot see it.
        maxItems={maxArrayItems(field, fields)}
      />
    );
  };

  /* Consecutive single-row fields pair up two-across; anything that renders as
     a section of its own (struct, array, bitfield) breaks the run and spans the
     full width.

     One column of label-and-control rows across a 768px card left a canyon
     between every label and its input, and still ran the message down the page
     one field at a time. Two columns close the gap AND halve the height, using
     width the panel already had. `@container` rather than a viewport breakpoint
     because the workspace is resizable now: what matters is how wide THIS card
     is, not the window.

     Runs are grouped rather than every field being a grid cell, because a
     struct dropped into a two-column grid would either be squeezed into half
     the width or need a span rule -- and the run boundary is exactly where the
     visual break belongs anyway. */
  const runs = [];
  for (const field of fields) {
    const leaf = isLeaf(field);
    const last = runs[runs.length - 1];
    if (leaf && last?.leaf) last.fields.push(field);
    else runs.push({ leaf, fields: [field] });
  }

  return (
    <div className="@container flex flex-col">
      {runs.map((run, index) =>
        run.leaf ? (
          <div key={index} className="grid grid-cols-1 gap-x-5 @md:grid-cols-2">
            {run.fields.map(renderOne)}
          </div>
        ) : (
          <div key={index} className="flex flex-col py-1">
            {run.fields.map(renderOne)}
          </div>
        ),
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Bitfields                                                           */
/* ------------------------------------------------------------------ */
function BitFieldGroup({ node, value, onChange, readOnly, depth, indexBadge, extraActions, path = [] }) {
  const current = value ?? {};

  return (
    <Group
      icon={ToggleLeft}
      name={node.name}
      type={node.struct}
      meta={node.byte_size ? `${node.byte_size} B packed` : undefined}
      depth={depth}
      badge={indexBadge}
      actions={extraActions}
    >
      <div className="@container">
        <div className="grid grid-cols-1 gap-x-5 @md:grid-cols-2">
          {node.bits.map((bit) => (
            <FieldRenderer
              key={bit.name}
              node={bit}
              value={current[bit.name]}
              onChange={(next) => onChange({ ...current, [bit.name]: next })}
              readOnly={readOnly}
              depth={depth + 1}
              // Every bit reports the BITFIELD's path, not its own: the bits
              // share one packed integer on the wire, so lighting any of them
              // should light that whole value. There are no separate bytes to
              // point at.
              path={path}
            />
          ))}
        </div>
      </div>
    </Group>
  );
}

/* ------------------------------------------------------------------ */
/* Arrays -- the "+ Add <Struct>" case                                 */
/* ------------------------------------------------------------------ */
function ArrayField({ node, value, onChange, readOnly, depth, maxItems = null, path = [] }) {
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
        <p className="px-1 py-1.5 text-center text-[11px] text-slate-500">
          {growable ? `Empty — use “Add ${node.item_label}”.` : 'Empty.'}
        </p>
      ) : node.item.kind === 'struct' || node.item.kind === 'bitfield' ? (
        // A struct/bitfield item already renders its own header+indent (see
        // `Group`) -- wrapping it in a second bordered box would just stack a
        // duplicate header on top of it. Instead its index and remove button
        // are handed to that same header via `indexBadge`/`extraActions`, so
        // one item is one section, not two.
        <div className="flex flex-col gap-2">
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
                path={itemPath(path, index)}
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
        <div className="flex flex-wrap gap-1.5 py-0.5">
          {items.map((item, index) => (
            <CompactArrayItem
              key={index}
              node={node.item}
              value={item}
              onChange={(next) => setItem(index, next)}
              onRemove={growable ? () => removeItem(index) : null}
              readOnly={readOnly}
              index={index}
              path={itemPath(path, index)}
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
            <div className="flex flex-col p-1.5">
              <FieldRenderer
                node={{ ...node.item, name: `${node.name}[${index}]` }}
                value={item}
                onChange={(next) => setItem(index, next)}
                readOnly={readOnly}
                depth={depth + 1}
                path={itemPath(path, index)}
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
function CompactArrayItem({ node, value, onChange, onRemove, readOnly, index, path = [] }) {
  const isEnum = node.kind === 'enum';
  const isFloat = node.numeric === 'float';
  const resolved = React.useMemo(
    () => (isEnum ? resolveEnumValue(value, node.options) : value),
    [isEnum, value, node.options],
  );
  const { active, rowProps, focusProps } = useFieldFocus(path);
  const member = isEnum ? node.options.find((option) => option.value === resolved) : null;

  return (
    <div
      {...rowProps}
      className={cx(
        'flex items-center gap-1 rounded-md border py-0.5 pl-1.5 pr-1 transition-colors',
        active ? 'border-sky-600/70 bg-sky-500/10' : 'border-slate-800 bg-slate-950/40',
      )}
    >
      <span className="font-mono text-[9px] text-slate-500">{index}</span>
      {readOnly ? (
        <span className="tnum px-1 font-mono text-[10px] text-slate-100">
          {isEnum ? (member?.name ?? String(value ?? '—')) : String(value ?? '—')}
        </span>
      ) : isEnum ? (
        <select
          value={resolved}
          title={node.enum}
          {...focusProps}
          onChange={(event) => onChange(parseInt(event.target.value, 10))}
          className={cx(
            'rounded border border-slate-700 bg-slate-950/60 py-0.5 pl-1 pr-5 text-[10px] text-slate-100',
            'cursor-pointer appearance-none transition-colors hover:border-slate-600',
            'focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500/40',
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
          placeholder="0"
          title={node.dtype}
          {...focusProps}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === '') return onChange('');
            onChange(isFloat ? parseFloat(raw) : parseInt(raw, 10));
          }}
          className={cx(
            'tnum w-12 rounded border border-slate-700 bg-slate-950/60 px-1 py-0.5 text-center font-mono text-[10px] text-slate-100',
            'transition-colors hover:border-slate-600',
            'focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500/40',
          )}
        />
      )}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          title={`Remove item ${index}`}
          aria-label={`Remove item ${index}`}
          className="grid h-4 w-4 shrink-0 place-items-center rounded text-slate-500 transition-colors hover:bg-rose-950/60 hover:text-rose-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
        >
          <Trash2 size={9} />
        </button>
      )}
    </div>
  );
}
