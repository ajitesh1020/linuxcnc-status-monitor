# UDP Payload Reference

Every second, `status.py` broadcasts a single JSON object over UDP.  
This document describes every field in the payload.

---

## Top-Level Fields

### Timestamp

| Field | Type | Unit | Description |
|---|---|---|---|
| `ts` | integer | milliseconds (epoch) | Packet timestamp — Unix epoch in ms |

### Cycle & Production

| Field | Type | Unit | Description |
|---|---|---|---|
| `cycle_state` | string | — | Current cycle state: `IDLE`, `RUNNING`, or `PAUSED` |
| `cycle_time_ms` | integer | ms | Elapsed active time of the current cycle (excludes paused time). `0` when idle |
| `parts_produced` | integer | count | Total parts produced this session (completed cycles only) |
| `abort_count` | integer | count | Total number of aborted cycles this session |
| `last_cycle_ms` | integer \| null | ms | Duration of the most recently completed cycle. `null` if no cycle has completed |
| `avg_cycle_ms` | float \| null | ms | Rolling average duration of all completed cycles. `null` if no cycles completed |
| `total_completed_cycles` | integer | count | Total number of completed (non-aborted) cycles this session |

### Machine State

| Field | Type | Description |
|---|---|---|
| `task_state` | integer | LinuxCNC task state: `1`=ESTOP, `2`=ESTOP_RESET, `3`=OFF, `4`=ON |
| `task_mode` | integer | LinuxCNC task mode: `1`=MANUAL, `2`=AUTO, `3`=MDI |
| `interp_state` | integer | Interpreter state (LinuxCNC internal enum) |
| `exec_state` | integer | Execution state (LinuxCNC internal enum) |
| `estop` | boolean | `true` if machine is in E-stop |
| `enabled` | boolean | `true` if machine is enabled (drives powered) |
| `paused` | boolean | `true` if program execution is paused (feed hold) |
| `tool_in_spindle` | integer | Tool number currently loaded in spindle |
| `g5x_index` | integer | Active work coordinate system index (1=G54, 2=G55, …) |
| `g5x_offset` | array[float] | Work coordinate offset array [X, Y, Z, A, B, C, U, V, W] |
| `gcodes` | array[integer] | Currently active G-codes |
| `mcodes` | array[integer] | Currently active M-codes |
| `settings` | array[float] | Current modal settings [feed, speed, …] |

### Motion

| Field | Type | Unit | Description |
|---|---|---|---|
| `current_vel` | float | machine units/s | Current combined axis velocity |
| `distance_to_go` | float | machine units | Remaining distance in current move |
| `motion_type` | integer | — | Type of motion: `0`=none, `1`=traverse, `2`=feed, `3`=arc, `4`=tool change |
| `motion_line` | integer | — | G-code line number currently being executed by motion controller |
| `current_line` | integer | — | G-code line number currently being interpreted |
| `delay_left` | float | seconds | Remaining time for active `G4` dwell |
| `feedrate` | float | ratio | Feed rate override (1.0 = 100%) |
| `rapidrate` | float | ratio | Rapid rate override (1.0 = 100%) |

---

## Nested Objects

### `axis` — Per-Axis Data

Object keyed by axis name (`"x"`, `"y"`, `"z"`, `"a"`, `"b"`, `"c"`, `"u"`, `"v"`, `"w"`).  
Only axes present in the machine config (based on `axis_mask`) are included.

```json
"axis": {
  "x": {
    "pos": 12.345678,
    "vel": 0.045231,
    "min_pos_limit": -200.0,
    "max_pos_limit":  200.0
  },
  "y": { ... },
  "z": { ... }
}
```

| Field | Type | Unit | Description |
|---|---|---|---|
| `pos` | float (6dp) | machine units | Current axis position (from encoder feedback) |
| `vel` | float (6dp) | machine units/s | Current axis velocity |
| `min_pos_limit` | float (4dp) | machine units | Configured minimum position limit |
| `max_pos_limit` | float (4dp) | machine units | Configured maximum position limit |

---

### `joints` — Per-Joint Data

Array of joint objects (one per configured joint).

```json
"joints": [
  {
    "id": 0,
    "pos": 12.345678,
    "vel": 0.045231,
    "homed": true,
    "fault": false,
    "ferror": 0.000012
  }
]
```

