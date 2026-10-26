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
| B | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 9 | **99 of 99 exact** — parts 3523/3523, aborts 258/258, E-stops 85/85, cmd fail 62/62, joint 15/15 | netem `loss 10%` for the busy 30 min, `loss 0%` for the settle; netem dropped 102710 of 988199 packets. 455 backfills, 0 `history_gaps`, no warnings. Run in a private network namespace (see note below the table) |
| C | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 10 | **99 of 99 exact** — parts 3486/3486, aborts 269/269, E-stops 93/93, cmd fail 59/59, joint 12/12 | netem `loss 30%` busy, off for the settle; netem dropped 308808 of 783398 packets. 1249 backfills, 0 `history_gaps`, no warnings. Namespace run |
| D | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 11 | **99 of 99 exact** — parts 3506/3506, aborts 265/265, E-stops 99/99, cmd fail 67/67, joint 19/19 | netem `delay 20ms reorder 25% 50%` busy, `delay 0ms` for the settle; 71254 packets sent out of order (netem requeues), 0 dropped. 39 backfills, 0 `history_gaps`, no warnings. Window not watched (offscreen), so "state never steps back" is shown only by the exact counts. Namespace run |
| E | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 12 | **99 of 99 exact** — parts 3534/3534, aborts 271/271, E-stops 99/99, cmd fail 34/34, joint 16/16 | netem `duplicate 10%` busy, off for the settle; 1192791 packets through netem (~9 % more than D). Nothing counted twice. 28 backfills, 0 `history_gaps`, no warnings. Namespace run |
| F | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 13 | **99 of 99 exact** — parts 3506/3506, aborts 253/253, E-stops 101/101, cmd fail 49/49, joint 19/19 | netem `loss 100%` 06:24:30–06:25:00 (15 min in), then `loss 0%`; 3863 packets lost. Log: 101 `Backfilling …` at 06:25 (every machine), no warnings, 0 `history_gaps`. F03 case passes. Namespace run |
| G | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 14 | **99 of 99 exact** — parts 3494/3494, aborts 278/278, E-stops 109/109, cmd fail 55/55, joint 21/21 | netem `loss 100%` for 10 min (06:19:30–06:29:30), then `loss 0%`; 323055 packets lost. Log shows the backfill: 104 `Backfilling …` at 06:29, no warnings, no "still failing", 0 `history_gaps`. Namespace run |
| H1 | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 2 | 97 of 99 exact — parts 3510/3510, aborts 280/282, E-stops 99/99, cmd fail 46/46, joint 27/27; 0 dropped / swapped | Known case, passes: FloorWatch ran `--hours 0.25`, down 60 s, restarted on the same `--dir`. SIM-062 and SIM-080 are 1 abort short each; for both the first missed journal entry is the abort (saved position 99 → abort at seq 100, 01:25:27.6; 97 → abort at 98, 01:25:29.4; FloorWatch stopped 01:25:29). Restart log: `Backfilling SIM-062: journal 99..107`, `SIM-080: journal 97..105`. 0 `history_gaps` |
| H1 (fix) | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 2; FloorWatch `fix/backfill-restart-baseline` (dd705d6) | **99 of 99 exact** — parts 3503/3503, aborts 284/284, E-stops 95/95, cmd fail 36/36, joint 17/17 | Same procedure as H1. Note: in this run no machine's first missed entry held an abort or E-stop, so the trigger was not hit; see H1 stress |
| H1 stress (fix) | 22 Oct 2026 | 99 sim, 20 min + 180 s, seed 16, `--cycle 4,10 --idle 1,4 --abort-prob 0.5 --estop-rate 60 --cmd-error-rate 90 --restart-rate 10`; FloorWatch dd705d6 | **99 of 99 exact** — parts 6408/6408, aborts 7979/7979, E-stops 1836/1836, cmd fail 1078/1078, joint 18/18 | H1 procedure with busy rates so the trigger is hit: 18 machines' first missed entry after the restart was an abort (16) or E-stop (5) (e.g. SIM-004, SIM-055, SIM-053). All exact — before the fix each would have been 1 short |
| H2 | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 3 | **99 of 99 exact** — parts 3496/3496, aborts 277/277, E-stops 93/93, cmd fail 46/46, joint 21/21; 0 dropped / swapped | FloorWatch `kill -9` at 15 min, down 60 s, restarted on the same `--dir`; backfill recovered everything. 0 `history_gaps`, no warnings |
| I | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 4 | 97 of 99 exact — parts 3538/3538, aborts 240/242, E-stops 92/92, cmd fail 46/46, joint 17/17 | Passes. FloorWatch ran 3 min (saved SIM-001 at seq 20), down 5 min; SIM-001 entries 1–21 deleted (journal 22..54 left). Log: `History for SIM-001 not recoverable: journal 20..21 (pruned)`; one `history_gaps` row (SIM-001, 20–21, `pruned`); SIM-001 itself still exact (seq 21 held no count change). SIM-010 and SIM-094 1 abort short each = the H case: saved position 20, abort at seq 21 (02:26:24.6 / 02:26:27.5), FloorWatch stopped 02:26:24. Also one unprompted backfill (`SIM-094: journal 158..160`) with no netem — a loopback packet loss, recovered |
| J | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 15 | **99 of 99 exact** — parts 3494/3494, aborts 275/275, E-stops 103/103, cmd fail 56/56, joint 24/24 | netem `loss 10% delay 20ms reorder 25% 50% duplicate 5%` busy, `delay 0ms` for the settle; 102025 packets lost, 66947 out of order. 412 backfills, 0 `history_gaps`, no warnings. Namespace run |
| K | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 5; `--gcode` adds a 5 MB program (10 machines get it) | **99 of 99 exact** — parts 3501/3501, aborts 260/260, E-stops 91/91, cmd fail 48/48, joint 21/21 | Known open (F05), as expected: `gcode_file from SIM-0x1 refused: 5243 chunks claimed (limit 4096)` for all 10 machines. Status and counts unaffected. Note: the refusal is logged on every resend (60 s), 30× per machine, 300 warnings in 33 min — log noise to fix with F05 |
| B* | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 6, **`--drop 0.1`** (stand-in, not netem) | **99 of 99 exact** — parts 3537/3537, aborts 261/261, E-stops 96/96, cmd fail 40/40, joint 21/21; sender 136446 sent, 15275 dropped | Stand-in only: sudo needs a password on this PC, so netem B could not run. Sender loss hits status packets only (not G-code chunks or history replies) and stays on through the settle. 542 backfills, 0 `history_gaps`, no warnings |
| D* | 22 Oct 2026 | 99 sim, 30 min + 180 s, defaults, seed 7, **`--reorder 0.25`** (stand-in, not netem) | **99 of 99 exact** — parts 3514/3514, aborts 269/269, E-stops 91/91, cmd fail 40/40, joint 16/16; sender 152050 sent, 30503 swapped | Stand-in for D (no netem). The window-never-steps-back check needs the visible window and was not judged |
| L | 21–22 Oct 2026 | = run A (99 sim, 1 h) | `soak_report`: PASSED (memory, CPU, handles, threads within §12) | Indicative only (Linux, not the Windows reference PC): RSS 150 → 213 MB, CPU ~4 % of one core, 19–23 handles, 6 threads, DB 35 MB/h. fleet_sim ~25 % of one core. Real agent idle (LinuxCNC not running) |

