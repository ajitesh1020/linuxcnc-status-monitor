# Instructions for Claude Code on the LinuxCNC PC — Keyence SR-X100 scanner

Written 28 Nov 2026 by the Windows session for a Claude Code session on the
LinuxCNC test PC (Dell OptiPlex 9020, Debian 12, LinuxCNC 2.9, AXIS, config
`test_config`). The Windows session's history doesn't come with you;
everything you need is here and in the linked files.

MES plan item **P2.1 (scanner hardware, decision D1) is decided**: a
**Keyence SR-X100** autofocus 1D/2D code reader on **Ethernet (TCP)**. This
document replaces, for this reader, the USB keyboard / serial adapters planned
in P2.2–P2.3 (those stay valid for other scanners). The reader is **not
connected yet**: packages 1–4 are built and tested against a fake reader;
package 0 (the spike) runs as soon as the reader is wired.

## Prompt to paste

Start Claude Code in `~/linuxcnc-status-monitor` (after `git pull` on `main`)
and paste:

> Read docs/SCANNER_KEYENCE_SRX_WORK.md and follow it. Work through its
> packages in order, one branch and one PR per package, in this repo and in
> ~/millwright. Keep to its interfaces (section 4) exactly. The reader is not
> connected yet: start with the packages that don't need it, and stop to ask
> me before anything the document says needs my OK.

---

## 1. Background

- **FloorWatch** (Windows, repo `ajitesh1020/floorwatch`): desktop and MES site
  service. The service receives MES events from this agent over HTTPS.
- **This agent** (`ajitesh1020/linuxcnc-status-monitor`): runs on every LinuxCNC
  PC; already has the MES spool, forwarder, enrolment, discovery and cycle-gate
  file (agent 1.9.0). It reads scanners and turns each read into a
  `scan.received` event (contract `fw.mes.event/1`, `contracts/mes/v1/`).
- **MillWright** (`ajitesh1020/millwright`): the operator GUI inside AXIS
  (PySide6 panels). It shows the MES strip from `mes_status.json`.

Read first: FloorWatch `docs/MES_IMPLEMENTATION_PLAN.md` §2 (rules), §7
(Phase 2) and Appendix A; this repo's `docs/MES_LINUX_WORK.md` §3 (rules) and
§4 (interfaces).

### 1.1 What the Windows session found about the SR-X100

Keyence ships a Windows-only SDK (`SR_SDK_8_71`: `Keyence.AutoID.SDK.dll`, .NET
Framework, WinForms). It is only a wrapper over network protocols, so **don't
port or emulate the SDK**; talk to the reader directly with the Python
standard library:

| SDK class / call | Protocol underneath | Here |
|---|---|---|
| `ReaderAccessor.Connect`, `ExecCommand("LON")`, data callback | One TCP socket. `CommandPort` and `DataPort` both default to **9004** (SDK help); commands and read data share it when the ports are equal. ASCII, commands end in CR | `socket` |
| `ExecCommand(cmd, timeoutms)` | Command → reply `OK,<cmd>[,data]` or `ER,<cmd>,<code>` | same socket |
| `OpenFtp`, `GetFileList("IMAGE")`, `GetFile` | FTP (SR-X also SFTP) on the reader: images and history JSON | `ftplib` |
| `LiveviewForm` | The reader's image stream | Not built: the SR-X's built-in **SR Web Tool** (browser, Chromium works) does live view |
| `ReaderSearcher` | UDP broadcast, format undocumented | Not built: the reader gets a fixed IP |

Commands seen in the SDK samples: `LON`, `LOFF`, `TUNE,01`, `FTUNE`,
`BLOAD,01`, `KEYENCE`. From sibling models' manuals (SR-750 / SR-1000 /
SR-2000; **confirm every one in the SR-X User's Manual**, communication
chapter): `TQUIT` (end tuning), `TEST1` / `TEST2` / `QUIT` (reading-rate and
tact tests), `RESET`, `SAVE`, `BCLR` (clear send buffer), `NUM` (read
history), `WP` / `RP` and `WN` / `RN` (write / read settings), banks 01–16,
error reply `ER,<cmd>,<code>` (e.g. 21 buffer overflow, 22 load/save error).

