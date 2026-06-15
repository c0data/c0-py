"""C0DATA — structured data using ASCII C0 control codes.

A thin Python layer over the c0 C core (the ``_c0`` extension). The read path is
zero-copy: accessors return ``memoryview`` slices into the input buffer.

>>> import c0
>>> buf = c0.build(lambda b: (
...     b.group("users", ["name", "amount"]),
...     b.record("Alice", "100"),
...     b.record("Bob", "200")))
>>> t = c0.Table(buf)
>>> bytes(t.record(0).field(0))
b'Alice'
"""
from __future__ import annotations

from enum import IntEnum
from typing import Iterator, Optional, Sequence, Union

from . import _c0

__all__ = [
    "SOH", "STX", "ETX", "EOT", "ENQ", "DLE", "ETB", "SUB", "FS", "GS", "RS", "US",
    "TokenType", "is_assigned", "canonical", "unescape", "tokenize",
    "pretty_format", "pretty_parse",
    "Record", "Table", "Group", "Document", "StreamReader", "Builder", "build",
]

# Assigned C0 control codes.
SOH, STX, ETX, EOT, ENQ, DLE = 0x01, 0x02, 0x03, 0x04, 0x05, 0x10
ETB, SUB, FS, GS, RS, US = 0x17, 0x1A, 0x1C, 0x1D, 0x1E, 0x1F

Bytes = Union[bytes, bytearray, memoryview]
Str = Union[str, Bytes]


class TokenType(IntEnum):
    DATA = 0
    SOH = 1
    STX = 2
    ETX = 3
    EOT = 4
    ENQ = 5
    ETB = 6
    SUB = 7
    FS = 8
    GS = 9
    RS = 10
    US = 11


def is_assigned(byte: int) -> bool:
    return _c0.is_assigned(byte)


def canonical(buf: Bytes) -> bool:
    """Whether bytes are a canonical document unit for content addressing."""
    return _c0.canonical(buf)


def unescape(buf: Bytes) -> bytes:
    """Decode DLE escapes, returning the logical bytes of a value."""
    return _c0.unescape(buf)


def tokenize(buf: Bytes) -> list[tuple[TokenType, int, int]]:
    """List of (type, start, end). Raises ValueError on malformed input."""
    return [(TokenType(t), s, e) for (t, s, e) in _c0.tokenize(buf)]


def pretty_format(buf: Bytes, indent: Optional[str] = None) -> str:
    """Format compact bytes as a human-readable Unicode string."""
    return _c0.pretty_format(buf, indent)


def pretty_parse(text: str) -> bytes:
    """Parse pretty text back to compact bytes."""
    return _c0.pretty_parse(text)


class Record:
    """A record: zero-copy field access over the underlying buffer."""

    __slots__ = ("_buf", "_start", "_end")

    def __init__(self, buf: Bytes, start: int, end: int):
        self._buf = buf
        self._start = start
        self._end = end

    @property
    def raw(self) -> memoryview:
        return memoryview(self._buf)[self._start:self._end]

    def _spans(self):
        return _c0.record_fields(self._buf, self._start, self._end)

    @property
    def fields(self) -> list[memoryview]:
        mv = memoryview(self._buf)
        return [mv[s:e] for s, e in self._spans()]

    def field(self, i: int) -> memoryview:
        s, e = self._spans()[i]
        return memoryview(self._buf)[s:e]

    def value(self, i: int) -> bytes:
        """Field i with DLE escapes decoded."""
        return unescape(self.field(i))

    @property
    def values(self) -> list[bytes]:
        mv = memoryview(self._buf)
        return [unescape(mv[s:e]) for s, e in self._spans()]

    def __len__(self) -> int:
        return len(self._spans())

    def __iter__(self) -> Iterator[memoryview]:
        mv = memoryview(self._buf)
        return (mv[s:e] for s, e in self._spans())


class Table:
    """A tabular group: name, headers, and records."""

    __slots__ = ("_buf", "_name", "_headers", "_records")

    def __init__(self, buf: Bytes, offset: int = 0):
        self._buf = buf
        ns, ne, headers, records = _c0.table(buf, offset)
        self._name = (ns, ne)
        self._headers = headers
        self._records = records

    @property
    def name(self) -> memoryview:
        return memoryview(self._buf)[self._name[0]:self._name[1]]

    @property
    def headers(self) -> list[memoryview]:
        mv = memoryview(self._buf)
        return [mv[s:e] for s, e in self._headers]

    @property
    def record_count(self) -> int:
        return len(self._records)

    def record(self, i: int) -> Record:
        s, e = self._records[i]
        return Record(self._buf, s, e)

    @property
    def records(self) -> list[Record]:
        return [self.record(i) for i in range(len(self._records))]

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Record]:
        return (self.record(i) for i in range(len(self._records)))


