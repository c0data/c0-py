import unittest

import c0


class Basic(unittest.TestCase):
    def test_build_read_roundtrip(self):
        buf = c0.build(lambda b: (
            b.group("users", ["name", "amount"]),
            b.record("Alice", "1502.30"),
            b.record("Bob", "340.00"),
        ))
        t = c0.Table(buf)
        self.assertEqual(bytes(t.name), b"users")
        self.assertEqual([bytes(h) for h in t.headers], [b"name", b"amount"])
        self.assertEqual(t.record_count, 2)
        self.assertEqual(bytes(t.record(0).field(0)), b"Alice")
        self.assertEqual(bytes(t.record(1).field(1)), b"340.00")
        self.assertTrue(c0.canonical(buf))

    def test_zero_copy_view(self):
        buf = c0.build(lambda b: (b.group("g"), b.record("hello")))
        field = c0.Table(buf).record(0).field(0)
        self.assertIsInstance(field, memoryview)
        # the view aliases the original buffer (no copy)
        self.assertEqual(bytes(field), b"hello")

    def test_document(self):
        buf = c0.build(lambda b: (
            b.file("mydb"),
            b.group("users", ["name"]),
            b.record("Alice"),
            b.group("products", ["id"]),
            b.record("01"),
        ))
        doc = c0.Document(buf)
        self.assertEqual(bytes(doc.name), b"mydb")
        self.assertEqual(doc.group_count, 2)
        self.assertEqual(bytes(doc.group_by_name("products").record(0).field(0)), b"01")
        self.assertIsNone(doc.group_by_name("missing"))

    def test_escaping(self):
        buf = c0.build(lambda b: (b.group("g"), b.record("a\x1fb", "c")))
        rec = c0.Table(buf).record(0)
        self.assertEqual(len(rec), 2)
        self.assertEqual(rec.value(0), b"a\x1fb")

    def test_trailing_empty_field(self):
        self.assertEqual(len(c0.Table(b"\x1eAlice\x1f").record(0)), 2)
        self.assertEqual(len(c0.Table(b"\x1eAlice").record(0)), 1)

    def test_names_reject_control_bytes(self):
        with self.assertRaises(ValueError):
            c0.Builder().group("bad\x1fname")

    def test_stream_torn_tail(self):
        buf = b"\x1ecreate\x1fa1b2\x17\x1ename\x1fdra"
        r = c0.StreamReader(buf)
        self.assertTrue(r.torn)
        self.assertEqual(r.block_count, 1)
        self.assertEqual(r.table.record_count, 1)

    def test_list_field_bytes_and_readback(self):
        buf = c0.build(lambda b: (
            b.group("users"),
            b.record("Alice"),
            b.list_field(["Admin", "Editor", "User"]),
            b.field("1502.30"),
        ))
        self.assertEqual(
            buf,
            b"\x1dusers\x1eAlice\x1f\x02Admin\x1fEditor\x1fUser\x03\x1f1502.30")
        rec = c0.Table(buf).record(0)
        self.assertEqual(len(rec), 3)
        self.assertEqual(rec.value(0), b"Alice")
        self.assertEqual(rec.list(1), [b"Admin", b"Editor", b"User"])
        self.assertEqual(rec.value(2), b"1502.30")

    def test_list_field_escaped_items_and_empty(self):
        items = ["a\x1fb", "c\x02d", ""]
        buf = c0.build(lambda b: (
            b.group("g"),
            b.record("x"),
            b.list_field(items),
            b.list_field([]),
        ))
        rec = c0.Table(buf).record(0)
        self.assertEqual(len(rec), 3)
        self.assertEqual(rec.list(1), [i.encode() for i in items])
        self.assertEqual(rec.list(2), [])

    def test_list_keeps_nested_scope_intact(self):
        # an item containing a nested STX/ETX scope with its own US separators
        buf = b"\x1dg\x1ex\x1f\x02a\x1f\x02p\x1fq\x03\x1fb\x03"
        rec = c0.Table(buf).record(0)
        self.assertEqual(rec.list(1), [b"a", b"\x02p\x1fq\x03", b"b"])

    def test_list_of_plain_field(self):
        buf = c0.build(lambda b: (b.group("g"), b.record("plain", "a\x1fb")))
        rec = c0.Table(buf).record(0)
        self.assertEqual(rec.list(0), [b"plain"])
        self.assertEqual(rec.list(1), [b"a\x1fb"])

    def test_builder_parity_bytes(self):
        b = c0.Builder()
        b.section("Intro").section("Deep", depth=2).block("text\x1e").item("one")
        self.assertEqual(
            b.bytes, b"\x1dIntro\x1d\x1dDeep\x1etext\x10\x1e\x1fone")
        b = c0.Builder()
        b.record("a").field("b\x1f").nested(lambda n: n.record("c"))
        self.assertEqual(b.bytes, b"\x1ea\x1fb\x10\x1f\x02\x1ec\x03")
        self.assertEqual(c0.Builder().ref("users").bytes, b"\x05users")
        self.assertEqual(
            c0.Builder().ref("users", "42", "name").bytes,
            b"\x05\x02users\x1f42\x1fname\x03")
        self.assertEqual(c0.Builder().etb().bytes, b"\x17")
        self.assertEqual(c0.Builder().etb("a1b2").bytes, b"\x17a1b2")

    def test_etb_payload_rejects_control_bytes(self):
        with self.assertRaises(ValueError):
            c0.Builder().etb("bad\x1fpayload")
        with self.assertRaises(ValueError):
            c0.Builder().ref("bad\x1fname")

    def test_pretty_roundtrip(self):
        buf = c0.build(lambda b: (b.group("g", ["a", "b"]), b.record("x", "y")))
        pretty = c0.pretty_format(buf)
        self.assertIn("␞", pretty)  # ␞
        self.assertEqual(c0.pretty_parse(pretty), buf)


if __name__ == "__main__":
    unittest.main()
