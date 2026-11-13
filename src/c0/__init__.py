"""C0DATA 1.0 — structured data using ASCII C0 control codes.

A thin Python layer over the c0 C core (the ``_c0`` extension). The read path
is zero-copy: raw accessors return ``memoryview`` slices into the input
buffer; logical values come back as ``bytes`` (decoded when the value is
text), with ``str`` conveniences beside them.

>>> import c0
>>> buf = c0.build(lambda b: (
...     b.group("users", ["name", "amount"]),
...     b.record("Alice", "100"),
...     b.record("Bob", "200")))
>>> doc = c0.Document(buf)
>>> doc.group("users").record(0).string("name")
'Alice'
"""
from __future__ import annotations

from enum import IntEnum
from typing import Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

from .errors import (
    BuildError,
    C0Error,
    C0SyntaxError,
    DamagedBlockError,
    DiffError,
    NotFoundError,
    UnassignedCodeError,
    UnexpectedEndError,
    UnitTypeError,
)
from . import _c0

__version__ = "0.3.2"
VERSION = _c0.VERSION

__all__ = [
    "SOH", "STX", "ETX", "EOT", "ENQ", "BEL", "DLE", "SYN", "ETB", "CAN", "EM", "SUB",
    "FS", "GS", "RS", "US", "HT", "LF", "CR", "SP",
    "C0Error", "C0SyntaxError", "UnassignedCodeError", "UnexpectedEndError",
    "UnitTypeError", "NotFoundError", "BuildError", "DiffError", "DamagedBlockError",
    "UnitType", "PrettyMode",
    "is_assigned", "is_layout", "is_text_escaped", "is_plain", "is_name",
    "decode_text", "validate", "canonical", "pretty_format", "pretty_parse",
    "Unit", "Record", "Group", "Table", "FileView", "Body", "Reference",
    "Heading", "Document", "Container", "StreamReader", "Builder", "build",
    "diff", "parse_diff", "apply_diff", "DiffBuilder", "PatternBuilder",
]

# Assigned control codes (DESIGN.md "Assigned Control Codes").
SOH, STX, ETX, EOT, ENQ, BEL = 0x01, 0x02, 0x03, 0x04, 0x05, 0x07
DLE, SYN, ETB, CAN, EM, SUB = 0x10, 0x16, 0x17, 0x18, 0x19, 0x1A
FS, GS, RS, US = 0x1C, 0x1D, 0x1E, 0x1F
# Layout codes outside text.
HT, LF, CR, SP = 0x09, 0x0A, 0x0D, 0x20

Bytes = Union[bytes, bytearray, memoryview]
Str = Union[str, Bytes]

_LAYOUT = b" \t\n\r"


class UnitType(IntEnum):
    """What a raw unit holds, by its first non-layout byte."""
    PLAIN = 0
    TEXT = 1
    LEVEL = 2
    REF = 3


class PrettyMode(IntEnum):
    COMPACT = 0   # one element per line, no padding
    ALIGNED = 1   # table columns aligned
    SPACED = 2    # aligned, plus a space after each prefix and around US


def _b(s: Str) -> bytes:
    """A str (UTF-8) or bytes-like as bytes."""
    return s.encode("utf-8") if isinstance(s, str) else bytes(s)


def _mv(buf: Bytes) -> memoryview:
    mv = memoryview(buf)
    if mv.ndim != 1 or mv.format != "B":
        mv = mv.cast("B")
    return mv


def _trim(b: bytes) -> bytes:
    return b.strip(_LAYOUT)


# ---- bytewise helpers ------------------------------------------------------

def is_assigned(byte: int) -> bool:
    """Whether a byte is one of the sixteen assigned codes."""
    return _c0.is_assigned(byte)


def is_layout(byte: int) -> bool:
    """Whether a byte is layout outside text (SP, HT, LF, CR)."""
    return _c0.is_layout(byte)


def is_text_escaped(byte: int) -> bool:
    """Whether a byte is one of the seven that take a DLE inside text."""
    return _c0.is_text_escaped(byte)


def is_plain(value: Str) -> bool:
    """Whether a value can be written plain: no byte below 0x20, no edge whitespace."""
    return _c0.is_plain(_b(value))


def is_name(value: Str) -> bool:
    """Whether bytes are a legal name (label, key, header name)."""
    return _c0.is_name(_b(value))


def decode_text(inside: Bytes) -> bytes:
    """Decode the inside of a text (between STX and ETX): DLE escapes removed."""
    return _c0.decode_text(inside)


def validate(buf: Bytes) -> None:
    """Check a whole buffer against the grammar. Raises a ``C0SyntaxError``
    (or a subclass) on the first error; returns None when well-formed.
    Constructing a ``Body``, ``Document`` or ``Container`` applies the same
    check: parse is validate."""
    _c0.validate(buf)


def canonical(buf: Bytes) -> bool:
    """Whether bytes are a canonical span: well-formed, no layout, one
    spelling per value, unique keys per keyed record, no framing, no notes
    or dead elements. Supersession is content and is kept. Opt-in: a
    canonical violation never fails parsing."""
    return _c0.canonical(buf)


def pretty_format(buf: Bytes, indent: Optional[str] = None,
                  mode: PrettyMode = PrettyMode.COMPACT) -> str:
    """Format compact bytes as pretty form (Unicode Control Pictures).
    ``indent`` defaults to two spaces. Raises ``C0SyntaxError`` on malformed
    input or when a value holds a glyph character and cannot be shown."""
    return _c0.pretty_format(buf, indent, int(mode))


def pretty_parse(text: str) -> bytes:
    """Parse pretty form back to compact bytes: glyphs become codes; outside
    text, whitespace touching a glyph or a line edge is dropped."""
    return _c0.pretty_parse(text)


# ---- records -----------------------------------------------------------------

