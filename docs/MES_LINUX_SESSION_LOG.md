# MES plan, Linux side — session log

What the Claude Code session on the LinuxCNC PC has implemented for
`docs/MES_LINUX_WORK.md`, and what is still pending. Last updated 28 Oct 2026 (two-PC J test passed; agent `G28` fix merged and checked on the machine).

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
| J | Enrolment + forwarder to the site service | `feat/mes-forwarder` → `main` | agent #26 | **Merged** (by the user, by 26 Oct); on the real agent 26 Oct; two-PC test **passed** 28 Oct (see below) |

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

## Two-PC J test (27-28 Oct)

**Passed 28 Oct.** FloorWatch site service as a Windows service on the Windows PC: `https://192.168.0.104:8443`
(was .105 on 27 Oct), site `cb6a4a6c-7625-4042-8f42-b4f28b7bb75d`, CA fingerprint `add2f98bb0fdbd9d…`, FloorWatch
`main` with floorwatch #49 (service install permissions) and contract 1.2. OFC_PC 192.168.0.120, same /24.

1. **Waiting events (27 Oct).** The queue held 92 agent events stamped with the loopback test site `7844c24c…`
   (would be `site_mismatch`, final, at the new service). User's choice: drain rather than let them be rejected.
   `~/fw-service-test` started once (17:43, 127.0.0.1 only): queue 92 → 0 in ~2 min, then stopped and removed.
   During the drain one `execution.started` (mode `atc_check`) was **rejected** (`enum at payload/mode`): that
   service was FloorWatch a3399b6, contract 1.1, which has no `atc_check`; the event is lost. On 28 Oct one
   event waited: a MillWright `operator.session_ended` (shutdown, 27 Oct) with `site_id: null`, which the
   service accepts (`ingest.py` checks the site only when it is set), so no second drain.
2. **Network.** 27 Oct: ping and TCP 8443 timed out (Windows network profile / firewall). Fixed by the user;
   28 Oct `curl -k https://192.168.0.104:8443/v1/health` returns the site above.
3. **Enrolment (28 Oct 10:02).** Run by the user in a terminal (first attempt did nothing: no new files, agent
   not restarted; second attempt OK). `--mes-enroll` wrote `mes/` (ca, client cert + key, `enrolment.json`)
   and, with "y", `mes_service_url`; agent restarted 10:02:25. `--check`: service `https://192.168.0.104:8443`,
   site `cb6a4a6c…`, certificate valid until 2027-10-28. `--mes-status`: link **ok**, queue 0. Spool `site_id`
   now `cb6a4a6c…`; the waiting MillWright event acked 10:02. Windows `floorwatch-service status`: ledger
   **1 event, 1 agent enrolled**.
4. **AXIS runs (10:05-10:10).** 7 events sent and acked within ~2 s each, none rejected; Windows ledger
   1 → **8 events**, 1 agent enrolled.

   | Run | MillWright | Agent `cycle.observed` |
   |---|---|---|
   | `SJ-942_Cutting1_2.NC`, 10:05:10-10:06:49 | normal, completed, line 188 | **aborted** (99 %), explicit |
   | Pick/Drop Check `atc_pickdrop_100705.ngc`, 10:07-10:10 | `atc_check`, completed, no job | completed, `time_window` |

   `atc_check` accepted by the contract 1.2 service.
5. **Outage (10:15-10:18).** `floorwatch-service stop` during an SJ-942 run: agent log 10:16:17 "not reachable
   (timed out)", link `ok -> offline`; the run went on; 5 events waited (logout, login, `execution.started`,
   `cycle.observed`, `execution.ended`). After `floorwatch-service start` all 5 acked at 10:18:41 in one go,
   queue 0, link ok, none rejected. (The Windows status pasted afterwards still said 8 events: taken just
   before 10:18:41; expected 13.)

**Agent bug found: a bare `G28` is not counted as a move.** SJ-942 ends `G00 Z20` (line 183), `G00 Z20` (185, no
motion: already at Z20, so LinuxCNC never shows it), `M5`, `G28` (188), `M30` (189). The agent took 185 as the
last move; since agent #38 it also drops motion lines that are not moves of the program, so 188 was ignored and
the last move kept was 183 → aborted at 99 % (both SJ-942 runs on 28 Oct; MillWright: completed). On 26 Oct,
before #38, 188 still counted. Fix: `G28` / `G30` count as moves (`G28.1` / `G30.1` still do not); branch
`fix/g28-is-a-move` (f77cd15; 246 tests pass, 5 skip; new scan test and two end-to-end tests): agent #41, merged; checked on the machine below.

**`G28` fix on the machine (agent #41, merged 28 Oct by this session at the user's request; .deb 1.7.0 from
a52d585 installed by the user 10:28).** SJ-942 to M30 four times; MillWright and agent agree every time (both
`completed`, line 188, `explicit`), all events acked by the Windows service:

