#!/usr/bin/env python3
"""legion-lights: control the Lenovo Legion T5 case lighting on Linux.

The case controller (USB 17EF:C955) speaks the standard HID LampArray protocol
(HID usage page 0x59, "Lighting and Illumination"). This tool talks to it
directly through /dev/hidrawN with feature reports - no driver, no dependencies.

It works with any HID LampArray device; the Legion controller is just preferred
when several are present.
"""

import argparse
import colorsys
import glob
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

PREFERRED_VID, PREFERRED_PID = 0x17EF, 0xC955

# --- HID LampArray usages (page 0x59) ------------------------------------------------

PAGE = 0x59


def L(usage):
    """Full 32-bit usage on the Lighting and Illumination page."""
    return (PAGE << 16) | usage


LAMP_ARRAY = L(0x01)
ATTRIBUTES_REPORT = L(0x02)
LAMP_COUNT = L(0x03)
BBOX_W, BBOX_H, BBOX_D = L(0x04), L(0x05), L(0x06)
LAMP_ARRAY_KIND = L(0x07)
MIN_UPDATE_US = L(0x08)
ATTR_REQUEST_REPORT = L(0x20)
LAMP_ID = L(0x21)
ATTR_RESPONSE_REPORT = L(0x22)
POS_X, POS_Y, POS_Z = L(0x23), L(0x24), L(0x25)
LAMP_PURPOSES = L(0x26)
UPDATE_LATENCY_US = L(0x27)
RED_LEVELS, GREEN_LEVELS, BLUE_LEVELS, INTENSITY_LEVELS = L(0x28), L(0x29), L(0x2A), L(0x2B)
IS_PROGRAMMABLE = L(0x2C)
INPUT_BINDING = L(0x2D)
MULTI_UPDATE_REPORT = L(0x50)
RED, GREEN, BLUE, INTENSITY = L(0x51), L(0x52), L(0x53), L(0x54)
UPDATE_FLAGS = L(0x55)
RANGE_UPDATE_REPORT = L(0x60)
LAMP_ID_START, LAMP_ID_END = L(0x61), L(0x62)
CONTROL_REPORT = L(0x70)
AUTONOMOUS_MODE = L(0x71)

FLAG_UPDATE_COMPLETE = 0x01

KINDS = {0: "undefined", 1: "keyboard", 2: "mouse", 3: "game controller", 4: "peripheral",
         5: "scene", 6: "notification", 7: "chassis", 8: "wearable", 9: "furniture", 10: "art"}
PURPOSES = {1: "control", 2: "accent", 4: "branding", 8: "status", 16: "illumination", 32: "presentation"}

# --- HID report descriptor parsing ----------------------------------------------------


class Field:
    __slots__ = ("usage", "bit_offset", "bit_size")

    def __init__(self, usage, bit_offset, bit_size):
        self.usage, self.bit_offset, self.bit_size = usage, bit_offset, bit_size


class Report:
    """One feature report: its ID, the collection usage that names it, and its fields."""

    def __init__(self, report_id, kind):
        self.id = report_id
        self.kind = kind
        self.fields = []
        self.bits = 0

    @property
    def size(self):
        """Buffer size for hidraw, including the leading report-ID byte."""
        return 1 + (self.bits + 7) // 8

    def count(self, usage):
        return sum(1 for f in self.fields if f.usage == usage)

    def encode(self, values):
        """values: {usage: int | list[int]}. Lists fill repeated fields in order."""
        buf = bytearray(self.size)
        buf[0] = self.id
        seen = {}
        for f in self.fields:
            v = values.get(f.usage)
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                k = seen.get(f.usage, 0)
                seen[f.usage] = k + 1
                if k >= len(v):
                    continue
                v = v[k]
            _put_bits(buf, 8 + f.bit_offset, f.bit_size, int(v))
        return bytes(buf)

    def decode(self, data):
        """Returns {usage: [values in field order]}."""
        out = {}
        for f in self.fields:
            out.setdefault(f.usage, []).append(_get_bits(data, 8 + f.bit_offset, f.bit_size))
        return out


def _put_bits(buf, offset, nbits, value):
    value &= (1 << nbits) - 1
    for b in range(nbits):
        byte, bit = divmod(offset + b, 8)
        if (value >> b) & 1:
            buf[byte] |= 1 << bit
        else:
            buf[byte] &= ~(1 << bit) & 0xFF