Hardware: 100BASE-TX Ethernet (TCP/IP, FTP, SFTP, HTTP/HTTPS, SNTP, BOOTP,
EtherNet/IP, PROFINET), RS-232C, USB 2.0, 2 inputs / 3 relay outputs,
**24 V DC ~650 mA (no PoE found: it needs its own supply)**, IP65/67, default IP
192.168.100.100. Get the **SR-X User's Manual** and **SR WEB Monitor User's
Manual** from https://www.keyence.com/support/user/barcode/sr-x/manual/ (free
registration; ask the user to download them if you can't) and keep them next
to this repo (not committed: Keyence's copyright).

## 2. Packages, in order

Each is one branch (`feat/<name>`) and one PR. Tests with every package
(`python3 -m unittest discover -s tests`, standard library only).

| # | Package | Repo | Needs the reader | Size |
|---|---|---|---|---|
| 0 | Spike: wire, address, confirm the protocol | — (notes in this doc) | **yes** | ½ day |
| 1 | `agent_srx.py`: reader client + `tools/fake_srx.py` | agent | no | M |
| 2 | `agent_scan_tcp.py`: production reading → spool | agent | no (fake), then yes | M |
| 3 | CLI: enrol, info, test, calibrate, backup / restore (`--json`) | agent | no (fake), then yes | M |
| 4 | MillWright **Scanner setup** wizard (calibration UI) | millwright | no (fake), then yes | M–L |
| 5 | Docs, packaging, network guide | both | no | S |
| 6 | Acceptance with the real reader (exit gate) | both | **yes** | 1 day |

Packages 1–4 can be done before the reader arrives; when it arrives do package
0, correct anything it contradicts (record it in §7), then package 6.

### Package 0 — Spike (when the reader is wired)

Ask the user before each physical step; they wire it.

1. **Power and network.** Reader on its own 24 V DC supply. Network: on the
   shop network card or a dedicated USB-Ethernet adapter — **never on a NIC
   used by LinuxCNC hardware** (Mesa `hm2_eth`). This test PC drives a parallel
   port, but write the rule into the guide anyway.
2. **First address.** Out of the box the reader is 192.168.100.100. Ask the
   user before changing this PC's network settings; prefer a temporary extra
   address (`sudo ip addr add 192.168.100.1/24 dev <nic>`, gone at reboot) over
   editing NetworkManager. Open `http://192.168.100.100` in Chromium (SR Web
   Tool) and give the reader the static address the user chooses.
3. **Probe** with `nc <ip> 9004` (or a 10-line Python script): send `KEYENCE\r`,
   `LON\r`, show a label, `LOFF\r`. Record in §7:
   - terminator of replies and of read data (CR? CRLF?), any header;
   - the no-read string (default "ERROR"?) and whether it is configurable;
   - how the symbology / AIM ID is appended when enabled;
   - the command that returns **model, firmware and serial number**;
   - what `TUNE,01`, `TQUIT`, `FTUNE`, `TEST1`, `QUIT` reply, and the tuning
     result format;
   - how settings are read and written (`RP`/`WP` or `RN`/`WN`), and which
     setting numbers matter (§3.4);
   - **how many TCP clients at once** (does a second connection to 9004 work,
     get refused, or kick the first?) and what happens to the socket on
     reader reboot / cable pull;
   - FTP login (user / password defaults) and the image folder name.
4. **USB check** (optional): plug its USB into the PC, record `lsusb` and
   `dmesg` (cdc_acm → `/dev/ttyACM*`? keyboard?). Only for the record.
5. Export the reader's settings from the Web Tool and keep them with the spike
   notes.

### Package 1 — `agent_srx.py` (reader client) and `tools/fake_srx.py`

Standard library only, Python 3.7+, no threads inside (callers own threads).

- `SrxClient(host, port=9004, timeout=3.0)`:
  `connect()`, `close()`, `command(cmd, timeout) -> SrxReply` (`ok`, `cmd`,
  `fields`, `error_code`), `read_line(timeout) -> bytes | None` for pushed
  read data, `info() -> {"model", "firmware", "serial"}`.
- One socket for commands and data (ports equal). Replies and read data
  arrive on the same stream: a reply is a line starting `OK,` or `ER,`
  followed by the command name just sent; everything else is read data.
  Keep this rule in one function with tests; package 0 confirms it.
- Bounded line length (4096 + framing), bounded buffers, `socket` timeouts on
  every call, TCP keepalive on. No blocking call without a timeout.
- FTP helpers: `list_images()`, `get_image(name, dest)` with `ftplib`, timeouts.
- `tools/fake_srx.py`: a TCP server that behaves like the reader as far as
  §1.1 and package 0 know (`KEYENCE`, `LON`/`LOFF` → pushes a scripted code or
  `ERROR`, `TUNE`/`TQUIT` with a scripted result, `TEST1`/`QUIT` streaming
  reading-rate lines, `RP`/`WP`, `SAVE`, `RESET` → drops the connection), plus
  fault modes: slow replies, half lines, disconnect mid-line, refuse a second
  client. All agent and MillWright tests use it.

### Package 2 — `agent_scan_tcp.py` (production reading)

- One worker thread per enrolled TCP scanner; never in the sampling loop.
- Connects to the reader, checks `info()["serial"]` against the enrolled
  serial: a different unit → `scanner.state` `error`, no reading, message
  "scanner replaced: run Scanner setup" (a swap needs re-enrolment and
  calibration).
- Each read-data line → the same checks as the other adapters (length ≤ 4096,
  rate limit, control characters) → `scan.received`
  (`framing: "cr"` or whatever package 0 finds, `symbology` when appended) →
  spool. Durable before acknowledged.
- The no-read string → `scan.rejected_local` with reason **`no_read`**
  (contract 1.3, §4.3), never a `scan.received`.
- Trigger: `scanner_<n>_trigger: reader` (default; reader's own button or
  auto-read, the agent only listens) or `agent` (the agent sends `LON`/`LOFF`
  when asked through `scan_request.json`, §4.2). A scan never changes what
  the machine does.
- Reconnect with backoff 1 s → 60 s; `scanner.state` connected / disconnected
  / error on every change; status in `mes_status.json` (`scanners` list:
  id, state, last read, no-read rate over the last 100 triggers).
- **Setup handover:** when `scanner_<n>.setup.lock` exists (§4.2), close the
  socket within 1 s, report `scanner.state` **`setup`** (contract 1.3), and
  reconnect when the lock is gone. The calibration tool owns the reader in
  between. Locks older than 2 h are stale: log, remove, reconnect.
- **Drift warning:** no-read rate over the last 100 triggers above
  `scanner_<n>_no_read_warn` (default 5 %) → status flag `retune_suggested`
  (the MES strip shows "scanner needs tuning").

### Package 3 — CLI (`lcnc-status-agent --scanner-…`)

Every command has `--json` for MillWright; text output for people.

- `--scanner-enroll tcp://<ip>:9004 --name "Scanner A"`: connects, reads
  model / firmware / serial, writes the config keys (§4.1). Refuses a model
  that isn't an SR-X unless `--force`.
- `--scanner-info <name>`: model, firmware, serial, address, state, last
  calibration (from `calibration.json`).
- `--scanner-test <name> [--count N]`: reads without forwarding, prints each
  code and the no-read count.
- `--scanner-calibrate <name>`: the text version of the wizard (§3), same
  steps and the same `calibration.json`.
- `--scanner-backup <name>` / `--scanner-restore <name> <file>`: all settings
  read through `RP`/`RN` (or the reader's export, per package 0) to a JSON
  file under `~/linuxcnc-monitor-agent/scanner/<name>/`; restore writes them
  and `SAVE`s. Used for a replacement unit.
- Every command that changes the reader takes the setup lock first and
  releases it after.

### Package 4 — MillWright "Scanner setup" wizard (calibration)

PySide6, MillWright's Tools menu, a separate window (not an AXIS panel). It
calls the agent CLI with `--json` (no reader code in MillWright). It is
available only when LinuxCNC is **not running a program** (MillWright reads
the task state; read-only). Steps in §3.

### Package 5 — Docs, packaging, network guide

- `docs/SCANNER_SRX.md` (agent): wiring, power, network rule, first address,
  enrolment, calibration, replacement unit, troubleshooting (no reads, wrong
  unit, disconnected, focus).
- `config.example.yaml`: the keys in §4.1, commented.
- MillWright user manual: the Scanner setup wizard.
- `docs/NETWORK.md`: the reader's subnet and the "never the Mesa NIC" rule.
- No udev rule and no root needed (outbound TCP only).

### Package 6 — Acceptance with the real reader

Section 6.

## 3. Calibration — every feature, in wizard order

Calibration is done the first time, after a reader swap, after a change of
label / product / mounting, and when the drift warning shows. The wizard (and
`--scanner-calibrate`) walks through these steps; each step can be repeated,
and nothing is saved to the reader's flash until step 10.

1. **Connect and identify.** Address (default from config), connection test,
   model / firmware / serial, enrolled-serial check. Takes the setup lock.
2. **Network and time.** Show IP / mask / gateway; change the static IP (the
   wizard reconnects on the new address and updates the config); set SNTP to
   this PC or the shop server so the reader's image names carry real time.
3. **Mounting check.** The reader's working distance range for SR-X100 (from
   the manual), shown with a hint; the operator enters the actual distance and
   angle (a small tilt, ~10–15°, avoids glare on glossy PCBs).
4. **Live view and focus.** Button "Open live view" → SR Web Tool in Chromium
   at the reader's address. Autofocus run (`FTUNE` or the SR-X focus command,
   package 0); show the focus position found; option to fix it (fixed focus
   for a fixed mounting is more repeatable).
