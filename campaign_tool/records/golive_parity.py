"""Offline check that a saved /golive page and `records golive --json` agree.

Reads two saved files and prints stage IDs, states and summary counts only. It
never fetches a page or handles a login, so it cannot show who may view /golive;
a saved login or denial page is simply refused as not a go-live page.
"""

import argparse
import json
import os
import re
import stat
import sys
from html.parser import HTMLParser

from . import golive_catalog

STATES = frozenset({"BLOCKED", "BUILT", "REHEARSED", "GATED", "LIVE"})
MAX_BYTES = 2 * 1024 * 1024
_TABLES = {"Pipeline stages": True, "Console stages": False}
_HEADINGS = ("Stage", "Name", "Rehearsed", "Gate", "Live", "State", "Next")
_SUMMARY = re.compile(r"(\d{1,6}) of (\d{1,6}) pipeline stages live")
_NOT_PAGE = "not a go-live page"
# The console shell ends every page with </main></body></html>; a page cut before that is truncated.
_CLOSERS = ("main", "body", "html")


class _Input(ValueError):
    """Unreadable or invalid input; the message is fixed text, never file content."""


def _not_page(reason):
    return ValueError(f"{_NOT_PAGE} ({reason})")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.statuses = []
        self.closers = []  # closing document tags seen, in order
        self.tables = {}  # caption -> [(stage, state), ...]
        self._status = None
        self._table = None  # None outside a table, else [caption, header rows, body rows]
        self._row = None
        self._cell = None
        self._caption = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "p" and ("role", "status") in attrs:
            self._status = []
        elif tag == "table":
            if self._table is not None:
                raise _not_page("nested table")
            self._table = {"caption": None, "head": [], "body": []}
        elif self._table is None:
            return
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = (tag, [])

    def handle_endtag(self, tag):
        if tag in _CLOSERS:
            self.closers.append(tag)
        if tag == "p" and self._status is not None:
            self.statuses.append(" ".join("".join(self._status).split()))
            self._status = None
        elif self._table is None:
            return
        elif tag == "caption" and self._caption is not None:
            self._table["caption"] = " ".join("".join(self._caption).split())
            self._caption = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append((self._cell[0], " ".join("".join(self._cell[1]).split())))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            kinds = {kind for kind, _ in self._row}
            if kinds == {"th"}:
                self._table["head"].append(tuple(text for _, text in self._row))
            elif kinds == {"td"}:
                self._table["body"].append(tuple(text for _, text in self._row))
            else:
                raise _not_page("malformed row")
            self._row = None
        elif tag == "table":
            caption = self._table["caption"]
            if caption in _TABLES:
                if caption in self.tables:
                    raise _not_page("repeated table")
                self.tables[caption] = self._table
            self._table = None

    def handle_data(self, data):
        if self._status is not None:
            self._status.append(data)
        if self._caption is not None:
            self._caption.append(data)
        if self._cell is not None:
            self._cell[1].append(data)


def _summary_counts(text):
    match = _SUMMARY.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError("summary line is not 'N of M pipeline stages live'")
    return int(match[1]), int(match[2])


def parse_page(html_text):
    """Return {summary, rows: {stage: state}} from the real /golive markup."""
    parser = _PageParser()
    parser.feed(html_text)
    parser.close()
    if parser._table is not None:
        raise _not_page("truncated table")
    if tuple(parser.closers) != _CLOSERS:
        raise _not_page("truncated page")
    if set(parser.tables) != set(_TABLES):
        raise _not_page("expected tables missing")
    summaries = [text for text in parser.statuses if _SUMMARY.fullmatch(text)]
    if len(summaries) != 1:
        raise _not_page("summary line missing")
    rows = {}
    for caption, pipeline in _TABLES.items():
        table = parser.tables[caption]
        if table["head"] != [_HEADINGS]:
            raise _not_page("unexpected columns")
        for cells in table["body"]:
            if len(cells) != len(_HEADINGS):
                raise _not_page("malformed row")
            stage, state = cells[0], cells[5]
            rule = golive_catalog.STAGES.get(stage)
            if rule is None:
                raise _not_page("unknown stage")
            if stage in rows:
                raise _not_page("duplicate stage")
            if rule.pipeline != pipeline:
                raise _not_page("stage in the wrong table")
            if state not in STATES:
                raise _not_page("unknown state")
            rows[stage] = state
    return {"summary": summaries[0], "rows": rows}


def _live(rows):
    return sum(1 for stage, state in rows if state == "LIVE" and golive_catalog.STAGES[stage].pipeline)


def _pipeline_total():
    return sum(rule.pipeline for rule in golive_catalog.STAGES.values())