def _get_bits(data, offset, nbits):
    v = 0
    for b in range(nbits):
        byte, bit = divmod(offset + b, 8)
        if byte < len(data) and (data[byte] >> bit) & 1:
            v |= 1 << b
    return v


def parse_feature_reports(desc):
    """Parse a HID report descriptor and return {report_id: Report} for feature reports.

    Each report's `kind` is the usage of the innermost collection it was declared in,
    which for LampArray devices is the report's name (e.g. LampMultiUpdateReport).
    """
    reports = {}
    glob_state = {"page": 0, "size": 0, "count": 0, "rid": 0}
    glob_stack = []
    usages, usage_min = [], None
    collections = []

    def full(value, size):
        return value if size == 4 else (glob_state["page"] << 16) | value

    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:  # long item, skip
            i += 3 + (desc[i + 1] if i + 1 < len(desc) else 0)
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        value = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        i += 1 + size

        if kind == 1:  # global
            if tag == 0:
                glob_state["page"] = value
            elif tag == 7:
                glob_state["size"] = value
            elif tag == 8:
                glob_state["rid"] = value
            elif tag == 9:
                glob_state["count"] = value
            elif tag == 10:
                glob_stack.append(dict(glob_state))
            elif tag == 11 and glob_stack:
                glob_state = glob_stack.pop()
        elif kind == 2:  # local
            if tag == 0:
                usages.append(full(value, size))
            elif tag == 1:
                usage_min = full(value, size)
            elif tag == 2 and usage_min is not None:
                usages.extend(range(usage_min, full(value, size) + 1))
        elif kind == 0:  # main
            if tag == 10:  # Collection
                collections.append(usages[0] if usages else 0)
            elif tag == 12:  # End Collection
                if collections:
                    collections.pop()
            elif tag == 11:  # Feature
                rid = glob_state["rid"]
                rep = reports.get(rid)
                if rep is None:
                    rep = reports[rid] = Report(rid, collections[-1] if collections else 0)
                # LampArray declares its read-only attributes as Constant fields, so only
                # fields without any usage are treated as padding.
                for k in range(glob_state["count"]):
                    if usages:
                        rep.fields.append(Field(usages[min(k, len(usages) - 1)], rep.bits, glob_state["size"]))
                    rep.bits += glob_state["size"]
            usages, usage_min = [], None
    return reports


# --- Device access --------------------------------------------------------------------


def _ioc(direction, nr, size, kind="H"):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


def HIDIOCSFEATURE(size):
    return _ioc(3, 0x06, size)


def HIDIOCGFEATURE(size):
    return _ioc(3, 0x07, size)


