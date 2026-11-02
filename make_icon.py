"""Draws the app icon (icon.ico + icon.png): Apple squircle, Nothing dot-matrix "B" with one red dot."""

from PIL import Image, ImageDraw

S = 1024  # draw large, then shrink for smooth edges
B = "1E11111E11111E"  # 5x7 glyph, same font as the dashboard intro
RED = (215, 25, 33, 255)  # Nothing red, as in the app


def squircle_mask(size, inset=0, n=5.0):
    m = Image.new("L", (size, size), 0)
    px = m.load()
    r = size / 2 - inset
    c = size / 2
    for y in range(size):
        for x in range(size):
            if abs((x - c) / r) ** n + abs((y - c) / r) ** n <= 1:
                px[x, y] = 255
    return m


def draw(size, faint_grid=True):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    pitch = size * 0.092
    # glyph is 5 wide plus a gap and the red dot = 7 columns; 7 rows tall
    x0 = (size - 7 * pitch) / 2
    y0 = (size - 7 * pitch) / 2

    def dot(cx, cy, r, fill):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)

    if faint_grid:  # the dim matrix behind, like an unlit Nothing display
        for gy in range(-1, 9):
            for gx in range(-1, 9):
                dot(x0 + (gx + 0.5) * pitch, y0 + (gy + 0.5) * pitch, pitch * 0.09, (38, 38, 38, 255))
    for r in range(7):
        bits = int(B[r * 2:r * 2 + 2], 16)
        for c in range(5):
            if bits & (16 >> c):
                dot(x0 + (c + 0.5) * pitch, y0 + (r + 0.5) * pitch, pitch * 0.38, (245, 245, 245, 255))
    dot(x0 + 6.5 * pitch, y0 + 6.5 * pitch, pitch * 0.38, RED)  # the full stop: "B."

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(img, (0, 0), squircle_mask(size, inset=int(size * 0.04)))
    return icon


big = draw(S)
big.resize((512, 512), Image.LANCZOS).save("icon.png")
# tiny sizes skip the faint grid so the B stays readable in the taskbar
small = draw(S, faint_grid=False)
frames = {n: (big if n >= 48 else small).resize((n, n), Image.LANCZOS) for n in (16, 24, 32, 48, 64, 128, 256)}
frames[256].save("icon.ico", sizes=[(n, n) for n in frames], append_images=[frames[n] for n in frames if n != 256])
print("icon.ico and icon.png written")
