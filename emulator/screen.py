"""Render the emulated PC's VGA text-mode framebuffer to text and PNG.

The emulated machine keeps an 80x25 character/attribute plane at physical
address 0xB8000 (see ``emulator.bios``): for each cell two bytes are stored,
the ASCII code first and the attribute byte second.

Attribute byte layout (standard CGA/EGA/MDA text attribute)::

    bit  7   6 5 4   3 2 1 0
         |   |     | |       |
         |   |     | +-------+-- foreground colour index (0..15)
         |   +-----+------------ background colour index (0..7)
         +------------------------ blink (or "bright background") flag

This module turns such a buffer into

* plain text (``text`` / ``text_rows`` / ``text_hash``), and
* a PNG screenshot (``render_png`` / ``png_bytes`` / ``image_hash``),

using nothing but the Python standard library (``zlib`` + ``struct`` write the
PNG by hand; Pillow is deliberately not a dependency).

Blink policy
------------
``cell_attr_to_colours`` always interprets bit 7 as "bright background": when
the blink bit is set, colour index 8 is added to the 3-bit background index,
so a blinking cell gets a bright/intense background.  ``render_rows``,
``png_bytes`` and ``render_png`` take an ``include_blink`` flag which defaults
to ``False``; in that default the blink bit is masked off before conversion, so
screenshots are steady and deterministic (a cell with attribute 0x9F renders
exactly like 0x1F).  Pass ``include_blink=True`` to see the brightened
background instead.

Colour indices use :data:`CGA_PALETTE`, the standard 16-entry CGA/EGA palette
with 8 "normal" and 8 "bright" entries.

All source in this module is ASCII-only.
"""

import hashlib
import os
import pathlib
import struct
import zlib

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Physical base address of the VGA text-mode framebuffer.
VIDEO_BASE = 0xB8000

#: Text columns visible on screen.
COLS = 80

#: Text rows visible on screen.
ROWS = 25

#: Bytes per character cell (ASCII code byte followed by attribute byte).
CELL_STRIDE = 2

#: Total size in bytes of one full 80x25 text screen.
VIDEO_SIZE = COLS * ROWS * CELL_STRIDE

#: Width of one character cell in pixels.
FONT_WIDTH = 8

#: Height of one character cell in pixels.
FONT_HEIGHT = 16

#: Standard CGA/EGA 16-colour palette as ``(r, g, b)`` with 0..255 components.
#: Index 0..7 are the normal colours, 8..15 the bright/intense variants.
CGA_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0x00, 0x00, 0x00),  # 0  black
    (0x00, 0x00, 0xAA),  # 1  blue
    (0x00, 0xAA, 0x00),  # 2  green
    (0x00, 0xAA, 0xAA),  # 3  cyan
    (0xAA, 0x00, 0x00),  # 4  red
    (0xAA, 0x00, 0xAA),  # 5  magenta
    (0xAA, 0x55, 0x00),  # 6  brown
    (0xAA, 0xAA, 0xAA),  # 7  light grey
    (0x55, 0x55, 0x55),  # 8  dark grey
    (0x55, 0x55, 0xFF),  # 9  bright blue
    (0x55, 0xFF, 0x55),  # 10 bright green
    (0x55, 0xFF, 0xFF),  # 11 bright cyan
    (0xFF, 0x55, 0x55),  # 12 bright red
    (0xFF, 0x55, 0xFF),  # 13 bright magenta
    (0xFF, 0xFF, 0x55),  # 14 yellow
    (0xFF, 0xFF, 0xFF),  # 15 white
)

