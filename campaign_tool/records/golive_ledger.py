"""Read-only go-live ledger for report observations and separate receipts."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import cast

from . import golive_catalog, golive_evidence, golive_stages


_REHEARSAL_AGE = timedelta(days=14)


def _report_proofs(reports):
    # Filter before summarizing so a recent idle run cannot erase older proof.
    # Adoption output counts this run's work. Digest gates v1 outputs are a
    # snapshot of all ledger 'done' counts, not completions in this run.
    def eligible_stage(stage):
        if stage["name"] == "digest_gates":
            return False
        if stage["name"] == "adoption":
            counts = stage["counts"]
            return (stage["status"] == "ok" and counts["output"] > 0
                    and counts["output"] == counts["input"]
                    and not (counts["failed"] or counts["skipped"]))
        return True

    filtered = [dict(report, stages=[stage for stage in report["stages"] if eligible_stage(stage)])
                for report in reports]
    rows = cast(list[dict], golive_stages.summarize(filtered, None)["stages"])
    return {row["stage"]: row["last_proven_run"] for row in rows}


def _when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _is_future(record, now):
    return record is not None and _when(record["recorded_at"]) > now


def _future(record):
    # Neither proof nor failure: the newest record is untrustworthy, so no older one stands in for it.
    return dict(status="missing", recorded_at=record["recorded_at"], reason="FUTURE_DATED")


def _check(required, record, newest_live, now, cutoff=None, *, null_engine_ok=False):
    if not required or record is None:
        return dict(status="missing" if required else "not needed", recorded_at=None)
    stamp = record["recorded_at"]
    if _is_future(record, now):
        return _future(record)
    if cutoff is not None and _when(stamp) < cutoff:
        return dict(status="missing", recorded_at=stamp, reason="STALE")
    # A recorded gate GO carries no engine (golive_record.from_gate); it is not an engine mismatch.
    unattributed = null_engine_ok and record["engine"] is None
    if newest_live is not None and not unattributed and record["engine"] != newest_live["engine"]:
        return dict(status="missing", recorded_at=stamp, reason="ENGINE_CHANGED")
    return dict(status=record["result"], recorded_at=stamp)


def _live(stage, rule, record, run_id, now):
    if _is_future(record, now):
        return _future(record)
    if record is not None and record["result"] == "pass":
        return dict(status="evidence", recorded_at=record["recorded_at"])
    source = golive_stages.STAGES[stage].source if rule.live_source == "run_report" else None
    if source == "outputs":
        return dict(status="missing", reason="AGGREGATE_ONLY")
    if record is not None:
        return dict(status="missing", reason="LIVE_FAILED", recorded_at=record["recorded_at"])
    if source is not None and run_id is not None:
        return dict(status="report", run_id=run_id)
    if source in {"adoption", "digest_gates"}:
        return dict(status="missing", reason="NOT_POSITIVE")
    return dict(status="missing")


def build(reports, evidence, now):
    """Merge loaded inputs in catalogue order; a naive ``now`` means UTC.

    LIVE reflects observed proof, not permission to run a stage. ``next`` names
    the first required column without positive evidence, even if a later
    column already has it. Freshness is relative to the newest live record
    across the ledger; absence of a live record does not establish an engine.
    A gate record with a null engine (as written by ``golive_record.from_gate``)
    is not compared with that engine; rehearsals and gates naming an engine are.
    A record dated after ``now`` is FUTURE_DATED: missing, never a pass or a
    failure, never replaced by an older record, and never the newest live engine.
    """
    if now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - _REHEARSAL_AGE
    proofs = _report_proofs(reports)
    evidence = tuple(evidence)
    # Descriptive sandbox proof does not qualify a rehearsal, even if newer.
    rehearsals = tuple(row for row in evidence if row["kind"] == "rehearsal"
                       and row["source"]["schema"] != "records-e2e-proof-v1")
    live_records = {stage: golive_evidence.latest(evidence, stage, "live")
                    for stage in golive_catalog.STAGES}
    settled = tuple(row for row in evidence if not _is_future(row, now))
    settled_live = (golive_evidence.latest(settled, stage, "live") for stage in golive_catalog.STAGES)
    # Match the evidence reader's timestamp/filename ordering across stages.
    newest_live = max((record for record in settled_live if record is not None),
                      key=lambda record: (record["recorded_at"], record["stage"],
                                          record["source"]["sha256"][:8]), default=None)
    rows = []
    for stage, rule in golive_catalog.STAGES.items():
        rehearsed = _check(rule.needs_rehearsal, golive_evidence.latest(rehearsals, stage, "rehearsal"),
                           newest_live, now, cutoff)
        gate = _check(rule.needs_gate, golive_evidence.latest(evidence, stage, "gate"), newest_live, now,
                      null_engine_ok=True)
        record = live_records[stage]
        live = _live(stage, rule, record, proofs.get(stage), now)
        if live["status"] != "missing":
            state = "LIVE"
        elif ("fail" in (rehearsed["status"], gate["status"])
              or record is not None and record["result"] == "fail" and not _is_future(record, now)):
            state = "BLOCKED"
        elif gate["status"] == "pass":
            state = "GATED"
        elif rehearsed["status"] == "pass":
            state = "REHEARSED"
        else:
            state = "BUILT"
        next_kind = next((kind for kind, column in (("rehearsal", rehearsed), ("gate", gate), ("live", live))
                          if column["status"] in {"fail", "missing"}), None)
        rows.append(dict(stage=stage, label=rule.label, pipeline=rule.pipeline, rehearsed=rehearsed,
                         gate=gate, live=live, state=state, next=next_kind))
    return rows


def live_count(rows):
    return sum(row["pipeline"] and row["state"] == "LIVE" for row in rows)


def _summary(rows):
    total = sum(rule.pipeline for rule in golive_catalog.STAGES.values())
    return f"{live_count(rows)} of {total} pipeline stages live"


def _cell(column):
    return " ".join(column[key] for key in ("status", "run_id", "recorded_at", "reason") if column.get(key))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        reports = golive_stages.load_reports(args.reports, now=now)
        evidence = golive_evidence.load(args.evidence)
    except (ValueError, OSError) as exc:
        print(f"golive_ledger: {exc}", file=sys.stderr)
        return 2
    rows = build(reports, evidence, now)
    summary = _summary(rows)
    if args.json:
        print(json.dumps(dict(rows=rows, live_count=live_count(rows), summary=summary), indent=2))
    else:
        print("Stage | Label | Pipeline | Rehearsed | Gate | Live | State | Next")
        for row in rows:
            print(f"{row['stage']} | {row['label']} | {'yes' if row['pipeline'] else 'no'} | "
                  f"{_cell(row['rehearsed'])} | {_cell(row['gate'])} | {_cell(row['live'])} | "
                  f"{row['state']} | {row['next'] or '-'}")
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
