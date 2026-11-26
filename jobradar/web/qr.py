"""A small QR code encoder (byte mode, versions 1-10), standard library only,
so the dashboard can show a code to open it on your phone.

Follows ISO/IEC 18004 the same way well-known minimal encoders do: data bits →
Reed-Solomon error correction per block → interleave → place in the zig-zag →
pick the mask with the lowest penalty → format/version information.
"""

from __future__ import annotations

# Error-correction codewords per block, and number of blocks, by level and version (1-10).
_ECC_PER_BLOCK = {"L": [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
                  "M": [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]}
_BLOCKS = {"L": [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
           "M": [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]}
_FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 10


class QrError(ValueError):
    pass


def _raw_modules(ver: int) -> int:
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        k = ver // 7 + 2
        n -= (25 * k - 10) * k - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver: int, ecl: str) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _BLOCKS[ecl][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    k = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 8 + k * 3 + 5) // (k * 4 - 4) * 2
    return [6] + sorted(size - 7 - i * step for i in range(k - 1))


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ay in enumerate(pos):
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits("L", 0)          # reserve; rewritten once the mask is known
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.put(a, b, bit)
                self.put(b, a, bit)

    def format_bits(self, ecl: str, mask: int) -> None:
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        n = self.size
        for i in range(6):
            self.put(8, i, bit(i))
        self.put(8, 7, bit(6))
        self.put(8, 8, bit(7))
        self.put(7, 8, bit(8))
        for i in range(9, 15):
            self.put(14 - i, 8, bit(i))
        for i in range(8):
            self.put(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.put(8, n - 15 + i, bit(i))
        self.put(8, n - 8, True)

    def codewords(self, data: list[int]) -> None:
        n, i, right = self.size, 0, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fixed[y][x] and i < len(data) * 8:
                        self.dark[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and f(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        """Rules 1 (runs), 2 (2x2 blocks), 3 (finder-like runs) and 4 (balance)."""
        n, d, score = self.size, self.dark, 0
        lines = [row for row in d] + [[d[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            s = "".join("1" if v else "0" for v in line)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(n - 1):
            for x in range(n - 1):
                if d[y][x] == d[y][x + 1] == d[y + 1][x] == d[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in d for v in row)
        score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
        return score


def encode(text: str, ecl: str = "M") -> list[list[bool]]:
    """The QR code for ``text`` as rows of booleans (True = dark)."""
    data = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        cap = _data_codewords(ver, ecl)
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + 8 * len(data) <= cap * 8:
            break
    else:
        raise QrError("text too long for a QR code here")
    bits: list[int] = []

    def add(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    add(0b0100, 4)
    add(len(data), count_bits)
    for b in data:
        add(b, 8)
    add(0, min(4, cap * 8 - len(bits)))
    add(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap * 8:
        add(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    nblocks, ecc_len = _BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
    raw = _raw_modules(ver) // 8
    short_count = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = words[k:k + short_len - ecc_len + (0 if i < short_count else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < short_count:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = [blk[i] for i in range(len(blocks[0])) for j, blk in enumerate(blocks)
             if i != short_len - ecc_len or j >= short_count]

    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8):
        m = _Matrix(ver)
        m.function_patterns()
        m.codewords(final)
        m.apply_mask(mask)
        m.format_bits(ecl, mask)
        p = m.penalty()
        if best is None or p < best[0]:
            best = (p, m.dark)
    return best[1]


def svg(text: str, *, scale: int = 5, border: int = 4) -> str:
    """The QR code as an SVG string (black on white)."""
    rows = encode(text)
    n = len(rows) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(rows)
                   for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" '
            f'height="{n * scale}" shape-rendering="crispEdges" role="img" aria-label="QR code">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