# ---------------------------------------------------------------------------
# 8x16 bitmap font
# ---------------------------------------------------------------------------
#
# Encoding: every glyph is 16 hex bytes written as 32 hex digits, one byte per
# font row from top (row 0) to bottom (row 15).  Within a byte, bit 7 is the
# LEFTMOST pixel (x = 0) and bit 0 is the rightmost pixel (x = 7); a set bit
# means "foreground pixel".  So "18" is ..##.... and "7e" is ".######.".
#
# The glyphs were drawn on a 6-pixel-wide art grid that sits at cell columns
# 1..6, i.e. there is a 1-pixel left bearing and a 1-pixel inter-character gap.
# Vertical metrics: row 0-1 blank, capitals/digits rows 2-10 (9 rows),
# ascenders start at row 2, x-height letters occupy rows 4-10, descenders
# (g, j, p, q, y and the comma/semicolon tails) reach row 12.
#
# 0x20 (space) and 0x00 are blank; 0x7F is a full block; any other code that has
# no glyph (0x01-0x1F and 0x80-0xFF) falls back to a hollow box.
_FONT_HEX: dict[int, str] = {
    0x20: "00000000000000000000000000000000",
    0x21: "00001818181818180018180000000000",
    0x22: "00002424000000000000000000000000",
    0x23: "00000024247e24247e24240000000000",
    0x24: "0000183c58583c1c1a3c180000000000",
    0x25: "0000006264081020404c0c0000000000",
    0x26: "000030484830344c444c320000000000",
    0x27: "00001818000000000000000000000000",
    0x28: "00000c102020202020100c0000000000",
    0x29: "00003008040404040408300000000000",
    0x2A: "0000185a3c5a18000000000000000000",
    0x2B: "0000000018187e181800000000000000",
    0x2C: "00000000000000000000181830000000",
    0x2D: "0000000000003c000000000000000000",
    0x2E: "00000000000000000018180000000000",
    0x2F: "00000204040810202040400000000000",
    0x30: "00003c42464a526242423c0000000000",
    0x31: "000018284808080808087e0000000000",
    0x32: "00003c4202020c1020407e0000000000",
    0x33: "00003c4202021c0202423c0000000000",
    0x34: "0000040c1424447e0404040000000000",
    0x35: "00007e4040407c0202423c0000000000",
    0x36: "00003c4240407c4242423c0000000000",
    0x37: "00007e02020404080810100000000000",
    0x38: "00003c4242423c4242423c0000000000",
    0x39: "00003c4242423e0202423c0000000000",
    0x3A: "00000000001818000018180000000000",
    0x3B: "00000000001818000018183000000000",
    0x3C: "00000000000c1830180c000000000000",
    0x3D: "00000000007e00007e00000000000000",
    0x3E: "000000000030180c1830000000000000",
    0x3F: "00003c42020c18180018180000000000",
    0x40: "00003c425a52525c40423c0000000000",
    0x41: "000018244242427e4242420000000000",
    0x42: "00007c4242427c4242427c0000000000",
    0x43: "00003c424040404040423c0000000000",
    0x44: "00007c424242424242427c0000000000",
    0x45: "00007e4040407c4040407e0000000000",
    0x46: "00007e4040407c404040400000000000",
    0x47: "00003c424040404e42423c0000000000",
    0x48: "0000424242427e424242420000000000",
    0x49: "00007e181818181818187e0000000000",
    0x4A: "00000e020202020242423c0000000000",
    0x4B: "00004244485060504844420000000000",
    0x4C: "000040404040404040407e0000000000",
    0x4D: "000042665a5a42424242420000000000",
    0x4E: "0000426252524a4a4642420000000000",
    0x4F: "00003c424242424242423c0000000000",
    0x50: "00007c4242427c404040400000000000",
    0x51: "00003c42424242424a443a0000000000",
    0x52: "00007c4242427c504844420000000000",
    0x53: "00003c4240403c0202423c0000000000",
    0x54: "00007e18181818181818180000000000",
    0x55: "000042424242424242423c0000000000",
    0x56: "00004242424242242418180000000000",
    0x57: "000042424242425a6642420000000000",
    0x58: "00004242242418242442420000000000",
    0x59: "00004242242418181818180000000000",
    0x5A: "00007e020204081020407e0000000000",
    0x5B: "00003c202020202020203c0000000000",
    0x5C: "00004020201008040402020000000000",
    0x5D: "00003c040404040404043c0000000000",
    0x5E: "00001824420000000000000000000000",
    0x5F: "000000000000000000000000007e0000",
    0x60: "00002010000000000000000000000000",
    0x61: "000000003c023e4242423c0000000000",
    0x62: "00004040407c424242427c0000000000",
    0x63: "000000003c42404040423c0000000000",
    0x64: "00000202023e424242423e0000000000",
    0x65: "000000003c42427e40423c0000000000",
    0x66: "00001c24207820202020200000000000",
    0x67: "000000003e4242423e0202423c000000",
    0x68: "00004040407c42424242420000000000",
    0x69: "00001800181818181818180000000000",
    0x6A: "00000c000c0c0c0c0c0c0c4c30000000",
    0x6B: "00004040404244487048440000000000",
    0x6C: "000018181818181818181e0000000000",
    0x6D: "00000000547c54545454540000000000",
    0x6E: "000000007c4242424242420000000000",
    0x6F: "000000003c42424242423c0000000000",
    0x70: "000000007c424242427c404040000000",
    0x71: "000000003e424242423e020202000000",
    0x72: "000000005c6240404040400000000000",
    0x73: "000000003c42403c02423c0000000000",
    0x74: "000000202078202020201c0000000000",
    0x75: "000000004242424242423e0000000000",
    0x76: "00000000424242422424180000000000",
    0x77: "00000000424242425a66420000000000",
    0x78: "00000000422418181824420000000000",
    0x79: "00000000424242241818181870000000",
    0x7A: "000000007e04081020407e0000000000",
    0x7B: "00000e101010301010100e0000000000",
    0x7C: "00181818181818181818181818180000",
    0x7D: "0000700808080c080808700000000000",
    0x7E: "00000000324c00000000000000000000",
}

