# Fleet test on the LinuxCNC PC — FloorWatch + 100 machines on one PC

Runs the whole system on the LinuxCNC test PC: the real agent (with LinuxCNC),
99 simulated agents (`tools/fleet_sim.py`), and FloorWatch receiving all of it
(`tools/soak_run.py --external`). Linux `tc netem` adds packet loss, reordering,
duplication and outages, so the review's gate 3 (network / data failure) can be
run on demand. `fleet_sim.py check` then compares FloorWatch's history database
with what the machines really did: parts, aborts, E-stops, failed commands and
joint faults must match **exactly**.

## What this covers, and what it does not

| Covered here | Still needed elsewhere |
|---|---|
| Real agent + 99 simulated agents into the real FloorWatch packet path (order filter, history backfill, failed commands) | Windows-only parts: installer, firewall rule, start with Windows, Windows Update / reboot |
| Loss, reordering, duplication, 30 s and long outages, dashboard restarts, pruned history | The real network: broadcast over several network cards, Mesa card exclusion, switches, Wi-Fi |
| Exact part / event reconciliation (`check`) | Performance gates (frame < 50 ms, RAM ≤ 300 MB, 72 h soak) — defined on the **Windows reference PC**; Linux numbers are only indicative |
| Large real G-code programs (shows F05) | Several GUIs / real hardware E-stop and drives (`docs/BENCH_TEST_1.6.0.md`) |

**Safety.** Use only a test PC that does not control a real machine during the
run. 100 simulated machines and the dashboard use CPU and can disturb
LinuxCNC's real-time threads, so do **not** take latency measurements during a
fleet run — do those separately (bench test §6). netem changes networking on
the PC until removed: always run `sudo tc qdisc del dev lo root` at the end.

## 1. Set up (once)

```bash
sudo apt install -y python3-pip python3-venv libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 sqlite3
git clone https://github.com/ajitesh1020/linuxcnc-status-monitor.git
git clone https://github.com/ajitesh1020/floorwatch.git            # needs access to the repo
cd floorwatch && git switch fix/review-f02-f03-f07                  # until PR #25 is merged
python3 -m venv ~/fw-venv && ~/fw-venv/bin/pip install -r requirements.txt -c constraints-release.txt
```

Use the release constraints (PySide6 6.11.2): PySide6 6.12.0, which a bare
`pip install` picks, crashed FloorWatch here within a minute
(`Fatal Python error: none_dealloc` while painting a tile).

Point the real agent at this PC only, so its packets do not also go to the
office: in `~/linuxcnc-monitor-agent/config.yaml` set `monitor_pc_ip: "127.0.0.1"`,
then `systemctl --user restart lcnc-status-agent`. (Set it back to `auto` after.)

## 2. Start FloorWatch (terminal 1)

```bash
cd ~/floorwatch
QT_QPA_PLATFORM=offscreen ~/fw-venv/bin/python tools/soak_run.py --external \
    --host 0.0.0.0 --port 5005 --hours 3 --log-interval 60 --dir ~/fw-test/run1
```

Leave out `QT_QPA_PLATFORM=offscreen` to watch the window (Plant tier, nothing
capped). It prints the history database path (`~/fw-test/run1/history.db`).

## 3. Start the fleet (terminal 2)

```bash
cd ~/linuxcnc-status-monitor
python3 tools/fleet_sim.py run --machines 99 --minutes 60 --settle 180 \
    --target 127.0.0.1:5005 --dir ~/fw-test/fleet1 \
    --gcode ~/linuxcnc/nc_files/*.ngc --seed 1
```

Machines are `SIM-001`…`SIM-099`, history ports 6100…6198 on 127.0.0.1. Defaults:
cycles of 20–60 s, 3–15 s between them, 5 % aborts, per machine-hour 2 E-stops,
2 joint faults, 4 failed commands (30 % of them twice in a row) and 0.3 agent
restarts. Turn rates up for a short, busy run (for example
`--cycle 4,10 --idle 1,4 --estop-rate 60 --cmd-error-rate 90 --restart-rate 10`).
The last `--settle` seconds are quiet (heartbeats only) so FloorWatch can fetch
anything it missed; keep netem **off** for the settle when a scenario says so.

Run the real machine too: load a program in AXIS and run it a few times; count
the finished parts yourself.

## 4. Check (after the fleet run ends)

```bash
python3 tools/fleet_sim.py check --truth ~/fw-test/fleet1/truth.json --db ~/fw-test/run1/history.db
```

Pass: `99 of 99 machines match exactly`. A mismatch lists the machine and the
counts (dashboard / truth). For the real machine, compare FloorWatch's part
count with your own count.

## 5. Scenarios

netem applies to everything sent **to** UDP port 5005 on the loopback interface
(status packets, G-code chunks and history replies, all heading to FloorWatch);
nothing else on the PC is touched:

```bash
# set up once per scenario
sudo tc qdisc add dev lo root handle 1: prio bands 4
sudo tc filter add dev lo parent 1:0 protocol ip prio 1 u32 \
    match ip protocol 17 0xff match ip dport 5005 0xffff flowid 1:4
# then the impairment for that scenario, e.g.
sudo tc qdisc add dev lo parent 1:4 handle 40: netem loss 10%
# change it during a run
sudo tc qdisc change dev lo parent 1:4 handle 40: netem loss 100%
# remove everything (always, at the end)
sudo tc qdisc del dev lo root
```

Use a fresh `--dir` for both tools in each scenario (`run2`, `fleet2`, …).

