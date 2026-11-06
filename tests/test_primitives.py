"""Builder primitives: one method per code, mirroring the Crystal Builder specs."""

import unittest

import c0


def build(fn):
    return c0.build(fn)


class Primitives(unittest.TestCase):
    def assertSpells(self, buf, glyphs):
        self.assertEqual(buf, c0.pretty_parse(glyphs))

    def test_refuses_an_empty_note(self):
        with self.assertRaises(c0.BuildError):
            build(lambda b: (b.record("a"), b.note("")))
        with self.assertRaises(c0.BuildError):
            build(lambda b: (b.record("a"), b.bel("")))

    def test_spells_primitives_as_the_bytes_read(self):
        self.assertSpells(build(lambda b: b.fs("inv").gs("users").soh().rs("name").us("amount")),
                          "␜inv␝users␁␞name␟amount")
        self.assertSpells(build(lambda b: b.rs("Alice").us("1502.30")), "␞Alice␟1502.30")
        self.assertSpells(build(lambda b: b.rs().soh("host").us("localhost").soh("port").us("5432")),
                          "␞␁host␟localhost␁port␟5432")
        self.assertSpells(build(lambda b: b.rs("cfg").us().dle().rs().soh("b").us("2").can()),
                          "␞cfg␟␐␞␁b␟2␘")
        self.assertSpells(build(lambda b: b.bel("checked").rs("x")), "␇checked␞x")
        self.assertSpells(build(lambda b: b.bel().gs("old").rs("1").gs("new").rs("2")),
                          "␇␝old␞1␝new␞2")
        self.assertSpells(build(lambda b: b.gs().gs("sub").rs().enq("users").us().enq("users", "1")),
                          "␝␝sub␞␅users␟␅␂users␟1␃")
        self.assertSpells(build(lambda b: b.syn().soh("version").us("1").rs("a").eot()),
                          "␖␁version␟1␞a␄")
        self.assertSpells(build(lambda b: b.rs("a").etb("c1").rs("b").etb()), "␞a␗c1␞b␗")
        self.assertEqual(build(lambda b: b.rs("a").em(b"\xff\x00tail")), b"\x1ea\x19\xff\x00tail")

    def test_values_keep_the_one_spelling(self):
        self.assertEqual(build(lambda b: b.rs(" x ").us("a\x1fb").bel("n\x02")),
                         b"\x1e\x02 x \x03\x1f\x02a\x1fb\x03\x07\x02n\x10\x02\x03")
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.gs(" bad"))
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.soh("a\x1fb"))
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.fs("a\x1db"))
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.etb(" x"))

    def test_mixes_primitives_with_composed_methods(self):
        buf = build(lambda b: (
            b.rs("a"),
            b.field("b"),
            b.record(),
            b.us("c"),
            b.gs("g"),
            b.key("k", "v"),
            b.group("h", ["h"]),
            b.field("x"),
        ))
        self.assertSpells(buf, "␞a␟b␞␟c␝g␞␁k␟v␝h␁␞h␞x")
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.can())
        with self.assertRaises(c0.BuildError):
            build(lambda b: b.rs().enq("a", "b", "c", "d"))
        with self.assertRaises(TypeError):
            build(lambda b: b.rs().enq())

    def test_primitives_update_the_record_state(self):
        # us() after dle()/can() resumes the outer record's unit count.
        self.assertSpells(build(lambda b: (b.rs("a"), b.dle(), b.rs("i"), b.can(), b.field("z"))),
                          "␞a␐␞i␘␟z")
        self.assertSpells(build(lambda b: (b.rs(), b.level(lambda l: l.rs("i")), b.field("z"))),
                          "␞␐␞i␘␟z")
        self.assertSpells(build(lambda b: (b.rs("a").us().dle(), b.record("i"), b.can(), b.list(["p", "q"]))),
                          "␞a␟␐␞i␘␟␐␞p␟q␘")
        # a bare primitive after a record-ending code starts fresh
        self.assertSpells(build(lambda b: (b.record("a", "b"), b.gs("g"), b.field("c"))), "␞a␟b␝g␞c")
        self.assertSpells(build(lambda b: (b.record("a"), b.syn(), b.key("k", "v"), b.field("c"))),
                          "␞a␖␁k␟v␞c")

    def test_writes_the_same_bytes_through_either_layer(self):
        hi = build(lambda b: (
            b.syn(shape="tabular"),
            b.group("users", ["name", "amount"]),
            b.record("Alice", "1502.30"),
            b.record("Bob", "3 40"),
            b.group("cfg"),
            b.map(host="localhost", port="5432"),
            b.record("r"),
            b.list(["x", "y"]),
            b.ref("users", "1"),
            b.level(lambda l: l.record("i")),
            b.eot(),
        ))
        lo = build(lambda b: (
            b.syn().soh("shape").us("tabular"),
            b.gs("users").soh().rs("name").us("amount").rs("Alice").us("1502.30").rs("Bob").us("3 40"),
            b.gs("cfg").rs().soh("host").us("localhost").soh("port").us("5432"),
            b.rs("r").us().dle().rs("x").us("y").can().us().enq("users", "1").us().dle().rs("i").can(),
            b.eot(),
        ))
        self.assertEqual(lo, hi)
        self.assertTrue(c0.canonical(c0.Document(lo).body.raw))

    def test_everything_returns_the_builder(self):
        b = c0.Builder()
        for r in (b.syn(), b.soh("k"), b.us("v"), b.fs("f"), b.gs(), b.gs("g"), b.rs(), b.us(),
                  b.rs("a"), b.dle(), b.can(), b.bel(), b.rs("x"), b.bel("n"), b.rs("y"), b.us(),
                  b.enq("g"), b.eot(), b.etb(), b.etb("p"), b.em(b"")):
            self.assertIs(r, b)
        b.close()


if __name__ == "__main__":
    unittest.main()