#: 0x7F is rendered as a solid block (handy as a cursor / test pattern).
_SOLID_BLOCK_HEX = "ff" * 16

#: Fallback glyph for codes with no bitmap: a 1-pixel hollow box, rows 1..14.
_HOLLOW_BOX_HEX = "00fe828282828282828282828282fe00"

#: Leftmost pixel first: bit 7 down to bit 0.
_MASKS: tuple[int, ...] = (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01)


def _parse_glyph(hex_rows: str) -> tuple[int, ...]:
    """Turn a 32-hex-digit string into 16 per-row bitmaps."""
    if len(hex_rows) != FONT_HEIGHT * 2:
        raise ValueError("glyph must be %d hex digits" % (FONT_HEIGHT * 2))
    return tuple(int(hex_rows[i:i + 2], 16) for i in range(0, len(hex_rows), 2))


def _build_glyph_table() -> tuple[tuple[int, ...], ...]:
    """Expand the compact font table into 256 ready-to-use glyph bitmaps."""
    blank = _parse_glyph(_FONT_HEX[0x20])
    block = _parse_glyph(_SOLID_BLOCK_HEX)
    hollow = _parse_glyph(_HOLLOW_BOX_HEX)
    table: list[tuple[int, ...]] = []
    for code in range(256):
        if code in (0x00, 0x20):
            table.append(blank)          # NUL and space render blank
        elif code == 0x7F:
            table.append(block)          # DEL renders as a solid block
        elif code in _FONT_HEX:
            table.append(_parse_glyph(_FONT_HEX[code]))
        else:
            table.append(hollow)         # unknown code: hollow box
    return tuple(table)


#: Index = character code (0..255), value = 16 row bitmaps.
_GLYPHS: tuple[tuple[int, ...], ...] = _build_glyph_table()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def glyph(char_code: int) -> list[int]:
    """Return the 16 row bitmaps of ``char_code``.

    Each element is an int whose low 8 bits are that row's pixels, bit 7 being
    the leftmost pixel of the 8-pixel-wide cell.

    Printable ASCII 0x20..0x7E uses the built-in 8x16 font, 0x00 and 0x20 are
    blank, 0x7F is a solid block, and any code outside 0..255 or without a
    bitmap (control codes 0x01..0x1F, high codes 0x80..0xFF) is drawn as a
    hollow box instead of raising.
    """
    if 0 <= char_code < len(_GLYPHS):
        return list(_GLYPHS[char_code])
    return list(_GLYPHS[0x01])  # hollow box fallback


