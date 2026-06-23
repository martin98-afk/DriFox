# -*- coding: utf-8 -*-
"""重新生成像素狐狸 Spritesheet — 全面优化所有状态动画"""
# pip install Pillow

from PIL import Image

# ═══════════════════════════════════════════════════
# 颜色
# ═══════════════════════════════════════════════════
ORANGE = (240, 136, 62)
ORANGE_LIGHT = (255, 179, 71)
WHITE = (255, 224, 178)
DARK = (26, 26, 46)
PINK = (255, 138, 128)
BLUE = (102, 198, 255)
YELLOW = (255, 235, 59)        # 烟花主色
YELLOW_BRIGHT = (255, 255, 150)  # 烟花高光
AMBER = (255, 193, 7)           # 烟花暗色

FRAME = 16
COLS = 8
ROWS = 7
W = FRAME * COLS   # 128
H = FRAME * ROWS   # 112

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))


# ═══════════════════════════════════════════════════
# 辅助绘制函数
# ═══════════════════════════════════════════════════

def draw_sparkle(canvas, ox, oy, cx, cy):
    """在 (ox+cx, oy+cy) 绘制一颗黄色四角星烟花粒子"""
    def sp(x, y, color):
        if 0 <= x < 16 and 0 <= y < 16:
            canvas.putpixel((ox + x, oy + y), color)

    # 十字星形：中心亮 + 四方向
    sp(cx, cy, YELLOW_BRIGHT)
    sp(cx - 1, cy, YELLOW)
    sp(cx + 1, cy, YELLOW)
    sp(cx, cy - 1, YELLOW)
    sp(cx, cy + 1, YELLOW)
    # 对角暗色
    sp(cx - 1, cy - 1, AMBER)
    sp(cx + 1, cy - 1, AMBER)
    sp(cx - 1, cy + 1, AMBER)
    sp(cx + 1, cy + 1, AMBER)


def draw_question_mark(canvas, ox, oy):
    """在左上角绘制一个小问号（3×3 像素）"""
    def qp(x, y, color):
        if 0 <= x < 16 and 0 <= y < 16:
            canvas.putpixel((ox + x, oy + y), color)

    qp(0, 0, YELLOW)
    qp(1, 0, YELLOW)
    qp(2, 1, YELLOW)
    qp(1, 2, YELLOW)
    qp(2, 2, YELLOW)


