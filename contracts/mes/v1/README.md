# MES event contract `fw.mes.event/1`

The one definition of every MES event that FloorWatch, the LinuxCNC agent and
MillWright exchange (MES plan P1.1–P1.3). **This folder in the FloorWatch repo
is the canonical copy**; the agent and MillWright repos carry an identical copy
(see *Vendoring*). Nothing here depends on anything outside the folder.

| File | What it is |
|---|---|
| `envelope.schema.json` | JSON Schema (2020-12) of the envelope every event has |
| `events/<type>.schema.json` | JSON Schema of each type's `payload` |
| `catalogue.json` | The types: who may produce each, which correlation ids it must carry, size limits |
| `fixtures/valid/` | Events every implementation must accept |
| `fixtures/invalid/` + `EXPECTED.json` | Events every implementation must reject, with the error expected |
| `validate.py` | Standard-library checker (Python 3.7+): schemas + the rules below |
| `MANIFEST.sha256` | SHA-256 of every file here; proves a copy is unchanged |

```
python validate.py event.json ...     # check events
python validate.py --check-manifest   # is this copy the published contract?
```

## Envelope

```json
{
  "schema": "fw.mes.event/1",
  "event_id": "<uuid v4>",
  "type": "scan.received",
  "producer": {"kind": "agent", "id": "<uuid>", "epoch": "<uuid>", "seq": 1042},
  "site_id": "<uuid or null>",
  "machine_id": "<uuid or null>",
  "station_id": "<uuid, optional>",
  "occurred_at": "2026-10-22T09:15:02.123Z",
  "clock": {"synced": true, "boot_id": "<agent session_id>", "mono_ms": 123456789},
  "correlation": {"execution_id": null, "scan_id": "<uuid>", "operator_session_id": null},
  "source": {"app": "linuxcnc-status-agent", "version": "1.7.0", "record": "spool:1042"},
  "payload": {}
}
```

## Rules

1. **Identity.** Every id is a lowercase UUID version 4. `event_id` is unique,
   and so is `(producer.id, producer.epoch, producer.seq)`. `seq` starts at 1
   and grows by 1 per event within an epoch. A producer starts a **new epoch**
   whenever its store is re-created or restored from a backup, so sequences
   never collide.
2. **Retries resend the identical bytes.** A producer stores the encoded event
   once (`validate.encode()`: UTF-8, sorted keys, no spaces) and resends exactly
   that. The service treats a second copy with the same `event_id` and the same
   bytes as a duplicate; same `event_id` with *different* bytes is a conflict
   and is held, never merged.
3. **Unknown fields are ignored**, in the envelope and in payloads. Within v1
   only optional fields are added (see `CHANGELOG.md`); anything else is v2, in
   a new folder `contracts/mes/v2/`.
4. **Time** is UTC with a `Z` (decision A11). `clock.synced` says whether the
   producer's clock was NTP-synchronised; `boot_id` + `mono_ms` order events
   from one boot even when it was not.
5. **Who may send what** is in `catalogue.json` (`producers`). Events the site
   service creates itself (lookups, binding results, quality, holds,
   corrections) are not in v1 yet; they arrive with Phase 3.
6. **Correlation ids** listed in a type's `requires` must not be null (a scan
   carries its `scan_id`; execution events their `execution_id`; operator
   events their `operator_session_id`).
7. **`site_id` / `machine_id` may be null before enrolment.** MillWright cannot
   know either, and an agent learns `site_id` only at enrolment. So:
   `machine_id` may be null only from MillWright, and `site_id` may be null from
   agents and MillWright. The agent forwarding a MillWright event sends it
   unchanged; the service takes the missing ids from the forwarding agent's
   enrolment (its client certificate), and rejects an event whose non-null
   `machine_id` differs from that agent's.
8. **Size.** An encoded event is at most 16 KiB (`max_event_bytes`); a scan's
   `raw` is at most 4096 bytes (`max_raw_scan_bytes`).
9. **Scan data.** `raw` is the text when it is valid UTF-8 without control
   characters, otherwise base64 with `raw_encoding: "base64"`. GS1 codes
   contain the GS separator (0x1D), so they are always base64
   (`fixtures/valid/scan_received_gs1_base64.json`).
10. **Strict text.** JSON with duplicate keys, NaN / Infinity, lone surrogates
    or invalid UTF-8 is rejected before its content is looked at, and an
    identifier or code ending in a newline is rejected (plain JSON Schema
    patterns let one through).

## Choices made here that refine the plan

`docs/MES_IMPLEMENTATION_PLAN.md` §6 sketched the contract; writing it down
fixed these points:

- `site_id` and `machine_id` are nullable (rule 7); the plan listed them as
  required, which MillWright could never meet.
- `operator_session_id` lives only in `correlation`, not again in the
  `execution.started` payload (one place to read it).
- `cycle.observed` names its method field `correlation_method` (the plan's
  `correlation` clashed with the envelope's `correlation` object).
- `tool.snapshot` requires `execution_id`: it is taken at run start, so it
  belongs to that run.
- Value lists that the plan left open are defined: `framing`, the
  `operator.session_ended` reasons and the `scanner.state` USB fields.

## Vendoring (agent and MillWright repos)

Copy this folder unchanged to `contracts/mes/v1/` in the other repo and write
`contracts/mes/CONTRACT_SOURCE`:

```
repository: ajitesh1020/floorwatch
commit: <FloorWatch commit the copy was taken from>
manifest_sha256: <python validate.py --check-manifest prints it>
```

`python tools/vendor_mes_contract.py <path to the other repo>` (in FloorWatch)
does both. The other repo's CI then runs `python contracts/mes/v1/validate.py
--check-manifest` and checks every fixture with its own producer code. Never
edit a vendored copy: change the contract here, then copy it again.