def _cli_view(cli):
    """Validate the `golive_ledger --json` document; return (rows list, printed count, total)."""
    if not isinstance(cli, dict) or not isinstance(cli.get("rows"), list):
        raise ValueError("ledger document has no rows list")
    rows = []
    for row in cli["rows"]:
        state = row.get("state") if isinstance(row, dict) else None
        # Type first: an unhashable state (list, dict) must be invalid input, not a TypeError.
        if not isinstance(row, dict) or not isinstance(row.get("stage"), str) \
                or not isinstance(state, str) or state not in STATES:
            raise ValueError("ledger row has no valid stage and state")
        rows.append((row["stage"], row["state"]))
    live_count = cli.get("live_count")
    if type(live_count) is not int:
        raise ValueError("ledger document has no integer live_count")
    return rows, live_count, *_summary_counts(cli.get("summary"))


def compare(page, cli):
    """Compare a parse_page() result with a parsed `golive_ledger --json` document.

    Stage differences are {stage, page, cli} with None for a stage that is absent.
    Count checks use pseudo-stages in parentheses; for the two '... vs rows' checks
    `page` is the count that side printed and `cli` the count recomputed from its rows.
    """
    page_rows = list(page["rows"].items())
    page_printed, page_total = _summary_counts(page["summary"])
    cli_rows, live_count, cli_printed, cli_total = _cli_view(cli)

    differences = []
    page_states = page["rows"]
    cli_states, repeated = {}, set()
    for stage, state in cli_rows:
        if stage in cli_states:
            repeated.add(stage)
        cli_states.setdefault(stage, state)
    known = list(golive_catalog.STAGES)
    for stage in known + sorted(set(cli_states) - set(known)):
        shown = "DUPLICATE" if stage in repeated else cli_states.get(stage)
        # A catalogue stage missing from both sides is a difference too: nothing was compared.
        if page_states.get(stage) != shown or (stage in golive_catalog.STAGES and shown is None):
            differences.append({"stage": stage, "page": page_states.get(stage), "cli": shown})

    page_live, cli_live = _live(page_rows), _live(
        (stage, state) for stage, state in cli_rows if stage in golive_catalog.STAGES)
    expected_total = _pipeline_total()
    checks = (
        ("(page summary vs rows)", page_printed, page_live),
        ("(cli summary vs rows)", cli_printed, cli_live),
        ("(cli live_count vs rows)", live_count, cli_live),
        ("(page total)", page_total, expected_total),
        ("(cli total)", cli_total, expected_total),
        ("(count)", page_printed, cli_printed),
    )
    for name, printed, other in checks:
        if printed != other:
            differences.append({"stage": name, "page": printed, "cli": other})
    return {"agree": not differences, "differences": differences,
            "page_count": page_printed, "cli_count": cli_printed}


def _read(path):
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise _Input("not a regular file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _Input("unreadable file") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _Input("not a regular file")
            data = handle.read(MAX_BYTES + 1)
    except OSError:  # An I/O error after a successful open (e.g. EIO on read) is still an unreadable input.
        raise _Input("unreadable file") from None
    if len(data) > MAX_BYTES:
        raise _Input("file is over 2 MiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Input("file is not UTF-8 text") from None


_PSEUDO = frozenset({"(page summary vs rows)", "(cli summary vs rows)", "(cli live_count vs rows)",
                    "(page total)", "(cli total)", "(count)"})


def _label(stage):
    """Only catalogue stage IDs and this module's own count names may be printed; never input text."""
    return stage if stage in golive_catalog.STAGES or stage in _PSEUDO else "(unrecognised stage)"


def _show(value):
    return "missing" if value is None else value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", required=True, help="saved /golive page (HTML file)")
    parser.add_argument("--cli", required=True, help="saved `records golive --json` output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        page = parse_page(_read(args.page))
        try:
            cli = json.loads(_read(args.cli))
        except json.JSONDecodeError:
            raise _Input("ledger file is not JSON") from None
        except RecursionError:  # A deeply nested document well under 2 MiB: still invalid input.
            raise _Input("ledger file is nested too deeply") from None
        result = compare(page, cli)
    except ValueError as exc:
        print(f"golive_parity: {exc}", file=sys.stderr)
        return 2
    result = dict(result, differences=[dict(item, stage=_label(item["stage"])) for item in result["differences"]])
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["agree"]:
        print(f"agree: {len(golive_catalog.STAGES)} stages match; {result['page_count']} of "
              f"{_pipeline_total()} pipeline stages live on both")
    else:
        print(f"differ: {len(result['differences'])} difference(s)")
        for item in result["differences"]:
            print(f"  {item['stage']}: page={_show(item['page'])} cli={_show(item['cli'])}")
    return 0 if result["agree"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