class HidrawTransport:
    """Feature reports through /dev/hidrawN (when the kernel's usbhid driver binds the interface)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

    def set_feature(self, data):
        import fcntl
        fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(data)), data)

    def get_feature(self, buf):
        import fcntl
        fcntl.ioctl(self.fd, HIDIOCGFEATURE(len(buf)), buf, True)

    def close(self):
        os.close(self.fd)


class UsbTransport:
    """Feature reports as HID class control transfers through /dev/bus/usb/BBB/DDD.

    The Legion T5 LampArray interface has no interrupt endpoint (feature reports only).
    Windows accepts that, but Linux usbhid refuses to bind it ("couldn't find an input
    interrupt endpoint"), so there is no hidraw node. The interface stays unclaimed and
    usbfs lets us talk to it directly; the kernel claims it for this fd on first use.
    """

    GET_REPORT, SET_REPORT, FEATURE = 0x01, 0x09, 3

    def __init__(self, path, interface):
        import ctypes
        self.path, self.interface = path, interface
        self.fd = os.open(path, os.O_RDWR)
        self._ctypes = ctypes
        # struct usbdevfs_ctrltransfer { u8 bRequestType, bRequest; u16 wValue, wIndex, wLength;
        #                                u32 timeout; void *data; }
        self._fmt = "BBHHHI" + ("4xQ" if ctypes.sizeof(ctypes.c_void_p) == 8 else "I")
        self._ioctl = _ioc(3, 0, struct.calcsize(self._fmt), "U")  # USBDEVFS_CONTROL

    def control(self, request_type, request, value, length_or_data, timeout_ms=1000):
        import fcntl
        ct = self._ctypes
        if isinstance(length_or_data, int):
            buf = ct.create_string_buffer(length_or_data)
        else:
            buf = ct.create_string_buffer(bytes(length_or_data), len(length_or_data))
        hdr = struct.pack(self._fmt, request_type, request, value, self.interface,
                          len(buf), timeout_ms, ct.addressof(buf))
        n = fcntl.ioctl(self.fd, self._ioctl, bytearray(hdr))
        return buf.raw[:n] if isinstance(n, int) and n >= 0 else buf.raw

    def report_descriptor(self, length):
        # Standard GET_DESCRIPTOR(HID Report) addressed to the interface
        return self.control(0x81, 0x06, 0x2200, length)

    def set_feature(self, data):
        self.control(0x21, self.SET_REPORT, (self.FEATURE << 8) | data[0], data)

    def get_feature(self, buf):
        got = self.control(0xA1, self.GET_REPORT, (self.FEATURE << 8) | buf[0], len(buf))
        buf[:len(got)] = got

    def close(self):
        os.close(self.fd)


def _usb_hid_interfaces(dev_dir):
    """Yield (interface_number, report_descriptor_length) for each HID interface of a USB
    device, read from its cached configuration descriptor in sysfs."""
    try:
        d = Path(dev_dir, "descriptors").read_bytes()
    except OSError:
        return
    i, iface = 0, None
    while i + 1 < len(d) and d[i] >= 2:
        length, dtype = d[i], d[i + 1]
        if dtype == 0x04 and length >= 9:
            iface = d[i + 2] if d[i + 5] == 0x03 else None
        elif dtype == 0x21 and iface is not None and length >= 9:
            for k in range(d[i + 5]):  # bNumDescriptors
                o = i + 6 + 3 * k
                if o + 2 < i + length and d[o] == 0x22:
                    yield iface, d[o + 1] | (d[o + 2] << 8)
        i += length


def _hidraw_lamp_arrays():
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            desc = Path(node, "device", "report_descriptor").read_bytes()
            uevent = Path(node, "device", "uevent").read_text()
        except OSError:
            continue
        reports = parse_feature_reports(desc)
        if not any(r.kind == ATTRIBUTES_REPORT for r in reports.values()):
            continue
        m = re.search(r"HID_ID=\w+:([0-9A-Fa-f]+):([0-9A-Fa-f]+)", uevent)
        vid, pid = (int(m.group(1), 16), int(m.group(2), 16)) if m else (0, 0)
        path = "/dev/" + os.path.basename(node)
        yield path, vid, pid, reports, lambda p=path: HidrawTransport(p)


def _usb_lamp_arrays():
    """LampArray interfaces that no kernel driver has bound (so they have no hidraw node)."""
    for dev_dir in sorted(glob.glob("/sys/bus/usb/devices/*")):
        if ":" in os.path.basename(dev_dir) or not Path(dev_dir, "idVendor").exists():
            continue
        hid = list(_usb_hid_interfaces(dev_dir))
        if not hid:
            continue
        try:
            vid = int(Path(dev_dir, "idVendor").read_text(), 16)
            pid = int(Path(dev_dir, "idProduct").read_text(), 16)
            bus = int(Path(dev_dir, "busnum").read_text())
            num = int(Path(dev_dir, "devnum").read_text())
            cfg = Path(dev_dir, "bConfigurationValue").read_text().strip() or "1"
        except (OSError, ValueError):
            continue
        path = f"/dev/bus/usb/{bus:03d}/{num:03d}"
        for iface, desc_len in hid:
            if Path(f"{dev_dir}:{cfg}.{iface}", "driver").exists():
                continue  # bound interfaces show up as hidraw instead
            opener = lambda p=path, n=iface: UsbTransport(p, n)
            try:
                t = opener()
            except OSError:
                if (vid, pid) == (PREFERRED_VID, PREFERRED_PID):
                    yield f"{path}:{iface}", vid, pid, None, opener
                continue
            try:
                reports = parse_feature_reports(t.report_descriptor(desc_len))
            except OSError:
                continue
            finally:
                t.close()
            if any(r.kind == ATTRIBUTES_REPORT for r in reports.values()):
                yield f"{path}:{iface}", vid, pid, reports, opener


def find_lamp_arrays():
    """Yield (name, vid, pid, reports, open_transport) for every LampArray device.

    reports is None when the device was found but could not be opened (permissions).
    """
    yield from _hidraw_lamp_arrays()
    yield from _usb_lamp_arrays()


class LampArray:
    def __init__(self, path, reports, transport):
        self.path = path
        self.by_kind = {r.kind: r for r in reports.values() if r.fields}
        needed = [ATTRIBUTES_REPORT, ATTR_REQUEST_REPORT, ATTR_RESPONSE_REPORT,
                  MULTI_UPDATE_REPORT, RANGE_UPDATE_REPORT, CONTROL_REPORT]
        missing = [hex(k & 0xFFFF) for k in needed if k not in self.by_kind]
        if missing:
            raise RuntimeError(f"{path}: LampArray descriptor is missing reports {missing}")
        self.io = transport
        a = self._get(ATTRIBUTES_REPORT)
        self.lamp_count = a[LAMP_COUNT][0]
        self.kind = a.get(LAMP_ARRAY_KIND, [0])[0]
        self.bbox_um = (a.get(BBOX_W, [0])[0], a.get(BBOX_H, [0])[0], a.get(BBOX_D, [0])[0])
        self.min_interval = max(a.get(MIN_UPDATE_US, [0])[0] / 1e6, 0.001)
        self.lamps_per_update = self.by_kind[MULTI_UPDATE_REPORT].count(LAMP_ID)

    def close(self):
        self.io.close()

    def _set(self, kind, values):
        self.io.set_feature(self.by_kind[kind].encode(values))

    def _get(self, kind):
        rep = self.by_kind[kind]
        buf = bytearray(rep.size)
        buf[0] = rep.id
        self.io.get_feature(buf)
        return rep.decode(buf)

    def set_autonomous(self, on):
        self._set(CONTROL_REPORT, {AUTONOMOUS_MODE: 1 if on else 0})

    def lamp_info(self, lamp_id):
        self._set(ATTR_REQUEST_REPORT, {LAMP_ID: lamp_id})
        r = self._get(ATTR_RESPONSE_REPORT)
        first = lambda u: r.get(u, [0])[0]
        return {"id": first(LAMP_ID), "x_um": first(POS_X), "y_um": first(POS_Y), "z_um": first(POS_Z),
                "purposes": first(LAMP_PURPOSES), "latency_us": first(UPDATE_LATENCY_US),
                "levels": (first(RED_LEVELS), first(GREEN_LEVELS), first(BLUE_LEVELS), first(INTENSITY_LEVELS)),
                "programmable": first(IS_PROGRAMMABLE)}

    def fill(self, rgb, start=0, end=None):
        end = self.lamp_count - 1 if end is None else end
        r, g, b = rgb
        self._set(RANGE_UPDATE_REPORT, {UPDATE_FLAGS: FLAG_UPDATE_COMPLETE, LAMP_ID_START: start,
                                        LAMP_ID_END: end, RED: r, GREEN: g, BLUE: b, INTENSITY: 255})

    def set_lamps(self, colors):
        """colors: {lamp_id: (r, g, b)}. Sent in as few multi-update reports as possible."""
        items = sorted(colors.items())
        n = self.lamps_per_update
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            last = i + n >= len(items)
            self._set(MULTI_UPDATE_REPORT, {
                LAMP_COUNT: len(chunk),
                UPDATE_FLAGS: FLAG_UPDATE_COMPLETE if last else 0,
                LAMP_ID: [k for k, _ in chunk],
                RED: [c[0] for _, c in chunk],
                GREEN: [c[1] for _, c in chunk],
                BLUE: [c[2] for _, c in chunk],
                INTENSITY: [255] * len(chunk),
            })

    def frame(self, colors):
        """Set every lamp; uses one range update when they all match."""
        values = list(colors.values())
        if len(values) == self.lamp_count and all(v == values[0] for v in values):
            self.fill(values[0])
        else:
            self.set_lamps(colors)


def open_device(path=None):
    found = list(find_lamp_arrays())
    if not found:
        sys.exit("No HID LampArray device found. Is the Legion lighting controller (17EF:C955) connected? "
                 "Check `grep -l c955 /sys/bus/usb/devices/*/idProduct`.")
    if path:
        match = [f for f in found if f[0] == path]
        if not match:
            sys.exit(f"{path} is not a LampArray device. Found: {', '.join(f[0] for f in found)}")
        dev = match[0]
    else:
        preferred = [f for f in found if (f[1], f[2]) == (PREFERRED_VID, PREFERRED_PID)]
        dev = (preferred or found)[0]
    name, _, _, reports, opener = dev
    try:
        transport = opener()
    except PermissionError:
        sys.exit(f"No permission to open {name}. Install the udev rule "
                 "(70-legion-lights.rules) and replug/reboot, or run with sudo.")
    if reports is None:
        transport.close()
        sys.exit(f"Couldn't read the report descriptor of {name}.")
    return LampArray(name, reports, transport)


# --- Colors, zones, state --------------------------------------------------------------

NAMED = {"red": "ff0000", "green": "00ff00", "blue": "0000ff", "white": "ffffff", "warm": "ffb070",
         "cyan": "00ffff", "magenta": "ff00ff", "purple": "8000ff", "pink": "ff4090",
         "orange": "ff6000", "yellow": "ffd000", "off": "000000", "black": "000000"}

# Default zones for the Legion T5 30AGB10, checked by lighting each one on the real case.
# "extra" (64-69) is reported by the controller but lit nothing visible on a T5.
# Override or rename them in ~/.config/legion-lights/zones.json.
DEFAULT_ZONES = {
    "lower_fans": [0, 11], "front_strip": [12, 27], "top_fans": [28, 39],
    "rear_fan": [40, 51], "cpu_cooler": [52, 63], "extra": [64, 69],
}


def parse_color(text):
    t = NAMED.get(text.lower(), text).lstrip("#")
    if t.lower().startswith("0x"):
        t = t[2:]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", t):
        raise argparse.ArgumentTypeError(f"bad color {text!r}: use RRGGBB, #RRGGBB or a name ({', '.join(NAMED)})")
    return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))


def scale(rgb, brightness):
    return tuple(max(0, min(255, round(c * brightness))) for c in rgb)


def home_dir():
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or home_dir() / ".config") / "legion-lights"


def state_dir():
    d = Path(os.environ.get("XDG_STATE_HOME") or home_dir() / ".local/state") / "legion-lights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_zones(lamp_count):
    zones = dict(DEFAULT_ZONES)
    f = config_dir() / "zones.json"
    if f.exists():
        zones = json.loads(f.read_text())
    out = {}
    for name, (a, b) in zones.items():
        a, b = max(0, a), min(lamp_count - 1, b)
        if a <= b:
            out[name] = list(range(a, b + 1))
    return out


def resolve_lamps(dev, spec):
    """spec: None (all), zone names and/or ranges like 'top_fans,front_strip,0-11'."""
    if not spec:
        return list(range(dev.lamp_count))
    zones = load_zones(dev.lamp_count)
    ids = []
    for part in spec.split(","):
        part = part.strip()
        if part in zones:
            ids += zones[part]
        elif re.fullmatch(r"\d+(-\d+)?", part):
            a, _, b = part.partition("-")
            ids += range(int(a), int(b or a) + 1)
        else:
            sys.exit(f"Unknown zone {part!r}. Zones: {', '.join(zones)}")
    return sorted({i for i in ids if 0 <= i < dev.lamp_count})


def runtime_dir():
    """Per-boot directory for the effect pidfile, so a stale PID never outlives a reboot."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not os.path.isdir(base):
        return state_dir()
    d = Path(base) / "legion-lights"
    d.mkdir(exist_ok=True)
    return d


def _pid_is_effect(pid):
    """True if pid is a legion-lights effect process (guards against PID reuse)."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"effect" in cmd and b"--foreground" in cmd and any(b"legion" in c for c in cmd)


def save_state(state):
    (state_dir() / "state.json").write_text(json.dumps(state))


def load_state():
    try:
        return json.loads((state_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return None


def stop_running_effect():
    pidfile = runtime_dir() / "effect.pid"
    try:
        pid = int(pidfile.read_text())
    except (OSError, ValueError):
        return
    if pid != os.getpid() and _pid_is_effect(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.02)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"warning: an effect (pid {pid}) is running as another user", file=sys.stderr)
    pidfile.unlink(missing_ok=True)


# --- Omarchy theme ------------------------------------------------------------------------


def omarchy_accent():
    """Best-effort accent color from the current Omarchy theme."""
    candidates = [Path(os.environ.get("XDG_STATE_HOME") or home_dir() / ".local/state") / "omarchy/current/theme",
                  home_dir() / ".config/omarchy/current/theme"]
    theme = next((c for c in candidates if c.is_dir()), candidates[0])
    probes = [
        ("colors.toml", r'^\s*accent\s*=\s*["\']?#?([0-9a-fA-F]{6})'),
        ("hyprland.conf", r'(?:activeBorderColor|col\.active_border)\s*=\s*rgba?\(\s*([0-9a-fA-F]{6})'),
        ("alacritty.toml", r'^\s*blue\s*=\s*["\'](?:#|0x)([0-9a-fA-F]{6})'),
    ]
    for name, pattern in probes:
        f = theme / name
        if f.exists():
            m = re.search(pattern, f.read_text(errors="ignore"), re.M)
            if m:
                return parse_color(m.group(1))
    sys.exit(f"Couldn't find an accent color in {theme}. Use `legion-lights color RRGGBB` instead.")


# --- Effects -------------------------------------------------------------------------------


def lamp_positions(dev):
    """Normalised (x, y) in 0..1 for each lamp, from the controller's own position data."""
    infos = [dev.lamp_info(i) for i in range(dev.lamp_count)]
    xs = [i["x_um"] for i in infos]
    ys = [i["y_um"] for i in infos]
    norm = lambda v, lo, hi: (v - lo) / (hi - lo) if hi > lo else 0.0
    return [(norm(x, min(xs), max(xs)), norm(y, min(ys), max(ys))) for x, y in zip(xs, ys)]


def hsv(h, s=1.0, v=1.0):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h % 1.0, s, v))


