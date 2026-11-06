import gc
import resource
import unittest

import c0


class Basic(unittest.TestCase):
    def test_build_read_roundtrip(self):
        buf = c0.build(lambda b: (
            b.group("users", ["name", "amount"]),
            b.record("Alice", "1502.30"),
            b.record("Bob", "340.00"),
        ))
        self.assertEqual(buf, b"\x1dusers\x01\x1ename\x1famount\x1eAlice\x1f1502.30\x1eBob\x1f340.00")
        g = c0.Body(buf).group("users")
        self.assertEqual(g.name, b"users")
        self.assertEqual(g.headers, [b"name", b"amount"])
        self.assertEqual(g.record_count, 2)
        self.assertEqual(g.record(0).value(0), b"Alice")
        self.assertEqual(g.record(1).string(1), "340.00")
        self.assertEqual(g.column("amount"), 1)
        self.assertEqual([r.strings for r in g], [["Alice", "1502.30"], ["Bob", "340.00"]])
        self.assertTrue(c0.canonical(buf))
        c0.validate(buf)

    def test_zero_copy_view(self):
        buf = c0.build(lambda b: (b.group("g"), b.record("hello")))
        field = c0.Body(buf).group(0).record(0).field(0)
        self.assertIsInstance(field, memoryview)
        self.assertEqual(bytes(field), b"hello")

    def test_document_and_files(self):
        buf = c0.build(lambda b: (
            b.file("mydb"),
            b.group("users", ["name"]),
            b.record("Alice"),
            b.group("products", ["id"]),
            b.record("01"),
        ))
        doc = c0.Document(buf)
        self.assertEqual(doc.name, b"mydb")
        self.assertEqual(doc.group_count, 2)
        self.assertEqual(doc.group_names, [b"users", b"products"])
        self.assertEqual(doc.group("products").record(0).value(0), b"01")
        self.assertEqual(doc["products"][0].string(0), "01")
        with self.assertRaises(c0.NotFoundError):
            doc.group("missing")
        self.assertIn("no SYN at the start of the document", doc.warnings)
        self.assertIn("no EOT at the end of the document", doc.warnings)

    def test_text_values_and_one_spelling(self):
        buf = c0.build(lambda b: (b.record("a\x1fb", " padded ", "x\x02y\x17z", "plain")))
        self.assertEqual(buf, b"\x1e\x02a\x1fb\x03\x1f\x02 padded \x03\x1f\x02x\x10\x02y\x10\x17z\x03\x1fplain")
        rec = c0.Body(buf).record(0)
        self.assertEqual(rec.field_count, 4)
        self.assertEqual(rec.values, [b"a\x1fb", b" padded ", b"x\x02y\x17z", b"plain"])
        self.assertEqual(rec.unit_type(0), c0.UnitType.TEXT)
        self.assertEqual(rec.unit_type(3), c0.UnitType.PLAIN)
        self.assertTrue(c0.canonical(buf))
        self.assertFalse(c0.canonical(b"\x1e\x02only\x03"))  # gratuitous text

    def test_layout_is_not_data(self):
        rec = c0.Body(b"\x1e Alice \x1f Bob\n").record(0)
        self.assertEqual(rec.values, [b"Alice", b"Bob"])
        with self.assertRaises(c0.C0SyntaxError):
            c0.validate(b"\x1ea\tb")  # HT inside a plain value

    def test_trailing_empty_field(self):
        self.assertEqual(c0.Body(b"\x1eAlice\x1f").record(0).field_count, 2)
        self.assertEqual(c0.Body(b"\x1eAlice").record(0).field_count, 1)
        self.assertEqual(c0.Body(b"\x1e").record(0).values, [b""])

    def test_keyed_records(self):
        buf = c0.build(lambda b: (b.group("database"), b.map({"host": "localhost", "port": "5432"})))
        self.assertEqual(buf, b"\x1ddatabase\x1e\x01host\x1flocalhost\x01port\x1f5432")
        rec = c0.Body(buf).group(0).record(0)
        self.assertTrue(rec.keyed)
        self.assertEqual(rec.keys, [b"host", b"port"])
        self.assertEqual(rec.pairs, [(b"host", b"localhost"), (b"port", b"5432")])
        self.assertEqual(rec["port"], b"5432")
        self.assertEqual(rec.string("host"), "localhost")
        self.assertIn("host", rec)
        self.assertIsNone(rec.get("nope"))
        self.assertEqual(list(rec), [b"host", b"port"])
        # duplicate keys: last wins, non-canonical
        dup = b"\x1e\x01k\x1f1\x01k\x1f2"
        self.assertEqual(c0.Body(dup).record(0).get("k"), b"2")
        self.assertFalse(c0.canonical(dup))
        # mixed records are malformed
        with self.assertRaises(c0.C0SyntaxError):
            c0.validate(b"\x1e\x01a\x1f1\x1f2")

    def test_nested_levels_and_lists(self):
        buf = c0.build(lambda b: (
            b.group("users", ["name", "roles", "address"]),
            b.record("Alice"),
            b.list(["Admin", "Editor"]),
            b.level(lambda l: (l.header("street", "city"), l.record("1 Main", "Springfield"))),
        ))
        self.assertEqual(
            buf,
            b"\x1dusers\x01\x1ename\x1froles\x1faddress\x1eAlice"
            b"\x1f\x10\x1eAdmin\x1fEditor\x18"
            b"\x1f\x10\x01\x1estreet\x1fcity\x1e1 Main\x1fSpringfield\x18")
        rec = c0.Body(buf).group(0).record(0)
        self.assertEqual(rec.field_count, 3)
        self.assertEqual(rec.list(1), [b"Admin", b"Editor"])
        self.assertEqual(rec.unit_type(2), c0.UnitType.LEVEL)
        addr = rec.level(2)
        self.assertTrue(addr.bare)
        self.assertEqual(addr.group(0).headers, [b"street", b"city"])
        self.assertEqual(addr.record(0).string(1), "Springfield")
        with self.assertRaises(c0.UnitTypeError):
            rec.value(2)
        with self.assertRaises(c0.UnitTypeError):
            rec.list(0)
        # context-manager form and the empty level
        with c0.Builder() as b:
            b.record("a")
            with b.level():
                b.record("x", "y")
            b.list([])
            self.assertEqual(b.bytes, b"\x1ea\x1f\x10\x1ex\x1fy\x18\x1f\x10\x18")
            self.assertEqual(c0.Body(b.bytes).record(0).list(2), [])

    def test_references(self):
        buf = c0.build(lambda b: (b.record("My Post"), b.ref("tags"), b.ref("tags", "001", "label")))
        self.assertEqual(buf, b"\x1eMy Post\x1f\x05tags\x1f\x05\x02tags\x1f001\x1flabel\x03")
        rec = c0.Body(buf).record(0)
        self.assertEqual(rec.ref(1).path, [b"tags"])
        self.assertTrue(rec.ref(1).is_group)
        r = rec.ref(2)
        self.assertEqual((r.group, r.record, r.field), (b"tags", b"001", b"label"))
        with self.assertRaises(c0.UnitTypeError):
            rec.ref(0)
        with self.assertRaises(c0.BuildError):
            c0.Builder().ref("bad\x1fname")
        with self.assertRaises(c0.BuildError):
            c0.Builder().ref("a", "", "c")

    def test_heading_container_and_blob(self):
        raw = b"lamp-bytes"
        buf = c0.build(lambda b: (
            b.syn(shape="tabular", version="1"),
            b.key_level("blob", lambda l: (l.header("name", "offset", "length"),
                                           l.record("lamp", "0", str(len(raw))))),
            b.group("items", ["name"]),
            b.record("Lamp"),
            b.eot(),
            b.em(raw),
        ))
        cont = c0.Container(buf)
        self.assertEqual(len(cont), 1)
        doc = cont.first
        self.assertTrue(doc.has_syn and doc.has_eot)
        self.assertEqual(doc.warnings, [])
        self.assertEqual(doc.heading.keys, [b"shape", b"version", b"blob"])
        self.assertEqual(doc.shape, "tabular")
        self.assertEqual(doc.heading.version, 1)
        self.assertEqual(doc.heading.blob.group(0).headers, [b"name", b"offset", b"length"])
        self.assertTrue(cont.has_em)
        self.assertEqual(bytes(cont.tail), raw)
        self.assertEqual(cont.blob("lamp"), raw)
        self.assertIsNone(cont.blob("desk"))
        self.assertEqual(cont.warnings, [])
        # EM with no blob table is a loud warning
        self.assertIn("EM with no blob table: the region is unaddressable",
                      c0.Container(b"\x16\x1ea\x04\x19xyz").warnings)
        # unknown key / shape warnings
        d = c0.Document(b"\x16\x01shape\x1fodd\x01color\x1fblue\x1ea\x04")
        self.assertIn("unknown heading key 'color'", d.warnings)
        self.assertIn("unknown shape 'odd'", d.warnings)
        # two documents, the second without SYN, is an error
        with self.assertRaises(c0.C0SyntaxError):
            c0.Container(b"\x16\x1ea\x04\x1eb")

    def test_comments(self):
        buf = b"\x07note\x1dg\x07\x1edead\x1eAlice\x1f\x07old\x1f100"
        g = c0.Body(buf).group(0)
        self.assertEqual(g.record_count, 1)
        self.assertEqual(g.record(0).values, [b"Alice", b"", b"100"])
        self.assertFalse(c0.canonical(buf))
        self.assertEqual(c0.Builder().note("see the runbook").bytes, b"\x07see the runbook")
        with self.assertRaises(c0.BuildError):
            c0.Builder().note("")

    def test_invalid_input_raises(self):
        with self.assertRaises(c0.UnassignedCodeError):
            c0.validate(b"\x1ea\x0eb")
        with self.assertRaises(c0.UnexpectedEndError):
            c0.validate(b"\x1e\x02open")
        try:
            c0.validate(b"\x1ea\x0eb")
        except c0.C0SyntaxError as e:
            self.assertEqual(e.position, 2)
            self.assertIsInstance(e, c0.C0Error)

    def test_names_reject_control_bytes_and_edge_space(self):
        with self.assertRaises(c0.BuildError):
            c0.Builder().group("bad\x1fname")
        with self.assertRaises(c0.BuildError):
            c0.Builder().file(" padded")
        with self.assertRaises(c0.BuildError):
            c0.Builder().etb("bad\x1fpayload")
        with self.assertRaises(c0.BuildError):
            with c0.Builder() as bad:
                with bad.level():
                    pass
                bad._b.level_close()  # CAN with no open level
        self.assertTrue(c0.is_name("ok name"))
        self.assertFalse(c0.is_plain(" x"))

    def test_stream(self):
        buf = c0.build(lambda b: (
            b.syn(shape="stream"), b.etb(),
            b.header("op", "id"), b.etb(),
            b.record("create", "a1b2"), b.etb("sha256:00"),
            b.record("name", "dra"),
        ))
        r = c0.StreamReader(buf)
        self.assertTrue(r.torn)
        self.assertEqual(r.block_count, 3)
        self.assertEqual(r.damaged, [])
        self.assertEqual(bytes(r.block(2)), b"\x1ecreate\x1fa1b2")
        self.assertEqual(bytes(r.tail), b"\x1ename\x1fdra")
        self.assertEqual([x.strings for x in r], [["create", "a1b2"]])
        self.assertEqual(r.document.shape, "stream")
        self.assertEqual(r.body.group(0).headers, [b"op", b"id"])
        # a raw ETB inside open text commits a damaged block
        d = c0.StreamReader(b"\x1e\x02a\x17\x1eb\x17")
        self.assertEqual(d.damaged, [0])
        self.assertTrue(d.is_damaged(0))
        self.assertEqual([x.strings for x in d.records], [["b"]])

    def test_builder_parity_bytes(self):
        b = c0.Builder()
        b.section("Intro").section("Deep", depth=2).block("text\x1e").item("one")
        self.assertEqual(b.bytes, b"\x1dIntro\x1d\x1dDeep\x1e\x02text\x1e\x03\x1fone")
        self.assertEqual(bytes(c0.Builder().etb()), b"\x17")
        self.assertEqual(c0.Builder().etb("a1b2").bytes, b"\x17a1b2")
        self.assertEqual(c0.Builder().syn([("shape", "diff")]).bytes, b"\x16\x01shape\x1fdiff")
        self.assertEqual(c0.Builder().record().field("a").field("b").bytes, b"\x1ea\x1fb")
        self.assertEqual(c0.Builder().record(["a", b"b"]).bytes, b"\x1ea\x1fb")
        self.assertEqual(c0.Builder().record().key("a", "1").key("b", "2").bytes, b"\x1e\x01a\x1f1\x01b\x1f2")
        self.assertEqual(c0.Builder().group("g", ["x"], depth=2).bytes, b"\x1d\x1dg\x01\x1ex")

    def test_builder_close_and_leak_loop(self):
        b = c0.Builder()
        b.record("x")
        b.close()
        with self.assertRaises(c0.BuildError):
            b.record("y")
        # A simple leak check: many builders, levels and reads should not grow RSS.
        def work():
            with c0.Builder() as bb:
                bb.group("g", ["a"])
                for i in range(20):
                    bb.record(str(i), "v" * 50)
                    with bb.level():
                        bb.record("n")
                buf = bb.bytes
            body = c0.Body(buf)
            for rec in body.group(0):
                rec.value(0)
                rec.level(2).record(0).values
            c0.pretty_parse(c0.pretty_format(buf))
            c0.StreamReader(buf + b"\x17").records
        for _ in range(200):
            work()
        gc.collect()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for _ in range(3000):
            work()
        gc.collect()
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertLess(after - before, 20 * 1024)  # KiB: well under 20 MiB growth

    def test_pretty_roundtrip(self):
        buf = c0.build(lambda b: (
            b.syn(shape="tabular"),
            b.group("g", ["a", "b"]), b.record("x", "y\x1fz"),
            b.record("n"), b.level(lambda l: l.record("1", "2")),
            b.eot(),
        ))
        pretty = c0.pretty_format(buf)
        self.assertIn("␞", pretty)  # RS glyph
        self.assertIn("␐", pretty)  # DLE glyph
        self.assertEqual(c0.pretty_parse(pretty), buf)
        aligned = c0.pretty_format(buf, "    ", c0.PrettyMode.ALIGNED)
        self.assertEqual(c0.pretty_parse(aligned), buf)
        with self.assertRaises(c0.C0SyntaxError):
            c0.pretty_format(b"\x1ea\x0eb")

    def test_helpers(self):
        self.assertTrue(c0.is_assigned(c0.RS))
        self.assertFalse(c0.is_assigned(0x0E))
        self.assertTrue(c0.is_layout(c0.LF))
        self.assertTrue(c0.is_text_escaped(c0.ETB))
        self.assertFalse(c0.is_text_escaped(c0.RS))
        self.assertEqual(c0.decode_text(b"a\x10\x02b"), b"a\x02b")
        self.assertEqual(c0.VERSION, "0.2.0")


if __name__ == "__main__":
    unittest.main()
