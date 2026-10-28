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
| P0.6 FloorWatch side (machines keyed by `machine_uuid`), P1.7 "MES-capable agent" display | FloorWatch PRs #40, #41 |
| Contract 1.1: `scan.rejected_local` reason `spool_full` | FloorWatch PR #39; copy in this repo (PR #21) |
| Contract 1.2: `execution.started` mode `atc_check` (ATC pick/drop check runs) | FloorWatch PR #48; copies: this repo and MillWright (re-vendor PRs) |
| Site service as a Windows service, single-instance lock, backup / restore (P3.8) | FloorWatch PR #46; FloorWatch `docs/MES_SERVICE_OPERATIONS.md` |
| Site service: ledger, ingestion and receipts; private CA, enrolment, HTTPS with client certificates (P3.1–P3.3) | FloorWatch PRs #42, #43; API: FloorWatch `docs/MES_SERVICE_API.md` |

The Windows session does next, in parallel with you: the rest of Phase 3 (the
service as a Windows service, users and roles, connectors), P0.4's FloorWatch
display of `journal_state` and `docs/COMPATIBILITY.md`.

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
| I | Scanner adapters HID / serial + scanner enrolment CLI | agent | P2.2, P2.3, P2.8 | **scanners bought (plan §16 D1)** — ask me |
| J | MES enrolment (`--mes-enroll`) + forwarder from the spool to the site service (§4.6, §6) | agent | P2.5 | F. The service API is ready: FloorWatch `docs/MES_SERVICE_API.md` |
| K | The agent follows the site service to a new address: UDP 5007 probe / announcements, switch only after the normal TLS check, `mes/service_address.json` (§4.8) | agent | — | J. Service side done: FloorWatch `docs/MES_SERVICE_DISCOVERY.md` (#51) |

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
   "mode": "normal", "station_id": "<uuid v4>", "operator_session_id": "<uuid v4 or null>",
   "run_file": "/home/cnc/linuxcnc/configs/test_config/mw_gen/atc_run_1.ngc"}
  ```

  `mode` uses the contract's `execution.started` values. `run_file` (added
  23 Oct 2026, agreed; optional) is the file LinuxCNC actually runs — the
  generated file for Job-tab runs, else equal to `program_path`. The agent
  treats a run as the same one when `program_path` or `run_file` is the file
  LinuxCNC runs and `started_at` is at most 3 s before (or, for poll jitter, up
  to 1 s after) the agent's own cycle start (plan P1.6); otherwise it makes its
  own `execution_id` and `correlation_method: "none"`. When the agent's first
  running sample is already inside a subroutine (an M6 remap; MillWright opens
  and starts a generated file at once, so LinuxCNC shows it for ~30 ms only),
  the agent does not know its program: it matches on the time window alone,
  takes `run_file` as the program and sends `correlation_method:
  "time_window"` (27 Oct 2026). MillWright knows the file from
  `mw_data/run_request.json`, which its program panel writes just before it
  starts a file.
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

### 4.6 Site service link (package J)

The service side is done; its API is FloorWatch `docs/MES_SERVICE_API.md`
(enrolment, `GET /v1/capabilities`, `POST /v1/events`, receipts, errors).
Follow it exactly. Agent side:

- **Enrolment CLI:** `lcnc-status-agent --mes-enroll https://<service>:8443
  <code>` does the four steps of the API page: fetch the CA, **check its
  fingerprint against the code before sending anything secret**, make the key and
  CSR with the `openssl` command (add `openssl` to the `.deb` `Depends`), send the
  code + CSR + `machine_uuid` over a connection that verifies the service with
  that CA. Files in `~/linuxcnc-monitor-agent/mes/` (directory 0700, files 0600):
  `client.key`, `client.crt`, `ca.crt`, `enrolment.json`
  (`{"service_url", "site_id", "machine_id", "not_after"}`). It writes
  `mes_service_url` into `config.yaml` only with the user's OK. `--check` shows
  the service URL, site id and certificate expiry (never the key).
- **Re-enrolment with another site** (found in the two-PC test, 27 Oct 2026):
  events already in the spool carry the old `site_id`, and the new service
  rejects them as `site_mismatch`, which is final. So after the CA check and
  before the code is sent, enrolment asks `GET /v1/health` (over TLS trusting
  that CA) for the new site. If pending events stamped with another site wait
  (MillWright's have `site_id` null and are not affected), it **refuses**,
  prints the count and the two ways on, and the code stays valid:
  drain them (run the old service until `--mes-status` shows the queue empty)
  or enrol with `--discard-waiting`, which marks them `dropped` with a reason
  (never sent; pruned after 7 days like acked rows). A service that does not
  say its site is treated the same way. Waiting events are not re-stamped with
  the new site: they happened under the old enrolment.
- **Spool:** after enrolment `meta.site_id` is set (§4.4).
- **Forwarder** (`agent_mes_forward.py`, own thread, never in the sampling
  loop): `http.client.HTTPSConnection` with an `ssl` context holding
  `client.crt` / `client.key` and trusting only `ca.crt` (host name checked; if
  the service's certificate lacks the name used, the admin adds it with
  `floorwatch-service server-cert`). `GET /v1/capabilities` at start and every
  hour; forward only listed types, oldest first, within its limits (≤ 200
  events, ≤ 256 KiB before gzip); mark rows acknowledged per receipt; 503 /
  timeouts / broken connections → resend later, backoff 1 s → 5 min with
  jitter. The feedback endpoint (`GET /v1/agents/{id}/feedback`, plan P2.5) does
  not exist yet: skip it.
- **Status field** `mes_link` = `{"state": "ok" | "offline" | "not_enrolled" |
  "cert_expired" | "rejected", "last_ok_ms": n}` (`last_ok_ms` = last 200 from the
  service, absent if never). Additive; PROTOCOL.md §4.9. FloorWatch will show it
  next to "MES: ready".
- Keys: `mes_service_url` (default `""` = not enrolled). `mes_enabled: false`
  means no forwarding either.

### 4.7 Operator readout (P2.6)

Added 26 Oct 2026 by the Linux session. The agent writes
`~/linuxcnc-monitor-agent/mes_status.json` when `mes_enabled` is on (removed
at start when it is off). Display only: nothing decides anything from it.

```
{"schema": "lcnc-agent.mes_status/1",
 "updated_at_ms": 1793000000000,          wall clock of the write
 "running": true,                         false = the agent stopped cleanly
 "machine_uuid": "...",
 "operator": {"name", "role", "assurance", "operator_session_id", "since"} | null,
 "queue": {"pending": n, "bytes": n, "state": "ok" | "full"},
 "link": {"state": "ok" | "offline" | "not_enrolled" | "cert_expired" | "rejected",
          "last_ok_ms": n},               last_ok_ms absent if never
 "scan": null | {"code", "status": "received" | "pending" | "bound" | "rejected",
                 "reason", "at_ms"},      null until scanners (P2.1-P2.3)
 "bound": []}                             items bound to this machine (P2.1-P2.3)
