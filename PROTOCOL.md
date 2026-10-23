# PROTOCOL.md — LinuxCNC Status Monitor Wire Protocol

**Protocol version: 1**

This is the **normative contract** between the two halves of the system:

- the open-source **agent** (`status.py`) that runs on the LinuxCNC machine, and
- the **dashboard/receiver** that runs on a monitoring PC.

Neither program imports the other. They stay in sync only by both obeying this
document. Any change to the wire format is a change to this file first.

---

## 1. Transport

| Property | Value |
|---|---|
| Transport | UDP (unreliable, connectionless, fire-and-forget) |
| Direction | Agent → monitoring PC for status. The agent's only listening socket is the read-only history port (`journal_port`, §5a) |
| Default port | `5005` (configurable via `config.yaml`) |
| Addressing | Default: **LAN broadcast** — the directed broadcast of each LAN interface, never the Mesa `hm2_eth` link. Optionally unicast to IPs / host names. Receivers bind `0.0.0.0:<port>` and must tolerate the same packet arriving twice (two interfaces on one network) |
| Encoding | UTF-8 |
| Framing | One JSON object per UDP datagram |
| Max datagram | Keep JSON < 65,000 bytes; large G-code is chunked (see §5) |

UDP is deliberate: the agent must never block or stall waiting on the network,
because it shares a machine with a real-time CNC controller. A dropped packet is
acceptable — the next one (≤1 s later) supersedes it. **Receivers must treat all
data as best-effort and must not assume every packet arrives or arrives in order.**

---

## 2. Versioning

Every packet carries an integer **`proto`** field equal to the protocol version
this document describes (currently `1`).

Rules:

1. **Additive changes** (new fields) do **not** bump `proto`. Receivers **MUST
   ignore unknown fields** so a newer agent works with an older dashboard.
2. **Breaking changes** (renaming/removing a field, changing a type or unit) bump
   `proto` and increment the "Protocol version" at the top of this file.
3. A receiver **SHOULD** warn (not crash) when it sees a `proto` it does not know.
4. A packet with no `proto` field predates version 1 and should be treated as
   legacy/unknown.

`proto` is separate from the application version (agent 1.4.0). App
versions can change freely without touching the protocol.

---

## 3. Common fields (all packet types)

| Field | Type | Description |
|---|---|---|
| `type` | string | `"status"` or `"gcode_file"` |
| `proto` | integer | Protocol version (currently `1`) |
| `ts` | integer | Unix epoch timestamp in **milliseconds** |
| `machine_name` | string | Machine identifier from `config.yaml`; since agent 1.4.0 it defaults to the PC's hostname (older agents may send `""`). Used by the dashboard to tell machines apart — not the sender IP, which can change |

---

## 4. Status packet (`type: "status"`)

Sent every `poll_interval_s` (default 1 s) while the machine is active, and once
per `idle_heartbeat_interval_s` (default 30 s) while idle (see §6).

### 4.1 Cycle & production

| Field | Type | Units | Description |
|---|---|---|---|
| `cycle_state` | string | — | `"IDLE"`, `"RUNNING"`, or `"PAUSED"` |
| `cycle_time_ms` | integer | ms | Active elapsed time of current cycle, pause-excluded. `0` when IDLE |
| `parts_produced` | integer | count | Parts completed this session — a run that reached M2/M30, either in one go or finished by Run From Here after an interruption |
| `abort_count` | integer | count | Cycles started from the top that ended before M2/M30 (a Run From Here continuation that stops again is not a new abort) |
| `run_from_here_count` | integer | count | Cycles started mid-program via "Run From Here" |
| `partial_parts` | integer | count | Parts abandoned unfinished — the program was restarted from the top instead of being continued |
| `partial_part_equiv` | float | parts | Sum of how far those partial parts got (e.g. one half-done part = `0.5`) |
| `last_partial_pct` | float \| null | % | How far the most recent partial part got |
| `program_progress_pct` | float | % | Progress of the current part through the program's moves (carried across Run From Here; kept while a part awaits continuation) |
| `part_in_progress` | boolean | — | `true` while a part is running or has been interrupted and not yet finished/abandoned |
| `last_cycle_ms` | integer \| null | ms | Duration of the most recently completed part cycle |
| `last_abort_ms` | integer \| null | ms | Duration of the most recently aborted cycle |
| `avg_cycle_ms` | float \| null | ms | Rolling average of completed cycle durations |
| `total_completed_cycles` | integer | count | Total cycles that produced a part |
| `cycle_complete_signalled` | boolean | — | `true` if the M2/M30 end line was reached this cycle |
| `is_run_from_here` | boolean | — | `true` if the current cycle started mid-program |
| `gcode_end_line` | integer | line | Line of the M2/M30 program end (the closing `%` only if the file has neither). `-1` if none found |
| `gcode_first_exec_line` | integer | line | First executable line number in the loaded file |
| `gcode_tail_line` | integer | line | Last move line before the program end — reaching it and going idle normally means the part finished. `-1` if none |

