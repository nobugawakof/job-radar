"""B2: "worldwide" (hire-from-anywhere) detection, which lets a job past the
region filter. False "worldwide" lets region-locked jobs through; false
"not worldwide" can hide a job when regions are set — both are tested."""

import unittest
from datetime import timedelta

from jobradar import extraction, geo
from jobradar.filters import Settings, evaluate
from jobradar.models import Posting
from jobradar.state import utcnow

WORLDWIDE = [
    "Acme | Backend | REMOTE (Worldwide) | Full-time",
    "Acme | Remote (Global) | Full-time | Python",
    "REMOTE, GLOBAL | Senior Engineer",
    "Location: Global",
    "Location: Remote - Anywhere",
    "Remote (Anywhere)",
    "We hire globally — work from anywhere in the world.",
    "Fully remote, location independent",
    "Remote – worldwide (except sanctioned countries)",
    "Anywhere in the world except the US",
    "We're remote-first and hire worldwide. Our HQ is in Berlin.",
    "Remote from anywhere. Async team.",
    "Remote (US & EU timezones preferred) — we hire worldwide",
    "remote in any timezone, apply below",
    "Globally remote team, we're hiring a backend engineer",
    "Remote in English-speaking team, hiring worldwide",
    "全球远程，招聘后端工程师",
    "远程（全球），招前端",
    "工作地点不限，远程办公",
    "不限国家，可远程",
]

NOT_WORLDWIDE = [
    # the word describes the company, not where you can work from
    "We are a global fintech. Onsite in Austin, TX.",
    "We serve international clients from our Berlin office.",
    "Trusted by 5,000 companies worldwide. Remote within the EU.",
    "Serving customers worldwide. Onsite in Paris.",
    "Hybrid, London. We operate in 40 countries worldwide.",
    "Our product is used globally. Remote within Canada.",
    "全球领先的金融科技公司，北京坐班",
    "全球化电商团队招聘运营，上海办公室",
    # "anywhere"/"remote" limited to a region
    "Remote anywhere in the US",
    "remote anywhere in the us",
    "Work from anywhere in Europe",
    "Work from anywhere in the EU or UK",
    "Remote (Anywhere in LATAM)",
    "Open to remote in Europe",
    # explicit restrictions override "worldwide"
    "Remote (worldwide) — US only",
    "US/Canada only. Fully remote.",
    "Remote role; worldwide (APAC timezones only)",
    "Remote, global team across 12 countries. Must be based in the UK.",
    "Remote, but you must be eligible to work in the United States.",
    "Our clients are international. Remote role for Singapore residents only.",
    "国内远程，全球化团队",
    "仅限国内候选人，远程",
]


class WorldwideTests(unittest.TestCase):
    def test_hire_from_anywhere(self):
        for text in WORLDWIDE:
            with self.subTest(text=text):
                self.assertTrue(extraction.extract(text).is_worldwide)

    def test_not_hire_from_anywhere(self):
        for text in NOT_WORLDWIDE:
            with self.subTest(text=text):
                self.assertFalse(extraction.extract(text).is_worldwide)

    def test_restriction_reports_the_phrase(self):
        self.assertEqual(geo.restriction("Remote anywhere in the US please"), "anywhere in the US")
        self.assertEqual(geo.restriction("国内远程"), "国内远程")
        self.assertIsNone(geo.restriction("Remote from anywhere. HQ in Berlin."))
        self.assertIsNone(geo.restriction("Remote in English, async"))  # not a place

    def test_onsite_is_never_worldwide(self):
        self.assertFalse(extraction.extract("Onsite in Lisbon. We hire worldwide.").is_worldwide)

    def test_location_field_counts(self):
        self.assertTrue(geo.worldwide("International", "We're hiring an engineer."))
        self.assertFalse(geo.worldwide("Austin, TX", "A global company."))


class RegionFilterEffectTests(unittest.TestCase):
    """What the user sees: with regions = Malaysia, region-locked jobs are
    dropped and hire-from-anywhere jobs still arrive."""

    def decide(self, text):
        e = extraction.extract(text)
        p = Posting(title=e.title, description=text, source="s", source_tier="A", source_url="",
                    posted_at=utcnow() - timedelta(hours=1), collected_at=utcnow(),
                    location=e.location, is_remote=e.is_remote, is_worldwide=e.is_worldwide)
        return evaluate(p, Settings(keywords=[], remote_only=False, regions=["Malaysia"])).decision

    def test_region_locked_jobs_no_longer_slip_through(self):
        for text in ("We are a global fintech hiring a backend engineer. Onsite in Austin, TX.",
                     "Remote anywhere in the US. Hiring a Python developer.",
                     "Remote (worldwide) — US only. Hiring a data engineer."):
            with self.subTest(text=text):
                self.assertEqual(self.decide(text), "reject")

    def test_worldwide_jobs_still_pass(self):
        for text in ("Acme | Backend Engineer | REMOTE (Worldwide)", "全球远程，招聘后端工程师"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(text), "pass")


if __name__ == "__main__":
    unittest.main()
