"""Pure-Python QR Code (Model 2) encoder producing a module matrix.

Byte mode (UTF-8) only, all four error-correction levels, versions 1 to 40,
automatic mask selection using the standard ISO/IEC 18004 penalty rules.

The algorithms are ported from Project Nayuki's QR Code generator library:

    QR Code generator library (Python)

    Copyright (c) Project Nayuki. (MIT License)
    https://www.nayuki.io/page/qr-code-generator-library

    Permission is hereby granted, free of charge, to any person obtaining a copy of
    this software and associated documentation files (the "Software"), to deal in
    the Software without restriction, including without limitation the rights to
    use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
    the Software, and to permit persons to whom the Software is furnished to do so,
    subject to the following conditions:
    - The above copyright notice and this permission notice shall be included in
      all copies or substantial portions of the Software.
    - The Software is provided "as is", without warranty of any kind, express or
      implied, including but not limited to the warranties of merchantability,
      fitness for a particular purpose and noninfringement. In no event shall the
      authors or copyright holders be liable for any claim, damages or other
      liability, whether in an action of contract, tort or otherwise, arising from,
      out of or in connection with the Software or the use or other dealings in the
      Software.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

MIN_VERSION = 1
MAX_VERSION = 40

_PENALTY_N1 = 3
_PENALTY_N2 = 3
_PENALTY_N3 = 40
_PENALTY_N4 = 10

# Error-correction level -> (ordinal used by the tables below, 2-bit format value).
_ECC_LEVELS: dict[str, tuple[int, int]] = {"L": (0, 1), "M": (1, 0), "Q": (2, 3), "H": (3, 2)}

# Index [ecc ordinal][version]; index 0 is unused padding.
_ECC_CODEWORDS_PER_BLOCK: tuple[tuple[int, ...], ...] = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_ERROR_CORRECTION_BLOCKS: tuple[tuple[int, ...], ...] = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

_MASK_PATTERNS: tuple[Callable[[int, int], int], ...] = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)

Grid = list[list[bool]]


def qr_matrix(text: str, *, ecc: str = "M") -> list[list[bool]]:
    """Encode ``text`` as UTF-8 bytes and return the QR module grid (True = dark).

    The grid is square with side ``17 + 4 * version`` and has no quiet zone; the
    renderer must add a light border of at least four modules. Raises
    ``ValueError`` for an unknown ECC level or text too long for version 40.
    """
    if ecc not in _ECC_LEVELS:
        raise ValueError(f"Unsupported error-correction level: {ecc}")
    ecc_ordinal, _format_value = _ECC_LEVELS[ecc]
    payload = text.encode("utf-8")
    version = _choose_version(len(payload), ecc_ordinal)
    data_codewords = _encode_data(payload, version, ecc_ordinal)
    all_codewords = _add_ecc_and_interleave(data_codewords, version, ecc_ordinal)
    return _QrBuilder(version, ecc).build(all_codewords)


def qr_version_for(text: str, *, ecc: str = "M") -> int:
    """Return the smallest version that holds ``text`` at the given ECC level."""
    if ecc not in _ECC_LEVELS:
        raise ValueError(f"Unsupported error-correction level: {ecc}")
    return _choose_version(len(text.encode("utf-8")), _ECC_LEVELS[ecc][0])


def _choose_version(byte_count: int, ecc_ordinal: int) -> int:
    """Pick the smallest version whose data capacity fits a byte-mode segment."""
    for version in range(MIN_VERSION, MAX_VERSION + 1):
        needed_bits = 4 + _char_count_bits(version) + byte_count * 8
        if byte_count < (1 << _char_count_bits(version)) and needed_bits <= (
            _num_data_codewords(version, ecc_ordinal) * 8
        ):
            return version
    raise ValueError("Text is too long to fit in a QR code")


def _char_count_bits(version: int) -> int:
    """Return the byte-mode character count field width for a version."""
    return 8 if version <= 9 else 16


def _encode_data(payload: bytes, version: int, ecc_ordinal: int) -> list[int]:
    """Build the data codewords: mode, count, bytes, terminator, and padding."""
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(payload), _char_count_bits(version))
    for value in payload:
        _append_bits(bits, value, 8)
    capacity_bits = _num_data_codewords(version, ecc_ordinal) * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    _append_bits(bits, 0, -len(bits) % 8)
    pad_byte = 0xEC
    while len(bits) < capacity_bits:
        _append_bits(bits, pad_byte, 8)
        pad_byte ^= 0xEC ^ 0x11
    return [
        int("".join(str(bit) for bit in bits[index:index + 8]), 2)
        for index in range(0, len(bits), 8)
    ]


def _append_bits(bits: list[int], value: int, length: int) -> None:
    """Append ``length`` low bits of ``value``, most significant first."""
    for shift in reversed(range(length)):
        bits.append((value >> shift) & 1)


def _num_raw_data_modules(version: int) -> int:
    """Count modules available for data and ECC after function patterns."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _num_data_codewords(version: int, ecc_ordinal: int) -> int:
    """Return the number of 8-bit data codewords for a version and ECC level."""
    return (
        _num_raw_data_modules(version) // 8
        - _ECC_CODEWORDS_PER_BLOCK[ecc_ordinal][version]
        * _NUM_ERROR_CORRECTION_BLOCKS[ecc_ordinal][version]
    )


