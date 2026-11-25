#!/usr/bin/env python3
"""Two-version policy reading worksheet: one field of one agency policy, prior and current revision side by side.

usage: policy_version_worksheet.py PAIR.json [--format json|text]

PAIR.json (``policy-version-pair-v1``) names the agency, the policy, the field, the prior and the current revision IDs,
and one row per revision: the original source file (a plain file name next to PAIR.json), that file's sha256, the
1-based page and the passage the row says that page holds. An original source file is JSON with ``agency``,
``policy``, ``revision``, ``effective`` (YYYY-MM-DD) and ``pages`` (a list of ``{field: text}`` objects).

A side is shown only when its passage is byte-bound: the file exists, its bytes hash to the row's sha256, the file
names the same agency, policy and revision as the row, it carries a valid effective date, and the cited page holds
exactly the row's passage for this field. Otherwise that side is withheld with a reason and no passage, locator or
source hash is printed for it: an absent revision, two rows for one revision (disputed), a row marked disputed, a
missing or changed source file, a crossed agency/policy/revision, or a passage that is not on the cited page. Any
withheld side makes the pair UNREVIEWED; it is never read as a removal, a match or a change.

The worksheet is refused as a whole (exit 2, nothing printed on stdout) when the two revision IDs are the same, when
the prior and current rows each carry the other revision's source (swapped), or when both sides are byte-bound but
the prior revision is not strictly older than the current one by effective date.

Exit 0 = both sides cited, 1 = worksheet printed with a withheld side, 2 = refused, usage or unreadable input.

Reviewer aid only. The finding fields are blank on purpose; a cited difference is a reason to read, not a policy
conclusion, a compliance finding or a legal determination. It fetches nothing, runs no OCR, writes no file, store,
ledger or queue, and makes no network call. Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path

SCHEMA = "policy-version-worksheet-v1"
PAIR_SCHEMA = "policy-version-pair-v1"
MAX_INPUT = 4 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
NOTE = ("Reviewer aid. Read both passages in their original sources before writing a finding. A cited difference is a "
        "reason to read, not a policy conclusion, compliance finding or legal determination. Withheld sides are "
        "unreviewed, not removed.")
SAME, DIFFERENT, UNREVIEWED = "SAME_CITED_WORDING", "CITED_DIFFERENCE", "UNREVIEWED"


class Refused(Exception):
    pass


def _read_bytes(path):
    """Regular, non-symlink file under the size cap, or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_INPUT:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_INPUT + 1)
    except OSError:
        return None
    return raw if len(raw) <= MAX_INPUT else None


def _text(value):
    return isinstance(value, str) and value.strip() != ""


def load_pair(path):
    raw = _read_bytes(path)
    if raw is None:
        raise Refused("pair file unreadable")
    try:
        pair = json.loads(raw)
    except ValueError:
        raise Refused("pair file is not JSON") from None
    if not isinstance(pair, dict) or pair.get("schema") != PAIR_SCHEMA:
        raise Refused("pair file schema is not %s" % PAIR_SCHEMA)
    for key in ("agency", "policy", "field", "prior_revision", "current_revision"):
        if not _text(pair.get(key)):
            raise Refused("pair file lacks %s" % key)
    if pair["prior_revision"] == pair["current_revision"]:
        raise Refused("prior and current revision are the same ID")
    rows = pair.get("revisions")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise Refused("pair file revisions must be a list of objects")
    return pair


def _effective(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _source(base, row):
    """(source dict, sha256) when the row's file is present and hashes to the row's digest, else (None, reason)."""
    name, digest = row.get("source"), row.get("source_sha256")
    if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(digest, str) or not HEX64.fullmatch(digest):
        return None, "SOURCE_REFERENCE_INVALID"
    raw = _read_bytes(base / name)
    if raw is None:
        return None, "SOURCE_UNAVAILABLE"
    if hashlib.sha256(raw).hexdigest() != digest:
        return None, "SOURCE_BYTES_CHANGED"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "SOURCE_NOT_JSON"
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), list):
        return None, "SOURCE_NOT_JSON"
    return doc, digest


