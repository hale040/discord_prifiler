#!/usr/bin/env python3
"""Генерация тематических изображений металлопроката (заглушки студийного уровня).
Запуск из корня проекта: python3 tools/gen_images.py
Результат перезаписывает assets/img/*.jpg
"""
from PIL import Image, ImageDraw, ImageFilter
import math

STEEL = (150, 160, 172)
STEEL_M = (128, 136, 146)
STEEL_D = (96, 106, 120)
STEEL_L = (200, 208, 216)
HOLE = (50, 55, 61)
DARK = (24, 28, 33)
OUT = "assets/img"


def canvas(w, h, base=(38, 43, 49)):
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        c = tuple(int(base[i] + (8 if i == 2 else 5) * (1 - t)) for i in range(3))
        d.line([(0, y), (w, y)], fill=c)
    return im, d


def save(im, name, q=85):
    im.save(f"{OUT}/{name}.jpg", quality=q)
    print(name, im.size)


def pipe_end(d, x, y, r, ring=STEEL):
    d.ellipse([x - r, y - r, x + r, y + r], fill=ring, outline=DARK, width=3)
    ri = r * 0.5
    d.ellipse([x - ri, y - ri, x + ri, y + ri], fill=HOLE, outline=(112, 120, 130), width=2)
    d.arc([x - r, y - r, x + r, y + r], 200, 320, fill=STEEL_L, width=4)


# ---------------- HERO ----------------
im, d = canvas(1920, 1080, (30, 34, 40))
d.polygon([(0, 1080), (1920, 760), (1920, 1080)], fill=(22, 25, 29))
cx, cy, R = 430, 620, 26
sp = R * 1.78
centers = []
for dx in range(-4, 5):
    for dy in range(-4, 5):
        x = cx + dx * sp + (dy % 2) * sp / 2
        y = cy + dy * sp * 0.87
        if ((x - cx) / (3.4 * R)) ** 2 + ((y - cy) / (2.8 * R)) ** 2 < 1:
            centers.append((x, y))
for (x, y) in centers:
    pipe_end(d, x, y, R * 0.55)
for i in range(26):
    y0 = 500 + i * 20
    x0 = 900 + (i % 3) * 14
    x1 = 1880
    col = STEEL if i % 2 else STEEL_M
    d.line([(x0, y0), (x1, y0 - 140)], fill=col, width=9)
    for t in range(0, x1 - x0, 46):
        xx = x0 + t
        yy = y0 - 140 * t / (x1 - x0)
        d.line([(xx, yy - 8), (xx + 14, yy + 10)], fill=STEEL_D, width=4)
im = im.filter(ImageFilter.GaussianBlur(0.6))
save(im, "hero-steel", q=82)

W, H = 600, 400

# ---------------- REBAR ----------------
im, d = canvas(W, H)
for i in range(7):
    y = 40 + i * 55
    col = STEEL if i % 2 else STEEL_M
    d.line([(20, y + 30), (580, y - 20)], fill=col, width=22)
    d.line([(20, y + 30), (580, y - 20)], fill=STEEL_L, width=6)
    for t in range(0, 560, 34):
        x = 20 + t
        yy = y + 30 - 50 * t / 560
        d.line([(x, yy - 12), (x + 12, yy + 12)], fill=STEEL_D, width=5)
save(im, "rebar")

# ---------------- BEAM (двутавр) ----------------
im, d = canvas(W, H)
def ibeam(cx, cy, s=1):
    w, h, t = 140 * s, 170 * s, 22 * s
    pts = [(cx-w/2, cy-h/2), (cx+w/2, cy-h/2), (cx+w/2, cy-h/2+t), (cx+t/2, cy-h/2+t),
           (cx+t/2, cy+h/2-t), (cx+w/2, cy+h/2-t), (cx+w/2, cy+h/2), (cx-w/2, cy+h/2),
           (cx-w/2, cy+h/2-t), (cx-t/2, cy+h/2-t), (cx-t/2, cy-h/2+t), (cx-w/2, cy-h/2+t)]
    d.polygon(pts, fill=STEEL, outline=DARK)
    d.line([(cx-w/2, cy-h/2), (cx+w/2, cy-h/2)], fill=STEEL_L, width=max(3, int(6*s)))
    inner = [(cx-w/2+t, cy-h/2+t), (cx+w/2-t, cy-h/2+t), (cx+w/2-t, cy+h/2-t), (cx-w/2+t, cy+h/2-t)]
    d.polygon(inner, fill=(70, 76, 84))
for (x, y, s) in [(150, 140, .8), (330, 120, .95), (500, 160, .7), (210, 300, 1.0), (430, 310, .85)]:
    ibeam(x, y, s)
save(im, "beam")

# ---------------- CHANNEL (швеллер, П-образное сечение) ----------------
im, d = canvas(W, H)
def channel(cx, cy, w=130, h=95, t=20):
    # стенка + две полки: внешний контур
    d.rectangle([cx-w/2, cy-h/2, cx-w/2+t, cy+h/2], fill=STEEL, outline=DARK)   # спинка
    d.rectangle([cx-w/2, cy-h/2, cx+w/2, cy-h/2+t], fill=STEEL, outline=DARK)   # верхняя полка
    d.rectangle([cx-w/2, cy+h/2-t, cx+w/2, cy+h/2], fill=STEEL, outline=DARK)   # нижняя полка
    # блик и тень
    d.rectangle([cx-w/2, cy-h/2, cx+w/2, cy-h/2+5], fill=STEEL_L)
    d.rectangle([cx-w/2, cy+h/2-4, cx+w/2, cy+h/2], fill=STEEL_D)
    # торцы полок
    d.rectangle([cx+w/2-t, cy-h/2, cx+w/2, cy-h/2+t], fill=STEEL_M, outline=DARK)
    d.rectangle([cx+w/2-t, cy+h/2-t, cx+w/2, cy+h/2], fill=STEEL_M, outline=DARK)
