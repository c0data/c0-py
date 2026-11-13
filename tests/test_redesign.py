"""The 1.0 redesign at c0-spec 7b13feb, mirroring c0-cr's spec/redesign_spec.cr:
supersession, the result model, reference patterns, damaged stream blocks,
parse-is-validate, and C0DIFF."""

import os
import tempfile
import unittest

import c0

P = c0.pretty_parse


class Supersession(unittest.TestCase):
    def test_drops_the_superseded_element_from_the_model_and_keeps_it_in_the_bytes(self):
        doc = c0.Document(P("␝g␞a␟b␚␟c␚␞x␟y"))
        g = doc.group("g")
        self.assertEqual(g.record_count, 1)
        self.assertEqual(g.record(0).values, [b"x", b"y"])
        self.assertEqual(doc.supersession_grains, [c0.US, c0.RS])
        rec = c0.Body(P("␞a␚␟b␟c")).record(0)
        self.assertEqual(rec.values, [b"b", b"c"])
        self.assertEqual(rec.field_count, 2)
        self.assertEqual([(u.value, u.superseded) for u in rec.units], [(b"a", True), (b"b", False), (b"c", False)])
        self.assertIsInstance(rec.units[0], c0.Unit)
        self.assertIsInstance(rec.units[0].raw, memoryview)
        # supersession is content: canonical form keeps it, pretty round-trips it
        self.assertTrue(c0.canonical(P("␞a␚␟b")))
        buf = P("␝g␞a␚␟b␚␞x␝h␚␝i␖␁k␟1␚␁k␟2␞z")
        self.assertEqual(c0.pretty_parse(c0.pretty_format(P("␝g␞a␚␟b␚␞x␝h␚␝i"))), P("␝g␞a␚␟b␚␞x␝h␚␝i"))
        self.assertEqual(c0.pretty_parse(c0.pretty_format(P("␖␁k␟1␚␁k␟2␞z"))), P("␖␁k␟1␚␁k␟2␞z"))
        del buf

    def test_chaining_keeps_the_last_and_empty_sides_are_empty_units(self):
        self.assertEqual(c0.Body(P("␞a␚␟b␚␟c")).record(0).values, [b"c"])
        self.assertEqual(c0.Body(P("␞a␚␟")).record(0).values, [b""])
        self.assertEqual(c0.Body(P("␞␚␟b")).record(0).values, [b"b"])
        rec = c0.Body(P("␞␁k␟1␚␁k␟2␁j␟3")).record(0)
        self.assertEqual(rec.pairs, [(b"k", b"2"), (b"j", b"3")])
        body = c0.Body(P("␜a␞1␚␜b␞2"))
        self.assertEqual([f.name for f in body.files], [b"b"])
        self.assertEqual(c0.Document(P("␖␁shape␟diff␚␁shape␟stream␞a")).shape, "stream")

    def test_nothing_to_supersede_and_sub_elsewhere_are_errors(self):
        for bad in ("␝g␚␞x", "␚␝g", "␚␜f", "␞a␚", "␞a␚␇n␟b", "␞a␚␗", "␞a␚␄", "␞a␟␐␞b␘␚␘", "␁␞a␚␁␞b",
                    "␞a␚␚␟b", "␝g␇␞a␚␞x", "␞␅␐␝a␚␝b␘"):
            with self.subTest(bad):
                with self.assertRaises(c0.C0SyntaxError):
                    c0.Body(P(bad))
                with self.assertRaises(c0.C0SyntaxError):
                    c0.validate(P(bad))

    def test_parse_is_validate_and_canonical_is_opt_in(self):
        with self.assertRaises(c0.C0SyntaxError):
            c0.Body(b"\x1ea\x0eb")
        with self.assertRaises(c0.C0SyntaxError) as cm:
            c0.Body(b"\x1ea\x1e\x02open")
        self.assertEqual(cm.exception.position, 3)
        with self.assertRaises(c0.C0SyntaxError):
            c0.Document(b"\x16\x01k\x1fv\x1ea\x1f\x10\x1eb")
        with self.assertRaises(c0.C0SyntaxError):
            c0.Container(b"\x1ea\x04\x1eb")
        # a sub-range is checked on its own
        with self.assertRaises(c0.C0SyntaxError) as cm:
            c0.Body(b"\x1eok\x1ea\x0e", 3)
        self.assertEqual(cm.exception.position, 5)
        # non-canonical input parses; canonical() reports it
        self.assertEqual(c0.Body(b"\x1e \x02a\x03 ").record(0).values, [b"a"])
        self.assertFalse(c0.canonical(b"\x1e \x02a\x03 "))


