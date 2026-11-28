# Keyence SR-X code reader (Ethernet)

The agent reads a **Keyence SR-X100** (or another SR-X) autofocus 1D/2D code
reader over the network and turns every read into an MES `scan.received`
event in its spool. MillWright's **Scanner Setup** wizard calibrates it. A
scan never changes what the machine does.

Contents: [What you need](#what-you-need) · [Wiring and power](#wiring-and-power) ·
[Network](#network) · [First address](#first-address) · [Enrolment](#enrolment) ·
[Calibration](#calibration) · [Production reading](#production-reading) ·
[Replacement unit](#replacement-unit) · [Commands](#commands) ·
[Troubleshooting](#troubleshooting)

> Status (28 Nov 2026): built and tested against a simulated reader
> (`tools/fake_srx.py`) and the SR-X User's Manual. Points still to be
> confirmed on the real reader are listed in `docs/SCANNER_KEYENCE_SRX_WORK.md`
> section 7.

## What you need

- The reader, its **own 24 V DC supply** (about 650 mA; the SR-X has no PoE)
  and its I/O / power cable.
- An Ethernet cable to the shop network card of the LinuxCNC PC, or to a
  USB-Ethernet adapter used only for it.
- An agent newer than 1.9.0 (the scanner is in the next release; see CHANGELOG
  [Unreleased]) with `mes_enabled: true`; MillWright with Scanner Setup for the wizard.
- No root rights, no udev rule and no firewall change: the agent only makes
  outbound TCP connections to the reader (port 9004).

## Wiring and power

1. Mount the reader at its working distance (SR-X100: about 70-1000 mm; at
   150 mm it sees about 35 x 27 mm). On glossy PCBs tilt it **10-15°** so the
   light does not reflect straight back.
2. Power it from its own 24 V DC supply as in the SR-X manual. Do not power it
   from the machine's control supply unless that supply has the headroom.
3. Plug in the Ethernet cable (see Network below).

## Network

**Never connect the reader to a network card used by LinuxCNC hardware** (a
Mesa `hm2_eth` card). The Mesa link needs its card to itself; any other
traffic on it can cause following errors. Use the shop / monitoring network
card, or a dedicated USB-Ethernet adapter. See `docs/NETWORK.md`.

Give the reader a **static address** on the network it is on (it has no
DHCP name the agent could follow). The agent finds it by that address only.

## First address

Out of the box the reader is **192.168.100.100/24**. To reach it once:

1. On the LinuxCNC PC add a temporary address on that subnet to the card the
   reader is plugged into (it is gone at the next reboot; nothing else changes):

   ```
   sudo ip addr add 192.168.100.1/24 dev <card>
   ```

2. Open `http://192.168.100.100` in Chromium: the reader's **SR Web Tool**.
3. Set the static address you chose (and mask, gateway), send the settings,
   let the reader restart.
4. Remove the temporary address: `sudo ip addr del 192.168.100.1/24 dev <card>`.

Or set it later from the wizard (*2 Network and time*, "Change the reader's
address"); the agent's config is updated with it.

## Enrolment

The agent reads only from an **enrolled** reader:

```
lcnc-status-agent --scanner-enroll tcp://<reader address>:9004 --name A
systemctl --user restart lcnc-status-agent
```

or MillWright → Tools → Scanner Setup… → *Enrol a new reader*. Enrolment
connects, reads the model, firmware and **MAC address**, and writes the keys
below into `~/linuxcnc-monitor-agent/config.yaml`. It refuses a reader that
is not an SR-X unless you add `--force`. The name (`A`) becomes part of the
config keys: letters, digits and `_` only.

The reader has no serial number it can report over the network, so **the unit
is identified by its MAC address** (`scanner_A_mac`). Restart the agent after
enrolling: it reads its config at start.

```yaml
scanners: "A"                     # comma list of enrolled scanners
scanner_A_mode: "tcp"
scanner_A_host: "192.168.10.50"
scanner_A_port: 9004
scanner_A_mac: "0001FC123456"     # written by --scanner-enroll: the unit's identity
scanner_A_serial: ""              # optional, from the label (the reader cannot report it)
scanner_A_model: "SR-X100"        # written by --scanner-enroll
scanner_A_trigger: "reader"       # reader (its button / auto-read; the agent listens) | agent
scanner_A_no_read: "ERROR"        # what the reader sends when it reads nothing
scanner_A_terminator: "cr"        # cr | lf | crlf, as set on the reader
scanner_A_max_len: 4096           # longer reads are rejected
scanner_A_no_read_warn: 5         # % no-reads over the last 100 triggers -> "needs tuning"
scanner_A_pass_rate: 98           # % reading rate the calibration test must reach
```

## Calibration

Calibrate the first time, after a reader swap, after a change of label,
product or mounting, and when the MES strip says **scanner needs tuning**.
Use MillWright → Tools → **Scanner Setup…** (or `lcnc-status-agent
--scanner-calibrate A` in a terminal: the same steps). The wizard is only
available while LinuxCNC is **not running a program**.

**Connect** takes the reader from the agent for the session: the agent shows
the scanner as *setup* and reads nothing until the session ends (Save, End
session, or closing the window). If a program starts, the session ends by
itself.

| Step | What it does |
|---|---|
| 1 Connect and identify | Model, firmware, MAC; refuses a unit whose MAC is not the enrolled one |
| 2 Network and time | Shows the address; changes it (saved, config updated); sets the reader's clock (UTC) and SNTP server |
| 3 Mounting | Distance and angle (recorded), the field of view at that distance |
| 4 Live view and focus | Opens the SR Web Tool live view; runs autofocus; records fixed / auto focus |
| 5 Code types | Enables only the symbologies you use (fewer = fewer misreads, faster) |
| 6 Tuning | Per bank (1-16): present the real label, Tune, name the bank; Stop aborts |
| 7 Reading-rate test | N triggers (default 100); pass at ≥ `scanner_A_pass_rate` and the matching-level threshold; hints when it fails |
| 8 Output format | Checks / fixes what the agent expects: no header, its terminator, symbology ID appended, no-read string, nothing else appended; a live read through the agent's own parser |
| 9 Trigger | Shows the trigger mode; test trigger; 5 test reads |
| 10 Save and back up | Saves to the reader's flash, backs the settings up, writes `calibration.json`; the agent takes the reader back |
| 11 No-read image | The latest image from the reader (FTP) |

Nothing reaches the reader's flash before step 10 (except a changed address,
which is saved at once). Files, in `~/linuxcnc-monitor-agent/scanner/A/`:

- `calibration.json`: the last calibration (`passed`, reading rate, banks,
  code types, distance, who); earlier ones are kept as `calibration-<UTC>.json`
  (wizard → *History*).
- `settings-<UTC>.json`: settings backups.

## Production reading

With the reader enrolled and `mes_enabled: true`, the agent keeps one
connection to it (its own thread; never in the status sampling loop):

- every read → `scan.received` in the spool (UTF-8 text, or base64 when it
  holds GS1 separators), stored before anything else is done;
- the no-read string → shown red on the MES strip as *no read* (and, once the
  MES contract 1.3 is installed, a `scan.rejected_local` `no_read` event);
- reads over 4096 bytes or with control characters are rejected and reported;
  more than 20 reads a second are dropped and counted;
- **trigger `agent`**: MillWright asks for a read through
  `mw_data/scan_request.json`; the agent sends LON, waits up to 2 s, LOFF;
- the connection comes back by itself (retry every 1 s up to every 60 s);
  `scanner.state` events say connected / disconnected / error / setup;
- the no-read rate over the last 100 triggers above `scanner_A_no_read_warn`
  shows **scanner needs tuning**.

`lcnc-status-agent --mes-status` and MillWright's MES strip show each scanner's
state, the last read and the no-read rate.

## Replacement unit

A different reader (another MAC) is refused: the scanner shows **error,
"scanner replaced: run Scanner setup"** and nothing is read. To put the new
unit in:

1. Give it the old unit's address (First address above).
2. Enrol it under the same name (`--scanner-enroll … --name A`), restart the agent.
3. Wizard → *Restore settings*: pick the last `settings-<UTC>.json` backup.
4. Run steps 1, 4 (focus), 7 (reading-rate test) and 10 (save).

## Commands

All take `--json` (one JSON object; `--scanner-test` one per line):

| Command | |
|---|---|
| `--scanner-enroll tcp://<ip>:9004 --name A [--force]` | Enrol |
| `--scanner-info A` | Address, model, MAC, state, last calibration |
| `--scanner-test A [--count N]` | Read without forwarding (through the agent's parser) |
| `--scanner-calibrate A` | The wizard in text |
| `--scanner-backup A` / `--scanner-restore A FILE [--with-network]` | Settings backup / restore (+ SAVE) |
| `--scanner-setup A <step> …` | One wizard step (MillWright uses these) |

Every command that talks to the reader takes the scanner's setup lock while it
runs, so the running agent hands the reader over and takes it back.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `disconnected`, "cannot connect" | Power (24 V), cable, link light; `ping <address>`; the reader's address equals `scanner_A_host`; the PC has an address on the reader's subnet |
| `error`, "scanner replaced" | Another unit at that address: see Replacement unit |
| `error`, "not enrolled" | `scanner_A_mac` is empty: enrol |
| `setup` for a long time | A Scanner Setup session is open (close the wizard). A lock left by a crashed tool is removed after 2 h, or at once by the next Connect if its process is gone |
| Many no-reads, "needs tuning" | Label, distance or lighting changed: run the wizard (focus, tuning, reading-rate test) |
| Reads arrive but look wrong (extra characters, joined codes) | Wizard step 8 Output format → Check / Fix; `scanner_A_terminator` must match the reader |
| Nothing at all, no state | `mes_enabled: true`? Agent restarted after enrolment? `lcnc-status-agent --scanner-info A` |
| Calibration commands refused "Scanner setup is already open" | Another Scanner Setup (MillWright or terminal) holds the reader |

The agent's log (`journalctl --user -u lcnc-status-agent`) has a line for every
state change and rejected read. Never update the reader's firmware from these
tools; a factory reset (wizard, asked twice) erases every setting.