def run_effect(dev, name, color, brightness, speed, lamps):
    pidfile = runtime_dir() / "effect.pid"
    pidfile.write_text(str(os.getpid()))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    zones = load_zones(dev.lamp_count)
    index_in_zone = {}
    for ids in zones.values():
        for k, lamp in enumerate(ids):
            index_in_zone[lamp] = (k, len(ids))
    pos = lamp_positions(dev) if name == "wave" else None
    interval = max(dev.min_interval, 1 / 40)
    t0 = time.monotonic()
    dev.set_autonomous(False)
    last = {}
    try:
        while running:
            t = (time.monotonic() - t0) * speed
            frame = {}
            for i in lamps:
                if name == "breathe":
                    c = scale(color, brightness * (0.08 + 0.92 * (0.5 - 0.5 * math.cos(t * 2 * math.pi / 4))))
                elif name == "cycle":
                    c = scale(hsv(t / 12), brightness)
                elif name == "wave":
                    # A full rainbow around each fan ring / along each strip, rotating, with each
                    # part phase-shifted by where it sits in the case. (Colouring by position alone
                    # makes every fan one blob: its 12 lamps are only a few cm apart.)
                    x, y = pos[i]
                    k, n = index_in_zone.get(i, (0, 1))
                    c = scale(hsv(t / 4 + k / n + (x + y) / 4), brightness)
                elif name == "spin":
                    # Comet that steps one lamp at a time at an even rate, with a stepped tail.
                    # (Cross-fading between lamps looked uneven on the Legion fans in testing;
                    # whole-lamp steps read as steady motion.)
                    k, n = index_in_zone.get(i, (0, 1))
                    head = int((t / 2 % 1.0) * n)
                    s = (head - k) % n          # lamps behind the head
                    level = 0.6 ** s if s < 4 else 0.0
                    # Gamma: LEDs are linear in PWM but the eye isn't, so without this a half-way
                    # cross-fade looks like two full lamps and the head seems to stall.
                    c = scale(color, brightness * (0.02 + 0.98 * level ** 2.2))
                else:
                    raise ValueError(name)
                frame[i] = c
            start = time.monotonic()
            # Only send lamps whose colour changed since the last frame.
            changed = {i: c for i, c in frame.items() if last.get(i) != c}
            if len(changed) == len(frame):
                dev.frame(frame)
            elif changed:
                dev.set_lamps(changed)
            last.update(changed)
            time.sleep(max(0.0, interval - (time.monotonic() - start)))
    finally:
        pidfile.unlink(missing_ok=True)


