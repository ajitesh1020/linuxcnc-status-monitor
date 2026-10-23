# MES plan, Linux side — session log

What the Claude Code session on the LinuxCNC PC has implemented for
`docs/MES_LINUX_WORK.md`, and what is still pending. Last updated 23 Oct 2026.

Repos: agent = `ajitesh1020/linuxcnc-status-monitor`; MillWright = `ajitesh1020/millwright`.
Nothing here is merged by this session. The user merges.

## Packages

| # | Package | Branch → base | PR | State |
|---|---|---|---|---|
| A | Stable machine identity (`machine_uuid`) | `feat/machine-identity` → `main` | agent #19 | **Merged** 22 Oct |
| B | `monitor_interface` | `feat/monitor-interface` → `main` | agent #20 | **Merged** 22 Oct |
| C | Fleet test exit gate (A–K + H3, L from A, on `main`) | — | — | Re-running on current `main` (see below) |
| D | MillWright outbox | `feat/mes-outbox` → `main` | millwright #1 | Open; contract 1.1 re-vendored locally (7fb82ff, not pushed); 467 tests pass; AXIS check pending |
| E | MillWright run context | `feat/mes-run-context` → `feat/mes-outbox` | millwright #2 | Open; #1's branch merged in locally (df4bb15, not pushed); 488 tests pass; AXIS check pending |
| F | Agent MES spool | `feat/mes-spool` | agent #22 | **Merged** 22 Oct |
| G | Agent ↔ MillWright link | `feat/mes-millwright-link` | agent #23 | **Merged** 22 Oct; AXIS check pending |
| H | `loop_lag_ms` | `feat/loop-lag` | agent #24 | **Merged** 22 Oct; impact runs pending |
| I | Scanner adapters | — | — | Blocked: scanners not bought (user, 22 Oct) |
| J | Enrolment + forwarder to the site service | `feat/mes-forwarder` → `main` | agent #26 | Open, ready; 212 tests pass |

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

Re-run (23 Oct, started 15:33) on current `main` of both repos: agent 8e02631,
FloorWatch c7c413b. Runner in `~/fw-test/gate2/` (`run_all.sh`, `scen.sh`;
started with `setsid nohup` so it survives the session): 13 scenarios, three at a
time, each in its own `unshare -rn` namespace (no sudo). 30 min busy + 180 s
settle (A: 60 min). H3 = FloorWatch restarted every ~40 s (`--hours 0.011`) for
the first 10 min. K sends the 5 MB and 32 MB programs (both should be accepted).
Results per scenario in `~/fw-test/gate2/<name>.log`; they go into
`docs/FLEET_TEST.md` when all are done.

| Scenario | Result |
|---|---|
| A–K, H3 | running |

## Contract gaps found

- ATC pick/drop check mode has no contract `mode` value: those runs get no
  execution events (millwright #2).
- (Closed) `spool_full` reason: contract 1.1 (FloorWatch #39, agent #21, MillWright
  re-vendor 7fb82ff).

## Pending — needs the user

1. **Push OK** for: MillWright
   `feat/mes-outbox` (re-vendor) and `feat/mes-run-context` (merge of #1); this
   log branch.
2. **Merge** millwright #1 → #2, and agent #26 (J).
3. **AXIS check of D + E + G together.** Say how MillWright is installed into
   `test_config`; then set `[MW_MES] OUTBOX = 1` and the agent's `mes_enabled` /
   `mes_millwright_config`, start AXIS and run a program. This session only reads the
   files and databases MillWright writes.
4. **J on the real agent** (needs your OK: config change + agent restart): run the
   site service on 127.0.0.1 (§6), `lcnc-status-agent --mes-enroll …`,
   `mes_enabled: true`, restart the agent, check `raw_events` in the ledger.
5. **H impact runs** (BENCH_TEST §6): latency test without LinuxCNC; then with a
   real program, `servo-thread.tmax` with the agent stopped vs running (user resets
   tmax with halcmd).
6. **Package I:** say when the scanners are bought (plan §16 D1).
7. **Agent version:** `AGENT_VERSION` in `status.py` is still 1.6.0 by the user's
   choice; the CHANGELOG says 1.7.0.

## Pending — this session

- Finish C, record it in `docs/FLEET_TEST.md`, open its PR (ask before pushing).
- Remove `~/fw-test/gate2/agent-src` and `fw-src` worktrees after C.
