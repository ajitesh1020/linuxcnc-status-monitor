# Configuration Reference  —  v1.4.0

Settings live in **`~/linuxcnc-monitor-agent/config.yaml`**. The agent creates
it from [`config.example.yaml`](../config.example.yaml) the first time it runs;
package upgrades never overwrite it.

The agent looks for its config in this order and uses the first it finds:

1. `--config /path/to/config.yaml`
2. `~/linuxcnc-monitor-agent/config.yaml`
3. `/etc/linuxcnc-status-agent/config.yaml` (optional machine-wide file)
4. `config.yaml` next to `status.py` (git checkout)

Apply changes with:

```bash
systemctl --user restart lcnc-status-agent
lcnc-status-agent --check        # shows the file in use and where packets go
```

Missing keys use the defaults below; unknown keys are ignored. PyYAML is used if
installed, otherwise a built-in parser reads the file — keep it flat.

---

## Keys

| Key | Type | Default | Description |
|---|---|---|---|
| `monitor_pc_ip` | string | `"auto"` | Where to send: `auto` (LAN broadcast), an IP, a host name, or a comma-separated mix. See [NETWORK.md](NETWORK.md) |
| `monitor_pc_port` | int | `5005` | UDP port the dashboard listens on |
| `exclude_interfaces` | string | `""` | Interfaces never to broadcast on, comma-separated. The Mesa `hm2_eth` interface is skipped automatically |
| `machine_name` | string | `""` | Name shown in the dashboard; empty = the PC's hostname. Must be unique per machine |
| `poll_interval_s` | float | `1.0` | Seconds between status packets while active |
| `sample_interval_s` | float | `0.1` | Seconds between LinuxCNC reads for cycle / part tracking |
| `idle_heartbeat_interval_s` | float | `30.0` | Keep-alive interval while idle |
| `gcode_chunk_size` | int | `50000` | Max bytes per UDP G-code chunk |
| `log_file` | string | `"/tmp/cnc_status.log"` | Log path (dev mode only) |
| `log_max_bytes` | int | `5242880` | Rotate the log after this many bytes (dev mode) |
| `log_backup_count` | int | `3` | Rotated logs to keep (dev mode) |

---

## Service

The `.deb` installs a **per-user systemd service** that starts at login:

```bash
systemctl --user status  lcnc-status-agent
systemctl --user restart lcnc-status-agent
systemctl --user stop    lcnc-status-agent   # e.g. before a manual --dev run
```

It waits for LinuxCNC's task server (`linuxcncsvr` / `milltask`), connects
when LinuxCNC starts — however it is launched — and disconnects when it exits.
Only one agent can run per user; a second copy exits with a message.

### LinuxCNC built from source (run-in-place)

If `python3 -c "import linuxcnc"` fails outside the RIP shell, point the
service at your build:

```bash
systemctl --user edit lcnc-status-agent
```

```ini
[Service]
Environment=PYTHONPATH=/home/<you>/linuxcnc-dev/lib/python
```

---

## Code constants (`cycle_time_calculator.py`)

```python
MIN_VALID_CYCLE_MS = 1_000   # cycles shorter than this are ignored (test jogs)
MAX_HISTORY        = 500     # completed / aborted durations kept in memory
```

Program-end and Run From Here detection rules are documented at the top of
[`program_tracker.py`](../program_tracker.py).

---

## udp_receiver.py (any PC)

| Flag | Default | Description |
|---|---|---|
| `--port` | `5005` | UDP port to listen on |
| `--pretty` | off | Pretty JSON + rotating log file |
| `--log` | `/tmp/udp_receiver_pretty.log` | Log path for `--pretty` |
| `--fields FIELD ...` | — | Print only named fields |
| `--save-gcode DIR` | — | Save received G-code files to disk |