### 4.1a History journal (agent ≥ 1.5.0; absent if the journal is disabled)

| Field | Type | Description |
|---|---|---|
| `agent_id` | string | Id of the agent's journal. Changes only if the journal file is deleted — then seq numbers restart |
| `journal_seq` | integer | Seq of the newest journal entry at send time |
| `journal_port` | integer | UDP port that answers history requests (§5a); `0` = not available (off, or the history server stopped — agent ≥ 1.6.0 restarts it within a minute) |
| `journal_state` | string | Agent ≥ 1.7.0. `"ok"`; `"failing"` = the journal cannot be written (retried, up to every 5 min); `"disk_low"` = paused, less than `journal_min_free_mb` free. Not `"ok"` = history for this time will be missing; a dashboard should show that |

### 4.1b Stream identity (agent ≥ 1.6.0)

| Field | Type | Description |
|---|---|---|
| `session_id` | string | Random id made each time the agent starts. The part / abort counters start again at 0 with a new one |
| `packet_seq` | integer | Counts status packets within a session, from 1 |
| `machine_uuid` | string | Agent ≥ 1.7.0. This machine's permanent id: UUID version 4, lowercase with hyphens. Made once on the machine's first start (`~/linuxcnc-monitor-agent/identity.json`) and never changed by restarts, renames or reinstalls. It is also the MES `machine_id`. Absent only if the agent cannot write its identity file |

A receiver orders a machine's packets by (`session_id`, `packet_seq`), not by
`ts`: UDP may deliver a packet late or out of order, and the machine's clock can
be set back. A packet whose `packet_seq` is not higher than the last one accepted
in its session, or that comes from a session already replaced, is stale and is
dropped. A counter that goes down **within** a session is not a restart; a new
`session_id` is. (Agents without these fields: order by `ts`, treating a packet up
to ~10 s older than the newest as a delayed copy.) `session_id` is also stored in
journal entries, so replayed history shows restarts too.

A receiver keys a machine by `machine_uuid` when present, so two PCs with the
same `machine_name` stay two machines and a renamed PC keeps its history;
`machine_name` is only the display name. Without `machine_uuid` (older agents),
key by `machine_name`. `machine_uuid` is stored in journal entries too. A new
`machine_uuid` for a known name means the PC's identity file was lost or
damaged (the agent keeps a damaged one as `identity.json.bad-<UTC time>`).

### 4.2 Machine state

| Field | Type | Description |
|---|---|---|
| `task_state` | integer | `1`=ESTOP `2`=ESTOP_RESET `3`=OFF `4`=ON |
| `task_mode` | integer | `1`=MANUAL `2`=AUTO `3`=MDI |
| `interp_state` | integer | LinuxCNC interpreter-state enum |
| `exec_state` | integer | LinuxCNC exec-state enum. `1` = EXEC_ERROR. Not set by a G-code error in MDI — use `cmd_state` (see §7) |
| `cmd_state` | integer | Result of the last command (`stat.state`): `1`=DONE `2`=EXEC `3`=**ERROR** — a command failed (G-code error, unknown M-code…); stays `3` until the next command succeeds. Agent ≥ 1.6.0 |
| `cmd_serial` | integer | Serial of the last command (`stat.echo_serial_number`). Changes with every command; a new value with `cmd_state == 3` is a new error. Agent ≥ 1.6.0 |
| `estop` | boolean | `true` if in E-stop |
| `enabled` | boolean | `true` if drives are powered |
| `paused` | boolean | `true` if feed hold is active |
| `tool_in_spindle` | integer | Currently loaded tool number |
| `g5x_index` | integer | Active WCS index (`1`=G54 … `9`=G59.3) |
| `wcs` | string | Active WCS name, e.g. `"G54"` (derived from `g5x_index`) |
| `g5x_offset` | float[] | Active WCS offset `[X,Y,Z,A,B,C,U,V,W]` |
| `g92_offset` | float[] | G92 offset `[X,Y,Z,A,B,C,U,V,W]` |
| `tool_offset` | float[] | Tool length/diameter offset `[X,Y,Z,A,B,C,U,V,W]` |
| `gcodes` | integer[] | Active modal G-codes |
| `mcodes` | integer[] | Active modal M-codes |
| `settings` | float[] | Modal settings `[sequence, feed, speed, …]` |