```

- Written by its own thread, atomically (temp file + `os.replace`), at most
  twice a second and only on a change, but at least every 5 s. A reader treats
  a file older than **15 s** as "agent not running", and a missing or
  unparsable file as "unknown" (never an error dialog).
- `operator` comes from MillWright's `operator.session_started` / `_ended`
  events (latest in the outbox at agent start, then as they are copied).
- Colours, the same in MillWright's strip and `lcnc-status-agent --mes-status`:
  scan grey = received, amber = pending, green = bound, red = rejected; queue
  red = full (new scans refused); link green = ok, amber = offline, grey =
  not_enrolled, red = cert_expired / rejected.
- MillWright reads it from the path in `[MW_MES] STATUS_FILE` (default
  `~/linuxcnc-monitor-agent/mes_status.json`) and shows the strip only when
  `[MW_MES] OUTBOX = 1`.

### 4.8 Finding the service at a new address (package K)

Interface: FloorWatch `docs/MES_SERVICE_DISCOVERY.md` (section 2). The service
announces itself on UDP 5007 every 5 s and answers probes; its certificate
follows the PC's addresses. Agent side (`agent_mes_discovery.py`, used by the
forwarder thread only):

- **When:** after **3 connection failures in a row** (timeout, refused,
  unreachable, reset, or a service certificate that does not verify, e.g. one
  not naming the address). Any HTTP answer resets the count; `401` / `403` never
  lead to a look. Repeated on every failed round, so with the forwarder's
  backoff (1 s → 5 min).
- **Looking:** one probe `{"type": "fw.service_probe", "v": 1, "site_id"}` per
  interface the status packets use (`monitor_interface` respected, Mesa and
  `exclude_interfaces` skipped), to that interface's broadcast address from
  its own address; answers collected for up to 3 s (0.3 s after the first).
  Meanwhile announcements on UDP 5007 are accepted too (`SO_REUSEADDR` /
  `SO_REUSEPORT`). That socket is open only while looking: a service on the
  same PC binds 5007 without sharing and must never find it taken (then the
  agent uses probe answers only).
- **Accept:** only `fw.service_beacon` v1 whose `site_id` is the enrolled site
  and whose `ca_sha256` is SHA-256 of the pinned `mes/ca.crt`. The address is
  the datagram's **source**. Probe answers are tried before announcements.
- **Switch:** `https://<source>:<port>` with the normal TLS check (pinned CA,
  certificate naming that address, client certificate) and `GET
  /v1/capabilities` = 200. Then one log line `MES service moved: A -> B
  (probe|announcement)`; the link stays `offline` until the next normal round
  succeeds. A candidate that fails is logged once per address and reason.
