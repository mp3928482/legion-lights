# legion-lights

Case lighting control for the Lenovo Legion T5 (30AGB10) on Omarchy / Arch Linux.

The case controller (USB `17EF:C955`, interface 1) is a standard **HID LampArray**:
70 RGB lamps, at most one update every 21 ms. It needs Python 3 only, with no extra packages and
no kernel driver.

**Why not hidraw:** the LampArray interface has no interrupt endpoint (it only uses feature reports).
Windows is fine with that, but Linux's `usbhid` refuses to bind it (`couldn't find an input interrupt
endpoint`), so there is no `/dev/hidrawN` for it. legion-lights sends the HID GET/SET_REPORT requests
itself as USB control transfers through `/dev/bus/usb/BBB/DDD`. Interface 0 (vendor page 0xFF89,
the one OpenRGB uses) is left alone. If a future kernel does bind the interface, the hidraw path is
used instead.

## Install

```bash
./install.sh
```

The installer adds the command and the Walker menu to `/usr/local/bin`. It also adds a udev rule
(so you don't need sudo), a suspend/resume hook, and two Omarchy hooks:
`~/.config/omarchy/hooks/theme-set.d/legion-lights` (follow theme changes) and
`~/.config/omarchy/hooks/post-boot.d/legion-lights` (restore your lights at login).

## Use

```bash
legion-lights info                     # controller attributes + report layout
legion-lights identify                 # light each zone in turn to learn which is which
legion-lights color purple             # solid color (names, RRGGBB or #RRGGBB)
legion-lights color ff6000 -b 40 --lamps front_strip
legion-lights effect wave              # rainbow rotating round each fan, phase-shifted across the case
legion-lights effect breathe -c cyan
legion-lights effect spin -c red       # comet with a tail chasing round each fan ring
legion-lights theme                    # each zone in a different theme color; follows theme changes
legion-lights theme --effect spin      # same colors, animated (breathe or spin)
legion-lights off
legion-lights auto                     # hand back to firmware: on the T5 this goes DARK (see below)
legion-lights-menu                     # Walker menu with all of the above
```

Effects run in the background as a transient systemd user unit. Any new command stops the
running effect. Each frame sends only the lamps whose color changed: re-sending unchanged lamps
makes the controller hitch, which shows up as uneven motion.

## Zones

Checked on a Legion T5 30AGB10 by lighting each zone on its own:

| Zone | Lamps | What lights up |
|---|---|---|
| `lower_fans` | 0–11 | lower front fans (one zone drives both) |
| `front_strip` | 12–27 | front light strip |
| `top_fans` | 28–39 | top fans |
| `rear_fan` | 40–51 | rear exhaust fan |
| `cpu_cooler` | 52–63 | CPU cooler fan |
| `extra` | 64–69 | nothing visible (the controller reports these lamps but none light) |

`legion-lights identify` lights each zone in turn. To rename or regroup zones, put your own map in
`~/.config/legion-lights/zones.json`. It replaces the defaults:

```json
{ "fans": [0, 11], "strip": [12, 27], "top": [28, 39], "rear": [40, 51], "cooler": [52, 63] }
```

## Theme colors

`legion-lights theme` gives each zone its own color from the current Omarchy theme's `colors.toml`.
It updates when you switch themes (through the `theme-set` hook). Each zone takes the first entry
in its list that the theme defines and whose hue isn't within 25° of a zone that has already picked.
Themes often reuse the accent as `blue`, for example.

| Zone | Tries, in order |
|---|---|
| `front_strip` | accent |
| `top_fans` | magenta, bright_magenta, red |
| `rear_fan` | cyan, bright_cyan, green |
| `cpu_cooler` | red, bright_red, orange, yellow |
| `lower_fans` | green, bright_green, yellow, blue |
| anything else | accent |

Theme colors are tuned for screens and wash out to near-white on LEDs. By default they're made
*vivid*: same hue, more saturation, full brightness (greys stay grey). To change the mapping or
turn that off, create `~/.config/legion-lights/theme.json`. Its zone lists replace the defaults one
zone at a time, and entries can be theme color names or fixed `#RRGGBB` values:

```json
{ "zones": { "rear_fan": ["yellow", "orange"], "cpu_cooler": ["#ffffff"] }, "vivid": true }
```

## Protocol notes

| Report | ID | Bytes (incl. ID) | Purpose |
|---|---|---|---|
| LampArrayAttributes | 1 | 23 | lamp count, size, kind, min update interval (get) |
| LampAttributesRequest | 2 | 3 | choose lamp N (set) |
| LampAttributesResponse | 3 | 29 | position, purpose, color levels of lamp N (get) |
| LampMultiUpdate | 4 | 51 | up to 8 lamps, each its own RGBI (set) |
| LampRangeUpdate | 5 | 10 | lamps A..B to one RGBI (set) |
| LampArrayControl | 6 | 2 | AutonomousMode: 0 = host controls, 1 = firmware effect (set) |

**Autonomous mode is dark on this controller.** Setting AutonomousMode=1 doesn't bring back a
built-in rainbow. On Windows, Legion Space streams every color from the host, and the firmware shows
nothing on its own. So on Linux, the lights are whatever this tool last sent. `off` and `color` are
the useful commands; `auto` is mostly for handing the device to other software.

The tool reads these IDs from the report descriptor, so it doesn't depend on the numbers above.
The controller declares the LampArrayAttributes fields as *Constant*, so the parser treats only
usage-less fields as padding. The layout was checked from Windows and on Linux against the real
controller; `tests/legion_t5_if1_descriptor.bin` is the descriptor it returns.

Each control transfer takes about 4 ms (full-speed USB), so a full 70-lamp frame
(9 multi-updates) takes about 32 ms, about 30 fps.

## OpenRGB

OpenRGB drives this controller's *other* interface (0, vendor protocol) and only reaches some of
the zones. Once legion-lights has taken host control of the LampArray, the LampArray wins: tested
on a T5, vendor-protocol writes to interface 0 then have no visible effect. So if you switch to
legion-lights, drop the case part of any OpenRGB theme hook. Keyboard control is unaffected.

## Tests

```bash
python3 -m unittest discover -s tests
```
