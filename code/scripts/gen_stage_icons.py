#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「生育期示意图」SVG
=======================
图谱里有 64 个生育期阶段名（各作物叫法不同），逐个配图不现实，
所以归并成 12 个通用阶段类型，每个用参数化方式画一张植株示意图。

产出：code/static/stages/*.svg（离线可用、体积极小、风格统一）

运行：python scripts/gen_stage_icons.py
"""
import io
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "static", "stages")

W, H = 160, 140
GROUND = 116

# 配色
GREEN = "#5f9243"
GREEN_L = "#84b163"
GREEN_D = "#3f6b2c"
GOLD = "#d9a72c"
GOLD_L = "#eccb63"
SOIL = "#e3d7c4"
SOIL_D = "#c9b79c"
FRUIT = "#d4753a"
POD = "#8fae4b"
SNOW = "#dbe7f0"


def _leaf(x, y, dx, dy, color=GREEN, width=3.2):
    """一片叶子：从 (x,y) 向 (x+dx, y+dy) 伸出，中部最宽"""
    mx, my = x + dx * 0.42, y + dy * 0.74
    ex, ey = x + dx, y + dy
    w = width * 1.55
    d = ("M%d,%d Q%.1f,%.1f %d,%d Q%.1f,%.1f %d,%d Z" % (
        x, y,
        mx - w, my - width * 0.95,
        ex, ey,
        mx + w, my + width * 1.7,
        x, y))
    return ('<path d="%s" fill="%s"/>'
            '<path d="M%d,%d Q%.1f,%.1f %d,%d" stroke="%s" stroke-width=".7" '
            'fill="none" opacity=".4"/>'
            % (d, color, x, y, mx, my, ex, ey, GREEN_D))


def _stem(x, y0, y1, color=GREEN, w=3.0, bend=0):
    """茎：自下而上，可轻微弯曲"""
    cy = (y0 + y1) / 2
    return ('<path d="M%d,%d Q%d,%d %d,%d" stroke="%s" stroke-width="%.1f" '
            'fill="none" stroke-linecap="round"/>'
            % (x, y0, x + bend, cy, x, y1, color, w))


def _ear(x, y, length=18, color=GOLD, grains=5):
    """穗：椭圆 + 交叉籽粒线"""
    s = ['<ellipse cx="%d" cy="%d" rx="5" ry="%.1f" fill="%s"/>' % (x, y, length / 2, color)]
    step = length / (grains + 1)
    for i in range(grains):
        yy = y - length / 2 + step * (i + 1)
        s.append('<path d="M%d,%d L%d,%d" stroke="#a97c18" stroke-width=".7" opacity=".55"/>'
                 % (x - 4, yy, x + 4, yy + 2.5))
    return "".join(s)


def _flowers(x, y, r=3.4, color="#f0e6c8", count=5):
    """小花：围绕中心排 5 瓣"""
    import math
    s = []
    for i in range(count):
        a = -math.pi / 2 + i * 2 * math.pi / count
        cx = x + math.cos(a) * r
        cy = y + math.sin(a) * r
        s.append('<circle cx="%.1f" cy="%.1f" r="2.5" fill="%s"/>' % (cx, cy, color))
    s.append('<circle cx="%d" cy="%d" r="2" fill="%s"/>' % (x, y, GOLD))
    return "".join(s)


def _fruit(x, y, rx=6, ry=7, color=FRUIT):
    a = ('<ellipse cx="%d" cy="%d" rx="%d" ry="%d" fill="%s"/>'
         % (x, y, rx, ry, color))
    b = ('<path d="M%d,%d q4,-3 8,0" stroke="#a8531f" stroke-width=".8" '
         'fill="none" opacity=".5"/>' % (x - 2, y - ry + 2))
    return a + b


def _ground(snow=False):
    s = ['<rect x="0" y="%d" width="%d" height="%d" fill="%s"/>' % (GROUND, W, H - GROUND, SOIL),
         '<path d="M0,%d L%d,%d" stroke="%s" stroke-width="1.4"/>' % (GROUND, W, GROUND, SOIL_D)]
    if snow:
        s.append('<path d="M0,%d q20,-8 40,0 t40,0 t40,0 t40,0 L%d,%d L0,%d Z" fill="%s"/>'
                 % (GROUND, W, GROUND + 8, GROUND, SNOW))
    return "".join(s)


def _seed(x, y):
    return ('<ellipse cx="%d" cy="%d" rx="4.5" ry="3.2" fill="#b08a52" transform="rotate(-18 %d %d)"/>'
            % (x, y, x, y))


# ------------------------------------------------------------------ 12 个阶段
def stage_sowing():
    """播种出苗：土里的种子 + 顶出两片子叶"""
    s = [_ground(), _seed(80, GROUND + 6),
         _stem(80, GROUND, GROUND - 12, GREEN_L, 2.4),
         _leaf(80, GROUND - 11, -13, -8, GREEN_L),
         _leaf(80, GROUND - 11, 13, -8, GREEN_L)]
    return "".join(s)


def stage_seedling():
    """育苗/苗期：小苗 4 片叶"""
    s = [_ground(), _stem(80, GROUND, GROUND - 26, GREEN, 2.6)]
    for i, (dy, dx) in enumerate([(-8, -16), (-8, 16), (-17, -13), (-17, 13)]):
        s.append(_leaf(80, GROUND + dy, dx, -5, GREEN_L if i < 2 else GREEN))
    return "".join(s)


def stage_tillering():
    """分蘖/分枝：基部抽出多茎"""
    s = [_ground()]
    for dx, h in ((-14, 24), (0, 30), (14, 24)):
        s.append(_stem(80 + dx, GROUND, GROUND - h, GREEN, 2.4, bend=dx * 0.25))
        s.append(_leaf(80 + dx, GROUND - h * 0.55, -11 if dx <= 0 else 11, -6, GREEN_L))
        s.append(_leaf(80 + dx, GROUND - h * 0.85, 9 if dx <= 0 else -9, -7, GREEN))
    return "".join(s)


def stage_jointing():
    """拔节/伸蔓：茎秆伸长，节间明显"""
    s = [_ground(), _stem(80, GROUND, GROUND - 58, GREEN, 3.2)]
    for i in range(3):
        y = GROUND - 18 - i * 15
        s.append('<path d="M74,%d L86,%d" stroke="%s" stroke-width="1.1" opacity=".5"/>'
                 % (y, y, GREEN_D))
    s.append(_leaf(80, GROUND - 30, -22, -9, GREEN))
    s.append(_leaf(80, GROUND - 42, 22, -9, GREEN_L))
    s.append(_leaf(80, GROUND - 20, 18, -7, GREEN_L))
    s.append(_leaf(80, GROUND - 52, -17, -8, GREEN))
    return "".join(s)


def stage_booting():
    """孕穗/现蕾：顶端鼓包"""
    s = [_ground(), _stem(80, GROUND, GROUND - 64, GREEN, 3.2),
         '<ellipse cx="80" cy="%d" rx="6.5" ry="9" fill="%s"/>'
         % (GROUND - 66, GREEN_L)]
    s.append(_leaf(80, GROUND - 26, -22, -9, GREEN))
    s.append(_leaf(80, GROUND - 38, 22, -9, GREEN))
    s.append(_leaf(80, GROUND - 50, -18, -8, GREEN_L))
    s.append(_leaf(80, GROUND - 58, 16, -7, GREEN))
    return "".join(s)


def stage_flowering():
    """开花：穗抽出 + 小花"""
    s = [_ground(), _stem(80, GROUND, GROUND - 66, GREEN, 3.2),
         _ear(80, GROUND - 74, 16, GOLD_L, 4), _flowers(80, GROUND - 84, 4, "#f5eecf")]
    s.append(_leaf(80, GROUND - 24, -22, -9, GREEN))
    s.append(_leaf(80, GROUND - 36, 22, -9, GREEN))
    s.append(_leaf(80, GROUND - 50, -19, -8, GREEN_L))
    s.append(_leaf(80, GROUND - 60, 17, -8, GREEN))
    return "".join(s)


def stage_filling():
    """灌浆/结荚：籽粒充实，穗更饱满"""
    s = [_ground(), _stem(80, GROUND, GROUND - 66, GREEN, 3.2),
         _ear(80, GROUND - 78, 26, GOLD, 6)]
    s.append(_leaf(80, GROUND - 26, -24, -10, GREEN))
    s.append(_leaf(80, GROUND - 42, 24, -10, GREEN))
    s.append(_leaf(80, GROUND - 56, -18, -8, GREEN_L))
    return "".join(s)


def stage_enlarging():
    """果实/块茎膨大：茎上挂果"""
    s = [_ground(), _stem(80, GROUND, GROUND - 52, GREEN, 3.0),
         _leaf(80, GROUND - 22, -20, -9, GREEN),
         _leaf(80, GROUND - 34, 20, -9, GREEN),
         _leaf(80, GROUND - 46, -15, -7, GREEN_L)]
    s.append(_fruit(64, GROUND - 30, 6, 7, FRUIT))
    s.append(_fruit(96, GROUND - 38, 5.5, 6.5, FRUIT))
    s.append(_fruit(78, GROUND - 18, 5, 6, FRUIT))
    return "".join(s)


def stage_maturing():
    """成熟收获：整株转黄，穗下垂"""
    s = [_ground(), _stem(80, GROUND, GROUND - 62, "#b99a4a", 3.2),
         _ear(84, GROUND - 72, 24, GOLD, 6)]
    s.append(_leaf(80, GROUND - 24, -22, -6, "#c7ad63"))
    s.append(_leaf(80, GROUND - 40, 22, -6, "#c7ad63"))
    s.append(_leaf(80, GROUND - 54, -16, -5, "#d4bd7c"))
    return "".join(s)


def stage_vegetative():
    """营养生长旺盛：叶片繁茂"""
    s = [_ground(), _stem(80, GROUND, GROUND - 34, GREEN, 2.8)]
    pairs = [(-8, 24), (-8, -24), (-16, 21), (-16, -21), (-24, 17), (-24, -17)]
    for i, (dy, dx) in enumerate(pairs):
        s.append(_leaf(80, GROUND + dy, dx, -8, GREEN if i % 2 else GREEN_L, 2.4))
    s.append(_leaf(80, GROUND - 34, 0, -12, GREEN_L, 2.4))
    return "".join(s)


def stage_heading():
    """结球/产品器官形成：外叶抱合"""
    s = [_ground()]
    for dx, dy in ((-26, -4), (26, -4), (-20, -16), (20, -16)):
        s.append(_leaf(80, GROUND - 4, dx, dy, GREEN, 2.6))
    s.append('<circle cx="80" cy="%d" r="17" fill="%s"/>' % (GROUND - 26, GREEN_L))
    s.append('<path d="M80,%d a17,17 0 0 0 0,34" fill="none" stroke="%s" stroke-width=".9" opacity=".5"/>'
             % (GROUND - 43, GREEN_D))
    s.append('<path d="M80,%d q-12,8 -6,26" fill="none" stroke="%s" stroke-width=".9" opacity=".45"/>'
             % (GROUND - 43, GREEN_D))
    s.append('<path d="M80,%d q12,8 6,26" fill="none" stroke="%s" stroke-width=".9" opacity=".45"/>'
             % (GROUND - 43, GREEN_D))
    return "".join(s)


def stage_dormant():
    """休眠/越冬：落叶 + 覆雪"""
    s = [_ground(snow=True), _stem(80, GROUND, GROUND - 40, "#8a7a5c", 3.0)]
    s.append('<path d="M80,%d q-14,-6 -22,-3" stroke="#8a7a5c" stroke-width="2" fill="none"/>'
             % (GROUND - 34))
    s.append('<path d="M80,%d q14,-6 22,-3" stroke="#8a7a5c" stroke-width="2" fill="none"/>'
             % (GROUND - 26))
    s.append(_leaf(80, GROUND - 20, -16, -4, "#a8905f"))
    s.append(_leaf(80, GROUND - 30, 15, -5, "#a8905f"))
    return "".join(s)


STAGES = [
    ("sowing", "播种出苗期", stage_sowing),
    ("seedling", "幼苗期", stage_seedling),
    ("tillering", "分蘖期 / 分枝期", stage_tillering),
    ("jointing", "拔节期 / 伸蔓期", stage_jointing),
    ("booting", "孕穗期 / 现蕾期", stage_booting),
    ("flowering", "抽穗开花期", stage_flowering),
    ("filling", "灌浆期 / 结荚期", stage_filling),
    ("enlarging", "果实膨大期", stage_enlarging),
    ("maturing", "成熟收获期", stage_maturing),
    ("vegetative", "营养生长旺盛期", stage_vegetative),
    ("heading", "结球期 / 产品器官形成期", stage_heading),
    ("dormant", "休眠期 / 越冬期", stage_dormant),
]


def build(stage_id, label, fn):
    body = fn()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
        'role="img" aria-label="%s">'
        '<title>%s</title>%s</svg>' % (W, H, W, H, label, label, body))


if __name__ == "__main__":
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    for sid, label, fn in STAGES:
        path = os.path.join(OUT_DIR, sid + ".svg")
        io.open(path, "w", encoding="utf-8").write(build(sid, label, fn))
        print("生成 %-12s %s" % (sid + ".svg", label))
    print("\n共 %d 张，输出目录：%s" % (len(STAGES), OUT_DIR))
