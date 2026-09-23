# Troubleshooting  —  v1.4.0

Start here — it answers most questions in one go:

```bash
lcnc-status-agent --check
```

```
lcnc-status-agent 1.4.0
  config file   : /home/indus/linuxcnc-monitor-agent/config.yaml
  machine name  : CNC-01
  monitor_pc_ip : auto
  sending to    : 192.168.0.255 port 5005
  LinuxCNC      : running
```

---

## Nothing arrives at the dashboard

1. `lcnc-status-agent --check` — is "sending to" a broadcast address on the
   same network as the office PC (e.g. `192.168.0.255` when the office PC is
   `192.168.0.x`)?
2. Is the service running? `systemctl --user status lcnc-status-agent`
3. Windows firewall / network profile on the office PC — see
   [NETWORK.md](NETWORK.md#windows-firewall-dashboard-pc).
4. Wi-Fi with client isolation — see [NETWORK.md](NETWORK.md#wi-fi-notes).
5. Watch the agent live:

   ```bash
   systemctl --user stop lcnc-status-agent
   lcnc-status-agent --dev          # look for "Packet SENT"
   systemctl --user start lcnc-status-agent
   ```

---

## `lcnc-status-agent is already running`

The background service is already running (only one agent per user so parts
are never double-counted). Stop it first for a manual run — see above.

---

## The machine appears twice / counts jump around

Two agents are sending under the same name — usually an **old launcher**
(`launch_ofc.sh`, `OFC_PC.desktop`, or an autostart entry running `status.py`).
The `.deb` installer lists any it finds. Delete them and start LinuxCNC
normally; the service handles the agent.

```bash
pgrep -af status.py      # should show only /usr/share/linuxcnc-status-agent/status.py
```

---

## `[FATAL] Could not import 'linuxcnc'`

The agent must run on the LinuxCNC PC.

```bash
python3 -c "import linuxcnc; print('OK')"
```

For LinuxCNC built from source, see "run-in-place" in
[CONFIGURATION.md](CONFIGURATION.md).

---

## Parts are not counted

Run `lcnc-status-agent --dev` and look for the program scan when the file loads:

```
Program scan: file=part_A.ngc first_exec=2 first_move=6 tail_move=2692 end=2693
```

- `end=-1` → the file has no `M2` / `M30`. Add one as the last command.
- At cycle end you should see `Part COUNTED (#n)`. If you see
  `End line NOT reached — ABORT recorded` instead, the program stopped before
  its last move (Stop, E-stop, error).
- Very short test programs (< 1 s) are ignored (`MIN_VALID_CYCLE_MS`).

## Partial parts

A part stopped midway and then **restarted from the top** (instead of being
finished with Run From Here) is recorded as a partial part with how far it
got. That's intended — finish it with Run From Here to count it as a part.

---

## Log files (dev mode only)

```bash
tail -f /tmp/cnc_status.log
```

Production runs are silent by design.
