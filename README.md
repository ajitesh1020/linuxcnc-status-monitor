# LinuxCNC Status Monitor

An industrial-grade, real-time status monitor for [LinuxCNC](https://linuxcnc.org/) machines.  
Streams machine state, axis positions, spindle data, cycle times, and production counts over UDP to a monitoring PC — with zero risk to the running CNC process.

---

## Features

- **Real-time status broadcast** — polls LinuxCNC every second and sends a JSON payload via UDP
- **Cycle time tracking** — measures actual G-code run time with millisecond precision (pause-aware)
- **Production counting** — counts completed parts, tracks aborted cycles separately
- **Axis monitoring** — position, velocity, and limits for all active axes (X, Y, Z, A, B, C…)
- **Spindle monitoring** — RPM, direction, override, and at-speed status per spindle
- **NML error capture** — drains and ships LinuxCNC error messages in every packet
- **Auto-shutdown** — `status.py` closes cleanly when LinuxCNC exits
- **Dev mode** — verbose DEBUG logging via `--dev` flag or `CNC_DEV_MODE=1` env var
- **Fault-tolerant** — all LinuxCNC calls are guarded; this process can never crash LinuxCNC

---

## Repository Structure

```
linuxcnc-status-monitor/
├── status.py                  # Main application — run this alongside LinuxCNC
├── cycle_time_calculator.py   # Cycle timing and production counting module
├── scripts/
│   ├── launch_ofc.sh          # Wrapper: starts LinuxCNC + status.py together
│   └── OFC_PC.desktop         # Desktop launcher (points to launch_ofc.sh)
├── docs/
│   ├── UDP_PAYLOAD.md         # Full JSON payload field reference
│   ├── CONFIGURATION.md       # All tuneable constants explained
│   └── TROUBLESHOOTING.md     # Common errors and fixes
├── examples/
│   └── udp_receiver.py        # Example: receive and print UDP packets on the monitoring PC
└── README.md
```

---

## Requirements

| Requirement | Detail |
|---|---|
| OS | Linux (Ubuntu 20.04 / 22.04 recommended) |
| LinuxCNC | 2.8 or later |
| Python | 3.8 or later (ships with LinuxCNC) |
| Network | UDP reachability from CNC PC to monitoring PC |

No external Python packages are required — only stdlib and the `linuxcnc` module (pre-installed with LinuxCNC).

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/ajitesh1020/linuxcnc-status-monitor.git
cd linuxcnc-status-monitor
```

### 2. Configure your target IP and port

Edit the constants at the top of `status.py`:

```python
MONITOR_PC_IP: str  = "192.168.1.100"   # IP of your monitoring PC
MONITOR_PC_PORT: int = 5005              # UDP port to send data to
```

### 3. Copy files to your LinuxCNC config directory

```bash
INSTALL_DIR="/home/user_name/linuxcnc/configs/config_name/script"
mkdir -p "$INSTALL_DIR"
cp status.py cycle_time_calculator.py "$INSTALL_DIR/"
cp scripts/launch_ofc.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/launch_ofc.sh"
```

Update the paths inside `scripts/launch_ofc.sh` and `scripts/OFC_PC.desktop` to match your config directory and `.ini` file location.

### 4. Update the desktop launcher

Edit `scripts/OFC_PC.desktop`:
```ini
Exec=bash /home/user_name/linuxcnc/configs/config_name/indus-ai/launch_ofc.sh
```

Copy it to your Desktop:
```bash
cp scripts/axis.desktop ~/Desktop/
chmod +x ~/Desktop/axis.desktop
```

### 5. Launch

Double-click `axis.desktop` on the CNC machine desktop.  
LinuxCNC and `status.py` start together. When LinuxCNC closes, `status.py` shuts down automatically.

---

## Running Manually (Terminal)

```bash
# Production mode (silent — only warnings/errors logged to /tmp/cnc_status.log)
python3 status.py

# Development mode (verbose DEBUG to console + log file)
python3 status.py --dev
```

---

## Dev Mode

Dev mode enables verbose `DEBUG`-level logging to both the console and the rotating log file.  
It can be activated in two ways — useful for debugging without modifying any script:

| Method | Command |
|---|---|
| CLI flag | `python3 status.py --dev` |
| Env var (one-time) | `CNC_DEV_MODE=1 python3 status.py` |
| Env var (via launcher) | `CNC_DEV_MODE=1 bash launch.sh` |
| Env var (session) | `export CNC_DEV_MODE=1` then run normally |

When active, you will see this printed immediately:
```
[DEV MODE ACTIVE — enabled via --dev flag]
```

---

## Log File

Logs are written to `/tmp/cnc_status.log` (rotated at 5 MB, 3 backups kept).

```bash
# Follow live log output
tail -f /tmp/cnc_status.log

# Show only errors
grep ERROR /tmp/cnc_status.log
```

In production mode only `WARNING` and `ERROR` entries are written — the log stays quiet.

---

## UDP Packet Format

Every second, a JSON object is broadcast to the configured IP:PORT.  
See [`docs/UDP_PAYLOAD.md`](docs/UDP_PAYLOAD.md) for the full field reference.

**Example packet (abbreviated):**
```json
{
  "ts": 1748563200000,
  "cycle_state": "RUNNING",
  "cycle_time_ms": 47320,
  "parts_produced": 12,
  "abort_count": 1,
  "last_cycle_ms": 95400,
  "avg_cycle_ms": 94870.5,
  "estop": false,
  "enabled": true,
  "paused": false,
  "task_state": 4,
  "task_mode": 2,
  "current_vel": 0.045231,
  "motion_line": 142,
  "feedrate": 1.0,
  "axis": {
    "x": { "pos": 12.345678, "vel": 0.045, "min_pos_limit": -200.0, "max_pos_limit": 200.0 },
    "y": { "pos": -5.123456, "vel": 0.0,   "min_pos_limit": -150.0, "max_pos_limit": 150.0 },
    "z": { "pos": -22.0,     "vel": 0.0,   "min_pos_limit": -100.0, "max_pos_limit": 0.0   }
  },
  "spindles": [
    { "id": 0, "speed": 8000.0, "direction": 1, "override": 1.0, "at_speed": true, "enabled": true }
  ],
  "file_name": "part_A.ngc",
  "file_size": 20480,
  "nml_errors": []
}
```

---

## Receiving Data on the Monitoring PC

Run the included example receiver:

```bash
python3 examples/udp_receiver.py
```

Or receive raw data with `netcat`:
```bash
nc -u -l 5005
```

---

## Architecture

```
┌─────────────────────────────────┐        UDP / JSON
│        CNC Machine              │  ──────────────────────►  Monitoring PC
│                                 │       port 5005
│  ┌──────────┐  ┌─────────────┐  │
│  │ LinuxCNC │  │  status.py  │  │
│  │          │◄─│  (poll loop)│  │
│  │  NML/    │  │             │  │
│  │  stat    │  │ cycle_time_ │  │
│  │  channel │  │ calculator  │  │
│  └──────────┘  └─────────────┘  │
│                                 │
│  launch_ofc.sh ties lifetimes   │
└─────────────────────────────────┘
```

`status.py` connects to LinuxCNC's **read-only** NML status channel — it never sends commands to the machine.

---

## Cycle State Machine

```
         ┌──────────────┐
         │     IDLE     │◄──────────────────────────┐
         └──────┬───────┘                           │
                │ program starts (AUTO+RUNNING)      │
                ▼                                   │
         ┌──────────────┐   feed hold    ┌──────────┴───────┐
         │   RUNNING    │───────────────►│     PAUSED       │
         │              │◄───────────────│                  │
         └──────┬───────┘   resume       └──────────┬───────┘
                │                                   │
       program  │ complete                 abort /  │ E-stop
       normally │                          error    │
                ▼                                   ▼
         stop_cycle()                        abort_cycle()
         parts_produced += 1                abort_count += 1
```

---

## Configuration Reference

See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for all tuneable constants.

| Constant | Default | Description |
|---|---|---|
| `MONITOR_PC_IP` | `"193.168.0.3"` | Monitoring PC IP address |
| `MONITOR_PC_PORT` | `5005` | UDP destination port |
| `POLL_INTERVAL_S` | `1.0` | Seconds between status packets |
| `LOG_FILE` | `/tmp/cnc_status.log` | Log file path |
| `LOG_MAX_BYTES` | `5 MB` | Max log file size before rotation |
| `LOG_BACKUP_COUNT` | `3` | Number of rotated log files to keep |
| `MIN_VALID_CYCLE_MS` | `1000` | Minimum cycle duration to record (ms) |
| `MAX_HISTORY` | `500` | Rolling buffer size for cycle durations |
| `PART_COUNT_THRESHOLD` | `0.50` | Min ratio vs last cycle to count as a part |

---

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for detailed fixes.

**Common issues:**

| Symptom | Likely cause |
|---|---|
| `[FATAL] Could not import 'linuxcnc'` | Script is not running inside LinuxCNC environment |
| No packets received on monitoring PC | Wrong IP/port, firewall blocking UDP 5005 |
| `status.py` still running after LinuxCNC closes | Using old desktop file — update to use `launch_ofc.sh` |
| Parts not being counted | Cycle duration below `MIN_VALID_CYCLE_MS` (1 second) |

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Follow PEP 8 strictly — this is safety-critical industrial code
4. Test on a real LinuxCNC machine or simulation (`linuxcnc -l` sim mode)
5. Submit a pull request with a clear description

---

## License

MIT License — see [LICENSE](LICENSE) file.

---

## Acknowledgements

Built for real-world CNC production monitoring using the [LinuxCNC](https://linuxcnc.org/) open-source CNC platform.
