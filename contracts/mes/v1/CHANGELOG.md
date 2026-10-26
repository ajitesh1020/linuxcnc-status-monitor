# fw.mes.event/1 changes

Within v1 only additive changes are allowed: new optional fields, new event
types, new values in an enum that consumers already treat as "unknown → keep".
Anything that would make an old event invalid, or change the meaning of a
field, goes into `contracts/mes/v2/`.

## 1.2 — `atc_check` run mode

- `execution.started` mode `atc_check`: an ATC tool pick/drop check run
  (MillWright's generated repeatability program). Machine time, not production:
  like `dryrun` / `frame` / `warmup` it is setup activity and never counts parts
  (plan P4.3). Before 1.2 MillWright sent no execution events for these runs.
  Additive: consumers keep an unknown mode as "not normal" (not production).

## 1.1 — `spool_full` (MES plan P2.4)

- `scan.rejected_local` reason `spool_full`: the agent's MES spool is at its
  disk budget, so a complete scan was not stored (`partial_len` = its length).
  Additive: consumers keep an unknown reason as an unknown rejection.

## 1.0 — first version (MES plan P1.1–P1.3)

- Envelope and 12 event types: `scan.received`, `scan.rejected_local`,
  `scanner.state`, `operator.session_started`, `operator.session_ended`,
  `execution.started`, `execution.ended`, `cycle.observed`,
  `binding.requested`, `binding.cleared`, `tool.snapshot`, `program.version`.
- Standard-library `validate.py`, fixtures and `MANIFEST.sha256`.
