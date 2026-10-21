# Bench test — agent 1.6.0 on a real LinuxCNC machine

Covers the agent fixes from the industrial readiness review of 21 Oct 2026
(F01 error channel, F04 idle faults, F06 history server, and the `session_id` /
`packet_seq` fields for F02) on real hardware. The unit tests use a fake
LinuxCNC; these checks are what they cannot prove. Record results in the table at
the end and keep it with the release evidence.

**Safety:** this is a monitoring program. Run these checks with the machine
under your normal supervision, spindle off and the table clear. Use E-stop as you
normally would — nothing here depends on the agent.

## 0. Where things stand (21 Oct 2026)

| Piece | State |
|---|---|
| F01 / F04 / F06 | Merged to `main` (PR #11), CI passed |
| `session_id` / `packet_seq` | Branch `feat/agent-session-packet-seq` (on top of `main`) |
| FloorWatch side (F02 ordering, F03 gap recovery, F07 count bound) | On the Windows PC, FloorWatch worktree `floorwatch-project-update-dda415`, not committed yet; needs these agent fields for F02 |
| Not started | F05 (G-code chunk limit), F08 (OEE), most "additional hardening" |

## 1. Install the branch

```bash
git clone https://github.com/ajitesh1020/linuxcnc-status-monitor.git
cd linuxcnc-status-monitor
git switch feat/agent-session-packet-seq
python3 -m unittest discover -s tests          # expect: OK (skipped=1)
bash packaging/deb/build.sh                    # needs dpkg-deb
sudo apt install ./dist/linuxcnc-status-agent_1.6.0_all.deb
systemctl --user restart lcnc-status-agent
lcnc-status-agent --version                    # 1.6.0
lcnc-status-agent --check                      # error channel : not read (default)
```

To watch it live instead of as a service:

```bash
systemctl --user stop lcnc-status-agent
lcnc-status-agent --dev
# ... later
systemctl --user start lcnc-status-agent
```

Watch the packets on the same PC (second terminal):

```bash
python3 examples/udp_receiver.py --fields ts session_id packet_seq cycle_state estop task_state exec_state cmd_state cmd_serial nml_errors journal_seq journal_port
```

## 2. F01 — the operator's screen keeps every message

Start LinuxCNC with the GUI you ship (AXIS, QtDragon, Gmoccapy…), agent running.
Do each 10 times and count how often the GUI shows the message:

| Action (MDI) | Expected on the GUI |
|---|---|
| `(MSG, bench test)` | message shown |
| `(DEBUG, bench test)` | message shown |
| `G1 X1` with no feed set (`F0` / fresh start) | error shown (feed rate missing) |
| `M999` (an M-code that does not exist) | error shown |

Pass: 10/10 for every row, with the agent running. Repeat once with the agent
stopped for comparison. Optional: set `read_error_channel: true` in
`~/linuxcnc-monitor-agent/config.yaml`, restart the agent and repeat — the GUI may
now miss some; that is why it is off. Set it back to `false`.

Also confirm in the receiver output that `nml_errors` is always `[]`, and that
each G-code error (rows 3 and 4) sends a packet at once with `cmd_state: 3` and a
new `cmd_serial`, and the next good command sends one with `cmd_state: 1`.
(`exec_state` does not go to `1` for these errors on LinuxCNC 2.9.)

## 3. F04 — idle faults arrive at once, both edges

Machine ON, idle (no program running). In the receiver:

1. Press E-stop, release and reset within about 2 s.
2. Trigger a fault the machine can produce safely while idle, if any (for example a
   limit switch by hand, or an amplifier-enable fault on a test drive).

Pass: a packet with `estop: true` within about 1 s of pressing, and one with
`estop: false` within about 1 s of reset — not 30 s later at the heartbeat. Same for
joint faults (`joints[n].fault`) and limits (`hard_limit`).

Then check both edges reached the journal:

```bash
sqlite3 ~/linuxcnc-monitor-agent/journal.db \
  "SELECT seq, ts_ms, json_extract(data,'$.estop'), json_extract(data,'$.exec_state')
   FROM entries ORDER BY seq DESC LIMIT 6"
```

## 4. F06 — bad history requests cannot stop the history server

```bash
python3 - <<'EOF'
import json, socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
for raw in [b'{"type":"journal_request","from_seq":1e309}',
            b'{"type":"journal_request","from_seq":99999999999999999999999}',
            b'[' * 3000 + b']' * 1000, b'\xff\xfe']:
    s.sendto(raw, ("127.0.0.1", 5006))
def ask(from_seq, to_seq):
    s.sendto(json.dumps({"type": "journal_request", "from_seq": from_seq,
                         "to_seq": to_seq}).encode(), ("127.0.0.1", 5006))
    return json.loads(s.recvfrom(65535)[0])
first = ask(0, 0)["first_seq"]           # oldest entry still kept (older ones are pruned)
print([e[0] for e in ask(first - 1, first + 2)["entries"]])   # must print 3 seqs
EOF
```

Pass: the good requests are answered (the bad ones get no reply), the last line
prints three seq numbers, and the receiver keeps showing `journal_port: 5006`
(it shows `0` only while the history server is down).

## 5. Stream identity (`session_id`, `packet_seq`)

- `packet_seq` goes up by 1 with every status packet.
- `systemctl --user restart lcnc-status-agent`: `session_id` changes and
  `packet_seq` starts again at 1; `parts_produced` restarts at 0.
- Restarting **LinuxCNC only** (agent keeps running): same `session_id`.

## 6. No effect on the machine (review gate 2)

Run LinuxCNC's latency test **without** LinuxCNC running first (it must never run
alongside LinuxCNC — see the LinuxCNC latency-test docs) to get the PC's baseline.
Then, with LinuxCNC running, compare the servo thread's worst time with the agent
stopped and running, each over the same 10 minutes of a real program:

