"""Derive stage 17b evidence from an operator's offline site-page capture."""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..content import SLUG, load_campaign_agencies
from .golive_evidence import validate


def records(check_tsv, agency_slugs, now, *, source_name="site-check.tsv"):
    """Return one live record from headerless TSV (UTF-8 bytes or text).

    The caller supplies the expected public slugs independently of the capture.
    Counts are encoded as code suffixes; receipt bytes are hashed verbatim.
    """
    raw = check_tsv.encode("utf-8") if isinstance(check_tsv, str) else check_tsv
    observed = {}
    for line, row in enumerate(raw.splitlines(), 1):
        fields = row.decode("utf-8", errors="surrogateescape").split("\t")
        if (len(fields) != 2 or not SLUG.fullmatch(fields[0])
                or not re.fullmatch(r"[1-5][0-9]{2}", fields[1])):
            raise ValueError(f"line {line}: expected slug and HTTP status")
        slug, status = fields
        if slug in observed:
            raise ValueError(f"line {line}: duplicate slug")
        observed[slug] = status

    expected = set()
    for slug in agency_slugs:
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise ValueError("invalid expected agency slug")
        if slug in expected:
            raise ValueError("duplicate expected agency slug")
        expected.add(slug)
    if not expected:
        raise ValueError("campaign has no agency slugs")
    missing = len(expected - observed.keys())
    errors = sum(observed[slug] != "200" for slug in expected & observed.keys())
    codes = [f"{code}_{count}" for code, count in
             (("SITE_PAGE_MISSING", missing), ("SITE_PAGE_ERROR", errors)) if count]
    if now.utcoffset() is None:
        raise ValueError("timestamp must identify an instant")
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    record = {
        "schema": "records-golive-evidence-v1", "stage": "17b", "kind": "live",
        "result": "fail" if codes else "pass", "recorded_at": stamp, "engine": None,
        "actor": "operator", "codes": codes,
        "source": {"schema": "records-site-page-check-v1",
                   "sha256": hashlib.sha256(raw).hexdigest(), "name": source_name},
    }
    validate(record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("check_tsv", type=Path, help="captured slug/status TSV")
    parser.add_argument("campaign", type=Path, help="campaign directory with its agency roster")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of TSV")
    args = parser.parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        agencies = load_campaign_agencies(args.campaign, today=now.date())
        raw = args.check_tsv.read_bytes()
    except (OSError, ValueError, RecursionError):
        print("SITE_EVIDENCE_BAD_INPUT", file=sys.stderr)
        return 2
    try:
        row = records(raw, (a["slug"] for a in agencies), now, source_name=args.check_tsv.name)
    except ValueError as exc:
        print(f"golive_adapt_site: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([row]))
    else:
        print(row["stage"], row["kind"], row["result"], row["recorded_at"],
              row["source"]["schema"], ",".join(row["codes"]), sep="\t")
    return 0 if row["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
