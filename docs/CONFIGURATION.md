# Configuration Reference

All configuration is done by editing constants at the top of `status.py` and `cycle_time_calculator.py`.  
No config files or environment setup needed — the values are clearly documented inline.

---

## status.py

```python
# ── Network ────────────────────────────────────────────────────────────────
MONITOR_PC_IP: str   = "193.168.0.3"   # IP address of your monitoring PC
MONITOR_PC_PORT: int = 5005            # UDP port (must be open/unblocked on monitoring PC)

# ── Timing ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_S: float = 1.0           # Seconds between status packet broadcasts
                                        # Lower = more responsive, higher CPU usage

# ── Logging ────────────────────────────────────────────────────────────────
LOG_FILE: str        = "/tmp/cnc_status.log"   # Absolute path to rotating log file
LOG_MAX_BYTES: int   = 5 * 1024 * 1024         # 5 MB per file before rotation
LOG_BACKUP_COUNT: int = 3                       # Number of old log files to keep
```

### Choosing POLL_INTERVAL_S

| Value | Use case |
|---|---|
| `0.5` | Near-real-time dashboards (doubles CPU usage) |
| `1.0` | **Default — recommended for production** |
| `2.0` | Low-bandwidth networks or resource-constrained machines |

---

## cycle_time_calculator.py

```python
MIN_VALID_CYCLE_MS: int    = 1_000    # Cycles shorter than 1 second are discarded
                                       # Prevents test runs / jogging from counting
                                       # Increase if your shortest real job > 1s

MAX_HISTORY: int           = 500      # Max completed/aborted cycles stored in memory
                                       # Older entries are dropped automatically (rolling buffer)
                                       # 500 cycles ≈ ~50 KB RAM — safe for embedded machines

PART_COUNT_THRESHOLD: float = 0.50   # A cycle counts as a part only if its duration
                                       # is ≥ 50% of the previous completed cycle
                                       # Prevents partial re-runs from inflating counts
                                       # Example: last cycle = 90s → new cycle must be ≥ 45s
```

### Tuning PART_COUNT_THRESHOLD

| Value | Behaviour |
|---|---|
| `0.0` | Every completed cycle counts (no filtering) |
| `0.50` | **Default** — filters cycles that are less than half the expected time |
| `0.80` | Strict — only counts cycles within 80% of the expected time |
| `1.0` | Exact match required (rarely useful — use only if cycle times are very consistent) |

---

## launch_ofc.sh

Edit these variables at the top of the script to match your installation:

```bash
LINUXCNC_CONFIG="/home/indus/linuxcnc/configs/OFC_PC/OFC_PC.ini"
STATUS_SCRIPT="/home/indus/linuxcnc/configs/OFC_PC/indus-ai/status.py"
LOG_FILE="/tmp/cnc_status_launcher.log"
```

The script waits 3 seconds after starting LinuxCNC before launching `status.py`.  
If your machine takes longer to initialise (slow hardware, many axes), increase this:

```bash
sleep 3   # ← increase to 5 or 10 if status.py starts before LinuxCNC is ready
```

---

## Dev Mode

Dev mode is not a constant — it's activated at runtime.  
See the main README for full details.

| Method | Command |
|---|---|
| CLI flag | `python3 status.py --dev` |
| Env var | `CNC_DEV_MODE=1 python3 status.py` |
| Via launcher | `CNC_DEV_MODE=1 bash launch_ofc.sh` |
