# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [1.4.0]

### Added

- **Debian package** `linuxcnc-status-agent_<ver>_all.deb`, built by GitHub
  Actions and attached to each `v*` release. `sudo apt install ./….deb`.
- **Runs automatically with LinuxCNC, however it is launched** (desktop icon,
  terminal, configuration picker): installed as a per-user systemd service that
  waits for LinuxCNC's task server, attaches when it starts and lets go when it
  exits. LinuxCNC's own files are not modified, so LinuxCNC updates can't
  remove it. Single-instance lock prevents double counting.
- **No static IP needed**: `monitor_pc_ip: "auto"` (new default) broadcasts on
  every LAN / Wi-Fi interface, re-checked every 30 s; the Mesa `hm2_eth`
  interface is detected and skipped. IPs and host names (re-resolved) still
  work, also as a comma-separated list. `exclude_interfaces` key.
- `machine_name` defaults to the PC's hostname.
- Settings in `~/linuxcnc-monitor-agent/config.yaml`, created on first run and
  never overwritten by upgrades.
- `lcnc-status-agent --check` and `--version`.
- `docs/NETWORK.md` (broadcast, Wi-Fi, static IP and Mesa NIC guidance).

### Removed

- `scripts/launch_ofc.sh` and `scripts/OFC_PC.desktop` (replaced by the service).
- `docs/UDP_PAYLOAD.md` (superseded by `PROTOCOL.md`).

### Fixed

- **Parts were never counted on a full program run.** The end check compared
  `motion_line` with the last `M2`/`M30`/`%` line, but `M2`/`M30` are not moves
  (so `motion_line` never gets there) and the trailing `%` never executes. The new
  `program_tracker.py` finds the last *move* before M2/M30 and counts a part when
  the cycle goes idle after that move (or once `current_line` is seen on M2/M30).
- **Full runs were flagged "Run From Here".** At cycle start `motion_line` still
  holds the previous run's last line, and with 1 s polling the program is already
  past `first_exec_line + 2`. The start point is now the first *new* move line,
  with a tolerance scaled to the program length.
- E-stop / machine-off mid-cycle is always an abort, even near the end.

### Added

- **Partial parts.** A part interrupted and then *restarted from the top* (instead
  of continued with Run From Here) is recorded as a partial part with how far it
  got: `partial_parts`, `partial_part_equiv` (e.g. `0.5` for half done),
  `last_partial_pct`. Also `program_progress_pct`, `part_in_progress`,
  `gcode_tail_line`, and per-joint `hard_limit`.
- `sample_interval_s` (default 0.1 s): LinuxCNC status is now read at 10 Hz for
  cycle tracking; packets are still sent every `poll_interval_s`, plus
  immediately on a cycle-state change.
- The program is rescanned when the loaded file is edited and reloaded under the
  same name (path + mtime + size).

### Changed

- **Run From Here now continues the cycle timer** instead of restarting it. When a
  run is stopped before M2/M30 the elapsed time is carried; the next Run From Here
  resumes from that time, and if it reaches the end line it is **counted as a
  part** with the full (carried + continued) duration. A fresh start from the top
  resets the clock. Aborts are still recorded when a fresh run is stopped early
  (with the time carried so it can be resumed). Added a calculator test suite.

### Added

- **Work-coordinate positions.** Each axis now also reports `work` — the position
  in the active work coordinate system (e.g. G54), matching the LinuxCNC DRO —
  computed as machine position minus g5x/g92/tool offsets. `pos` remains the
  machine (absolute) position.
- **Active WCS + offsets** in the status packet: `wcs` (e.g. `"G54"`),
  `g92_offset`, and `tool_offset` (alongside the existing `g5x_offset`).
- **`last_abort_ms`** — duration of the most recently aborted cycle, so the
  dashboard can show abort timing (not just the count).

### Fixed

- **Axis positions were always 0.** `_collect_axis_data` read the position from
  `stat.axis[n]["input"]`, but in LinuxCNC 2.8+ the per-axis dict has no position
  (only velocity + limits) — the actual position is in `stat.actual_position`.
  Positions are now read from there. Also read the camelCase limit keys
  (`minPositionLimit`/`maxPositionLimit`) with a snake_case fallback. Confirmed
  against LinuxCNC 2.9.10.

### Changed