def _add_ecc_and_interleave(data: list[int], version: int, ecc_ordinal: int) -> list[int]:
    """Split data into blocks, append Reed-Solomon ECC, and interleave codewords."""
    num_blocks = _NUM_ERROR_CORRECTION_BLOCKS[ecc_ordinal][version]
    block_ecc_len = _ECC_CODEWORDS_PER_BLOCK[ecc_ordinal][version]
    raw_codewords = _num_raw_data_modules(version) // 8
    num_short_blocks = num_blocks - raw_codewords % num_blocks
    short_block_len = raw_codewords // num_blocks
    divisor = _reed_solomon_divisor(block_ecc_len)

    blocks: list[list[int]] = []
    offset = 0
    for index in range(num_blocks):
        length = short_block_len - block_ecc_len + (0 if index < num_short_blocks else 1)
        block = data[offset:offset + length]
        offset += length
        ecc_bytes = _reed_solomon_remainder(block, divisor)
        if index < num_short_blocks:
            block.append(0)
        blocks.append(block + ecc_bytes)

    result: list[int] = []
    for position in range(len(blocks[0])):
        for index, block in enumerate(blocks):
            # Skip the padding byte added to short blocks.
            if position != short_block_len - block_ecc_len or index >= num_short_blocks:
                result.append(block[position])
    return result


def _reed_solomon_divisor(degree: int) -> list[int]:
    """Return the generator polynomial coefficients (highest power dropped)."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_multiply(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _reed_solomon_remainder(data: list[int], divisor: list[int]) -> list[int]:
    """Return the Reed-Solomon ECC codewords for one data block."""
    result = [0] * len(divisor)
    for value in data:
        factor = value ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_multiply(coefficient, factor)
    return result


def _gf_multiply(x: int, y: int) -> int:
    """Multiply two elements of GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    product = 0
    for shift in reversed(range(8)):
        product = (product << 1) ^ ((product >> 7) * 0x11D)
        product ^= ((y >> shift) & 1) * x
    return product


