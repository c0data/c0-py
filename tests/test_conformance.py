"""Runs the shared conformance vectors from the c0-spec submodule (schema v2;
see c0-spec/vectors/README.md): decode, encode, canonical, invalid, stream,
nested and diff. The harness mirrors c0-cr's spec/conformance_spec.cr."""
import json
import unittest
from pathlib import Path

import c0

VEC = Path(__file__).resolve().parent.parent / "c0-spec" / "vectors"


def cases(name):
    return json.loads((VEC / name).read_text())["cases"]


def scalar(f):
    return isinstance(f, str) or (isinstance(f, dict) and "hex" in f)


def scalar_bytes(f):
    return f.encode("utf-8") if isinstance(f, str) else bytes.fromhex(f["hex"])


def flat_groups(groups, depth=1):
    """Flatten a group list with "sections" into (depth, group) pairs. A
    section with a null name is a skipped depth: no group stands there."""
    out = []
    for g in groups:
        if g["name"] is not None:
            out.append((depth, g))
        if "sections" in g:
            out.extend(flat_groups(g["sections"], depth + 1))
    return out


class Checks(unittest.TestCase):
    """Shared checkers over the decoded structure."""

    def check_value(self, rec, i, f):
        if scalar(f):
            self.assertEqual(rec.value(i), scalar_bytes(f))
        elif "level" in f:
            self.check_body(rec.level(i), f["level"])
        elif "ref" in f:
            ref = f["ref"]
            r = rec.ref(i)
            if isinstance(ref, str):
                self.assertEqual(r.name, ref.encode())
                self.assertIsNone(r.pattern)
            else:
                self.assertIsNone(r.name)
                self.check_body(r.pattern, ref["level"])
        else:
            raise ValueError("unknown value form %r" % (f,))

    def check_record(self, rec, r):
        if isinstance(r, list):
            self.assertFalse(rec.keyed)
            self.assertEqual(rec.field_count, len(r))
            for j, f in enumerate(r):
                self.check_value(rec, j, f)
        else:
            self.assertTrue(rec.keyed)
            pairs = r["keys"]
            self.assertEqual(rec.keys, [p[0].encode() for p in pairs])
            got = rec.pairs
            for j, p in enumerate(pairs):
                self.assertEqual(got[j][1], scalar_bytes(p[1]))

    def check_group(self, g, e):
        self.assertEqual(g.name, e["name"].encode())
        if e["headers"] is not None:
            self.assertEqual(g.headers, [h.encode() for h in e["headers"]])
        else:
            self.assertFalse(g.has_header)
        records = e["records"]
        self.assertEqual(g.record_count, len(records))
        for i, r in enumerate(records):
            self.check_record(g.record(i), r)

    def check_groups(self, groups, expected):
        flat = flat_groups(expected)
        self.assertEqual(len(groups), len(flat))
        for i, (depth, e) in enumerate(flat):
            g = groups[i]
            self.check_group(g, e)
            if not (e["name"] == "" and i == 0):
                self.assertEqual(g.depth, depth)

    def check_body(self, body, e):
        if "files" in e:
            self.check_files(body, e["files"])
        elif "records" in e:
            # a level described as a single table
            self.assertEqual(len(body.files), 1)
            gs = body.files[0].groups
            if not e["records"] and e.get("headers") is None:
                self.assertEqual(len(gs), 0)
            else:
                self.assertEqual(len(gs), 1)
                self.check_group(gs[0], {"name": "", "headers": e.get("headers"), "records": e["records"]})
        else:
            self.check_groups(body.groups, e["groups"])

    def check_files(self, body, files):
        self.assertEqual(len(body.files), len(files))
        for i, f in enumerate(files):
            self.assertEqual(body.files[i].name, (f["name"] or "").encode())
            self.assertEqual(body.files[i].leading, f["name"] is None)
            self.check_groups(body.files[i].groups, f["groups"])

    def check_heading(self, h, e):
        self.assertEqual(h.keys, [p[0].encode() for p in e])
        pairs = h.pairs
        for i, p in enumerate(e):
            v = p[1]
            if scalar(v):
                self.assertEqual(pairs[i][1], scalar_bytes(v))
            elif "level" in v:
                self.check_body(h.level(p[0]), v["level"])


