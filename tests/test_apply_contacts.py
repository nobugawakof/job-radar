import unittest

from jobradar.apply.contacts import extract, extract_email, extract_telegram


class TelegramContactTests(unittest.TestCase):
    def test_tme_link_on_any_source(self):
        self.assertEqual(extract_telegram("Hiring a dev. Apply: t.me/hr_anna"), "@hr_anna")
        self.assertEqual(extract_telegram("apply https://telegram.me/hr_anna!"), "@hr_anna")

    def test_explicit_telegram_label_on_any_source(self):
        self.assertEqual(extract_telegram("Telegram: @dev_hire_bob"), "@dev_hire_bob")
        self.assertEqual(extract_telegram("TG @dev_hire_bob"), "@dev_hire_bob")
        self.assertEqual(extract_telegram("简历请发电报 @recruit_lily"), "@recruit_lily")

    def test_bare_handle_only_trusted_from_telegram_sources(self):
        text = "Hiring! DM @john_doe for details"
        self.assertIsNone(extract_telegram(text))  # e.g. an X / Bluesky handle
        self.assertEqual(extract_telegram(text, from_telegram=True), "@john_doe")
        self.assertEqual(extract_telegram("招聘后端，联系 @recruit_lily", from_telegram=True),
                         "@recruit_lily")

    def test_never_bots_own_channel_or_post_links(self):
        self.assertIsNone(extract_telegram("Apply via @ApplyJobsBot", from_telegram=True))
        self.assertIsNone(extract_telegram("contact @remotejobs", from_telegram=True,
                                           source_url="https://t.me/remotejobs/554"))
        self.assertIsNone(extract_telegram("see t.me/remotejobs/554"))
        self.assertIsNone(extract_telegram("join t.me/joinchat/AAAA"))

    def test_label_must_be_a_word(self):
        self.assertIsNone(extract_telegram("Find us at BTG @something_x"))

    def test_username_length_rules(self):
        self.assertIsNone(extract_telegram("Telegram: @abc"))  # Telegram needs 5+ chars


class EmailContactTests(unittest.TestCase):
    def test_prefers_hiring_mailbox(self):
        self.assertEqual(extract_email("press@acme.com or careers@acme.com"), "careers@acme.com")

    def test_skips_noreply_and_placeholders(self):
        self.assertIsNone(extract_email("noreply@acme.com and you@example.com"))

    def test_decodes_bracket_obfuscation_only(self):
        self.assertEqual(extract_email("jane [at] acme [dot] co [dot] uk"), "jane@acme.co.uk")
        self.assertIsNone(extract_email("meet us at acme dot com"))

    def test_trailing_period(self):
        self.assertEqual(extract_email("Send your CV to jobs@acme.io."), "jobs@acme.io")


class CombinedTests(unittest.TestCase):
    def test_extract_both(self):
        c = extract("招聘后端，联系 @recruit_lily 或者 简历发 jobs@acme.io", source_type="telegram")
        self.assertEqual((c.telegram, c.email), ("@recruit_lily", "jobs@acme.io"))
        self.assertTrue(c.any())

    def test_extra_text_is_searched(self):
        c = extract("We're hiring", extra="Apply: t.me/hr_anna")
        self.assertEqual(c.for_channel("telegram"), "@hr_anna")


if __name__ == "__main__":
    unittest.main()
