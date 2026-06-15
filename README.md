# c0

A Python implementation of [C0DATA](https://github.com/trans/c0data) — structured
data built on ASCII C0 control codes.

This is **not** a reimplementation: it's a thin, fast binding over the
[c0 C core](https://github.com/trans/c0-c) (a CPython extension, no third-party
dependencies), so the scan-heavy work runs as native C while the Python layer
provides an idiomatic API. The read path is **zero-copy** — accessors return
`memoryview` slices into the input buffer.

## Usage

```python
import c0

# Write
buf = c0.build(lambda b: (
    b.group("users", ["name", "amount"]),
    b.record("Alice", "100"),
    b.record("Bob", "200"),
))

# Read (zero-copy: fields are memoryviews into buf)
t = c0.Table(buf)
for rec in t:
    name, amount = rec.fields            # list[memoryview]
    rec.value(0)                         # bytes, DLE-escapes decoded

# Compact form is canonical — hashable for content addressing
assert c0.canonical(buf)

# Documents, streams, pretty
doc = c0.Document(buf)
log = c0.StreamReader(open("claims.c0", "rb").read())   # .torn, .committed, blocks
print(c0.pretty_format(buf))                            # Unicode Control Pictures
```

## Install / build

The C core and the shared conformance vectors are git submodules:

```sh
git submodule update --init        # pulls in c0-c (the header) and c0-spec
pip install .                      # builds the extension
```

Requires a C compiler and the Python development headers.

## Status

Binds the c0-c core: tokenizer, table/record and document/group readers (zero-copy),
canonical helpers, ETB stream mode, and pretty (`pretty_format`/`pretty_parse`).
The builder is pure Python (byte-identical to the C builder). Passes the shared
conformance vectors from [c0-spec](https://github.com/trans/c0-spec).

Converters (CSV / JSON / C0DIFF) are not yet wrapped.

## Test

```sh
python -m unittest discover -s tests
```

## License

MIT