# --- Commands ------------------------------------------------------------------------------


def apply_state(dev, state, args_device=None):
    """Re-create a saved state. Effects are relaunched in the background."""
    mode = state.get("mode")
    if mode == "auto":
        dev.set_autonomous(True)
    elif mode in ("color", "theme") and not state.get("effect"):
        color = omarchy_accent() if mode == "theme" else tuple(state["color"])
        dev.set_autonomous(False)
        lamps = resolve_lamps(dev, state.get("lamps"))
        dev.set_lamps({i: scale(color, state.get("brightness", 1.0)) for i in lamps})
    elif state.get("effect"):
        color = omarchy_accent() if mode == "theme" else tuple(state.get("color", (255, 255, 255)))
        launch_detached(state["effect"], color, state.get("brightness", 1.0), state.get("speed", 1.0),
                        state.get("lamps"), args_device)


def launch_detached(effect, color, brightness, speed, lamps, device):
    cmd = [sys.executable, os.path.abspath(__file__), "effect", effect, "--foreground",
           "--color", "%02x%02x%02x" % tuple(color), "--brightness", str(round(brightness * 100)),
           "--speed", str(speed), "--no-save"]
    if lamps:
        cmd += ["--lamps", lamps]
    if device:
        cmd += ["--device", device]
    # Prefer a transient systemd user unit: it survives being started from a hook
    # (e.g. the resume hook, whose service cgroup is torn down afterwards).
    if os.environ.get("XDG_RUNTIME_DIR"):
        try:
            r = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                "--description=legion-lights effect", *cmd],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return
        except FileNotFoundError:
            pass
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_list(args):
    for path, vid, pid, reports, _ in find_lamp_arrays():
        tag = "  <- Legion case lighting" if (vid, pid) == (PREFERRED_VID, PREFERRED_PID) else ""
        print(f"{path}  {vid:04x}:{pid:04x}{tag}")