### 4.3 Motion

| Field | Type | Units | Description |
|---|---|---|---|
| `current_vel` | float | machine units/s | Combined tool-tip velocity |
| `distance_to_go` | float | machine units | Remaining distance in current move |
| `motion_type` | integer | — | `0`=none `1`=traverse `2`=feed `3`=arc |
| `motion_line` | integer | line | G-code line executing in the motion controller |
| `current_line` | integer | line | G-code line being interpreted |
| `delay_left` | float | s | Remaining G4 dwell |
| `feedrate` | float | ratio | Feed override (`1.0` = 100%) |
| `rapidrate` | float | ratio | Rapid override (`1.0` = 100%) |

> **Units note:** positions, velocities and distances are in the machine's own
> units (mm or inch) as configured in LinuxCNC. The protocol does not convert.

### 4.4 Axis data — `axis` (object, active axes only)

```json
"axis": {
  "x": { "pos": 12.345678, "vel": 0.045, "min_pos_limit": -200.0, "max_pos_limit": 200.0 }
}
```

| Sub-field | Type | Description |
|---|---|---|
| `pos` | float (6dp) | **Machine** (absolute) position, from `actual_position` |
| `work` | float (6dp) | **Work** (relative) position in the active WCS = `pos − g5x − g92 − tool` |
| `vel` | float (6dp) | Axis velocity |
| `min_pos_limit` | float (4dp) | Soft lower limit |
| `max_pos_limit` | float (4dp) | Soft upper limit |

### 4.5 Joint data — `joints` (array)

```json
"joints": [
  { "id": 0, "pos": 12.345678, "vel": 0.045, "homed": true, "fault": false,
    "ferror": 0.000012, "hard_limit": false }
]
```

`hard_limit` is `true` while the joint is on its min or max hard limit switch.

### 4.6 Spindle data — `spindles` (array)

```json
"spindles": [
  { "id": 0, "speed": 8000.0, "direction": 1, "override": 1.0, "at_speed": true, "enabled": true }
]
```

`direction`: `1`=CW, `-1`=CCW, `0`=stopped.

### 4.7 File metadata

| Field | Type | Description |
|---|---|---|
| `file_name` | string | Basename of the loaded G-code file |
| `file_size` | integer | File size in bytes |
| `file_modified_ms` | integer | Last-modified time (epoch ms) |

### 4.8 NML errors — `nml_errors` (array)

```json
"nml_errors": [{ "kind": 1, "msg": "Joint 0 following error" }]
```

Agent ≥ 1.6.0: always `[]` unless `read_error_channel: true` is set on the
agent, because reading the NML queue removes messages before the operator's
GUI shows them (see §7). Do not rely on `nml_errors` for error detection; use
`cmd_state == 3`, `exec_state == 1`, `estop`, `task_state` and the joint flags.

---

### 4.9 MES capture (agent ≥ 1.7.0)

| Field | Type | Description |
|---|---|---|
| `mes_spool` | object | The agent's MES spool: `{"pending": n, "bytes": n, "state": "ok" \| "full" \| "off"}`. `pending` = events stored and not yet acknowledged by the site service; `bytes` = disk used by the spool files; `full` = the disk budget (`mes_spool_mb`) is used up and new events are refused (never dropped silently); `off` = MES is not enabled (`mes_enabled: false`, the default) or the spool could not be opened |
| `features` | array of strings | What this agent can do beyond monitoring. `"mes1"` = MES contract `fw.mes.event/1` capture is on. Absent when there is nothing to advertise |

Both are additive; a dashboard shows "MES-capable agent" for `"mes1"`.

## 5. G-code file packet (`type: "gcode_file"`)

Sent when a program is loaded, whenever the loaded file changes, and again every
`gcode_resend_s` seconds (agent default 60) so a dashboard started later, or one
that lost a packet, still gets it. Limits are the shared contract
`contracts/gcode/v1.json` (the agent repo vendors it).

