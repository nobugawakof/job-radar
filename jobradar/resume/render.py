"""Lay a :class:`ResumeContent` out and return the PDF bytes.

The look follows the owner's sample résumé: US Letter, a navy accent
(#1F3A5F) for headings and rules, near-black body text (#1A1A1A), grey
(#555555) for dates and secondary lines, "Role — Company" with the dates on the
right, an italic line for the team or area, ◆ before each project, and one-line
education and languages. Sizes and spacing are the sample's (in points).
"""

from __future__ import annotations

from pathlib import Path

from ..apply import language
from .content import Entry, ResumeContent
from .fonts import TrueTypeFont, find_bold, find_font
from .pdf import LETTER, PdfDoc, Run, Style

INK = (0x1A / 255, 0x1A / 255, 0x1A / 255)
ACCENT = (0x1F / 255, 0x3A / 255, 0x5F / 255)
GREY = (0x55 / 255, 0x55 / 255, 0x55 / 255)
TOP, BOTTOM, SIDE = 36.0, 36.0, 45.0          # page margins (the sample's 0.5" / 0.625")


class ResumeError(RuntimeError):
    pass


def pick_font(text: str, *, font_path: str = "", font_dirs: list[Path] | None = None) -> TrueTypeFont | None:
    font = find_font(text, preferred=font_path or None, extra_dirs=font_dirs)
    if font is None and any(ord(c) > 0xFF and c not in "•–—‘’“”…·｜" for c in text):
        raise ResumeError("No font with Chinese characters was found on this computer. Put a .ttf/.ttc font "
                          "(e.g. SimHei, Microsoft YaHei, WenQuanYi) in the jobradar-data/fonts folder, or set "
                          "resume_font_path.")
    return font   # None → built-in Helvetica for plain Latin text


class _Layout:
    def __init__(self, doc: PdfDoc):
        self.doc = doc
        self.left, self.right = SIDE, doc.width - SIDE
        self.y = doc.height - TOP
        f = doc.font
        # Line height from the font's own ascent/descent (CJK fonts are tall).
        self.lead = min(1.5, max(1.2, (f.ascent - f.descent) / 1000)) if f is not None else 1.25
        self.asc = 0.8

    @property
    def width(self) -> float:
        return self.right - self.left

    def need(self, h: float) -> None:
        if self.y - h < BOTTOM:
            self.doc.new_page()
            self.y = self.doc.height - TOP

    def space(self, pt: float) -> None:
        if self.y < self.doc.height - TOP - 0.1:   # no gap at the top of a page
            self.y -= pt

    def lines(self, runs: list[Run], *, indent: float = 0.0, after: float = 0.0,
              right: tuple[str, Style] | None = None) -> None:
        """Wrapped styled text; ``right`` is drawn right-aligned on the first line."""
        right_w = self.doc.text_width(right[0], right[1].size, right[1].bold) + 12 if right and right[0] else 0
        size = max(st.size for _, st in runs) if runs else 10
        wrapped = self.doc.wrap_runs(runs, self.width - indent, first_indent=right_w) if runs else [[]]
        for i, line in enumerate(wrapped):
            h = size * self.lead
            self.need(h)
            base = self.y - h * self.asc
            self.doc.runs(self.left + indent, base, line)
            if i == 0 and right_w:
                t, st = right
                self.doc.text(self.right - self.doc.text_width(t, st.size, st.bold), base, t, st.size,
                              color=st.color, bold=st.bold, italic=st.italic)
            self.y -= h
        self.y -= after

    def heading(self, text: str) -> None:
        self.need(12 + 12 * self.lead + 18 * self.lead)   # keep a heading with its first line
        self.space(12)
        self.lines([(text, Style(12, ACCENT, bold=True))])
        self.y -= 3
        self.doc.line(self.left, self.y, self.right, self.y, color=ACCENT, width=1.0)
        self.y -= 6

    def bullet(self, text: str, size: float = 10) -> None:
        h = size * self.lead
        self.need(h)
        top = self.y
        self.lines([(text, Style(size, INK))], indent=15, after=2)
        base = top - h * self.asc
        self.doc.dot(self.left + 6, base + size * 0.32, size * 0.18, color=ACCENT)