class Reference:
    """A parsed reference (DESIGN.md "References"): a group ``name`` written
    plain (``␅tags``), or a ``pattern``, the level ``␅␐…␘`` that is one
    branch of the target with its values left out, reported as the ``Body``
    it is written as (``␅␐␝tags␞001␟label␘``). Form checked, not resolved."""

    __slots__ = ("name", "pattern")

    def __init__(self, name: Optional[bytes], pattern: Optional["Body"]):
        self.name = name
        self.pattern = pattern

    @property
    def is_pattern(self) -> bool:
        return self.pattern is not None

    @property
    def is_group(self) -> bool:
        """Whether the reference names a group as a whole: a plain name, or
        a pattern that is a bare group line."""
        if self.name is not None:
            return True
        p = self.pattern
        files = p.files
        if len(files) != 1 or not files[0].leading:
            return False
        groups = files[0].groups
        return len(groups) == 1 and groups[0].depth > 0 and not groups[0].records

    @property
    def group_name(self) -> Optional[bytes]:
        """The group named, when the reference names a group as a whole."""
        if self.name is not None:
            return self.name
        return self.pattern.groups[0].name if self.is_group else None

    @property
    def group(self) -> bytes:
        """The group of a group reference; ``UnitTypeError`` for a pattern
        that walks into a group."""
        g = self.group_name
        if g is None:
            raise UnitTypeError("reference is a pattern, not a group")
        return g

    @property
    def path(self) -> list:
        """The steps of the pattern in walking order (file, group names,
        record id, field name, then the inner pattern's steps; a keyed step
        contributes its key), or the group name alone. Informal."""
        if self.name is not None:
            return [self.name]
        out: list = []
        files = self.pattern.files
        if not files:
            return out
        f = files[0]
        if not f.leading:
            out.append(f.name)
        groups = f.groups
        for g in groups:
            if g.depth != 0:
                out.append(g.name)
        if groups and groups[-1].records:
            rec = groups[-1].records[0]
            if rec.keyed:
                keys = rec.keys
                if keys:
                    out.append(keys[0])
            else:
                for i in range(rec.field_count):
                    if i < 2:
                        out.append(rec.value(i))
                    elif rec.unit_type(i) == UnitType.LEVEL:
                        out.extend(Reference(None, rec.level(i)).path)
        return out

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Reference):
            return False
        if self.name is not None or other.name is not None:
            return self.name == other.name
        return bytes(self.pattern.raw) == bytes(other.pattern.raw)

    def __repr__(self) -> str:
        if self.name is not None:
            return f"Reference(name={self.name!r})"
        return f"Reference(pattern={bytes(self.pattern.raw)!r})"


class Unit:
    """One positional unit of a record as written: its raw bytes and whether
    the unit after it supersedes it (``old␚␟new``)."""

    __slots__ = ("_buf", "_start", "_end", "superseded")

    def __init__(self, buf: Bytes, start: int, end: int, superseded: bool):
        self._buf = buf
        self._start = start
        self._end = end
        self.superseded = superseded

    @property
    def raw(self) -> memoryview:
        return _mv(self._buf)[self._start:self._end]

    @property
    def unit_type(self) -> "UnitType":
        return UnitType(_c0.unit_type(self._buf, self._start, self._end))

    @property
    def value(self) -> bytes:
        """Logical value; ``UnitTypeError`` for a level or a reference."""
        return _c0.value(self._buf, self._start, self._end)

    def __repr__(self) -> str:
        return f"Unit({bytes(self.raw)!r}, superseded={self.superseded})"


