"""Timeline route coverage for dates at the limits of Pacific conversion."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from campaign_tool.records import console_page_timeline
from tests.records.test_console_timeline import ALPHA, ROW, RUN, event, request, write


class TimelineDateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def get_rows(self, query=""):
        handler = SimpleNamespace(
            options={"project": self.project, "run_id": RUN},
            path="/timeline" + query,
            _fail=Mock(),
            _send=Mock(),
        )
        console_page_timeline.serve(handler)
        handler._fail.assert_not_called()
        handler._send.assert_called_once()
        status, body = handler._send.call_args.args
        self.assertEqual(status, 200)
        return ROW.findall(body)

    def test_unconvertible_event_is_undated_without_losing_the_timeline(self):
        # An uncaught astimezone overflow used to abort the whole route. Falling back
        # to the timestamp's literal day would instead make it match a month filter.
        for stamp in ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59-12:00"):
            with self.subTest(stamp=stamp):
                write(self.project, [request(ALPHA, "R-1", [
                    event("call", stamp, "Boundary event"),
                    event("call", "2026-11-01T05:00:00Z", "Month-end event"),
                ])])
                dated, undated = self.get_rows()
                self.assertTrue(dated.startswith("<tr><td>2026-10-31</td>"))
                self.assertIn("<td>Month-end event</td><td>DATED</td>", dated)
                self.assertTrue(undated.startswith("<tr><td>undated</td>"))
                self.assertIn("<td>Boundary event</td><td>UNKNOWN</td>", undated)
                self.assertEqual(self.get_rows("?month=2026-10"), [dated])
                self.assertEqual(self.get_rows("?month=2026-11"), [])
                self.assertEqual(self.get_rows("?month=" + stamp[:7]), [])

    def test_representable_boundary_dates_remain_dated(self):
        # Rejecting the entire min/max year to avoid overflow would hide valid dates.
        for stamp, day in (
            ("0001-01-01", "0001-01-01"),
            ("0001-01-01T08:00:00Z", "0001-01-01"),
            ("9999-12-31", "9999-12-31"),
            ("9999-12-31T23:59:59Z", "9999-12-31"),
        ):
            with self.subTest(stamp=stamp):
                write(self.project, [request(ALPHA, "R-1", [event("call", stamp)])])
                row, = self.get_rows("?month=" + day[:7])
                self.assertTrue(row.startswith(f"<tr><td>{day}</td>"))
                self.assertIn("<td>DATED</td>", row)