class Group:
    """A group within a document; read it as a Table."""

    __slots__ = ("_buf", "_start", "_end")

    def __init__(self, buf: Bytes, start: int, end: int):
        self._buf = buf
        self._start = start
        self._end = end

    @property
    def table(self) -> Table:
        return Table(self._buf, self._start)

    @property
    def name(self) -> memoryview:
        return self.table.name

    @property
    def raw(self) -> memoryview:
        return memoryview(self._buf)[self._start:self._end]

    def record(self, i: int) -> Record:
        return self.table.record(i)

    @property
    def record_count(self) -> int:
        return self.table.record_count


class Document:
    """A full document: navigate its top-level groups."""

    __slots__ = ("_buf", "_name", "_groups")

    def __init__(self, buf: Bytes):
        self._buf = buf
        ns, ne, groups = _c0.document(buf)
        self._name = (ns, ne)
        self._groups = groups

    @property
    def name(self) -> memoryview:
        return memoryview(self._buf)[self._name[0]:self._name[1]]

    @property
    def group_count(self) -> int:
        return len(self._groups)

    def group(self, i: int) -> Group:
        s, e = self._groups[i]
        return Group(self._buf, s, e)

    def group_by_name(self, name: Str) -> Optional[Group]:
        needle = name.encode() if isinstance(name, str) else bytes(name)
        for g in self:
            if bytes(g.name) == needle:
                return g
        return None

    def __len__(self) -> int:
        return len(self._groups)

    def __iter__(self) -> Iterator[Group]:
        return (self.group(i) for i in range(len(self._groups)))


class StreamReader:
    """An append-only log: committed region, torn-tail detection, blocks."""

    __slots__ = ("_buf", "committed_end", "torn", "_blocks")

    def __init__(self, buf: Bytes):
        self._buf = buf
        self.committed_end, self.torn, self._blocks = _c0.stream(buf)

    @property
    def committed(self) -> memoryview:
        return memoryview(self._buf)[:self.committed_end]

    @property
    def tail(self) -> memoryview:
        return memoryview(self._buf)[self.committed_end:]

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    def block(self, i: int) -> memoryview:
        s, e = self._blocks[i]
        return memoryview(self._buf)[s:e]

    @property
    def blocks(self) -> list[memoryview]:
        mv = memoryview(self._buf)
        return [mv[s:e] for s, e in self._blocks]

    @property
    def table(self) -> Table:
        return Table(self.committed)


def _as_bytes(s: Str) -> bytes:
    return s.encode("utf-8") if isinstance(s, str) else bytes(s)


class Builder:
    """Builds C0DATA compact bytes. Byte-identical to the C builder.

    Names (file/group/header) reject control bytes; record field values are
    byte-transparent and DLE-escaped automatically.
    """

    def __init__(self):
        self._buf = bytearray()

    def _name(self, s: Str) -> None:
        b = _as_bytes(s)
        if any(byte < 0x20 for byte in b):
            raise ValueError("names may not contain control bytes")
        self._buf += b

    def _escaped(self, s: Str) -> None:
        for byte in _as_bytes(s):
            if byte < 0x20:
                self._buf.append(DLE)
            self._buf.append(byte)

    def file(self, name: Str) -> "Builder":
        self._buf.append(FS)
        self._name(name)
        return self

    def group(self, name: Str, headers: Optional[Sequence[Str]] = None) -> "Builder":
        self._buf.append(GS)
        self._name(name)
        if headers is not None:
            self.header(headers)
        return self

    def header(self, names: Sequence[Str]) -> "Builder":
        self._buf.append(SOH)
        for i, n in enumerate(names):
            if i:
                self._buf.append(US)
            self._name(n)
        return self

    def record(self, *fields: Str) -> "Builder":
        if len(fields) == 1 and isinstance(fields[0], (list, tuple)):
            fields = tuple(fields[0])
        self._buf.append(RS)
        for i, f in enumerate(fields):
            if i:
                self._buf.append(US)
            self._escaped(f)
        return self

    def eot(self) -> "Builder":
        self._buf.append(EOT)
        return self

    def etb(self) -> "Builder":
        self._buf.append(ETB)
        return self

    @property
    def bytes(self) -> bytes:
        return bytes(self._buf)


def build(fn) -> bytes:
    """Run ``fn`` against a fresh Builder and return its bytes."""
    b = Builder()
    fn(b)
    return b.bytes