class Record:
    """A record: positional (units separated by US) or keyed (SOH key US
    value ...), never mixed. Zero-copy accessors over the buffer.

    The positional and keyed accessors report the record's **result**: a
    superseded unit (``old␚␟new``) and a dead one are not among them;
    ``units`` lists every positional unit as written.

    Positional access takes an index; keyed access takes a key (``str`` or
    ``bytes``). ``value``, ``string``, ``level``, ``list`` and ``ref`` accept
    either: on a positional record under a header row, a name is looked up
    as a column of the record's group.
    """

    __slots__ = ("_buf", "_start", "_end", "_units", "_pairs", "_group")

    def __init__(self, buf: Bytes, start: int, end: int, group: Optional["Group"] = None):
        self._buf = buf
        self._start = start
        self._end = end
        self._units = None
        self._pairs = None
        self._group = group

    @property
    def raw(self) -> memoryview:
        """The record's bytes after its RS, trailing layout excluded."""
        return _mv(self._buf)[self._start:self._end]

    @property
    def keyed(self) -> bool:
        """Whether the record is keyed (its first live unit is an SOH)."""
        return _c0.record_keyed(self._buf, self._start, self._end)

    # ---- positional

    def _spans(self) -> list:
        if self._units is None:
            self._units = _c0.record_units(self._buf, self._start, self._end)
        return self._units

    def _span(self, i: int) -> tuple:
        spans = self._spans()
        if not isinstance(i, int):
            raise TypeError("positional access takes an index")
        return spans[i]

    @property
    def field_count(self) -> int:
        """Number of positional units of the result (0 for a keyed record)."""
        return len(self._spans())

    @property
    def fields(self) -> list:
        """Raw units with their layout, as memoryviews."""
        mv = _mv(self._buf)
        return [mv[s:e] for s, e in self._spans()]

    @property
    def units(self) -> list:
        """Every positional unit as written, superseded ones included, each
        a ``Unit`` with its ``superseded`` flag."""
        return [Unit(self._buf, s, e, sup)
                for s, e, sup in _c0.record_written_units(self._buf, self._start, self._end)]

    def field(self, i: int) -> memoryview:
        """Raw unit ``i`` (layout at its edges included)."""
        s, e = self._span(i)
        return _mv(self._buf)[s:e]

    def unit_type(self, i: Union[int, Str]) -> UnitType:
        s, e = self._locate(i)
        return UnitType(_c0.unit_type(self._buf, s, e))

    def value(self, i: Union[int, Str]) -> bytes:
        """Logical value of unit ``i`` (an index, or a key for a keyed record):
        the plain value with layout trimmed, or the text decoded. Raises
        ``UnitTypeError`` for a level or a reference."""
        s, e = self._locate(i)
        return _c0.value(self._buf, s, e)

    def string(self, i: Union[int, Str], encoding: str = "utf-8") -> str:
        """``value(i)`` decoded as a string."""
        return self.value(i).decode(encoding)

    @property
    def values(self) -> list:
        """Logical values of the positional units."""
        return [_c0.value(self._buf, s, e) for s, e in self._spans()]

    @property
    def strings(self) -> list:
        return [v.decode("utf-8") for v in self.values]

    def level(self, i: Union[int, Str]) -> "Body":
        """Unit ``i`` as a nested level: the body between its DLE and CAN."""
        s, e = self._locate(i)
        bs, be = _c0.unit_level(self._buf, s, e)
        return Body(self._buf, bs, be, _valid=True)

    def list(self, i: Union[int, Str]) -> list:
        """Unit ``i`` as a flat list: a level holding at most one positional
        record (an empty level is ``[]``). Raises ``UnitTypeError`` for a
        level holding anything else."""
        s, e = self._locate(i)
        return [_c0.value(self._buf, a, b) for a, b in _c0.unit_list(self._buf, s, e)]

    def ref(self, i: Union[int, Str]) -> Reference:
        """Unit ``i`` as a reference (form checked, not resolved): a group
        name, or a pattern as a ``Body``."""
        s, e = self._locate(i)
        is_pattern, a, b = _c0.unit_ref(self._buf, s, e)
        if is_pattern:
            return Reference(None, Body(self._buf, a, b, _valid=True))
        return Reference(bytes(_mv(self._buf)[a:b]), None)

    # ---- keyed

    def _pair_spans(self) -> list:
        if self._pairs is None:
            self._pairs = _c0.pairs(self._buf, self._start, self._end)
        return self._pairs

    def _find(self, key: Str) -> Optional[tuple]:
        """Value span for a key (last wins), or None."""
        needle = _b(key)
        mv = _mv(self._buf)
        found = None
        for ks, ke, vs, ve in self._pair_spans():
            if bytes(mv[ks:ke]) == needle:
                found = (vs, ve)
        return found

    def _locate(self, i: Union[int, Str]) -> tuple:
        if isinstance(i, int):
            return self._span(i)
        if not self.keyed:
            col = self._group.column(i) if self._group is not None else None
            if col is None:
                raise NotFoundError(f"no column named {_b(i)!r}")
            return self._span(col)
        span = self._find(i)
        if span is None:
            raise NotFoundError(f"no key {_b(i)!r}")
        return span

    @property
    def keys(self) -> list:
        """Keys in written order, duplicates included."""
        mv = _mv(self._buf)
        return [bytes(mv[ks:ke]) for ks, ke, _, _ in self._pair_spans()]

    def _pair_value(self, vs: int, ve: int) -> bytes:
        if _c0.unit_type(self._buf, vs, ve) == UnitType.LEVEL:
            return bytes(_mv(self._buf)[vs:ve])
        return _c0.value(self._buf, vs, ve)

    @property
    def pairs(self) -> list:
        """``(key, value)`` in written order. A value that is a level comes
        back as its raw bytes; use ``level(key)`` to navigate it."""
        mv = _mv(self._buf)
        return [(bytes(mv[ks:ke]), self._pair_value(vs, ve)) for ks, ke, vs, ve in self._pair_spans()]

    def items(self) -> Iterator:
        return iter(self.pairs)

    def get(self, key: Str, default=None):
        """Logical value for a key (last wins), or ``default``."""
        span = self._find(key)
        if span is None:
            return default
        return self._pair_value(*span)

    def has_key(self, key: Str) -> bool:
        return self._find(key) is not None

    def __contains__(self, key: Str) -> bool:
        return self.has_key(key)

    def __getitem__(self, i: Union[int, Str]) -> bytes:
        if isinstance(i, int):
            return self.value(i)
        v = self.get(i)
        if v is None:
            raise NotFoundError(f"no key {_b(i)!r}")
        return v

    def __len__(self) -> int:
        return len(self._pair_spans()) if self.keyed else self.field_count

    def __iter__(self) -> Iterator:
        """Positional values, or the keys of a keyed record."""
        return iter(self.keys) if self.keyed else iter(self.values)

    def __repr__(self) -> str:
        return f"Record({bytes(self.raw)!r})"


class Group:
    """A group: its depth (GS count; 0 for the unnamed leading group), name,
    optional header row and records."""

    __slots__ = ("_buf", "depth", "_ns", "_ne", "_start", "_stop", "_header", "_records")

    def __init__(self, buf: Bytes, depth: int, ns: int, ne: int, start: int, stop: int):
        self._buf = buf
        self.depth = depth
        self._ns = ns
        self._ne = ne
        self._start = start
        self._stop = stop
        self._header = False
        self._records = None

    @property
    def name(self) -> bytes:
        return _trim(bytes(_mv(self._buf)[self._ns:self._ne]))

    @property
    def raw(self) -> memoryview:
        """The group's contents: header row and records."""
        return _mv(self._buf)[self._start:self._stop]

    def _args(self) -> tuple:
        return (self._buf, self.depth, self._ns, self._ne, self._start, self._stop)

    @property
    def header_record(self) -> Optional[Record]:
        """The header row as a record, or None."""
        if self._header is False:
            span = _c0.group_header(*self._args())
            self._header = Record(self._buf, *span) if span else None
        return self._header

    @property
    def has_header(self) -> bool:
        return self.header_record is not None

    @property
    def headers(self) -> list:
        """Header names, or an empty list."""
        h = self.header_record
        return [_trim(v) for v in h.values] if h else []

    @property
    def header_count(self) -> int:
        return len(self.headers)

    def header(self, i: int) -> bytes:
        return self.headers[i]

    def column(self, name: Str) -> Optional[int]:
        """Index of a header name, or None."""
        needle = _b(name)
        for i, h in enumerate(self.headers):
            if h == needle:
                return i
        return None

    @property
    def records(self) -> list:
        if self._records is None:
            self._records = [Record(self._buf, s, e, self) for s, e in _c0.group_records(*self._args())]
        return self._records

    @property
    def record_count(self) -> int:
        return len(self.records)

    def record(self, i: int) -> Record:
        return self.records[i]

    __getitem__ = record

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.records)

    def __repr__(self) -> str:
        return f"Group({self.name!r}, depth={self.depth}, records={len(self)})"


