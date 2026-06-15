"""Runs the shared conformance vectors from the c0-spec submodule."""
import json
import unittest
from pathlib import Path

import c0

VEC = Path(__file__).resolve().parent.parent / "c0-spec" / "vectors"


def cases(name):
    return json.loads((VEC / name).read_text())["cases"]


def hexbytes(h):
    return bytes.fromhex(h)


def field_bytes(f):
    # A field is a JSON string (UTF-8) or {"hex": "..."} (raw bytes).
    return f.encode("utf-8") if isinstance(f, str) else bytes.fromhex(f["hex"])


class Decode(unittest.TestCase):
    def _check_table(self, t, g):
        self.assertEqual(bytes(t.name), g["name"].encode())
        if g["headers"]:
            self.assertEqual([bytes(h) for h in t.headers],
                             [h.encode() for h in g["headers"]])
        else:
            self.assertEqual(len(t.headers), 0)
        recs = g["records"]
        self.assertEqual(t.record_count, len(recs))
        for i, r in enumerate(recs):
            rec = t.record(i)
            self.assertEqual(len(rec), len(r))
            for j, f in enumerate(r):
                self.assertEqual(rec.value(j), field_bytes(f))

    def test_decode(self):
        for c in cases("decode.json"):
            with self.subTest(c["name"]):
                buf = hexbytes(c["bytes"])
                groups = c["groups"]
                if c["file"] is None and len(groups) == 1 and groups[0]["name"] == "":
                    self._check_table(c0.Table(buf), groups[0])
                else:
                    doc = c0.Document(buf)
                    self.assertEqual(bytes(doc.name), (c["file"] or "").encode())
                    self.assertEqual(doc.group_count, len(groups))
                    for i, g in enumerate(groups):
                        self._check_table(doc.group(i).table, g)


class Encode(unittest.TestCase):
    def test_encode(self):
        for c in cases("encode.json"):
            with self.subTest(c["name"]):
                spec = c["build"]
                b = c0.Builder()
                if spec["file"] is not None:
                    b.file(spec["file"])
                for g in spec["groups"]:
                    b.group(g["name"], g["headers"])
                    for r in g["records"]:
                        b.record([field_bytes(f) for f in r])
                self.assertEqual(b.bytes.hex(), c["canonical"])
                self.assertTrue(c0.canonical(b.bytes))


class Canonical(unittest.TestCase):
    def test_canonical(self):
        for c in cases("canonical.json"):
            with self.subTest(c["name"]):
                buf = hexbytes(c["bytes"])
                wellformed = True
                try:
                    c0.tokenize(buf)
                except ValueError:
                    wellformed = False
                self.assertEqual(wellformed, c["wellformed"])
                self.assertEqual(c0.canonical(buf), c["canonical"])


class Invalid(unittest.TestCase):
    def test_invalid(self):
        for c in cases("invalid.json"):
            with self.subTest(c["name"]):
                with self.assertRaises(ValueError):
                    c0.tokenize(hexbytes(c["bytes"]))


class Stream(unittest.TestCase):
    def test_stream(self):
        for c in cases("stream.json"):
            with self.subTest(c["name"]):
                buf = hexbytes(c["bytes"])
                r = c0.StreamReader(buf)
                self.assertEqual(r.committed_end, c["committed_end"])
                self.assertEqual(r.torn, c["torn"])
                self.assertEqual(r.block_count, len(c["blocks"]))
                for i, h in enumerate(c["blocks"]):
                    self.assertEqual(bytes(r.block(i)).hex(), h)
                if "records" in c:
                    t = r.table
                    self.assertEqual(t.record_count, len(c["records"]))
                    for i, rr in enumerate(c["records"]):
                        got = [bytes(v) for v in t.record(i).values]
                        self.assertEqual(got, [x.encode() for x in rr])


if __name__ == "__main__":
    unittest.main()