for (x, y) in [(160, 120), (360, 110), (520, 150), (220, 300), (440, 310)]:
    channel(x, y)
save(im, "channel")

# ---------------- ANGLE (уголок L) ----------------
im, d = canvas(W, H)
def angle(cx, cy, L=180, t=26, col=STEEL):
    d.rectangle([cx-L/2, cy-L/2, cx+L/2, cy-L/2+t], fill=col, outline=DARK)
    d.rectangle([cx-L/2, cy-L/2, cx-L/2+t, cy+L/2], fill=col, outline=DARK)
    d.rectangle([cx-L/2, cy-L/2, cx+L/2, cy-L/2+5], fill=STEEL_L)
angle(170, 130, 190, 28)
angle(400, 110, 170, 24, STEEL_M)
angle(300, 290, 210, 30)
angle(500, 300, 150, 22, STEEL_M)
save(im, "angle")

# ---------------- SHEET (штабель листов) ----------------
im, d = canvas(W, H)
for i in range(9):
    y = 330 - i * 32
    off = i * 6
    col = STEEL if i % 2 else STEEL_M
    d.polygon([(40+off, y), (560-off, y), (540-off, y-22), (60+off, y-22)], fill=col, outline=DARK)
    d.line([(40+off, y), (560-off, y)], fill=STEEL_D, width=4)
d.rectangle([60, 60, 540, 84], fill=STEEL_L)
save(im, "sheet")

# ---------------- PIPE (трубы в штабеле) ----------------
im, d = canvas(W, H)
R = 34
for row, yy in enumerate([120, 190, 260, 330]):
    for col in range(7 - row // 2):
        x = 70 + col * 78 + row * 20
        pipe_end(d, x, yy, R, STEEL if (row + col) % 2 else STEEL_M)
save(im, "pipe")

# ---------------- ROUND + ПОЛОСА ----------------
im, d = canvas(W, H)
for row, yy in enumerate([110, 200, 290]):
    xs = [80, 170, 260, 350] if row != 1 else [125, 215, 305]
    for x in xs:
        r = 42
        col = STEEL if row % 2 == 0 else STEEL_M
        d.ellipse([x-r, yy-r, x+r, yy+r], fill=col, outline=DARK, width=3)
        d.arc([x-r, yy-r, x+r, yy+r], 200, 320, fill=STEEL_L, width=5)
d.rectangle([420, 70, 470, 340], fill=STEEL, outline=DARK)
d.rectangle([470, 70, 520, 340], fill=STEEL_M, outline=DARK)
d.rectangle([420, 70, 520, 80], fill=STEEL_L)
save(im, "round")

# ---------------- INOX (полированный лист) ----------------
im, d = canvas(W, H, (150, 158, 166))
for y in range(H):
    v = 170 + int(40 * math.sin(y / 9)) + (y // 3) % 7
    d.line([(0, y), (W, y)], fill=(v, min(255, v + 6), min(255, v + 12)))
for i in range(40):
    yy = (i * 17) % H
    d.line([(0, yy), (W, yy + 30)], fill=(215, 222, 230), width=1)
d.polygon([(0, 0), (200, 0), (0, 260)], fill=(232, 238, 244))
d.polygon([(600, 400), (420, 400), (600, 150)], fill=(120, 130, 140))
d.rounded_rectangle([70, 120, 300, 300], radius=14, outline=(60, 66, 72), width=5)
save(im, "inox", q=88)

# ---------------- ABOUT (склад) ----------------
im, d = canvas(900, 700, (40, 45, 51))
d.polygon([(0, 700), (900, 700), (900, 470), (0, 540)], fill=(28, 31, 35))
for i in range(10):
    y = 560 + i * 15
    d.line([(0, y), (900, y - 60)], fill=(45, 50, 56), width=2)
for lvl in range(3):
    y = 180 + lvl * 120
    d.rectangle([40, y, 430, y + 26], fill=STEEL, outline=DARK)
    d.rectangle([40, y, 430, y + 6], fill=STEEL_L)
    for s in range(4):
        sx = 60 + s * 95
        d.rectangle([sx, y - 34, sx + 70, y], fill=STEEL_M if s % 2 else STEEL, outline=DARK)
for cxr in (40, 240, 430):
    d.rectangle([cxr - 8, 150, cxr + 8, 520], fill=STEEL_D)
for row in range(3):
    for col in range(6):
        x = 560 + col * 52 + row * 20
        y = 430 - row * 46
        pipe_end(d, x, y, 22, STEEL if (row + col) % 2 else STEEL_M)
ORANGE = (230, 140, 40)
d.rectangle([80, 90, 860, 110], fill=ORANGE)
d.rectangle([420, 110, 450, 180], fill=ORANGE)
d.rectangle([400, 180, 470, 215], fill=(60, 66, 72))
d.polygon([(470, 215), (500, 245), (480, 300), (455, 290)], fill=STEEL)
d.line([(462, 215), (462, 330)], fill=STEEL_L, width=3)
d.rectangle([430, 330, 495, 360], fill=STEEL, outline=DARK)
for lx in (150, 350, 550, 750):
    d.polygon([(lx-26, 60), (lx+26, 60), (lx+60, 140), (lx-60, 140)], fill=(250, 250, 245))
im = im.filter(ImageFilter.GaussianBlur(0.5))
save(im, "about-warehouse", q=82)
print("OK")
