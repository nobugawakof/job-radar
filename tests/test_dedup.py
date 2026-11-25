import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar import dedup
from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config
from jobradar.models import RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State, utcnow

BASE = """【远程】杭州某金融科技公司招聘 Golang 后端工程师
岗位职责：负责支付核心系统的设计与开发；参与微服务架构演进；保障系统高可用。
任职要求：3 年以上 Go 开发经验，熟悉 MySQL、Redis、Kafka；有分布式系统经验优先。
薪资：25-35K·14 薪，全职远程，五险一金。简历请发 hr@fintech.example"""

SAME_STACK_OTHER_COMPANY = """深圳跨境电商团队急招 Golang 开发工程师
工作内容：负责订单与库存系统开发，对接海外电商平台接口，优化系统性能。
要求：两年以上 Go 经验，熟悉 MySQL、Redis，英语读写流利。
待遇：20-30K，可远程，弹性工作，年底双薪。联系微信 abc123"""

SAME_COMPANY_OTHER_ROLE = """【远程】杭州某金融科技公司招聘前端工程师
岗位职责：负责交易后台与管理系统的前端开发；与产品设计协作完成需求。
任职要求：3 年以上 React 开发经验，熟悉 TypeScript；有组件库经验优先。
薪资：20-30K·14 薪，全职远程，五险一金。简历请发 hr@fintech.example"""


class ChineseNearDuplicateTests(unittest.TestCase):
    def test_edited_repost_is_a_duplicate(self):
        self.assertTrue(dedup.is_duplicate(BASE, BASE.replace("25-35K·14 薪", "30-40K·15 薪")))

    def test_cross_post_with_new_title_is_a_duplicate(self):
        retitled = BASE.replace("【远程】杭州某金融科技公司招聘 Golang 后端工程师",
                                "[全职远程] 金融科技 Go 后端，薪资 25-35K")
        self.assertTrue(dedup.is_duplicate(BASE, retitled))

    def test_short_posts_differing_by_one_character(self):
        self.assertTrue(dedup.is_duplicate("招聘后端工程师，全球远程，需要五年经验",
                                           "招聘后端工程师，全球远程，需要三年经验"))

    def test_full_width_letters_match_ascii(self):
        wide = BASE.replace("Golang", "Ｇｏｌａｎｇ").replace("MySQL", "ＭｙＳＱＬ")
        self.assertEqual(dedup.content_hash(wide), dedup.content_hash(BASE))


class ChineseDistinctPostTests(unittest.TestCase):
    """Before CJK-aware shingling, these all scored 1.0 and shared a fingerprint,
    because only their English words were seen."""

    PAIRS = [
        ("杭州金融科技公司招 Golang 后端工程师，全职远程，简历发邮箱",
         "深圳跨境电商团队急招 Golang 开发，三年以上经验，可远程，私聊"),
        ("北京 AI 创业公司招算法工程师，远程，薪资面议", "上海游戏公司招 AI 美术设计师，坐班"),
        ("招聘 Java 后端，远程，Spring Boot MySQL Redis",
         "急招 Java 开发实习生，北京坐班，Spring Boot MySQL Redis"),
        (BASE, SAME_STACK_OTHER_COMPANY),
        (BASE, SAME_COMPANY_OTHER_ROLE),
    ]

    def test_not_duplicates(self):
        for a, b in self.PAIRS:
            with self.subTest(a=a[:20], b=b[:20]):
                self.assertFalse(dedup.is_duplicate(a, b))

    def test_distinct_fingerprints(self):
        for a, b in self.PAIRS:
            with self.subTest(a=a[:20], b=b[:20]):
                self.assertNotEqual(dedup.content_hash(a), dedup.content_hash(b))