Every packet fits one Ethernet frame (at most 1 400 bytes), so a program is split
into many chunks — about 1 000 characters of plain G-code each, fewer for
non-ASCII text. A program is sent only if it has at most **33 554 432 characters
(32 MiB)** and splits into at most **80 000** chunks; plain ASCII G-code of 32 MiB
is about 34 000 chunks.

```json
{
  "type":             "gcode_file",
  "proto":            1,
  "ts":               1748563200000,
  "machine_name":     "VMC-01",
  "file_name":        "part_A.ngc",
  "file_size":        20480,
  "file_modified_ms": 1748500000000,
  "content_sha256":   "36a3204b…",
  "chunk_index":      0,
  "total_chunks":     21,
  "content":          "%\nO0001 (Part A)\nG21 G90\n..."
}
```

| Field | Type | Description |
|---|---|---|
| `chunk_index` | integer | 0-based index of this chunk |
| `total_chunks` | integer | Total chunks for this file (1 … 80 000) |
| `content` | string | Text of this chunk |
| `content_sha256` | string | Optional (agent ≥ 1.7.0): lowercase hex SHA-256 of the **whole** program as UTF-8, the same in every chunk |

**Reassembly:** concatenate `content` from `chunk_index` `0` through
`total_chunks − 1`, in order. UDP may drop or reorder, so key chunks by machine
and `(file_name, file_size, file_modified_ms)`, and treat a file as complete only
once all indices are present. If `content_sha256` is given and the assembled text
does not match it, drop the program and wait for the next send. Bound memory by
characters (per program and all together) and by the number of chunks held for
all machines together — anyone on the network can send these packets.

### 5.1 Program too large (`type: "gcode_file_refused"`, agent ≥ 1.7.0)

Sent instead of the chunks when a program is over either limit (and repeated at
the resend interval):

```json
{ "type": "gcode_file_refused", "proto": 1, "ts": 1748563200000, "machine_name": "VMC-01",
  "file_name": "huge.ngc", "file_size": 52428800, "file_modified_ms": 1748500000000,
  "reason": "too_large", "limit_chars": 33554432, "limit_chunks": 80000 }
```

`reason` is `too_large` (characters) or `too_many_chunks`. The dashboard stops
showing the previous program (it is no longer the loaded one) and says why.

**Size limits (both sides).** The shared contract `contracts/gcode/v1.json`
(canonical copy in FloorWatch) sets them: a program is sent only if it has at
most **33,554,432 characters** (32 MiB of text) and splits into at most
**80,000** chunks. A receiver must accept anything within both limits; the agent
never sends more. Chunks are as large as one frame allows, with room left for a
site-key signature (§8.1) and `content_sha256`: an ASCII program needs about one
chunk per 980 characters (about 34,000 for 32 MiB; about 54,000 with a
128-character machine name and 255-character file name). Programs with many
non-ASCII characters fit fewer characters per chunk and can reach the chunk limit
before 32 MiB. A program over either limit is not sent: one `gcode_file_refused`
packet (§5.1) goes instead at each resend, the agent logs it once, and status
packets still name the file. A receiver keeps the chunks it already has between
resends, so a big program completes over two or more resends even with some
packet loss.

---

## 5a. History journal — request / reply

Status packets are fire-and-forget, so anything sent while no dashboard was
listening is lost. The agent (≥ 1.5.0) keeps a compact **journal** of status
on the machine (`~/linuxcnc-monitor-agent/journal.db`, default 14 days): an
entry whenever cycle state, part / abort / partial counts, errors, E-stop,
machine state or program change, and otherwise every 10 s while active and
on each idle heartbeat.

A dashboard fetches what it missed by sending a **unicast** request to
`<agent IP>:<journal_port>` (from the status packet); the reply goes back to
the request's source address and port.

```json
{ "type": "journal_request", "proto": 1, "from_seq": 1200, "to_seq": 1480 }
```

```json
{
  "type": "journal", "proto": 1, "ts": 1748563200000,
  "machine_name": "CNC-01", "agent_id": "6f1c…",
  "first_seq": 1, "last_seq": 1480, "from_seq": 1200,
  "entries": [[1201, 1748560000000, { "cycle_state": "RUNNING", "parts_produced": 12, "…": "…" }]],
  "more": true
}
```

