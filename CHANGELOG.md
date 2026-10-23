# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added

- **`loop_lag_ms`** in status packets (MES plan P2.7; PROTOCOL.md §4.10):
  p50 / p99 / max of how late the sampling loop's samples start, over the last
  60 s. Tested: a MES worker stuck inside the spool does not raise it.
- **Agent ↔ MillWright link `agent_mes_link.py`** (MES plan P1.6;
  `mes_millwright_config`). A worker thread copies MillWright's outbox
  (`mw_data/millwright.db`, opened read-only) into the spool in one
  transaction with its cursor, then writes `mw_data/mes_ack.json`
  (`{"epoch", "seq"}` = the last event stored). A new MillWright epoch is read
  from the start; a full spool or an event the contract rejects leaves the
  events in the outbox (logged once), never skipped. Every cycle becomes a
  `cycle.observed` event: at cycle start the sampling loop reads only
  `mw_data/current_execution.json` (<= 1 KB; again every 0.5 s in the first
  3 s if MillWright is slower) and takes MillWright's `execution_id` and
  operator session when the file names the program LinuxCNC runs
  (`program_path` or `run_file`) and MillWright's start is 3 s before to 1 s
  after its own (`correlation_method: "explicit"`); otherwise its own
  `execution_id` and no operator (`"none"`). The loop hands the cycle over with
  `put_nowait`; it never waits on the MES side.
- **MES spool `agent_mes_spool.py`** (MES plan P2.4). `mes_enabled` (default
  `false`: nothing MES runs) opens `~/linuxcnc-monitor-agent/mes_spool.db`
  (WAL, `synchronous=FULL`; tables `meta`, `spool`, `cursors` as the plan).
  Every event is checked with the vendored contract's `validate_event()` and
  stored as its `encode()` bytes; events from another producer (MillWright's
  outbox) are stored unchanged, in one transaction with that source's cursor.
  A new epoch when the database is created again or restored from an older
  copy (sidecar `mes_spool.producer.json`), so a `producer.seq` is never used
  twice. Budget `mes_spool_mb` (1024): warning at 90 %, at 100 % new events are
  refused, never dropped; acked rows kept 7 days, pending rows never pruned.
  Status packets carry `mes_spool` and `features: ["mes1"]` (PROTOCOL.md §4.9).
  The .deb ships the contract's checker and schemas.
- **Stable machine identity `machine_uuid`** (MES plan P0.6). Made once on the
  first start in `~/linuxcnc-monitor-agent/identity.json` (mode 0600, written
  atomically; key `machine_uuid_file`) and sent in every status packet and
  journal entry (PROTOCOL.md §4.1b). FloorWatch keys machines by it, so two PCs
  with the same name stay two machines; it is also the MES `machine_id`. A
  damaged file is kept as `identity.json.bad-<UTC time>` and replaced, with a
  warning. Package install, upgrade, remove and purge never touch it.
  `--check` shows it. `tools/fleet_sim.py` gives each simulated machine its own,
  kept in `<dir>/<name>.identity.json`.
- **MES event contract `fw.mes.event/1`** vendored from FloorWatch
  (`contracts/mes/v1/`, pinned in `contracts/mes/CONTRACT_SOURCE`; MES plan
  P1.1). Nothing uses it yet; `tests/test_mes_contract.py` checks the copy is
  unchanged and that its checker passes every example on this Python. Never
  edit the copy: change FloorWatch's and run `tools/vendor_mes_contract.py`
  there.
- MES contract copy updated to 1.1: `scan.rejected_local` reason `spool_full`
  (for the spool, MES plan P2.4).

### Fixed

- **The .deb was missing `agent_auth.py`** (site key, 1.7.0), so the installed
  agent would not start. It and `agent_identity.py` are now packaged, and
  `tests/test_packaging.py` fails if a module the agent imports is left out.
