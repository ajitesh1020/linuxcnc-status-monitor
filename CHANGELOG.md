# Changelog

All notable changes to this project are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.0] — 2025-05-30

### Added
- `status.py` — industrial-grade LinuxCNC status monitor
  - Polls LinuxCNC stat channel every second (non-blocking)
  - Collects axis position, velocity, limits (all active axes)
  - Collects per-joint position, velocity, homed state, following error
  - Collects per-spindle RPM, direction, override, at-speed
  - Drains and ships NML error messages in every UDP packet
  - Auto-reconnects to LinuxCNC after up to 10 consecutive poll failures
  - Graceful shutdown on `SIGTERM` / `SIGINT` — marks in-flight cycle as aborted
  - DEV_MODE via `--dev` flag or `CNC_DEV_MODE=1` environment variable
  - Rotating log file (`/tmp/cnc_status.log`, 5 MB × 3 backups)

- `cycle_time_calculator.py` — thread-safe cycle time and production counter
  - Tracks: start / pause / resume / stop / abort transitions
  - Millisecond-precision timing via `time.perf_counter_ns()`
  - Completed cycle durations stored in rolling buffer (last 500)
  - Aborted cycles tracked separately with their own duration history
  - Part counting with configurable threshold filter (default: 50% of last cycle)
  - `CycleSnapshot` dataclass — immutable, safe for cross-thread reads
  - `reset_stats()` for operator-initiated shift/job resets

- `scripts/launch_ofc.sh` — wrapper script
  - Starts LinuxCNC and `status.py` together
  - Sends `SIGTERM` to `status.py` when LinuxCNC exits (any reason)
  - Falls back to `SIGKILL` after 5-second grace period

- `scripts/OFC_PC.desktop` — desktop launcher pointing to `launch_ofc.sh`

- `examples/udp_receiver.py` — example receiver for monitoring PC
  - Compact summary mode (default)
  - Pretty-print mode (`--pretty`)
  - Selective field display (`--fields`)

- Full documentation: `README.md`, `docs/UDP_PAYLOAD.md`,
  `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`