def cell_attr_to_colours(attr: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Split an attribute byte into ``(foreground_rgb, background_rgb)``.

    Bits 0-3 are the foreground colour index and bits 4-6 the background
    colour index.  Bit 7 (blink) is honoured by *brightening the background*:
    when it is set, 8 is added to the background index so the cell uses one of
    the bright palette entries (this is also the usual "intense background"
    behaviour of VGA hardware when blinking is disabled).  The foreground is
    never changed by the blink bit.
    """
    foreground = CGA_PALETTE[attr & 0x0F]
    background_index = (attr >> 4) & 0x07
    if attr & 0x80:
        background_index += 8
    return foreground, CGA_PALETTE[background_index]


def cell_at(framebuffer: bytes | bytearray | memoryview, row: int, col: int) -> tuple[int, int]:
    """Return ``(char_code, attr)`` for the cell at ``row``, ``col``.

    ``row`` must be 0..24 and ``col`` 0..79; an IndexError is raised otherwise.
    """
    if not 0 <= row < ROWS:
        raise IndexError("row %d outside 0..%d" % (row, ROWS - 1))
    if not 0 <= col < COLS:
        raise IndexError("col %d outside 0..%d" % (col, COLS - 1))
    view = _as_view(framebuffer)
    offset = (row * COLS + col) * CELL_STRIDE
    return view[offset], view[offset + 1]


def render_rows(
    framebuffer: bytes | bytearray | memoryview,
    include_blink: bool = False,
) -> list[list[tuple[int, int, int]]]:
    """Render the framebuffer to RGB pixels.

    The result has :data:`ROWS` * :data:`FONT_HEIGHT` rows of
    :data:`COLS` * :data:`FONT_WIDTH` ``(r, g, b)`` tuples, i.e. 400 rows of 640
    pixels, top row first and left pixel first.

    With the default ``include_blink=False`` the blink bit is ignored (masked
    off) so the image is steady; pass ``include_blink=True`` to brighten
    backgrounds of cells whose blink bit is set.
    """
    rows: list[list[tuple[int, int, int]]] = []
    for line in _scanlines(framebuffer, include_blink):
        rows.append([
            (line[i], line[i + 1], line[i + 2]) for i in range(0, len(line), 3)
        ])
    return rows


def render_png(
    framebuffer: bytes | bytearray | memoryview,
    path: str | os.PathLike,
    scale: int = 1,
    include_blink: bool = False,
) -> pathlib.Path:
    """Write a PNG screenshot of ``framebuffer`` to ``path`` and return it.

    ``scale`` repeats every pixel into a ``scale`` x ``scale`` block, so scale 2
    produces a 1280x800 image.  The file is an 8-bit truecolour (colour type 2),
    non-interlaced PNG built here with :mod:`zlib` and :mod:`struct`.
    """
    target = pathlib.Path(path)
    target.write_bytes(png_bytes(framebuffer, scale=scale, include_blink=include_blink))
    return target


def png_bytes(
    framebuffer: bytes | bytearray | memoryview,
    scale: int = 1,
    include_blink: bool = False,
) -> bytes:
    """Return the PNG screenshot of ``framebuffer`` as bytes (used by tests)."""
    scale = _check_scale(scale)
    lines = _scanlines(framebuffer, include_blink)
    if scale != 1:
        lines = _scale_scanlines(lines, scale)

    raw = bytearray()
    for line in lines:
        raw.append(0)  # PNG filter type 0 (None) for each scanline
        raw += line

    width = COLS * FONT_WIDTH * scale
    height = ROWS * FONT_HEIGHT * scale
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def text_rows(framebuffer: bytes | bytearray | memoryview, rstrip: bool = True) -> list[str]:
    """Return the 25 display lines as text.

    Codes 0x20..0x7E are kept verbatim; every other code (NUL, control codes,
    0x7F and the high half) becomes a space.  With ``rstrip=True`` (the default)
    trailing spaces are removed from each line.
    """
    view = _as_view(framebuffer)
    lines: list[str] = []
    for row in range(ROWS):
        start = row * COLS * CELL_STRIDE
        chars = [
            chr(code) if 0x20 <= code <= 0x7E else " "
            for code in view[start:start + COLS * CELL_STRIDE:CELL_STRIDE]
        ]
        line = "".join(chars)
        lines.append(line.rstrip(" ") if rstrip else line)
    return lines


def text(framebuffer: bytes | bytearray | memoryview, rstrip: bool = True) -> str:
    """Return the screen as a string: :func:`text_rows` joined by newlines.

    Trailing blank lines are removed, so a screen with text only in the first
    two rows yields two lines.  Attributes are not part of the text.
    """
    lines = text_rows(framebuffer, rstrip=rstrip)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def text_hash(framebuffer: bytes | bytearray | memoryview) -> str:
    """SHA-256 hex digest of :func:`text` (default ``rstrip=True``) as ASCII.

    Stable across font/colour changes and meant for text regression tests; use
    :func:`image_hash` when pixels or colours matter.
    """
    return hashlib.sha256(text(framebuffer).encode("ascii")).hexdigest()


def image_hash(framebuffer: bytes | bytearray | memoryview, scale: int = 1) -> str:
    """SHA-256 hex digest of the rendered PNG bytes at ``scale``.

    Blink is ignored (``include_blink=False``), matching the default of
    :func:`png_bytes`.
    """
    return hashlib.sha256(png_bytes(framebuffer, scale=scale)).hexdigest()


# ---------------------------------------------------------------------------
# Implementation details
# ---------------------------------------------------------------------------

#: The 8-byte PNG file signature.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _as_view(framebuffer: bytes | bytearray | memoryview) -> memoryview:
    """Return an unsigned-byte, 1-D memoryview over ``framebuffer``.

    Any object supporting the buffer protocol is accepted (bytes, bytearray,
    memoryview, array.array, mmap, ...).  A ValueError naming the actual length
    is raised when the buffer is shorter than one full screen.
    """
    try:
        view = memoryview(framebuffer)
    except TypeError as exc:  # not a buffer at all
        raise TypeError(
            "framebuffer must support the buffer protocol (bytes, bytearray, "
            "memoryview, ...), got %s" % type(framebuffer).__name__
        ) from exc
    if view.ndim != 1 or view.itemsize != 1 or view.format != "B":
        try:
            view = view.cast("B")
        except (TypeError, ValueError):
            view = memoryview(bytes(framebuffer))
    if view.nbytes < VIDEO_SIZE:
        raise ValueError(
            "framebuffer must be at least %d bytes (%dx%d cells x %d), got %d"
            % (VIDEO_SIZE, COLS, ROWS, CELL_STRIDE, view.nbytes)
        )
    return view


def _check_scale(scale: int) -> int:
    """Validate the pixel scale factor."""
    if not isinstance(scale, int) or isinstance(scale, bool):
        raise TypeError("scale must be an int, got %s" % type(scale).__name__)
    if scale < 1:
        raise ValueError("scale must be >= 1, got %d" % scale)
    return scale


def _pixel_pattern(fg: bytes, bg: bytes) -> tuple[bytes, ...]:
    """24-byte pixel runs for all 256 possible glyph rows of one colour pair."""
    # Computed once per (foreground, background) pair actually used on screen,
    # which keeps the per-cell inner loop down to a single slice assignment.
    patterns: list[bytes] = []
    for bits in range(256):
        chunk = bytearray(FONT_WIDTH * 3)
        pos = 0
        for mask in _MASKS:
            chunk[pos:pos + 3] = fg if bits & mask else bg
            pos += 3
        patterns.append(bytes(chunk))
    return tuple(patterns)


def _scanlines(
    framebuffer: bytes | bytearray | memoryview,
    include_blink: bool,
) -> list[bytes]:
    """Render to PNG-ready scanlines: 400 rows of 640 * 3 bytes each."""
    view = _as_view(framebuffer)
    patterns_cache: dict[tuple[bytes, bytes], tuple[bytes, ...]] = {}
    cells: list[tuple[tuple[int, ...], tuple[bytes, ...]]] = []
    for offset in range(0, VIDEO_SIZE, CELL_STRIDE):
        code = view[offset]
        attr = view[offset + 1]
        if not include_blink:
            attr &= 0x7F
        fg_rgb, bg_rgb = cell_attr_to_colours(attr)
        key = (bytes(fg_rgb), bytes(bg_rgb))
        patterns = patterns_cache.get(key)
        if patterns is None:
            patterns = _pixel_pattern(key[0], key[1])
            patterns_cache[key] = patterns
        cells.append((_GLYPHS[code], patterns))

    lines: list[bytes] = []
    for row in range(ROWS):
        base = row * COLS
        for glyph_row in range(FONT_HEIGHT):
            line = bytearray(COLS * FONT_WIDTH * 3)
            pos = 0
            for col in range(COLS):
                glyph_rows, patterns = cells[base + col]
                chunk = patterns[glyph_rows[glyph_row]]
                line[pos:pos + FONT_WIDTH * 3] = chunk
                pos += FONT_WIDTH * 3
            lines.append(bytes(line))
    return lines


def _scale_scanlines(lines: list[bytes], scale: int) -> list[bytes]:
    """Widen each pixel by ``scale`` horizontally and repeat rows vertically."""
    scaled: list[bytes] = []
    for line in lines:
        wide = bytearray()
        for i in range(0, len(line), 3):
            wide += line[i:i + 3] * scale
        row = bytes(wide)
        for _ in range(scale):
            scaled.append(row)
    return scaled


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """Build one PNG chunk: length, type, data, CRC32 over type + data."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


__all__ = [
    "VIDEO_BASE", "COLS", "ROWS", "CELL_STRIDE", "VIDEO_SIZE", "CGA_PALETTE",
    "FONT_WIDTH", "FONT_HEIGHT", "PNG_SIGNATURE",
    "glyph", "cell_attr_to_colours", "cell_at", "render_rows", "render_png",
    "png_bytes", "text_rows", "text", "text_hash", "image_hash",
]