- **Remember:** `mes/service_address.json` (0600, atomic) `{"url",
  "found_at_ms", "via", "configured_url"}`. `configured_url` (an addition to the
  design's keys) is the `mes_service_url` it replaces: the file is ignored once
  `mes_service_url` is changed by hand, and `--mes-enroll` deletes it.
  `config.yaml` is never written. `--check`: `site service  : https://B (found
  on the network by probe on <date>; configured https://A), site …`.
- Key: `mes_discovery_port` (default `5007`, `0` = never look), the service's
  `discovery_port`.

## 5. Tests that must exist

- A: identity created once and reused; damaged file kept and replaced; field in
  status packets and journal entries; 0600.
- D–G: crash at every step (kill the writer between audit and outbox — the
  transaction makes it impossible; kill the reader between spool append and
  cursor advance — nothing lost or doubled); epoch change after a re-created
  database; pruning never passes `mes_ack.json`; every event passes the
  contract's `validate_event()`.
- F: spool survives `kill -9` mid-write; budget full → new scans are rejected
  (`scan.rejected_local`, reason `spool_full`: in contract 1.1, FloorWatch
  PR #39; this repo's copy is updated by PR #21).
- J, against a fake server (unit tests) **and** against the real FloorWatch
  service (§6; skip the test when `~/floorwatch` is not there):
  - enrolment stops, **without sending the code**, when the CA's fingerprint
    does not start with the code's last 16 hex digits;
  - every receipt status marks the row acknowledged; 503, a timeout or a broken
    connection mark nothing and the batch is resent with backoff;
  - `kill -9` after the service committed but before the agent marked the rows:
    after restart the resend answers `duplicate`, nothing is lost or doubled;
  - only event types listed by `GET /v1/capabilities` are sent; others stay
    pending;
  - a revoked agent (`floorwatch-service revoke`) stops forwarding and shows
    `mes_link.state = "rejected"`; an expired certificate shows `cert_expired`;
  - batches never exceed the limits `capabilities` returns.
- K (`tests/test_mes_discovery.py`): only beacons of this site and CA count;
  no look before 3 failures, none after `401` / `403`, an HTTP answer resets
  the count; a candidate failing TLS or not answering 200 is not used; the found
  address is used after a restart and dropped when `mes_service_url` changes or
  the machine enrols again; the real FloorWatch service moved to another port
  is found by probe and nothing is lost.
- H: the loop never waits on the MES threads (a stalled spool does not raise
  `loop_lag_ms`).

## 6. Running the site service on this PC for testing (package J)

The real service is FloorWatch's `floorwatch_service`. Run it **on the
loopback only**, in a test folder, and stop it when done. It needs
`cryptography` 42 or newer (Debian 12's `python3-cryptography` is older), so use
a virtual environment; nothing is installed system-wide.

```
cd ~/floorwatch && git pull          # until FloorWatch #42 / #43 are merged: git checkout feat/service-tls
python3 -m venv ~/fw-svc-venv
~/fw-svc-venv/bin/pip install "cryptography>=42"
export FWS="env PYTHONPATH=$HOME/floorwatch/src $HOME/fw-svc-venv/bin/python -m floorwatch_service --data $HOME/fw-service-test"
$FWS init --name 127.0.0.1 --name localhost
$FWS enroll-code --note test-agent        # prints the one-time code
$FWS run --bind 127.0.0.1 --port 8443     # in its own terminal; Ctrl+C stops it
```

Then enrol the agent with `--mes-enroll https://127.0.0.1:8443 <code>` and set
`mes_enabled: true` (config changes and the service restart need my OK). Look at
what arrived:

```
sqlite3 ~/fw-service-test/ledger.db "SELECT type, count(*) FROM raw_events GROUP BY type"
sqlite3 ~/fw-service-test/ledger.db "SELECT reason, count(*) FROM quarantine GROUP BY reason"
```

`$FWS revoke <machine_uuid>` tests the revoked path. When finished: stop the
service, put the agent config back, delete `~/fw-service-test` (and the venv if
you no longer need it). Never bind the test service to `0.0.0.0` or a real
interface without asking me.

## 7. When a package is done

Report: what changed, test results (counts), what the user must do (restart the
agent service, start AXIS for a MillWright check), anything that changes an
interface in section 4, and the PR link once pushed. Then start the next one.