class References(unittest.TestCase):
    def test_is_a_group_name_or_a_pattern_never_text(self):
        with self.assertRaises(c0.C0SyntaxError):
            c0.Body(P("␞␅␂a␟b␃"))
        with self.assertRaises(c0.C0SyntaxError):
            c0.Body(P("␞␅␐␝a␞␂ b␃␘"))
        with self.assertRaises(c0.C0SyntaxError):
            c0.Body(P("␖␁k␟␅tags␞a"))  # a reference in a heading
        r = c0.Body(P("␞␅␐␝a␞b c␟d␘")).record(0).ref(0)
        self.assertIsNone(r.name)
        self.assertEqual(r.path, [b"a", b"b c", b"d"])
        self.assertFalse(r.is_group)
        self.assertEqual(c0.Body(P("␞␅␐␝tags␘")).record(0).ref(0).group_name, b"tags")
        self.assertTrue(c0.Body(P("␞␅tags")).record(0).ref(0).is_group)
        # a level in the third unit continues into a field's level
        r = c0.Body(P("␞␅␐␝a␞001␟children␟␐␞2␘␘")).record(0).ref(0)
        self.assertEqual(r.path, [b"a", b"001", b"children", b"2"])
        self.assertEqual(r, c0.Body(P("␞␅␐␝a␞001␟children␟␐␞2␘␘")).record(0).ref(0))

    def test_builder_writes_patterns(self):
        self.assertEqual(c0.build(lambda b: (b.record("x"), b.ref("tags", "001"))), P("␞x␟␅␐␝tags␞001␘"))
        self.assertEqual(c0.build(lambda b: (b.record("x"), b.ref("tags", "001", "label"))),
                         P("␞x␟␅␐␝tags␞001␟label␘"))
        self.assertEqual(c0.build(lambda b: (b.record("x"), b.ref(lambda: b.gs("s").rs().soh("host").us()))),
                         P("␞x␟␅␐␝s␞␁host␟␘"))
        with self.assertRaises(TypeError):
            c0.Builder().ref("g", field="f")


class Stream(unittest.TestCase):
    def test_a_damaged_block_contributes_nothing_to_the_parse_state(self):
        # a header row in the block after a damaged first block counts as first
        r = c0.StreamReader(P("␞a␇␗␁␞h␗␞1␗"), continue_past_damage=True)
        self.assertEqual(r.damaged, [0])
        self.assertEqual(r.body.group(0).headers, [b"h"])
        self.assertEqual([x.strings for x in r.records], [["1"]])
        self.assertEqual(r.block_end(0), 4)
        self.assertEqual(r.undamaged, P("␁␞h␞1"))
        self.assertEqual(r.before_damage, b"")
        with self.assertRaises(c0.DamagedBlockError):
            c0.StreamReader(P("␞a␇␗␁␞h␗␞1␗")).document
        self.assertIsNone(c0.StreamReader(P("␞a␗")).resume_offset)
        self.assertFalse(c0.StreamReader(P("␞a␗")).has_damaged)


