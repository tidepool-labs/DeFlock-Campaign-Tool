"""Derive stage A5 (decisions applied) evidence from a copied console export and its run report.

Read-only. The export manifest (EXPORT.json) lists which decision-journal entries the records run applied
(``applied_journal_ids``) and every entry's final outcome (``apply_outcomes``). A5 passes only when that
manifest is bound to the run report exactly as ``golive_adapt_export`` binds it for A2, at least one entry was
APPLIED, the applied ids and the APPLIED outcomes name the same entries, and the recorded approval anchor is
ok. Output and codes never carry journal ids, signatures, keys or decision text. Machine-valid, not accepted.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import approval_anchor_status as anchor, console_export as ce, golive_evidence, pipeline_health as health
from . import golive_adapt_export as export

APPLIED = "APPLIED"
BAD_INPUT = "DECISIONS_EVIDENCE_BAD_INPUT"


def _applied_ids(manifest):
    """Return (ids listed as applied, ids whose outcome is exactly APPLIED), or None when malformed."""
    ids, outcomes = manifest.get("applied_journal_ids", []), manifest.get("apply_outcomes", {})
    if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)
            and isinstance(outcomes, dict)):
        return None
    done = set()
    for ident, entry in outcomes.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("outcome"), str):
            return None
        # A refusal row carries {outcome, refusal}; only the bare APPLIED shape counts as applied.
        if entry == {"outcome": APPLIED}:
            done.add(ident)
        elif entry["outcome"] == APPLIED:
            return None
    return set(ids), done


def _decision_codes(manifest):
    pair = _applied_ids(manifest)
    if pair is None or pair[0] != pair[1]:
        return ["DECISIONS_OUTCOMES_INCONSISTENT"]
    return [] if pair[1] else ["DECISIONS_NONE_APPLIED"]


def records(export_root, run_report, now):
    """Return the one A5 row. Raise ValueError if neither the manifest nor the run report can be read."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    path = Path(run_report)
    report_bytes, latest = export._report(path)
    if latest is not None and health._future(latest, now):
        latest = None
    raw, manifest = export._manifest(export_root, now)
    if raw is not None:
        source = {"schema": ce.SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(), "name": ce.MANIFEST}
    elif report_bytes is not None:
        # Never invent a digest for missing manifest bytes: bind the failure to the run report instead.
        source = {"schema": "records-run-report-v1", "sha256": hashlib.sha256(report_bytes).hexdigest(),
                  "name": path.name}
    else:
        raise ValueError("no readable evidence receipt")
    bound = (raw is not None and latest is not None
             and health._console_export(None, latest, now, health.LIMITS,
                                        pinned_run_id=manifest["run_id"])["reason_code"] == "console_export_current")
    state = anchor._normalize(manifest)
    codes = _decision_codes(manifest) if bound else ["DECISIONS_MANIFEST_UNBOUND"]
    if state != anchor.OK:
        codes.append("APPROVAL_ANCHOR_" + state.upper())
    ok = not codes
    engine = manifest.get("engine")
    if not isinstance(engine, str) or re.fullmatch(r"[0-9a-f]{40}", engine) is None:
        engine = None
    row = {"schema": "records-golive-evidence-v1", "stage": "A5", "kind": "live", "result": "pass" if ok else "fail",
           "recorded_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"), "engine": engine,
           "source": source, "actor": "pipeline", "codes": ["DECISIONS_APPLIED", "APPROVAL_ANCHOR_OK"] if ok else codes}
    golive_evidence.validate(row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--run-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print the evidence row as JSON")
    args = parser.parse_args(argv)
    try:
        row = records(args.export_root, args.run_report, datetime.now(timezone.utc))
    except (OSError, ValueError, RecursionError):
        print(BAD_INPUT, file=sys.stderr)
        return 2
    print(json.dumps(row) if args.json else " ".join([row["stage"], row["result"], *row["codes"]]))
    return 0 if row["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
