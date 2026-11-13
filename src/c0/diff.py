"""C0DIFF: atomic multi-file edits with anchored patterns (DIFF.md).

A diff is an ordinary C0DATA document of ``shape`` ``diff``: files (FS, the
path) of records (RS), each record one anchored pattern of US-separated
units, an anchor or a substitution written as supersession at the unit
grain, ``old␚␟new``. A record's search text (its anchors and olds) must
match exactly once from the previous record's match to the end of the
file; the replacement puts each new in place of its old. A record with no
substitution is an assertion. Notes are allowed; everything the text diff
does not use is refused (``DiffError``): keyed units, levels, references,
header rows, GS, supersession at another grain, chained supersession, dead
records and dead file lines (a hunk is kept without applying by writing it
in a note, ``␇␂␞…␃``), a record before the first FS, an empty path, the
same path twice, an empty search text, a bare insertion, ``␟␚␟``, a second
document after the diff's EOT. There is no replace-all: a tool expands one
into anchored records against the target.

>>> import c0
>>> diff = c0.diff.build(lambda d: (
...     d.file("greeting.txt"),
...     d.record(lambda r: (r.anchor("Hello "), r.sub("world", "universe"), r.anchor("!"))),
...     d.eot()))
>>> c0.diff.apply(diff, {"greeting.txt": "Hello world!"})
{'greeting.txt': 'Hello universe!'}

The parsing and applying run in the C core (``c0_diff.c``); the builder is
the ordinary ``Builder``'s primitives.
"""
from __future__ import annotations

import os
from typing import Callable, Iterator, List, Mapping, Optional, Union

from . import _c0
from .errors import DiffError

Bytes = Union[bytes, bytearray, memoryview]
Str = Union[str, Bytes]

__all__ = [
    "DiffError", "Unit", "Pattern", "FileEdit",
    "parse_diff", "apply", "apply_files", "build", "DiffBuilder", "PatternBuilder",
]


def _b(s: Str) -> bytes:
    return s.encode("utf-8") if isinstance(s, str) else bytes(s)


# ---- parsed model --------------------------------------------------------------

class Unit:
    """One unit of a pattern: an anchor (``new`` is None) or a substitution
    ``old␚␟new``. Values are decoded bytes."""

    __slots__ = ("old", "new")

    def __init__(self, old: bytes, new: Optional[bytes]):
        self.old = old
        self.new = new

    @property
    def is_anchor(self) -> bool:
        return self.new is None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unit) and (self.old, self.new) == (other.old, other.new)

    def __repr__(self) -> str:
        return f"Unit({self.old!r})" if self.new is None else f"Unit({self.old!r} -> {self.new!r})"


class Pattern:
    """One record: an anchored pattern."""

    __slots__ = ("units",)

    def __init__(self, units: List[Unit]):
        self.units = list(units)

    @property
    def search(self) -> bytes:
        """What the file must hold, once: anchors and superseded units."""
        return b"".join(u.old for u in self.units)

    @property
    def replacement(self) -> bytes:
        """What it will hold: superseding units in place of superseded ones."""
        return b"".join(u.old if u.new is None else u.new for u in self.units)

    @property
    def is_assertion(self) -> bool:
        return all(u.new is None for u in self.units)

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self) -> Iterator[Unit]:
        return iter(self.units)

    def __repr__(self) -> str:
        return f"Pattern({self.units!r})"


class FileEdit:
    """A file's records, in order. ``path`` is bytes."""

    __slots__ = ("path", "records")

    def __init__(self, path: bytes, records: List[Pattern]):
        self.path = path
        self.records = list(records)

    def __repr__(self) -> str:
        return f"FileEdit({self.path!r}, {len(self.records)} records)"


def parse_diff(buf: Bytes) -> List[FileEdit]:
    """Parse a diff document (with or without its SYN heading), refusing
    everything DIFF.md lists under "What a diff may not hold". Raises
    ``C0SyntaxError`` when the bytes are malformed, ``DiffError`` otherwise."""
    return [FileEdit(path, [Pattern([Unit(old, new) for old, new in rec]) for rec in records])
            for path, records in _c0.diff_parse(buf)]


# ---- apply ----------------------------------------------------------------------

def apply(diff_buf: Bytes, files: Mapping) -> dict:
    """Apply a diff to in-memory files, ``{path: content}`` with ``str`` or
    ``bytes`` paths and contents. Atomic: everything is validated before
    anything is produced; raises ``DiffError`` (or ``C0SyntaxError``) and
    returns nothing on any error. Returns a new mapping of the same keys
    with the edited contents, each in the type its input had."""
    keys = list(files.keys())
    inputs = [(_b(k), _b(files[k])) for k in keys]
    outputs = _c0.diff_apply(diff_buf, inputs)
    out = {}
    for k, (_path, content) in zip(keys, outputs):
        out[k] = content.decode("utf-8") if isinstance(files[k], str) else content
    return out


