"""
PWA 아이콘 생성 — MJT 모노그램 (골드 on 네이비)
실행: py -3.14 _gen_icons.py
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(BASE, 'static', 'icons')
os.makedirs(ICONS_DIR, exist_ok=True)

NAVY  = (15, 23, 42)        # #0F172A
NAVY2 = (30, 41, 59)        # #1E293B
GOLD  = (252, 211, 77)      # #FCD34D
GOLD2 = (245, 158, 11)      # #F59E0B
WHITE = (255, 255, 255)


def _find_font(size):
    """굵은 산세리프 폰트 찾기."""
    candidates = [
        r'C:\Windows\Fonts\malgunbd.ttf',     # 맑은 고딕 Bold
        r'C:\Windows\Fonts\arialbd.ttf',
        r'C:\Windows\Fonts\segoeuib.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_icon(size: int, fname: str, maskable: bool = False):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # maskable: 화면 잘림 대비 safe zone 80% 사용 (Android adaptive icon)
    if maskable:
        # 배경 정사각형 (전체)
        d.rectangle([0, 0, size, size], fill=NAVY)
        inner_pad = int(size * 0.10)
    else:
        # 배경 둥근 사각형 (전체)
        radius = int(size * 0.22)
        d.rounded_rectangle([0, 0, size, size], radius=radius, fill=NAVY)
        inner_pad = int(size * 0.12)

    # 골드 그라데이션 카드 (중앙)
    card_size = size - 2 * inner_pad
    card_x0 = inner_pad
    card_y0 = inner_pad + int(size * 0.02)
    card_x1 = card_x0 + card_size
    card_y1 = card_y0 + card_size - int(size * 0.04)
    card_radius = int(card_size * 0.18)

    # 골드 그라데이션 효과 (위→아래)
    grad = Image.new('RGBA', (card_size, card_size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(card_size):
        t = y / card_size
        r = int(GOLD[0] * (1 - t) + GOLD2[0] * t)
        g = int(GOLD[1] * (1 - t) + GOLD2[1] * t)
        b = int(GOLD[2] * (1 - t) + GOLD2[2] * t)
        gd.line([(0, y), (card_size, y)], fill=(r, g, b))

    # 라운드 마스크
    mask = Image.new('L', (card_size, card_size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, card_size, card_size], radius=card_radius, fill=255)
    img.paste(grad, (card_x0, card_y0), mask)

    # "MJT" 텍스트 중앙
    font_size = int(card_size * 0.42)
    font = _find_font(font_size)
    text = 'MJT'
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = card_x0 + (card_size - tw) // 2 - bbox[0]
    ty = card_y0 + (card_size - th) // 2 - bbox[1] - int(card_size * 0.04)
    # 텍스트 그림자 (살짝)
    d.text((tx + 2, ty + 2), text, font=font, fill=(0, 0, 0, 80))
    d.text((tx, ty), text, font=font, fill=NAVY)

    # 하단 미니 라벨 "SmartFactory"
    sub_font_size = max(8, int(card_size * 0.075))
    sub_font = _find_font(sub_font_size)
    sub = 'SmartFactory'
    sbbox = d.textbbox((0, 0), sub, font=sub_font)
    sw_ = sbbox[2] - sbbox[0]
    sx = card_x0 + (card_size - sw_) // 2 - sbbox[0]
    sy = card_y1 - int(card_size * 0.14)
    d.text((sx, sy), sub, font=sub_font, fill=NAVY)

    out = os.path.join(ICONS_DIR, fname)
    img.save(out, 'PNG')
    print(f'  ✓ {fname}  ({size}x{size}, {os.path.getsize(out)/1024:.0f}KB)')


def main():
    import sys
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass
    print('=' * 50)
    print('MJT PWA 아이콘 생성')
    print(f'출력: {ICONS_DIR}')
    print('=' * 50)
    make_icon(192, 'icon-192.png')
    make_icon(512, 'icon-512.png')
    make_icon(512, 'icon-maskable-512.png', maskable=True)
    # iOS Apple Touch Icon
    make_icon(180, 'apple-touch-icon.png')
    # 파비콘
    make_icon(64,  'favicon-64.png')
    print('=' * 50)


if __name__ == '__main__':
    main()
