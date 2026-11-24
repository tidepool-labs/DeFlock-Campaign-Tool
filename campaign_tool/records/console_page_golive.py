"""GET /golive: read-only progress from the go-live ledger."""
import os
import re
from datetime import datetime, timezone

from . import console_layout, console_runs_model, golive_catalog, golive_evidence, golive_ledger, golive_stages
from .outcomes.reading import esc

PATHS = ("/golive",)
NAV = ("Go-live", 16)
_HEADINGS = ("Stage", "Name", "Rehearsed", "Gate", "Live", "State", "Next")
# Match only the loader's file diagnostics, not its directory, run ID, or schema details.
_REPORT_FILE = re.compile(r'''(['"])([^/\\\r\n]+\.json)\1: (?:invalid report|unreadable report|duplicate run ID \w+)''')
_EVIDENCE_FILE = re.compile(r"([^/\\\r\n]+?\.json): ")


def _status(message, basename=None):
    detail = f" ({basename})" if basename else ""
    return f'<p role="status" class="diag">{esc(message + detail + ".")}</p>'


def _table(title, rows):
    headers = "".join(f'<th scope="col">{label}</th>' for label in _HEADINGS)
    body = []
    for row in rows:
        stage = row["stage"]
        next_step = f'{row["next"]} ({golive_catalog.STAGES[stage].owner})' if row["next"] else "—"
        cells = (stage, row["label"], *(golive_ledger._cell(row[key]) for key in ("rehearsed", "gate", "live")),
                 row["state"], next_step)
        body.append("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in cells) + "</tr>")
    return (f'<h2>{title}</h2><div class="scroll" role="region" tabindex="0" aria-label="{title}">'
            f'<table style="min-width:48rem"><caption>{title}</caption>'
            f'<thead><tr>{headers}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def _reports(directory, now):
    # As on /stages, bound archive work and read through the same no-follow directory descriptor.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(fd) as entries:
            for count, _ in enumerate((entry for entry in entries if entry.name.endswith(".json")), 1):
                if count > console_runs_model.FILE_LIMIT:
                    raise ValueError("report limit exceeded")
        return golive_stages.load_reports(f"/proc/self/fd/{fd}/.", now=now)
    finally:
        os.close(fd)


def _evidence(run_report_path):
    from .console_oversight import _target  # Use the same target without a registry import cycle.

    target = _target(run_report_path)
    if target is None:
        return _status("Go-live evidence is not configured on this console")
    now = datetime.now(timezone.utc)
    try:
        reports = _reports(target / "reports", now)
    except (OSError, ValueError, RecursionError) as exc:
        match = _REPORT_FILE.fullmatch(str(exc))
        return _status("Go-live reports unavailable", match[2] if match and match[2].isprintable() else None)
    try:
        evidence = golive_evidence.load(target / "golive-evidence")
    except (OSError, ValueError) as exc:
        match = _EVIDENCE_FILE.match(str(exc))
        return _status("Go-live evidence unavailable", match[1] if match and match[1].isprintable() else None)

    rows = golive_ledger.build(reports, evidence, now)
    return (f'<p role="status">{esc(golive_ledger._summary(rows))}</p>'
            + _table("Pipeline stages", (row for row in rows if row["pipeline"]))
            + _table("Console stages", (row for row in rows if not row["pipeline"])))


def serve(handler):
    if handler.path.partition("?")[0] not in PATHS:
        return False
    body = ("<h1>Go-live</h1><p>Read-only. Evidence shows recorded progress; it does not authorize a stage to run.</p>"
            + _evidence(handler.options.get("run_report_path")))
    handler._send(200, console_layout.shell(NAV[0], body, active=NAV[0]))
    return True
