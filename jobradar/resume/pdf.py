"""A small PDF writer (standard library only) for one-column documents.

Text uses an embedded, subset TrueType font (see :mod:`.fonts`) with
Identity-H encoding and a ToUnicode map, so Chinese renders everywhere and the
text can be selected, searched and read by applicant-tracking systems. When no
TrueType font is available and the text is plain Latin, it falls back to the
built-in Helvetica (nothing embedded).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .fonts import TrueTypeFont

A4 = (595.28, 841.89)
LETTER = (612.0, 792.0)

# Glyphs some fonts lack, and what to draw instead.
_SUBSTITUTE = {"•": "·", "·": "-", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
               "…": "...", "｜": "|", " ": " ", "　": "  "}

# Helvetica advance widths for ASCII 32..126 (Adobe AFM), 1/1000 em.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_EXTRA = {"•": 350, "–": 556, "—": 1000, "‘": 222, "’": 222, "“": 333, "”": 333, "…": 1000, "·": 278}

_CLOSE = set("，。、；：！？）」』》】〉,.;:!?)]}%”’")
_OPEN = set("（「『《【〈“‘")
_ITALIC_SKEW = 0.2


def is_wide(ch: str) -> bool:
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFFEF or 0x20000 <= o <= 0x2FA1F)


def _utf16_hex(s: str) -> str:
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


@dataclass(frozen=True)
class Style:
    size: float = 10.0
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bold: bool = False
    italic: bool = False


Run = tuple[str, Style]


class PdfDoc:
    """Pages of positioned text and simple shapes. ``font`` is the regular
    TrueType face (None = built-in Helvetica); ``bold_font`` a real bold face,
    else bold is drawn by stroking the regular one."""

    def __init__(self, font: TrueTypeFont | None, *, bold_font: TrueTypeFont | None = None,
                 title: str = "", author: str = "", size: tuple[float, float] = A4):
        self.font, self.title, self.author = font, title, author
        self.bold_font = bold_font if font is not None else None
        self.width, self.height = size
        self.pages: list[list[str]] = []
        self.used_by: dict[str, dict[int, str]] = {"F1": {}, "F2": {}}   # font -> gid -> text
        self.new_page()

    @property
    def used(self) -> dict[int, str]:
        return self.used_by["F1"]

    # --------------------------------------------------------------- text
    def _face(self, bold: bool) -> tuple[str, TrueTypeFont]:
        if bold and self.bold_font is not None:
            return "F2", self.bold_font
        return "F1", self.font

    def _glyphs(self, s: str, font: TrueTypeFont | None = None) -> list[tuple[int, str]]:
        font = font or self.font
        out = []
        for ch in s:
            if font.has(ch):
                out.append((font.gid(ch), ch))
                continue
            alt = _SUBSTITUTE.get(ch, ch)
            while alt != ch and len(alt) == 1 and not font.has(alt) and alt in _SUBSTITUTE:
                alt = _SUBSTITUTE[alt]
            for a in alt:
                out.append((font.gid(a), a))
        return out

    def _latin1(self, s: str) -> bytes:
        return s.encode("cp1252", "replace")

    def text_width(self, s: str, size: float, bold: bool = False) -> float:
        if self.font is None:
            w = 0
            for ch in s:
                o = ord(ch)
                w += _HELV[o - 32] if 32 <= o <= 126 else _HELV_EXTRA.get(ch, 556)
            return w * size / 1000 * (1.04 if bold else 1.0)
        _, face = self._face(bold)
        return sum(face.width(g) for g, _ in self._glyphs(s, face)) * size / 1000

    def wrap(self, s: str, size: float, max_width: float, bold: bool = False) -> list[str]:
        """Break ``s`` into lines no wider than ``max_width``: between words for
        Latin text, between characters for Chinese, never before 。，) etc."""
        st = Style(size=size, bold=bold)
        out = []
        for para in (s or "").split("\n"):
            out += ["".join(t for t, _ in line) for line in self.wrap_runs([(para, st)], max_width)]
        return out

    def wrap_runs(self, runs: list[Run], max_width: float, first_indent: float = 0.0) -> list[list[Run]]:
        """Wrap text made of differently styled pieces (e.g. a bold label then
        normal text). Returns lines of (text, style) runs."""
        tokens: list[Run] = []
        for text, st in runs:
            tokens += [(t, st) for t in _tokens(text)]
        lines: list[list[Run]] = []
        line: list[Run] = []
        width, limit = 0.0, max_width - first_indent

        def flush() -> None:
            nonlocal line, width, limit
            if line:   # trailing spaces don't count
                t, st = line[-1]
                line[-1] = (t.rstrip(), st)
            lines.append(_merge(line))
            line, width, limit = [], 0.0, max_width

        for tok, st in tokens:
            w = self.text_width(tok.rstrip(), st.size, st.bold)
            if line and width + w > limit:
                flush()
                tok = tok.lstrip()
                w = self.text_width(tok, st.size, st.bold)
            while w > limit and len(tok) > 1:   # a very long word / URL
                cut = len(tok)
                while cut > 1 and self.text_width(tok[:cut], st.size, st.bold) > limit - width:
                    cut -= 1
                line.append((tok[:cut], st))
                flush()
                tok = tok[cut:]
                w = self.text_width(tok, st.size, st.bold)
            line.append((tok, st))
            width += self.text_width(tok, st.size, st.bold)
        flush()
        return lines

    def new_page(self) -> None:
        self.pages.append([])

    def text(self, x: float, y: float, s: str, size: float, *, color=(0, 0, 0), bold: bool = False,
             italic: bool = False) -> float:
        """Draw one line of text; returns its width."""
        if not s:
            return 0.0
        ops = self.pages[-1]
        r, g, b = color
        place = (f"1 0 {_ITALIC_SKEW} 1 {x:.2f} {y:.2f} Tm" if italic else f"{x:.2f} {y:.2f} Td")
        if self.font is None:
            font = "/F2" if bold else "/F1"
            ops.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg {font} {size:.2f} Tf {place} <{self._latin1(s).hex()}> Tj ET")
            return self.text_width(s, size, bold)
        key, face = self._face(bold)
        glyphs = self._glyphs(s, face)
        for gid, ch in glyphs:
            self.used_by[key].setdefault(gid, ch)
        hexs = "".join(f"{gid:04x}" for gid, _ in glyphs)
        fake_bold = bold and key == "F1"
        mode = f"2 Tr {size * 0.03:.3f} w {r:.3f} {g:.3f} {b:.3f} RG " if fake_bold else "0 Tr "
        ops.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg {mode}/{key} {size:.2f} Tf {place} <{hexs}> Tj ET")
        return self.text_width(s, size, bold)

    def runs(self, x: float, y: float, line: list[Run]) -> float:
        """Draw a line of styled runs left to right; returns the total width."""
        start = x
        for text, st in line:
            x += self.text(x, y, text, st.size, color=st.color, bold=st.bold, italic=st.italic)
        return x - start

    def line(self, x1: float, y1: float, x2: float, y2: float, *, color=(0.7, 0.7, 0.7), width: float = 0.6) -> None:
        r, g, b = color
        self.pages[-1].append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def dot(self, x: float, y: float, r: float, *, color=(0, 0, 0)) -> None:
        """A filled circle (bullets don't depend on the font having "•")."""
        k = 0.5523 * r
        cr, cg, cb = color
        self.pages[-1].append(
            f"{cr:.3f} {cg:.3f} {cb:.3f} rg {x + r:.2f} {y:.2f} m "
            f"{x + r:.2f} {y + k:.2f} {x + k:.2f} {y + r:.2f} {x:.2f} {y + r:.2f} c "
            f"{x - k:.2f} {y + r:.2f} {x - r:.2f} {y + k:.2f} {x - r:.2f} {y:.2f} c "
            f"{x - r:.2f} {y - k:.2f} {x - k:.2f} {y - r:.2f} {x:.2f} {y - r:.2f} c "
            f"{x + k:.2f} {y - r:.2f} {x + r:.2f} {y - k:.2f} {x + r:.2f} {y:.2f} c f")

    def diamond(self, x: float, y: float, r: float, *, color=(0, 0, 0)) -> None:
        """A filled diamond (◆) centred on (x, y)."""
        cr, cg, cb = color
        self.pages[-1].append(f"{cr:.3f} {cg:.3f} {cb:.3f} rg {x:.2f} {y + r:.2f} m {x + r:.2f} {y:.2f} l "
                              f"{x:.2f} {y - r:.2f} l {x - r:.2f} {y:.2f} l h f")

    # ------------------------------------------------------------- output
    def to_bytes(self) -> bytes:
        objs: list[bytes] = []

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        def stream(data: bytes, extra: str = "") -> int:
            z = zlib.compress(data, 9)
            return add(f"<< /Length {len(z)} /Filter /FlateDecode{extra} >>\nstream\n".encode() + z
                       + b"\nendstream")

        fonts = self._font_objects(add, stream)
        catalog = add(b"")          # filled in below
        pages_id = add(b"")
        page_ids = []
        for ops in self.pages:
            content = stream("\n".join(ops).encode("latin-1"))
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {self.width:.2f} {self.height:.2f}] "
                f"/Resources << /Font << {fonts} >> >> /Contents {content} 0 R >>".encode()))
        objs[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] "
                              f"/Count {len(page_ids)} >>").encode()
        objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        now = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info = add(f"<< /Title {_utf16_hex(self.title)} /Author {_utf16_hex(self.author)} "
                   f"/Producer (Job Radar) /CreationDate ({now}) >>".encode())

        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)

    def _font_objects(self, add, stream) -> str:
        if self.font is None:
            f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
            f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
            return f"/F1 {f1} 0 R /F2 {f2} 0 R"
        out = [f"/F1 {self._embed(self.font, self.used_by['F1'], add, stream)} 0 R"]
        if self.bold_font is not None and self.used_by["F2"]:
            out.append(f"/F2 {self._embed(self.bold_font, self.used_by['F2'], add, stream, bold=True)} 0 R")
        return " ".join(out)

    @staticmethod
    def _embed(f: TrueTypeFont, used: dict[int, str], add, stream, bold: bool = False) -> int:
        gids = sorted(used) or [0]
        seed = sum(gids) + (7919 if bold else 0)
        tag = "".join(chr(65 + (seed // 26 ** i) % 26) for i in range(6))
        base = f"{tag}+{f.name}"
        data = f.subset(set(gids), used)
        file_id = stream(data, f" /Length1 {len(data)}")
        x0, y0, x1, y1 = f.bbox
        flags = 4 | (1 << 18 if bold else 0)    # symbolic (+ force bold)
        desc = add((f"<< /Type /FontDescriptor /FontName /{base} /Flags {flags} /FontBBox [{x0} {y0} {x1} {y1}] "
                    f"/ItalicAngle 0 /Ascent {f.ascent} /Descent {f.descent} /CapHeight {round(f.cap_height)} "
                    f"/StemV {120 if bold else 80} /FontFile2 {file_id} 0 R >>").encode())
        widths = " ".join(f"{g} [{round(f.width(g))}]" for g in gids)
        cid = add((f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} "
                   f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                   f"/FontDescriptor {desc} 0 R /DW 1000 /W [{widths}] /CIDToGIDMap /Identity >>").encode())
        tounicode = stream(_tounicode(used).encode("ascii"))
        return add((f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
                    f"/DescendantFonts [{cid} 0 R] /ToUnicode {tounicode} 0 R >>").encode())


def _merge(line: list[Run]) -> list[Run]:
    out: list[Run] = []
    for t, st in line:
        if out and out[-1][1] == st:
            out[-1] = (out[-1][0] + t, st)
        elif t:
            out.append((t, st))
    return out


def _tokens(s: str) -> list[str]:
    raw: list[str] = []
    buf = ""
    for ch in s:
        if is_wide(ch):
            if buf:
                raw.append(buf)
                buf = ""
            raw.append(ch)
        elif ch == " ":
            raw.append(buf + " ")
            buf = ""
        else:
            buf += ch
    if buf:
        raw.append(buf)
    out: list[str] = []
    for tok in raw:   # keep closing punctuation with what precedes it, opening with what follows
        if out and (tok[0] in _CLOSE) and not out[-1].endswith(" "):
            out[-1] += tok
        elif out and out[-1] and out[-1][-1] in _OPEN:
            out[-1] += tok
        else:
            out.append(tok)
    return out


def _tounicode(used: dict[int, str]) -> str:
    items = sorted(used.items())
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines += [f"<{g:04X}> <{ch.encode('utf-16-be').hex().upper()}>" for g, ch in chunk]
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines)
