# UDP Payload Reference  —  v1.2.0

Two packet types, distinguished by the `"type"` field.

---

## Packet Types

| `type` | Description | When sent |
|---|---|---|
| `"status"` | Machine status | Every second when active; heartbeat every 30 s when idle |
| `"gcode_file"` | G-code file content | Once on load; again when file changes |

---

## Status Packet (`type: "status"`)

### Metadata

| Field | Type | Description |
|---|---|---|
| `type` | string | Always `"status"` |
| `ts` | integer (ms) | Unix epoch timestamp in milliseconds |

### Cycle & Production

| Field | Type | Description |
|---|---|---|
| `cycle_state` | string | `"IDLE"`, `"RUNNING"`, or `"PAUSED"` |
| `cycle_time_ms` | integer | Active elapsed ms of current cycle (pause-excluded). `0` when IDLE |
| `parts_produced` | integer | Parts produced this session (M2/M30 reached, not run_from_here) |
| `abort_count` | integer | Cycles that ended before reaching M2/M30 |
| `run_from_here_count` | integer | Cycles started mid-program via "Run From Here" |
| `last_cycle_ms` | integer\|null | Duration of most recently completed part cycle |
| `avg_cycle_ms` | float\|null | Rolling average of completed cycle durations |
| `total_completed_cycles` | integer | Total cycles that produced a part |
| `cycle_complete_signalled` | boolean | `true` if M2/M30 end line was reached in current cycle |
| `is_run_from_here` | boolean | `true` if current cycle started mid-program |
| `gcode_end_line` | integer | Line number of M2/M30 in loaded file. `-1` if not found |
| `gcode_first_exec_line` | integer | First executable line number in loaded file |

### Machine State

| Field | Type | Description |
|---|---|---|
| `task_state` | integer | `1`=ESTOP `2`=ESTOP_RESET `3`=OFF `4`=ON |
| `task_mode` | integer | `1`=MANUAL `2`=AUTO `3`=MDI |
| `interp_state` | integer | Interpreter state (LinuxCNC enum) |
| `exec_state` | integer | Execution state (LinuxCNC enum) |
| `estop` | boolean | `true` if machine is in E-stop |
| `enabled` | boolean | `true` if drives are powered |
| `paused` | boolean | `true` if feed hold is active |
| `tool_in_spindle` | integer | Currently loaded tool number |
| `g5x_index` | integer | Active WCS (1=G54 … 6=G59) |
| `g5x_offset` | float[] | Work coordinate offset [X,Y,Z,A,B,C,U,V,W] |
| `gcodes` | integer[] | Active G-codes |
| `mcodes` | integer[] | Active M-codes |
| `settings` | float[] | Modal settings [feed, speed, …] |

### Motion

| Field | Type | Description |
|---|---|---|
| `current_vel` | float (6dp) | Combined velocity (machine units/s) |
| `distance_to_go` | float (6dp) | Remaining distance in current move |
| `motion_type` | integer | `0`=none `1`=traverse `2`=feed `3`=arc |
| `motion_line` | integer | G-code line being executed by motion controller |
| `current_line` | integer | G-code line being interpreted |
| `delay_left` | float | Remaining G4 dwell (seconds) |
| `feedrate` | float | Feed override ratio (1.0 = 100%) |
| `rapidrate` | float | Rapid override ratio (1.0 = 100%) |

### Axis Data (`axis` object)

Keyed by axis name. Only active axes included.

```json
"axis": {
  "x": { "pos": 12.345678, "vel": 0.045, "min_pos_limit": -200.0, "max_pos_limit": 200.0 },
  "y": { "pos": -5.123, "vel": 0.0, "min_pos_limit": -150.0, "max_pos_limit": 150.0 },
  "z": { "pos": -22.0, "vel": 0.0, "min_pos_limit": -100.0, "max_pos_limit": 0.0 }
}
```

### Joint Data (`joints` array)

```json
"joints": [
  { "id": 0, "pos": 12.345678, "vel": 0.045, "homed": true, "fault": false, "ferror": 0.000012 }
]
```

### Spindle Data (`spindles` array)

```json
"spindles": [
  { "id": 0, "speed": 8000.0, "direction": 1, "override": 1.0, "at_speed": true, "enabled": true }
]
```

`direction`: `1`=CW, `-1`=CCW, `0`=stopped.

### File Metadata

| Field | Type | Description |
|---|---|---|
| `file_name` | string | Basename of loaded G-code file |
| `file_size` | integer | File size in bytes |
| `file_modified_ms` | integer | Last-modified time (epoch ms) |

### NML Errors

```json
"nml_errors": [{ "kind": 1, "msg": "Joint 0 following error" }]
```

Empty `[]` when no errors are pending.

---

## G-code File Packet (`type: "gcode_file"`)

```json
{
  "type":             "gcode_file",
  "ts":               1748563200000,
  "file_name":        "part_A.ngc",
  "file_size":        20480,
  "file_modified_ms": 1748500000000,
  "chunk_index":      0,
  "total_chunks":     1,
  "content":          "%\nO0001 (Part A)\nG21 G90\n..."
}
```

Reassembly: concatenate `content` from chunks `0` through `total_chunks - 1` in order.

---

## Idle Suppression Behaviour

When `cycle_state == "IDLE"`:
- One packet sent on the IDLE transition
- Suppressed for `IDLE_HEARTBEAT_INTERVAL_S` seconds (default 30)
- Keep-alive packet every 30 s
- Full stream resumes immediately when machine becomes active