| Run | Condition | Result |
|---|---|---|
| 10:37:50-10:39:28 | not homed | completed / completed |
| 10:42:12-10:43:51 | not homed | completed / completed |
| 10:44:54-10:46:44 | Pause at line 91 (10:45:38), Resume 10:45:49 | completed / completed |
| 10:51:33-10:53:11 | E-stop, reset, machine on, then run | completed / completed |

**One stall, not reproduced (10:29:33-10:31:46).** First run after the .deb install and a LinuxCNC restart
(E-stop reset / machine on 10:29:13), not homed (`NO_FORCE_HOMING = 1`), paused 10:30:23-10:30:26 at line 102.
Motion finished line 183 at 10:31:14; then `exec_state` 5 (`WAITING_FOR_IO`) for 32 s, no axis moved (position
= end of line 183), until the user pressed Stop. Both logged it `aborted`, line 183 (correct for a Stop). In the
four runs above, after line 183 the spindle goes off and the `G28` starts within 20 ms (`exec_state` 7 → 3).
Not the cause: homing, Pause/Resume, first run after machine on. The `M5` / end of program go through the
I/O side; what it waited on is not known (no trace of that run, no AXIS message noted). Traces (read-only, 50 Hz,
`g28_trace.py`) in `~/fw-test/traces-2026-10-28/`. If it happens again: note any AXIS message and run a trace.

**Found, not fixed yet: re-enrolling with a different site while the agent's own events wait loses them.**
Waiting events carry the old `site_id`; the new service rejects them as `site_mismatch`, which is final (the
spool marks them `acked` like any receipt and prunes them later; only a warning in `agent.log` remains). MillWright events are not affected (`site_id` null). This test
avoided it only by draining by hand first. Options:
- *Warn only:* too easy to miss in a terminal; the events are still lost.
- *Re-stamp the waiting events with the new site:* not proposed. They happened under the old site's enrolment;
  changing their site misattributes them, and the event body is what the producer wrote.
- *Refuse (proposed):* `--mes-enroll` checks the spool before sending the code. If agent events are waiting and
  the new site differs from the spool's `site_id`, it stops, prints the count and the two ways on: drain (start
  the old service until `--mes-status` shows queue 0) or `--discard-waiting`, which marks them with a new spool
  state `dropped` and a reason (kept for the record, not sent). Same site, or nothing waiting: enrol as now. The
  code is not used when it refuses.

## Contract gaps found

- (Seen 27 Oct) A contract 1.1 service rejects contract 1.2 `atc_check` as `enum at payload/mode`, final. The
  site service must be on the contract version of its producers before they send.
- (Gap, by design) The agent's `clock.boot_id` is its session id (contract README example), MillWright's is the
  kernel boot id, so the service cannot order agent and MillWright events from one boot by `mono_ms`.

