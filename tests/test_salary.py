"""B3: salary parsing — currencies, Chinese formats, pay periods — and the
salary filter built on it. A wrong reading can silently drop a good job
(e.g. "$20M Series A" read as a $20 salary), so non-salaries are tested too."""

import unittest
from datetime import timedelta

from jobradar.filters import Settings, evaluate
from jobradar.models import Posting
from jobradar.salary import annual_usd, parse_salary
from jobradar.state import utcnow

# (text, approx annual USD, or None = not a salary / can't be told)
CASES = [
    ("Salary: $120k - $150k", 150_000),
    ("Compensation: $120,000–$150,000 + equity", 150_000),
    ("€60.000 - €75.000 per year", 81_000),
    ("£45k-£55k", 69_850),
    ("Base salary: £70,000 – £85,000 p.a. plus bonus", 107_950),
    ("Salary: HKD 40,000 per month", 61_440),
    ("HK$35,000/month, hybrid", 53_760),
    ("S$7,000 - S$9,000 monthly", 79_920),
    ("SGD 8k/month", 71_040),
    ("CAD 110k-130k", 94_900),
    ("A$140k + super", 92_400),
    ("RM 12,000 per month", 30_240),
    ("Stipend: ₱60,000 per month for interns", 12_240),
    ("$60/hour contract", 124_800),
    ("Rate: 80-100 USD/hr", 208_000),
    ("Hourly rate: $75", 156_000),
    ("€550/day, 6-month contract", 154_440),
    ("Day rate £600, outside IR35", 198_120),
    ("Annual salary 90,000 EUR", 97_200),
    ("120k–150k USD", 150_000),
    ("compensation $95-110k, remote us", 110_000),
    ("We're a team of 25 founded in 2019. Pay: 6000-8000 EUR/month, 30 days PTO.", 103_680),
    ("Acme (Series B, raised $45M) | Senior Backend | Remote | $140k-$170k + 0.1% equity", 170_000),
    ("Hiring a Go dev. 3+ years experience. Budget up to $90/hr, 20 hrs/week.", 187_200),
    ("月薪 20-30K·14薪", 58_800),
    ("薪资：25-35K·14 薪，全职远程", 68_600),
    ("招聘Java后端，5年以上经验，薪资30-45K/月，16薪，团队规模50人", 100_800),
    ("【深圳】前端工程师 15-25k 13薪 双休", 45_500),
    ("年薪 50-80万", 112_000),
    ("全职远程，年薪40-60W，期权", 84_000),
    ("2万/月，远程", 33_600),
    ("待遇 15k-25k，五险一金", 42_000),
    ("¥800,000/年", 112_000),
    # not salaries, or not convertible — must not produce a number
    ("We raised a $20M Series A and are hiring.", None),
    ("Our ARR is $12M and we pay competitively.", None),
    ("401k match and great benefits", None),
    ("5+ years of experience required", None),
    ("Founded in 2015, 200 employees", None),
    ("We serve 10,000 customers in 40 countries.", None),
    ("90% of our customers renew", None),
    ("Salary: competitive, DOE", None),
    ("2-3 years experience, 8 hours a day", None),
    ("月入过万不是梦，加入我们", None),
    ("₹25 LPA", None),
    ("Salary: $8,000. We offer 4 weeks per year of PTO.", None),  # period unknown: don't take "per year" from PTO
]


class ParseTests(unittest.TestCase):
    def test_cases(self):
        for text, want in CASES:
            with self.subTest(text=text):
                got = annual_usd(parse_salary(text))
                if want is None:
                    self.assertIsNone(got)
                else:
                    self.assertIsNotNone(got)
                    self.assertAlmostEqual(got, want, delta=want * 0.05)

    def test_fields(self):
        s = parse_salary("月薪 20-30K·14薪")
        self.assertEqual((s.min, s.max, s.currency, s.period, s.months, s.raw),
                         (20_000, 30_000, "CNY", "month", 14, "月薪 20-30K·14薪"))
        s = parse_salary("HK$35,000/month, hybrid")
        self.assertEqual((s.currency, s.period, s.raw), ("HKD", "month", "HK$35,000/month"))
        self.assertEqual(parse_salary("¥5,000,000 per year, Tokyo (東京)").currency, "JPY")


class FilterTests(unittest.TestCase):
    def decide(self, text, **kw):
        p = Posting(title="Backend engineer", description=text, source="s", source_tier="A", source_url="",
                    posted_at=utcnow() - timedelta(hours=1), collected_at=utcnow(), salary=parse_salary(text))
        return evaluate(p, Settings(keywords=[], remote_only=False, **kw)).decision

    def test_minimum(self):
        self.assertEqual(self.decide("Salary: HKD 40,000 per month", min_salary_usd=80_000), "reject")
        self.assertEqual(self.decide("月薪 20-30K·14薪", min_salary_usd=50_000), "pass")
        self.assertEqual(self.decide("€550/day contract", min_salary_usd=100_000), "pass")

    def test_funding_never_fails_a_minimum(self):
        self.assertEqual(self.decide("We raised a $20M Series A. Hiring a backend engineer.",
                                     min_salary_usd=60_000), "pass")

    def test_unconvertible_salary_isnt_guessed(self):
        self.assertEqual(self.decide("Salary: $8,000.", min_salary_usd=60_000), "pass")

    def test_require_salary_means_stated(self):
        self.assertEqual(self.decide("Salary: $8,000.", require_salary=True), "pass")
        self.assertEqual(self.decide("Salary: competitive", require_salary=True), "reject")


if __name__ == "__main__":
    unittest.main()
