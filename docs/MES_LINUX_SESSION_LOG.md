# MES plan, Linux side — session log

What the Claude Code session on the LinuxCNC PC has implemented for
`docs/MES_LINUX_WORK.md`, and what is still pending. Last updated 27 Oct 2026 (version 1.7.0, H impact runs passed).

Repos: agent = `ajitesh1020/linuxcnc-status-monitor`; MillWright = `ajitesh1020/millwright`.
The user merges, except millwright #1 and #2, merged by this session on 26 Oct at the user's request.

## Packages

| # | Package | Branch → base | PR | State |
|---|---|---|---|---|
| A | Stable machine identity (`machine_uuid`) | `feat/machine-identity` → `main` | agent #19 | **Merged** 22 Oct |
| B | `monitor_interface` | `feat/monitor-interface` → `main` | agent #20 | **Merged** 22 Oct |
| C | Fleet test exit gate (A–K + H3, L from A, on `main`) | — | — | **Passed** 26 Oct on `main`: 13 of 13 scenarios 99 of 99 exact (`docs/FLEET_TEST.md`) |
| D | MillWright outbox | `feat/mes-outbox` → `main` | millwright #1 | **Merged** 26 Oct (with contract 1.1 re-vendor 7fb82ff); AXIS check **passed** 26 Oct |
| E | MillWright run context | `feat/mes-run-context` → `main` | millwright #2 | **Merged** 26 Oct (base changed to `main` after #1; 488 tests pass on the combined branch); AXIS check **passed** 26 Oct |
| F | Agent MES spool | `feat/mes-spool` | agent #22 | **Merged** 22 Oct |
| G | Agent ↔ MillWright link | `feat/mes-millwright-link` | agent #23 | **Merged** 22 Oct; AXIS check **passed** 26 Oct |
| H | `loop_lag_ms` | `feat/loop-lag` | agent #24 | **Merged** 22 Oct; impact runs **passed** 27 Oct (see below) |
| I | Scanner adapters | — | — | Blocked: scanners not bought (user, 22 Oct) |
| J | Enrolment + forwarder to the site service | `feat/mes-forwarder` → `main` | agent #26 | **Merged** (by the user, by 26 Oct); on the real agent 26 Oct (see below) |

Also merged: agent #21 (contract 1.1), #25 (docs: J unblocked). FloorWatch #39
(contract 1.1) merged 23 Oct.

## What was implemented

