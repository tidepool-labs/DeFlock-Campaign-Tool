#!/usr/bin/env python3
"""Offline console tour for stages 10-14: for each stage, which supporting exhibit can a person reach by links?

usage: stage_console_tour.py FIVE_DRIVER_OUT --out DIR [--expect-engine SHA]

FIVE_DRIVER_OUT is the output directory of the five-driver rehearsal runner (one copied sandbox project per
driver). For each of stages 10 to 14 the tour serves THAT stage's own project through the real console export and
admin panel on loopback, walks by links only from Home, and looks for the stage's own identifiers and quotes (read
from the stage's output files, never typed here) on the pages a person can reach. Each exhibit ends as

  NAVIGABLE         shown on a page reached by links from Home,
  PRESENT_UNLINKED  a page answers with it but no link from Home leads there,
  MISSING_VIEW      no console page shows it (an unknown stage 11 proposal id stays MISSING_VIEW until the console
                    has a page for it; the tour never invents one).

Controls: another stage's unique identifiers must be absent from this stage's console (a swapped run cannot pass),
and an unknown draft id must not open a draft. A missing view is a finding about the console, not a failure. Exit
0 = controls pass, 1 = a control failed, 2 = usage, unreadable input or a refused engine/output.

Display aid only: loopback, read-only, nothing sent, no sign-in probe, no console or evaluator edit, no index write.
It never fills a human finding and never says a stage is accepted. The receipt holds statuses, page names, source
file digests and hashes of the searched tokens, never page text.
"""
import argparse
import hashlib
import html
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCHEMA = "stage-console-tour-v1"
STAGES = ("10", "11", "12", "13", "14")
PROJECT = {"10": "rehearse_comparison-*/proof/pair/project", "11": "rehearse_gap_proposals-*/replay/project",
           "12": "rehearse_received-*/replay/project", "13": "rehearse_drafts-*/replay/project",
           "14": "rehearse_drafts-*/replay/project"}
NAVIGABLE, UNLINKED, MISSING = "NAVIGABLE", "PRESENT_UNLINKED", "MISSING_VIEW"
HREF = re.compile(r'href="([^"#]+)"')
SKIP = ("/calendar.ics", "/phone-approve.js", "/send", "/approvals", "/healthz")
PAGE_LIMIT = 60
MAX_INPUT = 16 * 1024 * 1024
UNKNOWN_DRAFT = "9" * 64
NOTE = ("Review aid on sandbox data; loopback, read-only, nothing sent. A missing view is a console finding, not a stage "
        "failure. Human findings stay blank; this is not acceptance.")


class Refused(Exception):
    """Fixed reason for which no tour was produced."""


def _read(path):
    info = os.lstat(path)
    if not path.is_file() or path.is_symlink() or info.st_size > MAX_INPUT:
        raise Refused("INPUT_UNREADABLE")
    return path.read_bytes()


def _json(path):
    try:
        return json.loads(_read(path).decode("utf-8"))
    except (OSError, ValueError):
        raise Refused("INPUT_UNREADABLE")


