"""Turns a message layout into specialised Python source, once, at first use.

`MessageMeta` already resolves the *schema* at import time. This module finishes
the job: it resolves the *parse* too, by generating and `exec`ing a function
specific to one class instead of interpreting `_fields_` on every packet.

Why: profiling `BenchMessage` (92 bytes, every field kind IRS has) showed 62
calls to `Field.from_bytes` per parse, while the actual `Struct.unpack_from`
work was only ~10% of the runtime. The other 90% was Python call frames and
`reader.offset` attribute traffic -- overhead that exists only because the loop
does not know, at the moment it runs, what it is about to parse. Generated code
does know::

    class BenchMessage(Message):
        id: int = UInt32                  ONE unpack_from covering all of these:
        timestamp: int = UInt64             <IQBBfffHHHHHHHHH>
        kind: E_Kind                        16 values, 44 bytes, every offset a
        status: StatusFlags                 compile-time literal
        pos: Position                     <- nested struct FLATTENED into it
        samples: list[int] = [UInt16, 8]  <- fixed array FLATTENED into it
        count: int = UInt16
        payload: list[int] = [Byte, "count"]   list(data[a:b]) -- count is a local
        tail: list[int] = [Byte, None]         list(data[b:end])

Measured on CPython 3.11: `from_bytes` 13.98us -> 1.65us, `to_bytes` 9.34us ->
0.69us, byte-identical output.

What this module deliberately does NOT do:

  * It never changes `_fields_`. That tuple is a public introspection surface
    (`gsim/core_gateway/schema.py`, `payloads.py`); this is only a consumer.
  * It never touches `to_dict`/`from_dict`/`fill`, which are development
    surfaces and stay readable.
  * It never has to succeed. Any layout it cannot express raises `Uncompilable`
    and the class keeps the interpreted implementations, which remain the
    reference semantics. `IRS_COMPILE=0` in the environment forces that path
    everywhere -- which is also how the differential test gets its oracle.
"""
import os
import struct

from .bitfields import BitField
from .containers import FixedList
from .fields import EnumField, Field

#: Off switch AND test oracle -- see the module docstring.
ENABLED = os.environ.get("IRS_COMPILE", "1") not in ("0", "false", "False", "no")

#: An array of this many wire atoms or fewer is unrolled into the enclosing
#: block, so its values ride along in the parent's single `unpack_from`. Above
#: it the array gets its own one-shot unpack plus a build loop -- the same
#: number of `struct` calls, without emitting hundreds of local variables.
_UNROLL_CAP = 32

_BYTE_ORDERS = ("<", ">")

#: Every class MessageMeta has built, for `compile_all()`.
_CLASSES: list[type] = []


class Uncompilable(Exception):
    """This layout keeps the interpreted `_fields_` loop. Never user-visible."""


class _PackerCache(dict):
    """`self[n]` -> a bound `Struct` method for an n-element array.

    Counted and greedy arrays only learn their length at parse time, so their
    packer cannot be hoisted as a constant. A `dict` with `__missing__` makes
    the steady-state cost one dict hit -- cheaper than an `lru_cache` wrapper
    call, let alone constructing a `Struct`.
    """
    __slots__ = ("_template", "_method")

    def __init__(self, template: str, method: str) -> None:
        super().__init__()
        self._template = template          # e.g. "<%dH"
        self._method = method              # "unpack_from" or "pack"

    def __missing__(self, count: int):
        bound = getattr(struct.Struct(self._template % count), self._method)
        self[count] = bound
        return bound


""" Error helpers, hoisted so the generated code never builds a message inline """
def _enum_raiser(enum_class):
    def raise_undefined(value):
        raise ValueError(f"{enum_class.__qualname__}: {value} is not a defined member")
    return raise_undefined


def _length_raiser(name, declared):
    def raise_length(actual):
        raise ValueError(
            f"{name!r} holds {actual} items but is "
            f"declared as exactly {declared}")
    return raise_length


def _partial_raiser(name, size):
    def raise_partial(remaining):
        raise struct.error(
            f"{name!r} consumes the rest of the buffer in {size}-byte items, "
            f"but {remaining} bytes remain")
    return raise_partial


""" Layout analysis -- what shape is this field, and is it fixed-size? """
def _packer_of(field):
    """The `struct.Struct` a leaf field serializes through, or None."""
    if isinstance(field, (Field, EnumField)):
        return field.packer
    if isinstance(field, BitField):
        return type(field)._packer_
    return None


