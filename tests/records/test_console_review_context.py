"""Synthetic prepared review context, sealed-export isolation and stable navigation."""
import json
import re
from html import unescape
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_export as ce, console_review
from campaign_tool.records import console_snapshot as cs, draft_import, mail_preview, mail_drafts
from tests.records.test_console_snapshot import Panel
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_draft_import import document


def section(page):
    return re.search(r'<section class="review-context">.*?</section>', page, re.S)[0]


class ReviewContextTests(Panel):
    def imported(self, *, target=False):
        doc = document()
        doc['source_run_id'] = RUN
        doc['body'] = 'Long body\n' + 'x' * 65000 + '\nTAIL-Ω <script>body</script>'
        doc['citations'].append({'source_type': 'tracker', 'source_id': 'tracker-row-1',
                                 'locator': '<script>locator</script>', 'as_of': '2026-11-15',
                                 'verification': 'unverified', 'limits': 'Original not obtained'})
        if target:
            doc['kind'] = 'draft-agency-summary'
            doc.pop('envelope')
            doc['citations'] = [{k: c[k] for k in ('source_type', 'source_id', 'locator', 'as_of', 'verification')}
                                for c in doc['citations']]
            doc['target'] = {'website': 'https://example.test', 'page': 'agency', 'section': 'ALPR'}
        return aq.add_draft(self.live, **draft_import.validate(doc))

    def test_live_sealed_parity_no_mail_or_config_reads_and_full_tail(self):
        row = self.imported()
        live = cs.LiveSource(self.live).review_context(row['id'])
        name = self.make_export()
        pin = cs.Pinner(str(self.root)).pin()
        source = cs.SnapshotSource(pin)
        self.assertEqual(source.review_context(row['id']), live)
        doc = json.loads((self.root / 'exports' / name / 'console-model/queue.json').read_text())
        self.assertEqual(doc['rows'], aq.load_queue(self.live))
        self.assertEqual(set(doc['review_context']), {r['id'] for r in doc['rows']})
        for row0 in doc['rows']:
            self.assertEqual(doc['history'][row0['id']], aq.history(self.live, row0['id']))
        mail = self.live / 'mail'
        mail.mkdir(exist_ok=True)
        mail.chmod(0)
        self.addCleanup(lambda: mail.chmod(0o700) if mail.exists() else None)
        before = tree_state(self.live)
        self.up()
        with mock.patch.object(mail_preview, 'preview', side_effect=AssertionError('live preview forbidden')), \
                mock.patch.object(mail_drafts, 'load_config', side_effect=AssertionError('config forbidden')), \
                mock.patch.object(aq, '_project_id', side_effect=AssertionError('host project identity forbidden')):
            page = self.get('/review?id=' + row['id'])
            home = self.get('/')
            dashboard = self.get('/dashboard')
        self.assertEqual(section(page), mail_preview.context_html(live))
        for rendered in (page, home, dashboard):
            self.assertIn('custodian@agency.example', rendered)
            self.assertIn('source_type: original', rendered)
            self.assertIn('source_type: tracker', rendered)
            self.assertIn('Original not obtained', rendered)
            self.assertIn('&lt;script&gt;locator&lt;/script&gt;', rendered)
            self.assertNotIn('<script>locator', rendered)
        citation_panel = section(page)
        self.assertNotIn('approval-payload', citation_panel)
        self.assertIn('TAIL-Ω &lt;script&gt;body&lt;/script&gt;', page)
        shown = unescape(re.search(r'<textarea id="body"[^>]*>\n(.*?)</textarea>', page, re.S)[1])
        self.assertEqual(shown, row['body'])
        self.assertEqual(tree_state(self.live), before)
        mail.chmod(0o700)

    def test_target_and_provenance_visible_without_mail_recipient(self):
        row = self.imported(target=True)
        context = cs.LiveSource(self.live).review_context(row['id'])
        self.assertFalse(context['send_ready'])
        self.make_export()
        self.up()
        for path in ('/review?id=' + row['id'], '/', '/dashboard'):
            page = self.get(path)
            self.assertIn('Target website', page)
            self.assertIn('https://example.test', page)
            self.assertIn('source_type: tracker', page)
        self.assertIsNone(context['recipient'])

    def test_unknown_recipient_does_not_guess_from_body_or_tracker(self):
        row = next(r for r in aq.load_queue(self.live) if r['id'] == self.pending_id)
        context = cs.LiveSource(self.live).review_context(row['id'])
        self.assertIsNone(context['recipient'])
        self.assertFalse(context['send_ready'])
        self.assertIsNone(context['citations'])
        self.make_export()
        self.up()
        page = self.get('/review?id=' + row['id'])
        self.assertIn('Not send-ready', page)
        self.assertIn('Sources unknown', page)

    def test_old_missing_and_stale_context_ask_refresh(self):
        row = self.imported()
        context = cs.LiveSource(self.live).review_context(row['id'])
        original_root = self.root
        for mode in ('old', 'missing', 'project', 'revision', 'evidence', 'queue'):
            self.root = original_root / mode
            self.root.mkdir(mode=0o700)
            def mutate(dest):
                path = dest / 'console-model/queue.json'
                doc = json.loads(path.read_text())
                if mode == 'old':
                    doc.pop('review_context')
                elif mode == 'missing':
                    doc['review_context'].pop(row['id'])
                else:
                    key = {'project': 'source_project_id', 'revision': 'revision',
                           'evidence': 'evidence_sha256', 'queue': 'queue_id'}[mode]
                    doc['review_context'][row['id']][key] = 'stale'
                path.write_text(json.dumps(doc))
            with self.subTest(mode=mode):
                self.make_export(mutate=mutate)
                source = cs.SnapshotSource(cs.Pinner(str(self.root)).pin())
                self.assertIsNone(source.review_context(row['id']))
                self.assertIn(mail_preview.REFRESH, mail_preview.context_html(source.review_context(row['id'])))
        self.assertEqual(context['queue_id'], row['id'])

    def three_pending_rows(self):
        self.imported()
        aq.add_draft(self.live, item_id='f' * 64, kind='reply-extension', agency_id='caltrans',
                     request_id='req-3', body='Third', evidence={'why': 'Separate'}, source_run_id=RUN)
        return sorted((r for r in aq.load_queue(self.live) if r['status'] == 'pending'), key=lambda r: r['id'])

    def test_previous_pending_draft_uses_sorted_pinned_rows(self):
        first, middle, last = self.three_pending_rows()

        def reverse_pending(dest):
            path = dest / 'console-model/queue.json'
            doc = json.loads(path.read_text())
            doc['rows'] = [last, middle, first] + [r for r in doc['rows'] if r['status'] != 'pending']
            path.write_text(json.dumps(doc))

        self.make_export(mutate=reverse_pending)
        self.up()
        before = tree_state(self.root)
        page = self.get('/review?id=' + middle['id'])
        for label in ('Next', 'Leave pending'):
            self.assertIn(f'<a href="/review?id={last["id"]}">{label}</a>', page)
        self.assertIn('<a href="/">Queue index</a>', page)
        self.assertNotIn('>Previous pending draft</a>', self.get('/review?id=' + first['id']))
        # Missing backward navigation or using export order instead of sorted IDs breaks this GET link.
        previous = re.findall(r'<a href="([^"]+)">Previous pending draft</a>', page)
        self.assertEqual(previous, ['/review?id=' + first['id']])
        self.assertIn('Draft 1 of 3', self.get(previous[0]))
        self.assertEqual(tree_state(self.root), before)

    def test_navigation_leave_and_reject_preserve_other_rows(self):
        rows = self.three_pending_rows()
        first, middle, last = rows
        before = (self.live / 'approvals/queue.jsonl').read_bytes()
        # Next can work even when the position is missing, zero-based, or in input order.
        for current, position, following in ((first, 1, middle), (middle, 2, last), (last, 3, first)):
            with self.subTest(position=position):
                links = console_review.navigation(current, reversed(rows))
                self.assertIn(f'Draft {position} of 3', links)
                for label in ('Next', 'Leave pending'):
                    self.assertIn(f'<a href="/review?id={following["id"]}">{label}</a>', links)
                self.assertIn('<a href="/">Queue index</a>', links)
        self.assertEqual((self.live / 'approvals/queue.jsonl').read_bytes(), before)
        aq.reject(self.live, middle['id'], 'operator', 'Deferred by human')
        after = {r['id']: r for r in aq.load_queue(self.live)}
        self.assertEqual(after[first['id']], first)
        self.assertEqual(after[last['id']], last)
        unknown = {'id': '0' * len(first['id']), 'status': 'pending'}
        self.assertNotRegex(console_review.navigation(unknown, after.values()), r'Draft \d+ of \d+')
        links = console_review.navigation(last, [after[middle['id']], last])
        self.assertIn('Draft 1 of 1', links)
        self.assertIn('<a href="/">Queue index</a>', links)
        self.assertIn('No other pending drafts.', links)
        self.assertNotIn('>Next</a>', links)
        self.assertNotIn('>Leave pending</a>', links)
        self.make_export()
        self.up()
        home = self.get('/')
        self.assertIn('/review?id=' + first['id'], home)
        self.assertIn('/review?id=' + last['id'], home)
        self.assertNotIn('/review?id=' + middle['id'], home)
        page = self.get('/review?id=' + middle['id'])
        self.assertIn(f'<a href="/review?id={last["id"]}">Next</a>', page)
        self.assertNotIn('>Leave pending</a>', page)
        self.assertNotIn('>Previous pending draft</a>', page)
        self.assertNotRegex(page, r'Draft \d+ of \d+')
        page = self.get('/review?id=' + last['id'])
        self.assertIn('Draft 2 of 2', page)
        self.assertIn(f'<a href="/review?id={first["id"]}">Next</a>', page)
        self.assertIn(f'<a href="/review?id={first["id"]}">Leave pending</a>', page)
        page = self.get('/review?id=' + unknown['id'], status=404)
        self.assertNotRegex(page, r'Draft \d+ of \d+')
        self.assertNotIn('>Previous pending draft</a>', page)
