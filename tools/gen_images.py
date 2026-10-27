#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор кадров металлопроката «под фотографию» (raw-рендер на PIL).

Принцип: каждый кадр рисуется в 2x разрешении и даунскейлится LANCZOS —
это даёт мягкую объективную детализацию вместо плоской мультяшной графики.
Добавляются: градиентный фон цеха, тёплый контровой свет, цилиндрический
блик на металле, следы окалины/ржавчины, зерно и виньетка.

Запуск из корня проекта:
    python3 tools/gen_images.py                 # все кадры
    python3 tools/gen_images.py hero adv-cut    # выборочно

Результат: assets/img/*.jpg (1200x800, progressive JPEG)
"""
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageOps

W, H = 1200, 800
SS = 2                                   # суперсэмплинг
ACCENT = (255, 122, 26)
OUT = os.path.join("assets", "img")


# ---------------------------------------------------------------- helpers ----
def canvas(top=(34, 40, 47), bottom=(9, 11, 14), warm=None):
    """Вертикальный градиент + тёплое пятно контрового света."""
    bw, bh = W * SS, H * SS
    ramp = Image.linear_gradient("L").resize((bw, bh))
    base = Image.new("RGB", (bw, bh), top)
    bot = Image.new("RGB", (bw, bh), bottom)
    base.paste(bot, (0, 0), ramp.point(lambda v: 255 - v))
    if warm:
        cx, cy, r, col = warm
        glow = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(glow).ellipse([cx - r, cy - r, cx + r, cy + r], fill=120)
        glow = glow.filter(ImageFilter.GaussianBlur(r * 0.6)).point(lambda v: int(v * 0.45))
        base = Image.composite(Image.new("RGB", (bw, bh), col), base, glow)
    return base


def grain(im, n=2600, alpha=16, seed=3):
    d = ImageDraw.Draw(im, "RGBA")
    rnd = random.Random(seed)
    w, h = im.size
    for _ in range(n):
        x, y = rnd.randint(0, w - 1), rnd.randint(0, h - 1)
        r = rnd.choice([1, 1, 2])
        c = (255, 255, 255) if rnd.random() > 0.45 else (10, 12, 16)
        d.ellipse([x, y, x + r, y + r], fill=c + (rnd.randint(4, alpha),))
    return im


def finish(im):
    """Даунскейл → виньетка → микроконтраст."""
    im = im.resize((W, H), Image.LANCZOS)
    vm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vm).ellipse([-W * .22, -H * .3, W * 1.22, H * 1.3], fill=255)
    vm = vm.filter(ImageFilter.GaussianBlur(130))
    im = Image.composite(im, Image.new("RGB", (W, H), (5, 7, 9)), vm)
    return ImageOps.autocontrast(im.convert("RGB"), cutoff=1)


def save(im, name, quality=82):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    im.save(p, "JPEG", quality=quality, optimize=True, progressive=True)
    print(f"  \u2713 {name}  {os.path.getsize(p) // 1024} KB")


# ------------------------------------------------------------ metal shapes ---
def paste_rotated(img, tile, cx, cy):
    img.paste(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)), tile)


def bar(img, d, x0, y0, x1, y1, width, hi=(196, 203, 212), lo=(52, 58, 66),
        rust=0.0, seed=1):
    """Пруток/труба: полоса с профилем яркости цилиндра и продольным бликом."""
    ang = math.atan2(y1 - y0, x1 - x0)
    length = int(math.hypot(x1 - x0, y1 - y0)) + 4
    width = int(width)
    tile = Image.new("RGBA", (length, width + 4), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    rnd = random.Random(seed)
    for i in range(width):
        t = i / max(width - 1, 1)
        s = math.sin(math.pi * min(max(t * 1.18, 0.0), 1.0)) ** 0.72
        c = tuple(int(lo[k] + (hi[k] - lo[k]) * s) for k in range(3))
        j = rnd.randint(-7, 7)
        c = tuple(max(0, min(255, v + j)) for v in c)
        td.line([(0, i + 2), (length, i + 2)], fill=c + (255,), width=1)
    td.line([(2, 4), (length - 2, 4)], fill=(240, 246, 252, 190), width=max(2, width // 12))
    td.line([(2, width - 1), (length - 2, width - 1)], fill=(18, 21, 26, 190), width=max(2, width // 14))
    if rust:
        for _ in range(int(length * width * rust / 700)):
            rx, ry = rnd.randint(0, length - 1), rnd.randint(0, width - 1)
            rr = rnd.randint(2, 9)
            td.ellipse([rx, ry, rx + rr, ry + rr],
                       fill=(rnd.randint(118, 168), rnd.randint(60, 90), rnd.randint(28, 48),
                             rnd.randint(25, 90)))
    tile = tile.rotate(-math.degrees(ang), expand=True, resample=Image.BICUBIC)
    paste_rotated(img, tile, (x0 + x1) / 2, (y0 + y1) / 2)


def rebar(img, d, x0, y0, x1, y1, width, seed=1):
    """Арматура А500С: периодический профиль — кольцевые насечки."""
    bar(img, d, x0, y0, x1, y1, width, hi=(188, 196, 206), lo=(48, 54, 62), seed=seed)
    ang = math.atan2(y1 - y0, x1 - x0)
    length = math.hypot(x1 - x0, y1 - y0)
    rnd = random.Random(seed + 50)
    step = max(16, int(width * 0.9))
    for i in range(int(length / step)):
        along = (i + rnd.uniform(.25, .75)) * step
        px = x0 + math.cos(ang) * along
        py = y0 + math.sin(ang) * along
        off = rnd.uniform(-width * .16, width * .16)
        nx, ny = math.cos(ang + math.pi / 2), math.sin(ang + math.pi / 2)
        cxp, cyp = px + nx * off, py + ny * off
        ln = width * 0.95
        a2 = ang + 1.05
        d.line([(cxp - math.cos(a2) * ln / 2, cyp - math.sin(a2) * ln / 2),
                (cxp + math.cos(a2) * ln / 2, cyp + math.sin(a2) * ln / 2)],
               fill=(26, 30, 36, 160), width=max(3, int(width * .17)))
        d.line([(cxp - math.cos(a2) * ln / 2, cyp - math.sin(a2) * ln / 2 - 3),
                (cxp + math.cos(a2) * ln / 2, cyp + math.sin(a2) * ln / 2 - 3)],
               fill=(228, 236, 244, 130), width=max(1, int(width * .08)))


def tube_end(d, cx, cy, r, wall=.18, steel=(150, 158, 168)):
    """Торец трубы: фаска, кольцо стенки, тёмное нутро."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=steel)
    d.ellipse([cx - r * .95, cy - r * .95, cx + r * .95, cy + r * .95],
              fill=tuple(int(v * .8) for v in steel))
    ri = r * (1 - wall)
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=(13, 16, 20))
    d.arc([cx - r, cy - r, cx + r, cy + r], 195, 345,
          fill=(236, 244, 250, 200), width=max(2, int(r * .08)))
    d.arc([cx - ri, cy - ri, cx + ri, cy + ri], 15, 175,
          fill=(96, 104, 114, 190), width=max(2, int(r * .05)))


def beam_profile(img, d, cx, cy, hh, bf, tw, tf, rot, depth,
                 face=(126, 134, 144), edge=(66, 72, 80)):
    """Двутавр: I-профиль торца + экструзия в глубину кадра."""
    ca, sa = math.cos(rot), math.sin(rot)
    dx, dy = depth * .30, -depth * .34

    def pts(shift=(0, 0)):
        p = [(-tw / 2, -hh), (bf, -hh), (bf, -hh + tf), (tw / 2, -hh + tf),
             (tw / 2, hh - tf), (bf, hh - tf), (bf, hh), (-tw / 2, hh),
             (-tw / 2, hh - tf), (-bf, hh - tf), (-bf, hh), (-bf, hh - tf),
             (-tw / 2, hh - tf)]
        return [(cx + x * ca - y * sa + shift[0], cy + x * sa * .5 + y * ca * .82 + shift[1])
                for x, y in p]

    front, back = pts(), pts(shift=(dx, dy))
    d.polygon(back, fill=edge)
    for i in range(len(front) - 1):
        mid = tuple(int((face[k] + edge[k]) / 2 * .93) for k in range(3))
        d.polygon([front[i], front[i + 1], back[i + 1], back[i]], fill=mid)
    d.polygon(front, fill=face)
    d.polygon(front, outline=tuple(int(v * .55) for v in face), width=2)
    d.line([front[0], front[1]], fill=(238, 244, 250, 170), width=4)
    d.line([front[7], front[8]], fill=(232, 238, 246, 140), width=3)


def sheet_stack(d, x, y, n, w, t, lean=6, col=(128, 136, 146)):
    """Штабель листов: видимая кромка каждого листа."""
    for i in range(n):
        yy = y + i * (t + 2)
        sh = i * lean
        d.polygon([(x + sh, yy), (x + w + sh, yy - 4), (x + w + sh + 26, yy + 8),
                   (x + sh + 26, yy + t + 8)], fill=tuple(int(v * .62) for v in col))
        d.rectangle([x + sh, yy, x + w + sh, yy + t], fill=col)
        d.line([(x + sh, yy), (x + w + sh, yy)], fill=(228, 234, 242, 190), width=2)
        d.line([(x + sh, yy + t), (x + w + sh, yy + t)], fill=(38, 43, 50, 210), width=2)


def sparks(d, cx, cy, rnd, count=120):
    for _ in range(count):
        a = rnd.uniform(-math.pi, math.pi)
        r = (rnd.uniform(0, 260) ** .85) * 3.1
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r * .72
        s = rnd.uniform(1.4, 5)
        c = rnd.choice([(255, 238, 186), (255, 198, 98), ACCENT, (255, 255, 238)])
        al = rnd.randint(120, 255)
        d.ellipse([x, y, x + s, y + s], fill=c + (al,))
        if rnd.random() < .4:
            d.line([(x, y), (x + math.cos(a) * r * .14, y + math.sin(a) * r * .1)],
                   fill=c + (int(al * .45),), width=2)


def floor_plane(d, y0=650, y1=610, col=(24, 28, 33)):
    d.polygon([(0, y0), (W, y1), (W, H), (0, H)], fill=col)


def racks_bg(d, x0, y0, cols, rows, bay_w=250, bay_h=470, steel=(96, 104, 114), r=15):
    """Складские стеллажи с трубами в торцевом сечении."""
    for bay in range(cols):
        x = x0 + bay * (bay_w + 50)
        d.rectangle([x, y0, x + bay_w, y0 + bay_h], fill=(25, 29, 35))
        for lvl in range(rows):
            y = y0 + 40 + lvl * (bay_h - 60) / rows
            d.rectangle([x + 6, y, x + bay_w - 6, y + 11], fill=(50, 56, 64))
            n = int((bay_w - 40) / (r * 2.2))
            for i in range(n):
                tube_end(d, x + 26 + i * r * 2.2, y - r * 1.5, r, steel=steel)


# ------------------------------------------------------------------ frames ---
def frame_hero():
    """Главный экран: интерьер металлобазы, свет из ворот, связки арматуры."""
    im = canvas(top=(46, 54, 66), bottom=(8, 10, 13), warm=(760, 30, 640, (112, 98, 72)))
    d = ImageDraw.Draw(im, "RGBA")
    for i in range(6):                                    # фермы потолка
        x = -60 + i * 240
        d.line([(x, 0), (x + 210, 160)], fill=(28, 32, 38, 210), width=11)
        d.line([(x + 210, 0), (x, 160)], fill=(28, 32, 38, 160), width=8)
    d.line([(0, 160), (W, 128)], fill=(34, 39, 46, 230), width=13)
    for cx in (250, 700, 1090):                           # светильники
        gl = Image.new("L", im.size, 0)
        ImageDraw.Draw(gl).ellipse([cx - 130, 50, cx + 130, 210], fill=190)
        gl = gl.filter(ImageFilter.GaussianBlur(80)).point(lambda v: int(v * .38))
        im = Image.composite(Image.new("RGB", im.size, (205, 210, 216)), im, gl)
    d = ImageDraw.Draw(im, "RGBA")
    racks_bg(d, 690, 200, 3, 4, bay_w=170, bay_h=400, steel=(88, 96, 106), r=13)
    floor_plane(d, 645, 592, (20, 23, 28))
    for k, (bx, by, n, wd) in enumerate([(110, 700, 6, 34), (430, 730, 5, 40), (770, 706, 6, 32)]):
        for i in range(n):
            rebar(im, d, bx - 130 + i * 9, by - i * (wd + 6), bx + 470 + i * 9,
                  by - 130 - i * (wd + 4), wd, seed=60 + k * 12 + i)
        d.line([(bx + 50, by - 10), (bx + 320, by - 140)], fill=(192, 200, 210, 190), width=8)
    beam_profile(im, d, 980, 520, 70, 110, 20, 16, -.10, 300, face=(104, 112, 122), edge=(56, 62, 70))
    save(finish(grain(im, 3400, 13, 17)), "hero-steel.jpg", 84)


def frame_about():
    """О компании: аэровид металлобазы — штабеля, козловой кран, техника."""
    im = canvas(top=(52, 62, 76), bottom=(10, 12, 16), warm=(400, 60, 540, (98, 90, 72)))
    d = ImageDraw.Draw(im, "RGBA")
    d.polygon([(0, 420), (W, 358), (W, H), (0, H)], fill=(34, 38, 44))
    for y in range(430, H, 46):
        d.line([(0, y), (W, y - 42)], fill=(45, 50, 58, 150), width=3)
    rnd = random.Random(21)
    for row in range(4):
        yy, sc = 300 + row * 92, 1 - row * .12
        for col in range(6):
            xx = 60 + col * 190 + row * 26
            if rnd.random() < .22:
                continue
            sheet_stack(d, xx, yy, 4, int(150 * sc), int(12 * sc), lean=4,
                        col=(108 + row * 8, 116 + row * 8, 126 + row * 8))
    d.line([(140, 118), (1060, 88)], fill=(60, 66, 74), width=18)      # крановый путь
    d.rectangle([556, 94, 662, 150], fill=(76, 82, 92))
    d.line([(610, 150), (610, 300)], fill=(152, 160, 170, 220), width=4)
    d.rectangle([574, 300, 646, 332], fill=ACCENT + (255,))
    for i in range(4):
        rebar(im, d, 580 + i * 18, 332, 586 + i * 18, 424, 14, seed=70 + i)
    d.line([(140, 118), (140, 420)], fill=(52, 58, 66), width=16)
    d.line([(1060, 88), (1060, 400)], fill=(52, 58, 66), width=16)
    d.rectangle([820, 556, 982, 630], fill=(40, 45, 52))
    d.rectangle([900, 516, 978, 562], fill=(52, 58, 66))
    d.rectangle([906, 524, 968, 558], fill=(128, 158, 186, 210))
    for wx in (850, 950):
        d.ellipse([wx - 26, 614, wx + 26, 668], fill=(16, 18, 22))
    save(finish(grain(im, 3000, 13, 19)), "about-warehouse.jpg")


def frame_cut():
    """Преимущество «Резка в размер»: дисковая пила, искры."""
    im = canvas(top=(30, 35, 42), bottom=(8, 10, 13), warm=(1040, 110, 520, (74, 54, 30)))
    d = ImageDraw.Draw(im, "RGBA")
    floor_plane(d, 645, 605, (26, 30, 36))
    for x in range(0, W, 90):
        d.line([(x, 648 - x * .033), (x + 44, H)], fill=(40, 46, 54, 160), width=6)
    beam_profile(im, d, 430, 560, 96, 150, 26, 22, -.06, 430, face=(132, 140, 150), edge=(70, 76, 84))
    beam_profile(im, d, 150, 645, 78, 122, 22, 18, .12, 180, face=(118, 126, 136), edge=(62, 68, 76))
    cx, cy, r = 640, 250, 190
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(84, 90, 98))
    d.ellipse([cx - r * .93, cy - r * .93, cx + r * .93, cy + r * .93], fill=(114, 122, 132))
    d.ellipse([cx - r * .3, cy - r * .3, cx + r * .3, cy + r * .3], fill=(50, 55, 62))
    for a in range(0, 360, 6):
        aa = math.radians(a)
        d.line([(cx + math.cos(aa) * r * .9, cy + math.sin(aa) * r * .9),
                (cx + math.cos(aa) * r, cy + math.sin(aa) * r)], fill=(152, 162, 174, 120), width=2)
    d.arc([cx - r, cy - r, cx + r, cy + r], 300, 60, fill=(236, 244, 250, 190), width=6)
    d.pieslice([cx - r - 46, cy - r - 46, cx + r + 46, cy + r + 46], 190, 350, fill=(34, 39, 46))
    d.pieslice([cx - r - 28, cy - r - 28, cx + r + 28, cy + r + 28], 190, 350, fill=(50, 56, 64))
    d.ellipse([cx - 44, cy + r - 44, cx + 44, cy + r + 44], fill=(255, 246, 222, 225))
    sparks(d, cx + 12, cy + r, random.Random(4), 170)
    save(finish(grain(im, 3000, 14, 9)), "adv-cut.jpg")


def frame_delivery():
    """Преимущество «Доставка от 1 дня»: длинномер с манипулятором."""
    im = canvas(top=(48, 58, 72), bottom=(10, 12, 16), warm=(300, 90, 470, (92, 82, 62)))
    d = ImageDraw.Draw(im, "RGBA")
    for x, y, r in [(120, 120, 60), (250, 152, 44), (92, 232, 50), (332, 92, 36)]:
        tube_end(d, x, y, r, steel=(74, 82, 92))
    floor_plane(d, 620, 560, (24, 27, 32))
    for x in range(-100, W, 220):
        d.polygon([(x, 700 - x * .05), (x + 110, 694 - x * .05),
                   (x + 130, 712 - x * .05), (x + 20, 718 - x * .05)], fill=(122, 128, 136, 80))
    bx, by = 250, 470
    d.rectangle([bx, by, bx + 700, by + 34], fill=(40, 45, 52))
    sheet_stack(d, bx + 20, by - 118, 5, 660, 20, lean=3, col=(122, 130, 140))
    for i in range(9):
        bar(im, d, bx + 40 + i * 74, by - 130, bx + 70 + i * 74, by - 200, 26,
            hi=(196, 204, 214), lo=(60, 66, 74), seed=i)
    tx, ty = 950, 430
    d.rectangle([tx, ty, tx + 250, ty + 110], fill=(36, 41, 48))
    d.rectangle([tx + 30, ty - 70, tx + 210, ty + 10], fill=(44, 50, 58))
    d.rectangle([tx + 55, ty - 55, tx + 190, ty - 4], fill=(122, 152, 180, 200))
    d.polygon([(tx + 210, ty - 70), (tx + 250, ty - 30), (tx + 250, ty + 10), (tx + 210, ty + 10)],
              fill=(56, 62, 70))
    d.rectangle([tx + 214, ty - 66, tx + 248, ty + 6], fill=ACCENT + (255,))
    for wx in (tx + 60, tx + 170, bx + 90, bx + 250, bx + 410):
        d.ellipse([wx - 40, ty + 96, wx + 40, ty + 176], fill=(18, 20, 24))
        d.ellipse([wx - 22, ty + 114, wx + 22, ty + 158], fill=(58, 64, 72))
    d.line([(bx + 690, by - 30), (bx + 560, by - 260), (bx + 300, by - 300)],
           fill=(70, 76, 86), width=26, joint="curve")
    d.line([(bx + 690, by - 30), (bx + 560, by - 260), (bx + 300, by - 300)],
           fill=(152, 162, 174, 120), width=6, joint="curve")
    d.line([(bx + 300, by - 300), (bx + 250, by - 150)], fill=(56, 62, 72), width=14)
    d.line([(bx + 250, by - 150), (bx + 250, by - 60)], fill=(192, 200, 210, 220), width=3)
    for i in range(5):
        rebar(im, d, bx + 190 + i * 30, by - 60, bx + 196 + i * 30, by + 40, 16, seed=20 + i)
    save(finish(grain(im, 2800, 14, 11)), "adv-delivery.jpg")


def frame_docs():
    """Преимущество «Сертификаты ГОСТ»: паспорт качества на фоне штабеля."""
    im = canvas(top=(40, 46, 54), bottom=(11, 13, 17), warm=(880, 130, 430, (72, 68, 58)))
    d = ImageDraw.Draw(im, "RGBA")
    sheet_stack(d, 60, 150, 9, 520, 26, lean=9, col=(116, 124, 134))
    d.rectangle([70, 168, 214, 202], fill=(230, 234, 240, 220))
    d.text((84, 176), "GOST  09G2S", fill=(40, 46, 54))
    floor_plane(d, 560, 520, (30, 34, 40))
    px, py = 560, 250
    card = [(px + 30, py + 40), (px + 330, py + 10), (px + 360, py + 400), (px + 60, py + 430)]
    d.polygon([(x + 14, y + 16) for x, y in card], fill=(6, 8, 11, 150))
    d.polygon(card, fill=(238, 240, 243))
    d.polygon(card, outline=(150, 156, 164), width=2)
    d.polygon([(px + 44, py + 62), (px + 320, py + 34), (px + 322, py + 60), (px + 46, py + 88)],
              fill=ACCENT + (255,))
    d.polygon([(px + 46, py + 96), (px + 200, py + 80), (px + 202, py + 92), (px + 48, py + 108)],
              fill=(60, 66, 74))
    for i in range(9):
        y = py + 120 + i * 26
        d.line([(px + 50 + i * 1.5, y), (px + 300 + i * 1.5, y - 24)], fill=(120, 128, 138, 210), width=3)
        d.line([(px + 50 + i * 1.5, y + 8), (px + 230 + i * 1.5, y - 16)], fill=(152, 160, 170, 160), width=2)
    ccx, ccy = px + 268, py + 330
    d.ellipse([ccx - 62, ccy - 58, ccx + 62, ccy + 66], outline=(38, 62, 128, 235), width=5)
    d.ellipse([ccx - 44, ccy - 40, ccx + 44, ccy + 48], outline=(38, 62, 128, 190), width=3)
    for a in range(0, 360, 12):
        aa = math.radians(a)
        d.line([(ccx + math.cos(aa) * 50, ccy + math.sin(aa) * 52),
                (ccx + math.cos(aa) * 58, ccy + math.sin(aa) * 60)], fill=(38, 62, 128, 150), width=2)
    d.text((ccx - 26, ccy - 10), "OTK", fill=(38, 62, 128, 230))
    rnd = random.Random(8)
    d.line([(px + 90 + rnd.randint(0, 5), py + 372 + rnd.randint(-5, 5)) for _ in range(14)],
           fill=(30, 40, 70, 220), width=3, joint="curve")
    save(finish(grain(im, 2600, 13, 13)), "adv-docs.jpg")


def frame_price():
    """Преимущество «Цена от завода»: стеллажи, бирка с ценой."""
    im = canvas(top=(38, 44, 52), bottom=(9, 11, 15), warm=(620, 50, 570, (88, 76, 54)))
    d = ImageDraw.Draw(im, "RGBA")
    racks_bg(d, 40, 90, 4, 4, bay_w=250, bay_h=470, steel=(96, 104, 114), r=15)
    floor_plane(d, 660, 620, (24, 28, 33))
    for i in range(7):
        rebar(im, d, -40 + i * 26, 700 - i * 12, 760 + i * 26, 520 - i * 10, 30, seed=30 + i)
    d.line([(300, 470), (560, 430)], fill=(184, 192, 202, 200), width=7)
    d.line([(280, 600), (540, 560)], fill=(184, 192, 202, 200), width=7)
    lx, ly = 700, 560
    tag = [(lx, ly), (lx + 300, ly - 26), (lx + 320, ly + 150), (lx + 20, ly + 176)]
    d.polygon([(x + 12, y + 12) for x, y in tag], fill=(6, 8, 11, 140))
    d.polygon(tag, fill=(240, 241, 244))
    d.polygon(tag, outline=(140, 146, 154), width=2)
    d.polygon([(lx + 14, ly + 16), (lx + 286, ly - 6), (lx + 290, ly + 34), (lx + 18, ly + 56)],
              fill=ACCENT + (255,))
    d.ellipse([lx + 30, ly + 70, lx + 54, ly + 94], outline=(120, 126, 134), width=4)
    d.text((lx + 78, ly + 62), "42 000 RUB / T", fill=(36, 42, 50))
    d.line([(lx + 40, ly + 118), (lx + 280, ly + 100)], fill=(150, 156, 164), width=4)
    d.line([(lx + 40, ly + 138), (lx + 230, ly + 122)], fill=(172, 178, 186), width=3)
    save(finish(grain(im, 2800, 13, 15)), "adv-price.jpg")


CAT_KINDS = ["rebar", "beam", "channel", "angle", "sheet", "pipe", "round", "inox"]


def frame_cat(kind):
    """Кадры категорий: используются и в карточках, и в слайдере."""
    if kind == "inox":
        im = canvas(top=(58, 66, 78), bottom=(14, 17, 22), warm=(500, 50, 660, (122, 132, 152)))
        d = ImageDraw.Draw(im, "RGBA")
        for i in range(15):                                # полированные зеркальные полосы
            x = i * 88
            strip = ImageOps.colorize(
                Image.linear_gradient("L").rotate(90).resize((int(88 * SS), H * SS)).convert("L"),
                black=(68, 76, 90), white=(228, 236, 246))
            im.paste(strip, (x * SS, 0))
        d = ImageDraw.Draw(im, "RGBA")
        for i in range(22):
            x = i * 58
            d.line([(x, 0), (x + 280, H)], fill=(255, 255, 255, 24), width=9)
        floor_plane(d, 620, 560, (28, 32, 38))
        tube_end(d, 300, 420, 90, wall=.14, steel=(178, 188, 200))
        tube_end(d, 470, 470, 62, wall=.16, steel=(168, 178, 190))
        save(finish(grain(im, 2400, 12, 23)), "inox.jpg")
        return

    im = canvas(top=(42, 48, 56), bottom=(10, 12, 16), warm=(700, 80, 500, (86, 78, 62)))
    d = ImageDraw.Draw(im, "RGBA")
    floor_plane(d, 660, 620, (26, 30, 36))
    if kind == "rebar":
        for i in range(9):
            rebar(im, d, -60 + i * 30, 720 - i * 26, 700 + i * 30, 430 - i * 22, 34, seed=i)
        d.line([(200, 560), (520, 470)], fill=(192, 200, 210, 190), width=8)
    elif kind == "beam":
        for i in range(3):
            beam_profile(im, d, 260 + i * 300, 520 + i * 40, 110 - i * 8, 165 - i * 12, 28, 24,
                         -.05 + i * .04, 420 - i * 60, face=(126, 134, 144), edge=(68, 74, 82))
    elif kind == "channel":
        for i in range(4):
            cx, cy = 180 + i * 260, 540 - i * 26
            beam_profile(im, d, cx, cy, 86, 120, 22, 20, .02, 340, face=(120, 128, 138), edge=(64, 70, 78))
            d.polygon([(cx + 118, cy - 74), (cx + 152, cy - 78), (cx + 154, cy + 78), (cx + 118, cy + 74)],
                      fill=(28, 32, 38))       # правая полка срезана -> П-образное сечение швеллера
    elif kind == "angle":
        for i in range(6):
            cx, cy = 150 + i * 175, 560 - i * 22
            p = [(cx - 70, cy - 78), (cx + 78, cy - 60), (cx + 66, cy - 22), (cx - 6, cy - 30),
                 (cx + 4, cy + 74), (cx - 40, cy + 80), (cx - 52, cy - 22), (cx - 74, cy - 30)]
            d.polygon([(x + 92, y - 62) for x, y in p], fill=(60, 66, 74))
            d.polygon(p, fill=(124, 132, 142))
            d.polygon(p, outline=(70, 76, 84), width=2)
            d.line([p[0], p[1]], fill=(236, 242, 248, 170), width=4)
    elif kind == "sheet":
        sheet_stack(d, 90, 290, 11, 760, 24, lean=13, col=(124, 132, 142))
        d.rectangle([120, 336, 320, 378], fill=(232, 236, 242, 215))
        d.text((136, 346), "10x1500x6000  St3sp5", fill=(44, 50, 58))
    elif kind == "pipe":
        for r_ in range(5):
            for c in range(5 - r_):
                tube_end(d, 330 + c * 118 + r_ * 59, 620 - r_ * 104, 52, wall=.2, steel=(128, 136, 146))
        for i in range(4):
            bar(im, d, 60 + i * 40, 700, 520 + i * 40, 300, 30, hi=(180, 188, 198), lo=(56, 62, 70), seed=i)
    elif kind == "round":
        for i in range(8):
            bar(im, d, -40 + i * 26, 700 - i * 30, 640 + i * 26, 420 - i * 26, 40,
                hi=(200, 208, 216), lo=(58, 64, 72), rust=.004, seed=i)
    save(finish(grain(im, 2600, 12, 23)), f"{kind}.jpg")


FRAMES = {
    "hero": frame_hero,
    "about": frame_about,
    "adv-cut": frame_cut,
    "adv-delivery": frame_delivery,
    "adv-docs": frame_docs,
    "adv-price": frame_price,
    **{k: (lambda k=k: frame_cat(k)) for k in CAT_KINDS},
}

if __name__ == "__main__":
    keys = sys.argv[1:] or list(FRAMES)
    print("Генерация кадров:", ", ".join(keys))
    for k in keys:
        FRAMES[k]()
    print("Готово →", OUT)