def _atom_of(field):
    """A leaf field as (byte order, format char, size), or None if not a leaf."""
    packer = _packer_of(field)
    if packer is None:
        return None
    fmt = packer.format
    order = fmt[0] if fmt[:1] in _BYTE_ORDERS else "<"
    char = fmt.lstrip("<>=!@")
    if len(char) != 1:
        return None
    return order, char, packer.size


def _flat_atoms(field):
    """Every wire atom `field` occupies, in order -- or None if not fixed-size.

    Fixed-size is the property that lets a run of fields collapse into one
    `struct` call: a nested `Structure` of fixed fields is just more atoms, and
    so is a `[UInt16, 8]`. A counted or greedy array never is.
    """
    from .core import ArrayField, Structure

    atom = _atom_of(field)
    if atom is not None:
        return [atom]

    if isinstance(field, ArrayField):
        if not isinstance(field.length, int) or field.length < 0:
            return None
        element = _flat_atoms(field.baseType)
        if element is None:
            return None
        return element * field.length

    if isinstance(field, Structure):
        atoms = []
        for child in type(field)._fields_:
            child_atoms = _flat_atoms(child)
            if child_atoms is None:
                return None
            atoms.extend(child_atoms)
        return atoms

    return None


def _atoms_format(atoms):
    """One `struct` format for `atoms`, which must share a byte order."""
    if not atoms:
        raise Uncompilable("zero-atom element")
    order = atoms[0][0]
    if any(atom[0] != order for atom in atoms):
        raise Uncompilable("element mixes byte orders")
    return order, "".join(atom[1] for atom in atoms), sum(atom[2] for atom in atoms)


""" Source accumulation """
class _Source:
    """Generated lines plus the constants they close over.

    Constants -- bound `Struct` methods, `cls.__new__`, slot-descriptor setters,
    enum lookup tables -- become the `exec` globals, so the generated code
    reaches them with one LOAD_GLOBAL instead of an attribute chain.
    """

    def __init__(self) -> None:
        self.consts = {"_FixedList": FixedList}
        self._seq = 0

    def name(self, hint: str = "t") -> str:
        self._seq += 1
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in hint)
        return f"_{safe}_{self._seq}"

    def const(self, hint: str, value) -> str:
        name = self.name(hint)
        self.consts[name] = value
        return name

    def setter(self, owner: type, field_name: str) -> str:
        """The slot descriptor's `__set__`, hoisted.

        Faster than `object.__setattr__` (no name lookup, measured ~24%) and it
        bypasses `Structure.__setattr__`/beartype for exactly the reason
        `Structure.from_bytes` does today: bytes off the wire already match the
        schema by construction.
        """
        descriptor = owner.__dict__.get(field_name)
        if descriptor is None or not hasattr(descriptor, "__set__"):
            raise Uncompilable(f"{owner.__name__}.{field_name} is not a slot")
        return self.const(f"set_{field_name}", descriptor.__set__)


class _ParseEmitter(_Source):
    """Emits a parse body: `data`, `off`, `end` in, an instance out.

    Adjacent fixed fields accumulate into a pending block; anything variable, or
    a change of byte order, flushes it as one `unpack_from`. Lines that CONSUME
    a block's values are queued in `pending` and written straight after that
    block's unpack -- which is what lets a nested struct's atoms interleave with
    its parent's while its construction still happens in the right order.
    """

    def __init__(self) -> None:
        super().__init__()
        self.body = []
        self.pending = []
        self._fmt = []
        self._vars = []
        self._size = 0
        self._order = None

    def atom(self, order: str, char: str, size: int, hint: str) -> str:
        """Reserve one value in the current block; returns the local it lands in."""
        if self._order is not None and order != self._order:
            self.flush()
        self._order = order
        var = self.name(hint)
        self._fmt.append(char)
        self._vars.append(var)
        self._size += size
        return var

    def emit(self, line: str) -> None:
        """A line that uses the current block's values."""
        self.pending.append(line)

    def flush(self) -> None:
        if self._vars:
            unpack = self.const(
                "unpack", struct.Struct(self._order + "".join(self._fmt)).unpack_from)
            targets = ", ".join(self._vars)
            comma = "," if len(self._vars) == 1 else ""
            self.body.append(f"{targets}{comma} = {unpack}(data, off)")
            self.body.append(f"off += {self._size}")
            self._fmt, self._vars, self._size, self._order = [], [], 0, None
        self.body.extend(self.pending)
        self.pending = []

    def direct(self, line: str) -> None:
        """A line that must run in sequence, after everything queued so far."""
        self.flush()
        self.body.append(line)