**A — machine identity (agent #19).** New `agent_identity.py`: `identity.json`
(mode 0600, written without overwriting; a damaged file is kept as `.bad-<UTC>`).
`machine_uuid` in every status packet and journal entry (left out if the file
cannot be written). Config key `machine_uuid_file`; `--check` shows it. `fleet_sim`
gives each simulated machine its own uuid. PROTOCOL §4.1b, NETWORK.md.
Also fixed: `packaging/deb/build.sh` did not ship `agent_auth.py` (bug on `main`);
new `tests/test_packaging.py`.

**B — `monitor_interface` (agent #20).** `agent_net.py`: interface address,
route device, `plan_interface`. `UdpSender(interface=)` binds to the interface's
address and re-binds when it changes. `--check` shows it. NETWORK.md
"Restricted pilot network".

**D — MillWright outbox (millwright #1).** `mw_outbox` table, `panels/mw_mes.py`.
The event is written in the same transaction as its audit row; login and logout are
now atomic. `operator.session_started` / `operator.session_ended`;
`audit_session.operator_session_id`. State keys `station_id`, `outbox_epoch`,
`outbox_producer_id`; outbox pruned at session open (never above `mes_ack.json`).
Flag `[MW_MES] OUTBOX = 1`. Contract vendored from FloorWatch a8b9d42, re-vendored
23 Oct from c7c413b (contract 1.1, `spool_full`); installed with the panels and in the .deb.

**E — run context (millwright #2).** Run frozen at start: `execution_id`, program
hashes (worker thread for files > 1 MB, cached), `current_execution.json`,
`execution.started` / `execution.ended`. Interface change agreed with the user:
`run_file` added to `current_execution.json` (§4.3).

**F — spool (agent #22).** `agent_mes_spool.py`: SQLite WAL, `synchronous=FULL`,
disk budget (`mes_spool_mb`), never prunes unacknowledged events, new epoch when the
database was restored (sidecar `mes_spool.producer.json`). Config `mes_enabled`;
packets carry `mes_spool` and `features` (PROTOCOL §4.9). The .deb ships the
contract runtime files.

**G — agent ↔ MillWright (agent #23).** `agent_mes_link.py`. Sampling loop:
reads `current_execution.json` (≤ 1 KB) at cycle start and enqueues with
`put_nowait` only. Same run = `program_path` or `run_file` matches and MillWright's
start is between 3 s before and 1 s after the agent's. Worker thread: outbox
(read-only) → spool in one transaction with its cursor, then `mes_ack.json`;
`cycle.observed` with MillWright's `execution_id` ("explicit") or its own ("none",
operator null). Config `mes_millwright_config`. §4.3 updated (`run_file`, window).

**H — `loop_lag_ms` (agent #24).** p50 / p99 / max of how late loop samples start,
over 60 s (PROTOCOL §4.10). Test: a MES worker stuck in the spool does not change
loop timing.

**J — enrolment + forwarder (agent #26).** New
`agent_mes_forward.py`.
- `lcnc-status-agent --mes-enroll https://<service>:8443 <code>`: fetches the CA
  unverified and **stops before sending the code** unless its SHA-256 starts with
  the code's last 16 hex digits; key + CSR with `openssl` (EC P-256; `openssl`
  added to the .deb `Depends`); POST over TLS trusting only that CA; files in
  `~/linuxcnc-monitor-agent/mes/` (0700, files 0600). A failed attempt leaves an
  earlier enrolment untouched. Writes `mes_service_url` into `config.yaml` only
  when the user answers "y" at the prompt.
- Forwarder thread: capabilities at start and hourly; listed types only (others
  stay pending); oldest first within `max_batch_events` / `max_batch_bytes` /
  `max_event_bytes` (gzip over 1 KiB); ack per receipt (all four statuses
  final; a receipt for another `event_id` is not acked); 503 / timeout / broken
  connection → nothing marked, backoff 1 s → 5 min with jitter; 413 halves the
  batch; 401/403 → `rejected`, TLS "expired" alert or `not_after` passed →
  `cert_expired`; both stop until the certificate file changes (re-enrolment).
  Sets the spool's `site_id` from the enrolment (only if it is a UUID).
- `mes_link` in status packets (PROTOCOL §4.9) when MES is on; `--check` shows
  service, site, certificate expiry. Key `mes_service_url` (config example,
  CONFIGURATION.md). CHANGELOG.
- Tests `tests/test_mes_forward.py` (31): fake service + **real
  `floorwatch_service`** on 127.0.0.1 (enrol, wrong fingerprint leaves the code
  unused, forward + duplicate on resend, real `kill -9` between the service's
  commit and the spool update, revoke → rejected → re-enrol resumes). The real-
  service tests skip unless `FLOORWATCH_DIR` points at a FloorWatch with
  `src/floorwatch_service` (`~/floorwatch` is on `feat/f08-oee-estimate`, which
  has no service) and a Python with `cryptography` ≥ 42 is found (`~/fw-venv` has 50).
- CLI smoke test against a temporary service (HOME in a temp folder; the real
  agent config was not touched): enrolled, reuse of the code refused
  (`code_used`), `--check` shows the line.
- Separate commit: `tests/test_mes_link.py` fake MillWright now closes its SQLite
  connections; `test_never_writes_millwright_db` failed on clean `main` too (the
  GC closed the connection mid-test and SQLite checkpointed the WAL).

## C — fleet exit gate

First attempt (22 Oct, agent 84e2402, FloorWatch 52addfc): A, B, C all 99 of 99
exact; D, E, F were killed mid-settle at 23:50 when the session ended (not valid).

Second attempt (23 Oct, agent 8e02631, FloorWatch c7c413b): B (loss 10%) and C
(loss 30%) 99 of 99 exact, 0 warnings, 0 errors. A, D, E stopped at 16:08 and the
other scenarios never started (the PC was rebooted before 26 Oct). Logs kept in
`~/fw-test/gate2/old-run-2026-10-23/`.

Third run (26 Oct, started 12:27) on current `main` of both repos: agent cb6b545
(includes J), FloorWatch a3399b6. Smoke run first (F with `SMOKE=1`, check by
hand): 99 of 99 exact. Runner in `~/fw-test/gate2/` (`run_all.sh`, `scen.sh`;
started with `setsid nohup` so it survives the session): 13 scenarios, three at a
time, each in its own `unshare -rn` namespace (no sudo). 30 min busy + 180 s
settle (A: 60 min). H3 = FloorWatch restarted every ~40 s (`--hours 0.011`) for
the first 10 min. K sends the 5 MB and 32 MB programs (both should be accepted).
Results per scenario in `~/fw-test/gate2/<name>.log`; they go into
`docs/FLEET_TEST.md` when all are done. Note: F, G, H2, I are scripted on fixed
times (up to 20 min), so `SMOKE=1` does not shorten them.

| Scenario | Result |
|---|---|
| A–K, H3 | **all 99 of 99 exact**, 0 warnings / errors / gaps; done 15:23. I's pruned path not hit this time (timing); K: no F05 refusal any more. Details: `docs/FLEET_TEST.md` |

## AXIS check of D + E + G (26 Oct)

Setup (with the user's OK): `millwright install test_config --feature manager`
(backup 20261026-123621; undo `millwright rollback test_config`) and
`[MW_MES] OUTBOX = 1` added to `test_config.ini`; agent .deb built from `main`
(cb6b545) and installed by the user; agent `config.yaml` got `mes_enabled: true`
and `mes_millwright_config` = test_config (backup `config.yaml.bak-20261026`).

The user logged in as operator, ran `axis.ngc` (aborted after 37 s) and
`SJ-942_Cutting1_2.NC` (completed, 98 s), logged out, then logged in and out as
manager. Read-only results:
- MillWright outbox: 8 events, seq 1..8 in one epoch: 2 × session_started/ended
  (operator `selected_name`, manager `role_password`), 2 × execution.started/ended
  with the same `execution_id` and `operator_session_id`, program and run-file
  SHA-256, outcomes `aborted` / `completed`. `execution.started` has no audit row
  by design (the machine log writes one row per run, at its end).
- `current_execution.json` removed after each run; `mes_ack.json` = epoch, seq 8.
- Agent spool: all 8 MillWright events plus 2 `cycle.observed` (agent producer),
  both `correlation_method: explicit` with MillWright's `execution_id` and the
  operator session; agent start 0.13 s before MillWright's (inside the window).
- `--check`: MES on, not enrolled. Agent log: no errors.

**Agent bugs found (older cycle-time code, not D/E/G)** — fixed in agent #28
(merged 26 Oct; .deb built from 3c6e6a5, installed by the user 13:02):
1. `program_tracker._MOTION_RE` counts `G4` (dwell) as a move. In SJ-942 the
   "first move" is the `G04 P3` on line 10; LinuxCNC never shows a dwell as
   `motion_line`, the first real move is line 20 (> 10 + 5 tolerance), so a run
   from the top was classified as Run From Here.
2. The carried time of an aborted run is used for a Run From Here of a
   *different* program: the 37 s of `axis.ngc` was added to SJ-942's part.

## J on the real agent (26 Oct)

Site service from a separate FloorWatch `main` worktree (`~/fw-svc-src`, a3399b6), data
`~/fw-service-test`, bound to **127.0.0.1:8443 only**, run with `~/fw-venv` (cryptography 50); left
running at the user's request (background of the session: it stops when the session ends).
`lcnc-status-agent --mes-enroll` enrolled OFC-PC (certificate to 2027-10-26). The enrol prompt does not
accept piped input, so `mes_service_url` was set by hand with the user's OK (backup
`config.yaml.bak-20261026-J`). After the restart all 26 spool events reached `raw_events` (event ids
identical, 0 quarantine); spool all `acked`, `site_id` set. J's tests with `FLOORWATCH_DIR=~/fw-svc-src`:
31 of 31 (the 5 real-service tests run instead of skip).

## P2.6 operator readout (26 Oct)

Agent #31 (merged): `agent_mes_status.py` writes `mes_status.json`; `--mes-status`; format in
`MES_LINUX_WORK.md` §4.7. MillWright #4 (merged): MES box on the machine panel with `[MW_MES] OUTBOX = 1`.
MillWright updated in `test_config` (backup 20261026-151754); .deb from 8436ef3 built.
**Checked in AXIS 26 Oct (17:08-17:12):** .deb installed by the user, agent restarted; `mes_status.json`
refreshed every 5 s; `--mes-status` and the MES box agreed at every step (queue 11 → 16 → 18, link
offline/amber, operator shown after login). ATC panel run of `LX052_drill_ATC.ngc` (`selected_tools`,
generated `atc_run_171042.ngc`): `execution.started` / `execution.ended` (aborted, line 130 of 158) and the
agent's `cycle.observed` (aborted, explicit, same `execution_id`) agree.

**MillWright bug found (fixed in millwright #6, merged 26 Oct as e12a7f2 by this session at the user's request):** a plain AXIS run of
LX052 stopped at line 44 of 634 kept `motion_line` = 44 at idle, so MillWright logged it `completed` (agent:
aborted), counted it as a job and dropped the resume line. New `run_end.py` (the agent's rule: motion_line at
idle at or past the last move before M2/M30, not the stale line of the previous run; old rule when the end
cannot be judged) used by `mw_audit`, `machine_panel` and `program_controller`. 520 tests pass.

**Fix checked in AXIS (26 Oct, 17:38-17:41):** MillWright reinstalled in `test_config` from e12a7f2
(backup 20261026-173657; also brings contract 1.2 from millwright #5). MillWright and the agent agree on all
three runs (same `execution_id`, explicit):

| Run | MillWright `execution.ended` | Agent `cycle.observed` |
|---|---|---|
| `SJ-942_Cutting1_2.NC` to M30, 99 s | completed, line 188 | completed |
| `LX052_drill_ATC.ngc` stopped at line 38, 7 s | **aborted**, line 38 | aborted |
| LX052 T4 from the Job tab (`run_from_here`, `atc_run_174032.ngc`), 20.6 s | completed | completed |

The LX052 stop line was kept until the Job-tab run completed the program and cleared it. Job counter not
checked: `test_config` has no `[MW_JOBS]` section (no counts since 16 Oct); it uses the same `run_end` check.

## Version 1.7.0 (27 Oct)

Agent #34 (merged, 759ac21): `AGENT_VERSION` 1.7.0 (user's choice), README, CHANGELOG `[1.7.0] - 2026-10-27`.
.deb `linuxcnc-status-agent_1.7.0_all.deb` built from it and installed by the user (12:02); events now carry
`version: 1.7.0`. Also merged: #33 (session log). ATC run of `LX052_drill_ATC.ngc` from the top (12:04-12:12,
466.8 s, line 633 of 634): MillWright `execution.ended` and the agent's `cycle.observed` both `completed`,
same `execution_id` (explicit), same operator session.

## H impact runs (27 Oct)

OFC_PC: Intel i3-4170 3.70 GHz, kernel 6.1.0-53-rt-amd64, LinuxCNC 2.9.10 (POSIX realtime), AXIS with
MillWright panels, `test_config` (parport stepgen; base 25 µs, servo 990 µs).

**Baseline** (LinuxCNC off, `latency-histogram --nox`, 10 min, agent running): base jitter max 39.9 µs, servo
42.3 µs (21 Oct: 76.0 / 65.4 µs).

**With LinuxCNC:** `LX052_drill_ATC.ngc` to the end each run; the user reset `servo-thread.tmax` and
`base-thread.tmax` before each run; the session stopped / started the agent and read the values afterwards
(CPU clocks; ≈ µs at 3.7 GHz):

| Run | Agent | `servo-thread.tmax` | `base-thread.tmax` | Run time |
|---|---|---|---|---|
| 1 | stopped | 810 916 (≈ 219 µs) | 211 764 (≈ 57 µs) | 327.0 s |
| 2 | running | 528 336 (≈ 143 µs) | 165 120 (≈ 45 µs) | 314.4 s |
| 3 | stopped | 669 696 (≈ 181 µs) | 264 716 (≈ 72 µs) | 432.0 s |
| 4 | running | 541 436 (≈ 146 µs) | 211 481 (≈ 57 µs) | 520.4 s |

All four completed (line 633), no following errors (`joint.*.f-errored` never true), agent CPU 0.5 %. The two
runs with the agent running have the lower servo maxima; the spread between runs is noise on this non-isolated
PC. **Pass:** no sign the agent affects the machine. Extra point: the first ATC run (agent running, tmax not
reset, from LinuxCNC start) had 995 924 / 272 608. Runs 2 and 4: `cycle.observed` explicit, same
`execution_id` as MillWright. While the agent was stopped MillWright kept its events; the agent took them
within seconds of starting (queue 38 → 42).

## Contract gaps found

- (Gap, by design) The agent's `clock.boot_id` is its session id (contract README example), MillWright's is the
  kernel boot id, so the service cannot order agent and MillWright events from one boot by `mono_ms`.

- (Closed) ATC pick/drop check mode had no contract `mode` value: contract 1.2 `atc_check` (agent #30,
  millwright #5); installed in `test_config` 26 Oct, not yet seen in a pick/drop check run.
- (Closed) `spool_full` reason: contract 1.1 (FloorWatch #39, agent #21, MillWright
  re-vendor 7fb82ff).

## Pending — needs the user

1. **ATC pick/drop check run** (`atc_tool_pick_drop_check.ngc`) in AXIS: check it gets `atc_check`
   execution events (contract 1.2).
2. **Two-PC J test:** the Windows PC's service address + a fresh code + OK to move the agent off
   the loopback test service. Then stop `~/fw-service-test`'s service and remove it.
3. **Package I:** say when the scanners are bought (plan §16 D1).
4. **Time sync:** OFC_PC has no NTP (`timedatectl`: not synchronised, NTP service n/a), so every event has
   `clock.synced: false`; `agent.log` has a line dated 2026-04-28 from before the clock was set at boot.
   Pilot PCs need time sync.
5. (Done 27 Oct) H impact runs; version 1.7.0.

## Pending — this session

- C recorded in `docs/FLEET_TEST.md` (branch `docs/fleet-gate-2026-10-26`, push needs the user's OK).
  Worktrees `~/fw-test/gate2/agent-src` and `fw-src` removed; logs kept in `~/fw-test/gate2/`.
- (Done) Agent #30 (contract 1.2) and #32 (fleet gate docs) merged; the installed .deb has contract 1.2.
- Link state label: `not_enrolled -> offline` is logged at start on an enrolled PC (initial label only).
- Leftover `mes_ack.json.tmp-<pid>` files in MillWright's `mw_data` (26 Oct 16:13, 27 Oct 03:53) from writes cut
  off by a stop; harmless, but the agent should remove its old temp files at start.
- The loopback test service (`~/fw-service-test`) stopped with the 26 Oct session; 42 events wait in the
  spool. Start it again to drain them, or wait for the two-PC test.