Table = Group


class FileView:
    """A file within a body: its name and groups. The unnamed file before
    the first FS is ``leading``."""

    __slots__ = ("_buf", "_ns", "_ne", "_start", "_stop", "leading", "_groups")

    def __init__(self, buf: Bytes, ns: int, ne: int, start: int, stop: int, leading: bool = False):
        self._buf = buf
        self._ns = ns
        self._ne = ne
        self._start = start
        self._stop = stop
        self.leading = leading
        self._groups = None

    @property
    def name(self) -> bytes:
        return _trim(bytes(_mv(self._buf)[self._ns:self._ne]))

    @property
    def raw(self) -> memoryview:
        return _mv(self._buf)[self._start:self._stop]

    @property
    def groups(self) -> list:
        if self._groups is None:
            self._groups = [Group(self._buf, *g)
                            for g in _c0.file_groups(self._buf, self._ns, self._ne, self._start, self._stop,
                                                     self.leading)]
        return self._groups

    def group(self, which: Union[int, Str]) -> Group:
        """Group by index, or the first group with the given name."""
        if isinstance(which, int):
            return self.groups[which]
        needle = _b(which)
        for g in self.groups:
            if g.name == needle:
                return g
        raise NotFoundError(f"no group named {needle!r}")

    __getitem__ = group

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self) -> Iterator[Group]:
        return iter(self.groups)


class Body:
    """Zero-copy navigator for a document body (the bytes between a heading
    and EOT) or the inside of a nested level. Records before the first GS of
    a file form an unnamed group; records and groups before the first FS form
    an unnamed (leading) file.

    ``Body(buf)`` parses the slice: the whole grammar is applied and a
    ``C0SyntaxError`` raised on malformed input (parse is validate). The
    model is the **result**: dead and superseded elements are not in it."""

    __slots__ = ("_buf", "start", "stop", "_files")

    def __init__(self, buf: Bytes, start: int = 0, stop: Optional[int] = None, *, _valid: bool = False):
        self._buf = buf
        self.start = start
        self.stop = len(_mv(buf)) if stop is None else stop
        self._files = None
        if not _valid:
            _c0.body_read(buf, self.start, self.stop)

    @property
    def raw(self) -> memoryview:
        return _mv(self._buf)[self.start:self.stop]

    @property
    def files(self) -> list:
        if self._files is None:
            self._files = [FileView(self._buf, *f) for f in _c0.body_files(self._buf, self.start, self.stop)]
        return self._files

    @property
    def groups(self) -> list:
        """All groups of all files, in order."""
        return [g for f in self.files for g in f.groups]

    def _first_file(self) -> FileView:
        return self.files[0]

    @property
    def name(self) -> bytes:
        """Name of the first file (empty when the body has no FS)."""
        return self._first_file().name

    @property
    def group_count(self) -> int:
        return len(self._first_file().groups)

    def group(self, which: Union[int, Str]) -> Group:
        """Group of the first file by index or name."""
        return self._first_file().group(which)

    __getitem__ = group

    @property
    def group_names(self) -> list:
        return [g.name for g in self._first_file().groups]

    @property
    def records(self) -> list:
        """Records of the body when it is a bare record stream: the first
        group's records (empty when there is no group)."""
        groups = self._first_file().groups
        return groups[0].records if groups else []

    @property
    def record_count(self) -> int:
        return len(self.records)

    def record(self, i: int) -> Record:
        return self.records[i]

    @property
    def bare(self) -> bool:
        """Whether the body is just records: one unnamed file, at most the unnamed group."""
        return _c0.body_bare(self._buf, self.start, self.stop)

    def __len__(self) -> int:
        return self.group_count

    def __iter__(self) -> Iterator[Group]:
        return iter(self._first_file().groups)


# ---- documents ---------------------------------------------------------------

