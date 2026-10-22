# Instructions for Claude Code on the LinuxCNC PC — FloorWatch fleet test

Written 21 Oct 2026 for a Claude Code session started on the LinuxCNC test PC
(Dell OptiPlex 9020, LinuxCNC 2.9.10, AXIS, config `test_config`, no hardware on
the port). The earlier work happened in a Windows session whose history does not
come with it; everything needed is here and in the linked files.

## Prompt to paste

Start Claude Code in `~/linuxcnc-status-monitor` (branch `feat/fleet-sim`) and
paste:

> Read docs/CLAUDE_CODE_ON_LINUXCNC_PC.md and follow it. Set up FloorWatch and
> the fleet simulator on this PC, run the scenarios in docs/FLEET_TEST.md one by
> one, record every result in its table, and stop to ask me before anything the
> instructions say needs my OK.

---

## 1. Background (what is being tested and why)

An industrial readiness review (21 Oct 2026) of FloorWatch (Windows dashboard,
repo `ajitesh1020/floorwatch`) and this agent found data-integrity defects. Fixed
so far:

| Finding | What | Where |
|---|---|---|
| F01 | Agent no longer reads the NML error channel (it could hide errors from AXIS) | agent `main` (PR #11) |
| F04 | Faults while idle are sent at once, both edges, and journalled | agent `main` (PR #11) |
| F06 | Bad history requests cannot stop the history server | agent `main` (PR #11) |
| F02 | Delayed / reordered packets dropped; restarts recognised by `session_id`, ordered by `packet_seq` | agent `main` (PR #12) + FloorWatch PR #25 |
| F03 | Missed history recovered from any `journal_seq` step; saved position never skips unstored history; failed fetches retried; pruned spans recorded in `history_gaps` | FloorWatch PR #25 |
| F07 (part) | Implausible part-count jumps not expanded | FloorWatch PR #25 |
| F05 | G-code programs up to 32 MiB / 65,536 chunks reach the dashboard; a program over the limit is logged once | agent PR #15 + FloorWatch's matching limit |
| F07 | Optional `site_key`: every packet signed (HMAC-SHA256), history requests signed with a fresh nonce; `journal_bind` / `journal_allow` | agent PR #15 (FloorWatch must check `sig`) |
| Hardening | Journal reopens itself after errors, pauses on low disk; `journal_state` in packets | agent PR #15 |
| Bench find | G-code errors reported as `cmd_state` 3 / `cmd_serial` (agent) and shown as error events in FloorWatch | agent `main` (PR #12) + FloorWatch PR #25 |

Still open, **not** for this session: F08 (OEE), FloorWatch checking `site_key`
signatures (F07), the remaining hardening items.

The goal now: prove FloorWatch's history is **exact** under real agent code and
bad networks, using one real agent plus 99 simulated agents on this PC, before
testing across the real network.

## 2. The tools

- `tools/fleet_sim.py` (this repo, branch `feat/fleet-sim`): simulated agents
  built from the agent's own modules (journal, history server, packet timing,
  G-code sender). `run` writes `truth.json` (what really happened); `check`
  compares FloorWatch's `history.db` with it.
- `tools/soak_run.py --external` (FloorWatch, branch `fix/review-f02-f03-f07`):
  the real FloorWatch packet path and window at Plant tier, listening for
  external agents.
- `docs/FLEET_TEST.md` (this repo): setup, the netem commands, scenarios A–L,
  expected results and the results table. **Follow it exactly.**
- `docs/BENCH_TEST_1.6.0.md`: the earlier single-machine bench test (done).

Check the branches are current before starting:

```bash
cd ~/linuxcnc-status-monitor && git fetch && git switch feat/fleet-sim && git pull --ff-only
python3 -m unittest discover -s tests          # expect OK
cd ~/floorwatch && git fetch && git switch fix/review-f02-f03-f07 && git pull --ff-only
```

If either branch is missing on GitHub, stop and tell the user (the Windows
session may not have pushed it yet).

## 3. Rules for this session

**Safety — this PC runs LinuxCNC.**
- Never change LinuxCNC's configuration (INI, HAL files), never write HAL pins
  (`halcmd setp`/`sets`), never send commands to LinuxCNC (no `linuxcnc.command()`,
  no MDI), never stop or restart LinuxCNC. The user runs programs in AXIS.
- Do not run latency tests (`latency-test`, `latency-histogram`) — and never
  while LinuxCNC runs. Do not take latency numbers during a fleet run.
- Confirm with the user that this PC is not controlling a real machine before
  the first fleet run.

**netem.** Every `tc` command needs `sudo`: show the user the exact commands and
let them run them (or approve each). Only use the filtered setup from
FLEET_TEST.md §5 (UDP port 5005 on `lo`), never a bare `netem` on `lo` or on a
real interface. **Always** remove it when a scenario ends or anything goes wrong:
`sudo tc qdisc del dev lo root`, and check with `tc qdisc show dev lo`.

**Agent config.** Changing `~/linuxcnc-monitor-agent/config.yaml`
(`monitor_pc_ip: "127.0.0.1"` for the test) and restarting the user service
(`systemctl --user restart lcnc-status-agent`) need the user's OK; put it back
to `auto` at the end.

**Code.** Do not change product code (`status.py`, `agent_*.py`, FloorWatch
`src/`) to make a scenario pass. A mismatch is a finding: investigate, explain,
and propose a fix; change code only when the user says so, on a new branch.
Fixing the test tools (`tools/fleet_sim.py`, `tools/soak_run.py`) or the docs is
fine when they are wrong — say what changed.

**Git.** Commit results to `feat/fleet-sim` (FLEET_TEST.md table, notes); ask
before pushing. Never push to `main`, never merge PRs, never force-push.

## 4. How to run each scenario

1. Fresh folders per scenario: FloorWatch `--dir ~/fw-test/runN`, fleet
   `--dir ~/fw-test/fleetN`.
2. Terminal 1 — FloorWatch, with `--hours` comfortably longer than the fleet's
   busy time + settle (for a 60 min fleet with 180 s settle, `--hours 1.2`).
   Wait for its "History database:" line **before** starting the fleet.
3. Terminal 2 — the fleet. Run long commands in the background and wait for
   them to finish; do not poll in a tight loop.
4. netem per the scenario (user runs / approves the `sudo tc` lines); remove it
   before the settle when the table says so.
5. When the fleet prints `done.`, let FloorWatch run on for at least a minute,
   then run `fleet_sim.py check … -v` against `~/fw-test/runN/history.db`.
6. Record the result line ("N of N machines match exactly"), the totals, the
   sender's dropped / swapped counts, and anything notable from FloorWatch's log
   (`~/fw-test/runN/floorwatch/logs/`) in the FLEET_TEST.md table.

Start with a **short** version of A (`--machines 20 --minutes 5 --settle 120`,
busy rates as in FLEET_TEST.md §3) to prove the setup, then the full scenarios.

## 5. What a result means

- **Exact match** (`N of N`): the scenario passes.
- **Parts short or long, or events missing**: a finding. Collect for that
  machine: `check -v` line; its packets around the time (`sqlite3
  ~/fw-test/fleetN/<name>.journal.db "SELECT seq, ts_ms, data FROM entries ORDER
  BY seq"`); FloorWatch's rows (`sqlite3 ~/fw-test/runN/history.db "SELECT * FROM
  completed_cycles WHERE machine_key='<name>' ORDER BY ended_ts_ms"`, same for
  `machine_events`, `journal_state`, `history_gaps`); FloorWatch log lines
  mentioning the machine ("Backfilling", "not recoverable", "still failing").
  Work out which part of the chain lost or doubled it and report that.
- **Known, not a failure**: scenario H — aborts may be 1 short for a machine
  whose first missed entry was an abort; scenario I — the pruned machine is short
  by what was deleted; scenario K — only programs over 32 MiB / 65,536 chunks are
  refused (F05), logged once per program; a FloorWatch still on the old
  4096-chunk limit refuses the 5 MB program.
- The 30 s outage (F) and long outage (G) are the review's main F03 cases; give
  them extra attention.

## 6. When done

- `sudo tc qdisc del dev lo root` (user), `tc qdisc show dev lo` shows no netem.
- Agent config back to `monitor_pc_ip: "auto"`, service restarted (user OK).
- Stop any FloorWatch / fleet processes still running.
- FLEET_TEST.md table filled in (date, machines, result, notes, versions line
  with `git rev-parse --short HEAD` of both repos).
- A short summary for the user: which scenarios passed exactly, any findings
  with evidence, and what is left (the real network between two PCs, Windows
  reference-PC performance, F08).