class _PackEmitter(_Source):
    """Emits a serialization body: an instance in, `bytes` out.

    The mirror of `_ParseEmitter` -- adjacent fixed fields accumulate into one
    `Struct.pack`, and each flush contributes one chunk to the final join.
    """

    def __init__(self) -> None:
        super().__init__()
        self.body = []
        self.chunks = []
        self._pre = []
        self._fmt = []
        self._args = []
        self._order = None

    def value(self, order: str, char: str, expression: str) -> None:
        if self._order is not None and order != self._order:
            self.flush()
        self._order = order
        self._fmt.append(char)
        self._args.append(expression)

    def prepare(self, line: str) -> None:
        """A line that must run before the current block is packed."""
        self._pre.append(line)

    def flush(self) -> None:
        self.body.extend(self._pre)
        self._pre = []
        if self._args:
            pack = self.const("pack", struct.Struct(self._order + "".join(self._fmt)).pack)
            chunk = self.name("chunk")
            self.body.append(f"{chunk} = {pack}({', '.join(self._args)})")
            self.chunks.append(chunk)
            self._fmt, self._args, self._order = [], [], None

    def direct(self, line: str) -> None:
        self.flush()
        self.body.append(line)

    def chunk(self, expression: str) -> None:
        """One variable-length piece of the output."""
        name = self.name("chunk")
        self.direct(f"{name} = {expression}")
        self.chunks.append(name)


""" Parse: building a value out of already-unpacked atoms """
def _build_value(em, field, values, emit, hint):
    """One fixed-size field's Python value, as an expression.

    `values` yields one expression per wire atom, in order; `emit` takes any
    setup lines the expression needs first. This is the single recursive routine
    behind both in-block flattening (where `values` allocates block slots) and
    the array build loop (where they index a tuple), which is why a struct
    inside an array inside a struct needs no special case.
    """
    from .core import ArrayField, Structure

    if isinstance(field, Field):
        return next(values)

    if isinstance(field, EnumField):
        raw = next(values)
        member = em.name(hint)
        lookup = em.const(f"enum_{hint}", field.enum_class._value2member_map_.get)
        raiser = em.const(f"undefined_{hint}", _enum_raiser(field.enum_class))
        # Matches EnumField.from_bytes exactly: a defined member wins, a plain 0
        # reads as None, anything else is an error.
        emit(f"{member} = {lookup}({raw})")
        emit(f"if {member} is None and {raw}: {raiser}({raw})")
        return member

    if isinstance(field, BitField):
        raw = next(values)
        cls = type(field)
        target = em.name(hint)
        emit(f"{target} = {em.const('new_bits', cls.__new__)}({em.const('bits', cls)})")
        emit(f"{em.setter(BitField, '_value')}({target}, {raw})")
        return target

    if isinstance(field, Structure):
        cls = type(field)
        target = em.name(hint)
        emit(f"{target} = {em.const('new', cls.__new__)}({em.const('cls', cls)})")
        for child in cls._fields_:
            expression = _build_value(em, child, values, emit, child._name)
            emit(f"{em.setter(cls, child._name)}({target}, {expression})")
        return target

    if isinstance(field, ArrayField):
        items = [_build_value(em, field.baseType, values, emit, f"{hint}_item")
                 for _ in range(field.length)]
        if not items:
            return "_FixedList(())"
        return f"_FixedList(({', '.join(items)}{',' if len(items) == 1 else ''}))"

    raise Uncompilable(f"cannot build {type(field).__name__}")


def _block_values(em, atoms, hint):
    """Allocate `atoms` slots in the emitter's current block, lazily."""
    for order, char, size in atoms:
        yield em.atom(order, char, size, hint)


""" Parse: walking a structure, variable-size fields included """
def _emit_parse_struct(em, cls, target, scope):
    em.emit(f"{target} = {em.const('new', cls.__new__)}({em.const('cls', cls)})")
    for field in cls._fields_:
        _emit_parse_field(em, cls, field, target, scope)