LinuxCNC version: 2.9.10 (not running during the runs; the real agent was idle) · PC: Dell OptiPlex 9020, 4 cores, 3.7 GB, Debian 12, kernel 6.1.0-53-rt-amd64 · FloorWatch commit: 991efd5 (`fix/review-f02-f03-f07`) for A, H1, H2, I, K, L, B\*, D\*; dd705d6 (`fix/backfill-restart-baseline` = 991efd5 + the restart-baseline fix) for H1 (fix), H1 stress, B–G, J; PySide6 6.11.2 · agent commit: `feat/fleet-sim` 80eebcf (fleet_sim fix) onward

**How netem was run here (22 Oct 2026).** `sudo` needs a password on this PC, so B–G and J ran in a private
network namespace instead of on the host's `lo`: `unshare -rn` (an unprivileged user + network namespace)
gives FloorWatch and the fleet their own loopback, where `tc` works without root. The §5 filter and netem
lines were applied unchanged inside it; the host's networking was never touched and the namespace vanished
with the run. The real agent cannot reach FloorWatch in there, which did not matter (LinuxCNC was not
running). Script: `unshare -rn` → `ip link set lo up` → §5 `tc` lines → start FloorWatch and the fleet →
`tc qdisc change … netem …` on schedule → `tc -s qdisc show dev lo` for the drop count.

**Findings from these runs.**
1. fleet_sim over-counted E-stops / joint faults that started on the very sample the previous one ended
   (two faults merged, invisible to any packet). Fixed in `tools/fleet_sim.py` (80eebcf), with a test.
2. FloorWatch: after a start, an abort or E-stop in the first entry after the saved position gave no
   event (H1 and I were 1 abort short on 2 of 99 machines each). Cause: `backfill._run_task` compared that
   entry with nothing (`gap.packet_lo` is None after a start). Fixed on FloorWatch branch
   `fix/backfill-restart-baseline` (dd705d6): the saved entry is fetched again as the baseline. H1 stress:
   18 machines hit the case, all exact.
3. Install: a bare `pip install -r requirements.txt` gives PySide6 6.12.0, which crashed FloorWatch in a
   tile paint (`Fatal Python error: none_dealloc`). Use `-c constraints-release.txt` (§1).
4. F05 log noise: a refused big program is logged on every resend (30× per machine per 30 min).

### Exit gate on current `main` (26 Oct 2026) — MES plan package C