class _QrBuilder:
    """Draw function patterns, codewords, and the best mask for one symbol."""

    def __init__(self, version: int, ecc: str) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.format_value = _ECC_LEVELS[ecc][1]
        self.modules: Grid = [[False] * self.size for _ in range(self.size)]
        self.is_function: Grid = [[False] * self.size for _ in range(self.size)]

    def build(self, codewords: list[int]) -> Grid:
        """Return the finished, masked module grid."""
        self._draw_function_patterns()
        self._draw_codewords(codewords)
        mask = self._best_mask()
        self._apply_mask(mask)
        self._draw_format_bits(mask)
        return [row[:] for row in self.modules]

    def _set_function(self, x: int, y: int, dark: bool) -> None:
        """Set a module at column ``x``, row ``y`` and mark it as a function module."""
        self.modules[y][x] = dark
        self.is_function[y][x] = True

    def _draw_function_patterns(self) -> None:
        """Draw timing, finder, alignment, format (placeholder), and version patterns."""
        for index in range(self.size):
            self._set_function(6, index, index % 2 == 0)
            self._set_function(index, 6, index % 2 == 0)
        self._draw_finder(3, 3)
        self._draw_finder(self.size - 4, 3)
        self._draw_finder(3, self.size - 4)
        positions = self._alignment_positions()
        last = len(positions) - 1
        for i, x in enumerate(positions):
            for j, y in enumerate(positions):
                if (i, j) not in ((0, 0), (0, last), (last, 0)):
                    self._draw_alignment(x, y)
        self._draw_format_bits(0)
        self._draw_version()

    def _draw_finder(self, cx: int, cy: int) -> None:
        """Draw a 9x9 finder pattern (with separator) centred at ``cx, cy``."""
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    self._set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def _draw_alignment(self, cx: int, cy: int) -> None:
        """Draw a 5x5 alignment pattern centred at ``cx, cy``."""
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def _alignment_positions(self) -> list[int]:
        """Return the ascending centre coordinates of alignment patterns."""
        if self.version == 1:
            return []
        num_align = self.version // 7 + 2
        step = (self.version * 8 + num_align * 3 + 5) // (num_align * 4 - 4) * 2
        result = [self.size - 7 - index * step for index in range(num_align - 1)] + [6]
        return list(reversed(result))

    def _draw_format_bits(self, mask: int) -> None:
        """Draw both copies of the 15-bit BCH-protected format information."""
        data = self.format_value << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412

        for index in range(0, 6):
            self._set_function(8, index, _bit(bits, index))
        self._set_function(8, 7, _bit(bits, 6))
        self._set_function(8, 8, _bit(bits, 7))
        self._set_function(7, 8, _bit(bits, 8))
        for index in range(9, 15):
            self._set_function(14 - index, 8, _bit(bits, index))

        for index in range(0, 8):
            self._set_function(self.size - 1 - index, 8, _bit(bits, index))
        for index in range(8, 15):
            self._set_function(8, self.size - 15 + index, _bit(bits, index))
        self._set_function(8, self.size - 8, True)  # The always-dark module.

    def _draw_version(self) -> None:
        """Draw both copies of the 18-bit version information for version 7+."""
        if self.version < 7:
            return
        remainder = self.version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = self.version << 12 | remainder
        for index in range(18):
            dark = _bit(bits, index)
            a = self.size - 11 + index % 3
            b = index // 3
            self._set_function(a, b, dark)
            self._set_function(b, a, dark)

    def _draw_codewords(self, codewords: list[int]) -> None:
        """Place codeword bits in the zigzag order, skipping function modules."""
        bit_index = 0
        total_bits = len(codewords) * 8
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5  # Skip the vertical timing column.
            upward = (right + 1) & 2 == 0
            for vertical in range(self.size):
                y = self.size - 1 - vertical if upward else vertical
                for offset in range(2):
                    x = right - offset
                    if not self.is_function[y][x] and bit_index < total_bits:
                        byte = codewords[bit_index >> 3]
                        self.modules[y][x] = _bit(byte, 7 - (bit_index & 7))
                        bit_index += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        """XOR a mask pattern onto all non-function modules (self-inverse)."""
        pattern = _MASK_PATTERNS[mask]
        for y in range(self.size):
            row = self.modules[y]
            function_row = self.is_function[y]
            for x in range(self.size):
                if not function_row[x] and pattern(x, y) == 0:
                    row[x] = not row[x]

    def _best_mask(self) -> int:
        """Try all eight masks and return the one with the lowest penalty."""
        best_mask = 0
        best_penalty: int | None = None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = _penalty_score(self.modules)
            if best_penalty is None or penalty < best_penalty:
                best_mask, best_penalty = mask, penalty
            self._apply_mask(mask)
        return best_mask


def _bit(value: int, index: int) -> bool:
    """Return whether bit ``index`` of ``value`` is set."""
    return (value >> index) & 1 != 0


def _penalty_score(modules: Grid) -> int:
    """Compute the ISO/IEC 18004 mask penalty (N1 runs, N2 blocks, N3 finders, N4 balance)."""
    size = len(modules)
    result = 0
    for row in modules:
        result += _line_penalty(row, size)
    for x in range(size):
        result += _line_penalty([modules[y][x] for y in range(size)], size)
    for y in range(size - 1):
        upper, lower = modules[y], modules[y + 1]
        for x in range(size - 1):
            if upper[x] == upper[x + 1] == lower[x] == lower[x + 1]:
                result += _PENALTY_N2
    dark = sum(1 for row in modules for cell in row if cell)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    result += k * _PENALTY_N4
    return result


def _line_penalty(line: list[bool], size: int) -> int:
    """Penalty for same-colour runs and finder-like patterns in one row or column."""
    result = 0
    run_color = False
    run_length = 0
    history: deque[int] = deque([0] * 7, 7)
    for cell in line:
        if cell == run_color:
            run_length += 1
            if run_length == 5:
                result += _PENALTY_N1
            elif run_length > 5:
                result += 1
        else:
            _add_history(run_length, history, size)
            if not run_color:
                result += _count_finder_patterns(history) * _PENALTY_N3
            run_color = cell
            run_length = 1
    if run_color:
        _add_history(run_length, history, size)
        run_length = 0
    _add_history(run_length + size, history, size)
    result += _count_finder_patterns(history) * _PENALTY_N3
    return result


def _add_history(run_length: int, history: deque[int], size: int) -> None:
    """Push a run length, treating the outer light border as part of the first run."""
    if history[0] == 0:
        run_length += size
    history.appendleft(run_length)


def _count_finder_patterns(history: deque[int]) -> int:
    """Count 1:1:3:1:1 dark/light patterns with four light modules on either side."""
    n = history[1]
    core = (
        n > 0
        and history[2] == history[4] == history[5] == n
        and history[3] == n * 3
    )
    return (
        (1 if core and history[0] >= n * 4 and history[6] >= n else 0)
        + (1 if core and history[6] >= n * 4 and history[0] >= n else 0)
    )
