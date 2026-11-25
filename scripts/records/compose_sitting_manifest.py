#!/usr/bin/env python3
"""Compose an unsigned, offline sitting manifest for the stage 10-14 engine sitting.

The composer only joins evidence that other tools already produced. It reads:

* the repin render receipt (engine commit and rendered wrapper digest),
* the five-driver rehearsal set (driver and receipt digests for stages 10-14),
* the existing D8 sitting-manifest receipt (its stage verdicts are copied, never re-evaluated),
* the engine's own ``mail_drafts.py`` from a local clone, to confirm exact-revision Drafts placement exists,
* optionally a sitting window and an independent review of that window and this manifest.

Every disagreement becomes a named HOLD reason: mixed engine pins, a D8 receipt for another rehearsal set,
CI still held, a stage D8 did not match, missing exact-revision placement, or an absent, expired or unreviewed
window. Even with no HOLD reason the result is only ``READY_FOR_BOOKING_REQUEST_REVIEW``: the manifest is
unsigned, books nothing, installs nothing and runs nothing live. It writes only the ``--out`` file and refuses
to overwrite it.

usage: compose_sitting_manifest.py --kit RENDER.json --five-driver-set SET.json --d8 D8.json --repo CLONE
                                   [--window WINDOW.json [--review REVIEW.json]] --out MANIFEST.json
"""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "records-sitting-manifest-composition-v1"
STAGES = ("10", "11", "12", "13", "14")
DRIVERS = {"10": "rehearse_comparison", "11": "rehearse_gap_proposals", "12": "rehearse_received",
           "13": "rehearse_drafts", "14": "rehearse_drafts"}
D8_GATE_CASE = "reviewed_candidate_positive"
D8_VERIFIED, D8_CI_HELD = "REHEARSALS_VERIFIED", "REHEARSALS_VERIFIED_CI_HELD"
PLACEMENT_SOURCE = "campaign_tool/records/mail_drafts.py"
HOLD, READY = "HOLD", "READY_FOR_BOOKING_REQUEST_REVIEW"
NOTE = ("Unsigned offline composition. READY only permits an independent review of a booking request; it is "
        "never a booking, signature, installation, phone release or live run.")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Z0-9_]{1,64}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_REVIEWER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.@-]{0,63}\Z")


class InputError(Exception):
    """An input file is missing or is not a JSON object."""


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load(path):
    """Return (object, sha256 of the exact bytes) for a JSON object file."""
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InputError("%s: %s" % (Path(path).name, type(exc).__name__)) from None
    if not isinstance(value, dict):
        raise InputError("%s: not a JSON object" % Path(path).name)
    return value, sha256(raw)


def _is_commit(value):
    return isinstance(value, str) and bool(_COMMIT.match(value))


def _is_digest(value):
    return isinstance(value, str) and bool(_DIGEST.match(value))


def _obj(value):
    """A nested JSON object, or an empty one for anything else (a malformed shape then fails its own check)."""
    return value if isinstance(value, dict) else {}


def _commit_or_none(value):
    return value if _is_commit(value) else None


def _digest_or_none(value):
    return value if _is_digest(value) else None


def _token(value):
    """An upper-case status/code token copied from an input, or MALFORMED; free text is never copied out."""
    return value if isinstance(value, str) and _TOKEN.match(value) else "MALFORMED"


def kit_section(render):
    """Engine and wrapper from the repin render receipt."""
    holds = []
    engine = render.get("candidate_head")
    if not _is_commit(engine):
        holds.append("KIT_ENGINE_INVALID")
        engine = None
    if render.get("new_verdict") != "FREEZABLE" or render.get("unexpected") != []:
        holds.append("KIT_NOT_FREEZABLE")
    wrapper_row = _obj(_obj(render.get("renders")).get("with-decisions"))
    wrapper = wrapper_row.get("wrapper_sha256")
    if type(wrapper_row.get("rc")) is not int or wrapper_row["rc"] != 0 or not _is_digest(wrapper):
        holds.append("KIT_WRAPPER_MISSING")
        wrapper = None
    contains = render.get("contains")
    required = {}
    if not isinstance(contains, dict) or not contains:
        holds.append("KIT_REQUIRED_CHANGE_MISSING")
    else:
        required = {name: value is True for name, value in contains.items() if _NAME.match(name)}
        if len(required) != len(contains) or not all(required.values()):
            holds.append("KIT_REQUIRED_CHANGE_MISSING")
    return {"engine": engine, "wrapper_sha256": wrapper, "required_changes": required}, holds


