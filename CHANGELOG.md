# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.2.0] — 2025-05-30

### Changed

**Program completion detection — no G-code changes required**

Replaced the M100/M101 custom M-code marker approach (v1.1.0) with automatic
M2/M30 end-line detection. Custom M-codes caused LinuxCNC to throw
"unknown M-code" errors unless empty shell scripts were installed per machine.

The new approach:
- `status.py` scans the loaded `.ngc` file on every file change
- Finds `first_exec_line` (first non-blank, non-comment line)
- Finds `end_line` (LAST line matching `M2`, `M30`, or standalone `%`)
- During execution, when `motion_line >= end_line` → `signal_cycle_complete()`
- If program stops before `end_line` → abort recorded
- **Zero changes to G-code files required on any machine**

**`cycle_time_calculator.py`**
- Renamed `signal_program_start()` → removed (no longer needed)
- Renamed `signal_program_complete()` → `signal_cycle_complete()` (cleaner API)
- Renamed snapshot field `program_complete_signalled` → `cycle_complete_signalled`
- Removed `MCODE_PROGRAM_START` and `MCODE_PROGRAM_COMPLETE` constants
- Version bumped to v1.2.0

**`status.py`**
- Replaced `_GcodeMarkerScanner` with `_GcodeEndDetector`
  - Scans for M2/M30/% using regex `_GCODE_END_RE`
  - Skips blank/comment/O-word lines to find `first_exec_line`
  - Inline comment stripping before pattern match (handles `M2 (end)` style)
  - Run-From-Here tolerance of `+2` lines for LinuxCNC preamble
- Added `gcode_end_line` and `gcode_first_exec_line` to UDP payload
- Removed MCODE imports and all M-code related warnings
- Version bumped to v1.2.0

### Removed
- `docs/GCODE_MARKERS.md` — M-code marker guide no longer needed
- `MCODE_PROGRAM_START`, `MCODE_PROGRAM_COMPLETE` constants
- `signal_program_start()` method from `CycleTimeCalculator`

### Added
- `signal_cycle_complete()` method on `CycleTimeCalculator`
- `gcode_end_line` and `gcode_first_exec_line` fields in status packets
- Warning logged when no M2/M30/% found in loaded file

### Fixed
- LinuxCNC "unknown M-code" error that occurred when M100/M101 were present
  in G-code without corresponding shell scripts in the config directory

---

## [1.1.0] — 2025-05-30

### Added

**`status.py`**
- Idle suppression: one packet on IDLE transition, then silent for 30 s,
  then keep-alive heartbeat (`IDLE_HEARTBEAT_INTERVAL_S`)
- G-code file streaming: `_GcodeFileSender` sends full `.ngc` file as
  `type: "gcode_file"` chunks on load; re-sends on file change
- `_GcodeMarkerScanner`: detects M100/M101 line numbers — replaced in v1.2.0
- Run-From-Here detection via `motion_line > first_exec_line`
- New payload fields: `type`, `run_from_here_count`, `program_complete_signalled`,
  `is_run_from_here`
- `IDLE_HEARTBEAT_INTERVAL_S` and `GCODE_CHUNK_SIZE` constants

**`cycle_time_calculator.py`**
- `signal_program_start()` / `signal_program_complete()` methods (replaced in v1.2.0)
- `start_cycle(run_from_here=True)` parameter
- `run_from_here_count` counter
- Extended `CycleSnapshot` with new fields

**`examples/udp_receiver.py`**
- `--pretty` writes rotating log file (`/tmp/udp_receiver_pretty.log`, 10 MB × 3)
- `--log PATH` for custom log path
- `--save-gcode DIR` to save received G-code files to disk
- Handles `gcode_file` packet type

**`docs/GCODE_MARKERS.md`** *(removed in v1.2.0)*

**`README.md`**
- Static IP setup procedure for monitoring PC

### Changed
- `stop_cycle()` classification changed from duration-heuristic to marker-based

---

## [1.0.0] — 2025-05-30

### Added
- `status.py`: polls LinuxCNC stat/error channels, broadcasts JSON via UDP
- `cycle_time_calculator.py`: thread-safe cycle timing and production counting
- `scripts/launch_ofc.sh`: ties `status.py` lifetime to LinuxCNC process
- `scripts/OFC_PC.desktop`: desktop launcher
- `examples/udp_receiver.py`: monitoring PC receiver (summary/pretty/field modes)
- Documentation: README, UDP_PAYLOAD, CONFIGURATION, TROUBLESHOOTING