| Field | Type | Unit | Description |
|---|---|---|---|
| `id` | integer | — | Joint index |
| `pos` | float (6dp) | machine units | Current joint position |
| `vel` | float (6dp) | machine units/s | Current joint velocity |
| `homed` | boolean | — | `true` if joint has been homed this session |
| `fault` | boolean | — | `true` if joint has a following error fault |
| `ferror` | float (6dp) | machine units | Current following error magnitude |

---

### `spindles` — Per-Spindle Data

Array of spindle objects (one per configured spindle).

```json
"spindles": [
  {
    "id": 0,
    "speed": 8000.0,
    "direction": 1,
    "override": 1.0,
    "at_speed": true,
    "enabled": true
  }
]
```

| Field | Type | Unit | Description |
|---|---|---|---|
| `id` | integer | — | Spindle index |
| `speed` | float | RPM | Actual spindle speed |
| `direction` | integer | — | `1`=forward (CW), `-1`=reverse (CCW), `0`=stopped |
| `override` | float | ratio | Spindle speed override (1.0 = 100%) |
| `at_speed` | boolean | — | `true` if spindle has reached commanded speed |
| `enabled` | boolean | — | `true` if spindle is enabled |

---

### File Data

| Field | Type | Description |
|---|---|---|
| `file_name` | string | Basename of the currently loaded G-code file (e.g. `"part_A.ngc"`) |
| `file_size` | integer | File size in bytes |
| `file_modified_ms` | integer | File last-modified time (Unix epoch ms) |

All three fields are empty/zero when no file is loaded.

---

### `nml_errors` — LinuxCNC Error Messages

Array of error objects drained from the LinuxCNC NML error channel since the last packet.  
Empty array `[]` when no errors are pending.

```json
"nml_errors": [
  { "kind": 1, "msg": "Joint 0 following error" }
]
```

| Field | Type | Description |
|---|---|---|
| `kind` | integer | LinuxCNC error kind code |
| `msg` | string | Human-readable error message |

---

## Full Example Packet

```json
{
  "ts": 1748563200000,
  "cycle_state": "RUNNING",
  "cycle_time_ms": 47320,
  "parts_produced": 12,
  "abort_count": 1,
  "last_cycle_ms": 95400,
  "avg_cycle_ms": 94870.5,
  "total_completed_cycles": 12,
  "task_state": 4,
  "task_mode": 2,
  "interp_state": 1,
  "exec_state": 3,
  "estop": false,
  "enabled": true,
  "paused": false,
  "tool_in_spindle": 3,
  "g5x_index": 1,
  "g5x_offset": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
  "gcodes": [0, 170, 400, 200, 900, 940, 210, 910, 80, 970],
  "mcodes": [50, 90, 70],
  "settings": [0.0, 500.0, 1.0],
  "current_vel": 0.045231,
  "distance_to_go": 3.214,
  "motion_type": 2,
  "motion_line": 142,
  "current_line": 144,
  "delay_left": 0.0,
  "feedrate": 1.0,
  "rapidrate": 1.0,
  "axis": {
    "x": { "pos": 12.345678, "vel": 0.045231, "min_pos_limit": -200.0, "max_pos_limit": 200.0 },
    "y": { "pos": -5.123456, "vel": 0.0,      "min_pos_limit": -150.0, "max_pos_limit": 150.0 },
    "z": { "pos": -22.0,     "vel": 0.0,      "min_pos_limit": -100.0, "max_pos_limit": 0.0   }
  },
  "joints": [
    { "id": 0, "pos": 12.345678, "vel": 0.045231, "homed": true, "fault": false, "ferror": 0.000012 },
    { "id": 1, "pos": -5.123456, "vel": 0.0,      "homed": true, "fault": false, "ferror": 0.000008 },
    { "id": 2, "pos": -22.0,     "vel": 0.0,      "homed": true, "fault": false, "ferror": 0.000005 }
  ],
  "spindles": [
    { "id": 0, "speed": 8000.0, "direction": 1, "override": 1.0, "at_speed": true, "enabled": true }
  ],
  "file_name": "part_A.ngc",
  "file_size": 20480,
  "file_modified_ms": 1748500000000,
  "nml_errors": []
}
```