def driver_section(run, engine):
    """Per-stage driver and receipt digests from the five-driver set; every pin must be the kit engine."""
    holds = []
    if run.get("status") != "QUALIFIED_SET" or run.get("ran") is not True or run.get("refusals") != []:
        holds.append("SET_NOT_QUALIFIED")
    if not (run.get("head") == run.get("expect_engine") == engine and engine):
        holds.append("MIXED_PINS:SET_ENGINE_NOT_KIT_ENGINE")
    stages = _obj(run.get("stages"))
    rows = {}
    for stage in STAGES:
        row = stages.get(stage)
        if not isinstance(row, dict):
            holds.append("STAGE_MISSING:%s" % stage)
            rows[stage] = None
            continue
        rows[stage] = {"driver": DRIVERS[stage], "driver_sha256": _digest_or_none(row.get("driver_source_sha256")),
                       "receipt_sha256": _digest_or_none(row.get("receipt_sha256")),
                       "engine": _commit_or_none(row.get("engine"))}
        if (row.get("modules") != [DRIVERS[stage]] or row.get("code") != "QUALIFIED_SAME_ENGINE"
                or row.get("qualified") is not True or not _is_digest(row.get("driver_source_sha256"))
                or not _is_digest(row.get("receipt_sha256"))):
            holds.append("STAGE_NOT_QUALIFIED:%s" % stage)
        if row.get("engine") != engine or not engine:
            holds.append("MIXED_PINS:STAGE_ENGINE_NOT_KIT_ENGINE:%s" % stage)
    if set(stages) - set(STAGES):
        holds.append("SET_HAS_UNKNOWN_STAGE")
    return rows, holds


def d8_section(report, set_sha, engine, drivers):
    """Copy the existing D8 verdict for exactly this rehearsal set; never re-evaluate a receipt."""
    holds = []
    d8 = _obj(report.get("d8"))
    status = report.get("status")
    if status == D8_CI_HELD:
        holds.append("D8_CI_HELD")
    elif status != D8_VERIFIED:
        holds.append("D8_NOT_VERIFIED")
    if not (report.get("main_commit") == d8.get("reader_head") == engine and engine):
        holds.append("MIXED_PINS:D8_ENGINE_NOT_KIT_ENGINE")
    if not (report.get("t12_set_sha256") == d8.get("t12_set_sha256") == set_sha):
        holds.append("D8_FOR_OTHER_SET")
    inputs = _obj(d8.get("stage_inputs"))
    case = _obj(_obj(d8.get("cases")).get(D8_GATE_CASE))
    listed = case.get("stages")
    if not isinstance(listed, list):
        listed = []
    by_stage, seen = {}, []
    for row in listed:
        stage = row.get("stage") if isinstance(row, dict) else None
        if not isinstance(stage, str) or stage not in STAGES:
            holds.append("D8_GATE_UNEXPECTED_ROW")
            continue
        seen.append(stage)
        by_stage[stage] = row
    codes = {}
    for stage in STAGES:
        ours, theirs, verdict = drivers.get(stage), _obj(inputs.get(stage)), _obj(by_stage.get(stage))
        codes[stage] = _token(verdict.get("code", "STAGE_ABSENT"))
        if seen.count(stage) > 1:
            codes[stage] = "STAGE_DUPLICATE"
        if ours is None or any(theirs.get(k) != ours[k] for k in ("driver", "driver_sha256", "receipt_sha256")) \
                or verdict.get("receipt_sha256") != ours["receipt_sha256"]:
            holds.append("D8_STAGE_INPUT_MISMATCH:%s" % stage)
        if codes[stage] != "EVIDENCE_MATCH":
            holds.append("D8_STAGE_NOT_MATCH:%s:%s" % (stage, codes[stage]))
    if case.get("status") != "READY_FOR_REVIEW":
        holds.append("D8_GATE_NOT_READY")
    return {"status": _token(status), "gate_case": D8_GATE_CASE, "gate_status": _token(case.get("status")),
            "stage_codes": codes, "d8_head": _commit_or_none(report.get("d8_head"))}, holds


def _has_exact_revision_placement(source):
    tree = ast.parse(source)
    place = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "place"]
    names = {a.arg for a in place[0].args.kwonlyargs} if len(place) == 1 else set()
    flag = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
               and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "--revision"
               for n in ast.walk(tree))
    return {"only_id", "only_revision"} <= names and flag