def _emit_parse_field(em, owner, field, target, scope):
    from .core import ArrayField, Structure

    name = field._name
    atoms = _flat_atoms(field)
    if atoms is not None and len(atoms) <= _UNROLL_CAP:
        expression = _build_value(em, field, _block_values(em, atoms, name), em.emit, name)
        em.emit(f"{em.setter(owner, name)}({target}, {expression})")
        # Counted arrays name a SIBLING; in generated code it is already a local.
        scope[name] = expression
        return

    if isinstance(field, ArrayField):
        value = _emit_parse_array(em, field, scope)
    elif isinstance(field, Structure):
        parse = em.const(f"parse_{name}", _compiled(type(field))[0])
        value = em.name(name)
        em.direct(f"{value}, off = {parse}(data, off, end)")
    else:
        raise Uncompilable(f"cannot parse {type(field).__name__} {name!r}")

    em.direct(f"{em.setter(owner, name)}({target}, {value})")
    scope[name] = value


def _emit_parse_count(em, field, scope):
    """The local holding how many elements to read, or None for greedy."""
    length = field.length
    if isinstance(length, int):
        return str(length)
    if isinstance(length, str):
        sibling = scope.get(length)
        if sibling is None:
            # Reading it would need `getattr(instance, ...)` on a field parsed
            # later, which the interpreted path handles by raising. Let it.
            raise Uncompilable(f"array {field._name!r} counts a field not parsed before it")
        return sibling
    return None


def _emit_parse_array(em, field, scope):
    """Emit a variable-size (or too-large-to-unroll) array; returns its local."""
    from .core import Structure

    name = field._name
    element = field.baseType
    fixed = isinstance(field.length, int)
    count = _emit_parse_count(em, field, scope)
    element_atoms = _flat_atoms(element)
    result = em.name(name)

    if element_atoms is None:
        # A variable-size element: nothing to coalesce, loop over its own parser.
        parse = em.const(f"parse_{name}", _compiled(type(element))[0])
        item = em.name(f"{name}_item")
        em.direct(f"{result} = []")
        append = em.name(f"{name}_append")
        em.direct(f"{append} = {result}.append")
        em.direct(f"while off < end:" if count is None else f"for _ in range({count}):")
        em.direct(f"    {item}, off = {parse}(data, off, end)")
        em.direct(f"    {append}({item})")
        if fixed:
            em.direct(f"{result} = _FixedList({result})")
        return result

    order, fmt, size = _atoms_format(element_atoms)
    per_element = len(element_atoms)

    if count is None:
        remaining = em.name(f"{name}_remaining")
        raiser = em.const(f"partial_{name}", _partial_raiser(name, size))
        em.direct(f"{remaining} = end - off")
        em.direct(f"if {remaining} < 0: {remaining} = 0")
        if size > 1:
            # A trailing partial element raises, exactly as the interpreted
            # `while reader.offset < reader._len` loop does by overrunning.
            em.direct(f"if {remaining} % {size}: {raiser}({remaining})")
            count = em.name(f"{name}_count")
            em.direct(f"{count} = {remaining} // {size}")
        else:
            count = remaining

    container = "_FixedList" if fixed else "list"

    # A plain single-atom primitive needs no per-element construction at all.
    if isinstance(element, Field):
        if fmt == "B" and not fixed:
            em.direct(f"{result} = list(data[off:off + {count}])")
            em.direct(f"off += {count}")
            return result
        unpack = (em.const(f"unpack_{name}",
                           struct.Struct(f"{order}{field.length * per_element}{fmt}").unpack_from)
                  if fixed else
                  f"{em.const(f'cache_{name}', _PackerCache(f'{order}%d{fmt}', 'unpack_from'))}[{count}]")
        em.direct(f"{result} = {container}({unpack}(data, off))")
        em.direct(f"off += {count} * {size}" if not fixed else f"off += {field.length * size}")
        return result

    # Enum / bitfield / struct elements: one unpack for the whole run, then a
    # build loop that only allocates objects.
    values = em.name(f"{name}_values")
    if fixed:
        unpack = em.const(f"unpack_{name}", struct.Struct(
            f"{order}{fmt * field.length}").unpack_from)
        em.direct(f"{values} = {unpack}(data, off)")
        em.direct(f"off += {field.length * size}")
    else:
        cache = em.const(f"cache_{name}", _PackerCache(f"{order}%s", "unpack_from"))
        em.direct(f"{values} = {cache}[{fmt!r} * {count}](data, off)")
        em.direct(f"off += {count} * {size}")

    em.direct(f"{result} = []")
    append = em.name(f"{name}_append")
    em.direct(f"{append} = {result}.append")

    body = []
    if per_element == 1:
        cursor = em.name(f"{name}_value")
        em.direct(f"for {cursor} in {values}:")
        item = _build_value(em, element, iter([cursor]), body.append, f"{name}_item")
    else:
        cursor = em.name(f"{name}_i")
        em.direct(f"for {cursor} in range(0, len({values}), {per_element}):")
        slots = iter([f"{values}[{cursor} + {k}]" for k in range(per_element)])
        item = _build_value(em, element, slots, body.append, f"{name}_item")
    for line in body:
        em.body.append(f"    {line}")
    em.body.append(f"    {append}({item})")

    if fixed:
        em.direct(f"{result} = _FixedList({result})")
    return result