def cmd_info(args):
    dev = open_device(args.device)
    w, h, d = (v / 1000 for v in dev.bbox_um)
    print(f"device         {dev.path}")
    print(f"kind           {KINDS.get(dev.kind, dev.kind)}")
    print(f"lamps          {dev.lamp_count}")
    print(f"size           {w:.0f} x {h:.0f} x {d:.0f} mm")
    print(f"min update     {dev.min_interval * 1000:.1f} ms")
    print(f"lamps/update   {dev.lamps_per_update}")
    print("feature reports:")
    names = {ATTRIBUTES_REPORT: "LampArrayAttributes", ATTR_REQUEST_REPORT: "LampAttributesRequest",
             ATTR_RESPONSE_REPORT: "LampAttributesResponse", MULTI_UPDATE_REPORT: "LampMultiUpdate",
             RANGE_UPDATE_REPORT: "LampRangeUpdate", CONTROL_REPORT: "LampArrayControl"}
    for kind, rep in sorted(dev.by_kind.items(), key=lambda kv: kv[1].id):
        print(f"  id {rep.id:3d}  {rep.size:3d} bytes  {names.get(kind, hex(kind))}")
    print("zones:")
    for name, ids in load_zones(dev.lamp_count).items():
        print(f"  {name:10s} {ids[0]}-{ids[-1]}")
    st = load_state()
    if st:
        print(f"saved state    {json.dumps(st)}")