class Heading:
    """The heading of a document: keyed units after SYN, in written order,
    dead and superseded units dropped. Lookups take the last value for a
    duplicate key."""

    __slots__ = ("_buf", "_start", "_end", "_pairs")

    KNOWN_KEYS = (b"shape", b"schema", b"version", b"blob")
    SHAPES = ("diff", "stream")

    def __init__(self, buf: Bytes, start: int, end: int):
        self._buf = buf
        self._start = start
        self._end = end
        self._pairs = None

    @property
    def raw(self) -> memoryview:
        return _mv(self._buf)[self._start:self._end]

    def _pair_spans(self) -> list:
        if self._pairs is None:
            self._pairs = _c0.pairs(self._buf, self._start, self._end)
        return self._pairs

    def _find(self, key: Str) -> Optional[tuple]:
        needle = _b(key)
        mv = _mv(self._buf)
        found = None
        for ks, ke, vs, ve in self._pair_spans():
            if bytes(mv[ks:ke]) == needle:
                found = (vs, ve)
        return found

    def _value(self, vs: int, ve: int) -> bytes:
        if _c0.unit_type(self._buf, vs, ve) == UnitType.LEVEL:
            return bytes(_mv(self._buf)[vs:ve])
        return _c0.value(self._buf, vs, ve)

    @property
    def pairs(self) -> list:
        """``(key, value)`` in written order; a level value is its raw bytes."""
        mv = _mv(self._buf)
        return [(bytes(mv[ks:ke]), self._value(vs, ve)) for ks, ke, vs, ve in self._pair_spans()]

    @property
    def keys(self) -> list:
        mv = _mv(self._buf)
        return [bytes(mv[ks:ke]) for ks, ke, _, _ in self._pair_spans()]

    def get(self, key: Str, default=None):
        span = self._find(key)
        return default if span is None else self._value(*span)

    def __getitem__(self, key: Str) -> bytes:
        v = self.get(key)
        if v is None:
            raise NotFoundError(f"no heading key {_b(key)!r}")
        return v

    def __contains__(self, key: Str) -> bool:
        return self._find(key) is not None

    def level(self, key: Str) -> Body:
        """The value of a key as a nested level (the blob table)."""
        span = self._find(key)
        if span is None:
            raise NotFoundError(f"no heading key {_b(key)!r}")
        bs, be = _c0.unit_level(self._buf, *span)
        return Body(self._buf, bs, be, _valid=True)

    @property
    def shape(self) -> Optional[str]:
        """The ``shape`` value (``diff`` or ``stream``; anything else is a warning)."""
        v = self.get("shape")
        return None if v is None else v.decode("utf-8", "replace")

    @property
    def schema(self):
        """The schema the body claims: its name as ``bytes``, or the keyed
        ``Record`` of ``name`` / ``url`` / ``hash`` when written as a level;
        None when absent. The form is checked at parse time."""
        span = self._find("schema")
        if span is None:
            return None
        if _c0.unit_type(self._buf, *span) == UnitType.LEVEL:
            bs, be = _c0.unit_level(self._buf, *span)
            records = Body(self._buf, bs, be, _valid=True).records
            return records[0] if records else None
        return _c0.value(self._buf, *span)

    @property
    def version(self) -> int:
        """The ``version`` value as an integer; 1 when absent or not a number."""
        v = self.get("version")
        if v is None or not v.isdigit():
            return 1
        return int(v)

    @property
    def raw_blob(self) -> Optional[bytes]:
        """Raw unit of the ``blob`` key, or None."""
        span = self._find("blob")
        return None if span is None else bytes(_mv(self._buf)[span[0]:span[1]])

    @property
    def blob(self) -> Optional[Body]:
        """The blob table as a Body when it is declared as a level, else None."""
        span = self._find("blob")
        if span is None or _c0.unit_type(self._buf, *span) != UnitType.LEVEL:
            return None
        bs, be = _c0.unit_level(self._buf, *span)
        return Body(self._buf, bs, be, _valid=True)

    @property
    def empty(self) -> bool:
        return not self._pair_spans()

    def __len__(self) -> int:
        return len(self._pair_spans())

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.keys)


class Document:
    """One C0DATA document: an optional SYN heading, a body, an optional EOT.
    ``Document(buf)`` parses the first document in the buffer, applying the
    whole grammar (a ``C0SyntaxError`` on malformed input); use
    ``Container`` for a buffer holding several documents or an EM tail.
    ``warnings`` lists the loud warnings (no SYN, no EOT, unknown keys,
    shape or version, duplicate key). ``dead_elements`` counts the dead
    records, header rows, groups and files met; ``supersession_grains``
    lists the codes (US, SOH, RS, GS, FS) at which supersession was met."""

    __slots__ = ("_buf", "start", "stop", "has_syn", "has_eot", "heading", "body", "warnings",
                 "dead_elements", "supersession_grains")

    def __init__(self, buf: Bytes, start: int = 0, _parsed: Optional[tuple] = None):
        self._buf = buf
        t = _parsed if _parsed is not None else _c0.document(buf, start)
        (self.start, self.stop, self.has_syn, self.has_eot, hs, he, bs, be, bits, dead, grains) = t
        self.heading = Heading(buf, hs, he)
        self.body = Body(buf, bs, be, _valid=True)
        self.warnings = self._warnings(bits)
        self.dead_elements = dead
        self.supersession_grains = [c for c in (US, SOH, RS, GS, FS) if grains & (1 << (c & 31))]

    def _warnings(self, bits: int) -> list:
        out = []
        if bits & _c0.WARN_NO_SYN:
            out.append("no SYN at the start of the document")
        if bits & _c0.WARN_NO_EOT:
            out.append("no EOT at the end of the document")
        for k in self.heading.keys:
            if k not in Heading.KNOWN_KEYS:
                out.append(f"unknown heading key '{k.decode('utf-8', 'replace')}'")
        if bits & _c0.WARN_DUPLICATE_KEY:
            out.append("duplicate heading key (last wins)")
        shape = self.heading.shape
        if shape is not None and shape not in Heading.SHAPES:
            out.append(f"unknown shape '{shape}'")
        v = self.heading.get("version")
        if v is not None and v != b"1":
            out.append(f"unknown version '{v.decode('utf-8', 'replace')}'")
        return out

    @property
    def raw(self) -> memoryview:
        return _mv(self._buf)[self.start:self.stop]

    @property
    def shape(self) -> Optional[str]:
        return self.heading.shape

    # Convenience accessors over the body.
    @property
    def name(self) -> bytes:
        return self.body.name

    @property
    def files(self) -> list:
        return self.body.files

    @property
    def groups(self) -> list:
        return self.body.groups

    @property
    def group_count(self) -> int:
        return self.body.group_count

    def group(self, which: Union[int, Str]) -> Group:
        return self.body.group(which)

    __getitem__ = group

    @property
    def group_names(self) -> list:
        return self.body.group_names

    @property
    def records(self) -> list:
        return self.body.records

    def __len__(self) -> int:
        return self.body.group_count

    def __iter__(self) -> Iterator[Group]:
        return iter(self.body)


