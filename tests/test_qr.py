"""QR code encoder tests: structure checks and real decoding through OpenCV."""

from __future__ import annotations

import pytest

from app.services import qr, two_factor

FINDER = [
    [True, True, True, True, True, True, True],
    [True, False, False, False, False, False, True],
    [True, False, True, True, True, False, True],
    [True, False, True, True, True, False, True],
    [True, False, True, True, True, False, True],
    [True, False, False, False, False, False, True],
    [True, True, True, True, True, True, True],
]
# Byte-mode capacities at ECC level M for versions 1..10 (ISO/IEC 18004 table 7).
M_BYTE_CAPACITY = (14, 26, 42, 62, 84, 106, 122, 152, 180, 213)


def _version(matrix: list[list[bool]]) -> int:
    """Return the symbol version implied by the matrix size."""
    return (len(matrix) - 17) // 4


def _block(matrix: list[list[bool]], top: int, left: int, size: int) -> list[list[bool]]:
    """Cut a square sub-grid out of the matrix."""
    return [row[left:left + size] for row in matrix[top:top + size]]


def _format_bits(matrix: list[list[bool]]) -> tuple[int, int]:
    """Read both 15-bit format-information copies (bit 0 first, like the encoder)."""
    size = len(matrix)
    first_positions = (
        [(8, index) for index in range(6)] + [(8, 7), (8, 8), (7, 8)]
        + [(14 - index, 8) for index in range(9, 15)]
    )
    second_positions = (
        [(size - 1 - index, 8) for index in range(8)]
        + [(8, size - 15 + index) for index in range(8, 15)]
    )

    def read(positions: list[tuple[int, int]]) -> int:
        return sum(1 << index for index, (x, y) in enumerate(positions) if matrix[y][x])

    return read(first_positions), read(second_positions)


@pytest.mark.parametrize("ecc", ["L", "M", "Q", "H"])
def test_structure_has_finders_timing_and_format(ecc: str) -> None:
    """Size, finder patterns, separators, timing lines, dark module, and format info."""
    matrix = qr.qr_matrix("otpauth://totp/AkremMobile:owner", ecc=ecc)
    size = len(matrix)
    version = _version(matrix)
    assert size == 17 + 4 * version
    assert all(len(row) == size for row in matrix)

    assert _block(matrix, 0, 0, 7) == FINDER
    assert _block(matrix, 0, size - 7, 7) == FINDER
    assert _block(matrix, size - 7, 0, 7) == FINDER
    # Light separators around the top-left finder.
    assert not any(matrix[7][x] for x in range(8))
    assert not any(matrix[y][7] for y in range(8))

    for index in range(8, size - 8):
        assert matrix[6][index] == (index % 2 == 0)
        assert matrix[index][6] == (index % 2 == 0)
    assert matrix[size - 8][8] is True

    first, second = _format_bits(matrix)
    assert first == second
    data = (first ^ 0x5412) >> 10
    expected_level = {"L": 1, "M": 0, "Q": 3, "H": 2}[ecc]
    assert data >> 3 == expected_level
    assert 0 <= data & 0b111 <= 7


@pytest.mark.parametrize("version", range(1, 11))
def test_version_boundaries_at_level_m(version: int) -> None:
    """The smallest fitting version is chosen at each capacity boundary."""
    capacity = M_BYTE_CAPACITY[version - 1]
    assert qr.qr_version_for("a" * capacity) == version
    assert _version(qr.qr_matrix("a" * capacity)) == version
    assert qr.qr_version_for("a" * (capacity + 1)) == version + 1


def test_version_info_drawn_for_version_seven_and_later() -> None:
    """Both 6x3 version-information blocks match the BCH code for the version."""
    matrix = qr.qr_matrix("x" * 120)  # version 7 at level M
    size = len(matrix)
    assert _version(matrix) == 7
    bottom_left = sum(
        1 << index for index in range(18) if matrix[size - 11 + index % 3][index // 3]
    )
    top_right = sum(
        1 << index for index in range(18) if matrix[index // 3][size - 11 + index % 3]
    )
    assert bottom_left == top_right == 0x07C94  # Published version-7 information word.


def test_invalid_level_and_too_long_text_raise() -> None:
    """Unknown ECC levels and text beyond version 40 are rejected."""
    with pytest.raises(ValueError):
        qr.qr_matrix("hello", ecc="X")
    with pytest.raises(ValueError):
        qr.qr_matrix("a" * 2954, ecc="L")
    assert _version(qr.qr_matrix("a" * 2953, ecc="L")) == 40


def test_output_is_deterministic_and_boolean() -> None:
    """The same input always produces the same grid of booleans."""
    first = qr.qr_matrix("AkremMobile")
    assert first == qr.qr_matrix("AkremMobile")
    assert {cell for row in first for cell in row} <= {True, False}


def _decode(matrix: list[list[bool]]) -> str:
    """Render with a 4-module quiet zone at scale 8 and decode with OpenCV."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image = np.where(np.array(matrix, dtype=bool), 0, 255).astype(np.uint8)
    image = np.pad(image, 4, constant_values=255)
    image = np.kron(image, np.ones((8, 8), dtype=np.uint8))
    text = cv2.QRCodeDetector().detectAndDecode(image)[0]
    if not text and hasattr(cv2, "QRCodeDetectorAruco"):
        # The classic detector occasionally misses valid dense symbols.
        text = cv2.QRCodeDetectorAruco().detectAndDecode(image)[0]
    return text


@pytest.mark.parametrize(
    ("text", "ecc"),
    [
        ("hello", "M"),
        ("A", "L"),
        ("مرحبا بكم في أكرم موبايل", "M"),
        ("https://example.com/?q=1&r=2", "Q"),
        ("0123456789" * 3, "H"),
        ("a" * 14, "M"),  # version 1 full
        ("a" * 15, "M"),  # version 2
        ("b" * 106, "M"),  # version 6 full
        ("b" * 107, "M"),  # version 7 (version info)
        ("c" * 213, "M"),  # version 10 full
        ("c" * 214, "M"),  # version 11
        ("d" * 400, "L"),
    ],
)
def test_round_trip_decodes(text: str, ecc: str) -> None:
    """Generated symbols decode back to the original text."""
    assert _decode(qr.qr_matrix(text, ecc=ecc)) == text


def test_real_provisioning_uri_decodes() -> None:
    """An authenticator URI for a 30-character username fits and decodes."""
    secret = two_factor.generate_secret()
    uri = two_factor.provisioning_uri(secret, "u" * 30)
    matrix = qr.qr_matrix(uri)
    assert _version(matrix) <= 10
    assert _decode(matrix) == uri