def _walk(obj, key, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v.strip():
                out.append(v)
            _walk(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, key, out)
    return out


def _uniq(values):
    return list(dict.fromkeys(values))


def _phrase(text):
    return re.sub(r"\s+", " ", text).strip()[:40]


def _latest_run_dir(project):
    runs = sorted(p for p in (project / "run-outputs").glob("*/") if p.is_dir())
    if not runs:
        raise Refused("NO_RUN_OUTPUTS")
    return runs[-1]


def exhibits(stage, project):
    """(exhibit list, source file digests) read from the stage's own copied project.

    Each exhibit: label, tokens, expected pages (where a person would look first), unique (an identifier of this
    run that no other stage's console may show)."""
    run = _latest_run_dir(project)
    sources = []

    def load(*parts):
        path = run.joinpath(*parts)
        sources.append({"file": "/".join(parts), "sha256": hashlib.sha256(_read(path)).hexdigest()})
        return _json(path)

    if stage == "10":
        comparison, facts = load("comparison.json"), load("agency-factsheets.json")
        rows = [("compared agencies (comparison rows)", _uniq(_walk(comparison, "agency_id", []))[:6], ("/comparison",), False),
                ("cited agency statements (source quotes)", [_phrase(q) for q in _uniq(_walk(facts, "quote", []))[:4]],
                 ("/facts",), False)]
    elif stage == "11":
        props = load("outcomes", "gap-proposals.json")
        rows = [("gap proposal: the denial passage it cites", [_phrase(q) for q in _uniq(_walk(props, "quote", []))[:3]], (), False),
                ("gap proposal: its review question", [_phrase(q) for q in _uniq(_walk(props, "question", []))[:3]], (), False),
                ("gap proposal: its own proposal id", [i[:12] for i in _uniq(_walk(props, "proposal_id", []))[:3]], (), True)]
    elif stage == "12":
        items, timeline = load("outcomes", "received-items.json"), load("outcomes", "timeline.json")
        rows = [("received item: its own item id", [i[:12] for i in _uniq(_walk(items, "item_id", []))[:3]], (), True),
                ("timeline: the request it belongs to", _uniq(_walk(timeline, "request_id", []))[:3], ("/timeline",), False)]
    elif stage in ("13", "14"):
        ids = []
        queue = project / "approvals" / "queue.jsonl"
        sources.append({"file": "approvals/queue.jsonl", "sha256": hashlib.sha256(_read(queue)).hexdigest()})
        for line in _read(queue).decode("utf-8").splitlines():
            row = json.loads(line)
            if row.get("event") == "draft" and isinstance(row.get("id"), str):
                ids.append(row["id"])
        rows = [("queued draft: its own draft id", [i[:12] for i in _uniq(ids)[:3]], ("/drafts",), True)]
    else:
        raise Refused("UNKNOWN_STAGE")
    if not all(tokens for _, tokens, _, _ in rows):
        raise Refused("STAGE_OUTPUT_EMPTY")  # an exhibit with nothing to look for would read as "found everywhere"
    return ([{"label": l, "tokens": t, "expect": e, "unique": u} for l, t, e, u in rows], sources, run.name)


def serve(project):
    """Serve one project's console export on loopback; returns (server, temp dir handle)."""
    from campaign_tool.records import admin_panel as ap, console_export
    tmp = tempfile.TemporaryDirectory(prefix="stage-tour-")
    base = Path(tmp.name).resolve()
    run = console_export.latest_run(str(project))
    run = run if isinstance(run, str) else run[0]
    dest, root = base / "dest", base / "root"
    dest.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    console_export.build(str(project), run, str(dest))
    buf = io.BytesIO()
    console_export.write_tar(str(dest), buf)
    buf.seek(0)
    console_export.install(str(root), buf)
    server = ap.build_server(export_root=str(root), run_id="auto", actor="operator", system=False,
                             run_report_path=str(base / "run-report.json"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, tmp


def make_fetch(port):
    def fetch(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
    return fetch


def _norm(body):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S)
    links = " ".join(html.unescape(h) for h in HREF.findall(body))  # an id shown only as a link target counts as shown
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))) + " " + links


def crawl(fetch, wanted=(), limit=PAGE_LIMIT):
    """Breadth-first from Home by links only: {path: (status, text, depth)}; wanted pages not reached are probed
    directly and get depth None (they answer, but no link from Home leads there)."""
    pages, queue = {}, deque([("/", 0)])
    while queue and len(pages) < limit:
        path, depth = queue.popleft()
        if path in pages:
            continue
        status, body = fetch(path)
        pages[path] = (status, _norm(body), depth)
        if status != 200:
            continue
        for link in HREF.findall(body):
            link = html.unescape(link)
            if link.startswith("/") and not link.startswith("//") and not link.startswith(SKIP) and link not in pages:
                queue.append((link, depth + 1))
    for path in wanted:
        if path not in pages:
            status, body = fetch(path)
            pages[path] = (status, _norm(body), None)
    return pages


def where(tokens, pages):
    return [(path, depth) for path, (status, text, depth) in sorted(pages.items())
            if status == 200 and tokens and all(t in text for t in tokens)]


def classify(exhibit, pages):
    hits = where(exhibit["tokens"], pages)
    status = NAVIGABLE if any(d is not None for _, d in hits) else (UNLINKED if hits else MISSING)
    return {"exhibit": exhibit["label"], "status": status, "token_count": len(exhibit["tokens"]),
            "token_sha256": hashlib.sha256("\n".join(exhibit["tokens"]).encode()).hexdigest()[:12],
            "expected_pages": list(exhibit["expect"]),
            "shown_on": [{"page": p, "link_depth": d} for p, d in hits],
            "expected_page_shows_it": any(p in exhibit["expect"] for p, _ in hits)}


