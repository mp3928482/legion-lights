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


class RealDescriptorTests(unittest.TestCase):
    """The report descriptor read from a real Legion T5 controller (17EF:C955, interface 1)."""

    def setUp(self):
        desc = (Path(__file__).parent / "legion_t5_if1_descriptor.bin").read_bytes()
        self.by_kind = {r.kind: r for r in ll.parse_feature_reports(desc).values()}

    def test_report_layout(self):
        expect = {ll.ATTRIBUTES_REPORT: (1, 23), ll.ATTR_REQUEST_REPORT: (2, 3),
                  ll.ATTR_RESPONSE_REPORT: (3, 29), ll.MULTI_UPDATE_REPORT: (4, 51),
                  ll.RANGE_UPDATE_REPORT: (5, 10), ll.CONTROL_REPORT: (6, 2)}
        for kind, (rid, size) in expect.items():
            self.assertEqual((self.by_kind[kind].id, self.by_kind[kind].size), (rid, size), hex(kind))

    def test_constant_attribute_fields_are_kept(self):
        # The controller declares its attributes as Constant; they must not be parsed as padding.
        raw = bytes.fromhex("01 46 00 80 1a 06 00 80 1a 06 00 40 0d 03 00 07 00 00 00 08 52 00 00")
        got = self.by_kind[ll.ATTRIBUTES_REPORT].decode(raw)
        self.assertEqual(got[ll.LAMP_COUNT], [70])
        self.assertEqual(got[ll.LAMP_ARRAY_KIND], [7])
        self.assertEqual(got[ll.MIN_UPDATE_US], [21000])
        self.assertEqual((got[ll.BBOX_W], got[ll.BBOX_H], got[ll.BBOX_D]), ([400000], [400000], [200000]))


class UsbTests(unittest.TestCase):
    def test_hid_interfaces_from_config_descriptor(self):
        import tempfile
        # Device + config + iface 0 (HID, 46-byte report desc, 1 endpoint) + iface 1 (HID, 327 bytes, no endpoints)
        raw = bytes.fromhex(
            "12010002000000 40ef1755c90019010200 01"
            "0902340002010 0e032".replace(" ", "")
            + "090400000103010000" "092110010001222e00" "0705810340 0001".replace(" ", "")
            + "090401000003000000" "092110010001224701")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "descriptors").write_bytes(raw)
            self.assertEqual(list(ll._usb_hid_interfaces(d)), [(0, 46), (1, 327)])

    def test_usbdevfs_control_ioctl_number(self):
        # USBDEVFS_CONTROL = _IOWR('U', 0, struct usbdevfs_ctrltransfer) = 0xC0185500 on 64-bit
        import struct
        self.assertEqual(ll._ioc(3, 0, struct.calcsize("BBHHHI4xQ"), "U"), 0xC0185500)


class ThemeTests(unittest.TestCase):
    CFG = {"zones": dict(ll.DEFAULT_THEME_ZONES), "vivid": False}
    ZONES = ["lower_fans", "front_strip", "top_fans", "rear_fan", "cpu_cooler", "extra"]

    def test_accent_reused_as_blue_still_gives_distinct_zones(self):
        # Tokyo Night: accent == blue, and magenta is only ~37 degrees of hue from it.
        pal = {k: ll.parse_color(v) for k, v in {
            "accent": "7aa2f7", "blue": "7aa2f7", "bright_blue": "7da6ff", "magenta": "ad8ee6",
            "cyan": "449dab", "red": "f7768e", "green": "9ece6a", "yellow": "e0af68"}.items()}
        got = ll.theme_zone_colors(self.ZONES, pal, self.CFG)
        self.assertEqual(list(got), self.ZONES)
        self.assertEqual(got["front_strip"], pal["accent"])
        self.assertEqual(got["top_fans"], pal["magenta"])
        self.assertEqual(got["lower_fans"], pal["green"])
        self.assertEqual(got["extra"], pal["accent"])

    def test_skips_a_colour_another_zone_already_has(self):
        pal = {"accent": (255, 0, 0), "red": (250, 10, 10), "orange": (255, 140, 0)}
        got = ll.theme_zone_colors(["front_strip", "cpu_cooler"], pal, self.CFG)
        self.assertEqual(got["cpu_cooler"], (255, 140, 0))

    def test_fixed_hex_in_config(self):
        cfg = {"zones": {"rear_fan": ["#00ff00"]}, "vivid": False}
        got = ll.theme_zone_colors(["rear_fan"], {"accent": (1, 2, 3)}, cfg)
        self.assertEqual(got["rear_fan"], (0, 255, 0))

    def test_vivid_keeps_hue_and_greys(self):
        self.assertEqual(ll.vivid((0x7a, 0xa2, 0xf7)), (0x4a, 0x84, 0xff))
        self.assertEqual(ll.vivid((128, 128, 128)), (128, 128, 128))


class ColorTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ll.parse_color("#ff8000"), (255, 128, 0))
        self.assertEqual(ll.parse_color("0x0000ff"), (0, 0, 255))
        self.assertEqual(ll.parse_color("purple"), (128, 0, 255))

    def test_scale(self):
        self.assertEqual(ll.scale((255, 100, 0), 0.5), (128, 50, 0))


if __name__ == "__main__":
    unittest.main()