def draw_fox(canvas, ox, oy, **overrides):
    """
    绘制一只 16×16 像素狐狸，ox/oy 是左上角偏移。
    关键：头(3-12行)和身体(9-14行)有重叠区域(9-12行)，确保不分家。
    """
    def p(x, y, color):
        if 0 <= x < 16 and 0 <= y < 16:
            canvas.putpixel((ox + x, oy + y), color)

    # ── 尾巴 ──
    tail = [
        (13, 11), (14, 10), (15, 10),
        (13, 12), (14, 11), (15, 11),
        (13, 13), (14, 12),
    ]
    for x, y in tail:
        p(x, y, ORANGE)
    p(15, 9, WHITE)  # 尾巴尖

    # ── 腿 ──
    for x in [5, 6, 9, 10]:
        p(x, 13, ORANGE)
    for x in [5, 6, 9, 10]:
        p(x, 14, ORANGE)

    # ── 身体 (9-13行，包含和头的重叠 9行) ──
    body = []
    for y in range(9, 13):
        for x in range(4, 12):
            body.append((x, y))
    # 去掉腿的位置
    body = [(x, y) for x, y in body if y not in (13, 14) or x not in (5, 6, 9, 10)]

    for x, y in body:
        p(x, y, ORANGE)

    # 白肚皮 (10-11行)
    for y in range(10, 12):
        for x in range(6, 10):
            p(x, y, WHITE)

    # ── 耳朵 ──
    # 左耳
    ear_l = [(4, 1), (5, 1), (4, 2), (5, 2), (6, 2)]
    for x, y in ear_l:
        p(x, y, ORANGE)
    p(5, 1, ORANGE_LIGHT)
    p(5, 2, ORANGE_LIGHT)

    # 右耳 — 支持耳动覆盖
    ear_r = overrides.get("ear_r", [(10, 1), (11, 1), (10, 2), (11, 2), (9, 2)])
    for x, y in ear_r:
        p(x, y, ORANGE)
    p(10, 1, ORANGE_LIGHT)
    p(10, 2, ORANGE_LIGHT)

    # 左耳 — 支持耳动覆盖
    if overrides.get("ear_l_alt"):
        for x, y in overrides["ear_l_alt"]:
            p(x, y, ORANGE)
        # 高光
        p(5, 1, ORANGE_LIGHT)

    # ── 头 (3-8行) ──
    for y in range(3, 9):
        for x in range(3, 13):
            p(x, y, ORANGE)
    # 白色面部 (5-7行)
    for y in range(5, 8):
        for x in range(5, 11):
            p(x, y, WHITE)

    # ── 眼睛 ──
    eyes_white = overrides.get("eyes_white", None)
    pupils = overrides.get("pupils", None)
    highlights = overrides.get("highlights", None)

    if eyes_white is None:
        p(5, 4, WHITE); p(6, 4, WHITE)   # 左眼白
        p(9, 4, WHITE); p(10, 4, WHITE)  # 右眼白
    else:
        for x, y in eyes_white:
            p(x, y, WHITE)

    if pupils is None:
        p(6, 4, DARK)   # 左瞳孔
        p(9, 4, DARK)   # 右瞳孔
    else:
        for x, y in pupils:
            p(x, y, DARK)

    if highlights is None:
        p(5, 4, (255, 255, 255, 180))
        p(9, 4, (255, 255, 255, 180))
    else:
        for x, y in highlights:
            p(x, y, (255, 255, 255, 180))

    # ── 鼻子 ──
    nose = overrides.get("nose", [(7, 6), (8, 6)])
    for x, y in nose:
        p(x, y, DARK)

    # ── 嘴 ──
    mouth = overrides.get("mouth", [(7, 7), (8, 7)])
    for x, y in mouth:
        p(x, y, DARK)

    # ── 腮红 ──
    p(4, 6, PINK); p(11, 6, PINK)

    # ── 泪滴（error 用） ──
    if overrides.get("tear"):
        p(11, 5, BLUE)
        p(11, 6, BLUE)

    # ── 额外粒子（烟花等） ──
    particles = overrides.get("particles", [])
    for cx, cy in particles:
        draw_sparkle(canvas, ox, oy, cx, cy)

    # ── 问号 ──
    if overrides.get("question_mark"):
        draw_question_mark(canvas, ox, oy)


# ═══════════════════════════════════════════════════
# Row 0: idle — 呼吸起伏 + 眨眼（8帧）
# ═══════════════════════════════════════════════════
y0 = 0 * FRAME
# 微妙呼吸：身体微微上下 + 偶尔眨眼
idle_dys = [0, -1, -1, 0, 0, 1, 0, 0]
idle_overrides = [
    {},                                                          # 0: 正
    {},                                                          # 1: 吸气
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(5, 4), (6, 4)],
     "highlights": []},                                          # 2: 闭眼（吸气顶）
    {},                                                          # 3: 呼气
    {},                                                          # 4: 正
    {},                                                          # 5: 微呼
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(5, 4), (6, 4)],
     "highlights": []},                                          # 6: 闭眼
    {},                                                          # 7: 正
]
for f in range(8):
    dy = idle_dys[f]
    draw_fox(img, f * FRAME, y0 + dy, **idle_overrides[f])