def cmd_lamps(args):
    dev = open_device(args.device)
    for i in range(dev.lamp_count):
        info = dev.lamp_info(i)
        purpose = ",".join(n for bit, n in PURPOSES.items() if info["purposes"] & bit) or "-"
        print(f"{i:3d}  pos=({info['x_um'] / 1000:6.1f}, {info['y_um'] / 1000:6.1f}, {info['z_um'] / 1000:6.1f}) mm"
              f"  {purpose:8s} levels={info['levels']}")


def cmd_color(args):
    stop_running_effect()
    dev = open_device(args.device)
    dev.set_autonomous(False)
    lamps = resolve_lamps(dev, args.lamps)
    c = scale(args.color, args.brightness / 100)
    if len(lamps) == dev.lamp_count:
        dev.fill(c)
    else:
        dev.set_lamps({i: c for i in lamps})
    if not args.no_save:
        save_state({"mode": "color", "color": list(args.color), "brightness": args.brightness / 100,
                    "lamps": args.lamps})


def cmd_off(args):
    stop_running_effect()
    dev = open_device(args.device)
    dev.set_autonomous(False)
    dev.fill((0, 0, 0))
    if not args.no_save:
        save_state({"mode": "color", "color": [0, 0, 0], "brightness": 1.0, "lamps": None})


