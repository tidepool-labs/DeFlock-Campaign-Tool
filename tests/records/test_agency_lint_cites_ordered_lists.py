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

    def test_non_marker_numbers_remain_facts(self):
        # Neither decimals nor marker lookalikes (including wrapped dates) are prefixes.
        for text in ("12.5 was the reading.", "12)without a space", "See item 12. Please confirm.",
                     "The letter was sent March\n12."):
            with self.subTest(text=text):
                self.assertEqual(lint_cites.lint(text, set()), [(1, C4)])