| # | Scenario | netem / action | Expected |
|---|---|---|---|
| A | Baseline | none | `check` exact; no `history_gaps` rows |
| B | Steady loss | `netem loss 10%` for the whole busy time, off for the settle | exact |
| C | Heavy loss | `netem loss 30%`, off for the settle | exact |
| D | Reordering | `netem delay 20ms reorder 25% 50%` | exact; in the window no machine's state ever steps back |
| E | Duplication | `netem duplicate 10%` | exact |
| F | 30 s outage | `loss 100%` for 30 s mid-run (`change`), then `loss 0%` | exact — the review's F03 case |
| G | Long outage | `loss 100%` for 10 min, then `loss 0%` | exact; FloorWatch log shows the backfill |
| H1 | Dashboard stops cleanly | start terminal 1 with `--hours 0.25`; when it ends, start it again with the **same** `--dir` | exact parts; aborts may be 1 short for a machine whose first missed entry was an abort |
| H2 | Dashboard crashes | `pkill -9 -f soak_run.py` mid-run, then start it again with the same `--dir` | as H1 (the saved position is at most ~10 s old; re-fetching is harmless) |
| I | Pruned history | stop FloorWatch; `sqlite3 ~/fw-test/fleetN/SIM-001.journal.db "DELETE FROM entries WHERE seq <= (SELECT MIN(seq)+20 FROM entries)"`; start FloorWatch again | `history_gaps` has a `pruned` row for SIM-001; its other counts may be short by what was deleted; every other machine exact |
| J | Everything at once | `netem loss 10% delay 20ms reorder 25% 50% duplicate 5%` | exact |
| K | Big programs (F05) | `--gcode` with a > 4 MB program | known open: FloorWatch log shows `gcode_file … refused: … chunks claimed (limit 4096)`; status and counts still exact |
| L | Load (indicative) | baseline with `--machines 99`, 1 hour | `python3 tools/soak_report.py ~/fw-test/runN/floorwatch/logs --interval 60`; note CPU / memory; agent PC stays responsive |

`check` can also be run during a busy run (the truth file is rewritten every
30 s, `final: false`), but counts only settle after the quiet period.

## 6. Record

| Scenario | Date | Machines | Result of `check` | Notes (FloorWatch log warnings, gaps, CPU) |
|---|---|---|---|---|
| A | 21–22 Oct 2026 | 99 sim, 60 min + 180 s, defaults, seed 1 | **99 of 99 exact** — parts 6938/6938, aborts 579/579, E-stops 192/192, cmd fail 95/95, joint 31/31; sender 301907 sent, 0 dropped, 0 swapped | 0 `history_gaps`; no warnings in FloorWatch log. Short run first (20 sim, 5 min, busy rates): 20 of 20 exact after the fleet_sim fix below |
| B | | | | |
| C | | | | |
| D | | | | |
| E | | | | |
| F | | | | |
| G | | | | |
| H1 | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 2 | 97 of 99 exact — parts 3510/3510, aborts 280/282, E-stops 99/99, cmd fail 46/46, joint 27/27; 0 dropped / swapped | Known case, passes: FloorWatch ran `--hours 0.25`, down 60 s, restarted on the same `--dir`. SIM-062 and SIM-080 are 1 abort short each; for both the first missed journal entry is the abort (saved position 99 → abort at seq 100, 01:25:27.6; 97 → abort at 98, 01:25:29.4; FloorWatch stopped 01:25:29). Restart log: `Backfilling SIM-062: journal 99..107`, `SIM-080: journal 97..105`. 0 `history_gaps` |
| H2 | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 3 | **99 of 99 exact** — parts 3496/3496, aborts 277/277, E-stops 93/93, cmd fail 46/46, joint 21/21; 0 dropped / swapped | FloorWatch `kill -9` at 15 min, down 60 s, restarted on the same `--dir`; backfill recovered everything. 0 `history_gaps`, no warnings |
| I | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 4 | 97 of 99 exact — parts 3538/3538, aborts 240/242, E-stops 92/92, cmd fail 46/46, joint 17/17 | Passes. FloorWatch ran 3 min (saved SIM-001 at seq 20), down 5 min; SIM-001 entries 1–21 deleted (journal 22..54 left). Log: `History for SIM-001 not recoverable: journal 20..21 (pruned)`; one `history_gaps` row (SIM-001, 20–21, `pruned`); SIM-001 itself still exact (seq 21 held no count change). SIM-010 and SIM-094 1 abort short each = the H case: saved position 20, abort at seq 21 (02:26:24.6 / 02:26:27.5), FloorWatch stopped 02:26:24. Also one unprompted backfill (`SIM-094: journal 158..160`) with no netem — a loopback packet loss, recovered |
| J | | | | |
| K | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 5; `--gcode` adds a 5 MB program (10 machines get it) | **99 of 99 exact** — parts 3501/3501, aborts 260/260, E-stops 91/91, cmd fail 48/48, joint 21/21 | Known open (F05), as expected: `gcode_file from SIM-0x1 refused: 5243 chunks claimed (limit 4096)` for all 10 machines. Status and counts unaffected. Note: the refusal is logged on every resend (60 s), 30× per machine, 300 warnings in 33 min — log noise to fix with F05 |
| L | 21–22 Oct 2026 | = run A (99 sim, 1 h) | `soak_report`: PASSED (memory, CPU, handles, threads within §12) | Indicative only (Linux, not the Windows reference PC): RSS 150 → 213 MB, CPU ~4 % of one core, 19–23 handles, 6 threads, DB 35 MB/h. fleet_sim ~25 % of one core. Real agent idle (LinuxCNC not running) |

LinuxCNC version: · PC: · FloorWatch commit: · agent commit:

After the last run: `sudo tc qdisc del dev lo root`, and set `monitor_pc_ip` back
in the agent's config.