class CompatibilityTests(unittest.TestCase):
    def test_english_fingerprint_unchanged(self):
        # Value computed by the pre-CJK implementation; existing state files
        # depend on English fingerprints staying identical.
        text = "We're hiring a remote React developer! DM me."
        self.assertEqual(dedup.content_hash(text),
                         "0d10449036283eee5902cfa579cd11867e5f2cbf84a091dfc8574c5c")

    def test_english_shingles_unchanged(self):
        self.assertEqual(dedup.shingles("Senior Python Engineer, remote"),
                         {"senior python engineer", "python engineer remote"})

    def test_legacy_hash(self):
        latin = "Acme | Senior Backend Engineer | Remote"
        self.assertEqual(dedup.legacy_content_hash(latin), dedup.content_hash(latin))
        cjk_only = "招聘后端工程师，全球远程，需要五年经验"
        self.assertEqual(dedup.legacy_content_hash(cjk_only),
                         __import__("hashlib").sha1(cjk_only.encode()).hexdigest()[:16])
        # Old fingerprints of mixed CJK + Latin text collided; never trust them.
        self.assertIsNone(dedup.legacy_content_hash("招聘 Java 后端，远程"))

    def test_mixed_text_keeps_both_kinds_of_shingle(self):
        sh = dedup.shingles("招聘 Java 后端 Spring Boot")
        self.assertIn("招聘", sh)
        self.assertIn("java spring boot", sh)


# ------------------------------------------------------------ via the pipeline
class _Collector:
    type = "rss"
    items: list[str] = []

    def __init__(self, *, name, tier, config, http):
        self.name = name

    def fetch(self, ctx):
        for i, text in enumerate(self.items):
            yield RawItem(source=self.name, external_id=f"{self.name}-{i}", raw_text=text,
                          url=f"https://example.com/{self.name}/{i}",
                          posted_at=utcnow() - timedelta(hours=1))


class _Transport:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append(text)


class PipelineDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(REGISTRY, {"rss": _Collector})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = State(Path(self.tmp.name) / "state.json")
        self.cfg = Config(keywords=[], remote_only=False, telegram_chat_id="1",
                          message_interval_seconds=0)

    def run_pipeline(self, items, sources=("v2ex-jobs",)):
        _Collector.items = items
        transport = _Transport()
        defs = [{"name": n, "type": "rss", "tier": "A"} for n in sources]
        summary = Pipeline(self.cfg, self.state, defs, transport=transport).run()
        return summary, transport

    def test_distinct_chinese_jobs_are_all_delivered(self):
        posts = ["杭州金融科技公司招聘 Golang 后端工程师，全职远程，简历发邮箱",
                 "深圳跨境电商团队招聘 Golang 开发，三年以上经验，可远程，私聊"]
        summary, _ = self.run_pipeline(posts)
        self.assertEqual((summary.duplicates_merged, summary.delivered), (0, 2))
        # …and a later run doesn't hide one as "already sent" either.
        summary, _ = self.run_pipeline(posts[:1] + ["北京团队招聘 Golang 工程师，坐班，薪资面议"])
        self.assertEqual(summary.delivered, 1)

    def test_cross_posted_chinese_job_is_sent_once_with_both_origins(self):
        summary, transport = self.run_pipeline([BASE], sources=("v2ex-jobs", "v2ex-remote"))
        self.assertEqual((summary.duplicates_merged, summary.delivered), (1, 1))
        self.assertIn("v2ex-jobs, v2ex-remote", transport.messages[0])

    def test_upgrade_does_not_resend_postings_an_older_version_sent(self):
        cjk_only = "招聘后端工程师，全球远程，需要五年经验，简历发邮箱"
        self.state.mark_sent(dedup.legacy_content_hash(cjk_only))  # as the old version stored it
        summary, _ = self.run_pipeline([cjk_only])
        self.assertEqual((summary.delivered, summary.already_sent_skipped), (0, 1))
        self.assertTrue(self.state.already_sent(dedup.content_hash(cjk_only)))  # migrated


if __name__ == "__main__":
    unittest.main()