""" Pack: the mirror image """
def _emit_pack_flat(em, field, expression):
    """Push one fixed-size field's values into the current pack block."""
    from .core import ArrayField, Structure

    if isinstance(field, Field):
        order, char, _ = _atom_of(field)
        em.value(order, char, expression)
        return

    if isinstance(field, EnumField):
        order, char, _ = _atom_of(field)
        # Matches EnumField.to_bytes: a member packs as its int, None as 0, and
        # a bare int passes straight through.
        held = em.name("enum")
        em.prepare(f"{held} = {expression}")
        em.value(order, char, f"(0 if {held} is None else {held})")
        return

    if isinstance(field, BitField):
        order, char, _ = _atom_of(field)
        em.value(order, char, f"{expression}._value")
        return

    if isinstance(field, Structure):
        cls = type(field)
        held = em.name("struct")
        em.prepare(f"{held} = {expression}")
        for child in cls._fields_:
            _emit_pack_flat(em, child, f"{held}.{child._name}")
        return

    if isinstance(field, ArrayField):
        held = em.name("array")
        em.prepare(f"{held} = {expression}")
        em.prepare(f"if len({held}) != {field.length}: "
                   f"{em.const('length_' + field._name, _length_raiser(field._name, field.length))}"
                   f"(len({held}))")
        for index in range(field.length):
            _emit_pack_flat(em, field.baseType, f"{held}[{index}]")
        return

    raise Uncompilable(f"cannot pack {type(field).__name__}")


def _emit_pack_struct(em, cls, source):
    for field in cls._fields_:
        _emit_pack_field(em, field, f"{source}.{field._name}")


def _emit_pack_field(em, field, expression):
    from .core import ArrayField, Structure

    atoms = _flat_atoms(field)
    if atoms is not None and len(atoms) <= _UNROLL_CAP:
        _emit_pack_flat(em, field, expression)
        return

    if isinstance(field, ArrayField):
        _emit_pack_array(em, field, expression)
        return

    if isinstance(field, Structure):
        pack = em.const(f"pack_{field._name}", _compiled(type(field))[1])
        em.chunk(f"{pack}({expression})")
        return

    raise Uncompilable(f"cannot pack {type(field).__name__} {field._name!r}")


def _emit_pack_array(em, field, expression):
    """A whole array as one chunk. Never writes the count -- the sibling
    counted field owns that, exactly as `ArrayField.to_bytes` does today."""
    from .core import Structure

    name = field._name
    element = field.baseType
    held = em.name(name)
    em.direct(f"{held} = {expression}")
    if isinstance(field.length, int):
        raiser = em.const(f"length_{name}", _length_raiser(name, field.length))
        em.direct(f"if len({held}) != {field.length}: {raiser}(len({held}))")

    atoms = _flat_atoms(element)
    if atoms is None:
        pack = em.const(f"pack_{name}", _compiled(type(element))[1])
        item = em.name(f"{name}_item")
        em.chunk(f"b''.join([{pack}({item}) for {item} in {held}])")
        return

    order, fmt, _ = _atoms_format(atoms)
    if isinstance(element, Field):
        if fmt == "B":
            em.chunk(f"bytes({held})")
            return
        values = f"*{held}"
    elif isinstance(element, EnumField):
        item = em.name(f"{name}_item")
        values = f"*[0 if {item} is None else {item} for {item} in {held}]"
    elif isinstance(element, BitField):
        item = em.name(f"{name}_item")
        values = f"*[{item}._value for {item} in {held}]"
    else:
        pack = em.const(f"pack_{name}", _compiled(type(element))[1])
        item = em.name(f"{name}_item")
        em.chunk(f"b''.join([{pack}({item}) for {item} in {held}])")
        return

    cache = em.const(f"cache_{name}", _PackerCache(f"{order}%s", "pack"))
    em.chunk(f"{cache}[{fmt!r} * len({held})]({values})")


