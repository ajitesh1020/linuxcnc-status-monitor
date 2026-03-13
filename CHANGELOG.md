# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
