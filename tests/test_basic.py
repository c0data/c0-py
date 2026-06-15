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

    def test_pretty_roundtrip(self):
        buf = c0.build(lambda b: (b.group("g", ["a", "b"]), b.record("x", "y")))
        pretty = c0.pretty_format(buf)
        self.assertIn("␞", pretty)  # ␞
        self.assertEqual(c0.pretty_parse(pretty), buf)


if __name__ == "__main__":
    unittest.main()
