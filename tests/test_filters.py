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
        for text in (
            "retarding the ignition timing",
            "the fire retardant coating held",
            "water retardation testing continues",
        ):
            with self.subTest(text=text):
                self.assertFalse(contains_disallowed_content(text))

    def test_bare_slur_is_blocked(self) -> None:
        self.assertTrue(contains_disallowed_content("nigger"))

    def test_trailing_suffixes_do_not_bypass(self) -> None:
        # A plural or a leetspeak-style trailing digit must not slip past a
        # boundary that only recognised a bare word.
        for text in ("niggers", "nigger1"):
            with self.subTest(text=text):
                self.assertTrue(contains_disallowed_content(text))

    def test_combining_and_format_padding_does_not_bypass(self) -> None:
        # Each of these inserts an invisible/format code point between every
        # letter of "nigger" to break up the literal match.
        paddings = {
            "soft hyphen": "\u00ad",
            "combining grapheme joiner": "\u034f",
            "combining low line": "\u0332",
            "hair space": "\u200a",
            "mongolian vowel separator": "\u180e",
        }
        for label, pad in paddings.items():
            with self.subTest(label=label):
                padded = pad.join("nigger")
                self.assertTrue(contains_disallowed_content(padded))

    def test_cyrillic_homoglyphs_do_not_bypass(self) -> None:
        # Cyrillic U+0430 and U+043E stand in for Latin a/o.
        self.assertTrue(contains_disallowed_content("f\u0430gg\u043et"))

    def test_confusable_folding_does_not_over_block(self) -> None:
        # Words that merely contain letters also present in the confusables
        # map, with no denylisted word anywhere, must stay allowed.
        self.assertFalse(contains_disallowed_content("cafe"))
        self.assertFalse(contains_disallowed_content("gorgeous"))


if __name__ == "__main__":
    unittest.main()
