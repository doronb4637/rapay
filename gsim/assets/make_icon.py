"""
Render the GSim badge to `gsim.ico` (window/taskbar icon) and `gsim.png`
(source image, also handy for docs/README).

Pure stdlib on purpose: Pillow is not a dependency of this project, and adding
one just to draw a handful of polygons would be a poor trade. This rasterises
the same geometry as `web/src/components/Logo.jsx` (hex shield, broadcast arcs,
isometric cube) with 3x supersampling.

The .ico must be a REAL Windows icon container, not a renamed PNG. pywebview's
WinForms backend calls `System.Drawing.Icon(path)` directly (see
`webview/platforms/winforms.py`), and GDI+'s `Icon` loader on classic .NET
Framework does not accept a bare PNG or a PNG-compressed ICO frame -- only the
legacy uncompressed BMP-in-ICO format, which is what `write_ico` below hand-
assembles (ICONDIR + BITMAPINFOHEADER + top-down BGRA pixels + a 1bpp AND
mask). Skipping this and just wrapping raw PNG bytes in an ICONDIR is what
produced "Argument 'picture' must be a picture that can be used as an Icon."

Re-run after changing the mark:  python gsim/assets/make_icon.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).parent
ICO_SIZES = (24, 32, 48, 64, 128, 256)   # standard Windows icon sizes
PNG_SIZE = 256
SS = 3                      # supersampling factor per axis

# Matches the app's slate/emerald palette.
BG = (15, 23, 42, 255)          # slate-950
HEX = (52, 211, 153, 190)       # emerald-400, badge ring + arcs
CUBE_TOP = (167, 243, 208, 245)  # emerald-200
CUBE_LEFT = (110, 231, 183, 190)  # emerald-300
CUBE_RIGHT = (52, 211, 153, 130)  # emerald-400


def hexagon(cx: float, cy: float, w: float, h: float) -> list[tuple[float, float]]:
    """Flat-top-ish shield: the same 6 points the SVG path uses."""
    return [
        (cx, cy - h),
        (cx + w, cy - h * 0.45),
        (cx + w, cy + h * 0.45),
        (cx, cy + h),
        (cx - w, cy + h * 0.45),
        (cx - w, cy - h * 0.45),
    ]


def inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Even-odd ray cast."""
    x, y = point
    result = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            result = not result
        j = i
    return result


def on_arc(point: tuple[float, float], cx: float, cy: float,
           radius: float, thickness: float, spread: float) -> bool:
    """A band of an upward-opening circular arc, used for the signal waves."""
    x, y = point
    dx, dy = x - cx, y - cy
    distance = math.hypot(dx, dy)
    if abs(distance - radius) > thickness / 2:
        return False
    angle = math.atan2(dy, dx)          # -pi..pi; -pi/2 is straight up
    return abs(angle + math.pi / 2) < spread


def build(size: int, supersample: int = SS) -> list[list[tuple[int, int, int, int]]]:
    """Rasterise the mark at `size`x`size`, returned as rows of RGBA tuples."""
    unit = size / 64.0                  # the SVG is authored on a 64x64 grid

    outer = hexagon(size / 2, size / 2, 26 * unit, 30 * unit)
    inner = hexagon(size / 2, size / 2, 22.5 * unit, 26 * unit)

    top = [(32, 31), (43, 37.2), (32, 43.5), (21, 37.2)]
    left = [(21, 37.2), (32, 43.5), (32, 56), (21, 49.7)]
    right = [(43, 37.2), (32, 43.5), (32, 56), (43, 49.7)]
    scale = lambda pts: [(px * unit, py * unit) for px, py in pts]  # noqa: E731
    top, left, right = scale(top), scale(left), scale(right)

    arc_cx, arc_cy = 32 * unit, 41 * unit

    pixels = [[BG] * size for _ in range(size)]
    step = 1.0 / supersample
    samples = supersample * supersample

    for py in range(size):
        row = pixels[py]
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(supersample):
                for sx in range(supersample):
                    point = (px + (sx + 0.5) * step, py + (sy + 0.5) * step)
                    colour = BG
                    if inside(point, outer) and not inside(point, inner):
                        colour = HEX
                    elif on_arc(point, arc_cx, arc_cy, 17 * unit, 2.6 * unit, 0.85):
                        colour = (HEX[0], HEX[1], HEX[2], 115)
                    elif on_arc(point, arc_cx, arc_cy, 9.5 * unit, 2.6 * unit, 0.95):
                        colour = HEX
                    # Cube last so it sits above the arcs.
                    if inside(point, top):
                        colour = CUBE_TOP
                    elif inside(point, left):
                        colour = CUBE_LEFT
                    elif inside(point, right):
                        colour = CUBE_RIGHT
                    for channel in range(4):
                        acc[channel] += colour[channel]
            row[px] = tuple(int(value / samples) for value in acc)  # type: ignore[assignment]
    return pixels


def png_bytes(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    size = len(pixels)
    raw = bytearray()
    for row in pixels:
        raw.append(0)                   # filter type 0 (None) per scanline
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _bmp_frame(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """One ICONIMAGE: BITMAPINFOHEADER + top-down 32bpp BGRA + a (unused but
    format-required) 1bpp AND mask, all zero since alpha already carries
    transparency. This is the legacy uncompressed form every GDI+ Icon loader
    accepts, unlike PNG-compressed ICO frames."""
    size = len(pixels)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,           # biSize
        size,         # biWidth
        size * 2,     # biHeight (XOR + AND, per ICO convention)
        1,            # biPlanes
        32,           # biBitCount
        0,            # biCompression (BI_RGB)
        size * size * 4,  # biSizeImage
        0, 0,         # biXPelsPerMeter, biYPelsPerMeter
        0, 0,         # biClrUsed, biClrImportant
    )

    # Negative biHeight-equivalent isn't available for icon DIBs (must be
    # positive / bottom-up per spec), so store rows bottom-to-top.
    xor = bytearray()
    for row in reversed(pixels):
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))   # BGRA

    and_row_bytes = ((size + 31) // 32) * 4   # rows padded to a 4-byte boundary
    and_mask = bytes(and_row_bytes * size)     # all zero: nothing masked out

    return header + bytes(xor) + and_mask


def write_ico(sizes: tuple[int, ...], path: Path) -> None:
    frames = [(size, _bmp_frame(build(size))) for size in sizes]

    entry_count = len(frames)
    header = struct.pack("<HHH", 0, 1, entry_count)   # reserved, type=icon, count

    directory = bytearray()
    data = bytearray()
    offset = 6 + entry_count * 16   # ICONDIR + ICONDIRENTRY table

    for size, frame in frames:
        wh = size if size < 256 else 0    # ICO stores 256 as 0 (1-byte field)
        directory += struct.pack(
            "<BBBBHHII",
            wh, wh,       # width, height
            0, 0,         # colour count, reserved
            1, 32,        # planes, bit count
            len(frame), offset,
        )
        data += frame
        offset += len(frame)

    path.write_bytes(header + bytes(directory) + bytes(data))


if __name__ == "__main__":
    png_path = OUT_DIR / "gsim.png"
    ico_path = OUT_DIR / "gsim.ico"

    png_path.write_bytes(png_bytes(build(PNG_SIZE)))
    print(f"wrote {png_path} ({png_path.stat().st_size} bytes, {PNG_SIZE}x{PNG_SIZE})")

    write_ico(ICO_SIZES, ico_path)
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes, sizes={ICO_SIZES})")