- **Big G-code programs reach the dashboard** (review F05; MES plan P0.2). A
  program is sent if it has at most 32 MiB of text (33,554,432 characters) and
  splits into at most 80,000 frame-sized packets: the shared contract
  `contracts/gcode/v1.json` (canonical copy in FloorWatch, which accepts up to
  the same limits; FloorWatch before 2.0 refused anything over 4,096 chunks,
  about 4 MB). Chunks are as large as one frame allows: with long machine or
  file names the splitter used to halve to ~500 characters, which put a 32 MiB
  program over the chunk limit.
- **A program over either limit is announced, not dropped silently**: one small
  `gcode_file_refused` packet (`reason`, `limit_chars`, `limit_chunks`,
  PROTOCOL.md §5.1) goes instead, again on every resend so a dashboard started
  later learns it too. The agent logs it once, and does not read the file again
  on each resend; a file whose size alone rules it out is not read at all, and
  at most one character past the limit is ever read.
- **The program file is read in the sender thread**, not in the status loop, and
  packets are built one at a time while sending (80,000 packets held at once
  would be about 110 MB).
- Every chunk carries `content_sha256` (the whole program, UTF-8) so the dashboard
  can tell mixed or damaged chunks. PROTOCOL.md §5 / §5.1.
- `contracts/gcode/fixtures/`: real sender packets for an awkward program
  (non-ASCII, quotes, backslashes, tabs, CR LF), regenerated by
  `tools/make_gcode_fixture.py`, assembled by FloorWatch's tests.

### Added — site key (review F07)

- **`site_key`**: one shared secret per plant (the same text in FloorWatch).
  Every packet — status, G-code chunks, history replies — is signed with
  HMAC-SHA256 (`sig`, the last JSON member, PROTOCOL.md §8.1). History requests
  must then be signed and carry a fresh `nonce`; a replayed request is ignored.
  A dashboard without the key ignores `sig`, so it can be rolled out machine by
  machine. Not encryption: packets can still be read on the network.
- **`journal_bind`** (listen on one address) and **`journal_allow`** (answer
  only these dashboard IPs) for the history port.
- `config.yaml` is created readable by its owner only; the agent warns if it
  holds a site key and others can read it, or if the key is under 16
  characters. `--check` shows whether packets are signed.
- `tools/fleet_sim.py run --site-key`.

### Changed (hardening, from the readiness review)

- **The journal recovers by itself.** A database error used to switch the
  history off until the agent restarted. It is now reopened after 5 s, 10 s,
  ... up to every 5 min. Below `journal_min_free_mb` (new, default 100) free
  disk it pauses, so it never fills the disk LinuxCNC needs. Status packets
  carry `journal_state` (`ok` / `failing` / `disk_low`); `agent_id` and
  `journal_seq` are sent while it is failing too. SQLite waits at most 1 s
  (was 5 s) in the sampling loop.
- **Warnings are no longer thrown away.** Without `--dev` the agent used to
  log nothing at all. Warnings and errors now go to
  `~/linuxcnc-monitor-agent/agent.log` (1 MB x 3) and the systemd journal, each
  message at most once a minute.
- **Nothing in the sampling loop waits.** Programs over 1 MB are scanned on a
  background thread (a 32 MiB program took 2.4 s); the loop waits for the
  result only if a cycle starts or ends before the scan is done. The UDP socket
  is non-blocking: a full send buffer drops that packet instead of stalling;
  G-code chunks try three times.
- **Restart mid-part** is covered by a test: the part is still counted (as a
  Run From Here), no abort. Documented in TROUBLESHOOTING.md.
- CI: actions pinned to commits, read-only by default, a separate publish job
  for tags, `SHA256SUMS` with every package, and a published release is never
  replaced.
- `agent_gcode.py` licence header now matches the project (GPL-2.0-or-later).
- `dist/` is ignored by git.

### Added

