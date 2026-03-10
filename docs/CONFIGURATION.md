# Configuration Reference  —  v1.2.0

All configuration is done by editing constants directly in `status.py` and `cycle_time_calculator.py`.

---

## status.py

```python
# ── Network ───────────────────────────────────────────────────────────────
MONITOR_PC_IP: str   = "193.168.0.3"    # Static IP of monitoring PC
MONITOR_PC_PORT: int = 5005             # UDP destination port

# ── Timing ────────────────────────────────────────────────────────────────
POLL_INTERVAL_S: float           = 1.0   # Seconds between active packets
IDLE_HEARTBEAT_INTERVAL_S: float = 30.0  # Keep-alive interval when idle
                                          # Set to 0 to disable suppression

# ── G-code file streaming ─────────────────────────────────────────────────
GCODE_CHUNK_SIZE: int = 50_000          # Max bytes per UDP chunk

# ── Logging ───────────────────────────────────────────────────────────────
LOG_FILE: str         = "/tmp/cnc_status.log"
LOG_MAX_BYTES: int    = 5 * 1024 * 1024   # 5 MB per file before rotation
LOG_BACKUP_COUNT: int = 3                  # Number of rotated files to keep
```

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