# ---------------------------------------------------------------- building

def build_value(b, f):
    if scalar(f):
        b.field(scalar_bytes(f))
    elif "level" in f:
        with b.level():
            build_body(b, f["level"])
    elif "ref" in f:
        ref = f["ref"]
        if isinstance(ref, str):
            b.ref(ref)
        else:
            b.ref(lambda: build_body(b, ref["level"]))
    elif "map" in f:
        with b.level():
            build_record(b, {"keys": f["map"]})
    else:
        raise ValueError("unknown value form %r" % (f,))


def build_record(b, r):
    b.record()
    if isinstance(r, list):
        for f in r:
            build_value(b, f)
    else:
        for k, v in r["keys"]:
            if scalar(v):
                b.key(k, scalar_bytes(v))
            elif "map" in v:
                with b.key_level(k):
                    build_record(b, {"keys": v["map"]})
            elif "level" in v:
                with b.key_level(k):
                    build_body(b, v["level"])


def build_groups(b, groups, depth=1):
    for i, g in enumerate(groups):
        headers = g["headers"]
        if g["name"] == "" and i == 0 and depth == 1:
            if headers is not None:
                b.header(headers)  # the unnamed leading group: no GS
        elif g["name"] is not None:
            b.group(g["name"], headers, depth)
        for r in g["records"]:
            build_record(b, r)
        if "sections" in g:
            build_groups(b, g["sections"], depth + 1)


def build_body(b, e):
    if "records" in e:
        if e.get("headers") is not None:
            b.header(e["headers"])
        for r in e["records"]:
            build_record(b, r)
    elif "groups" in e:
        build_groups(b, e["groups"])
    elif "files" in e:
        for f in e["files"]:
            if f["name"] is not None:
                b.fs(f["name"])
            build_groups(b, f["groups"])


# ================================================================== tests

class Decode(Checks):
    def test_decode(self):
        for c in cases("decode.json"):
            with self.subTest(c["name"]):
                buf = bytes.fromhex(c["bytes"])
                cont = c0.Container(buf)
                if "documents" in c:
                    docs = c["documents"]
                    self.assertEqual(len(cont.documents), len(docs))
                    for i, d in enumerate(docs):
                        doc = cont.documents[i]
                        if "heading" in d:
                            self.check_heading(doc.heading, d["heading"])
                        self.assertEqual(doc.name, (d["file"] or "").encode())
                        self.check_groups(doc.groups, d["groups"])
                else:
                    doc = cont.first
                    if "heading" in c:
                        self.assertTrue(doc.has_syn)
                        self.check_heading(doc.heading, c["heading"])
                    if "files" in c:
                        self.check_files(doc.body, c["files"])
                    else:
                        self.assertEqual(doc.name, (c["file"] or "").encode())
                        self.check_groups(doc.groups, c["groups"])
                    if "tail" in c:
                        self.assertTrue(cont.has_em)
                        self.assertEqual(bytes(cont.tail).hex(), c["tail"])


class Encode(unittest.TestCase):
    def test_encode(self):
        for c in cases("encode.json"):
            with self.subTest(c["name"]):
                spec = c["build"]

                def emit(b, spec=spec):
                    if spec["file"] is not None:
                        b.file(spec["file"])
                    build_groups(b, spec["groups"])

                canonical = c0.build(emit)
                self.assertEqual(canonical.hex(), c["canonical"])
                self.assertTrue(c0.canonical(canonical))
                if "heading" in spec:
                    whole = c0.build(lambda b: (b.syn(spec["heading"]), emit(b), b.eot()))
                    self.assertEqual(whole.hex(), c["bytes"])