def cmd_auto(args):
    stop_running_effect()
    dev = open_device(args.device)
    dev.set_autonomous(True)
    if not args.no_save:
        save_state({"mode": "auto"})


def cmd_effect(args):
    stop_running_effect()
    state = {"mode": "color", "effect": args.name, "color": list(args.color),
             "brightness": args.brightness / 100, "speed": args.speed, "lamps": args.lamps}
    if not args.no_save:
        save_state(state)
    if not args.foreground:
        launch_detached(args.name, args.color, args.brightness / 100, args.speed, args.lamps, args.device)
        return
    dev = open_device(args.device)
    run_effect(dev, args.name, args.color, args.brightness / 100, args.speed, resolve_lamps(dev, args.lamps))


def cmd_theme(args):
    if args.if_following:
        st = load_state()
        if not st or st.get("mode") != "theme":
            return
        args.effect = st.get("effect")
        args.brightness = round(st.get("brightness", 1.0) * 100)
    stop_running_effect()
    state = {"mode": "theme", "effect": args.effect, "brightness": args.brightness / 100, "speed": 1.0}
    save_state(state)
    dev = open_device(args.device)
    apply_state(dev, state, args.device)


def cmd_restore(args):
    st = load_state()
    if not st:
        return
    stop_running_effect()
    dev = open_device(args.device)
    apply_state(dev, st, args.device)


def cmd_identify(args):
    stop_running_effect()
    dev = open_device(args.device)
    dev.set_autonomous(False)
    for name, ids in load_zones(dev.lamp_count).items():
        dev.fill((0, 0, 0))
        dev.set_lamps({i: (255, 255, 255) for i in ids})
        print(f"{name:10s} lamps {ids[0]}-{ids[-1]} lit - press Enter for next", end="", flush=True)
        input()
    print("Walking single lamps: 0 is red, last is blue. Press Enter to finish.")
    dev.fill((0, 0, 0))
    dev.set_lamps({0: (255, 0, 0), dev.lamp_count - 1: (0, 0, 255)})
    input()
    st = load_state()
    if st:
        apply_state(dev, st, args.device)


def build_parser():
    p = argparse.ArgumentParser(prog="legion-lights", description=__doc__.split("\n")[0])
    p.add_argument("--device", help="device as printed by `list`, e.g. /dev/bus/usb/001/003:1 (default: auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def lamps_arg(sp):
        sp.add_argument("--lamps", help="zones and/or ranges, e.g. 'top_fans,front_strip' or '0-11,40-51' (default: all)")

    sub.add_parser("list", help="list LampArray devices").set_defaults(func=cmd_list)
    sub.add_parser("info", help="show controller attributes and report layout").set_defaults(func=cmd_info)
    sub.add_parser("lamps", help="list every lamp with its position").set_defaults(func=cmd_lamps)

    sp = sub.add_parser("color", help="set a solid color")
    sp.add_argument("color", type=parse_color)
    sp.add_argument("-b", "--brightness", type=int, default=100, help="0-100")
    lamps_arg(sp)
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_color)

    sp = sub.add_parser("off", help="turn all lamps off")
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_off)

    sp = sub.add_parser("auto", help="hand control back to the controller's firmware "
                                     "(on the Legion T5 this is dark - the firmware has no effect of its own)")
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_auto)

    sp = sub.add_parser("effect", help="run an animated effect (in the background unless --foreground)")
    sp.add_argument("name", choices=["breathe", "cycle", "wave", "spin"])
    sp.add_argument("-c", "--color", type=parse_color, default=(255, 255, 255))
    sp.add_argument("-b", "--brightness", type=int, default=100)
    sp.add_argument("-s", "--speed", type=float, default=1.0)
    lamps_arg(sp)
    sp.add_argument("--foreground", action="store_true")
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_effect)

    sp = sub.add_parser("theme", help="follow the current Omarchy theme's accent color")
    sp.add_argument("--effect", choices=["breathe", "spin"], help="animate the accent color")
    sp.add_argument("-b", "--brightness", type=int, default=100)
    sp.add_argument("--if-following", action="store_true",
                    help="only act if the saved state is theme mode (for the theme-set hook)")
    sp.set_defaults(func=cmd_theme)

    sub.add_parser("restore", help="re-apply the last saved state").set_defaults(func=cmd_restore)
    sub.add_parser("identify", help="light each zone in turn to find which is which").set_defaults(func=cmd_identify)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