def side(pair, base, revision):
    """One revision's cited passage, or a withheld record carrying only the revision ID and a reason."""
    rows = [r for r in pair["revisions"] if r.get("revision") == revision]
    if not rows:
        return {"revision": revision, "withheld": "REVISION_ABSENT"}
    if len(rows) > 1:
        return {"revision": revision, "withheld": "DISPUTED_DUPLICATE_ROWS"}
    row = rows[0]
    if row.get("disputed", False) is not False:
        return {"revision": revision, "withheld": "DISPUTED_PASSAGE"}
    doc, digest = _source(base, row)
    if doc is None:
        return {"revision": revision, "withheld": digest}
    if (doc.get("agency"), doc.get("policy")) != (pair["agency"], pair["policy"]):
        return {"revision": revision, "withheld": "CROSSED_SOURCE"}
    if doc.get("revision") != revision:
        return {"revision": revision, "withheld": "SOURCE_NAMES_OTHER_REVISION", "_source_revision": doc.get("revision")}
    effective = _effective(doc.get("effective"))
    if effective is None:
        return {"revision": revision, "withheld": "EFFECTIVE_DATE_MISSING"}
    page, passage = row.get("page"), row.get("passage")
    if type(page) is not int or page < 1 or page > len(doc["pages"]) or not _text(passage):
        return {"revision": revision, "withheld": "LOCATOR_INVALID"}
    held = doc["pages"][page - 1]
    if not isinstance(held, dict) or held.get(pair["field"]) != passage:
        return {"revision": revision, "withheld": "PASSAGE_NOT_ON_CITED_PAGE"}
    return {"revision": revision, "effective": effective.isoformat(), "locator": "page %d" % page,
            "source": row["source"], "source_sha256": digest, "passage": passage}


def build(pair_path):
    pair_path = Path(pair_path)
    pair = load_pair(pair_path)
    base = pair_path.resolve().parent
    prior = side(pair, base, pair["prior_revision"])
    current = side(pair, base, pair["current_revision"])
    if (prior.get("_source_revision") == pair["current_revision"]
            and current.get("_source_revision") == pair["prior_revision"]):
        raise Refused("prior and current rows carry each other's source (swapped revisions)")
    for item in (prior, current):
        item.pop("_source_revision", None)
    if "withheld" not in prior and "withheld" not in current:
        if prior["effective"] >= current["effective"]:
            raise Refused("prior revision is not older than the current revision (swapped or same date)")
        wording = SAME if prior["passage"] == current["passage"] else DIFFERENT
    else:
        wording = UNREVIEWED
    return {"schema": SCHEMA, "agency": pair["agency"], "policy": pair["policy"], "field": pair["field"],
            "prior": prior, "current": current, "wording": wording, "review_required": True,
            "policy_conclusion": False, "human": {"reviewer": "", "read_on": "", "finding": ""}, "note": NOTE}


def render(sheet):
    labels = {SAME: "Same cited wording - human review required", DIFFERENT: "Cited difference - human review required",
              UNREVIEWED: "Unreviewed - one side withheld"}
    out = ["Policy version worksheet: %s / %s / field %s" % (sheet["agency"], sheet["policy"], sheet["field"]),
           sheet["note"], "Status: " + labels[sheet["wording"]], ""]
    for title, item in (("Prior", sheet["prior"]), ("Current", sheet["current"])):
        if "withheld" in item:
            out.append("%s revision %s: withheld (%s); no passage shown" % (title, item["revision"], item["withheld"]))
        else:
            out.append("%s revision %s (effective %s), %s of %s, sha256 %s" % (
                title, item["revision"], item["effective"], item["locator"], item["source"], item["source_sha256"]))
            out.append("    " + json.dumps(item["passage"], ensure_ascii=False))
    out += ["", "Reviewer: ____    Read on: ____    Finding: ____"]
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Two-version policy reading worksheet (review aid, read-only).")
    parser.add_argument("pair")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        sheet = build(args.pair)
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    if args.format == "json":
        sys.stdout.write(json.dumps(sheet, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(render(sheet))
    return 0 if sheet["wording"] != UNREVIEWED else 1


if __name__ == "__main__":
    raise SystemExit(main())