| Field | Description |
|---|---|
| `from_seq` / `to_seq` | Entries with `from_seq < seq <= to_seq` (`to_seq` optional = newest), oldest first |
| `entries` | `[seq, ts_ms, data]`; `data` holds the status-packet fields listed in `agent_journal.JOURNAL_FIELDS`, plus `joints` (only faulted / on-limit joints) and `nml_errors` (when present) |
| `more` | `true` → ask again with `from_seq` = the last seq received |
| `first_seq` | Oldest seq still kept — anything below it was pruned |

**Finding what is missing.** Every journal entry is written from a status packet
that carries its seq in `journal_seq`, so consecutive packets from one agent
differ by 0 or 1. A step of more than 1 means packets carrying journal entries
were lost, however short the outage: fetch `(last seq held, journal_seq]`.
Keep the position saved per machine as a *committed watermark* — move it past a
range only once that range is stored — so a failed fetch is retried rather than
skipped. If `first_seq` is above the range's start, those entries were pruned:
record that span as unrecoverable history and carry on from `first_seq`.

Replies are ~16 KB at most. Requests are read-only and rate-limited; they
cannot change anything on the machine. Treat `ts_ms` as the time of the data.

---

## 6. Cadence & idle suppression

| Machine state | Behaviour |
|---|---|
| Active (RUNNING/PAUSED) | One status packet every `poll_interval_s` (default 1 s) |
| Transition to IDLE | One status packet sent immediately (the edge) |
| Stays IDLE | Suppressed for `idle_heartbeat_interval_s` (default 30 s) |
| IDLE heartbeat | One keep-alive status packet every `idle_heartbeat_interval_s` |
| Becomes active | Full-rate stream resumes immediately |

A receiver should consider a machine **offline** if no packet (not even a
heartbeat) arrives for noticeably longer than `idle_heartbeat_interval_s`
(a threshold of ~3× is reasonable).

---

## 7. Error semantics

The LinuxCNC NML error channel is a **queue**: the first reader to `poll()` an
error **deletes it** for everyone else, and LinuxCNC recommends a single reader.
So since 1.6.0 the agent does **not** open the error channel unless
`read_error_channel: true` is set. Therefore:

- `nml_errors` is `[]` by default. With `read_error_channel: true` it carries the
  messages the agent read — and the operator's GUI may then miss them.
- A **failed command** (G-code error, unknown M-code, zero feed…) shows as
  **`cmd_state == 3`** (RCS_ERROR). It stays `3` until the next command
  succeeds; each new `cmd_serial` while `cmd_state == 3` is one more error.
  Nothing is consumed, so the GUI still shows the message. The message text
  itself is not available without the error channel.
- `exec_state == 1` (EXEC_ERROR) is an error during execution. Do not rely on
  it alone: on LinuxCNC 2.9 a G-code error in MDI never sets it (exec_state
  goes from waiting straight back to done).
- `estop == true` and `task_state == 1` indicate an E-stop condition.
- **Fault edges are immediate** (agent ≥ 1.6.0): the agent compares `estop`,
  `task_state`, EXEC_ERROR, a failed command (`cmd_state` / `cmd_serial`) and
  each joint's `fault` / `hard_limit` on every
  sample (default 0.1 s) and sends a status packet at once when any of them
  changes, also while idle; the journal records both edges. A fault shorter
  than one sample can still be missed.

---

## 8. Security

Packets are **not encrypted**: anyone on the machine network can read them.
Deploy on a dedicated machine network; never on an untrusted or
internet-exposed one without a VPN or equivalent. The monitoring PC's firewall
should accept UDP `5005` from the machine subnet only.

### 8.1 Site key (agent ≥ 1.7.0, optional)

With `site_key` set in the agent's `config.yaml` (the same text in the
dashboard), every datagram the agent sends — status, G-code chunks, history
replies — is signed, and the history port answers only signed requests.

```
unsigned  {"type":"status",...,"packet_seq":7}
signed    {"type":"status",...,"packet_seq":7,"sig":"<64 lowercase hex digits>"}
sig       = HMAC-SHA256(key = site_key as UTF-8 bytes,
                        msg = "lcnc-sig-v1\n" + the unsigned bytes)
```