5. **Code types and length.** Choose the symbologies to enable (Data Matrix,
   QR, Code 128, … — only the ones used; fewer types means fewer misreads and
   faster reads) and min / max length. Written with `WP`/`WN`.
6. **Tuning (per bank).** Present the real label; `TUNE,<bank>`; show progress
   and the tuning result (exposure, gain, decode settings, per package 0);
   `TQUIT` to abort. Up to 16 banks: name each one (e.g. "PCB DM small",
   "Panel QR") and choose whether the reader alternates banks.
7. **Reading-rate test.** `TEST1` for N triggers (default 100) on the label:
   reading rate %, matching level / margin, read time; pass when reading rate
   ≥ `scanner_<n>_pass_rate` (default 98 %) and matching level ≥ the threshold
   from the manual. `QUIT` ends it. Failed → back to step 4 or 6, with hints
   (glare → angle, low contrast → exposure, out of focus → step 4).
8. **Output format check.** Read the output settings and fix them to what the
   agent expects (§4.4): no header, terminator CR, symbology ID appended,
   no-read string "ERROR", data only. Then one live read through the agent's
   own parser shows exactly what production will store.
9. **Trigger mode.** Reader trigger (button / auto-read when a code appears)
   or agent trigger, matching `scanner_<n>_trigger`; a test trigger.
