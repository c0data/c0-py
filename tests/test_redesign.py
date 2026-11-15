"""The 1.0 redesign at c0-spec f677533, mirroring c0-cr's spec/redesign_spec.cr:
subordinates (SUB), the result model, reference patterns, damaged stream
blocks, parse-is-validate, and C0DIFF."""

import os
import tempfile
import unittest

import c0

P = c0.pretty_parse


class Subordinates(unittest.TestCase):
    def test_reads_sub_gs_as_a_subsection_and_sub_us_as_a_sub_unit(self):
        doc = c0.Document(P("␝g␞a␟b␚␟c␚␝s␞x"))
        self.assertEqual([(g.name, g.depth) for g in doc.groups], [(b"g", 1), (b"s", 2)])
        rec = doc.group("g").record(0)
        self.assertEqual(rec.values, [b"a", b"b"])
        self.assertEqual(rec.field_count, 2)
        self.assertEqual([u.value for u in rec.unit(1).subs], [b"c"])
        self.assertEqual(rec.unit(0).subs, [])
        self.assertEqual([(u.value, [s.value for s in u.subs]) for u in rec.units], [(b"a", []), (b"b", [b"c"])])
        self.assertIsInstance(rec.units[1], c0.Unit)
        self.assertIsInstance(rec.units[1].raw, memoryview)
        self.assertIsInstance(rec.unit(1).subs[0], c0.Unit)
        self.assertEqual(bytes(rec.unit(1).raw), b"b")
        self.assertEqual(len(rec.unit(1)), 1)
        self.assertEqual([s.string() for s in rec.unit(1)], ["c"])
        with self.assertRaises(IndexError):
            rec.unit(2)
        # nested: the SUB run is the depth; several sub-units on one unit
        rec = c0.Body(P("␞a␚␟b␚␚␟c␟d")).record(0)
        self.assertEqual(rec.values, [b"a", b"d"])
        self.assertEqual([u.value for u in rec.unit(0).subs[0].subs], [b"c"])
        self.assertEqual([u.value for u in c0.Body(P("␞a␚␟b␚␟c")).record(0).unit(0).subs], [b"b", b"c"])
        # the first unit exists even when empty; a sub-unit may be empty or hold a level
        self.assertEqual(c0.Body(P("␞␚␟new")).record(0).values, [b""])
        self.assertEqual([u.value for u in c0.Body(P("␞␚␟new")).record(0).unit(0).subs], [b"new"])
        self.assertEqual([u.value for u in c0.Body(P("␞old␚␟")).record(0).unit(0).subs], [b""])
        sub = c0.Body(P("␞a␚␟␐␞x␘")).record(0).unit(0).subs[0]
        self.assertEqual(sub.unit_type, c0.UnitType.LEVEL)
        self.assertEqual(sub.level.record(0).values, [b"x"])
        # a keyed unit's value takes sub-units; a dead keyed unit takes its sub-units with it
        rec = c0.Body(P("␞␁k␟v␚␟w")).record(0)
        self.assertEqual(rec.pairs, [(b"k", b"v")])
        self.assertEqual([u.value for u in rec.unit("k").subs], [b"w"])
        self.assertEqual([u.value for u in rec.unit(b"k").subs], [b"w"])
        with self.assertRaises(c0.NotFoundError):
            rec.unit("nope")
        self.assertEqual(c0.Body(P("␞␇␁k␟v␚␟w␁z␟1")).record(0).keys, [b"z"])
        # a column name under a header row
        g = c0.Body(P("␁␞n␟m␞1␟2␚␟3")).group(0)
        self.assertEqual([u.value for u in g.record(0).unit("m").subs], [b"3"])
        # a SUB prefix is structure: canonical form keeps it; pretty round-trips it
        self.assertTrue(c0.canonical(P("␞a␚␟b")))
        self.assertTrue(c0.canonical(P("␝a␚␝b␞x")))
        self.assertTrue(c0.canonical(P("␝␝b␞x")))
        self.assertFalse(c0.canonical(b"\x1ea\x1a \x1fb"))  # layout after SUB, as after every code
        self.assertEqual(c0.Body(b"\x1ea\x1a \x1fb").record(0).unit(0).subs[0].value, b"b")
        for s in ("␝g␞a␚␟b␚␚␟c␞x␚␝h␚␚␝i␞␁k␟1␚␟2", "␖␁k␟1␞z␚␟y␄"):
            self.assertEqual(c0.pretty_parse(c0.pretty_format(P(s))), P(s))
        # GS repetition is an empty-named group, not depth
        self.assertEqual([(g.name, g.depth) for g in c0.Body(P("␝␝b␞x")).groups], [(b"", 1), (b"b", 1)])
        self.assertEqual([(g.name, g.depth) for g in c0.Body(P("␝a␚␝b␚␚␝c␝d")).groups],
                         [(b"a", 1), (b"b", 2), (b"c", 3), (b"d", 1)])
        # the builder writes the prefix
        self.assertEqual(c0.build(lambda b: b.gs("a").gs("s", depth=2).rs("x").sub_unit("y")), P("␝a␚␝s␞x␚␟y"))
        self.assertEqual(c0.build(lambda b: (b.group("a"), b.group("s", ["h"], depth=2), b.section("t", depth=3))),
                         P("␝a␚␝s␁␞h␚␚␝t"))
        self.assertEqual(c0.build(lambda b: (b.record("a"), b.sub_unit("b"), b.sub_unit("c", depth=2), b.field("d"))),
                         P("␞a␚␟b␚␚␟c␟d"))
        self.assertEqual(c0.build(lambda b: b.sub_unit("new")), P("␞␚␟new"))
        self.assertEqual(c0.build(lambda b: (b.record("old"), b.sub_unit())), P("␞old␚␟"))
        self.assertEqual(c0.build(lambda b: (b.record("a", "b"), b.sub_unit("c"), b.field("d"))), P("␞a␟b␚␟c␟d"))
        with self.assertRaises(ValueError):
            c0.Builder().sub_unit("x", depth=0)
        with self.assertRaises(ValueError):
            c0.Builder().gs("x", depth=0)
        self.assertFalse(hasattr(c0.Builder(), "supersede"))
        self.assertFalse(hasattr(c0.Document(P("␞a")), "supersession_grains"))

    def test_reserved_grains_skips_and_dead_units_are_errors(self):
        for bad in ("␝g␚␞x", "␚␝g", "␚␜f", "␞a␚", "␞a␚␇n␟b", "␞a␚␗", "␞a␚␄", "␞a␟␐␞b␘␚␘", "␁␞a␚␁␞b",
                    "␞a␚␚␟b", "␝a␚␚␝c", "␜f␚␝g", "␝a␝b␚␚␝c", "␞a␚␞b", "␜a␚␜b", "␞␁k␟v␚␁j␟w", "␚␁␞h",
                    "␇␚␟c", "␞a␇␚␟c", "␚␇␝b", "␖␁schema␟a␚␟b␞x", "␞a␚␟b␚␚␚␟c",
                    "␞a␟␅␐␝g␞3␚␟x␘", "␞a␟␅␐␝g␞␁k␟␚␟x␘", "␞␅␐␝a␚␝b␘␚"):
            with self.subTest(bad):
                with self.assertRaises(c0.C0SyntaxError):
                    c0.Body(P(bad))
                with self.assertRaises(c0.C0SyntaxError):
                    c0.validate(P(bad))
        # not errors: a dead group line keeps its prefix; a subsection under a dead group dies with it
        self.assertEqual([g.name for g in c0.Body(P("␝a␞1␇␚␝s␞2␝b␞3")).groups], [b"a", b"b"])
        self.assertEqual([g.name for g in c0.Body(P("␝x␇␚␝draft␚␚␝notes␝y")).groups], [b"x", b"y"])
        self.assertEqual(c0.Document(P("␝x␇␚␝draft␚␚␝notes␝y")).dead_elements, 1)
        # a pattern spells depth the same way
        r = c0.Body(P("␞␅␐␝docs␚␝intro␞3␘")).record(0).ref(0)
        self.assertEqual(r.path, [b"docs", b"intro", b"3"])
        self.assertEqual([(g.name, g.depth) for g in r.pattern.groups], [(b"docs", 1), (b"intro", 2)])

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
            ("two sub-units", "␜f␞a␚␟b␚␟c"),
            ("nested sub-unit", "␜f␞a␚␟b␚␚␟c"),
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
        with self.assertRaises(c0.C0SyntaxError):
            c0.diff.apply(P("␜f␞a␚␞b"), files)  # SUB before RS is a reserved grain
        with self.assertRaises(c0.C0SyntaxError):
            c0.diff.apply(P("␜f␞a␚␟b␚␜g"), files)  # SUB before FS too
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
