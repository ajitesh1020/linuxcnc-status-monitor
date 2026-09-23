# LinuxCNC Status Monitor

A real-time, read-only status monitor for [LinuxCNC](https://linuxcnc.org/) machines.
Streams machine state, axis positions, spindle, cycle times, part counts and
errors over UDP — with **zero changes to your G-code** and zero risk to the
running CNC process.

**Version: 1.4.0**

---

## This is the free agent. Want the dashboard?

This repository is the **free, open-source agent** that runs on your CNC machine
and streams its status. It's the safe, read-only half of a two-part system:

- **Agent (this repo, free & GPL)** — runs on the LinuxCNC machine, read-only,
  streams status over UDP. Use it standalone with the included
  [`examples/udp_receiver.py`](examples/udp_receiver.py).
- **LinuxCNC Status Dashboard (paid)** — a desktop app for your office PC that
  turns this stream into a **live multi-machine grid, historical logging, OEE
  (Availability × Performance × Quality), reports, and email/Telegram alerts**.
  No Python needed on the monitoring PC.

  A **free tier** monitors a single machine (live view). Paid tiers add history,
  OEE, alerts, and multiple machines.

  👉 **Get the dashboard:** _<add your Gumroad/store link here>_

The agent speaks a documented wire protocol ([`PROTOCOL.md`](PROTOCOL.md)), so
it works with the dashboard or your own receiver.

---

## Install (Debian / LinuxCNC 2.9 ISO)

1. Download `linuxcnc-status-agent_<version>_all.deb` from the
   [latest release](https://github.com/ajitesh1020/linuxcnc-status-monitor/releases/latest).
2. Install it:

   ```bash
   sudo apt install ./linuxcnc-status-agent_*_all.deb
   ```

That's it. Start LinuxCNC **any way you like** — desktop icon, terminal,
the configuration picker — and the agent attaches automatically.

- It runs as a per-user background service (`lcnc-status-agent`) that starts at
  login, **waits for LinuxCNC**, connects when LinuxCNC starts and lets go when
  it exits.
- Nothing inside LinuxCNC is modified, so **LinuxCNC updates can't remove it**.
- Upgrading: install the newer `.deb` the same way; your settings are kept.
- Removing: `sudo apt remove linuxcnc-status-agent`.

### Settings

`~/linuxcnc-monitor-agent/config.yaml` — created the first time the agent runs.
Out of the box it needs **no editing**: it broadcasts on the local network and
names the machine after the PC's hostname. Typical edit:

```yaml
machine_name: "VMC-01"      # how the machine appears in the dashboard
```

Apply changes with `systemctl --user restart lcnc-status-agent`.
See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for every key.

### Useful commands

```bash
lcnc-status-agent --check                 # config file, name, where packets go, LinuxCNC up?
systemctl --user status lcnc-status-agent # is the service running?

# Watch it live (verbose):
systemctl --user stop lcnc-status-agent
lcnc-status-agent --dev
systemctl --user start lcnc-status-agent
```

### Running from a git checkout (developers)

```bash
git clone https://github.com/ajitesh1020/linuxcnc-status-monitor.git
cd linuxcnc-status-monitor
python3 status.py --dev          # while LinuxCNC is running (or started later)
```

Build the `.deb` yourself with `bash packaging/deb/build.sh` (output in `dist/`).

---

## Networking — no static IP needed

By default the agent **broadcasts** its packets on every LAN / Wi-Fi interface.
Any dashboard on the same network receives them, whatever IP it currently has,
so nothing needs to change when the office PC's address changes.

- The Mesa `hm2_eth` card's interface is detected and **never** broadcast on.
- A fixed IP or a host name can still be used (`monitor_pc_ip`).
- Details, Wi-Fi notes and when a static IP makes sense:
  [`docs/NETWORK.md`](docs/NETWORK.md).

---

## Features

- **Zero G-code changes** — program completion detected from the `M2`/`M30`
  program end; no custom M-codes
- **Accurate part counting** — a run to `M2`/`M30` counts as a part; an
  interrupted part finished with *Run From Here* still counts once; a part
  abandoned by restarting from the top is recorded as a **partial part**
- **Real-time stream** — LinuxCNC read at 10 Hz, one JSON packet per second
- **Idle suppression** — one heartbeat every 30 s while idle
- **G-code file streaming** — full program sent once on load, again on change
- **Cycle time tracking** — millisecond precision, pause-aware
- **Errors** — E-stop, execution errors, joint faults, hard limits, and any NML
  messages the agent catches
- **Fault-tolerant** — every LinuxCNC call guarded; the agent can never crash LinuxCNC

---

## How parts are counted

The agent scans the loaded file for the first move, the **last move before
`M2`/`M30`**, and the `M2`/`M30` line itself, then watches the executed line
numbers.

| What happens on the machine | Result |
|---|---|
| Top → `M2`/`M30` | 1 part |
| Top → stopped midway → Run From Here → `M2`/`M30` | 1 part (time carried over), 1 abort |
| Top → stopped midway → started again from top | partial part with % done (e.g. 0.5), then a new part |
| E-stop / machine off during a run | abort |

See [`program_tracker.py`](program_tracker.py) for the exact rules.

---

## Repository structure

```
linuxcnc-status-monitor/
├── status.py                  # The agent (main loop, state machine, packets)
├── program_tracker.py         # M2/M30 + Run From Here detection from line numbers
├── cycle_time_calculator.py   # Cycle timing, parts / aborts / partial parts
├── agent_net.py               # Where packets go (broadcast / IP / host name)
├── agent_runtime.py           # LinuxCNC process watch, config location, lock
├── config.example.yaml        # Settings template
├── PROTOCOL.md                # Normative UDP wire protocol (agent ↔ dashboard)
├── packaging/                 # .deb build script, user service, wrapper
├── docs/                      # CONFIGURATION, NETWORK, TROUBLESHOOTING
├── examples/udp_receiver.py   # Minimal receiver for testing on any PC
└── tests/                     # python3 -m unittest discover -s tests
```

---

## Receiving data without the dashboard

```bash
python3 examples/udp_receiver.py            # compact live summary
python3 examples/udp_receiver.py --pretty   # full JSON + rotating log
```

This is also the quickest way to check the network: if it shows packets, the
dashboard will too.

---

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

| Symptom | Likely cause |
|---|---|
| Nothing arrives at the dashboard | Windows firewall / network set to *Public*, or Wi-Fi "client isolation" — see NETWORK.md |
| `lcnc-status-agent is already running` | The service is running; stop it before a manual `--dev` run |
| Parts not counted | No `M2`/`M30` in the file — check `lcnc-status-agent --dev` output |
| Machine shows twice in the dashboard | An old launcher also starts `status.py` — remove it |

---

## Contributing

1. Fork the repo
2. Branch: `git checkout -b feature/your-feature`
3. Follow PEP 8; run `python3 -m unittest discover -s tests`
4. Test on real LinuxCNC or sim: `linuxcnc -l`
5. Submit a pull request

---

## License

**GPL-2.0-or-later** — see [LICENSE](LICENSE).

Copyright (c) 2025-2026 Ajitesh Kannojia (CNC Tool Tech).

This agent imports the LinuxCNC Python module (`import linuxcnc`), which is
licensed under the GNU General Public License. A program that links GPL code
must itself be distributed under the GPL, so this project is GPL — not MIT.
The paid monitoring dashboard is a **separate, independent program** that
communicates only over the network (UDP/JSON) and is not covered by this license.