def tour(stage_projects, fetchers):
    """stage_projects: {stage: project path}; fetchers: {stage: fetch(path)} serving that stage's own console."""
    own, receipt = {}, {"schema": SCHEMA, "stages": {}, "controls": {}, "failed": [], "note": NOTE}
    for stage in STAGES:
        own[stage] = exhibits(stage, stage_projects[stage])
    wanted = sorted({p for stage in STAGES for e in own[stage][0] for p in e["expect"]})
    pages = {}
    for stage in STAGES:
        key = "13" if stage == "14" else stage  # stages 13 and 14 share one driver project
        if key not in pages:
            pages[key] = crawl(fetchers[key], wanted)
        found, sources, run = own[stage]
        receipt["stages"][stage] = {"run": run, "pages_walked": len(pages[key]), "sources": sources,
                                    "exhibits": [classify(e, pages[key]) for e in found], "human_finding": None}
    for stage in ("10", "11", "12", "13"):  # a swapped run cannot pass: foreign unique ids must be absent
        leaks = [[other, e["label"]] for other in ("11", "12", "13") if other != stage
                 for e in own[other][0] if e["unique"] and where(e["tokens"], pages[stage])]
        receipt["controls"]["swap_into_stage_%s" % stage] = {"foreign_unique_ids_shown": leaks,
                                                            "result": "PASS" if not leaks else "FAIL"}
        if leaks:
            receipt["failed"].append("swap_into_stage_%s" % stage)
    absent = {}
    for stage in ("11", "12", "13"):  # without its own id shown, the swap control proves nothing for that stage
        for e in own[stage][0]:
            if e["unique"] and not where(e["tokens"], pages[stage]):
                absent["%s: %s" % (stage, e["label"])] = "ABSENT"
    receipt["controls"]["own_id_present"] = absent
    status, body = fetchers["13"]("/review?id=" + UNKNOWN_DRAFT)
    shows = any(t in _norm(body) for e in own["13"][0] for t in e["tokens"])
    ok = status in (400, 404, 409) and not shows
    receipt["controls"]["unknown_draft_review"] = {"http": status, "shows_any_draft": shows, "result": "PASS" if ok else "FAIL"}
    if not ok:
        receipt["failed"].append("unknown_draft_review")
    receipt["result"] = "PASS" if not receipt["failed"] else "FAIL"
    return receipt


def render(receipt):
    out = ["# Stage console tour (review aid)", "", receipt["note"], "",
           "Engine: %s%s   Result: %s" % (receipt.get("engine"), " (dirty checkout)" if receipt.get("dirty") else "",
                                           receipt["result"])]
    for stage in STAGES:
        info = receipt["stages"][stage]
        out += ["", "## Stage %s (run %s, %d pages walked)" % (stage, info["run"], info["pages_walked"])]
        for e in info["exhibits"]:
            shown = ", ".join(h["page"] + ("" if h["link_depth"] is not None else " (no link from Home)")
                              for h in e["shown_on"][:5]) or "no console page"
            out.append("- %s: %s (%s)" % (e["exhibit"], e["status"], shown))
        out.append("- Human finding: ______ (blank on purpose)")
    out += ["", "## Controls"]
    out += ["- %s: %s" % (k, v["result"] if "result" in v else (sorted(v) or "all own ids shown")) for k, v in receipt["controls"].items()]
    return "\n".join(out) + "\n"


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO)] + list(args), capture_output=True, text=True).stdout.strip()


def _write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tour the sandbox console for stages 10-14 (review aid).")
    parser.add_argument("five_driver_out")
    parser.add_argument("--out", required=True)
    parser.add_argument("--expect-engine")
    args = parser.parse_args(argv)
    servers = []
    try:
        base, out = Path(args.five_driver_out).resolve(), Path(args.out).resolve()
        if out == REPO or REPO in out.parents or out.exists():
            raise Refused("OUTPUT_DIR_NOT_NEW_OR_INSIDE_CHECKOUT")
        engine = _git("rev-parse", "HEAD")
        if args.expect_engine and engine != args.expect_engine:
            raise Refused("ENGINE_MISMATCH")
        projects = {}
        for stage in STAGES:
            found = sorted(base.glob(PROJECT[stage]))
            if len(found) != 1:
                raise Refused("STAGE_PROJECT_NOT_UNIQUE_%s" % stage)
            projects[stage] = found[0]
        fetchers = {}
        for key in ("10", "11", "12", "13"):
            server, tmp = serve(projects[key])
            servers.append((server, tmp))
            fetchers[key] = make_fetch(server.server_port)
        receipt = tour(projects, fetchers)
    except Refused as exc:
        print("REFUSED " + str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("REFUSED INPUT_UNREADABLE", file=sys.stderr)
        return 2
    finally:
        for server, tmp in servers:
            server.shutdown()
            server.server_close()
            tmp.cleanup()
    receipt["engine"] = engine
    receipt["dirty"] = bool(_git("status", "--porcelain"))
    out.mkdir(mode=0o700)
    _write_new(out / "tour-receipt.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    _write_new(out / "TOUR.md", render(receipt))
    print(json.dumps({"result": receipt["result"], "engine": engine, "failed": receipt["failed"]}))
    return 0 if receipt["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
