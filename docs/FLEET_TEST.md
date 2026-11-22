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

## 7. MES traffic (MES plan P4.5)

`fleet_sim.py run --mes` adds the MES side to every simulated machine
(`tools/fleet_sim_mes.py`): a scanner, an operator and a MillWright-like panel.
Each machine sends contract-valid MES v1 events (`contracts/mes/v1`, checked
before they are stored) to the FloorWatch site service exactly the way the
real agent does: stored in the agent's own spool (`agent_mes_spool`), sent by
the agent's own forwarder (`agent_mes_forward`: mTLS `POST /v1/events`,
batches of at most 200, the same bytes again after any failure, backoff up to
5 min). Each machine is enrolled once with a one-time code, like a real one
(files in `<dir>/SIM-nnn.mes/`).

Two producers per machine, each with a stable id (`<dir>/SIM-nnn.mes_producers.json`),
a new epoch every run and its own rising `seq`:

| Producer | Events |
|---|---|
| agent | `scan.received`, `binding.requested`, `binding.cleared` |
| MillWright (via the agent's spool, as from its outbox) | `operator.session_started` / `_ended`, `execution.started` (with `bound_scan_ids`, `mode`, program and run-file hashes), `tool.snapshot`, `execution.ended`; `site_id` / `machine_id` null (contract rule 7) |

### Scenarios

Every machine first runs each scenario once (starting at a different one), then
picks them at random with weights. `--mes-scenarios` limits the list.

| Scenario | What the machine does | What the service must record (truth) |
|---|---|---|
| `unit` | one unit label at position A, a normal run (10 % aborted) | one attempt, `completed_pending_inspection` (or `aborted`), binding consumed |
| `panel` | 2–4 panel labels at positions P1…Pn, one run | one attempt per panel, each with its position |
| `lot` | a GS1 lot label (GS separated, so sent base64), no position | one attempt, no fixture position |
| `duplicate_read` | the same label read twice; the second read re-uses position A | the first binding superseded, **one** attempt (of the second scan) |
| `wrong_revision` | a label of revision R2: no profile matches; the operator clears it and runs a good unit | that scan `unresolved`, its binding superseded, no attempt; the good unit's attempt |
| `setup` | a dry run or probe, then the real run | a `setup` attempt (not production, no binding, position A), then the production attempt that consumes the binding |
| `run_from_here` | a run aborted, then run-from-here of the same item | `aborted`, then a continuation of it (`counts_as_production` false, no binding) |
| `aborted` | a run aborted | `aborted` |
| `interrupted` | a run ending `interrupted_unknown` | `interrupted_unknown` and one `interrupted_run` exception listing the attempt |
| `two_heads` | `tool.snapshot` with two heads | still one attempt, with 2 heads |
| `operator_change` | the operator is replaced mid-run (`session_ended` "replaced", new `session_started`) | the start operator stays on the attempt; the new one is a participant until the run ends |
| `late_scan` | a run with nothing bound; the label is scanned after the end | no attempt, the scan `validated` (a supervisor would attach it; never matched by time) |
| `conflict` | the same unit scanned and running on two machines at once | two attempts, **exactly one held**, one `unit_on_two_machines` exception on the held run |

**The operator waits for green.** Before a run that uses scans, the simulated
operator waits until the readout shows them bound (the simulator asks
`GET /v1/agents/{id}/feedback`, the readout's own source). The service resolves
scans in the background after a batch is stored, so a run started before that
records no binding and no fixture position, the item is marked
`identified_after_start`, and the two-machine check cannot see it
(`docs/MES_BINDING.md` in FloorWatch). With the wait, the record is the same
whatever the network does; during an outage the machines simply wait. The two
runs of a `conflict` keep running until both starts are acknowledged, so the
service always sees them overlap.

`truth.json` gains `"mes"`: every unit (`namespace/id`) with its expected
attempts (machine, run, scan, state, mode, `counts_as_production`, held,
continuation, fixture position, binding, operator, participants, heads), its
scans and bindings; the unresolved scans; the expected exceptions; all run ids;
per machine the producers, event counts and scenarios; the forwarders' receipts
and anything still undelivered.

### Run

On the site service PC (once per test site), load the profiles the simulator's
labels need, and add a desktop user for the HTTPS check:

```bash
floorwatch-service profiles set tools/fleet_sim_profiles.json
floorwatch-service user add simcheck --role viewer --password-env FW_PASSWORD
```

Then the fleet (here 99 machines; with MES the run also enrols every machine
that is not enrolled yet, getting one code per machine from the command):

```bash
python3 tools/fleet_sim.py run --machines 99 --minutes 30 --settle 180 \
    --target 127.0.0.1:5005 --dir ~/fw-test/fleet1 --seed 1 \
    --mes --mes-service https://127.0.0.1:8443 \
    --mes-code-cmd "floorwatch-service --data <service data> enroll-code"
```

Or make the codes first (`enroll-code`, one per line in a file) and pass
`--mes-codes FILE`. Options: `--mes-cycle 20,60` run length, `--mes-idle 5,20`
time between jobs (seconds), `--mes-drain 300` how long after the settle the
simulator lets running jobs end and the spools empty. Use a fresh `--dir` per
run, or keep it to reuse the enrolments (every run gets new ids and a new
epoch, so runs never collide).

### Check

```bash
# read the service's ledger directly (read-only)
python3 tools/fleet_sim.py check --truth ~/fw-test/fleet1/truth.json --mes-db <service data>/ledger.db
# or over HTTPS, as a desktop user
FW_PASSWORD=... python3 tools/fleet_sim.py check --truth ~/fw-test/fleet1/truth.json \
    --mes-url https://127.0.0.1:8443 --mes-login simcheck --mes-ca ~/fw-test/fleet1/SIM-001.mes/ca.crt
# both, with the dashboard's history as before
python3 tools/fleet_sim.py check --truth … --db ~/fw-test/run1/history.db --mes-db …
```

Pass: `MES: … checked: exact`, exit code 0. Every difference is one
`MISMATCH` line: a missing, extra or duplicated attempt; a wrong state, mode,
production flag, held flag, continuation, fixture position, binding, operator,
participant or head count; a scan or binding in the wrong state; an expected
exception missing, or an extra one on the simulator's runs (also any
quarantine exception via a simulated machine). Warnings say when the truth is
not final or events were still in the spools.

### Scenarios A–L with MES traffic

The service is HTTPS (TCP), so netem goes on its port instead of UDP 5005.
Shape both directions on `lo`, in the same namespace as in §5:

```bash
sudo tc qdisc add dev lo root handle 1: prio bands 4
sudo tc filter add dev lo parent 1:0 protocol ip prio 1 u32 \
    match ip protocol 6 0xff match ip dport 8443 0xffff flowid 1:4
sudo tc filter add dev lo parent 1:0 protocol ip prio 1 u32 \
    match ip protocol 6 0xff match ip sport 8443 0xffff flowid 1:4
sudo tc qdisc add dev lo parent 1:4 handle 40: netem loss 10%
```

Add both the UDP 5005 filter (§5) and these two to run the dashboard and the
MES side through the same impairment.

| # | MES variant | Expected |
|---|---|---|
| A | baseline | `check --mes-db` exact; receipts: 0 quarantined, 0 rejected |
| B, C | loss 10 % / 30 % on port 8443 | exact (TCP and the forwarder's resend recover) |
| D | reorder | exact (per machine the spool keeps order; between machines order does not matter) |
| E | duplicate | exact; a resend after a lost receipt answers `duplicate` |
| F | 30 s `loss 100%` | exact; machines wait for green during the outage |
| G | 10 min `loss 100%` | exact; give `--mes-drain 900` (the forwarder's backoff reaches 5 min) |
| H1, H2, H3 | stop / `kill -9` / restart the **site service** (`floorwatch-service run`) instead of the dashboard | exact; batches not acknowledged are sent again |
| I | pruned journal: not applicable (the MES spool never drops unsent events) | — |
| J | everything at once on port 8443 | exact |
| K | big programs: unchanged (UDP side only) | exact |
| L | load: 99 machines with `--mes`, note the service's CPU / memory | indicative |

### Record (MES)

| Date | Where | Run | Result |
|---|---|---|---|
| 22 Nov 2026 | Windows 11 dev PC, local FloorWatch site service (`main` 62ea96d, fresh `init`, profiles loaded) on 127.0.0.1:18443, no netem; 13 machines enrolled by the run with `--mes-code-cmd` | 13 sim, 4 min + 10 s settle, `--mes-cycle 3,8 --mes-idle 1,3`, seed 12; **H2-like**: the site service killed (forced) at +60 s, started again on the same data ~45 s later | **exact** with `--mes-db` and with `--mes-url` (viewer login): 258 units, 287 attempts, 303 scans, 28 expected exceptions; every scenario (conflict 15, wrong_revision 15, setup 16, run_from_here 13 …). Receipts: 1416 stored, 0 quarantined, 0 rejected; spools empty at the end. During the outage the pending events rose to 51 and the machines waited for green. Service log: no warnings except discovery's UDP 5007 being in use on that PC. (A first run crashed in the simulator's own progress report — fixed before this run.) |
| 22 Nov 2026 | in-process, FloorWatch `main` 62ea96d ingest + projections + resolver (`FLOORWATCH_SRC=… python -m unittest tests.test_fleet_sim_mes`) | 13 sim, 150 s virtual; machines delivered in random order, 2 % chance of a 2–15 s outage per machine per step, 10 % of receipts lost (batch resent) | **exact** (every scenario, 0 quarantined / rejected, duplicates seen). The same run with the operator **not** waiting for green: 984 mismatches (`binding none`, `identified_after_start`), as described above |

Not yet run: the netem scenarios A–L with `--mes` on the LinuxCNC PC (needs that PC
and its namespace setup).