def apply_files(diff_buf: Bytes, base_dir: str = ".") -> None:
    """Apply a diff to files on disk under ``base_dir``, atomically: every
    file is read and validated before any is written, and each is written
    through a temporary file and a rename."""
    edits = parse_diff(diff_buf)
    base = os.path.realpath(base_dir)
    inputs = []
    targets = []
    for e in edits:
        path = e.path.decode("utf-8")
        target = os.path.realpath(os.path.join(base, path))
        if not (target == base or target.startswith(base + os.sep)):
            raise DiffError(f"path escapes the target directory: {path}")
        if not os.path.isfile(target):
            raise DiffError(f"no such file: {path}")
        with open(target, "rb") as f:
            inputs.append((e.path, f.read()))
        targets.append(target)
    outputs = _c0.diff_apply(diff_buf, inputs)
    for target, (_path, content) in zip(targets, outputs):
        tmp = target + ".c0diff.tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, target)


# ---- builder --------------------------------------------------------------------

class PatternBuilder:
    """The units of one record: ``anchor(text)`` and ``sub(old, new)``,
    in the order they must appear in the file."""

    __slots__ = ("_b",)

    def __init__(self, builder):
        self._b = builder

    def anchor(self, text: Str) -> "PatternBuilder":
        """Text that must be present, unchanged."""
        self._b.field(text)
        return self

    def sub(self, old: Str, new: Str) -> "PatternBuilder":
        """``old␚␟new``: ``old`` replaced by ``new``. Either may be empty
        (an insertion after the anchor before it, or a deletion), not both."""
        self._b.supersede(old, new)
        return self


class _RecordScope:
    __slots__ = ("_p",)

    def __init__(self, p: PatternBuilder):
        self._p = p

    def __enter__(self) -> PatternBuilder:
        return self._p

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class DiffBuilder:
    """Writes a C0DIFF document over the ordinary ``Builder``. With
    ``heading`` (the default) it starts with ``␖␁shape␟diff``. Every method
    returns the builder, so calls chain; ``bytes`` is the result.

    ::

        d = c0.DiffBuilder()
        d.file("greeting.txt")
        d.record(lambda r: (r.anchor("Hello "), r.sub("world", "universe"), r.anchor("!")))
        d.insert_after("universe!", "\\n")
        d.assert_("Hello")         # must be present once; nothing changes
        d.eot()
        c0.diff.apply(d.bytes, {"greeting.txt": "Hello world!"})
    """

    __slots__ = ("_b",)

    def __init__(self, heading: bool = True):
        from . import Builder  # the package imports this module after Builder is defined
        self._b = Builder()
        if heading:
            self._b.syn().key("shape", "diff")

    @property
    def builder(self):
        """The underlying ``Builder``, for notes and anything else."""
        return self._b

    def file(self, path: Str, fn: Optional[Callable[["DiffBuilder"], object]] = None) -> "DiffBuilder":
        """FS and the path (a name); the records that follow are its edits.
        With ``fn``, runs ``fn(self)`` for them."""
        self._b.fs(path)
        if fn is not None:
            fn(self)
        return self

    def record(self, fn: Optional[Callable[[PatternBuilder], object]] = None):
        """One anchored pattern: RS, then the units ``fn`` writes on the
        ``PatternBuilder`` it is given. Without ``fn``, a context manager
        yielding the ``PatternBuilder``."""
        self._b.rs()
        p = PatternBuilder(self._b)
        if fn is None:
            return _RecordScope(p)
        fn(p)
        return self

    def replace(self, before: Str, old: Str, new: Str, after: Str = "") -> "DiffBuilder":
        """One record: anchor, ``old␚␟new``, anchor (empty anchors are omitted)."""
        self._b.rs()
        if _b(before):
            self._b.field(before)
        self._b.supersede(old, new)
        if _b(after):
            self._b.field(after)
        return self

    def insert_after(self, anchor: Str, text: Str) -> "DiffBuilder":
        """One record: insert ``text`` after ``anchor`` (``␞anchor␟␚␟text``)."""
        self._b.rs().field(anchor).supersede("", text)
        return self

    def delete(self, old: Str, before: Str = "", after: Str = "") -> "DiffBuilder":
        """One record: remove ``old``, found once after the cursor, between
        optional anchors."""
        return self.replace(before, old, "", after)

    def assert_(self, text: Str) -> "DiffBuilder":
        """One record: assert that ``text`` is present once; nothing changes."""
        self._b.rs().field(text)
        return self

    def note(self, text: Str) -> "DiffBuilder":
        """BEL and a note. A hunk written inside one (``␇␂␞…␃``) is kept
        without being applied."""
        self._b.note(text)
        return self

    def eot(self) -> "DiffBuilder":
        self._b.eot()
        return self

    @property
    def bytes(self) -> bytes:
        return self._b.bytes

    def __bytes__(self) -> bytes:
        return self._b.bytes

    def close(self) -> None:
        self._b.close()

    def __enter__(self) -> "DiffBuilder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def build(fn: Callable[[DiffBuilder], object], heading: bool = True) -> bytes:
    """Run ``fn`` against a fresh ``DiffBuilder`` and return its bytes."""
    with DiffBuilder(heading) as d:
        fn(d)
        return d.bytes
