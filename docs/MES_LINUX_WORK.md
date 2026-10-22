# Instructions for Claude Code on the LinuxCNC PC — MES plan, Linux side

Written 22 Oct 2026 for a Claude Code session on the LinuxCNC test PC
(Dell OptiPlex 9020, LinuxCNC 2.9.10, AXIS, config `test_config`). A Windows
session is working on FloorWatch **at the same time**; its history does not come
with you. Everything you need is here and in the linked files. Section 4 fixes
the interfaces between the two sessions so the work can run in parallel without
waiting for each other.

## Prompt to paste

Start Claude Code in `~/linuxcnc-status-monitor` (after `git pull` on `main`) and
paste:

> Read docs/MES_LINUX_WORK.md and follow it. Work through its packages in
> order, one branch and one PR per package, in this repo and in ~/millwright.
> Keep to its interfaces (section 4) exactly — the Windows session builds the
> other side against them. Stop and ask me before anything it says needs my OK,
> and when a package is blocked.

---

## 1. Background

FloorWatch (Windows dashboard, repo `ajitesh1020/floorwatch`) receives status
packets from this agent (`ajitesh1020/linuxcnc-status-monitor`) on every
LinuxCNC PC. MillWright (`ajitesh1020/millwright`) is the operator GUI that runs
inside AXIS. The plan that drives all of this is
`docs/MES_IMPLEMENTATION_PLAN.md` in the FloorWatch repo — read its sections 2
(rules), 5 (Phase 0), 6 (Phase 1), 7 (Phase 2) and Appendix A before starting.
Get FloorWatch with `git clone https://github.com/ajitesh1020/floorwatch
~/floorwatch` and MillWright with `git clone
https://github.com/ajitesh1020/millwright ~/millwright` (or `git pull` where
they are already cloned; MillWright's installed copy inside the LinuxCNC config
is not a git checkout — ask me how it is installed before testing a change).

Done so far (all on `main` of their repos, or open PRs about to be merged):

