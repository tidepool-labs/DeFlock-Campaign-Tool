"""Offline Home go-live contract tests with synthetic report and evidence archives."""
import re
import tempfile
import threading
from pathlib import Path

from campaign_tool.records import admin_panel, golive_catalog
from tests.records.test_admin_panel import AS_OF, Base, Http
from tests.records.test_console_home import RUN, fixture
from tests.records.test_golive_ledger import record, report, set_stage


class HomeGoLiveTests(Base):
    UNAVAILABLE = "Go-live status unavailable."

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name) / "project"
        self.project.mkdir(mode=0o700)
        fixture(self.project)
        self.target = Path(folder.name) / "synthetic-target"
        self.reports = self.target / "reports"
        self.evidence = self.target / "golive-evidence"
        self.reports.mkdir(parents=True)
        self.evidence.mkdir()
        self.client = self.start(self.target / "run-report.json")

    def start(self, report_path):
        server = admin_panel.build_server(self.project, RUN, actor="operator", as_of=AS_OF, system=False,
                                          run_report_path=report_path)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join()))
        return Http(server.server_port)

    def write_live(self, stage):
        return self.write(self.evidence / f"20260102T000000Z-{stage}-live-cccccccc.json", record(stage, "live"))

    def get(self, expected, client=None):
        status, _, page = (client or self.client).send("GET", "/")
        self.assertEqual(status, 200)
        line = f'<p id="golive-status" role="status">{expected} <a href="/golive">Go-live</a></p>'
        self.assertEqual(re.findall(r'<p id="golive-status"[^>]*>.*?</p>', page), [line])
        self.assertLess(page.index(line), page.index('<section id="system">'))
        return page.replace(line + "\n", "", 1)

    def test_c1_first_ordered_step_and_pipeline_count(self):
        """C1: The first planned item need not be the first unfinished ledger row."""
        # Counting A1 or choosing the first unfinished row would give the wrong line.
        archive = report()
        set_stage(archive, "sync")
        self.write(self.reports / (archive["run_id"] + ".json"), archive)
        self.write_live("A1")
        self.get("1 of 22 pipeline stages live · Next: pipeline — Rehearse offline for stage 7 (Catalogue / adoption).")

    def test_c1_all_live(self):
        """C1: An empty plan is complete, not unavailable or a missing first item error."""
        for stage in golive_catalog.STAGES:
            self.write_live(stage)
        self.get("22 of 22 pipeline stages live · No remaining steps.")

    def test_c2_missing_or_invalid_inputs(self):
        """C2: Missing or invalid archives change only the line, with no error detail."""
        self.write_live("1")
        baseline = self.get("1 of 22 pipeline stages live · Next: pipeline — Rehearse offline for stage 2 (Email intake / sync).")
        with self.subTest(input="unconfigured"):
            self.assertEqual(self.get(self.UNAVAILABLE, self.start(None)), baseline)
        for directory in (self.reports, self.evidence):
            with self.subTest(input=directory.name, failure="missing"):
                moved = directory.rename(self.target / "moved")
                self.assertEqual(self.get(self.UNAVAILABLE), baseline)
                moved.rename(directory)
            with self.subTest(input=directory.name, failure="invalid"):
                invalid = self.write(directory / "synthetic-private-detail.json", {"invalid": True})
                self.assertEqual(self.get(self.UNAVAILABLE), baseline)
                invalid.rename(directory / "ignored.txt")

    def test_c3_other_system_lines_unchanged(self):
        """C3: The banner leaves the existing System links, counts and order intact."""
        # The whole-page pin uses a bare Home model. Also check the route with go-live enabled.
        page = self.get(self.UNAVAILABLE, self.start(None))
        self.assertEqual(re.findall(r'<section id="system">.*?</section>', page), [
            '<section id="system"><h2>System</h2><ul>'
            '<li><a href="/runs">Last run</a>: unknown</li>'
            '<li><a href="/health">Health</a>: unknown</li>'
            '<li><a href="/drafts?state=pending">Drafts waiting for a decision</a>: 3</li>'
            '<li><a href="/health">Data age</a>: unknown</li></ul></section>'
        ])