class Container:
    """A file or message: one or more documents and an optional raw tail
    after EM. ``warnings`` holds loud warnings about the container as a
    whole; each document carries its own."""

    __slots__ = ("_buf", "documents", "has_em", "_em_pos", "warnings")

    def __init__(self, buf: Bytes):
        self._buf = buf
        docs, self.has_em, self._em_pos = _c0.documents(buf)
        self.documents = [Document(buf, _parsed=t) for t in docs]
        self.warnings = self._warnings()

    def _warnings(self) -> list:
        out = []
        for i, doc in enumerate(self.documents):
            raw = doc.heading.raw_blob
            if raw is None:
                continue
            if i > 0:
                out.append("blob table in a later heading is ignored")
            elif not raw or raw[0] != DLE:
                out.append("blob is not a level: declares nothing")
            else:
                table = doc.heading.blob
                groups = table.groups if table else []
                if not groups or not groups[0].has_header:
                    out.append("blob table has no header row: declares nothing")
        if self.has_em and self.blob_table is None:
            out.append("EM with no blob table: the region is unaddressable")
        return out

    @property
    def tail(self) -> memoryview:
        """The raw bytes after EM (empty without EM)."""
        mv = _mv(self._buf)
        return mv[self._em_pos + 1:] if self.has_em else mv[len(mv):]

    @property
    def first(self) -> Document:
        return self.documents[0]

    @property
    def blob_table(self) -> Optional[Body]:
        """The blob table from the first document's heading."""
        return self.documents[0].heading.blob if self.documents else None

    def blob(self, name: Str) -> Optional[bytes]:
        """Bytes of a named blob from the tail, or None."""
        try:
            s, e = _c0.blob(self._buf, _b(name))
        except NotFoundError:
            return None
        return bytes(_mv(self._buf)[s:e])

    def __len__(self) -> int:
        return len(self.documents)

    def __iter__(self) -> Iterator[Document]:
        return iter(self.documents)


# ---- stream mode -------------------------------------------------------------

class StreamReader:
    """An ETB-committed append log. An unescaped ETB is always a commit, even
    inside open text or a level. A committed block that is not well-formed,
    cut by its ETB or holding any other error, is **damaged**: nothing in it
    is delivered and it adds nothing to the parse state. By default the
    reader delivers the records before the first damaged block and then
    raises ``DamagedBlockError`` with the offset to resume at; with
    ``continue_past_damage=True`` it reads on past every damaged block. The
    tail after the last commit is torn unless it holds only layout, notes,
    EOT or EM."""

    __slots__ = ("_buf", "committed_end", "torn", "_blocks", "damaged", "continue_past_damage",
                 "resume_offset", "_sound")

    def __init__(self, buf: Bytes, continue_past_damage: bool = False):
        self._buf = buf
        self.continue_past_damage = continue_past_damage
        self.committed_end, self.torn, blocks, has_damaged, _first, resume = _c0.stream(buf)
        self._blocks = [(s, e) for s, e, _ in blocks]
        self.damaged = [i for i, (_, _, d) in enumerate(blocks) if d]
        self.resume_offset = resume if has_damaged else None
        self._sound = None

    @property
    def committed(self) -> memoryview:
        return _mv(self._buf)[:self.committed_end]

    @property
    def tail(self) -> memoryview:
        return _mv(self._buf)[self.committed_end:]

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    def block(self, i: int) -> memoryview:
        """Committed block ``i``: previous commit to ETB, marker and payload excluded."""
        s, e = self._blocks[i]
        return _mv(self._buf)[s:e]

    @property
    def blocks(self) -> list:
        mv = _mv(self._buf)
        return [mv[s:e] for s, e in self._blocks]

    def is_damaged(self, i: int) -> bool:
        return i in self.damaged

    @property
    def has_damaged(self) -> bool:
        return bool(self.damaged)

    def block_end(self, i: int) -> int:
        """Offset just past block ``i``'s ETB and payload: where reading can
        resume after it."""
        return self._blocks[i + 1][0] if i + 1 < len(self._blocks) else self.committed_end

    @property
    def undamaged(self) -> bytes:
        """Every sound committed block, markers and payloads removed,
        continuing past damage."""
        return _c0.stream_undamaged(self._buf)

    @property
    def before_damage(self) -> bytes:
        """The sound committed blocks before the first damaged one, markers
        and payloads removed (all of them when none is damaged)."""
        return _c0.stream_before_damage(self._buf)

    def _check_damage(self) -> None:
        if self.damaged and not self.continue_past_damage:
            raise DamagedBlockError(self.damaged[0], self.resume_offset)

    def _sound_container(self) -> Container:
        if self._sound is None:
            self._sound = Container(self.undamaged if self.continue_past_damage else self.before_damage)
        return self._sound

    @property
    def container(self) -> Container:
        """The committed region as documents, damaged blocks left out.
        Raises ``DamagedBlockError`` when a block is damaged, unless continuing."""
        self._check_damage()
        return self._sound_container()

    @property
    def document(self) -> Document:
        """The first document of the committed region."""
        return self.container.first

    @property
    def body(self) -> Body:
        return self.document.body

    def iter_records(self) -> Iterator[Record]:
        """Every committed record in order, header rows and heading excluded,
        up to the first damaged block; then ``DamagedBlockError``, unless
        continuing, in which case every record of every sound block."""
        for doc in self._sound_container().documents:
            for g in doc.body.groups:
                for r in g.records:
                    yield r
        self._check_damage()

    @property
    def records(self) -> list:
        """``iter_records()`` as a list."""
        return list(self.iter_records())

    def __len__(self) -> int:
        return len(self._blocks)

    def __iter__(self) -> Iterator[Record]:
        return self.iter_records()