class Diff(unittest.TestCase):
    GREETING = "␖␁shape␟diff␜greeting.txt␞␂Hello ␃␟world␚␟universe␟!␄"

    def test_builds_the_readme_diff(self):
        d = c0.DiffBuilder()
        d.file("greeting.txt")
        d.record(lambda r: (r.anchor("Hello "), r.sub("world", "universe"), r.anchor("!")))
        d.eot()
        self.assertEqual(d.bytes, P(self.GREETING))
        self.assertEqual(c0.diff.build(lambda d: (
            d.file("greeting.txt"), d.replace("Hello ", "world", "universe", "!"), d.eot())), P(self.GREETING))
        with c0.DiffBuilder(heading=False) as d:
            d.file("tally.rb")
            with d.record() as r:
                r.anchor("def tally").sub("(items)", "(list)")
            d.insert_after("items)", "\n  # counts").delete("  # TODO\n").assert_("end").note("a note")
            self.assertEqual(d.bytes, P("␜tally.rb␞def tally␟(items)␚␟(list)␞items)␟␚␟␂\n  # counts␃"
                                        "␞␂  # TODO\n␃␚␟␞end␇a note"))

    def test_parses_and_applies(self):
        edits = c0.parse_diff(P(self.GREETING))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].path, b"greeting.txt")
        pat = edits[0].records[0]
        self.assertEqual([(u.old, u.new) for u in pat], [(b"Hello ", None), (b"world", b"universe"), (b"!", None)])
        self.assertEqual((pat.search, pat.replacement, pat.is_assertion), (b"Hello world!", b"Hello universe!", False))
        self.assertEqual(c0.apply_diff(P(self.GREETING), {"greeting.txt": "Hello world!", "other": "x"}),
                         {"greeting.txt": "Hello universe!", "other": "x"})
        self.assertEqual(c0.diff.apply(P(self.GREETING), {b"greeting.txt": b"Hello world!"}),
                         {b"greeting.txt": b"Hello universe!"})
        # each record matches once from the cursor to the end of the file
        diff = c0.diff.build(lambda d: (d.file("f"), d.replace("", "a1", "b"), d.replace("", "a2", "c"), d.eot()))
        self.assertEqual(c0.diff.apply(diff, {"f": "a1 a2"}), {"f": "b c"})
        with self.assertRaises(c0.DiffError):
            c0.diff.apply(diff, {"f": "a2 a1"})  # a2 is not found after a1
        with self.assertRaises(c0.DiffError):
            c0.diff.apply(c0.diff.build(lambda d: (d.file("f"), d.replace("", "a", "b"))), {"f": "a a"})

    def test_refuses_what_a_diff_may_not_hold(self):
        files = {"f": "aaa"}
        for name, bad in [
            ("wrong shape", "␖␁shape␟stream␜f␞a␚␟b"),
            ("duplicate path", "␜f␜f"),
            ("empty search", "␜f␞"),
            ("bare insertion", "␜f␞␚␟b"),
            ("nothing by nothing", "␜f␞a␟␚␟"),
            ("keyed unit", "␜f␞␁k␟v"),
            ("GS", "␜f␝s␞a␚␟b"),
            ("header row", "␜f␁␞x␞a␚␟b"),
            ("level", "␜f␞␐␞a␘␚␟b"),
            ("reference", "␜f␞␅a␚␟b"),
            ("chained", "␜f␞a␚␟b␚␟c"),
            ("record grain", "␜f␞a␚␞b"),
            ("dead record", "␜f␇␞a␚␟b"),
            ("dead file", "␜f␞a␚␟b␇␜g"),
            ("record before FS", "␞a␚␟b␜f"),
            ("empty path", "␜␞a␚␟b"),
            ("two documents", "␖␜f␞a␚␟b␄␖␞x␄"),
            ("no such file", "␜nope␞x"),
            ("not found", "␜f␞zzz␚␟b"),
            ("ambiguous", "␜f␞a␚␟b"),
        ]:
            with self.subTest(name):
                with self.assertRaises(c0.DiffError):
                    c0.diff.apply(P(bad), files)
        with self.assertRaises(c0.C0SyntaxError):
            c0.diff.apply(P("␜f␞a␚b"), files)
        # a hunk kept without applying: a text note
        self.assertEqual(c0.diff.apply(P("␜f␇␂␞a␚␟b␃␞a␚␟x"), {"f": "a"}), {"f": "x"})
        # a diff with no files is a no-op
        self.assertEqual(c0.diff.apply(P("␖␁shape␟diff␄"), files), files)

    def test_apply_files_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.txt"), "w") as f:
                f.write("Hello world!")
            diff = c0.diff.build(lambda d: (d.file("a.txt"), d.replace("Hello ", "world", "universe", "!"),
                                            d.file("b.txt"), d.assert_("x"), d.eot()))
            with self.assertRaises(c0.DiffError):
                c0.diff.apply_files(diff, tmp)
            with open(os.path.join(tmp, "a.txt")) as f:
                self.assertEqual(f.read(), "Hello world!")
            with open(os.path.join(tmp, "b.txt"), "w") as f:
                f.write("x")
            c0.diff.apply_files(diff, tmp)
            with open(os.path.join(tmp, "a.txt")) as f:
                self.assertEqual(f.read(), "Hello universe!")
            with self.assertRaises(c0.DiffError):
                c0.diff.apply_files(c0.diff.build(lambda d: (d.file("../x"), d.assert_("x"))), tmp)


if __name__ == "__main__":
    unittest.main()