10. **Save and back up.** `SAVE` to the reader's flash; `--scanner-backup` to
    `~/linuxcnc-monitor-agent/scanner/<n>/settings-<UTC>.json`; write
    `calibration.json` (§4.5). Releases the setup lock; the agent reconnects
    and the wizard shows its state turning `connected`.
11. **Optional: no-read image.** Fetch the latest image over FTP and show it
    (useful when a label won't read). Production image upload to FloorWatch is
    **not** part of this work.

Also in the wizard: **Restore settings** (from a backup, for a replacement
unit, then steps 1, 4, 7 and 10), **Factory reset** (only after the user
confirms twice, then a full calibration), and the calibration history
(previous `calibration.json` files).

## 4. Interfaces fixed for both sessions

The Windows session codes against these. Change one only after asking the
user, and say so in the PR title.

### 4.1 Config keys (flat, Appendix A style)

```yaml
scanners: "A"                         # comma list of enrolled scanners
scanner_A_mode: "tcp"                 # new: tcp (besides hid | serial)
scanner_A_host: "192.168.100.100"
scanner_A_port: 9004
scanner_A_serial: ""                  # written by --scanner-enroll
scanner_A_model: ""                   # written by --scanner-enroll
scanner_A_trigger: "reader"           # reader | agent
scanner_A_no_read: "ERROR"
scanner_A_terminator: "cr"            # cr | lf | crlf (package 0 confirms)
scanner_A_max_len: 4096
scanner_A_no_read_warn: 5             # % over the last 100 triggers
scanner_A_pass_rate: 98               # % for the calibration test
```

### 4.2 Files (all in `~/linuxcnc-monitor-agent/scanner/`, atomic writes)

- `<name>.setup.lock` — `{"owner": "millwright|cli", "pid": 1234,
  "since": "<UTC ISO>"}`. Exists while a calibration tool owns the reader.
- `<name>/calibration.json` — §4.5. `<name>/settings-<UTC>.json` — backups.
- `<config dir>/mw_data/scan_request.json` (agent trigger only, written by
  MillWright) — `{"scanner": "A", "request_id": "<uuid>", "at": "<UTC ISO>"}`;
  the agent sends `LON`, waits up to `timeout_ms` (default 2000), `LOFF`.

### 4.3 Contract 1.3 (the Windows session adds it to FloorWatch; you re-vendor)

Additive changes only; never edit your copy:
- `scan.rejected_local.reason` gains **`no_read`** (the reader saw no code).
- `scanner.state.state` gains **`setup`** (a calibration tool owns it).
- Optional field on `scanner.state`: `model`, `serial`, `firmware`.
- Agent `features` in status packets gains **`scanner_tcp`**.

Until 1.3 is vendored, build packages 2–3 with these values behind a feature
check and mark the PRs "waits for contract 1.3".

### 4.4 Reader output format the agent expects

Data only, no header, terminator CR (package 0 may change it to what the SR-X
can do; then update §4.1 defaults), symbology ID appended in the format
package 0 records, no-read string `ERROR`. Calibration step 8 sets it.

### 4.5 `calibration.json`

```json
{"scanner": "A", "model": "SR-X100", "serial": "…", "firmware": "…",
 "at": "2026-12-01T10:15:00Z", "by": "operator name or null",
 "distance_mm": 120, "angle_deg": 12, "focus": {"mode": "fixed", "position": 0},
 "symbologies": ["datamatrix", "qr"], "banks": [{"bank": 1, "name": "PCB DM small"}],
 "test": {"triggers": 100, "reading_rate": 100.0, "matching_level": 0, "read_ms": 0},
 "passed": true, "backup": "settings-20261201T101500Z.json"}
```

Values are what the reader reports; nothing invented (`null` when unknown).

## 5. Rules

**Safety — this PC runs LinuxCNC** (as `docs/MES_LINUX_WORK.md` §3): never
change LinuxCNC's configuration, never write HAL pins, never send commands to
LinuxCNC, never stop or restart it. The user runs programs. The scanner work
never commands the machine.

**Network:** ask the user before changing this PC's network configuration
(prefer temporary `ip addr add`); never put the reader on a NIC used by
LinuxCNC hardware; no firewall changes needed (outbound TCP).

**Reader:** settings changes and `SAVE` only inside calibration (setup lock
held); **never update the reader's firmware**; factory reset only when the user
asks. Don't keep a second connection open while the agent holds the reader,
unless package 0 shows the reader allows it.

**Code:** agent standard library only, Python 3.7+; MillWright PySide6 as it
is. Nothing slow in the sampling loop. Durable before acknowledged. Never
invent data. Contract copies are never edited (§4.3).

**Git:** one branch and PR per package; ask before each push; never push to
`main`, never merge, never force-push.

## 6. Acceptance (exit gate with the real reader)

- Enrol, calibrate, back up; `calibration.json` passed; reading rate ≥ 98 %
  on the real PCB labels.
- 1000 reads at production pace: every read is one `scan.received` in the
  spool and, after forwarding, in the site service; no duplicates, no losses.
- No-read → `scan.rejected_local no_read`, visible red on the MES strip.
- Cable pulled for 2 min, reader power-cycled, PC rebooted: `scanner.state`
  shows each change; reading resumes by itself; nothing read is lost.
- Second unit (or a changed serial) → refused with "scanner replaced".
- Setup lock: wizard opened during reading → agent hands over within 1 s,
  `setup` state; closed → agent back within 5 s.
- `loop_lag_ms` p99 unchanged with the scanner reading (agent-on / agent-off
  comparison, plan P2.7).
- No read changes anything on the machine; AXIS unaffected.

## 7. Spike results (fill in during package 0)

| Question | Answer |
|---|---|
| Reply / data terminator | |
| Header | |
| No-read string | |
| Symbology ID format | |
| Model / firmware / serial command | |
| `TUNE` / `TQUIT` / `FTUNE` replies, result format | |
| `TEST1` / `QUIT` output | |
| Settings read / write commands and the numbers used | |
| Concurrent TCP clients | |
| Behaviour on reboot / cable pull | |
| FTP login and image folder | |
| USB enumeration (optional) | |

## Sources

- Keyence SR-X100: https://www.keyence.com/products/barcode/barcode-readers/sr-x/models/sr-x100/
- SR-X specs: https://www.keyence.co.uk/products/barcode/barcode-readers/sr-x/specs/
- SR-X manuals: https://www.keyence.com/support/user/barcode/sr-x/manual/
- SR Web Tool: https://www.keyence.com/resources/monitor-code-reader-from-office_611T52.jsp
- SR-750 manual (port 9004, LON/LOFF, TUNE, TEST1, error replies): https://manualmachine.com/keyence/sr750/1579925-user-manual/ and https://www.manualsdir.com/manuals/658122/keyence-sr-750-series.html?page=72
- SR-1000 / SR-2000 manuals (TUNE banks 01–16, TQUIT, FTUNE): https://www.scribd.com/document/297959336/SR-1000-escaner-Manual , https://studylib.net/doc/25858685/sr-2000-user-s-manual-e
- SDK `SR_SDK_8_71` help (`Keyence.AutoID.SDK_Help.chm`): `CommandPort` / `DataPort` default 9004, default IP 192.168.100.100