All 13 scenarios again, on one commit of each repo: agent **cb6b545** (`main`, incl. MES packages A, B, F,
G, H, J), FloorWatch **a3399b6** (`main`, incl. the restart-baseline fix and phase 0). 99 sim each, 30 min busy +
180 s settle (A: 60 min), defaults, three scenarios at a time, each in its own `unshare -rn` namespace (runner:
`~/fw-test/gate2/run_all.sh` + `scen.sh`). Started 12:27, all done 15:23. A smoke run first (F with 2 min):
99 of 99 exact. The partial run of 23 Oct (B and C exact, the rest stopped by a reboot) is superseded.

| Scenario | Seed | Result of `check` | Notes |
|---|---|---|---|
| A | 31 | **99 of 99 exact** — parts 7003/7003, aborts 530/530, E-stops 190/190, cmd fail 78/78, joint 33/33 | 60 min. 70 backfills, 0 `history_gaps`, 0 warnings / errors |
| B | 32 | **99 of 99 exact** — parts 3513/3513, aborts 264/264, E-stops 104/104, cmd fail 37/37, joint 16/16 | `loss 10%` busy; netem dropped 102982. 468 backfills, 0 gaps, 0 warnings |
| C | 33 | **99 of 99 exact** — parts 3500/3500, aborts 262/262, E-stops 97/97, cmd fail 38/38, joint 18/18 | `loss 30%` busy; netem dropped 308808. 1290 backfills, 0 gaps, 0 warnings |
| D | 34 | **99 of 99 exact** — parts 3498/3498, aborts 266/266, E-stops 90/90, cmd fail 51/51, joint 12/12 | `delay 20ms reorder 25% 50%`. 39 backfills, 0 gaps, 0 warnings. Window not watched (offscreen) |
| E | 35 | **99 of 99 exact** — parts 3510/3510, aborts 237/237, E-stops 85/85, cmd fail 38/38, joint 15/15 | `duplicate 10%`. 50 backfills, 0 gaps, 0 warnings |
| F | 36 | **99 of 99 exact** — parts 3498/3498, aborts 284/284, E-stops 99/99, cmd fail 47/47, joint 19/19 | `loss 100%` 30 s at 15 min. 127 backfills, 0 gaps, 0 warnings |
| G | 37 | **99 of 99 exact** — parts 3488/3488, aborts 280/280, E-stops 91/91, cmd fail 45/45, joint 17/17 | `loss 100%` 10 min. 112 backfills, 0 gaps, 0 warnings |
| H1 | 38 | **99 of 99 exact** — parts 3517/3517, aborts 246/246, E-stops 93/93, cmd fail 54/54, joint 19/19 | FloorWatch ended at +895 s, down 60 s, same `--dir`. 0 gaps, 0 warnings (the 22 Oct 1-short case is fixed on `main`) |
| H2 | 39 | **99 of 99 exact** — parts 3470/3470, aborts 276/276, E-stops 90/90, cmd fail 36/36, joint 17/17 | `kill -9` at 15 min, down 60 s. 0 gaps, 0 warnings |
| H3 | 40 | **99 of 99 exact** — parts 3517/3517, aborts 283/283, E-stops 100/100, cmd fail 47/47, joint 17/17 | New: FloorWatch restarted every ~40 s (`--hours 0.011`) for the first 10 min — 14 runs — then to the end. 707 backfills, 0 gaps, 0 warnings |
| I | 41 | **99 of 99 exact** — parts 3492/3492, aborts 282/282, E-stops 99/99, cmd fail 38/38, joint 24/24 | Passes, but the pruned path was **not** hit: SIM-001's entries 1–21 were deleted while FloorWatch (stopped after 3 min) had already received them, so nothing was missing — `Backfilling SIM-001: journal 21..53`, no `pruned` row. On 22 Oct entry 21 had not arrived, so the gap showed. Timing-dependent; the pruned path stays covered by the 22 Oct run and FloorWatch's tests |
| J | 42 | **99 of 99 exact** — parts 3519/3519, aborts 264/264, E-stops 108/108, cmd fail 45/45, joint 23/23 | `loss 10% delay 20ms reorder 25% 50% duplicate 5%`; netem dropped 105544. 489 backfills, 0 gaps, 0 warnings |
| K | 43 | **99 of 99 exact** — parts 3408/3408, aborts 261/261, E-stops 97/97, cmd fail 61/61, joint 13/13 | `--gcode` adds the 5 MB and 32 MB programs: 310 / 309 cycles of them on 9 machines, all counted. **No refusal** in the FloorWatch log (22 Oct: `… refused: 5243 chunks claimed`) and no log noise. FloorWatch `main` no longer logs a received program, so this run does not show that the 32 MB file reached the preview complete — check that in the window |

FloorWatch log in every scenario: 0 warnings, 0 errors. The real agent (MES on, LinuxCNC partly running for the
AXIS check of D + E + G) could not reach the namespaces and played no part. Result: **exit gate passed** on
`main`, with the I and K notes above.

After the last run: `sudo tc qdisc del dev lo root`, and set `monitor_pc_ip` back
in the agent's config.
