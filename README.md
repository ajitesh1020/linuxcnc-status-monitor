# LinuxCNC Status Monitor

An industrial-grade, real-time status monitor for [LinuxCNC](https://linuxcnc.org/) machines.  
Streams machine state, axis positions, spindle data, cycle times, and production counts over UDP — with **zero changes required to your G-code files** and zero risk to the running CNC process.

**Version: 1.2.0**

---

## Features

- **Zero G-code changes** — program completion detected by scanning for `M2`/`M30` line numbers; no custom M-codes needed
- **Real-time status broadcast** — polls LinuxCNC every second; JSON over UDP
- **Idle suppression** — no packets while machine is idle; one heartbeat every 30 s
- **Run From Here detection** — mid-program starts flagged automatically; not counted as parts
- **G-code file streaming** — full program sent once on load; re-sent only when file changes
- **Cycle time tracking** — millisecond-precision, pause-aware
- **Production counting** — completed parts, aborts, and Run-From-Here counts tracked separately
- **NML error capture** — LinuxCNC error messages shipped in every packet
- **Auto-shutdown** — `status.py` closes cleanly when LinuxCNC exits
- **Dev mode** — verbose DEBUG logging via `--dev` or `CNC_DEV_MODE=1`
- **Fault-tolerant** — all LinuxCNC calls guarded; this process can never crash LinuxCNC

---

## Repository Structure

```
linuxcnc-status-monitor/
├── status.py                  # Main application
├── cycle_time_calculator.py   # Cycle timing and production counting
├── scripts/
│   ├── launch_ofc.sh          # Launcher: starts LinuxCNC + status.py together
│   └── OFC_PC.desktop         # Desktop shortcut
├── docs/
│   ├── UDP_PAYLOAD.md         # Full JSON payload reference
│   ├── CONFIGURATION.md       # All tuneable constants
│   └── TROUBLESHOOTING.md     # Common errors and fixes
├── examples/
│   └── udp_receiver.py        # Receive and display packets on monitoring PC
├── CHANGELOG.md
└── README.md
```

---

## Requirements

| Item | Detail |
|---|---|
| OS | Linux (Ubuntu 20.04 / 22.04) |
| LinuxCNC | 2.8 or later |
| Python | 3.8 or later (ships with LinuxCNC) |
| Network | Static IP on monitoring PC (see below) |

No external Python packages required.

---

## Step 1 — Set a Static IP on the Monitoring PC

The monitoring PC **must** have a static IP so the CNC machine always knows where to send data.

### Find your network interface name

```bash
ip a
```

Look for the interface connected to your CNC network — typically `eth0`, `enp2s0`, or similar.

### Configure the static IP

```bash
sudo geany /etc/network/interfaces
```

Append at the end of the file (replace `enp2s0` with your actual interface name):

```
auto enp2s0
iface enp2s0 inet static
    address 193.168.0.3
    netmask 255.255.255.0
    gateway 193.168.0.1
```

### Apply and verify

```bash
# Restart networking
sudo systemctl restart networking

# Confirm IP is assigned
ip a show enp2s0

# Ping the CNC machine (Mesa card) to confirm routing
ping 10.10.10.1
```

---

## Step 2 — Configure status.py

Edit the constants at the top of `status.py`:

```python
MONITOR_PC_IP:  str = "193.168.0.3"   # your monitoring PC static IP
MONITOR_PC_PORT: int = 5005            # UDP port
```

---

## Step 3 — Install

```bash
git clone https://github.com/your-org/linuxcnc-status-monitor.git
cd linuxcnc-status-monitor

INSTALL_DIR="/home/user_name/linuxcnc/configs/OFC_PC/indus-ai"
mkdir -p "$INSTALL_DIR"
cp status.py cycle_time_calculator.py "$INSTALL_DIR/"
cp scripts/launch_ofc.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/launch_ofc.sh"
```

Update paths inside `scripts/launch_ofc.sh` and `scripts/OFC_PC.desktop`, then copy the launcher:

```bash
cp scripts/OFC_PC.desktop ~/Desktop/
chmod +x ~/Desktop/OFC_PC.desktop
```

---

## Step 4 — Launch

Double-click `OFC_PC.desktop`. LinuxCNC and `status.py` start together.  
When LinuxCNC closes, `status.py` shuts down automatically.

---

## How Program Completion Is Detected

`status.py` scans the loaded G-code file and finds:

| Detected value | What it is |
|---|---|
| `gcode_first_exec_line` | First non-blank, non-comment line in the file |
| `gcode_end_line` | **Last** line containing `M2`, `M30`, or a trailing `%` |

During execution, when `motion_line >= gcode_end_line`, the cycle is marked **complete** and counts as a part.

If the program stops before reaching `gcode_end_line` (E-stop, operator cancel), it is recorded as an **abort**.

**No M-codes, no shell scripts, no G-code changes required.**

---

## Run From Here

When an operator uses LinuxCNC's "Run From Here" feature:
- `status.py` detects `motion_line > gcode_first_exec_line` at cycle start
- The cycle is flagged `is_run_from_here: true`
- The cycle is **not** counted as a completed part (partial run)
- It is counted in `run_from_here_count` for visibility

---

## Running Manually

```bash
# Production (silent; logs to /tmp/cnc_status.log)
python3 status.py

# Development (verbose DEBUG to console + file)
python3 status.py --dev
```

---

## Dev Mode

| Method | Command |
|---|---|
| CLI flag | `python3 status.py --dev` |
| Env var (one-time) | `CNC_DEV_MODE=1 python3 status.py` |
| Env var (via launcher) | `CNC_DEV_MODE=1 bash launch_ofc.sh` |
| Env var (session) | `export CNC_DEV_MODE=1` |

Verify env var:
```bash
printenv CNC_DEV_MODE   # prints 1 if set
```

In dev mode, watch the end-line detection:
```
End-line scan: file=part_A.ngc  first_exec=3  end_line=47
```

And cycle classification:
```
Program END LINE reached — cycle complete at 95230 ms.
Part COUNTED (#7). Cycle time: 95230 ms.
```

---

## Idle Suppression

| Event | Behaviour |
|---|---|
| Machine transitions to IDLE | One packet sent immediately |
| Machine stays IDLE | Silent for 30 s |
| 30 s heartbeat | One keep-alive packet |
| Machine becomes active | Full stream resumes |

Configurable: `IDLE_HEARTBEAT_INTERVAL_S = 30.0` in `status.py`.

---

## Receiving Data on the Monitoring PC

```bash
# Compact live summary
python3 examples/udp_receiver.py

# Full pretty JSON + rotating log file
python3 examples/udp_receiver.py --pretty

# Custom log path
python3 examples/udp_receiver.py --pretty --log /var/log/cnc_packets.log

# Save received G-code files to disk
python3 examples/udp_receiver.py --save-gcode ./received/

# Show specific fields only
python3 examples/udp_receiver.py --fields cycle_state parts_produced gcode_end_line

# Open firewall on monitoring PC
sudo ufw allow 5005/udp
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐      UDP / JSON
│               CNC Machine                       │ ────────────────► Monitoring PC
│                                                  │     port 5005
│  ┌──────────┐    ┌────────────────────────────┐  │
│  │ LinuxCNC │    │        status.py            │  │
│  │  NML     │◄───│  ┌──────────────────────┐   │  │
│  │  stat    │    │  │  CycleStateMachine    │   │  │
│  │  channel │    │  ├──────────────────────┤   │  │
│  │  error   │    │  │  GcodeEndDetector     │   │  │
│  │  channel │    │  │  (scans M2/M30 line)  │   │  │
│  └──────────┘    │  ├──────────────────────┤   │  │
│                  │  │  CycleTimeCalculator  │   │  │
│                  │  ├──────────────────────┤   │  │
│                  │  │  GcodeFileSender      │   │  │
│                  │  └──────────────────────┘   │  │
│                  └────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

---

## Cycle State Machine

```
         ┌──────────────┐
         │     IDLE     │◄────────────────────────────┐
         └──────┬───────┘                             │
                │ AUTO+RUNNING detected                │
                │ motion_line > first_exec+2           │
                │ → run_from_here = True               │
                ▼                                     │
         ┌──────────────┐  feed hold  ┌───────────────┴──┐
         │   RUNNING    │────────────►│     PAUSED        │
         │              │◄────────────│                   │
         └──────┬───────┘  resume     └───────────┬───────┘
                │                                 │
   motion_line  │                       abort while│ paused
   >= end_line  │  → signal_cycle_complete()       │ → abort_cycle()
   IDLE         │  → stop_cycle()                  │
                │                                 │
    ┌───────────┴──────────────────────────┐      │
    │ end line reached?  → Part counted    │      │
    │ end line missed?   → Abort recorded  │      │
    │ run_from_here?     → RFH recorded    │      │
    └──────────────────────────────────────┘      │
         ▲                                        │
         └────────────────────────────────────────┘
```

---

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

| Symptom | Likely cause |
|---|---|
| `[FATAL] Could not import 'linuxcnc'` | Not inside LinuxCNC environment |
| No packets on monitoring PC | Wrong IP/port, firewall blocking UDP 5005 |
| All cycles recorded as aborts | No M2/M30 found in G-code file — check with `--dev` |
| `gcode_end_line: -1` in packet | File has no M2/M30/% — add one |
| `run_from_here_count` increasing | Operator using "Run From Here" |
| `status.py` outlives LinuxCNC | Using old desktop file — use `launch_ofc.sh` |

---

## Contributing

1. Fork the repo
2. Branch: `git checkout -b feature/your-feature`
3. Follow PEP 8 strictly
4. Test on real LinuxCNC or sim: `linuxcnc -l`
5. Submit a pull request

---

## License

MIT — see [LICENSE](LICENSE).
