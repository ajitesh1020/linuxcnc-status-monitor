# PROTOCOL.md — LinuxCNC Status Monitor Wire Protocol

**Protocol version: 1**

This is the **normative contract** between the two halves of the system:

- the open-source **agent** (`status.py`) that runs on the LinuxCNC machine, and
- the **dashboard/receiver** that runs on a monitoring PC.

Neither program imports the other. They stay in sync only by both obeying this
document. Any change to the wire format is a change to this file first.

> This file supersedes `docs/UDP_PAYLOAD.md` as the authoritative spec.
> `docs/UDP_PAYLOAD.md` remains as a human-friendly field reference.

---

## 1. Transport

| Property | Value |
|---|---|
| Transport | UDP (unreliable, connectionless, fire-and-forget) |
| Direction | Agent → monitoring PC (one-way; the agent never listens) |
| Default port | `5005` (configurable via `config.yaml`) |
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

`proto` is separate from the application version (`status.py` v1.3.0). App
versions can change freely without touching the protocol.

---

## 3. Common fields (all packet types)

| Field | Type | Description |
|---|---|---|
| `type` | string | `"status"` or `"gcode_file"` |
| `proto` | integer | Protocol version (currently `1`) |
| `ts` | integer | Unix epoch timestamp in **milliseconds** |
| `machine_name` | string | Operator-assigned machine identifier from `config.yaml`. May be `""` if unset. Used by the dashboard to distinguish machines in a multi-machine view |

---

## 4. Status packet (`type: "status"`)

Sent every `poll_interval_s` (default 1 s) while the machine is active, and once
per `idle_heartbeat_interval_s` (default 30 s) while idle (see §6).

### 4.1 Cycle & production

| Field | Type | Units | Description |
|---|---|---|---|
| `cycle_state` | string | — | `"IDLE"`, `"RUNNING"`, or `"PAUSED"` |
| `cycle_time_ms` | integer | ms | Active elapsed time of current cycle, pause-excluded. `0` when IDLE |
| `parts_produced` | integer | count | Parts completed this session (reached M2/M30; excludes run-from-here) |
| `abort_count` | integer | count | Cycles that ended before reaching M2/M30 |
| `run_from_here_count` | integer | count | Cycles started mid-program via "Run From Here" |
| `last_cycle_ms` | integer \| null | ms | Duration of the most recently completed part cycle |
| `avg_cycle_ms` | float \| null | ms | Rolling average of completed cycle durations |
| `total_completed_cycles` | integer | count | Total cycles that produced a part |
| `cycle_complete_signalled` | boolean | — | `true` if the M2/M30 end line was reached this cycle |
| `is_run_from_here` | boolean | — | `true` if the current cycle started mid-program |
| `gcode_end_line` | integer | line | Line number of last M2/M30/`%` in the loaded file. `-1` if none found |
| `gcode_first_exec_line` | integer | line | First executable line number in the loaded file |

### 4.2 Machine state

| Field | Type | Description |
|---|---|---|
| `task_state` | integer | `1`=ESTOP `2`=ESTOP_RESET `3`=OFF `4`=ON |
| `task_mode` | integer | `1`=MANUAL `2`=AUTO `3`=MDI |
| `interp_state` | integer | LinuxCNC interpreter-state enum |
| `exec_state` | integer | LinuxCNC exec-state enum. **`1` = EXEC_ERROR** — reliable error indicator (see §7) |
| `estop` | boolean | `true` if in E-stop |
| `enabled` | boolean | `true` if drives are powered |
| `paused` | boolean | `true` if feed hold is active |
| `tool_in_spindle` | integer | Currently loaded tool number |
| `g5x_index` | integer | Active WCS (`1`=G54 … `6`=G59) |
| `g5x_offset` | float[] | Work-coordinate offset `[X,Y,Z,A,B,C,U,V,W]` |
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
| `pos` | float (6dp) | Commanded/input position |
| `vel` | float (6dp) | Axis velocity |
| `min_pos_limit` | float (4dp) | Soft lower limit |
| `max_pos_limit` | float (4dp) | Soft upper limit |

### 4.5 Joint data — `joints` (array)

```json
"joints": [
  { "id": 0, "pos": 12.345678, "vel": 0.045, "homed": true, "fault": false, "ferror": 0.000012 }
]
```

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

Empty `[]` when nothing was captured. **This channel is best-effort only** — the
AXIS GUI wins the NML queue race in most cases and consumes the message first
(see §7). Do not rely on `nml_errors` for error detection; use `exec_state == 1`.

---

## 5. G-code file packet (`type: "gcode_file"`)

Sent once when a program is loaded, and again whenever the loaded file changes.
Large files are split into chunks of `gcode_chunk_size` bytes (default 50,000).

```json
{
  "type":             "gcode_file",
  "proto":            1,
  "ts":               1748563200000,
  "machine_name":     "VMC-01",
  "file_name":        "part_A.ngc",
  "file_size":        20480,
  "file_modified_ms": 1748500000000,
  "chunk_index":      0,
  "total_chunks":     1,
  "content":          "%\nO0001 (Part A)\nG21 G90\n..."
}
```

| Field | Type | Description |
|---|---|---|
| `chunk_index` | integer | 0-based index of this chunk |
| `total_chunks` | integer | Total chunks for this file |
| `content` | string | UTF-8 text of this chunk |

**Reassembly:** concatenate `content` from `chunk_index` `0` through
`total_chunks − 1`, in order. Because UDP may drop or reorder, a receiver should
key chunks by `(file_name, file_size, file_modified_ms)` and only treat a file as
complete once all indices `0…total_chunks−1` are present. A missing chunk will be
re-sent on the next file change; the agent also re-sends the whole file when the
fingerprint changes.

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
error **deletes it** for everyone else. The agent deliberately yields this race
to the AXIS GUI so the operator at the machine always sees the error. Therefore:

- `nml_errors` is a **bonus**, not a guarantee — `[]` is normal even during a fault.
- Use **`exec_state == 1` (EXEC_ERROR)** as the reliable, non-consuming
  error-condition indicator in the dashboard.
- `estop == true` and `task_state == 1` indicate an E-stop condition.

---

## 8. Security (current & planned)

- **Current (v1):** no authentication or encryption. Deploy only on a trusted,
  isolated machine network. Bind the monitoring PC firewall to UDP `5005` from
  the CNC subnet only.
- **Planned (Phase 2):** an optional shared token in `config.yaml`, echoed in a
  `token` field, so a receiver can reject packets from unexpected senders; and
  optional bind-to-interface on the agent. When added, the token will be an
  additive field and will **not** bump `proto` unless verification becomes
  mandatory.

Never place machine data on an untrusted or internet-exposed network without a
VPN or equivalent.

---

## 9. Change log (protocol only)

| `proto` | Date | Change |
|---|---|---|
| 1 | 2026-09 | Initial formal spec. Adds `proto` and `machine_name` to all packets (additive over the pre-spec v1.2.0 payload). |
