"""TrueType fonts for the résumé PDF: find one on this computer, read its
metrics, and cut a small subset to embed.

Chinese text needs a Chinese font *embedded* in the PDF, or phones and browsers
show empty boxes. Every desktop OS ships one (Windows: Microsoft YaHei / SimHei
/ SimSun; macOS: STHeiti / Hiragino; Linux: WenQuanYi / Droid). Only fonts with
TrueType outlines (a ``glyf`` table) are used, since those can be subset with
the standard library; CFF-based OpenType fonts are skipped.

Subsetting keeps every glyph slot (so glyph ids, ``hmtx`` and the PDF's
CIDToGIDMap stay the identity) but empties the outlines of glyphs that aren't
used. A résumé's font is then a few hundred KB instead of 10-20 MB.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# Composite glyph flags (TrueType "glyf" spec).
_ARG_WORDS, _HAVE_SCALE, _MORE, _XY_SCALE, _TWO_BY_TWO = 0x0001, 0x0008, 0x0020, 0x0040, 0x0080
_KEEP_TABLES = ("head", "hhea", "maxp", "OS/2", "hmtx", "cvt ", "fpgm", "prep", "loca", "glyf", "post")


class FontError(ValueError):
    pass


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def _i16(b: bytes, o: int) -> int:
    return struct.unpack_from(">h", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


class TrueTypeFont:
    """One face of a .ttf/.ttc file, with what the PDF writer needs."""

    def __init__(self, data: bytes, index: int = 0, path: str = ""):
        self.data, self.path, self.index = data, path, index
        start = 0
        if data[:4] == b"ttcf":
            count = _u32(data, 8)
            if index >= count:
                raise FontError(f"{path}: no face {index}")
            start = _u32(data, 12 + 4 * index)
        flavor = data[start:start + 4]
        if flavor not in (b"\x00\x01\x00\x00", b"true"):
            raise FontError(f"{path}: not a TrueType-outline font")
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(_u16(data, start + 4)):
            rec = start + 12 + 16 * i
            tag = data[rec:rec + 4].decode("latin-1")
            self.tables[tag] = (_u32(data, rec + 8), _u32(data, rec + 12))
        for need in ("head", "hhea", "maxp", "hmtx", "loca", "glyf", "cmap"):
            if need not in self.tables:
                raise FontError(f"{path}: missing table {need!r}")
        head = self.table("head")
        self.units_per_em = _u16(head, 18) or 1000
        self.bbox = tuple(self._scale(_i16(head, o)) for o in (36, 38, 40, 42))
        self.loca_long = _i16(head, 50) == 1
        hhea = self.table("hhea")
        self.ascent = self._scale(_i16(hhea, 4))
        self.descent = self._scale(_i16(hhea, 6))
        n_hmetrics = _u16(hhea, 34)
        self.num_glyphs = _u16(self.table("maxp"), 4)
        hmtx = self.table("hmtx")
        adv = [_u16(hmtx, 4 * i) for i in range(min(n_hmetrics, len(hmtx) // 4))] or [self.units_per_em]
        self.advances = adv + [adv[-1]] * max(0, self.num_glyphs - len(adv))
        self.cap_height = self.ascent * 0.7
        if "OS/2" in self.tables:
            os2 = self.table("OS/2")
            if _u16(os2, 0) >= 2 and len(os2) >= 90:
                self.cap_height = self._scale(_i16(os2, 88)) or self.cap_height
        self.cmap = self._read_cmap()
        self.name = self._postscript_name() or Path(path).stem or "Font"

    # ------------------------------------------------------------- reading
    def table(self, tag: str) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def _scale(self, v: int) -> int:
        return round(v * 1000 / self.units_per_em)

    def _read_cmap(self) -> dict[int, int]:
        cmap = self.table("cmap")
        best = None
        for i in range(_u16(cmap, 2)):
            pid, eid, off = _u16(cmap, 4 + 8 * i), _u16(cmap, 6 + 8 * i), _u32(cmap, 8 + 8 * i)
            fmt = _u16(cmap, off)
            rank = {(3, 10, 12): 0, (0, 4, 12): 1, (0, 6, 12): 1, (3, 1, 4): 2, (0, 3, 4): 3}.get((pid, eid, fmt))
            if rank is None and pid == 0 and fmt in (4, 12):
                rank = 4
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, off, fmt)
        if best is None:
            raise FontError(f"{self.path}: no Unicode cmap")
        _, off, fmt = best
        out: dict[int, int] = {}
        if fmt == 12:
            for g in range(_u32(cmap, off + 12)):
                s, e, gid = struct.unpack_from(">III", cmap, off + 16 + 12 * g)
                for cp in range(s, min(e, s + 70000) + 1):
                    out[cp] = gid + (cp - s)
        else:
            segs = _u16(cmap, off + 6) // 2
            ends, starts = off + 14, off + 16 + 2 * segs
            deltas, ranges = starts + 2 * segs, starts + 4 * segs
            for s in range(segs):
                end, start = _u16(cmap, ends + 2 * s), _u16(cmap, starts + 2 * s)
                delta, ro = _i16(cmap, deltas + 2 * s), _u16(cmap, ranges + 2 * s)
                for cp in range(start, end + 1):
                    if cp == 0xFFFF:
                        continue
                    if ro == 0:
                        gid = (cp + delta) & 0xFFFF
                    else:
                        p = ranges + 2 * s + ro + 2 * (cp - start)
                        gid = _u16(cmap, p) if p + 2 <= len(cmap) else 0
                        gid = (gid + delta) & 0xFFFF if gid else 0
                    if gid:
                        out[cp] = gid
        return {cp: g for cp, g in out.items() if 0 < g < self.num_glyphs}

    def _postscript_name(self) -> str:
        if "name" not in self.tables:
            return ""
        t = self.table("name")
        strings = _u16(t, 4)
        for i in range(_u16(t, 2)):
            pid, eid, _, nid, length, off = struct.unpack_from(">HHHHHH", t, 6 + 12 * i)
            if nid != 6:
                continue
            raw = t[strings + off:strings + off + length]
            try:
                s = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
            except UnicodeDecodeError:
                continue
            s = "".join(c for c in s if c.isascii() and (c.isalnum() or c in "-_"))
            if s:
                return s[:60]
        return ""

    # ------------------------------------------------------------- metrics
    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def gid(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def width(self, gid: int) -> float:
        """Advance width in 1/1000 em."""
        return self.advances[gid] * 1000 / self.units_per_em if gid < len(self.advances) else 500

    def coverage(self, text: str) -> float:
        chars = {c for c in text if not c.isspace()}
        return 1.0 if not chars else sum(1 for c in chars if self.has(c)) / len(chars)

    # ------------------------------------------------------------ subset
    def _glyph_range(self, gid: int) -> tuple[int, int]:
        loca = self.table("loca")
        if self.loca_long:
            return _u32(loca, 4 * gid), _u32(loca, 4 * gid + 4)
        return 2 * _u16(loca, 2 * gid), 2 * _u16(loca, 2 * gid + 2)

    def _components(self, glyph: bytes) -> list[int]:
        if len(glyph) < 10 or _i16(glyph, 0) >= 0:
            return []
        out, p = [], 10
        while p + 4 <= len(glyph):
            flags, comp = _u16(glyph, p), _u16(glyph, p + 2)
            out.append(comp)
            p += 4 + (4 if flags & _ARG_WORDS else 2)
            p += 2 if flags & _HAVE_SCALE else 4 if flags & _XY_SCALE else 8 if flags & _TWO_BY_TWO else 0
            if not flags & _MORE:
                break
        return out

    def subset(self, gids: set[int], chars: dict[int, str] | None = None) -> bytes:
        """A standalone TrueType file whose glyphs outside ``gids`` are empty.
        ``chars`` (gid → character) becomes a small cmap; viewers that check
        embedded fonts like to see one, though the PDF maps glyphs directly."""
        glyf = self.table("glyf")
        keep, todo = set(), {0, *gids}
        while todo:
            g = todo.pop()
            if g in keep or g >= self.num_glyphs:
                continue
            keep.add(g)
            s, e = self._glyph_range(g)
            todo.update(self._components(glyf[s:e]))
        new_glyf, offsets = bytearray(), []
        for g in range(self.num_glyphs):
            offsets.append(len(new_glyf))
            if g in keep:
                s, e = self._glyph_range(g)
                new_glyf += glyf[s:e]
                new_glyf += b"\0" * (-len(new_glyf) % 4)
        offsets.append(len(new_glyf))
        head = bytearray(self.table("head"))
        struct.pack_into(">I", head, 8, 0)      # checksumAdjustment, fixed below
        struct.pack_into(">h", head, 50, 1)     # long loca offsets
        tables = {"head": bytes(head), "loca": struct.pack(f">{len(offsets)}I", *offsets),
                  "glyf": bytes(new_glyf)}
        for tag in _KEEP_TABLES:
            if tag not in tables and tag in self.tables:
                tables[tag] = self.table(tag)
        tables["cmap"] = _cmap4({ord(ch): g for g, ch in (chars or {}).items() if len(ch) == 1 and ord(ch) < 0xFFFF})
        if "post" in tables:  # version 3: no glyph names, much smaller
            tables["post"] = b"\x00\x03\x00\x00" + tables["post"][4:32]
        return _build_sfnt(tables)


def _cmap4(mapping: dict[int, int]) -> bytes:
    """A (3,1) format-4 cmap with one segment per character."""
    cps = sorted(mapping)
    ends = cps + [0xFFFF]
    starts = cps + [0xFFFF]
    deltas = [(mapping[c] - c) & 0xFFFF for c in cps] + [1]
    n = len(ends)
    pow2 = 1 << (n.bit_length() - 1)
    body = struct.pack(">HHHH", n * 2, pow2 * 2, pow2.bit_length() - 1, n * 2 - pow2 * 2)
    body += struct.pack(f">{n}H", *ends) + b"\0\0" + struct.pack(f">{n}H", *starts)
    body += struct.pack(f">{n}H", *deltas) + struct.pack(f">{n}H", *([0] * n))
    sub = struct.pack(">HHH", 4, 6 + len(body), 0) + body
    return struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub


def _checksum(b: bytes) -> int:
    b = b + b"\0" * (-len(b) % 4)
    return sum(struct.unpack(f">{len(b) // 4}I", b)) & 0xFFFFFFFF


def _build_sfnt(tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    pow2 = 1 << (n.bit_length() - 1)
    header = struct.pack(">IHHHH", 0x00010000, n, pow2 * 16, pow2.bit_length() - 1, n * 16 - pow2 * 16)
    offset = 12 + 16 * n
    directory, body = b"", b""
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    font = bytearray(header + directory + body)
    head_at = offset + sum(len(tables[t]) + (-len(tables[t]) % 4) for t in tags[:tags.index("head")])
    struct.pack_into(">I", font, head_at + 8, (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


# --------------------------------------------------------------- discovery
def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs += [Path(windir) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"),
                 Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts",
                 Path.home() / ".fonts"]
    return dirs


# Preferred file names, Chinese-capable first. Matched case-insensitively.
CJK_FONTS = ("msyh.ttc", "msyh.ttf", "simhei.ttf", "deng.ttf", "simsun.ttc", "msjh.ttc",
             "stheiti light.ttc", "stheiti medium.ttc", "hiragino sans gb.ttc", "arial unicode.ttf",
             "songti.ttc", "wqy-microhei.ttc", "wqy-zenhei.ttc", "droidsansfallbackfull.ttf",
             "droidsansfallback.ttf", "uming.ttc", "ukai.ttc")
LATIN_FONTS = ("arial.ttf", "segoeui.ttf", "calibri.ttf", "helvetica.ttc", "dejavusans.ttf",
               "liberationsans-regular.ttf", "freesans.ttf", "carlito-regular.ttf")


def _candidates(extra_dirs: list[Path]) -> dict[str, Path]:
    """Every .ttf/.ttc on this computer by lower-cased file name (first wins)."""
    found: dict[str, Path] = {}
    for d in [*extra_dirs, *_font_dirs()]:
        if not d.is_dir():
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                low = f.lower()
                if low.endswith((".ttf", ".ttc")) and low not in found:
                    found[low] = Path(root) / f
    return found


def _faces(path: Path) -> list[TrueTypeFont]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    count = _u32(data, 8) if data[:4] == b"ttcf" else 1
    faces = []
    for i in range(min(count, 4)):
        try:
            faces.append(TrueTypeFont(data, i, str(path)))
        except (FontError, struct.error, IndexError):
            continue
    return faces


def find_font(text: str, *, preferred: str | Path | None = None,
              extra_dirs: list[Path] | None = None) -> TrueTypeFont | None:
    """The best installed font for ``text``: the one you configured if it covers
    the text, else a known Chinese or Latin font, else any font that covers it."""
    needs_cjk = any(ord(c) >= 0x2E80 for c in text)
    if preferred:
        for face in _faces(Path(preferred).expanduser()):
            if face.coverage(text) >= 0.999:
                return face
    found = _candidates(list(extra_dirs or []))
    order = (CJK_FONTS if needs_cjk else LATIN_FONTS + CJK_FONTS)
    best: tuple[float, TrueTypeFont] | None = None
    for name in order:
        if name in found:
            for face in _faces(found[name]):
                cov = face.coverage(text)
                if cov >= 0.999:
                    return face
                if best is None or cov > best[0]:
                    best = (cov, face)
    if needs_cjk:  # any other installed font with Chinese glyphs
        probe = "的一是在不了有和人这中大为上个国我以要他"
        for name, path in sorted(found.items()):
            if name in order or os.path.getsize(path) < 2_000_000:
                continue
            for face in _faces(path):
                if face.coverage(probe) >= 0.999:
                    cov = face.coverage(text)
                    if cov >= 0.999:
                        return face
                    if best is None or cov > best[0]:
                        best = (cov, face)
    return best[1] if best and best[0] >= 0.9 else None


# Regular → bold file names (lower-case), for a real bold weight.
BOLD_OF = {"msyh.ttc": "msyhbd.ttc", "msyh.ttf": "msyhbd.ttf", "deng.ttf": "dengb.ttf", "msjh.ttc": "msjhbd.ttc",
           "stheiti light.ttc": "stheiti medium.ttc", "arial.ttf": "arialbd.ttf", "segoeui.ttf": "segoeuib.ttf",
           "calibri.ttf": "calibrib.ttf", "dejavusans.ttf": "dejavusans-bold.ttf",
           "liberationsans-regular.ttf": "liberationsans-bold.ttf", "freesans.ttf": "freesansbold.ttf",
           "carlito-regular.ttf": "carlito-bold.ttf", "notosanssc-regular.ttf": "notosanssc-bold.ttf"}


def find_bold(regular: TrueTypeFont, text: str, *, extra_dirs: list[Path] | None = None) -> TrueTypeFont | None:
    """The bold sibling of ``regular`` (e.g. msyhbd.ttc for msyh.ttc) if it is
    installed and covers ``text``; None means "draw bold by stroking"."""
    name = Path(regular.path).name.lower()
    want = BOLD_OF.get(name)
    if not want:
        if "regular" in name:
            want = name.replace("regular", "bold")
        else:
            return None
    path = Path(regular.path).with_name(want)
    if not path.exists():
        path = next((p for p in Path(regular.path).parent.iterdir() if p.name.lower() == want), None) \
            or _candidates(list(extra_dirs or [])).get(want)
    if not path:
        return None
    faces = _faces(path)
    face = faces[regular.index] if regular.index < len(faces) else (faces[0] if faces else None)
    return face if face and face.coverage(text) >= 0.999 else None