- `sig` is always the **last** member, written as `,"sig":"…"}` at the very end
  (`{"sig":"…"}` for an empty object). To verify, cut that text off the received
  bytes, put the `}` back, and compare the HMAC over exactly those bytes — no
  JSON re-encoding. A receiver without a key ignores the field.
- A dashboard with the key should drop a packet whose signature is wrong. During
  roll-out it may still accept **unsigned** packets; once every agent has the
  key it should refuse them, or anyone can send unsigned packets in a machine's
  name.
- **History requests** (§5a) must then be signed and carry a `nonce` (a string
  of 8–64 characters, new for every request, including retries). The agent
  ignores a request with a nonce it has seen among the last 4,096, so a captured
  request cannot be replayed. Replies are signed.
- **Replays of status packets** are dropped by the stream order (§4.1b): a
  packet whose `packet_seq` is not newer than one already accepted, or from a
  session that has already been replaced, is stale. A dashboard remembers
  replaced sessions only while it runs, so a captured old session could be
  replayed after a dashboard restart; the next real packet supersedes it, and
  the history backfill (§5a) corrects the counts.
- The key proves a packet came from something that knows the key and was not
  changed. It does **not** tell machines apart: anyone with the key can claim
  any machine name. Treat it like a password; the agent warns if `config.yaml`
  is readable by other users or the key is shorter than 16 characters.

### 8.2 History port

`journal_bind` makes the history port listen on one address only (e.g. the
machine-network card); `journal_allow` (a comma list of dashboard IPs) makes it
answer only those. A reply always goes to the address the request came from, so
a request with a forged source address sends the history to that address, not
to the forger.

---

## 9. Change log (protocol only)

| `proto` | Date | Change |
|---|---|---|
| 1 | 2026-09 | Initial formal spec. Adds `proto` and `machine_name` to all packets (additive over the pre-spec v1.2.0 payload). |
| 1 | 2026-09 | Additive: per-axis `work` position; `wcs`, `g92_offset`, `tool_offset` in machine state; `last_abort_ms`. No version bump (additive). |
| 1 | 2026-09 | Additive: `partial_parts`, `partial_part_equiv`, `last_partial_pct`, `program_progress_pct`, `part_in_progress`, `gcode_tail_line`, per-joint `hard_limit`. `gcode_end_line` now points at M2/M30 rather than a trailing `%`. No version bump. |
| 1 | 2026-09 | Transport: default addressing is LAN broadcast; `machine_name` defaults to the hostname. Receivers should drop exact duplicates (same machine + `ts`). No version bump. |
| 1 | 2026-09 | Additive: history journal — `agent_id`, `journal_seq`, `journal_port` in status packets; `journal_request` / `journal` packets (§5a). No version bump. |
| 1 | 2026-10 | Agent 1.7.0: optional site-key signature `sig` on every packet and history request, `nonce` in signed history requests (§8.1). Additive; no version bump. |
| 1 | 2026-10 | Agent 1.7.0: `journal_state`; `agent_id` / `journal_seq` also sent while the journal is failing (last known values). No version bump. |
| 1 | 2026-10 | Agent 1.6.0: `nml_errors` is `[]` unless `read_error_channel` is set; fault edges sent at once; `journal_request` `from_seq` / `to_seq` must be whole numbers 0…2⁶³−1 (else no reply); `journal_port` is `0` while the history server is down. No version bump. |
| 1 | 2026-10 | Additive (agent 1.6.0): `session_id` and `packet_seq` in status packets (§4.1b), `session_id` in journal entries; receivers order by them and find journal gaps by `journal_seq` steps (§5a). No version bump. |
| 1 | 2026-10 | Additive (agent 1.7.0): `content_sha256` in `gcode_file`; new `gcode_file_refused` packet (§5.1); limits from `contracts/gcode/v1.json` (32 MiB, 80 000 chunks; was 4 096 chunks in FloorWatch). No version bump. |
| 1 | 2026-10 | Additive (agent 1.7.0): `mes_spool` and `features` in status packets (§4.9). No version bump. |
| 1 | 2026-10 | Additive (agent 1.7.0): `machine_uuid` in status packets and journal entries (§4.1b); receivers key machines by it when present. No version bump. |
| 1 | 2026-10 | Additive (agent 1.6.0): `cmd_state` and `cmd_serial` in status packets and journal entries; a failed command is a fault edge (sent at once, journalled). Use `cmd_state == 3` for G-code errors, which do not set `exec_state` to 1. No version bump. |
