# MES plan, Linux side — session log

What the Claude Code session on the LinuxCNC PC has implemented for
`docs/MES_LINUX_WORK.md`, and what is still pending. Last updated 22–23 Oct 2026.

Repos: agent = `ajitesh1020/linuxcnc-status-monitor`; MillWright = `ajitesh1020/millwright`.
Nothing here is merged by this session. The user merges.

## Packages

| # | Package | Branch → base | PR | State |
|---|---|---|---|---|
| A | Stable machine identity (`machine_uuid`) | `feat/machine-identity` → `main` | agent #19 | Open, ready |
| B | `monitor_interface` | `feat/monitor-interface` → `main` | agent #20 | Open, ready |
| C | Fleet test exit gate (A–L + H3 on `main`) | — | — | Running (see below) |
| D | MillWright outbox | `feat/mes-outbox` → `main` | millwright #1 | Open; 467 tests pass; AXIS check pending |
| E | MillWright run context | `feat/mes-run-context` → `feat/mes-outbox` | millwright #2 | Open; 488 tests pass; AXIS check pending |
| F | Agent MES spool | `feat/mes-spool` → `feat/machine-identity` | agent #22 | Open, ready |
| G | Agent ↔ MillWright link | `feat/mes-millwright-link` → `feat/mes-spool` | agent #23 | Open; 164 tests pass; AXIS check pending |
| H | `loop_lag_ms` | `feat/loop-lag` → `feat/mes-millwright-link` | agent #24 | Open; 170 tests pass; impact runs pending |
| I | Scanner adapters | — | — | Blocked: scanners not bought (user, 22 Oct) |
| J | Forwarder to the site service | — | — | Not started; agent PR #25 (docs) unblocks it once merged |

Merge order (stacked PRs): agent #19 → #22 → #23 → #24; #20 on its own (CHANGELOG
"Added" lines clash with #19: trivial rebase). MillWright #1 → #2.

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
Flag `[MW_MES] OUTBOX = 1`. Contract vendored from FloorWatch a8b9d42; installed
with the panels and in the .deb.

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

## C — fleet exit gate (in progress)

Both repos at `main` (agent 84e2402, FloorWatch 52addfc, which includes #33–#38).
99 simulated agents + FloorWatch, each scenario in its own private network
namespace (`unshare -rn`, no sudo, host networking untouched), three at a time.
30 min busy + 180 s settle (A: 60 min). Results go into `docs/FLEET_TEST.md`.

| Scenario | Result |
|---|---|
| A (also L) | 99 of 99 exact — parts 6979/6979, aborts 539/539, E-stops 199/199, cmd fail 87/87, joint 33/33; 0 gaps, 0 warnings |
| B loss 10 % | 99 of 99 exact — parts 3489/3489; 0 gaps, 0 warnings |
| C loss 30 % | 99 of 99 exact — parts 3505/3505; 0 gaps, 0 warnings |
| D, E, F, G, H1, H2, H3, I, J, K | running |

K now sends 5 MB and 32 MB programs: with F05 merged both should be accepted.

## Contract gaps found

- ATC pick/drop check mode has no contract `mode` value: those runs get no
  execution events (millwright #2).
- The spool had no `spool_full` reason: contract 1.1 adds it (FloorWatch #39,
  agent #21). Re-vendor into MillWright after #39 merges.

## Pending — needs the user

1. **Merge** the open PRs in the order above.
2. **AXIS check of D + E + G together.** Say how MillWright is installed into
   `test_config`; then set `[MW_MES] OUTBOX = 1` and the agent's `mes_enabled` /
   `mes_millwright_config`, start AXIS and run a program. This session only reads the
   files and databases MillWright writes.
3. **H impact runs** (BENCH_TEST §6): latency test without LinuxCNC; then with a
   real program, `servo-thread.tmax` with the agent stopped vs running (user resets
   tmax with halcmd).
4. **Package I:** say when the scanners are bought (plan §16 D1).
5. **Package J:** starts after agent PR #25 merges (it depends only on F).
6. **Agent version:** `AGENT_VERSION` in `status.py` is still 1.6.0 by the user's
   choice; the CHANGELOG says 1.7.0.

## Pending — this session

- Finish C, record it in `docs/FLEET_TEST.md`, open its PR (ask before pushing).
- Re-vendor contract 1.1 into MillWright once FloorWatch #39 merges.
- Remove the scratchpad worktrees when their branches are pushed.
