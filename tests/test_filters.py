import unittest

from riskyroller.filters import contains_disallowed_content


class ContentFilterTests(unittest.TestCase):
    def test_ordinary_text_passes(self) -> None:
        self.assertFalse(contains_disallowed_content("What is your favorite color?"))
        self.assertFalse(contains_disallowed_content(""))

    def test_denylist_terms_are_caught_case_insensitively(self) -> None:
        for word in ("faggot", "FAGGOT", "Retard"):
            with self.subTest(word=word):
                self.assertTrue(contains_disallowed_content(f"you are a {word}"))

    def test_leetspeak_variants_are_caught(self) -> None:
        self.assertTrue(contains_disallowed_content("f@gg0t"))
        self.assertTrue(contains_disallowed_content("r3t@rd"))

    def test_zero_width_padding_does_not_bypass(self) -> None:
        self.assertTrue(contains_disallowed_content("fag\u200bgot"))

    def test_fullwidth_lookalikes_do_not_bypass(self) -> None:
        # NFKC folds fullwidth Latin letters back to ASCII.
        self.assertTrue(contains_disallowed_content("\uff46\uff41\uff47\uff47\uff4f\uff54"))

    def test_word_boundaries_spare_longer_words(self) -> None:
        self.assertFalse(contains_disallowed_content("retarding the ignition timing"))


if __name__ == "__main__":
    unittest.main()