""" Generation and installation """
def _generate_parse(cls):
    em = _ParseEmitter()
    root = em.name("obj")
    _emit_parse_struct(em, cls, root, {})
    em.flush()
    body = "\n".join(f"    {line}" for line in em.body) or "    pass"
    source = (
        f"def _parse_(data, off, end):\n{body}\n    return {root}, off\n\n"
        f"def from_bytes(cls, reader, instance=None):\n"
        f"    data = reader.data\n"
        f"    off = reader.offset\n"
        f"    end = reader._len\n"
        f"{body}\n"
        f"    reader.offset = off\n"
        f"    return {root}\n")
    return source, em.consts


def _generate_pack(cls):
    em = _PackEmitter()
    _emit_pack_struct(em, cls, "obj")
    em.flush()
    body = "\n".join(f"    {line}" for line in em.body)
    if not em.chunks:
        result = "b''"
    elif len(em.chunks) == 1:
        result = em.chunks[0]
    else:
        result = f"b''.join(({', '.join(em.chunks)}))"
    source = (
        f"def _pack_(obj):\n{body or '    pass'}\n    return {result}\n\n"
        f"def to_bytes(self, writer=None, value=None):\n"
        f"    obj = self if value is None else value\n"
        f"{body}\n"
        f"    _out = {result}\n"
        f"    if writer is None:\n"
        f"        return _out\n"
        f"    writer.buffer += _out\n")
    return source, em.consts


def _compiled(cls):
    """(parse, pack) for `cls`, compiling it if needed. Raises `Uncompilable`."""
    cached = cls.__dict__.get("_irs_compiled_")
    if cached is not None:
        return cached
    if cls.__dict__.get("_irs_uncompilable_"):
        raise Uncompilable(f"{cls.__name__} already failed to compile")

    parse_source, parse_consts = _generate_parse(cls)
    pack_source, pack_consts = _generate_pack(cls)

    parse_globals = dict(parse_consts)
    exec(compile(parse_source, f"<IRS {cls.__name__} parse>", "exec"), parse_globals)
    pack_globals = dict(pack_consts)
    exec(compile(pack_source, f"<IRS {cls.__name__} pack>", "exec"), pack_globals)

    pair = (parse_globals["_parse_"], pack_globals["_pack_"])
    cls._irs_compiled_ = pair
    cls._irs_source_ = f"{parse_source}\n{pack_source}"
    cls.from_bytes = classmethod(parse_globals["from_bytes"])
    cls.to_bytes = pack_globals["to_bytes"]
    return pair


def _fallback(cls) -> None:
    """Drop the trampolines so lookup finds the interpreted implementations."""
    cls._irs_uncompilable_ = True
    for name in ("from_bytes", "to_bytes"):
        if name in cls.__dict__:
            delattr(cls, name)


def ensure_compiled(cls) -> bool:
    """Compile `cls` if it is not already. False means it stays interpreted."""
    try:
        _compiled(cls)
        return True
    except Uncompilable:
        _fallback(cls)
        return False
    except Exception:                                    # pragma: no cover
        # A generator bug must never take a working parser down with it.
        import logging
        logging.getLogger("IRS.compiler").exception(
            "IRS could not compile %s; falling back to the interpreted parser",
            getattr(cls, "__qualname__", cls))
        _fallback(cls)
        return False


""" The trampolines MessageMeta installs -- one compile, then out of the way """
def lazy_from_bytes(cls, reader, instance=None):
    ensure_compiled(cls)
    return cls.from_bytes(reader, instance)


def lazy_to_bytes(self, writer=None, value=None):
    cls = type(self)
    ensure_compiled(cls)
    return cls.to_bytes(self, writer, value)


def register(cls) -> None:
    """MessageMeta tells us about every class it builds, for `compile_all()`."""
    _CLASSES.append(cls)


def compile_all() -> int:
    """Compile every message layout defined so far; returns how many succeeded.

    Compilation is lazy by default -- roughly 100us per class, paid on the first
    packet of that type, so a structures file with hundreds of unused layouts
    costs nothing. Call this at startup instead when the first packet of each
    type must not be the slow one.
    """
    return sum(ensure_compiled(cls) for cls in list(_CLASSES))


def dump_source(cls) -> str:
    """The generated source for `cls` -- for reading, and for bug reports."""
    ensure_compiled(cls)
    return cls.__dict__.get("_irs_source_", f"# {cls.__name__} is not compiled")