# ═══════════════════════════════════════════════════
# Row 1: thinking — 歪头思考 + 眼球转 + 思考点（8帧）
# ═══════════════════════════════════════════════════
y1 = 1 * FRAME
thinks = [
    # 帧0: 正 + 眼球偏左上
    {"pupils": [(6, 3), (9, 3)], "highlights": [(5, 3), (9, 3)]},
    # 帧1: 头微歪右 + 眼球右上
    {"pupils": [(7, 3), (10, 3)], "highlights": [(6, 3), (10, 3)]},
    # 帧2: 正 + 眨眼
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(5, 4), (6, 4)],
     "highlights": []},
    # 帧3: 头微歪左 + 眼球左
    {"pupils": [(5, 4), (8, 4)], "highlights": [(4, 4), (8, 4)]},
    # 帧4: 正 + 眼球偏上
    {"pupils": [(6, 3), (9, 3)], "highlights": [(5, 3), (9, 3)]},
    # 帧5: 眨眼 + 右耳微动
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(5, 4), (6, 4)],
     "highlights": [],
     "ear_r": [(10, 0), (11, 0), (10, 1), (11, 1), (9, 1)]},  # 耳朵上移1px
    # 帧6: 眼球右
    {"pupils": [(7, 4), (10, 4)], "highlights": [(6, 4), (10, 4)]},
    # 帧7: 正
    {},
]
for f in range(8):
    draw_fox(img, f * FRAME, y1, **thinks[f])


# ═══════════════════════════════════════════════════
# Row 2: streaming — 说话嘴动 + 身体微弹 + 眨眼眯眼（8帧）
# ═══════════════════════════════════════════════════
y2 = 2 * FRAME
stream_dys = [0, -1, 0, 1, 0, -1, 0, 0]  # 说话时身体微弹
streams = [
    # 帧0: 小口
    {"mouth": [(7, 7), (8, 7)]},
    # 帧1: 大张嘴 + 身体上弹 + 眯眼
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)],
     "eyes_white": [(5, 4), (6, 4), (9, 4), (10, 4)],
     "pupils": [(5, 4), (9, 4)],
     "highlights": [(5, 4), (9, 4)]},
    # 帧2: 偏右小口
    {"mouth": [(7, 7), (8, 7), (9, 7)]},
    # 帧3: 大嘴 + 身体下落 + 眨眼
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)],
     "eyes_white": [(5, 4), (6, 4), (9, 4), (10, 4)],
     "pupils": [(5, 4), (9, 4)],
     "highlights": []},
    # 帧4: 偏左小口
    {"mouth": [(6, 7), (7, 7), (8, 7)]},
    # 帧5: 闭嘴 + 身体上弹 + 眯眼
    {"mouth": [(7, 7), (8, 7)],
     "eyes_white": [(5, 4), (6, 4), (9, 4), (10, 4)],
     "pupils": [(5, 4), (9, 4)],
     "highlights": [(5, 4), (9, 4)]},
    # 帧6: 大嘴
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)]},
    # 帧7: 正常小口
    {"mouth": [(7, 7), (8, 7)]},
]
for f in range(8):
    dy = stream_dys[f]
    draw_fox(img, f * FRAME, y2 + dy, **streams[f])


# ═══════════════════════════════════════════════════
# Row 3: question — 歪头疑惑 + 问号 + 黄色烟花（8帧）
# ═══════════════════════════════════════════════════
y3 = 3 * FRAME
questions = [
    # 帧0: 歪左 + 大小眼 + 问号 + 火花
    {"eyes_white": [(5, 4), (6, 5)], "pupils": [(6, 5)], "highlights": [(5, 5)],
     "question_mark": True,
     "particles": [(14, 1), (1, 4)]},
    # 帧1: 正 + 问号 + 火花闪烁
    {"question_mark": True,
     "particles": [(13, 0), (2, 3)]},
    # 帧2: 歪右 + 大小眼 + 问号 + 火花
    {"eyes_white": [(9, 4), (10, 5)], "pupils": [(9, 5)], "highlights": [(9, 5)],
     "question_mark": True,
     "particles": [(15, 2), (0, 1)]},
    # 帧3: 正 + 张嘴疑惑 + 问号
    {"mouth": [(6, 7), (7, 7), (8, 7)],
     "question_mark": True,
     "particles": [(14, 1)]},
    # 帧4: 歪左 + 大小眼 + 问号 + 火花爆发
    {"eyes_white": [(5, 4), (6, 5)], "pupils": [(6, 5)], "highlights": [(5, 5)],
     "question_mark": True,
     "particles": [(13, 1), (1, 3), (15, 0)]},
    # 帧5: 正 + 大张嘴 + 问号
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)],
     "question_mark": True,
     "particles": [(14, 0), (0, 2)]},
    # 帧6: 歪右 + 大小眼 + 问号 + 火花
    {"eyes_white": [(9, 4), (10, 5)], "pupils": [(9, 5)], "highlights": [(9, 5)],
     "question_mark": True,
     "particles": [(13, 2), (1, 1)]},
    # 帧7: 正 + 问号
    {"question_mark": True,
     "particles": [(14, 1), (0, 3)]},
]
for f in range(8):
    draw_fox(img, f * FRAME, y3, **questions[f])