class Canonical(unittest.TestCase):
    def test_canonical(self):
        for c in cases("canonical.json"):
            with self.subTest(c["name"]):
                buf = bytes.fromhex(c["bytes"])
                wellformed = True
                try:
                    c0.validate(buf)
                except c0.C0Error:
                    wellformed = False
                self.assertEqual(wellformed, c["wellformed"])
                self.assertEqual(c0.canonical(buf), c["canonical"])


class Invalid(unittest.TestCase):
    def test_invalid(self):
        for c in cases("invalid.json"):
            with self.subTest(c["name"]):
                with self.assertRaises(c0.C0SyntaxError):
                    c0.validate(bytes.fromhex(c["bytes"]))


class Nested(Checks):
    def test_nested(self):
        for c in cases("nested.json"):
            with self.subTest(c["name"]):
                buf = bytes.fromhex(c["bytes"])
                expected = c["record"]
                rec = c0.Body(buf).record(0)
                self.assertEqual(rec.field_count, len(expected))
                for i, f in enumerate(expected):
                    if scalar(f):
                        self.assertEqual(rec.value(i), scalar_bytes(f))
                    elif "list" in f:
                        items = f["list"]
                        if all(scalar(x) for x in items):
                            self.assertEqual(rec.list(i), [scalar_bytes(x) for x in items])
                        else:
                            inner = rec.level(i).record(0)
                            self.assertEqual(inner.field_count, len(items))
                            for j, x in enumerate(items):
                                if scalar(x):
                                    self.assertEqual(inner.value(j), scalar_bytes(x))
                                else:
                                    self.assertEqual(inner.list(j), [scalar_bytes(y) for y in x["list"]])
                    elif "level" in f:
                        self.check_body(rec.level(i), f["level"])
                        with self.assertRaises(c0.UnitTypeError):
                            rec.list(i)
                if c["canonical"]:
                    def emit(b, expected=expected):
                        b.record()
                        for f in expected:
                            if not scalar(f) and "list" in f:
                                items = f["list"]
                                if all(scalar(x) for x in items):
                                    b.list([scalar_bytes(x) for x in items])
                                else:
                                    with b.level():
                                        b.record()
                                        for x in items:
                                            if scalar(x):
                                                b.field(scalar_bytes(x))
                                            else:
                                                b.list([scalar_bytes(y) for y in x["list"]])
                            else:
                                build_value(b, f)
                    out = c0.build(emit)
                    self.assertEqual(out.hex(), c["bytes"])
                    self.assertTrue(c0.canonical(out))


class Diff(unittest.TestCase):
    def test_diff(self):
        for c in cases("diff.json"):
            with self.subTest(c["name"]):
                diff = bytes.fromhex(c["diff"])
                files = dict(c["files"])
                if c.get("error"):
                    with self.assertRaises(c0.C0Error):
                        c0.diff.apply(diff, files)
                else:
                    self.assertEqual(c0.diff.apply(diff, files), c["result"])


class Stream(unittest.TestCase):
    def test_stream(self):
        for c in cases("stream.json"):
            with self.subTest(c["name"]):
                buf = bytes.fromhex(c["bytes"])
                r = c0.StreamReader(buf, continue_past_damage=True)
                self.assertEqual(r.committed_end, c["committed_end"])
                self.assertEqual(r.torn, c["torn"])
                self.assertEqual(r.block_count, len(c["blocks"]))
                for i, h in enumerate(c["blocks"]):
                    self.assertEqual(bytes(r.block(i)).hex(), h)
                self.assertEqual(r.damaged, c.get("damaged", []))
                if "records" in c:
                    got = r.records
                    self.assertEqual(len(got), len(c["records"]))
                    for i, rr in enumerate(c["records"]):
                        self.assertEqual(got[i].strings, rr)


if __name__ == "__main__":
    unittest.main()
