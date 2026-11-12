"""Offline tests: run with `python3 -m unittest discover -s tests` from the project folder.

The descriptor below reproduces the feature-report layout Windows reported for the
Legion T5 controller (17EF:C955, interface 1): report IDs 1-6, 51-byte multi-update
with 8 lamps per report.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import legion_lights as ll  # noqa: E402


def item(prefix, value=None, size=None):
    if value is None:
        return bytes([prefix])
    size = size or (1 if value < 0x100 else 2 if value < 0x10000 else 4)
    code = {1: 1, 2: 2, 4: 3}[size]
    return bytes([prefix | code]) + value.to_bytes(size, "little")


UPAGE, USAGE, COLL, END, RID, RSIZE, RCOUNT, FEAT = 0x04, 0x08, 0xA0, 0xC0, 0x84, 0x74, 0x94, 0xB0


def field(usages, bits, count=1):
    out = b"".join(item(USAGE, u) for u in usages)
    return out + item(RSIZE, bits) + item(RCOUNT, count) + item(FEAT, 0x02)


def report(rid, kind, body):
    return item(RID, rid) + item(USAGE, kind) + item(COLL, 0x02) + body + item(END)


def legion_descriptor():
    d = item(UPAGE, 0x59) + item(USAGE, 0x01) + item(COLL, 0x01)
    d += report(1, 0x02, field([0x03], 16) + field([0x04, 0x05, 0x06, 0x07, 0x08], 32, 5))
    d += report(2, 0x20, field([0x21], 16))
    d += report(3, 0x22, field([0x21], 16) + field([0x23, 0x24, 0x25, 0x27, 0x26], 32, 5)
                + field([0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D], 8, 6))
    d += report(4, 0x50, field([0x03], 8) + field([0x55], 8) + field([0x21], 16, 8)
                + field([0x51, 0x52, 0x53, 0x54] * 8, 8, 32))
    d += report(5, 0x60, field([0x55], 8) + field([0x61, 0x62], 16, 2) + field([0x51, 0x52, 0x53, 0x54], 8, 4))
    d += report(6, 0x70, field([0x71], 8))
    return d + item(END)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.reports = ll.parse_feature_reports(legion_descriptor())
        self.by_kind = {r.kind: r for r in self.reports.values()}

    def test_report_ids_and_sizes_match_windows(self):
        expect = {ll.ATTRIBUTES_REPORT: (1, 23), ll.ATTR_REQUEST_REPORT: (2, 3),
                  ll.ATTR_RESPONSE_REPORT: (3, 29), ll.MULTI_UPDATE_REPORT: (4, 51),
                  ll.RANGE_UPDATE_REPORT: (5, 10), ll.CONTROL_REPORT: (6, 2)}
        for kind, (rid, size) in expect.items():
            self.assertEqual((self.by_kind[kind].id, self.by_kind[kind].size), (rid, size), hex(kind))

    def test_lamps_per_multi_update(self):
        self.assertEqual(self.by_kind[ll.MULTI_UPDATE_REPORT].count(ll.LAMP_ID), 8)

    def test_range_update_bytes(self):
        data = self.by_kind[ll.RANGE_UPDATE_REPORT].encode({
            ll.UPDATE_FLAGS: 1, ll.LAMP_ID_START: 0, ll.LAMP_ID_END: 69,
            ll.RED: 255, ll.GREEN: 0, ll.BLUE: 0, ll.INTENSITY: 255})
        self.assertEqual(data, bytes([5, 1, 0, 0, 69, 0, 255, 0, 0, 255]))

    def test_multi_update_bytes(self):
        data = self.by_kind[ll.MULTI_UPDATE_REPORT].encode({
            ll.LAMP_COUNT: 2, ll.UPDATE_FLAGS: 1, ll.LAMP_ID: [3, 300],
            ll.RED: [1, 4], ll.GREEN: [2, 5], ll.BLUE: [3, 6], ll.INTENSITY: [255, 255]})
        self.assertEqual(len(data), 51)
        self.assertEqual(data[:7], bytes([4, 2, 1, 3, 0, 0x2C, 0x01]))
        self.assertEqual(data[19:27], bytes([1, 2, 3, 255, 4, 5, 6, 255]))
        self.assertEqual(data[27:], bytes(24))

    def test_decode_attributes(self):
        rep = self.by_kind[ll.ATTRIBUTES_REPORT]
        raw = rep.encode({ll.LAMP_COUNT: 70, ll.BBOX_W: 400000, ll.LAMP_ARRAY_KIND: 7, ll.MIN_UPDATE_US: 21000})
        got = rep.decode(raw)
        self.assertEqual(got[ll.LAMP_COUNT], [70])
        self.assertEqual(got[ll.BBOX_W], [400000])
        self.assertEqual(got[ll.LAMP_ARRAY_KIND], [7])
        self.assertEqual(got[ll.MIN_UPDATE_US], [21000])

    def test_ioctl_numbers(self):
        # Values from <linux/hidraw.h>: HIDIOCSFEATURE(2) / HIDIOCGFEATURE(51)
        self.assertEqual(ll.HIDIOCSFEATURE(2), 0xC0024806)
        self.assertEqual(ll.HIDIOCGFEATURE(51), 0xC0334807)


class ColorTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ll.parse_color("#ff8000"), (255, 128, 0))
        self.assertEqual(ll.parse_color("0x0000ff"), (0, 0, 255))
        self.assertEqual(ll.parse_color("purple"), (128, 0, 255))

    def test_scale(self):
        self.assertEqual(ll.scale((255, 100, 0), 0.5), (128, 50, 0))


if __name__ == "__main__":
    unittest.main()