# ═══════════════════════════════════════════════════
# Row 4: success — 开心跳跃（降高）+ 黄色烟花（8帧）
# ═══════════════════════════════════════════════════
y4 = 4 * FRAME
# 跳跃高度大幅降低：最高 -4px（原 -8px）
success_dys = [0, -1, -2, -4, -2, -1, 0, 0]
success_overrides = [
    # 帧0: 预备 — 笑眼 + 小火花
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(6, 4)], "highlights": [(5, 4)],
     "particles": [(14, 2)]},
    # 帧1: 起跳 — 眯眼 + 火花
    {"eyes_white": [(5, 4)], "pupils": [(5, 4)], "highlights": [(5, 4)],
     "particles": [(13, 1), (1, 3)]},
    # 帧2: 升空 — 闭眼开心 + 烟花爆发
    {"eyes_white": [], "pupils": [], "highlights": [],
     "particles": [(14, 0), (1, 1), (0, 4)]},
    # 帧3: 最高点 — 眯眼 + 大烟花
    {"eyes_white": [(5, 4)], "pupils": [(5, 4)], "highlights": [(5, 4)],
     "particles": [(13, 0), (15, 2), (2, 0), (0, 3)]},
    # 帧4: 下落 — 笑眼 + 火花散去
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(6, 4)], "highlights": [(5, 4)],
     "particles": [(14, 1), (1, 2)]},
    # 帧5: 着地 — 笑眼
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(6, 4)], "highlights": [(5, 4)],
     "particles": [(13, 2)]},
    # 帧6: 缓冲 — 笑眼
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(6, 4)], "highlights": [(5, 4)]},
    # 帧7: 恢复 — 笑眼
    {"eyes_white": [(5, 4), (6, 4)], "pupils": [(6, 4)], "highlights": [(5, 4)]},
]
for f in range(8):
    dy = success_dys[f]
    draw_fox(img, f * FRAME, y4 + dy, **success_overrides[f])


# ═══════════════════════════════════════════════════
# Row 5: error — 惊吓发抖 + 泪滴（8帧）
# ═══════════════════════════════════════════════════
y5 = 5 * FRAME
# 水平抖动维持，增加幅度让效果更明显
error_shifts = [1, -2, 2, -1, 2, -2, 1, -1]
for f in range(8):
    dx = error_shifts[f]
    # 双眼瞪大 3×1 + 瞳孔居中
    overrides = {
        "eyes_white": [(4, 3), (5, 3), (6, 3), (9, 3), (10, 3), (11, 3)],
        "pupils": [(5, 3), (10, 3)],
        "highlights": [(4, 3), (9, 3)],
    }
    if f in (2, 4, 6):
        overrides["tear"] = True  # 泪滴 — 间隔出现更有节奏
    draw_fox(img, f * FRAME + dx, y5, **overrides)


# ═══════════════════════════════════════════════════
# Row 6: sleeping — 闭眼睡眠 + ZZZ 飘动（8帧）
# ═══════════════════════════════════════════════════
y6 = 6 * FRAME
sleep_dys = [0, 1, 0, 1, 0, 1, 0, 1]  # 呼吸浮动
sleep_eyes = {
    "eyes_white": [(5, 4), (6, 4)],
    "pupils": [(5, 4), (6, 4)],   # 瞳孔撑满 = 闭眼
    "highlights": [],
}
for f in range(8):
    dy = sleep_dys[f]
    draw_fox(img, f * FRAME, y6 + dy, **sleep_eyes)


# ═══════════════════════════════════════════════════
# 保存
# ═══════════════════════════════════════════════════
out_path = "D:/work/DriFoxx/app/widgets/pet_sprites.png"
img.save(out_path)
print(f"Done: {out_path}  ({W}×{H})")