# ---- builder -----------------------------------------------------------------

class _LevelScope:
    """Closes a level on exit: ``with b.level(): ...``."""

    __slots__ = ("_b", "_close")

    def __init__(self, b: "Builder", close: Optional[Callable[[], object]] = None):
        self._b = b
        self._close = close if close is not None else b._b.level_close

    def __enter__(self) -> "Builder":
        return self._b

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._close()
        return False


def _fields(args: tuple) -> list:
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        args = tuple(args[0])
    return [_b(f) for f in args]


def _pairs(pairs, kw: dict) -> list:
    out = []
    if pairs is not None:
        items = pairs.items() if isinstance(pairs, Mapping) else pairs
        out.extend((_b(k), _b(v)) for k, v in items)
    out.extend((k.encode("utf-8"), _b(v)) for k, v in kw.items())
    return out


class Builder:
    """Writes C0DATA in canonical compact form over the C builder.

    Two layers. The primitives are one method per code (``fs``, ``gs``,
    ``rs``, ``us``, ``sub``, ``soh``, ``dle``, ``can``, ``bel``, ``enq``,
    ``syn``, ``eot``, ``etb``, ``em``); each writes its code and, when given,
    what grammatically follows it, so a chain reads like the bytes::

        b.rs("Alice").us("1502.30")                        # ␞Alice␟1502.30
        b.rs().soh("host").us("localhost").soh("port").us("5432")
        b.bel().gs("old")                                  # a dead group
        b.rs("old").sub().us("new")                        # ␞old␚␟new: old superseded by new
        b.rs().enq("tags").us().enq(lambda: b.gs("tags").rs("001"))  # ␞␅tags␟␅␐␝tags␞001␘

    The composed methods (``file``, ``group``, ``header``, ``record``,
    ``field``, ``supersede``, ``map``, ``key``, ``level``, ``list``, ``ref``,
    ``note``) are built on the primitives and cannot write ill-formed output.
    The primitives write what you spell; well-formedness is then yours to
    keep. Both layers share the record state, so they mix.

    Values are byte-transparent and written in their one spelling: plain when
    they can be, text otherwise with only the seven codes escaped. Names
    (file, group, header, key, group reference, ETB payload) must be plain
    or ``BuildError`` is raised. Every method returns the builder, so calls
    chain; ``level()``, ``key_level()``, ``ref()`` and ``enq()`` without
    arguments are context managers that close the level on exit. Use ``with
    Builder() as b:`` or ``close()`` to release the buffer early; otherwise
    it is freed when the builder is collected.
    """

    __slots__ = ("_b",)

    def __init__(self):
        self._b = _c0.Builder()

    # ---- primitives: one method per code

    def fs(self, name: Str) -> "Builder":
        """FS and a file name. Ends any open record."""
        self._b.fs(_b(name))
        return self

    def gs(self, name: Optional[Str] = None) -> "Builder":
        """GS and, when given, a group name: ``gs().gs("sub")`` is a group at
        depth 2. Ends any open record."""
        if name is None:
            self._b.gs()
        else:
            self._b.gs(_b(name))
        return self

    def rs(self, value: Optional[Str] = None) -> "Builder":
        """RS and, when given, the record's first value."""
        if value is None:
            self._b.rs()
        else:
            self._b.rs(_b(value))
        return self

    def us(self, value: Optional[Str] = None) -> "Builder":
        """US and, when given, the value that follows it. Bare: an empty
        unit, or the separator before ``dle()`` / ``enq()``."""
        if value is None:
            self._b.us()
        else:
            self._b.us(_b(value))
        return self

    def sub(self) -> "Builder":
        """SUB: the element before the next separator or opener is
        superseded by the one after it. ``rs("old").sub().us("new")`` writes
        ``␞old␚␟new``; before ``rs``, ``gs``, ``fs`` or ``soh`` it supersedes
        at that grain."""
        self._b.sub()
        return self

    def soh(self, name: Optional[Str] = None) -> "Builder":
        """SOH and, when given, a key. Bare before ``rs()`` it opens a header row."""
        if name is None:
            self._b.soh()
        else:
            self._b.soh(_b(name))
        return self

    def dle(self) -> "Builder":
        """DLE: open a nested level. Inside, the builder is at the top of a fresh body."""
        self._b.dle()
        return self

    def can(self) -> "Builder":
        """CAN: close the innermost level. ``BuildError`` if none is open."""
        self._b.can()
        return self

    def bel(self, text: Optional[Str] = None) -> "Builder":
        """BEL and, when given, a note. Bare, it marks the element that
        follows dead. An empty note is a ``BuildError``."""
        if text is None:
            self._b.bel()
        else:
            self._b.bel(_b(text))
        return self

    def enq(self, name_or_fn: Union[Str, Callable[[], object], None] = None):
        """ENQ and a reference: a group name (``␅name``), or a pattern, a
        level that is the target with its values left out, built by the
        callable (``enq(lambda: b.gs("tags").rs("001").us("label"))`` writes
        ``␅␐␝tags␞001␟label␘``). With no argument, a context manager that
        opens the pattern and closes it on exit."""
        if name_or_fn is None:
            self._b.enq_dle()
            return _LevelScope(self, self._b.can)
        if callable(name_or_fn):
            self._b.enq_dle()
            name_or_fn()
            self._b.can()
            return self
        self._b.enq(_b(name_or_fn))
        return self

    def syn(self, pairs=None, **kw) -> "Builder":
        """SYN, then a heading when given: keys in the order given (a mapping,
        an iterable of pairs, or keyword arguments). Bare ``syn()`` writes the
        code alone; heading pairs then follow as ``soh(key).us(value)``."""
        self._b.syn()
        for k, v in _pairs(pairs, kw):
            self._b.soh(k)
            self._b.us(v)
        return self

    def eot(self) -> "Builder":
        """EOT. Ends any open record."""
        self._b.eot()
        return self

    def etb(self, payload: Optional[Str] = None) -> "Builder":
        """ETB and an optional plain payload."""
        if payload is None:
            self._b.etb()
        else:
            self._b.etb(_b(payload))
        return self

    def em(self, tail: Bytes) -> "Builder":
        """EM and the raw tail. Nothing may follow."""
        self._b.em(tail)
        return self

    # ---- composed: built on the primitives, cannot write ill-formed output

    # ---- structure

    def file(self, name: Str) -> "Builder":
        """FS and a file name."""
        return self.fs(name)

    def group(self, name: Str, headers: Optional[Iterable[Str]] = None, depth: int = 1) -> "Builder":
        """GS (repeated ``depth`` times) and a name, then a header row when given."""
        if depth < 1:
            raise ValueError("depth must be at least 1")
        for _ in range(depth - 1):
            self._b.gs()
        self._b.gs(_b(name))
        if headers is not None:
            self.header(headers)
        return self

    def section(self, name: Str, depth: int = 1) -> "Builder":
        """Document mode: a section at depth."""
        return self.group(name, None, depth)

    def header(self, *names) -> "Builder":
        """A header row: SOH RS name US name ..."""
        self._b.header(_fields(names))
        return self

    # ---- records

    def record(self, *fields) -> "Builder":
        """A positional record. With no fields, opens an empty record that
        ``field``, ``level``, ``list`` and ``ref`` extend."""
        self._b.record(_fields(fields))
        return self

    def field(self, value: Str) -> "Builder":
        """The next unit of the open record (opens one if none)."""
        self._b.field(_b(value))
        return self

    def supersede(self, old: Str, new: Str) -> "Builder":
        """A superseded unit: ``old`` superseded by ``new``, written
        ``old␚␟new`` as the next unit and the one after it."""
        self._b.supersede(_b(old), _b(new))
        return self

    def map(self, pairs=None, **kw) -> "Builder":
        """A keyed record: a map, entries in the order given."""
        ps = _pairs(pairs, kw)
        self._b.map([k for k, _ in ps], [v for _, v in ps])
        return self

    def key(self, name: Str, value: Str) -> "Builder":
        """One keyed unit, on the open record or in the heading after ``syn``."""
        self._b.key(_b(name), _b(value))
        return self

    def key_level(self, name: Str, fn: Optional[Callable[["Builder"], object]] = None):
        """A keyed unit whose value is a nested level. As a context manager
        the level closes on exit; with ``fn`` it runs ``fn(builder)`` inside
        and returns the builder."""
        self._b.key_level_open(_b(name))
        if fn is None:
            return _LevelScope(self)
        fn(self)
        self._b.level_close()
        return self

    def block(self, text: Str) -> "Builder":
        """Document mode: a paragraph (a record of one unit)."""
        self._b.block(_b(text))
        return self

    def item(self, text: Str) -> "Builder":
        """Document mode: a list item (the next unit of the open block)."""
        self._b.item(_b(text))
        return self

    # ---- values that are not plain

    def level(self, fn: Optional[Callable[["Builder"], object]] = None):
        """A nested level as the next unit. Inside, the builder is at the top
        of a fresh body. Context manager, or pass ``fn``."""
        self._b.level_open()
        if fn is None:
            return _LevelScope(self)
        fn(self)
        self._b.level_close()
        return self

    def list(self, items: Iterable[Str]) -> "Builder":
        """A flat list as the next unit: DLE RS item US item ... CAN (DLE CAN when empty)."""
        self._b.list([_b(i) for i in items])
        return self

    def ref(self, group: Union[Str, Callable[[], object], None] = None,
            record: Optional[Str] = None, field: Optional[Str] = None):
        """A reference as the next unit. ``ref("tags")`` is a group reference
        (``␅tags``); ``ref("tags", "001")`` and ``ref("tags", "001", "label")``
        are the patterns ``␅␐␝tags␞001␘`` and ``␅␐␝tags␞001␟label␘``;
        ``ref(fn)`` writes the pattern ``fn`` builds; with no argument, a
        context manager that opens the pattern and closes it on exit."""
        if group is None:
            self._b.ref_open()
            return _LevelScope(self)
        if callable(group):
            self._b.ref_open()
            group()
            self._b.level_close()
            return self
        if record is None:
            if field is not None:
                raise TypeError("ref() takes a field only with a record")
            self._b.ref(_b(group))
        else:
            self._b.ref_record(_b(group), _b(record), None if field is None else _b(field))
        return self

    # ---- comments

    def note(self, text: Str) -> "Builder":
        """A comment at the current position: BEL and the note. Content is
        required; a bare BEL is a dead marker, written with ``bel()``."""
        return self.bel(text)

    # ---- output

    @property
    def bytes(self) -> bytes:
        return self._b.bytes()

    def __bytes__(self) -> bytes:
        return self._b.bytes()

    @property
    def depth(self) -> int:
        """Open levels."""
        return self._b.depth()

    def close(self) -> None:
        """Free the C buffer now; the builder is then unusable."""
        self._b.close()

    def __enter__(self) -> "Builder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def build(fn: Callable[[Builder], object]) -> bytes:
    """Run ``fn`` against a fresh Builder and return its bytes."""
    with Builder() as b:
        fn(b)
        return b.bytes


# ---- C0DIFF ------------------------------------------------------------------

from . import diff  # noqa: E402  (needs Builder above)
from .diff import DiffBuilder, PatternBuilder, parse_diff  # noqa: E402
from .diff import apply as apply_diff  # noqa: E402
