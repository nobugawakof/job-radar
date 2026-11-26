"""A small PDF writer (standard library only) for one-column documents.

Text uses an embedded, subset TrueType font (see :mod:`.fonts`) with
Identity-H encoding and a ToUnicode map, so Chinese renders everywhere and the
text can be selected, searched and read by applicant-tracking systems. When no
TrueType font is available and the text is plain Latin, it falls back to the
built-in Helvetica (nothing embedded).
"""

from __future__ import annotations

import zlib
from datetime import datetime, timezone

from .fonts import TrueTypeFont

A4 = (595.28, 841.89)

# Glyphs some fonts lack, and what to draw instead.
_SUBSTITUTE = {"•": "·", "·": "-", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
               "…": "...", " ": " ", "　": "  "}

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


def is_wide(ch: str) -> bool:
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFFEF or 0x20000 <= o <= 0x2FA1F)


def _utf16_hex(s: str) -> str:
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


class PdfDoc:
    def __init__(self, font: TrueTypeFont | None, *, title: str = "", author: str = "",
                 size: tuple[float, float] = A4):
        self.font, self.title, self.author = font, title, author
        self.width, self.height = size
        self.pages: list[list[str]] = []
        self.used: dict[int, str] = {}        # gid -> the text it stands for
        self.new_page()

    # --------------------------------------------------------------- text
    def _glyphs(self, s: str) -> list[tuple[int, str]]:
        out = []
        for ch in s:
            if self.font.has(ch):
                out.append((self.font.gid(ch), ch))
                continue
            alt = _SUBSTITUTE.get(ch, ch)
            while alt != ch and len(alt) == 1 and not self.font.has(alt) and alt in _SUBSTITUTE:
                alt = _SUBSTITUTE[alt]
            for a in alt:
                out.append((self.font.gid(a), a))
        return out

    def _latin1(self, s: str) -> bytes:
        return s.encode("cp1252", "replace")

    def text_width(self, s: str, size: float) -> float:
        if self.font is None:
            w = 0
            for ch in s:
                o = ord(ch)
                w += _HELV[o - 32] if 32 <= o <= 126 else _HELV_EXTRA.get(ch, 556)
            return w * size / 1000
        return sum(self.font.width(g) for g, _ in self._glyphs(s)) * size / 1000

    def wrap(self, s: str, size: float, max_width: float) -> list[str]:
        """Break ``s`` into lines no wider than ``max_width``: between words for
        Latin text, between characters for Chinese, never before 。，) etc."""
        lines: list[str] = []
        for para in (s or "").split("\n"):
            tokens = _tokens(para)
            line = ""
            for tok in tokens:
                cand = line + tok
                if not line or self.text_width(cand.rstrip(), size) <= max_width:
                    line = cand
                    continue
                lines.append(line.rstrip())
                line = tok.lstrip()
                while self.text_width(line, size) > max_width and len(line) > 1:  # a very long word / URL
                    cut = len(line)
                    while cut > 1 and self.text_width(line[:cut], size) > max_width:
                        cut -= 1
                    lines.append(line[:cut])
                    line = line[cut:]
            lines.append(line.rstrip())
        return lines

    def new_page(self) -> None:
        self.pages.append([])

    def text(self, x: float, y: float, s: str, size: float, *, color=(0, 0, 0), bold: bool = False) -> None:
        if not s:
            return
        ops = self.pages[-1]
        r, g, b = color
        if self.font is None:
            font = "/F2" if bold else "/F1"
            ops.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg {font} {size:.2f} Tf {x:.2f} {y:.2f} Td "
                       f"<{self._latin1(s).hex()}> Tj ET")
            return
        glyphs = self._glyphs(s)
        for gid, ch in glyphs:
            self.used.setdefault(gid, ch)
        hexs = "".join(f"{gid:04x}" for gid, _ in glyphs)
        mode = (f"2 Tr {size * 0.035:.3f} w {r:.3f} {g:.3f} {b:.3f} RG " if bold else "0 Tr ")
        ops.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg {mode}/F1 {size:.2f} Tf {x:.2f} {y:.2f} Td <{hexs}> Tj ET")

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
        f = self.font
        gids = sorted(self.used) or [0]
        tag = "".join(chr(65 + (sum(gids) // 26 ** i) % 26) for i in range(6))
        base = f"{tag}+{f.name}"
        data = f.subset(set(gids), self.used)
        file_id = stream(data, f" /Length1 {len(data)}")
        x0, y0, x1, y1 = f.bbox
        desc = add((f"<< /Type /FontDescriptor /FontName /{base} /Flags 4 /FontBBox [{x0} {y0} {x1} {y1}] "
                    f"/ItalicAngle 0 /Ascent {f.ascent} /Descent {f.descent} /CapHeight {round(f.cap_height)} "
                    f"/StemV 80 /FontFile2 {file_id} 0 R >>").encode())
        widths = " ".join(f"{g} [{round(f.width(g))}]" for g in gids)
        cid = add((f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} "
                   f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                   f"/FontDescriptor {desc} 0 R /DW 1000 /W [{widths}] /CIDToGIDMap /Identity >>").encode())
        tounicode = stream(_tounicode(self.used).encode("ascii"))
        f1 = add((f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
                  f"/DescendantFonts [{cid} 0 R] /ToUnicode {tounicode} 0 R >>").encode())
        return f"/F1 {f1} 0 R"


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