def capability_section(repo, engine):
    """Exact-revision Drafts placement must exist in the engine itself, read from its git object."""
    if repo is None or not engine:
        return {"exact_revision_placement": "UNKNOWN"}, ["CAPABILITY_UNKNOWN:EXACT_REVISION_PLACEMENT"]
    shown = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (engine, PLACEMENT_SOURCE)],
                           capture_output=True)
    if shown.returncode != 0:
        return {"exact_revision_placement": "UNKNOWN"}, ["CAPABILITY_UNKNOWN:EXACT_REVISION_PLACEMENT"]
    try:
        present = _has_exact_revision_placement(shown.stdout.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        present = False
    section = {"exact_revision_placement": "PRESENT" if present else "MISSING",
               "source_sha256": sha256(shown.stdout)}
    return section, [] if present else ["REVISION_PLACEMENT_MISSING"]


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def window_section(window, review, core_sha, engine, wrapper, now):
    """A window counts only if it pins this engine/wrapper, is still open and is independently reviewed."""
    if window is None:
        holds = ["WINDOW_ABSENT", "WINDOW_UNREVIEWED"]
        return {"window": "ABSENT", "review": "ABSENT" if review is None else "IGNORED_NO_WINDOW"}, holds
    body, window_sha = window
    holds = []
    if body.get("engine") != engine or body.get("wrapper_sha256") != wrapper or not engine or not wrapper:
        holds.append("MIXED_PINS:WINDOW_NOT_KIT")
    start, end = _instant(body.get("start")), _instant(body.get("end"))
    if start is None or end is None or end <= start:
        holds.append("WINDOW_INVALID")
    elif end <= now:
        holds.append("WINDOW_EXPIRED")
    section = {"window": "PRESENT", "window_sha256": window_sha,
               "start": start.isoformat() if start else None, "end": end.isoformat() if end else None}
    if review is None:
        holds.append("WINDOW_UNREVIEWED")
        section["review"] = "ABSENT"
        return section, holds
    verdict, review_sha = review
    reviewer = verdict.get("reviewer")
    reviewer = reviewer.strip() if isinstance(reviewer, str) and _REVIEWER.match(reviewer.strip()) else None
    section.update(review="PRESENT", review_sha256=review_sha, reviewer=reviewer)
    if verdict.get("window_sha256") != window_sha or verdict.get("manifest_sha256") != core_sha:
        holds.append("WINDOW_UNREVIEWED:REVIEW_FOR_OTHER_DIGEST")
    elif verdict.get("verdict") != "APPROVE":
        holds.append("WINDOW_UNREVIEWED:REVIEW_NOT_APPROVE")
    elif reviewer is None:
        holds.append("WINDOW_UNREVIEWED:REVIEWER_UNNAMED")
    return section, holds


def compose(render, run, d8_report, *, repo, window=None, review=None, now=None):
    """Each of render/run/d8_report/window/review is (object, sha256 of its bytes) or None for window/review."""
    now = now or datetime.now(timezone.utc)
    kit, holds = kit_section(render[0])
    engine, wrapper = kit["engine"], kit["wrapper_sha256"]
    drivers, more = driver_section(run[0], engine)
    holds += more
    d8, more = d8_section(d8_report[0], run[1], engine, drivers)
    holds += more
    capability, more = capability_section(repo, engine)
    holds += more
    core = {"schema": SCHEMA, "engine": engine, "wrapper_sha256": wrapper, "kit": kit, "stages": drivers,
            "d8": d8, "capability": capability,
            "inputs": {"render_sha256": render[1], "five_driver_set_sha256": run[1], "d8_sha256": d8_report[1]}}
    core_sha = canonical_sha256(core)
    win, more = window_section(window, review, core_sha, engine, wrapper, now)
    holds += more
    holds = sorted(set(holds))
    return dict(core, manifest_sha256=core_sha, window=win, holds=holds, disposition=HOLD if holds else READY,
                unsigned=True, booked=False, offline=True, composed_at=now.replace(microsecond=0).isoformat(),
                note=NOTE)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compose an unsigned offline stage 10-14 sitting manifest.")
    parser.add_argument("--kit", required=True, type=Path, help="repin render receipt (engine and wrapper)")
    parser.add_argument("--five-driver-set", required=True, type=Path, help="five-driver rehearsal set JSON")
    parser.add_argument("--d8", required=True, type=Path, help="existing D8 sitting-manifest receipt")
    parser.add_argument("--repo", type=Path, help="local clone containing the engine commit")
    parser.add_argument("--window", type=Path, help="proposed sitting window JSON")
    parser.add_argument("--review", type=Path, help="independent review of the window and manifest digest")
    parser.add_argument("--out", required=True, type=Path, help="new file to write; never overwritten")
    args = parser.parse_args(argv)
    try:
        render, run, d8 = load(args.kit), load(args.five_driver_set), load(args.d8)
        window = load(args.window) if args.window else None
        review = load(args.review) if args.review else None
    except InputError as exc:
        print("REFUSED input: %s" % exc, file=sys.stderr)
        return 2
    manifest = compose(render, run, d8, repo=args.repo, window=window, review=review)
    try:
        with args.out.open("x", encoding="utf-8") as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write("\n")
    except FileExistsError:
        print("REFUSED: %s already exists" % args.out.name, file=sys.stderr)
        return 2
    print(json.dumps({"disposition": manifest["disposition"], "holds": manifest["holds"],
                      "manifest_sha256": manifest["manifest_sha256"], "engine": manifest["engine"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
