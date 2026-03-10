# Troubleshooting  —  v1.2.0

---

## `[FATAL] Could not import 'linuxcnc'`

`status.py` must run on the CNC machine, not the monitoring PC.

```bash
python3 -c "import linuxcnc; print('OK')"
```

---

## No packets on monitoring PC

```bash
# 1. Confirm status.py is sending
python3 status.py --dev
# Look for: "Packet SENT (XXXX bytes)"

# 2. Confirm IP and port match in status.py
#    MONITOR_PC_IP  = "193.168.0.3"
#    MONITOR_PC_PORT = 5005

# 3. Open firewall on monitoring PC
sudo ufw allow 5005/udp

# 4. Test receive with netcat
nc -u -l 5005

# 5. Ping monitoring PC from CNC machine
ping 193.168.0.3
```

---

## Monitoring PC has no static IP

Packets arrive at the right place only if the monitoring PC always has the same IP.  
Follow the static IP setup in the README:

```bash
# Check current IP
ip a show enp2s0

# If wrong, edit and restart
sudo geany /etc/network/interfaces
sudo systemctl restart networking
```

---

## All cycles recorded as aborts

**Most common cause:** `gcode_end_line` is `-1` — no M2/M30/% found in the file.

```bash
# Check in dev mode
python3 status.py --dev 2>&1 | grep "end_line"
```

Expected output:
```
End-line scan: file=part_A.ngc  first_exec=3  end_line=47
```

If you see `end_line=-1`:
```
WARNING: No M2/M30/% found in 'part_A.ngc' — all cycles will be aborts.
```

**Fix:** make sure your G-code file ends with `M2` or `M30`.  
Every standard G-code program should have one — if yours doesn't, add it:

```gcode
; last line of your program:
M2
```

Also verify the packet field:
```bash
python3 examples/udp_receiver.py --fields gcode_end_line gcode_first_exec_line
```

---

## Parts not counting even though M2/M30 is present

**Possible cause 1 — motion_line never reaches end_line**

LinuxCNC's `motion_line` is the line being executed by the *motion controller*, which can lag behind the *interpreter* (`current_line`). If the program is very short, the motion controller may not reach the last line before LinuxCNC transitions to IDLE.

Check in dev mode:
```
Cycle STOP. duration=XXXX ms  end_line_reached=False  run_from_here=False
```

**Fix:** add a small `G4 P0.1` dwell just before `M2` to give the motion controller time to catch up:
```gcode
G4 P0.1   (short dwell — allows motion_line to reach M2)
M2
```

**Possible cause 2 — Run From Here flag set**

Check `is_run_from_here` in the received packet. If `true`, the cycle started mid-program.

**Possible cause 3 — cycle too short**

Minimum valid cycle is 1 second (`MIN_VALID_CYCLE_MS`). If your program runs faster, lower this constant in `cycle_time_calculator.py`.

---

## `run_from_here_count` keeps increasing

The operator is using LinuxCNC's "Run From Here" feature. These cycles are tracked but not counted as parts.

To count a run-from-here cycle as a part, the operator must restart from line 1.

---

## Machine is idle but packets keep arriving

Idle suppression is active by default. If packets still arrive rapidly:

1. Check `IDLE_HEARTBEAT_INTERVAL_S` in `status.py` (default 30 s)
2. Verify `cycle_state` is actually `"IDLE"` in received packets — if `"RUNNING"` then the machine isn't idle
3. Run `--dev` and look for: `"IDLE — packet suppressed."`

---

## G-code file not being received on monitoring PC

```bash
# On monitoring PC — watch for gcode_file packets
python3 examples/udp_receiver.py --save-gcode ./received/
```

The file is only sent when it changes. To force a resend:
- Load a different file in LinuxCNC then reload the original, OR
- Restart `status.py`

---

## `status.py` outlives LinuxCNC

Make sure `OFC_PC.desktop` uses `launch_ofc.sh`:
```ini
Exec=bash /home/indus/.../launch_ofc.sh
```

Verify the launcher log:
```bash
cat /tmp/cnc_status_launcher.log
# Should show: "LinuxCNC exited — stopping status.py"
```

Manual cleanup:
```bash
pkill -f status.py
```

---

## Log files

```bash
# CNC machine — follow live log
tail -f /tmp/cnc_status.log

# Monitoring PC — follow pretty log
tail -f /tmp/udp_receiver_pretty.log

# Clear logs
> /tmp/cnc_status.log
> /tmp/udp_receiver_pretty.log
```

---

## Checking dev mode env var

```bash
printenv CNC_DEV_MODE        # prints 1 if set
export CNC_DEV_MODE=1        # set for this session
unset CNC_DEV_MODE           # remove

# Make permanent
echo 'export CNC_DEV_MODE=1' >> ~/.bashrc
source ~/.bashrc
```