def _entry_head(L: _Layout, e: Entry, size: float, *, marker: bool = False) -> None:
    runs: list[Run] = [(e.title, Style(size, INK, bold=True))]
    if e.org:
        runs.append((f" — {e.org}", Style(size, ACCENT, bold=True)))
    indent = 0.0
    if marker:   # ◆ drawn as a shape, so it never depends on the font
        indent = size * 1.1
    h = size * L.lead
    L.need(h + 10 * L.lead)
    top = L.y
    L.lines(runs, indent=indent, after=1, right=(e.period, Style(10, GREY)) if e.period else None)
    if marker:
        L.doc.diamond(L.left + size * 0.4, top - h * L.asc + size * 0.35, size * 0.33, color=INK)


def render(c: ResumeContent, *, font: TrueTypeFont | None = None, bold_font: TrueTypeFont | None = None,
           font_path: str = "", font_dirs: list[Path] | None = None) -> bytes:
    text = c.plain_text()
    if font is None:
        font = pick_font(text, font_path=font_path, font_dirs=font_dirs)
    if font is not None and bold_font is None:
        bold_font = find_bold(font, text, extra_dirs=font_dirs)
    doc = PdfDoc(font, bold_font=bold_font, title=f"{c.name} — {c.job_title}", author=c.name, size=LETTER)
    L = _Layout(doc)
    lang = c.lang
    colon = "：" if lang == "zh" else ": "

    # name, job title · focus, contact details
    L.lines([(c.name, Style(20, INK, bold=True))], after=1)
    L.lines([(c.headline, Style(11, ACCENT, bold=True))], after=6 if not c.contact else 2)
    if c.contact:
        L.lines([("  ·  ".join(c.contact), Style(9.5, GREY))], after=4)

    if c.intro:
        L.heading(language.t("cv_intro", lang))
        for part in [x.strip() for x in c.intro.split("\n") if x.strip()]:
            L.lines([(part, Style(10, INK))], after=4)
    if c.skills:
        L.heading(language.t("cv_skills", lang))
        for label, items in c.skills:
            runs: list[Run] = [(label + colon, Style(10, ACCENT, bold=True))] if label else []
            L.lines(runs + [(items, Style(10, INK))], after=3)
    if c.experience:
        L.heading(language.t("cv_experience", lang))
        for i, e in enumerate(c.experience):
            L.space(7 if i else 2)
            _entry_head(L, e, 11)
            if e.sub:
                L.lines([(e.sub, Style(10, GREY, italic=True))], after=3)
            for b in e.bullets:
                L.bullet(b)
    if c.projects:
        L.heading(language.t("cv_projects", lang))
        for i, e in enumerate(c.projects):
            L.space(7 if i else 2)
            _entry_head(L, e, 10.5, marker=True)
            for label, value in e.meta:
                L.lines(([(label, Style(9.5, GREY, bold=True))] if label else []) + [(value, Style(9.5, GREY))],
                        after=2)
            for b in e.bullets:
                L.bullet(b)
    if c.education:
        L.heading(language.t("cv_education", lang))
        for i, e in enumerate(c.education):
            L.space(5 if i else 0)
            L.lines([(e.title, Style(10, INK, bold=True))], after=2, right=(e.period, Style(10, GREY)) if e.period else None)
            for n in e.notes:
                L.lines([(n, Style(9.5, GREY))], after=2)
    if c.languages:
        L.heading(language.t("cv_languages", lang))
        L.lines([((" ｜ " if lang == "zh" else "  |  ").join(c.languages), Style(10, INK))])
    return doc.to_bytes()
