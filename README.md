# legion-lights

Case lighting control for the Lenovo Legion T5 (30AGB10) on Omarchy / Arch Linux.

The case controller (USB `17EF:C955`, interface 1) is a standard **HID LampArray**:
70 RGB lamps, at most one update every 21 ms. This tool sends it feature reports through
`/dev/hidrawN`. It needs Python 3 only, with no extra packages and no kernel driver.

## Install

```bash
./install.sh
```

The installer adds the command and the Walker menu to `/usr/local/bin`. It also adds a udev rule
(so you don't need sudo), a suspend/resume hook, an Omarchy `theme-set` hook, and a line in Hyprland
autostart that restores your lights at login.

## Use

```bash
legion-lights info                     # controller attributes + report layout
legion-lights identify                 # light each zone in turn to learn which is which
legion-lights color purple             # solid color (names, RRGGBB or #RRGGBB)
legion-lights color ff6000 -b 40 --lamps bar
legion-lights effect wave              # rainbow across the case, using real lamp positions
legion-lights effect breathe -c cyan
legion-lights effect spin -c red       # rotating highlight on each fan ring
legion-lights theme                    # match the Omarchy theme accent, and follow theme changes
legion-lights theme --effect breathe
legion-lights off
legion-lights auto                     # hand back to firmware: on the T5 this goes DARK (see below)
legion-lights-menu                     # Walker menu with all of the above
```

Effects run in the background as a transient systemd user unit. Any new command stops the
running effect.

## Zones

The defaults come from the lamp positions the controller reports: four rings of 12 (`ring1`–`ring4`),
a 16-lamp `bar`, and 6 `extra` lamps. Run `legion-lights identify`, then give them real names in
`~/.config/legion-lights/zones.json`:

```json
{ "rear_fan": [0, 11], "front_bar": [12, 27], "front_fan_top": [28, 39],
  "front_fan_mid": [40, 51], "front_fan_low": [52, 63], "logo": [64, 69] }
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
This layout was checked against the real controller from Windows.

## Tests

```bash
python3 -m unittest discover -s tests
```