- **License: relicensed from MIT to GPL-2.0-or-later.** The agent imports the
  GPL-licensed LinuxCNC Python module, so a program linking it must be GPL. The
  prior MIT license (with a blank copyright holder) was incorrect. Added
  copyright + SPDX headers to all source files.
- **Configuration moved to `config.yaml`** — no more editing constants in
  `status.py`. Copy `config.example.yaml` to `config.yaml` and edit. A `--config`
  flag selects an alternate path; missing files fall back to built-in defaults.
  PyYAML is used if installed, else a built-in flat parser keeps the agent
  dependency-free.

### Added

- **`PROTOCOL.md`** — the normative, versioned UDP wire-protocol spec shared by
  the agent and the dashboard. `docs/UDP_PAYLOAD.md` is now a companion reference.
- Every packet now carries **`proto`** (protocol version, currently `1`) and
  **`machine_name`** (from config), so the dashboard can detect version drift and
  distinguish machines in a multi-machine view. Both are additive/backward-compatible.
- `.gitignore` (ignores the local `config.yaml`, Python caches, and logs).

---

## [1.3.0] — 2026-03-13

### Fixed

**Logging not silent in production**

In v1.2.0 the root logger was set to `DEBUG` unconditionally, then a file
handler filtered at `WARNING` was added. Log records at DEBUG/INFO were
still *created* — Python string interpolation in every `logger.debug()`
call was still executing — just discarded before writing. Zero-overhead
silence requires the root logger itself to be `WARNING`.

Fix:
- Production (no `--dev`): `root.setLevel(WARNING)` + `NullHandler` only.
  No file created. No console output. All `logger.debug()` / `logger.info()`
  calls short-circuit instantly at the root level — zero overhead.
- Dev mode (`--dev` or `CNC_DEV_MODE=1`): `root.setLevel(DEBUG)` +
  console `StreamHandler` + rotating file handler → `/tmp/cnc_status.log`.
  Log file is only created when dev mode is active.

### Decision: NML errors remain passive (AXIS keeps priority)

`_NmlPoller` (the v1.3.0-pre background thread) was evaluated but rejected.

Root cause of the original bug: LinuxCNC's NML error channel is a
single-consumer queue. The first caller of `poll()` gets the message and
it is **permanently deleted** for all other readers. Making `status.py`
poll faster than AXIS would cause the operator at the CNC machine to stop
seeing error notifications in the AXIS GUI — a safety concern.

Decision: AXIS keeps priority on the error queue. `status.py` polls
passively once per second inside the main loop. Any errors caught are
included in the UDP packet as a bonus; `nml_errors: []` is normal and
expected. The `exec_state` field in every packet is the reliable way to
detect an error condition on the monitoring PC — it is read from the
broadcast stat channel (not the queue) so every reader sees it
simultaneously without deletion.

exec_state values: `1`=EXEC_ERROR, `2`=EXEC_DONE, `3`=EXEC_WAITING_FOR_MOTION,
`4`=EXEC_WAITING_FOR_MOTION_QUEUE, `7`=EXEC_WAITING_FOR_PAUSE

---

## [1.2.0] — 2026-03-13

### Changed
- Replaced M100/M101 custom M-code approach with automatic M2/M30 end-line
  detection — no G-code changes required, no shell scripts needed
- `_GcodeMarkerScanner` → `_GcodeEndDetector` (scans for M2/M30/%)
- `signal_program_complete()` → `signal_cycle_complete()`
- `program_complete_signalled` → `cycle_complete_signalled` in snapshot

### Removed
- `docs/GCODE_MARKERS.md` (M-code approach obsolete)
- `MCODE_PROGRAM_START`, `MCODE_PROGRAM_COMPLETE` constants

### Fixed
- LinuxCNC "unknown M-code" error caused by M100/M101 in G-code without
  corresponding shell scripts in the config directory

---

## [1.1.0] — 2026-03-13

### Added
- Idle suppression: one packet on IDLE transition, then 30 s heartbeat
- G-code file streaming: `gcode_file` UDP packet type
- Run-From-Here detection via `motion_line > first_exec_line`
- `--pretty` log file in `udp_receiver.py`
- `--save-gcode DIR` in `udp_receiver.py`
- Static IP setup procedure in README

---

## [1.0.0] — 2026-03-13

### Added
- Initial release: `status.py`, `cycle_time_calculator.py`,
  `launch_ofc.sh`, `OFC_PC.desktop`, `udp_receiver.py`
