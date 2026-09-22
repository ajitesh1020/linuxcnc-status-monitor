# Configuration Reference  —  v1.3.0

The agent is configured with a **`config.yaml`** file — no code editing required.
Copy the template and edit it:

```bash
cp config.example.yaml config.yaml
```

`status.py` loads `config.yaml` from the same directory automatically. Use a
different path with `--config`:

```bash
python3 status.py --config /etc/cnc/config.yaml
```

If `config.yaml` is missing or a key is absent, the built-in defaults (below) are
used, so the agent always starts. PyYAML is used if installed; otherwise a
built-in parser reads the flat file (keep it flat — no nested structures).

---

## config.yaml keys

| Key | Type | Default | Description |
|---|---|---|---|
| `monitor_pc_ip` | string | `"193.168.0.3"` | Static IP of the monitoring PC |
| `monitor_pc_port` | int | `5005` | UDP destination port |
| `machine_name` | string | `""` | Identifies this machine in the dashboard's multi-machine view |
| `poll_interval_s` | float | `1.0` | Seconds between status packets while active |
| `idle_heartbeat_interval_s` | float | `30.0` | Keep-alive interval while idle |
| `gcode_chunk_size` | int | `50000` | Max bytes per UDP G-code chunk |
| `log_file` | string | `"/tmp/cnc_status.log"` | Log path (dev mode only) |
| `log_max_bytes` | int | `5242880` | Rotate log after this many bytes (dev mode) |
| `log_backup_count` | int | `3` | Rotated log files to keep (dev mode) |

`machine_name`, `poll_interval_s`, `idle_heartbeat_interval_s`,
`monitor_pc_ip`/`monitor_pc_port` and `gcode_chunk_size` map to fields and cadence
described in [`PROTOCOL.md`](../PROTOCOL.md).

---

## cycle_time_calculator.py

```python
MIN_VALID_CYCLE_MS: int = 1_000   # Discard cycles shorter than 1 second
                                   # Prevents test jogs from counting
                                   # Increase if shortest real job > 1 s

MAX_HISTORY: int = 500            # Max completed/aborted cycles in memory
                                   # Older entries dropped automatically
```

### No M-code constants

As of v1.2.0, `MCODE_PROGRAM_START` and `MCODE_PROGRAM_COMPLETE` have been removed.  
Program completion is now detected automatically via M2/M30 line scanning — no G-code changes required.

---

## End-Line Detection Tuning

`_GcodeEndDetector` in `status.py` uses these regex patterns (not user-configurable via constants — edit the source if needed):

```python
# Matches program end lines: M2, M02, M30, M030, or standalone %
_GCODE_END_RE = re.compile(r"^(m0*2\b|m0*30\b|%\s*$)", re.IGNORECASE)

# Lines skipped when finding first executable line: blank, ;comment, (, O-word, %
_GCODE_SKIP_RE = re.compile(r"^(\s*$|;|%|\(|o\s*\d)", re.IGNORECASE)
```

**Run-From-Here tolerance:** a cycle is flagged as mid-program start if:
```
motion_line > (first_exec_line + 2)
```
The `+2` tolerance accounts for LinuxCNC executing a couple of preamble lines before the first user-visible line. Adjust in `_GcodeEndDetector.is_run_from_here()` if needed.

---

## launch_ofc.sh

```bash
LINUXCNC_CONFIG="/home/indus/linuxcnc/configs/OFC_PC/OFC_PC.ini"
STATUS_SCRIPT="/home/indus/linuxcnc/configs/OFC_PC/indus-ai/status.py"

sleep 3   # Increase to 5–10 on slower machines
```

---

## udp_receiver.py (monitoring PC)

Controlled by command-line flags — no constants to edit.

| Flag | Default | Description |
|---|---|---|
| `--port` | `5005` | UDP port to listen on |
| `--pretty` | off | Pretty JSON + rotating log file |
| `--log` | `/tmp/udp_receiver_pretty.log` | Log path for `--pretty` mode |
| `--fields FIELD ...` | — | Print only named fields |
| `--save-gcode DIR` | — | Save received G-code files to disk |
