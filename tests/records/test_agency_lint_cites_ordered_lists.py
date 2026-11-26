"""C4 treats ordered-list markers as structure, not evidence or claims."""
import unittest

from campaign_tool.records.agency_run import lint_cites


C4 = "C4_unsourced_fact"


class OrderedListCitesTest(unittest.TestCase):
    def test_ordinals_are_not_facts(self):
        # Scanning the raw prefix flags multi-digit ordinals in non-factual items.
        for marker in ("10.", "10)"):
            for suffix in ("", "\t", "  Please clarify."):
                with self.subTest(marker=marker, suffix=suffix):
                    previous = "9. Please confirm.\n" if suffix else ""
                    self.assertEqual(lint_cites.lint(f"{previous}  {marker}{suffix}", set()), [])

    def test_unsourced_counts_stay_on_their_physical_lines(self):
        # An ordinal can add a duplicate warning or steal the continuation's line.
        for marker in ("10.", "10)"):
            for wrap, line in ((" ", 3), ("\r\n    ", 4)):
                with self.subTest(marker=marker, wrap=wrap):
                    text = f"Intro.\r\n\r\n  {marker} We received{wrap}12 documents."
                    self.assertEqual(lint_cites.lint(text, set()), [(line, C4)])

    def test_same_sentence_source_clears_count_in_ordered_item(self):
        # Splitting at the marker's period creates a bogus unsourced sentence.
        for marker in ("10.", "10)"):
            for wrap in (" ", "\n    "):
                with self.subTest(marker=marker, wrap=wrap):
                    text = f"{marker} We received 12 documents{wrap}(D3)."
                    self.assertEqual(lint_cites.lint(text, set()), [])

    def test_later_item_or_sentence_cannot_source_the_count(self):
        # Masking before finding item boundaries could let a later source leak back.
        for ending in ("\n11. See D3.", "\n11) See D3.", "\n- See D3.", ".\n    See D3."):
            with self.subTest(ending=ending):
                text = f"10) We received\n    12 documents{ending}"
                self.assertEqual(lint_cites.lint(text, set()), [(2, C4)])

    def test_marker_alone_after_a_finished_sentence_is_not_a_fact(self):
        # RETURN t_278456af: "10." alone after a nonblank line was left in the scanned text.
        for marker in ("10.", "10)", "  10.", "  10)", "10.\t"):
            for before, after in (("Please confirm.", "Please clarify."),
                                  ('He said "confirm."', "Please clarify."),
                                  ("Please confirm (D3).", "Please clarify."),
                                  ("Please confirm!", ""),
                                  ("Please confirm.", "We received 12 documents (D3).")):
                with self.subTest(marker=marker, before=before, after=after):
                    text = f"{before}\n{marker}" + (f"\n{after}" if after else "")
                    self.assertEqual(lint_cites.lint(text, set()), [])

    def test_marker_alone_at_paragraph_start_before_text_is_not_a_fact(self):
        for marker in ("10.", "10)"):
            with self.subTest(marker=marker):
                self.assertEqual(lint_cites.lint(f"Intro.\n\n{marker}\nPlease clarify.", set()), [])

    def test_marker_alone_item_still_reports_its_own_unsourced_count(self):
        # The lone marker is masked; the item's text is still scanned, on its own line.
        for marker in ("10.", "10)"):
            with self.subTest(marker=marker):
                text = f"Please confirm.\n{marker}\nWe received 12 documents."
                self.assertEqual(lint_cites.lint(text, set()), [(3, C4)])
                text = f"Please confirm.\n{marker}\nWe received 12 documents.\n11. See D3."
                self.assertEqual(lint_cites.lint(text, set()), [(3, C4)])

    def test_lone_number_continuing_an_unfinished_sentence_stays_a_fact(self):
        # Without a sentence end above, a lone "12." is wrapped text (a day, a count).
        for text, line in (("The letter was sent March\n12.", 1), ("We received\n12.", 2),
                           ("We received\n12)", 2), ("Sent on March 3, then\n12.", 1)):
            with self.subTest(text=text):
                self.assertEqual(lint_cites.lint(text, set()), [(line, C4)])

    def test_non_marker_numbers_remain_facts(self):
        # Neither decimals nor marker lookalikes (including wrapped dates) are prefixes.
        for text in ("12.5 was the reading.", "12)without a space", "See item 12. Please confirm.",
                     "The letter was sent March\n12."):
            with self.subTest(text=text):
                self.assertEqual(lint_cites.lint(text, set()), [(1, C4)])
