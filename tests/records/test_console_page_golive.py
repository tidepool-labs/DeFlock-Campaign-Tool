"""Numbered /golive checks through the real console, using synthetic inputs."""
import threading
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from campaign_tool.records import admin_panel, console_page_golive, console_runs_model
from campaign_tool.records import golive_catalog, golive_evidence, golive_ledger, golive_stages
from tests.records.test_admin_panel import AS_OF, RUN, Base, Http
from tests.records.test_golive_ledger import NOW, record, report, set_stage


class GoLivePageTests(Base):
    def setUp(self):
        super().setUp()
        self.target = Path(self.tmp.name).resolve() / "synthetic-target"
        self.reports = self.target / "reports"
        self.evidence = self.target / "golive-evidence"
        self.reports.mkdir(parents=True)
        self.evidence.mkdir()
        self.http = self.start(self.target / "run-report.json")
        clock = mock.patch.object(console_page_golive, "datetime", now=mock.Mock(return_value=NOW))
        clock.start()
        self.addCleanup(clock.stop)

    def start(self, report_path):
        server = admin_panel.build_server(self.project, RUN, actor="operator", as_of=AS_OF, system=False,
                                          run_report_path=report_path)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join()))
        return Http(server.server_port)

    def get(self, path="/golive", client=None):
        status, _, html = (client or self.http).send("GET", path)
        self.assertEqual(status, 200)
        return html

    @staticmethod
    def main(html):
        return ElementTree.fromstring(html[html.index('<main '):html.index('</main>') + len('</main>')])

    def test_c1_ledger_projection(self):
        """C1: Registry GET projects every ledger cell and counts only pipeline LIVE rows."""
        # Catch a report-only view, dropped evidence details, or incorrect splitting/counting.
        archive = report()
        set_stage(archive, "sync")
        self.write(self.reports / (archive["run_id"] + ".json"), archive)
        for data in (
            record("1", "live"), record("A1", "live"), record("3", "rehearsal"), record("3", "gate"),
            record("4", "gate", "fail"), record("5", "rehearsal"), record("6", "live", "fail"),
            record("15", "rehearsal", stamp="2026-01-01T00:00:00Z"),
            record("16", "gate", engine="b" * 40),
        ):
            stamp = str(data["recorded_at"]).replace("-", "").replace(":", "")
            path = self.evidence / f"{stamp}-{data['stage']}-{data['kind']}-cccccccc.json"
            path.touch(mode=0o600)
            self.write(path, data)

        rows = golive_ledger.build(golive_stages.load_reports(self.reports, now=NOW),
                                   golive_evidence.load(self.evidence), NOW)
        self.assertEqual({row["state"] for row in rows}, {"LIVE", "BUILT", "REHEARSED", "GATED", "BLOCKED"})
        self.assertEqual({row["live"]["status"] for row in rows}, {"report", "evidence", "missing"})
        html = self.get("/golive?view=fixture")
        self.assertIn('<a href="/golive" aria-current="page">Go-live</a>', html)
        main = self.main(html)
        self.assertEqual(main.findtext("./p[@role='status']"), "2 of 22 pipeline stages live")
        self.assertEqual([heading.text for heading in main.findall("h2")], ["Pipeline stages", "Console stages"])
        tables = main.findall("./div/table")
        self.assertEqual(len(tables), 2)
        for pipeline, table in zip((True, False), tables):
            self.assertEqual([cell.text for cell in table.findall("./thead/tr/th")],
                             ["Stage", "Name", "Rehearsed", "Gate", "Live", "State", "Next"])
            expected = []
            for row in rows:
                if row["pipeline"] != pipeline:
                    continue
                stage = row["stage"]
                next_step = "—" if row["next"] is None else f"{row['next']} ({golive_catalog.STAGES[stage].owner})"
                evidence = [" ".join(filter(None, (column.get("status"), column.get("run_id"),
                                                   column.get("recorded_at"), column.get("reason"))))
                            for column in (row["rehearsed"], row["gate"], row["live"])]
                expected.append([stage, row["label"], *evidence, row["state"], next_step])
            self.assertEqual([[cell.text for cell in row] for row in table.findall("./tbody/tr")], expected)

    def assert_diagnostic(self, expected, client=None):
        html = self.get(client=client)
        main = self.main(html)
        self.assertEqual([(node.tag, node.attrib, node.text) for node in main.findall("./*[@role='status']")],
                         [("p", {"class": "diag", "role": "status"}, expected)])
        self.assertNotIn("<table", html)
        for private in (str(self.target), "synthetic-target", "Traceback", "SECRET_PAYLOAD", "<script>"):
            self.assertNotIn(private, html)

    def test_c2_unconfigured(self):
        """C2: An unconfigured target does not masquerade as empty evidence."""
        self.assert_diagnostic("Go-live evidence is not configured on this console.", self.start(None))

    def test_c2_report_archive_unavailable(self):
        """C2: Report archive and inventory failures use fixed copy, without paths."""
        self.write(self.reports / "not-canonical.json", {})
        with mock.patch.object(console_runs_model, "FILE_LIMIT", 0):
            self.assert_diagnostic("Go-live reports unavailable.")
        self.reports.rename(self.target / "moved-reports")
        self.assert_diagnostic("Go-live reports unavailable.")
        self.reports.touch()
        self.assert_diagnostic("Go-live reports unavailable.")

    def test_c2_report_file_diagnostics(self):
        """C2: Invalid, unreadable and duplicate reports show only the escaped basename."""
        bad = self.reports / "z<script>.json"
        bad.symlink_to("SECRET_PAYLOAD")
        self.assert_diagnostic("Go-live reports unavailable (z<script>.json).")
        bad.rename(self.reports / "not-a-report")
        self.write(bad, {"body": "SECRET_PAYLOAD"})
        self.assert_diagnostic("Go-live reports unavailable (z<script>.json).")
        archive = report()
        self.write(self.reports / "a.json", archive)
        self.write(bad, archive)
        self.assert_diagnostic("Go-live reports unavailable (z<script>.json).")

    def test_c2_evidence_directory_unavailable(self):
        """C2: A missing or unusable evidence directory never becomes an all-missing ledger."""
        self.evidence.rename(self.target / "moved-evidence")
        self.assert_diagnostic("Go-live evidence unavailable.")
        self.evidence.touch()
        self.assert_diagnostic("Go-live evidence unavailable.")

    def test_c2_evidence_file_diagnostics(self):
        """C2: Evidence loader details never become markup or leak extra data."""
        self.write(self.evidence / "bad<script>.json", {"body": "SECRET_PAYLOAD"})
        self.assert_diagnostic("Go-live evidence unavailable (bad<script>.json).")
        # The file label now includes a repr-escaped control character, not a safe basename.
        (self.evidence / "bad<script>.json").rename(self.evidence / "bad\nname.json")
        self.assert_diagnostic("Go-live evidence unavailable.")

    def test_c2_unreadable_evidence_file(self):
        """C2: An I/O failure after evidence discovery stays a basename-only diagnostic."""
        name = "20260102T000000Z-1-live-cccccccc.json"
        (self.evidence / name).touch(mode=0o600)
        with mock.patch.object(golive_evidence, "_read_at", side_effect=PermissionError("SECRET_PAYLOAD")):
            self.assert_diagnostic(f"Go-live evidence unavailable ({name}).")

    def test_c3_get_only_and_scroll_regions(self):
        """C3: GET uses phone scroll regions and does not write; mutating methods cannot dispatch."""
        def snapshot():
            return {p.relative_to(self.target): (p.stat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
                    for p in (self.target, *self.target.rglob("*"))}

        (self.evidence / "untouched.txt").write_text("synthetic marker", encoding="utf-8")
        before = snapshot()
        html = self.get()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn('.scroll{overflow-x:auto;max-width:100%}', html)
        main = self.main(html)
        self.assertEqual(main.findtext("./p[@role='status']"), "0 of 22 pipeline stages live")
        regions = main.findall("./div[@role='region']")
        self.assertEqual([region.attrib for region in regions], [
            {"class": "scroll", "role": "region", "tabindex": "0", "aria-label": title}
            for title in ("Pipeline stages", "Console stages")
        ])
        self.assertEqual([len(region.findall("table")) for region in regions], [1, 1])
        self.assertEqual(main.findall(".//form"), [])
        self.assertEqual(self.http.post("/golive", {})[0], 404)
        self.assertEqual(self.http.send("PUT", "/golive")[0], 405)
        self.assertEqual(self.http.send("GET", "/golive/")[0], 404)
        unrelated = mock.Mock(path="/golive/")
        self.assertIs(console_page_golive.serve(unrelated), False)
        self.assertEqual(unrelated.mock_calls, [])
        self.assertEqual(snapshot(), before)
