"""B4: region filter — countries/groupings compared as sets, and jobs that
don't say where they hire are not silently dropped (SRS FR-17..FR-20)."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar import extraction, places
from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config
from jobradar.filters import Settings, evaluate
from jobradar.models import Posting, RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State, utcnow

# (your regions, job post, expected): pass / reject / unknown (= no place stated)
CASES = [
    (["Germany"], "Remote (EU only). Hiring a backend engineer.", "pass"),
    (["Germany"], "Backend engineer — remote within Europe", "pass"),
    (["Germany"], "Remote, EMEA timezones. Hiring a Go developer.", "pass"),
    (["Germany"], "Remote (US only). Hiring a backend engineer.", "reject"),
    (["EU"], "Hiring: backend engineer, Berlin or remote in Germany", "pass"),
    (["EU"], "Location: Toronto, Canada. Backend engineer.", "reject"),
    (["Malaysia"], "Remote — APAC only. Frontend developer.", "pass"),
    (["Malaysia"], "Location: Kuala Lumpur (hybrid). Frontend dev.", "pass"),
    (["Malaysia"], "Remote within Southeast Asia, hiring React dev", "pass"),
    (["Malaysia"], "Remote (LATAM). React developer.", "reject"),
    (["Malaysia"], "Remote (US/EU). Senior Go engineer.", "reject"),
    (["Hong Kong"], "Hiring in Greater China: backend engineer", "pass"),
    (["中国"], "国内远程，招聘后端工程师", "pass"),
    (["中国"], "北京，后端开发工程师", "pass"),
    (["中国"], "海外远程，美国时区，招聘后端", "reject"),
    (["China"], "上海办公室招聘前端", "pass"),
    (["马来西亚"], "Remote — Southeast Asia. React developer.", "pass"),
    (["深圳"], "Location: Shenzhen. 后端开发.", "pass"),
    (["Japan"], "Remote (Americas). Backend engineer.", "reject"),
    (["LATAM"], "Remote (Americas). Backend engineer.", "pass"),
    (["APAC"], "Location: Tokyo. Backend engineer.", "pass"),
    (["Singapore"], "Acme (HQ in Singapore) hiring remote engineers across APAC", "pass"),
    (["Singapore"], "Remote within the Nordics only — data engineer", "reject"),
    (["Poland"], "Remote (CEE). Java developer.", "pass"),
    (["Poland"], "Location: Remote, Europe (CET ±2h)", "pass"),
    (["India"], "remote anywhere in the us, react developer", "reject"),
    (["Australia"], "Location: Sydney or Melbourne. Frontend.", "pass"),
    (["New Zealand"], "Remote (ANZ). Backend engineer.", "pass"),
    (["UK"], "Remote UK&I only. Backend engineer.", "pass"),
    (["UK"], "Remote (EU only). Backend engineer.", "reject"),
    (["Hong Kong", "Singapore"], "Location: Shenzhen. Backend.", "reject"),
    (["Bay Area"], "Location: Bay Area, CA. Backend engineer.", "pass"),
    (["Bay Area"], "Location: London. Backend engineer.", "reject"),
    (["Germany"], "Acme | Backend Engineer | REMOTE (Worldwide)", "pass"),
    (["Germany", "Worldwide"], "Location: Berlin. Backend engineer.", "pass"),
    # nothing about location — must not be silently dropped (FR-20)
    (["Germany"], "We're hiring a backend engineer. Python, Postgres. DM me.", "unknown"),
    (["Malaysia"], "招聘后端工程师，远程，薪资面议", "unknown"),
    (["Malaysia"], "Send us your CV! Remote role, salary in USD. Python dev.", "unknown"),
    (["Germany"], "Talk to Sofia or Austin from our team. Remote backend role.", "unknown"),
]


def posting(text):
    e = extraction.extract(text)
    return Posting(title=e.title, description=text, source="s", source_tier="A", source_url="",
                   posted_at=utcnow() - timedelta(hours=1), collected_at=utcnow(),
                   location=e.location, is_remote=e.is_remote, is_worldwide=e.is_worldwide)


class RegionFilterTests(unittest.TestCase):
    def test_cases(self):
        for regions, text, want in CASES:
            with self.subTest(regions=regions, text=text):
                r = evaluate(posting(text), Settings(keywords=[], remote_only=False, regions=regions))
                got = "unknown" if r.reason in ("location_not_stated", "review") else r.decision
                self.assertEqual(got, want)

    def test_unknown_location_can_be_dropped(self):
        r = evaluate(posting("We're hiring a backend engineer. DM me."),
                     Settings(keywords=[], remote_only=False, regions=["Germany"], unknown_location="drop"))
        self.assertEqual((r.decision, r.reason), ("reject", "location_not_stated"))

    def test_no_regions_means_no_region_filter(self):
        self.assertEqual(evaluate(posting("Location: Toronto."), Settings(keywords=[], remote_only=False)).decision,
                         "pass")


class GazetteerTests(unittest.TestCase):
    def test_groupings_expand_to_countries(self):
        self.assertIn("DE", places.find("Remote (EU only)"))
        self.assertIn("MY", places.find("APAC"))
        self.assertEqual(places.find("Greater China"), {"CN", "HK", "MO", "TW"})
        self.assertEqual(places.find("国内远程"), {"CN"})

    def test_no_false_places(self):
        for text in ("contact us", "salary in USD", "Sofia from HR", "our Milan team lead", "Austin, our CTO",
                     "Remote, OR hybrid", "REMOTE, OK?", "Senior, IN office"):
            with self.subTest(text=text):
                self.assertEqual(places.find(text), set())

    def test_city_state_format(self):
        self.assertEqual(places.find("Onsite in Austin, TX."), {"US"})
        self.assertEqual(places.find("Toronto, ON"), {"CA"})

    def test_onsite_job_with_unrecognised_place_is_not_sent_as_unknown(self):
        r = evaluate(posting("Hiring a backend engineer. Onsite in Ipoh."),
                     Settings(keywords=[], remote_only=False, regions=["Germany"]))
        self.assertEqual(r.decision, "reject")

    def test_abbreviations_in_any_case_only_when_geographic(self):
        self.assertEqual(places.find("anywhere in the us"), set())
        self.assertEqual(places.find("anywhere in the us", any_case=True), {"US"})

    def test_resolve_what_users_type(self):
        self.assertEqual(places.resolve("Germany"), {"DE"})
        self.assertEqual(places.resolve("us"), {"US"})
        self.assertEqual(len(places.resolve("EU")), 27)
        self.assertEqual(places.resolve("马来西亚"), {"MY"})
        self.assertEqual(places.resolve("Narnia"), set())


class PipelineTests(unittest.TestCase):
    def test_card_says_location_not_stated(self):
        class Src:
            type = "rss"

            def __init__(self, **kw):
                pass

            def fetch(self, ctx):
                yield RawItem(source="s", external_id="1", url="https://x/1", posted_at=utcnow() - timedelta(hours=1),
                              raw_text="We're hiring a remote backend engineer. Python, Postgres. DM me.")
                yield RawItem(source="s", external_id="2", url="https://x/2", posted_at=utcnow() - timedelta(hours=1),
                              raw_text="We're hiring a remote frontend engineer. Remote (US only). React.")

        class T:
            def __init__(self):
                self.messages = []

            def send_message(self, chat, text):
                self.messages.append(text)

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(REGISTRY, {"rss": Src}):
            t = T()
            s = Pipeline(Config(keywords=[], regions=["Malaysia"], unknown_location="send", telegram_chat_id="1",
                                message_interval_seconds=0),
                         State(Path(d) / "db"), [{"name": "s", "type": "rss"}], transport=t).run()
        self.assertEqual((s.delivered, s.rejected_region), (1, 1))
        self.assertIn("Location: not stated", t.messages[0])


if __name__ == "__main__":
    unittest.main()
