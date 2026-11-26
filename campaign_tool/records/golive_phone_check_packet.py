"""Prepare a phone-check packet from reviewed, saved evidence only.

The caller pins the current seal and reviewed hosted deployment independently
of the captures. `capture` attests the signed-in /golive and CLI bytes; `denial`
records the reviewed edge outcomes for anonymous and unauthorized visits to
Home and Go-live. Neither is generated or authenticated by this module.
READY means the comparison packet is usable, not that the phone check passed
or that D7's separate draft/approval and attended release gates have cleared.
"""

import hashlib
import importlib
import json
import re

from . import golive_catalog

ORIGIN = "https://admin.deflockslo.com"
_ROUTES = frozenset({"/", "/golive"})
_SEAL = re.compile(r"[0-9a-f]{64}")
_HEAD = re.compile(r"[0-9a-f]{40}")


def _hold(reason):
    return {"status": "HOLD", "reason": reason}


def _bound(receipt, expected):
    return (isinstance(receipt, dict) and receipt.get("scope") == "hosted_protected"
            and receipt.get("binding") == expected)


def _denied(routes, allowed):
    return (isinstance(routes, dict) and routes.keys() == _ROUTES
            and all(outcome in allowed for outcome in routes.values()))


def build(*, expected, page_bytes, cli_bytes, capture, denial):
    """Return READY with allowlisted comparison fields, or HOLD with a fixed reason.

    ACCESS_LOGIN is an edge login challenge, not just any redirect. For an
    already authenticated but unauthorized account, require actual denial.
    Never pass raw headers, tokens, paths, identities or bodies as receipts.
    """
    if (not isinstance(expected, dict) or set(expected) != {"origin", "seal_sha256", "console_head"}
            or expected["origin"] != ORIGIN
            or not isinstance(expected["seal_sha256"], str) or not _SEAL.fullmatch(expected["seal_sha256"])
            or not isinstance(expected["console_head"], str) or not _HEAD.fullmatch(expected["console_head"])):
        return _hold("SNAPSHOT_UNVERIFIED")

    if (not _bound(denial, expected)
            or not _denied(denial.get("signed_out"), ("DENIED", "ACCESS_LOGIN"))
            or not _denied(denial.get("unauthorized"), ("DENIED",))):
        return _hold("DENIAL_UNVERIFIED")

    # #1984 owns page parsing and exact row/count comparison. Until its reader
    # is installed this consumer cannot make a positive finding.
    try:
        golive_parity = importlib.import_module(".golive_parity", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.golive_parity":
            raise
        return _hold("PARITY_READER_UNAVAILABLE")

    if not _bound(capture, expected) or any(
        not isinstance(data, bytes) or not (0 < len(data) <= golive_parity.MAX_BYTES)
        or capture.get(field) != hashlib.sha256(data).hexdigest()
        for field, data in (("page_sha256", page_bytes), ("cli_sha256", cli_bytes))
    ):
        return _hold("CAPTURE_UNVERIFIED")
    try:
        page = golive_parity.parse_page(page_bytes.decode("utf-8"))
        result = golive_parity.compare(page, json.loads(cli_bytes.decode("utf-8")))
    except (ValueError, RecursionError):
        return _hold("PARITY_UNVERIFIED")
    if not result["agree"]:
        return _hold("PARITY_MISMATCH")

    return {
        "status": "READY",
        "title": "DeFlock phone check, 5–10 min",
        "recommendation": "MATCH if every live stage ID and both totals agree; otherwise DIFFERENT.",
        "url": ORIGIN + "/golive",
        "compare": "After signing in, compare every LIVE stage ID on Go-live and the totals on Go-live and Home.",
        "live_stage_ids": [stage for stage in golive_catalog.STAGES if page["rows"][stage] == "LIVE"],
        "pipeline_live": result["cli_count"],
        "pipeline_total": sum(row.pipeline for row in golive_catalog.STAGES.values()),
    }
