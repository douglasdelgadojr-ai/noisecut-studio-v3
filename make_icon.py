"""Generate the NoiseCut Studio application icons. Requires Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'assets'
SIZE = 512


def make_icon():
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, 496, 496), radius=104, fill='#1e3a8a')

    center_y = 256
    bar_width = 27
    gap = 22
    heights = [72, 130, 190, 112, 250, 170, 218, 126, 78]
    total_width = len(heights) * bar_width + (len(heights) - 1) * gap
    left = (SIZE - total_width) // 2
    for index, height in enumerate(heights):
        x = left + index * (bar_width + gap)
        top = center_y - height // 2
        bottom = center_y + height // 2
        draw.rounded_rectangle((x, top, x + bar_width, bottom), radius=bar_width // 2, fill='#ffffff')

    # A clean diagonal cut through the sound wave.
    draw.line((184, 336, 330, 190), fill='#7dd3fc', width=24)
    r = 12
    for x, y in ((184, 336), (330, 190)):
        draw.ellipse((x - r, y - r, x + r, y + r), fill='#7dd3fc')

    png = ASSETS / 'icon.png'
    ico = ASSETS / 'icon.ico'
    image.save(png, format='PNG', optimize=True)
    image.save(ico, format='ICO', sizes=[(n, n) for n in (16, 32, 48, 64, 128, 256)])
    print(f'Iconos creados: {png} y {ico}')


if __name__ == '__main__':
    make_icon()