- (Closed) ATC pick/drop check mode had no contract `mode` value: contract 1.2 `atc_check` (agent #30,
  millwright #5); installed in `test_config` 26 Oct, not yet seen in a pick/drop check run.
- (Closed) `spool_full` reason: contract 1.1 (FloorWatch #39, agent #21, MillWright
  re-vendor 7fb82ff).

## Pending — needs the user

1. (Done 27-28 Oct) ATC pick/drop check run again: `atc_check` logged (see below and the two-PC test).
   Still open: a trace of a Stop after the last M6 (15:48 case below).
2. (Done 28 Oct) Two-PC J test; `G28` fix (agent #41) installed and checked. Watch for the
   `WAITING_FOR_IO` stall at the end of a run (seen once, 10:29); if it happens, note the AXIS message.
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
- (Done, agent #36; checked 27 Oct 13:33: both `mes_ack.json.tmp-*` removed) Leftover temp files of cut-off
  atomic writes are removed at start (dead pid only).
- **Pick/drop check in AXIS (27 Oct 13:31-13:34)**, MillWright program panel button, Repeat 1,
  `atc_pickdrop_133115.ngc`, completed (line 110). Found:
  1. MillWright logged `mode: normal` (not `atc_check`) and counted a job (`job_events` +1):
     `_program_of` / `run_finished` did not know the generator's header `(auto-generated tool pick/drop check`;
     the test faked the Job-tab header. **Fixed** in millwright `fix/pickdrop-atc-check` (f89a78f, 521 tests).
  2. Agent tracked the run as `rack_change.ngc` (the M6 remap: `stat.file` names it, `motion_line` counts its
     lines), as Run From Here, `cycle.observed` correlation `none`. **Fixed** in agent
     `fix/subroutine-program-file`: files in `[RS274NGC] SUBROUTINE_PATH` / `USER_M_PATH` or at
     `call_level` > 0 keep the program loaded before; their line numbers are ignored.
  3. The .deb install restarted the agent during the run (postinst, then the user's restart): the stop counted
     an abort and wrote no MES record. **Fixed** in the same agent branch (user's choice: unknown): nothing
     counted, `cycle.observed` `outcome: unknown`.
  Agent #37 and millwright #7 merged; MillWright reinstalled in `test_config` and .deb installed by the user.
- **Pick/drop check again (27 Oct 14:30-14:33)**, `atc_pickdrop_143012.ngc` (28 lines, first move line 26):
  - MillWright: `execution.started` mode **`atc_check`**, no job counted (`job_events` unchanged).
  - Agent: `axis.ngc` was loaded; MillWright opened the program and started it at once, so the agent first
    saw the run inside `rack_change.ngc` and kept `axis.ngc` (`cycle.observed`: `axis.ngc`, `none`). After
    the last M6 LinuxCNC was back in the program while motion ran the remap's last moves (`motion_line` 98,
    103, 110): Run From Here at 100 %.
- **Status traces (27 Oct, read-only script, 50-100 samples/s; user ran the check 3 times):**
  - 14:48 and 14:52 (both completed): MillWright `normal` + **aborted** both times (its first poll, every
    200 ms, saw `rack_change.ngc`; the 14:30 `atc_check` was luck). Agent: completed, but `none` /
    `axis.ngc`.
  - The program file shows for **~30 ms** at the start (then `call_level` 1, `rack_change.ngc`); inside the
    remap `motion_line`, `current_line` **and** `read_line` count its lines; after the last M6 LinuxCNC is
    back in the program with **`read_line` 27 = its M30** while motion runs the remap's 98 / 103 / 110; idle
    at 110. Line 26 (`G53 G0 Z76`, already at Z76) never shows.
  - No trace of a Stop yet: the rule for it below is inferred (a remap stops read-ahead).
- **Fixes (all four agreed with the user):**
  1. MillWright: the program panel writes `mw_data/run_request.json` just before it starts a file; the
     machine tracker takes it (once, ≤ 15 s old) when its first running poll is inside a macro.
  2. MillWright `run_end`: macro lines are not program lines (also after LinuxCNC is back); a run whose last
     motion line is a macro's completed only if `read_line` reached M2/M30 in the program file. Programs
     without a macro keep the old rule. `last_line` / resume line only program lines; job count and run time
     use the file that ran. Branch `fix/remap-run-start-end` (accc467), 531 tests.
  3. Agent: the same end rule (`CycleLineTracker`: remap lines, `read_end`).
  4. Agent: a run that starts inside a subroutine is matched to MillWright's by start time only
     (`time_window`) and takes its `run_file`. Branch `fix/remap-start-and-lines`, 248 tests.
  Agent #38 and millwright #8 merged; both installed by the user.
- **Pick/drop check with the fixes (27 Oct 15:19 and 15:24)**, both run to the end (the second was meant to
  be stopped during T9 but ran to M30: T9 picked 15:27:58, idle 15:28:03 at line 110):

  | Run | MillWright | Agent |
  |---|---|---|
  | 15:19 | `atc_check`, completed, `last_line` none, no job | completed, `explicit`, `atc_pickdrop_151944.ngc` |
  | 15:24 | `atc_check`, completed, `last_line` **110**, no job | completed, `time_window`, `atc_pickdrop_152426.ngc` |

  Two leftovers, fixed: (1) MillWright `last_line` 110 at 15:24: the first running poll still showed the
  previous run's 110 before the first macro and it was never replaced (millwright `fix/run-line-stale`,
  dd1aa5e, 531 tests). (2) Agent packets showed `axis.ngc` for the whole 15:24 run although the MES record
  was right: a time-window match now also sets the program (agent `fix/time-window-program`, 248 tests).
  Still to do: a run with **Stop** during T9 (the abort rule is not yet seen on the machine).
  Agent #39 and millwright #9 merged; MillWright reinstalled in `test_config` 15:42 (backup 20261027-154226,
  `OUTBOX = 1` kept); .deb installed by the user.
- **Pick/drop check after #39 / #9 (27 Oct):**

  | Run | Stop | MillWright | Agent |
  |---|---|---|---|
  | 15:44 | none | completed, no job | completed, `time_window`, `atc_pickdrop_154426.ngc` in packets |
  | 15:48 | after T9, last ~2 s | **aborted**, no job | **completed** (part counted), `explicit` |
  | 16:01 | during T8 | aborted, no job | aborted, `explicit`, abort #1, no part |

  **Pass** for a Stop during a tool change (16:01, traced): the Stop hit inside `rack_change.ngc`
  (`call_level` 1, `read_line` 29); LinuxCNC then showed the program at `motion_line` 0, `read_line` 0, then
  `axis.ngc`. Both said aborted.
  **Open (edge case):** a Stop after the last tool change, during the program's own `G4 P2` / `G53 G0 Z76`
  before M30 (15:48). After the last M6 LinuxCNC has already read M30 (`read_line` 27) and keeps showing the
  remap's line 110 through the dwell and the Z move, the same as a normal end, so the agent says completed.
  MillWright said aborted; why it differs is not known yet (no trace of that run). Needs a trace of a Stop in
  those last seconds before a fix is chosen.
- (Done 27 Oct) The loopback test service's waiting events drained; `~/fw-service-test` removed.
