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
    b.syn(shape="tabular"),
    b.group("users", ["name", "amount"]),
    b.record("Alice", "100"),
    b.record("Bob", "200"),
    b.eot(),
))
# ␖␁shape␟tabular␝users␁␞name␟amount␞Alice␟100␞Bob␟200␄

# Read
doc = c0.Document(buf)                  # or c0.Container(buf) for several documents / an EM tail
doc.shape                               # 'tabular'
users = doc.group("users")              # by name or index; doc["users"] too
users.headers                           # [b'name', b'amount']
for rec in users:
    rec.values                          # [b'Alice', b'100'] — plain trimmed, text decoded
    rec.string("amount")                # '100', by header name or index
    rec.field(0)                        # memoryview of the raw unit (zero-copy)

# The body is the canonical unit — hashable for content addressing
assert c0.canonical(c0.build(lambda b: (b.group("users", ["name", "amount"]),
                                        b.record("Alice", "100"), b.record("Bob", "200"))))
c0.validate(buf)                        # raises c0.C0SyntaxError on malformed input

# Streams and pretty form
log = c0.StreamReader(open("claims.c0", "rb").read())   # .torn, .blocks, .damaged, .records
print(c0.pretty_format(buf))                            # Unicode Control Pictures
```

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
References are `b.ref("tags")` (`␅tags`) or `b.ref("tags", "001", "label")`
(`␅␂tags␟001␟label␃`), read back with `Record.ref`.

The builder also has `file`, `section`, `header`, `field`, `key`, `note`,
`etb(payload)`, `em(tail)`, `block` and `item`, matching the Crystal reference.
Every error derives from `c0.C0Error`.

## Install / build

The C core and the shared conformance vectors are git submodules:

```sh
git submodule update --init        # pulls in c0-c (the header) and c0-spec
pip install .                      # builds the extension
```

Requires a C compiler and the Python development headers.

## Status

Binds the c0-c 1.0 core: validator, zero-copy body/document/container readers
(files, groups, header rows, positional and keyed records, nested levels,
references, headings, EM blobs), canonical check, the builder, ETB stream mode
and pretty form (`pretty_format`/`pretty_parse`). Passes the shared
conformance vectors from [c0-spec](https://github.com/c0data/c0-spec).

Converters (CSV / JSON / C0-DIFF) are not yet wrapped.

## Test

```sh
python -m unittest discover -s tests
```

## License

MIT
