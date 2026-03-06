# Troubleshooting

---

## `[FATAL] Could not import 'linuxcnc'`

**Cause:** The `linuxcnc` Python module is only available inside a LinuxCNC environment.

**Fix:** Make sure you are running `status.py` on the CNC machine itself, not on the monitoring PC.  
The script must be launched from a terminal inside the LinuxCNC session, or via `launch_ofc.sh`.

```bash
# Verify the module is available
python3 -c "import linuxcnc; print('OK')"
```

If this fails, LinuxCNC is not installed or not on the Python path.

---

## No packets received on the monitoring PC

**Step 1 — Verify status.py is running and sending:**
```bash
python3 status.py --dev
# Look for: "Packet sent (XXXX bytes) → 192.168.x.x:5005"
```

**Step 2 — Check the IP and port in status.py match your monitoring PC:**
```python
MONITOR_PC_IP: str  = "192.168.1.100"   # must match monitoring PC
MONITOR_PC_PORT: int = 5005
```

**Step 3 — Check firewall on the monitoring PC:**
```bash
# On Linux monitoring PC — allow UDP port 5005
sudo ufw allow 5005/udp

# On Windows monitoring PC — add inbound rule for UDP 5005
```

**Step 4 — Test with netcat on the monitoring PC:**
```bash
nc -u -l 5005
# Should print JSON when status.py runs
```

**Step 5 — Verify network reachability:**
```bash
# From CNC machine
ping 192.168.1.100
```

---

## `status.py` still running after LinuxCNC closes

**Cause:** Using the old desktop launcher that starts both processes independently.

**Fix:** Use the new `launch_ofc.sh` wrapper which ties the lifetimes together.

```ini
# OFC_PC.desktop — must look like this:
Exec=bash /home/indus/linuxcnc/configs/OFC_PC/indus-ai/launch_ofc.sh
```

**Verify the launcher is working:**
```bash
cat /tmp/cnc_status_launcher.log
# Should show: LinuxCNC started (PID XXXX), status.py started (PID XXXX)
# And after closing: LinuxCNC exited (code 0) — stopping status.py
```

**Manual cleanup if status.py is already orphaned:**
```bash
pkill -f status.py
```

---

## Parts not being counted

**Possible cause 1 — Cycle is too short:**  
The minimum cycle duration is 1 second (`MIN_VALID_CYCLE_MS = 1000`).  
If your G-code program runs in under 1 second, lower this constant in `cycle_time_calculator.py`.

**Possible cause 2 — Cycle duration is less than 50% of the previous cycle:**  
The `PART_COUNT_THRESHOLD = 0.50` filter prevents partial runs from counting.  
In dev mode, you will see: `"Cycle too short vs previous (XXXX ms) — part NOT counted."`  
Lower `PART_COUNT_THRESHOLD` or set it to `0.0` to disable this filter.

**Possible cause 3 — Program not running in AUTO mode:**  
Cycle counting only works when LinuxCNC is in `MODE_AUTO` (`task_mode == 2`).  
MDI commands and manual jogging are not counted.

**Debug — run with dev mode and watch the log:**
```bash
python3 status.py --dev 2>&1 | grep -E "STARTED|STOPPED|PAUSED|counted|abort"
```

---

## `status.py` exits immediately with a poll error

**Cause:** LinuxCNC is not running when `status.py` starts.

**Fix:** The script will automatically retry every 5 seconds.  
If using `launch_ofc.sh`, the 3-second delay before starting `status.py` is usually sufficient.  
For slower machines, increase the delay:

```bash
# In launch_ofc.sh
sleep 3   # ← increase to 5 or 10
```

---

## Log file grows too large

The log rotates automatically at 5 MB with 3 backups.  
If you need smaller logs:

```python
# In status.py
LOG_MAX_BYTES: int   = 1 * 1024 * 1024  # 1 MB
LOG_BACKUP_COUNT: int = 2
```

To clear the log immediately:
```bash
> /tmp/cnc_status.log
```

---

## High CPU usage

`status.py` polls every 1 second and sleeps 50ms between checks — CPU usage should be negligible (< 1%).  
If you see high CPU:

1. Make sure you haven't accidentally set `POLL_INTERVAL_S` to a very small value
2. Check the monitoring PC receiver isn't overwhelming the CNC machine with reply traffic (UDP is one-way — it shouldn't be)
3. Use `htop` to confirm which process is causing the load

---

## Checking the env var is set

```bash
# Check if set
printenv CNC_DEV_MODE      # prints "1" if set, nothing if not

# Set for current session
export CNC_DEV_MODE=1

# Unset
unset CNC_DEV_MODE

# Set permanently (adds to ~/.bashrc)
echo 'export CNC_DEV_MODE=1' >> ~/.bashrc
source ~/.bashrc
```