| Item | Where |
|---|---|
| F01–F06 review fixes, `session_id` / `packet_seq`, `cmd_state` | agent + FloorWatch |
| Fleet simulator and LinuxCNC-PC fleet test | agent `tools/fleet_sim.py`, `docs/FLEET_TEST.md` |
| P0.4 journal recovery and logging, CI pinning (P0.8 agent part) | agent PR #15 |
| F07 site key: agent signs; FloorWatch checks | agent PR #15; FloorWatch PR #36 |
| P0.2 G-code 32 MiB / 80,000 chunks, `gcode_file_refused` | agent PR #17; FloorWatch PR #34 |
| P1.1–P1.3 MES event contract `fw.mes.event/1` | FloorWatch `contracts/mes/v1/` (PR #32); copy in this repo (PR #16) |
| P0.1, P0.3, P0.7, P0.9 | FloorWatch PRs #33, #38, #35, #37 |

The Windows session does next, in parallel with you: P0.4's FloorWatch display
of `journal_state`, P0.6's FloorWatch side (machines keyed by `machine_uuid`),
`docs/COMPATIBILITY.md`, then Phase 3 (the site service: API, ledger,
connectors).

## 2. Your packages, in order

Each is one branch (`feat/<name>` or `fix/<name>`) and one PR. Tests with every
one (`python3 -m unittest discover -s tests`). Update `CHANGELOG.md`
(*Unreleased*, agent 1.7.0), `PROTOCOL.md` and `config.example.yaml` /
`docs/CONFIGURATION.md` when packets or keys change.

| # | Package | Repo | Plan | Blocked by |
|---|---|---|---|---|
| A | Stable machine identity: `identity.json`, `machine_uuid` in every status packet and journal entry, `--check` shows it | agent | P0.6 | — |
| B | `monitor_interface` (send status only on that interface) + `NETWORK.md` "Restricted pilot network" | agent | P0.5 | — |
| C | Fleet test exit gate: scenarios A–L + H3 against `main` of both repos, K included; record in `docs/FLEET_TEST.md` | agent | Phase 0 gate | FloorWatch PRs #33–#38 merged |
| D | MillWright outbox (`mw_outbox`, same transaction as the audit row), vendored contract, `[MW_MES] OUTBOX` flag | millwright | P1.4 | — |
| E | MillWright run context frozen at start: `execution_id`, hashes off the Tk thread, `current_execution.json`, `execution.started` / `ended` | millwright | P1.5 | D |
| F | Agent MES spool `agent_mes_spool.py` (WAL, `synchronous=FULL`, budget, never prunes unacked) | agent | P2.4 | — |
| G | Agent ↔ MillWright: outbox reader → spool in one transaction with its cursor, `mes_ack.json`, `cycle.observed` with `execution_id` | agent | P1.6 | E, F |
| H | `loop_lag_ms` (p99) in status packets; agent-on / agent-off impact runs | agent | P2.7 | — |
| I | Scanner adapters HID / serial + enrolment CLI | agent | P2.2, P2.3, P2.8 | **scanners bought (plan §16 D1)** — ask me |
| J | Forwarder to the site service | agent | P2.5 | **Phase 3 API from Windows** — do not start |

If a package is blocked, say so and move to the next one. A and B are small;
do them first: the Windows session needs A's field to build its side.

## 3. Rules

**Safety — this PC runs LinuxCNC** (same as `docs/CLAUDE_CODE_ON_LINUXCNC_PC.md` §3).
- Never change LinuxCNC's configuration (INI, HAL), never write HAL pins, never
  send commands to LinuxCNC, never stop or restart it. The user runs programs.
- MillWright changes that need AXIS (D, E): the user starts AXIS with
  `test_config`; you test by reading the files and databases MillWright writes.
- No latency tests while LinuxCNC runs.

**Plan rules (plan §2) that bite here.**
- The agent and MillWright never command the machine (no MDI, no program load,
  no cycle start, no HAL write). MES data is advisory.
- Nothing slow in the agent's sampling loop: hashing, file scans, SQLite waits
  over 1 s and network calls go to bounded worker threads. The loop only reads
  `current_execution.json` (≤ 1 KB) at cycle start and enqueues with
  `put_nowait`.
- Durable before acknowledged; at-least-once delivery with unique `event_id`s;
  no invented data (unknown operator stays `null`, not a guess).
- The agent stays **standard-library only** and runs on Python 3.7+
  (`packaging/deb/control.in`). No `python-evdev`, no `pyserial`.

**The MES contract.** `contracts/mes/v1/` here and in MillWright is a copy of
FloorWatch's. **Never edit a copy.** Build every event with the contract's
`validate.py` (`validate_event()` before storing, `encode()` for the bytes you
store and resend). If the contract is missing something, stop and write down
what and why (in the PR description); the Windows session changes FloorWatch's
copy and you re-vendor with
`python3 ~/floorwatch/tools/vendor_mes_contract.py <repo>` (on a clean
FloorWatch `main`).

**Git.** One branch and PR per package; ask me before each push. Never push to
`main`, never merge PRs, never force-push. Keep the agent's version at 1.7.0
unless I say otherwise.

## 4. Interfaces fixed for both sessions

The Windows session codes against exactly these. Change one only after asking
me, and say so in the PR title.

### 4.1 Machine identity (package A)

- File `~/linuxcnc-monitor-agent/identity.json` (path overridable with config
  key `machine_uuid_file`), mode 0600, written once atomically (temp + rename):

  ```json
  {"machine_uuid": "8d7c6b5a-4f3e-4d2c-8b1a-0f9e8d7c6b5a", "created_at": "2026-10-22T09:15:02Z"}
  ```

- `machine_uuid`: UUID version 4, lowercase, with hyphens (`str(uuid.uuid4())`) —
  the same form as the contract's ids; it **is** the MES `machine_id`.
- Created on first start when the file is missing. If the file exists but is
  unreadable or damaged: keep it as `identity.json.bad-<UTC time>`, log a
  warning, create a new one. Never regenerate otherwise; package install,
  upgrade, remove and purge never touch it.
- Status packets: new field `machine_uuid` (string) on every `status` packet;
  history journal entries carry it too (as `session_id` is). PROTOCOL.md §4.1b,
  "Agent ≥ 1.7.0", additive, no `proto` bump. `machine_name` stays the display
  name.
- `--check` prints it. FloorWatch will key a machine by `machine_uuid` when
  present (falling back to the name for older agents), so two PCs with the same
  name stay two machines.

### 4.2 Network (package B)

- Config `monitor_interface: ""` (default = today: every interface /
  `monitor_pc_ip`). When set (e.g. `enp2s0`), status packets go out only on that
  interface (`SO_BINDTODEVICE` needs root; use the interface's IPv4 address as
  the source and its broadcast address instead). Never the Mesa / control NIC.
  `--check` prints the interface, its address and broadcast address.

### 4.3 MillWright → agent files (packages D, E, G)

All in `<LinuxCNC config dir>/mw_data/` (the agent finds it through config key
`mes_millwright_config`), written atomically (temp + `os.replace`):

- `current_execution.json` — written by MillWright at run start, removed (or
  `"ended_at"` set) at run end:

  ```json
  {"execution_id": "<uuid v4>", "started_at": "2026-10-22T09:08:10.000Z",
   "program_path": "/home/cnc/linuxcnc/nc_files/bracket_v3.ngc",
   "mode": "normal", "station_id": "<uuid v4>", "operator_session_id": "<uuid v4 or null>"}
  ```

  `mode` uses the contract's `execution.started` values. The agent treats a run
  as the same one when `program_path` matches and `started_at` is at most 3 s
  before the agent's own cycle start (plan P1.6); otherwise it makes its own
  `execution_id` and `correlation_method: "none"`.
- `millwright.db` table `mw_outbox` exactly as plan P1.4 (`seq`, `event_id`,
  `type`, `occurred_at`, `body` = the encoded envelope, `audit_id`), with
  `state` rows `station_id` and `outbox_epoch`. MillWright's envelopes:
  `producer.kind = "millwright"`, `producer.id` = a MillWright producer uuid kept
  in `state`, `producer.epoch` = `outbox_epoch`, `site_id = null`,
  `machine_id = null`, `station_id` set. The agent **never writes**
  `millwright.db`.
- `mes_ack.json` — written by the agent: `{"epoch": "<outbox_epoch>", "seq": <last
  seq stored in the spool>}`. MillWright prunes outbox rows older than 30 days
  **and** at or below it.

### 4.4 Spool (packages F, G)

- `~/linuxcnc-monitor-agent/mes_spool.db`, schema exactly as plan P2.4 (`meta`,
  `spool`, `cursors`). `meta` holds `producer_id`, `epoch`, `machine_id`,
  `site_id` (null until enrolment). A new `epoch` whenever the file is created
  again or restored.
- Rows store `validate.encode(event)` bytes in `body`; the forwarder later sends
  exactly those bytes. Events from MillWright's outbox are stored unchanged
  (the service fills `site_id` / `machine_id` from the forwarding agent, contract
  README rule 7).
- Config keys as plan Appendix A (`mes_enabled`, `mes_spool_mb`,
  `mes_millwright_config`). `mes_enabled: false` (default) = nothing MES runs.
- Status packets gain `mes_spool` = `{"pending": n, "bytes": n, "state":
  "ok" | "full" | "off"}` once F lands, and `features` = `["mes1"]` when the
  spool is on (plan P1.7). Both additive.

### 4.5 `loop_lag_ms` (package H)

Status packet field `loop_lag_ms` = `{"p50": n, "p99": n, "max": n}` over the
last 60 s of sampling-loop delays (ms, integers). Additive.

## 5. Tests that must exist

- A: identity created once and reused; damaged file kept and replaced; field in
  status packets and journal entries; 0600.
- D–G: crash at every step (kill the writer between audit and outbox — the
  transaction makes it impossible; kill the reader between spool append and
  cursor advance — nothing lost or doubled); epoch change after a re-created
  database; pruning never passes `mes_ack.json`; every event passes the
  contract's `validate_event()`.
- F: spool survives `kill -9` mid-write; budget full → new scans would be
  rejected (`scan.rejected_local`, reason `spool_full` — tell me: the contract's
  reason list has no `spool_full` yet, the Windows session must add it first).
- H: the loop never waits on the MES threads (a stalled spool does not raise
  `loop_lag_ms`).

## 6. When a package is done

Report: what changed, test results (counts), what the user must do (restart the
agent service, start AXIS for a MillWright check), anything that changes an
interface in section 4, and the PR link once pushed. Then start the next one.
