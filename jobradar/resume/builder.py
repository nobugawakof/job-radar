"""Make the résumé PDF for one application (or a preview).

Files go in ``jobradar-data/resumes/<posting>/<Name>_简历.pdf``, so the exact
résumé sent with each application is kept, and the recruiter sees a sensible
file name.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..apply import language
from ..config import Config
from . import content as cv
from . import profile as prof
from .render import ResumeError, render

log = logging.getLogger("jobradar.resume")

# (profile, posting text, language) -> the AI's unchecked JSON, or None
Tailor = Callable[[dict[str, Any], str, str], "dict[str, Any] | None"]


class ResumeBuilder:
    def __init__(self, config: Config, base_dir: Path, profile: dict[str, Any], *,
                 tailor: Tailor | None = None):
        self.config = config
        self.base_dir = Path(base_dir)
        self.profile = prof.validate(profile)   # raises ProfileError if incomplete
        self.tailor = tailor

    def language_for(self, posting_text: str) -> str:
        return language.resolve(self.config.message_language, posting_text or prof.facts_text(self.profile))

    def content(self, posting_text: str = "") -> cv.ResumeContent:
        lang = self.language_for(posting_text)
        obj = None
        if self.tailor is not None:
            try:
                obj = self.tailor(self.profile, posting_text, lang)
            except Exception as e:  # noqa: BLE001 - fall back to your own wording
                log.warning("résumé tailoring failed: %s", e)
        return cv.from_ai(obj, self.profile, lang) if obj else cv.from_profile(self.profile, lang)

    def build(self, posting_text: str = "", *, key: str | None = None) -> Path:
        """Write the PDF and return its path. Raises ResumeError."""
        c = self.content(posting_text)
        try:
            data = render(c, font_path=self.config.resume_font_path or "",
                          font_dirs=[self.base_dir / "fonts"])
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
        def ai_tailor(p: dict[str, Any], posting: str, lang: str) -> dict[str, Any] | None:
            return ai.tailor_resume(p, posting, language=lang, provider=config.ai_provider, api_key=key or "",
                                    model=config.ai_model, max_chars=config.ai_max_chars)
        tailor = ai_tailor
    return ResumeBuilder(config, base_dir, profile, tailor=tailor)