```bash
halcmd show param servo-thread.tmax    # reset between runs: halcmd setp servo-thread.tmax 0
top -b -n 1 | grep -i status.py        # agent CPU
```

Pass: no meaningful change in `servo-thread.tmax`, no following errors, agent CPU
low (a few % at most). Note the PC model, CPU, LinuxCNC version, GUI and Mesa card.

## 7. Results

| Check | Date | Result | Notes |
|---|---|---|---|
| Install / `--check` | 2026-10-21 | Pass | 1.6.0 installed over 1.5.0; error channel not read; history port 5006; 66 unit tests OK (none skipped, LinuxCNC is installed) |
| F01 messages (per GUI) | 2026-10-21 | GUI: pass. Packets: **fail** | AXIS showed 10/10 for MSG, DEBUG, zero-feed G1 and M999, with the agent running and stopped (checked by screenshot). `nml_errors` always `[]`. But `exec_state` never goes to 1 on an MDI error in LinuxCNC 2.9.10 (7 → 2, polled every 1 ms), so no packet shows the error. `stat.state` goes to 3 (RCS_ERROR) and stays until the next good command. **Fixed in 21d30f7**: packets now carry `cmd_state` / `cmd_serial`; on the machine two M999 then a good command gave three packets within ~100 ms (cmd_state 3, 3, 1). Optional `read_error_channel: true` run not done |
| F04 E-stop edges | 2026-10-21 | Pass | Software E-stop (no hardware on the port), reset within 2 s: packets 217 ms after press, 254 ms after reset + ON; both edges journalled |
| F04 joint fault / limit | 2026-10-21 | Pass | Simulated with `halcmd setp` (pins unconnected): j0 `neg-lim-sw-in` on/off → 93 / 11 ms; j1 `amp-fault-in` on/off → 54 / 72 ms; a 0.3 s amp fault → 133 / 32 ms. All edges journalled with the right `joints` entry |
| F06 bad requests | 2026-10-21 | Pass | Answered after the bad requests, same PID, still on 5006 |
| Session / packet_seq | 2026-10-21 | Pass | +1 per packet over 220+ packets, no gaps; agent restart → new `session_id`, seq from 1; LinuxCNC-only restart → same `session_id`. `parts_produced` reset not shown (was already 0) |
| Latency / CPU | 2026-10-21 | CPU pass; latency inconclusive | Baseline (no LinuxCNC, 10 min): base jitter max 76.0 µs, servo 65.4 µs. 10 min of the PCB program each (`servo-thread.tmax`, CPU clocks; ≈ µs at 3.7 GHz): agent stopped 1 123 180 (≈ 304 µs), running 2 083 656 (≈ 563 µs); `base-thread.tmax` 276 296 vs 268 529 (≈ 75 / 73 µs). A 20 s dry run earlier went the other way (3.67 M stopped, 1.10 M running), so one max per run is noise-dominated on this non-isolated PC. No following errors, no aborts, max ferror 0.042 mm both runs. Agent CPU 0.66 % average (top shows 6.7 % at the 60 s G-code resend). Repeat on OFC_PC with several alternating runs |

LinuxCNC version: 2.9.10 · GUI: AXIS (config `test_config`, no hardware on the port) · Mesa / parport: parport `0xe100` · PC: Dell OptiPlex 9020, Intel i3-4170

Also seen: a large loaded program (2,760 chunks) goes out in one ~38 ms burst
and again every 60 s; check that FloorWatch receives all of it (see F05).

## Continuing with Claude Code on the machine PC

Conversation history stays on the PC where it happened, so the Windows
session cannot be resumed here. Start a new one in this repo:

```bash
cd linuxcnc-status-monitor && git switch feat/agent-session-packet-seq
claude
```

and tell it: *"Read docs/BENCH_TEST_1.6.0.md and the industrial readiness review
notes in it; help me run the bench test and fix what fails."* Claude Code can run
the safe, read-only checks above (§1, §4, §5, journal queries). The steps that
press E-stop, run G-code or run the latency test are yours to do at the machine.
