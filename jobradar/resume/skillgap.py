"""Which skills a job post asks for, and which of them your profile shows.
No AI: a fixed vocabulary plus a few safe patterns, so it's free, instant and
predictable.

Each skill the post names lands in one of three groups:

* ``existing``  — in your Technical skills (or a project's tech stack);
* ``supported`` — not listed as a skill, but your experience or projects
  mention it, so it's fair to bring forward;
* ``gap``       — nowhere in your profile. Reported to you; never added to the
  résumé (the AI is told so, and the résumé checks would drop it anyway).

Coverage is (existing + supported) / all, like an applicant-tracking system's
keyword match. (Idea from career-ops' jd-skill-gap / keyword-match, written
independently.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import profile as prof

# canonical name -> extra spellings. Matching is case-insensitive except where
# noted in _CASE_SENSITIVE (short words that are also ordinary English).
VOCAB: dict[str, tuple[str, ...]] = {
    # languages
    "Go": ("Golang",), "Python": (), "Java": (), "Kotlin": (), "Scala": (), "Rust": (), "C++": ("cpp",),
    "C#": ("csharp",), ".NET": ("dotnet",), "JavaScript": ("JS",), "TypeScript": ("TS",), "PHP": (), "Ruby": (),
    "Swift": (), "Objective-C": (), "Dart": (), "Elixir": (), "Erlang": (), "Haskell": (), "Lua": (), "Perl": (),
    "SQL": (), "Bash": ("Shell",), "Solidity": (), "MATLAB": (),
    # web / frameworks
    "React": ("React.js", "ReactJS"), "Vue": ("Vue.js", "VueJS"), "Angular": (), "Next.js": (), "Node.js": ("Node", "NodeJS"),
    "Django": (), "Flask": (), "FastAPI": (), "Spring": ("Spring Boot", "SpringBoot"), "Spring Cloud": (),
    "Rails": ("Ruby on Rails",), "Laravel": (), "Express": (), "Gin": (), "Echo": (), "gRPC": (), "GraphQL": (),
    "REST": ("RESTful",), "WebSocket": ("WebSockets",), "Flutter": (), "React Native": (), "Tailwind": (),
    "MyBatis": (),
    # data / storage
    "MySQL": (), "PostgreSQL": ("Postgres",), "Redis": (), "MongoDB": ("Mongo",), "Elasticsearch": ("ES",),
    "ClickHouse": (), "Cassandra": (), "DynamoDB": (), "SQLite": (), "Oracle": (), "TiDB": (), "RocksDB": (),
    "Snowflake": (), "BigQuery": (), "Hadoop": (), "Spark": (), "Flink": (), "Hive": (), "Airflow": (),
    "dbt": (), "Kafka": (), "RabbitMQ": (), "RocketMQ": (), "NATS": (), "Pulsar": (), "ETL": (),
    # cloud / devops
    "AWS": (), "GCP": ("Google Cloud",), "Azure": (), "Alibaba Cloud": ("阿里云",), "Docker": (),
    "Kubernetes": ("K8s",), "Helm": (), "Terraform": (), "Ansible": (), "Jenkins": (), "GitLab CI": (),
    "GitHub Actions": (), "CI/CD": (), "ArgoCD": (), "Linux": (), "Nginx": (), "Istio": (),
    "Prometheus": (), "Grafana": (), "OpenTelemetry": (), "Jaeger": (), "SkyWalking": (), "ELK": (),
    "Serverless": (), "Lambda": (),
    # AI / data science
    "Machine Learning": ("ML", "机器学习"), "Deep Learning": ("深度学习",), "PyTorch": (), "TensorFlow": (),
    "LLM": ("大模型", "大语言模型"), "RAG": (), "NLP": ("自然语言处理",), "Computer Vision": ("计算机视觉",),
    "Pandas": (), "NumPy": (), "Data Analysis": ("数据分析",),
    # practices / architecture (Chinese posts name these a lot)
    "Microservices": ("微服务", "micro-services"), "Distributed Systems": ("分布式", "distributed"),
    "High Concurrency": ("高并发",), "Low Latency": ("低延迟",), "High Availability": ("高可用",),
    "Performance Tuning": ("性能调优", "性能优化"), "System Design": ("系统设计", "架构设计"),
    "Blockchain": ("区块链",), "Smart Contracts": ("智能合约",), "Web3": (),
    "Matching Engine": ("撮合", "撮合引擎"), "Risk Control": ("风控",), "Payments": ("支付",),
    "Security": ("安全",), "Unit Testing": ("单元测试",), "TDD": (), "Agile": ("Scrum", "敏捷"),
    "Git": (), "Figma": (), "Excel": (), "Tableau": (), "Power BI": (),
}
_CASE_SENSITIVE = {"Go", "Gin", "Echo", "Express", "Spring", "Swift", "Rust", "Dart", "Lambda", "Security",
                   "Agile", "Oracle", "Hive", "Spark", "Flink", "Helm", "Node", "Shell", "ES", "ML", "TS", "JS",
                   "Excel", "REST", "Mongo", "distributed"}


def _pattern(term: str) -> str:
    if re.search(r"[一-鿿]", term):
        return re.escape(term)
    return r"(?<![A-Za-z0-9+#.])" + re.escape(term) + r"(?![A-Za-z0-9+#])"


_MATCHERS: list[tuple[str, re.Pattern]] = []
for _canon, _alts in VOCAB.items():
    for _t in (_canon, *_alts):
        flags = 0 if _t in _CASE_SENSITIVE else re.I
        _MATCHERS.append((_canon, re.compile(_pattern(_t), flags)))


def find_skills(text: str) -> list[str]:
    """Skills named in ``text``, canonical names in order of first mention."""
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for canon, rx in _MATCHERS:
        m = rx.search(text or "")
        if m and canon not in seen:
            seen.add(canon)
            hits.append((m.start(), canon))
    return [c for _, c in sorted(hits)]


@dataclass
class SkillReport:
    skills: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    supported: list[str] = field(default_factory=list)
    gap: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return len(self.existing) + len(self.supported)

    @property
    def percent(self) -> int:
        return round(100 * self.matched / len(self.skills)) if self.skills else 0

    def to_dict(self) -> dict[str, Any]:
        return {"skills": self.skills, "existing": self.existing, "supported": self.supported, "gap": self.gap,
                "matched": self.matched, "percent": self.percent}


def check(posting_text: str, profile: dict[str, Any], *, limit: int = 25) -> SkillReport:
    """Compare the skills a post names with your profile."""
    p = prof.normalize(profile)
    declared = "\n".join([p["skills"], *(x["stack"] for x in p["projects"])])
    declared_skills = set(find_skills(declared))
    everything = set(find_skills(prof.facts_text(p) + "\n" + declared))
    r = SkillReport(skills=find_skills(posting_text)[:limit])
    for s in r.skills:
        (r.existing if s in declared_skills else r.supported if s in everything else r.gap).append(s)
    return r


def ai_hint(r: SkillReport) -> str:
    """A note for the tailoring prompt: what to bring forward, what never to claim."""
    if not r.skills:
        return ""
    lines = []
    if r.existing or r.supported:
        lines.append("Skills this posting asks for that the candidate's facts show (bring these forward): "
                     + ", ".join(r.existing + r.supported) + ".")
    if r.gap:
        lines.append("Skills this posting asks for that the candidate's facts do NOT show (never claim, mention "
                     "or imply these): " + ", ".join(r.gap) + ".")
    return "\n".join(lines)
