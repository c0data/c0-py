# c0

A Python implementation of [C0DATA](https://github.com/c0data) 1.0 — structured
data built on ASCII C0 control codes.

This is **not** a reimplementation: it's a thin, fast binding over the
[c0 C core](https://github.com/c0data/c0-c) (a CPython extension, no third-party
dependencies), so the scan-heavy work runs as native C while the Python layer
provides an idiomatic API. The read path is **zero-copy** — raw accessors return
`memoryview` slices into the input buffer; logical values come back as `bytes`,
with `str` conveniences beside them.

## Usage

```python
import c0

# Write: SYN heading, a group with a header row (␁␞), records, EOT
buf = c0.build(lambda b: (
    b.syn(schema="ledger"),
    b.group("users", ["name", "amount"]),
    b.record("Alice", "100"),
    b.record("Bob", "200"),
    b.eot(),
))
# ␖␁schema␟ledger␝users␁␞name␟amount␞Alice␟100␞Bob␟200␄

# Read (parse is validate: malformed input raises c0.C0SyntaxError)
doc = c0.Document(buf)                  # or c0.Container(buf) for several documents / an EM tail
doc.heading.schema                      # b'ledger'
users = doc.group("users")              # by name or index; doc["users"] too
users.headers                           # [b'name', b'amount']
for rec in users:
    rec.values                          # [b'Alice', b'100'] — plain trimmed, text decoded
    rec.string("amount")                # '100', by header name or index
    rec.field(0)                        # memoryview of the raw unit (zero-copy)

# The body is a canonical span — hashable for content addressing
assert c0.canonical(c0.build(lambda b: (b.group("users", ["name", "amount"]),
                                        b.record("Alice", "100"), b.record("Bob", "200"))))
c0.validate(buf)                        # the same grammar check on a buffer, without building a model

# Streams and pretty form
log = c0.StreamReader(open("claims.c0", "rb").read())   # .torn, .blocks, .damaged, .records
print(c0.pretty_format(buf))                            # Unicode Control Pictures
```

The model is the **result**: a dead element (`␇␞…`) is parsed and dropped
with everything it holds (a dead group takes its header row, records and
subsections, a dead file its groups). SUB makes the element after it a
child of the element before, at that code's grain: `␚␝s` is a subsection
at depth two (`g.depth`; a run of SUB is the depth, never skipped), and
`␞a␟b␚␟c` is two units with `c` a sub-unit of `b`, not a unit of the
record (`field_count`, `values`, `pairs` see units only). `rec.unit(n)` and
`rec.unit(key)` give a `Unit` with its `subs`, each a `Unit` in turn;
`rec.units` lists them all. `␚␞`, `␚␜` and `␚␁` are reserved and refused.
A SUB prefix is structure: canonical form keeps it. Canonical checking is
opt-in (`c0.canonical`) and never fails parsing.

Values are written in their one spelling: plain when they can be, text
(`␂…␃`) when they hold a control byte or edge whitespace. Keyed records
(`␞␁host␟localhost␁port␟5432`) are maps that keep their order:

```python
buf = c0.build(lambda b: (b.group("database"), b.map(host="localhost", port="5432")))
rec = c0.Body(buf).group(0).record(0)
rec.keyed, rec.keys, rec["port"]        # True, [b'host', b'port'], b'5432'
```

### Nested levels, lists and references

A unit may be a nested level, `␐…␘`, whose contents are a whole body. A
level holding one record is a list; `Builder.list` writes one and
`Record.list` reads it back.

```python
buf = c0.build(lambda b: (
    b.group("users", ["name", "roles", "address"]),
    b.record("Alice"),
    b.list(["Admin", "Editor"]),                     # ␐␞Admin␟Editor␘
    b.level(lambda l: (l.header("street", "city"),   # ␐␁␞street␟city␞1 Main␟Springfield␘
                       l.record("1 Main", "Springfield"))),
))
rec = c0.Body(buf).group(0).record(0)
rec.list(1)                              # [b'Admin', b'Editor']
rec.level(2).record(0).string("city")    # 'Springfield'
```

`level()` and `key_level(name)` also work as context managers (`with b.level(): …`).

A reference is a group name written plain, `b.ref("tags")` (`␅tags`), or a
**pattern**: a level that is one branch of the target with its values left
out, `b.ref("tags", "001", "label")` (`␅␐␝tags␞001␟label␘`) or any pattern
built in a callable, `b.ref(lambda: b.gs("s").rs().soh("host").us())`
(`␅␐␝s␞␁host␟␘`). `Record.ref` reads one back as a `Reference` with a
`name` or a `pattern` (the level as a `Body`); `ref.path` linearises it and
`ref.is_group` says whether it names a group as a whole. `b.sub_unit("new")`
writes `␚␟new`, a sub-unit of the unit just written (`depth=2` one deeper);
`b.group("intro", depth=2)` writes `␚␝intro`, a subsection.

The builder also has `file`, `section`, `header`, `field`, `key`, `note`,
`etb(payload)`, `em(tail)`, `block` and `item`, matching the Crystal reference.
Every error derives from `c0.C0Error`.

### Primitives

Underneath, the builder has one method per code: `fs`, `gs`, `rs`, `us`,
`sub`, `soh`, `dle`, `can`, `bel`, `enq`, `syn`, `eot`, `etb`, `em`. Each
writes its code and, when given, what follows it, so a chain reads like the
bytes, and anything the grammar allows can be written. The argument is
optional where the grammar allows the code alone (`gs()`, `rs()`, `us()`,
`soh()`, `bel()`, `etb()`); `sub()` is bare; `enq` takes a group name or a
callable that writes a pattern:

```python
b.rs("Alice").us("1502.30")                                 # ␞Alice␟1502.30
b.rs().soh("host").us("localhost").soh("port").us("5432")   # ␞␁host␟localhost␁port␟5432
b.gs("users").soh().rs("name").us("amount")                 # ␝users␁␞name␟amount: a header row
b.rs("cfg").us().dle().rs().soh("b").us("2").can()          # ␞cfg␟␐␞␁b␟2␘
b.bel("checked").rs("x")                                    # ␇checked␞x: a note
b.bel().gs("old")                                           # ␇␝old: a dead group
b.rs("old").sub().us("new")                                 # ␞old␚␟new: new is a sub-unit of old
b.gs("top").gs("sub", depth=2)                              # ␝top␚␝sub: a subsection
b.gs("sub").rs().enq("users").us().enq(lambda: b.gs("users").rs("1"))  # ␝sub␞␅users␟␅␐␝users␞1␘
b.syn().soh("version").us("1").rs("a").eot()                # ␖␁version␟1␞a␄
```

The composed methods above are built on these and cannot write ill-formed
output; with the primitives, well-formedness is yours. Values keep their one
spelling either way (there is no STX/ETX primitive), and names are still
checked. The two layers share the record state and mix freely:
`b.rs("a").field("b")` writes `␞a␟b`, `b.record().us("c")` writes `␞␟c`.

## Install / build

```sh
pip install c0data-py     # the package is c0data-py (PyPI treats c0data as codata); in code it is `import c0`
```

From a checkout, the C core and the shared conformance vectors are git submodules:

```sh
git submodule update --init        # pulls in c0-c (the header) and c0-spec
pip install .                      # builds the extension
```

Requires a C compiler and the Python development headers.

### C0DIFF

A diff (`DIFF.md`) is an ordinary document of `shape` `diff`: files of
records, each record one anchored pattern whose substitutions are written
as a sub-unit, `old␚␟new`: `new` is the sub-unit of `old`.

```
␖␁shape␟diff
␜greeting.txt
␞␂Hello ␃␟world␚␟universe␟!
␄
```

```python
diff = c0.diff.build(lambda d: (
    d.file("greeting.txt"),
    d.record(lambda r: (r.anchor("Hello "), r.sub("world", "universe"), r.anchor("!"))),
    d.insert_after("universe!", "\n"),
    d.assert_("Hello"),                       # must be present once; nothing changes
    d.eot(),
))
c0.diff.apply(diff, {"greeting.txt": source})   # validates everything first; a dict of the results
c0.diff.apply_files(diff, "src")                 # on disk, atomically
c0.parse_diff(diff)                              # [FileEdit(path, [Pattern([Unit(old, new), …])])]
```

Each record's search text must match once from the previous match to the
end of the file. There is no replace-all: a tool expands one into anchored
records against the target (DIFF.md). Anything the text diff does not use
(keyed units, levels, references, header rows, GS, dead records and dead
file lines, two sub-units on one unit, a sub-unit of a sub-unit, an empty
search text, a bare insertion, `␟␚␟`, a record before the first FS, an
empty path, the same path twice) is refused with `c0.DiffError`; a hunk is
kept without applying by writing it in a note, `␇␂␞…␃`.

### Stream logs

```python
r = c0.StreamReader(open("events.c0", "rb").read())
r.torn              # uncommitted tail?
r.damaged           # indexes of committed blocks that are not well-formed
for rec in r:       # every committed record, header rows and heading excluded
    ...
```

Any error inside a committed block damages that block alone: it delivers
nothing and adds nothing to the parse state. By default the reader yields
the records before it and raises `c0.DamagedBlockError` with the index and
the offset to resume at (`r.resume_offset`); with
`c0.StreamReader(buf, continue_past_damage=True)` it reads on past it.

## Status

Binds the c0-c 1.0 core at c0-spec f677533: one parser (parse is validate),
zero-copy body/document/container readers reporting the result model
(files, groups with SUB GS depth, header rows, positional and keyed records
with their units and sub-units, nested levels, reference patterns, headings
with `schema`, EM blobs), canonical check, the builder, ETB stream mode
with damaged blocks, pretty form (`pretty_format` / `pretty_parse`) and
C0DIFF (`c0.diff`). Passes all 295 shared conformance vectors from
[c0-spec](https://github.com/c0data/c0-spec).

Converters (CSV / JSON) are not yet wrapped.

## Test

```sh
python -m unittest discover -s tests
```

## License

MIT
