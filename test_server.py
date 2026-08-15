import unittest

from server import _parse_dep_cis_html, _parse_ptaps_html, detect_block, parse_bbl


class SharedScraperTests(unittest.TestCase):
    def test_bbl_is_split_for_portal_queries(self):
        self.assertEqual(("1", "1", "1"), parse_bbl("1000010001"))

    def test_bad_bbl_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bbl("123")

    def test_block_page_is_never_clean_data(self):
        self.assertIn("blocked", detect_block("Access Denied", "https://example.test", ["Property Profile"]))

    def test_ptaps_parser_preserves_source_and_balance(self):
        result = _parse_ptaps_html("Total Due: $1,234.56", "1000010001")
        self.assertEqual("ptaps_live", result["source"])
        self.assertGreaterEqual(result["totals"]["outstanding"], 0)

    def test_dep_parser_preserves_source_and_balance(self):
        result = _parse_dep_cis_html("Amount Due: $42.50", bbl="1000010001")
        self.assertEqual("cis_live", result["source"])
        self.assertEqual(42.50, result["totals"]["outstanding"])


if __name__ == "__main__":
    unittest.main()
