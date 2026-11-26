"""Make the résumé PDF for one application (or a preview).

Files go in ``jobradar-data/resumes/<posting>/<Name>_简历.pdf``, so the exact
résumé sent with each application is kept, and the recruiter sees a sensible
file name.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..apply import language
from ..config import Config
from . import content as cv
from . import profile as prof
from . import skillgap
from .render import ResumeError, render_doc

log = logging.getLogger("jobradar.resume")

# (profile, posting text, language, skill hint) -> the AI's unchecked JSON, or None
Tailor = Callable[[dict[str, Any], str, str, str], "dict[str, Any] | None"]


@dataclass
class BuildInfo:
    """What the last build made, for notices and the dashboard."""
    path: Path
    pages: int = 1
    paper: str = "letter"
    tailored: bool = False
    skills: skillgap.SkillReport = field(default_factory=skillgap.SkillReport)
    max_pages: int = 0

    @property
    def too_long(self) -> bool:
        return bool(self.max_pages) and self.pages > self.max_pages


def paper_for(config: Config, posting_text: str) -> str:
    """"letter" / "a4": as set, or with "auto" by the job's country
    (Letter for the US and Canada, A4 elsewhere, Letter when unknown)."""
    setting = (config.resume_page_size or "letter").strip().lower()
    if setting in ("letter", "a4"):
        return setting
    from .. import places

    countries = places.find(posting_text or "")
    if not countries:
        return "letter"
    return "letter" if countries <= {"US", "CA"} else "a4"


class ResumeBuilder:
    def __init__(self, config: Config, base_dir: Path, profile: dict[str, Any], *,
                 tailor: Tailor | None = None):
        self.config = config
        self.base_dir = Path(base_dir)
        self.profile = prof.newest_first(prof.validate(profile))   # raises ProfileError if incomplete
        self.tailor = tailor
        self.last: BuildInfo | None = None

    def language_for(self, posting_text: str) -> str:
        return language.resolve(self.config.message_language, posting_text or prof.facts_text(self.profile))

    def skills(self, posting_text: str) -> skillgap.SkillReport:
        return skillgap.check(posting_text, self.profile) if posting_text.strip() else skillgap.SkillReport()

    def content(self, posting_text: str = "", report: skillgap.SkillReport | None = None) -> cv.ResumeContent:
        lang = self.language_for(posting_text)
        obj = None
        if self.tailor is not None:
            hint = skillgap.ai_hint(report or self.skills(posting_text))
            try:
                obj = self.tailor(self.profile, posting_text, lang, hint)
            except Exception as e:  # noqa: BLE001 - fall back to your own wording
                log.warning("résumé tailoring failed: %s", e)
        return cv.from_ai(obj, self.profile, lang) if obj else cv.from_profile(self.profile, lang)

    def build(self, posting_text: str = "", *, key: str | None = None) -> Path:
        """Write the PDF and return its path (details in ``self.last``). Raises ResumeError."""
        report = self.skills(posting_text)
        c = self.content(posting_text, report)
        paper = paper_for(self.config, posting_text)
        try:
            doc = render_doc(c, font_path=self.config.resume_font_path or "",
                             font_dirs=[self.base_dir / "fonts"], paper=paper)
            data = doc.to_bytes()
        except ResumeError:
            raise
        except Exception as e:  # noqa: BLE001 - a font or layout bug
            raise ResumeError(f"{type(e).__name__}: {e}") from e
        folder = self.base_dir / "resumes" / (_safe(key)[:24] if key else "preview")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{cv.file_stem(self.profile, c.lang)}.pdf"
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self.last = BuildInfo(path, pages=len(doc.pages), paper=paper, tailored=c.tailored, skills=report,
                              max_pages=max(0, int(self.config.resume_max_pages or 0)))
        if self.last.too_long:
            log.warning("the résumé is %d pages (limit %d)", self.last.pages, self.last.max_pages)
        return path

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.base_dir).as_posix()
        except ValueError:
            return str(path)


def _safe(key: str) -> str:
    return "".join(c for c in key if c.isalnum() or c in "-_") or "job"


def make_builder(config: Config, base_dir: Path, profile: dict[str, Any]) -> ResumeBuilder | None:
    """A builder when your profile is complete (with AI tailoring when AI is
    set up), else None."""
    if not prof.is_complete(profile):
        return None
    from .. import ai
    from ..apply.engine import ai_key

    key = ai_key(config)
    tailor: Tailor | None = None
    if config.use_ai and key:
        def ai_tailor(p: dict[str, Any], posting: str, lang: str, hint: str = "") -> dict[str, Any] | None:
            return ai.tailor_resume(p, posting, language=lang, provider=config.ai_provider, api_key=key or "",
                                    model=config.ai_model, max_chars=config.ai_max_chars, hint=hint)
        tailor = ai_tailor
    return ResumeBuilder(config, base_dir, profile, tailor=tailor)