- **Fleet simulator** `tools/fleet_sim.py` (a test tool; not installed by the
  .deb). Simulates many machines with the agent's own journal, history server,
  packet timing and G-code sender — `session_id` / `packet_seq` / `cmd_state`,
  cycles, aborts, E-stops, joint faults, failed commands, agent restarts — and
  writes what really happened to `truth.json`. `fleet_sim.py check` compares a
  dashboard's history database with it: parts and events must match exactly.
  `--drop` / `--reorder` lose or swap packets; `docs/FLEET_TEST.md` is the
  procedure for running FloorWatch and 100 machines on the LinuxCNC PC with
  `tc netem` loss, reordering, duplication and outages.

## [1.6.0]

### Added

- **Stream identity** (for review finding F02): every status packet carries
  `session_id` (new on each agent start; the part counters restart with it) and
  `packet_seq` (counts packets within the session). A dashboard can then drop a
  delayed or reordered packet, and tell a restart from a lower count, whatever the
  wall clock does. `session_id` is journalled too. See PROTOCOL.md §4.1b.

### Changed

- **The NML error channel is no longer read by default** (review F01). Reading it
  removes each message from LinuxCNC's queue, so the agent could take an error
  before the operator's GUI showed it. `nml_errors` is now `[]` unless the new
  `read_error_channel: true` is set. Faults are reported from status flags
  (`exec_state`, `estop`, `task_state`, joint `fault` / `hard_limit`), which
  nothing consumes. README no longer claims "zero risk".

### Fixed

- **G-code errors reach the dashboard** (found on the bench test, F01). With the
  error channel no longer read, a failed MDI command (G-code error, unknown
  M-code, zero feed) left no trace in the packets: LinuxCNC 2.9 never sets
  `exec_state` to EXEC_ERROR for it. Status packets now carry `cmd_state`
  (`stat.state`; `3` = the last command failed) and `cmd_serial`
  (`stat.echo_serial_number`). Each failed command is a fault edge: sent at
  once, also while idle, and journalled. Successful commands (jogs, override
  moves) do not trigger extra packets or journal writes. Read-only; the GUI
  still shows every message.
- **Faults while idle are sent at once** (review F04). The fault flags are compared
  on every sample (0.1 s); any change sends a status packet immediately, even
  while idle, and is journalled. Before, an idle fault could wait up to 30 s for
  the heartbeat, and one that cleared in between was in neither the live stream
  nor the journal. New `agent_packets.py`.
- **A malformed history request can no longer stop the history server** (review
  F06). `from_seq` / `to_seq` must be whole numbers 0…2⁶³−1; `1e309`, NaN,
  fractions, booleans and numbers too big for SQLite are rejected (they raised
  `OverflowError`, which ended the server thread). Every request is handled in its
  own catch-all (bad UTF-8, deep JSON nesting...). Status packets report
  `journal_port: 0` while the server is down, and the agent restarts it within
  60 s. `lcnc-status-agent --check` shows the error-channel and history settings.
- **The loaded program now reaches the dashboard (live toolpath).** It used to be sent
  once, in chunks of up to 50,000 characters. Such a datagram is cut into dozens of IP
  fragments, so losing any one lost the whole chunk, and a chunk could pass the UDP size
  limit and fail outright; the file was still marked as sent, and reloading the same file
  sent nothing. Now every packet fits one network frame, the file counts as sent only when
  every packet went out (else it is retried), it is sent from a background thread with
  pacing, and it is repeated every `gcode_resend_s` (60 s) for a dashboard started later.

## [1.5.0]

### Added

- **History journal on the machine PC** (`~/linuxcnc-monitor-agent/journal.db`,
  14 days by default): a compact copy of status whenever something that matters
  changes, else every 10 s while active. A dashboard that was closed, or whose
  PC was off or offline, fetches exactly what it missed over UDP
  (`journal_port`, default 5006, read-only). Status packets now carry
  `agent_id`, `journal_seq`, `journal_port`. See PROTOCOL.md §5a.
- Config: `journal_days`, `journal_interval_s`, `journal_port`.

### Fixed

- Error messages caught while the machine was idle were dropped with the
  suppressed idle packet; a packet carrying an error is now always sent.

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
