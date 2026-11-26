"""Lay a :class:`ResumeContent` out on A4 pages and return the PDF bytes."""

from __future__ import annotations

from pathlib import Path

from ..apply import language
from .content import Entry, ResumeContent
from .fonts import TrueTypeFont, find_font
from .pdf import PdfDoc

ACCENT = (0.11, 0.30, 0.55)
GREY = (0.38, 0.40, 0.44)
INK = (0.10, 0.10, 0.12)
MARGIN_X, TOP, BOTTOM = 52.0, 56.0, 50.0


class ResumeError(RuntimeError):
    pass


def pick_font(text: str, *, font_path: str = "", font_dirs: list[Path] | None = None) -> TrueTypeFont | None:
    font = find_font(text, preferred=font_path or None, extra_dirs=font_dirs)
    if font is None and any(ord(c) > 0xFF and c not in "•–—‘’“”…·" for c in text):
        raise ResumeError("No font with Chinese characters was found on this computer. Put a .ttf/.ttc font "
                          "(e.g. SimHei, Microsoft YaHei, WenQuanYi) in the jobradar-data/fonts folder, or set "
                          "resume_font_path.")
    return font   # None → built-in Helvetica for plain Latin text


class _Layout:
    def __init__(self, doc: PdfDoc):
        self.doc = doc
        self.left, self.right = MARGIN_X, doc.width - MARGIN_X
        self.y = doc.height - TOP

    def need(self, h: float) -> None:
        if self.y - h < BOTTOM:
            self.doc.new_page()
            self.y = self.doc.height - TOP

    def para(self, text: str, size: float = 10, *, color=INK, indent: float = 0, lead: float = 1.5,
             bold: bool = False) -> None:
        for ln in self.doc.wrap(text, size, self.right - self.left - indent):
            self.need(size * lead)
            self.y -= size * lead
            self.doc.text(self.left + indent, self.y + size * 0.3, ln, size, color=color, bold=bold)

    def heading(self, text: str) -> None:
        self.need(40)
        self.y -= 20
        self.doc.text(self.left, self.y, text, 12, color=ACCENT, bold=True)
        self.y -= 6
        self.doc.line(self.left, self.y, self.right, self.y, color=(0.75, 0.80, 0.88), width=0.8)
        self.y -= 2

    def entry(self, e: Entry) -> None:
        self.need(34)
        self.y -= 16
        date_w = self.doc.text_width(e.period, 9.5)
        title_lines = self.doc.wrap(e.title, 10.5, self.right - self.left - date_w - 12)
        self.doc.text(self.left, self.y, title_lines[0], 10.5, color=INK, bold=True)
        if e.period:
            self.doc.text(self.right - date_w, self.y, e.period, 9.5, color=GREY)
        for ln in title_lines[1:]:
            self.y -= 14
            self.doc.text(self.left, self.y, ln, 10.5, color=INK, bold=True)
        if e.subtitle:
            self.para(e.subtitle, 9.5, color=GREY, lead=1.75)
        for b in e.bullets:
            self.bullet(b)

    def bullet(self, text: str, size: float = 10) -> None:
        lines = self.doc.wrap(text, size, self.right - self.left - 14)
        for i, ln in enumerate(lines):
            self.need(size * 1.5)
            self.y -= size * 1.5
            if i == 0:
                self.doc.dot(self.left + 5, self.y + size * 0.3 + size * 0.33, size * 0.16, color=ACCENT)
            self.doc.text(self.left + 14, self.y + size * 0.3, ln, size, color=INK)


def render(c: ResumeContent, *, font: TrueTypeFont | None = None, font_path: str = "",
           font_dirs: list[Path] | None = None) -> bytes:
    if font is None:
        font = pick_font(c.plain_text(), font_path=font_path, font_dirs=font_dirs)
    doc = PdfDoc(font, title=f"{c.name} — {c.job_title}", author=c.name)
    L = _Layout(doc)
    lang = c.lang

    # name, job title, contact details
    L.y -= 6
    doc.text(L.left, L.y, c.name, 22, color=INK, bold=True)
    L.y -= 22
    doc.text(L.left, L.y, c.job_title, 12.5, color=ACCENT)
    if c.contact:
        L.y -= 4
        L.para("  ·  ".join(c.contact), 9.5, color=GREY, lead=1.5)
    L.y -= 6
    doc.line(L.left, L.y, L.right, L.y, color=ACCENT, width=1.2)

    if c.intro:
        L.heading(language.t("cv_intro", lang))
        for part in [x for x in c.intro.split("\n") if x.strip()]:
            L.para(part.strip(), 10)
    if c.skills:
        L.heading(language.t("cv_skills", lang))
        sep = "、" if lang == "zh" else ", "
        for label, items in c.skills:
            if label:
                L.para(f"{label}{'：' if lang == 'zh' else ': '}{sep.join(items)}", 10)
            else:
                L.para(sep.join(items), 10)
    if c.experience:
        L.heading(language.t("cv_experience", lang))
        for e in c.experience:
            L.entry(e)
    if c.projects:
        L.heading(language.t("cv_projects", lang))
        for e in c.projects:
            L.entry(e)
    if c.education:
        L.heading(language.t("cv_education", lang))
        for e in c.education:
            L.entry(e)
    if c.languages:
        L.heading(language.t("cv_languages", lang))
        for ln in c.languages:
            L.bullet(ln)
    return doc.to_bytes()
