# -*- coding: utf-8 -*-
"""重新生成像素狐狸 Spritesheet — 增强版 v2：10状态×12帧，更丰富的动画"""

from PIL import Image
from pathlib import Path

# ════════════════════════════════════════════════════════════════
# 调色板
# ════════════════════════════════════════════════════════════════
ORANGE       = (240, 136, 62)
ORANGE_MID   = (220, 120, 50)
ORANGE_DARK  = (180, 95, 35)
ORANGE_LIGHT = (255, 179, 71)
CREAM        = (255, 224, 178)
WHITE_FACE   = (255, 240, 210)
DARK         = (26, 26, 46)
DARK_SOFT    = (60, 55, 70)
PINK         = (255, 138, 128)
PINK_SOFT    = (255, 180, 170)
BLUE         = (102, 198, 255)
BLUE_DARK    = (60, 160, 220)
YELLOW       = (255, 235, 59)
YELLOW_BRIGHT= (255, 255, 180)
AMBER        = (255, 193, 7)
RED          = (255, 80, 70)
GREEN        = (150, 255, 140)
PURPLE       = (200, 150, 255)
WHITE_PURE   = (255, 255, 255)
TEAR_BLUE    = (130, 210, 255)
GLASSES_TINT = (180, 220, 255, 80)

# ── 尾巴形状 ──
TAIL_NORMAL   = [(15,8),(12,9),(13,9),(14,9),(15,9),(12,10),(13,10),(14,10),(15,10),(12,11),(13,11),(14,11),(15,11),(12,12),(13,12),(14,12)]
TAIL_WAG_UP   = [(15,7),(12,8),(13,8),(14,8),(15,8),(12,9),(13,9),(14,9),(15,9),(12,10),(13,10),(14,10),(15,10),(12,11),(13,11),(14,11)]
TAIL_WAG_DOWN = [(15,9),(12,10),(13,10),(14,10),(15,10),(12,11),(13,11),(14,11),(15,11),(12,12),(13,12),(14,12),(15,12),(12,13),(13,13),(14,13)]
TAIL_HAPPY    = [(15,6),(12,7),(13,7),(14,7),(15,7),(12,8),(13,8),(14,8),(15,8),(12,9),(13,9),(14,9),(15,9),(12,10),(13,10),(14,10)]
TAIL_WAG_FAST = [(15,6),(12,7),(13,7),(14,7),(15,7),(12,8),(13,8),(14,8),(15,8),(12,9),(13,9),(14,9),(15,9),(12,11),(13,11),(14,11),(15,11)]

FRAME = 16
COLS = 12
# 15 行：idle, thinking, streaming, question, success, error, sleeping, writing,
#        thinking_hard, excited, dragging, warning, playing, music, wakeup
ROWS = 15
W = FRAME * COLS  # 192
H = FRAME * ROWS  # 240

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

# ════════════════════════════════════════════════════════════════
# 绘制辅助 + 细胞裁剪
# ════════════════════════════════════════════════════════════════

# 当前帧的裁剪矩形（由 draw_fox 设置）
_current_cell_rect = None  # (left, top, right, bottom)


def _set_cell_rect(l, t, r, b):
    """设置当前帧裁剪区域"""
    global _current_cell_rect
    _current_cell_rect = (l, t, r, b)


def p_px(canvas, ox, oy, x, y, color):
    """在画布 (ox+x, oy+y) 画一个像素（已裁剪到当前细胞边界）"""
    px, py = ox + x, oy + y
    # 先检查全局边界
    if not (0 <= px < W and 0 <= py < H):
        return
    # 再检查细胞边界裁剪
    if _current_cell_rect is not None:
        l, t, r, b = _current_cell_rect
        if not (l <= px <= r and t <= py <= b):
            return
    if isinstance(color, tuple) and len(color) == 4:
        existing = canvas.getpixel((px, py))
        if existing[3] > 0:
            a = color[3] / 255
            blended = tuple(int(existing[i] * (1-a) + color[i] * a) for i in range(3)) + (255,)
            canvas.putpixel((px, py), blended)
        else:
            canvas.putpixel((px, py), color)
    else:
        canvas.putpixel((px, py), color)

def px_list(canvas, ox, oy, coords, color):
    for x, y in coords:
        p_px(canvas, ox, oy, x, y, color)

def draw_sparkle(canvas, ox, oy, cx, cy, color=YELLOW):
    pts = [(cx,cy),(cx-1,cy),(cx+1,cy),(cx,cy-1),(cx,cy+1),
           (cx-1,cy-1),(cx+1,cy-1),(cx-1,cy+1),(cx+1,cy+1)]
    for x,y in pts:
        p_px(canvas, ox, oy, x, y, color)

def draw_mini_star(canvas, ox, oy, cx, cy):
    for x,y in [(cx,cy),(cx-1,cy-1),(cx+1,cy-1)]:
        p_px(canvas, ox, oy, x, y, YELLOW_BRIGHT)

def draw_heart(canvas, ox, oy, cx, cy):
    pts = [(cx,cy),(cx+1,cy),(cx-1,cy+1),(cx,cy+1),(cx+1,cy+1),(cx+2,cy+1),(cx,cy+2),(cx+1,cy+2)]
    for x,y in pts:
        p_px(canvas, ox, oy, x, y, PINK)

def draw_music_note(canvas, ox, oy, cx, cy):
    """小音符"""
    for x,y in [(cx,cy),(cx+1,cy),(cx,cy+1),(cx,cy+2),(cx+1,cy+2),(cx+2,cy+1)]:
        p_px(canvas, ox, oy, x, y, PURPLE)

def draw_question_mark(canvas, ox, oy, frame=0):
    """右侧弹跳问号，frame 控制位置（已在细胞边界内）"""
    bounce_offsets = [0, -1, -1, 0, 0, -1, -1, 0, -1, 0, -1, 0]
    dy = bounce_offsets[frame] if frame < len(bounce_offsets) else 0
    qx, qy = 11, 1 + dy
    q_pts = [(qx+1,qy),(qx+2,qy),(qx+3,qy+1),(qx+2,qy+2),(qx+2,qy+3),(qx+2,qy+4)]
    for x,y in q_pts:
        p_px(canvas, ox, oy, x, y, WHITE_PURE)
    p_px(canvas, ox, oy, qx+2, qy+5, WHITE_PURE)

def draw_zzz(canvas, ox, oy, seq=0):
    zzz_positions = [None, (12,2,1), (12,1,1), (11,0,2), (11,-1,2), (10,-2,3), (10,-2,3), None,
                     (11,1,2), (10,0,3), (9,-1,3), None]
    entry = zzz_positions[seq] if seq < len(zzz_positions) else None
    if entry is None: return
    dx, dy, size = entry
    def zz(x, y, c): p_px(canvas, ox, oy, dx+x, dy+y, c)
    if size == 1:
        for x,y in [(0,0),(1,0),(2,0),(2,1),(0,2),(1,2),(2,2)]: zz(x,y,BLUE)
    elif size == 2:
        for x,y in [(0,0),(1,0),(2,0),(3,0),(3,1),(2,2),(0,3),(1,3),(2,3)]: zz(x,y,BLUE)
    elif size == 3:
        for x,y in [(0,0),(1,0),(2,0),(3,0),(4,0),(4,1),(3,2),(2,3),(0,4),(1,4),(2,4),(3,4),(4,4)]: zz(x,y,BLUE_DARK)

def draw_thinking_dots(canvas, ox, oy, frame=0):
    dot_patterns = [
        [], [(6,0),(8,0)], [(6,0),(8,0),(10,0)], [(8,0)], [],
        [(6,0),(8,0),(10,0),(12,0)], [(6,0),(8,0)], [(8,0),(10,0)],
        [(6,0),(10,0)], [(6,0),(8,0),(10,0)], [(8,0)], [(6,0),(8,0),(10,0),(12,0)],
    ]
    pattern = dot_patterns[frame] if frame < len(dot_patterns) else []
    for x, y in pattern:
        p_px(canvas, ox, oy, x, y, WHITE_PURE)

def draw_sweat(canvas, ox, oy, frame=0):
    """思考过度流汗 — 双侧散落效果"""
    # 左右两侧汗水散落，每帧不同组合产生飞溅动画
    # 左侧 x=2~4, y=1~4   右侧 x=11~13, y=1~4
    sweat_patterns = [
        [(3, 2), (12, 2)],                          # 0: 左右各一
        [(2, 2), (13, 2)],                          # 1
        [(3, 1), (12, 3)],                          # 2
        [(2, 3), (4, 2), (11, 2), (13, 3)],         # 3: 各二
        [(3, 2), (12, 1)],                          # 4
        [(2, 1), (4, 3), (11, 3), (13, 1)],         # 5: 各二散落
        [(3, 3), (12, 2)],                          # 6
        [(2, 2), (4, 1), (11, 2), (13, 3)],         # 7: 各二
        [(3, 2), (12, 3), (13, 2)],                 # 8: 右二左一
        [(2, 3), (3, 1), (11, 2)],                  # 9: 左二右一
        [(3, 2), (4, 2), (12, 2), (13, 2)],         # 10: 各二排开
        [(2, 2), (3, 3), (11, 3), (12, 1), (13, 3)], # 11: 最多散落
    ]
    drops = sweat_patterns[frame] if frame < len(sweat_patterns) else []
    for x, y in drops:
        p_px(canvas, ox, oy, x, y, TEAR_BLUE)

def draw_pen(canvas, ox, oy, frame=0):
    """写作时笔尖在右侧点动"""
    pen_y_offsets = [0, -1, 0, 1, 0, -1, 0, 1, 0, -1, 0, 0]
    dy = pen_y_offsets[frame] if frame < len(pen_y_offsets) else 0
    # 笔尖（右侧）
    for x,y in [(13,4+dy),(13,5+dy),(13,6+dy),(12,6+dy)]:
        p_px(canvas, ox, oy, x, y, DARK)
    # 笔身
    for x,y in [(13,7+dy),(13,8+dy)]:
        p_px(canvas, ox, oy, x, y, BLUE)

# ════════════════════════════════════════════════════════════════
# 核心狐狸绘制
# ════════════════════════════════════════════════════════════════

def draw_fox(canvas, ox, oy, cell_oy=None, **overrides):
    """
    增强版狐狸绘制 — 紧凑 16×16，带细胞裁剪
    cell_oy: 所在行的顶部 Y（不含 dy 偏移），用于裁剪
    """
    # 设置细胞裁剪边界
    if cell_oy is not None:
        _set_cell_rect(ox, cell_oy, ox + FRAME - 1, cell_oy + FRAME - 1)
    else:
        _set_cell_rect(ox, oy, ox + FRAME - 1, oy + FRAME - 1)

    def p(x, y, color): p_px(canvas, ox, oy, x, y, color)

    # ── 尾巴 ──
    tail = overrides.get("tail", TAIL_NORMAL)
    px_list(canvas, ox, oy, tail, ORANGE)
    tail_tip = overrides.get("tail_tip", [(15,8)])
    px_list(canvas, ox, oy, tail_tip, CREAM)

    # ── 腿 ──
    leg_color = overrides.get("leg_color", ORANGE_DARK)
    for x in [5, 6, 9, 10]:
        p(x, 12, leg_color); p(x, 13, leg_color)

    # ── 身体 ──
    for y in range(8, 12):
        for x in range(4, 12): p(x, y, ORANGE)
    for x in range(4, 12): p(x, 11, ORANGE_MID)

    # ── 白肚皮 ──
    for y in range(9, 11):
        for x in range(6, 10): p(x, y, CREAM)

    # ── 耳朵 ──
    ear_l = overrides.get("ear_l", [(4,2),(5,2),(4,3),(5,3)])
    px_list(canvas, ox, oy, ear_l, ORANGE)
    p(5, 3, ORANGE_LIGHT)
    ear_r = overrides.get("ear_r", [(10,2),(11,2),(10,3),(11,3)])
    px_list(canvas, ox, oy, ear_r, ORANGE)
    p(10, 3, ORANGE_LIGHT)
    p(4, 3, PINK_SOFT); p(11, 3, PINK_SOFT)

    # ── 头 ──
    for y in range(4, 8):
        for x in range(4, 12): p(x, y, ORANGE)
    for y in range(5, 8):
        for x in range(6, 10): p(x, y, CREAM)

    # ── 眼镜（writing 专用） ──
    glasses = overrides.get("glasses")
    if glasses:
        # 眼镜框
        for x in range(5, 8):
            p(x, 4, DARK_SOFT); p(x, 6, DARK_SOFT)
        for x in range(9, 12):
            p(x, 4, DARK_SOFT); p(x, 6, DARK_SOFT)
        p(8, 5, DARK_SOFT)  # 镜桥
        # 镜片反光
        p(6, 5, GLASSES_TINT); p(10, 5, GLASSES_TINT)

    # ── 眼睛 ──
    eyes_white = overrides.get("eyes_white", [(6,5),(7,5),(9,5),(10,5)])
    px_list(canvas, ox, oy, eyes_white, WHITE_PURE)
    pupils = overrides.get("pupils", [(7,5),(9,5)])
    px_list(canvas, ox, oy, pupils, DARK)
    highlights = overrides.get("highlights", [(6,5),(10,5)])
    px_list(canvas, ox, oy, highlights, WHITE_PURE)

    # ── 星星眼（success/excited 专用） ──
    star_eyes = overrides.get("star_eyes")
    if star_eyes:
        for sx, sy in star_eyes:
            draw_mini_star(canvas, ox, oy, sx, sy)

    # ── 鼻子 ──
    nose = overrides.get("nose", [(7,6),(8,6)]); px_list(canvas, ox, oy, nose, DARK)

    # ── 嘴 ──
    mouth = overrides.get("mouth", [(7,7),(8,7)]); px_list(canvas, ox, oy, mouth, DARK)
    smile = overrides.get("smile")
    if smile: px_list(canvas, ox, oy, smile, DARK)

    # ── 腮红 ──
    p(5, 6, PINK_SOFT); p(10, 6, PINK_SOFT)

    # ── 泪滴（原来的单侧泪滴，保留兼容）
    if overrides.get("tear"):
        p(11, 4, TEAR_BLUE); p(11, 5, TEAR_BLUE); p(10, 6, TEAR_BLUE)

    # ── 流泪 😭（双侧泪痕 + 流动动画）
    cry_frame = overrides.get("cry_frame")
    if cry_frame is not None:
        # 右眼泪痕（从右眼角 x=11,y=4 流下）
        r_len = 2 + (cry_frame % 5)  # 长度 2~6
        for i in range(r_len):
            if i == 0:
                p(11, 4, TEAR_BLUE)
            elif i == 1:
                p(11, 5, TEAR_BLUE)
            elif i == 2:
                p(10, 6, TEAR_BLUE)
            elif i == 3:
                p(11, 7, TEAR_BLUE)
            elif i == 4:
                p(10, 8, TEAR_BLUE)
            elif i == 5:
                p(10, 9, TEAR_BLUE)
        # 左眼泪痕（从左眼角 x=4,y=4 流下）
        l_len = 2 + ((cry_frame + 2) % 5)  # 长度 2~6，错开右眼节奏
        for i in range(l_len):
            if i == 0:
                p(4, 4, TEAR_BLUE)
            elif i == 1:
                p(4, 5, TEAR_BLUE)
            elif i == 2:
                p(5, 6, TEAR_BLUE)
            elif i == 3:
                p(4, 7, TEAR_BLUE)
            elif i == 4:
                p(5, 8, TEAR_BLUE)
            elif i == 5:
                p(5, 9, TEAR_BLUE)
        # 泪滴飞溅（底部溅开的小点）
        if cry_frame % 2 == 0:
            p(9, 10, TEAR_BLUE)   # 右侧溅射
            p(10, 11, TEAR_BLUE)
        if cry_frame % 3 == 0:
            p(6, 10, TEAR_BLUE)   # 左侧溅射
            p(5, 11, TEAR_BLUE)

    # ── 特效粒子 ──
    for cx, cy in overrides.get("sparkles", []): draw_sparkle(canvas, ox, oy, cx, cy, YELLOW)
    for cx, cy in overrides.get("hearts", []): draw_heart(canvas, ox, oy, cx, cy)
    for cx, cy in overrides.get("mini_stars", []): draw_mini_star(canvas, ox, oy, cx, cy)
    for cx, cy in overrides.get("music_notes", []): draw_music_note(canvas, ox, oy, cx, cy)

    # ── 问号 ──
    q_frame = overrides.get("q_frame")
    if q_frame is not None: draw_question_mark(canvas, ox, oy, q_frame)

    # ── Zzz ──
    z_seq = overrides.get("zzz_seq")
    if z_seq is not None: draw_zzz(canvas, ox, oy, z_seq)

    # ── 思考点 ──
    dot_frame = overrides.get("dot_frame")
    if dot_frame is not None: draw_thinking_dots(canvas, ox, oy, dot_frame)

    # ── 流汗 ──
    if overrides.get("sweat"): draw_sweat(canvas, ox, oy, overrides.get("sweat_frame", 0))

    # ── 笔 ──
    if overrides.get("pen"): draw_pen(canvas, ox, oy, overrides.get("pen_frame", 0))


# ════════════════════════════════════════════════════════════════
# Row 0: idle — 呼吸起伏 + 眨眼 + 耳动 + 摇尾（12帧）
# ════════════════════════════════════════════════════════════════
idle_dys = [0, -1, -1, 0, 0, 1, 0, 0, -1, 0, 1, 0]
idle_frames = [
    # 0  正常
    {},
    # 1  右耳动 + 尾上摆
    {"ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "tail": TAIL_WAG_UP},
    # 2  闭眼 + 尾下摆
    {"eyes_white": [], "pupils": [], "highlights": [], "tail": TAIL_WAG_DOWN},
    # 3  左耳动 + 尾上摆
    {"ear_l": [(4,1),(5,1),(4,2),(5,2)], "tail": TAIL_WAG_UP, "tail_tip": [(15,7)]},
    # 4  正常
    {},
    # 5  右耳动
    {"ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "tail": TAIL_WAG_UP},
    # 6  闭眼 + 微笑
    {"eyes_white": [], "pupils": [], "highlights": [], "mouth": [(7,7),(8,7)], "smile": [(6,8),(9,8)]},
    # 7  张嘴哈欠 + 尾下摆
    {"mouth": [(6,7),(7,7),(8,7),(9,7)], "tail": TAIL_WAG_DOWN, "tail_tip": [(15,9)]},
    # 8  正常（补充帧）
    {},
    # 9  半闭眼（补充帧）— 左眼半睁，右眼闭着
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)]},
    # 10 伸懒腰（身体抬起）
    {"tail": TAIL_WAG_UP, "tail_tip": [(15,7)]},
    # 11 恢复正常
    {},
]
for f in range(12):
    dy = idle_dys[f]
    kwargs = dict(idle_frames[f])
    draw_fox(img, f * FRAME, 0 * FRAME + dy, cell_oy=0 * FRAME, **kwargs)

# ════════════════════════════════════════════════════════════════
# Row 1: thinking — 眼球转 + 思考点 + 耳动（12帧）
# ════════════════════════════════════════════════════════════════
thinking_frames = [
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},                   # 0 看上
    {"pupils": [(7,5),(10,5)], "highlights": [(6,5),(9,5)], "dot_frame": 1},   # 1 看右
    {"eyes_white": [], "pupils": [], "highlights": [], "dot_frame": 2},        # 2 闭眼思考
    {"pupils": [(6,5),(9,5)], "highlights": [(7,5),(10,5)], "dot_frame": 3},  # 3 看左
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},                   # 4 看上
    {"eyes_white": [], "pupils": [], "highlights": [],                         # 5 歪头思考
     "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "dot_frame": 5},
    {"pupils": [(7,4),(10,4)], "highlights": [(6,5),(9,5)], "dot_frame": 6},  # 6 看右上
    {"eyes_white": [], "pupils": [], "highlights": [],                         # 7 闭眼+耳动
     "ear_l": [(4,1),(5,1),(4,2),(5,2)], "dot_frame": 7},
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)], "dot_frame": 8},  # 8 看上+远点
    {"pupils": [(6,4),(9,4)], "highlights": [(7,5),(10,5)], "dot_frame": 9},  # 9 看左上
    {"eyes_white": [], "pupils": [], "highlights": [], "dot_frame": 10},       # 10 闭眼+两点
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)], "dot_frame": 11}, # 11 回来
]
for f in range(12):
    draw_fox(img, f * FRAME, 1 * FRAME, cell_oy=1 * FRAME, **thinking_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 2: streaming — 说话嘴动 + 身体起伏 + 眨眼（12帧）
# ════════════════════════════════════════════════════════════════
stream_dys = [0, -1, 0, 1, 0, -1, 0, 0, -1, 0, 1, 0]
stream_frames = [
    {"mouth": [(7,7),(8,7)], "tail": TAIL_WAG_UP},                            # 0 小嘴
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 1 张嘴大
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [(7,5),(10,5)], "tail": TAIL_WAG_DOWN},
    {"mouth": [(7,7),(8,7),(9,7)], "tail": TAIL_WAG_UP},                     # 2 中嘴
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 3 张嘴+闭右眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [], "tail": TAIL_WAG_DOWN},
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 4 大张嘴
     "tail": TAIL_WAG_UP, "tail_tip": [(15,7)]},
    {"mouth": [(7,7),(8,7)],                                                  # 5 小嘴+睁大眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [(7,5),(10,5)], "tail": TAIL_WAG_DOWN},
    {"mouth": [(6,7),(7,7),(8,7),(9,7)], "tail": TAIL_WAG_UP},               # 6 中张嘴
    {"mouth": [(7,7),(8,7)], "smile": [(6,8),(9,8)]},                        # 7 小嘴+微笑
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 8 补充：大张嘴眨眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [(7,5),(10,5)]},
    {"mouth": [(7,7),(8,7)], "tail": TAIL_WAG_UP},                           # 9 补充
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 10 补充：大张嘴
     "tail": TAIL_WAG_DOWN},
    {"mouth": [(7,7),(8,7)], "smile": [(6,8),(9,8)]},                        # 11 微笑结尾
]
for f in range(12):
    dy = stream_dys[f]
    draw_fox(img, f * FRAME, 2 * FRAME + dy, cell_oy=2 * FRAME, **stream_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 3: question — 疑惑歪头 + 右侧弹跳问号（12帧）
# ════════════════════════════════════════════════════════════════
question_frames = [
    {"q_frame": 0, "pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},       # 0 看上
    {"q_frame": 1, "mouth": [(6,7),(7,7),(8,7),(9,7)]},                        # 1 好奇张嘴
    {"q_frame": 2, "eyes_white": [], "pupils": [], "highlights": []},          # 2 闭眼疑惑
    {"q_frame": 3, "pupils": [(6,5),(9,5)], "highlights": [(7,5),(10,5)]},    # 3 左看
    {"q_frame": 4, "pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},     # 4 归位
    {"q_frame": 5, "pupils": [(7,4),(10,4)], "highlights": [(6,5),(9,5)]},     # 5 右看
    {"q_frame": 6, "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)],              # 6 歪头
     "pupils": [(7,4),(10,4)], "highlights": [(6,5),(9,5)]},
    {"q_frame": 7, "mouth": [(6,7),(7,7),(8,7),(9,7)],                        # 7 张嘴
     "pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},
    {"q_frame": 8, "pupils": [(6,5),(9,5)], "highlights": [(7,5),(10,5)]},    # 8 左看
    {"q_frame": 9, "eyes_white": [], "pupils": [], "highlights": []},          # 9 闭眼
    {"q_frame": 10, "pupils": [(7,4),(10,4)], "highlights": [(6,5),(9,5)]},    # 10 右看
    {"q_frame": 11, "pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)]},    # 11 结束
]
for f in range(12):
    draw_fox(img, f * FRAME, 3 * FRAME, cell_oy=3 * FRAME, **question_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 4: success — 笑眼 + 星星眼 + 烟花散开 + 小心心（12帧）
# ════════════════════════════════════════════════════════════════
success_frames = [
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 0
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "sparkles": [(8,1)], "mini_stars": [(4,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                            # 1 星星眼
     "smile": [(6,8),(9,8)], "sparkles": [(7,0),(9,0)], "hearts": [(3,3)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                            # 2
     "smile": [(6,8),(9,8)], "sparkles": [(6,0),(10,0),(7,1),(9,1)],
     "mini_stars": [(4,1),(12,1)]},
    {"eyes_white": [], "pupils": [], "highlights": [],                         # 3 闭眼开心
     "smile": [(6,8),(9,8)], "sparkles": [(5,0),(11,0),(6,2),(10,2),
               (4,1),(12,1),(7,1),(9,1)], "hearts": [(3,2),(13,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                            # 4
     "smile": [(6,8),(9,8)], "sparkles": [(4,2),(12,2),(5,3),(11,3),(7,1),(9,1)],
     "mini_stars": [(3,3),(13,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 5
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "sparkles": [(3,2),(13,2),(6,3),(10,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 6 尾巴翘起
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY, "sparkles": [(4,3),(12,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 7
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                            # 8 补充
     "smile": [(6,8),(9,8)], "sparkles": [(5,2),(11,2)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 9 补充
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY, "hearts": [(4,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 10
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],             # 11 结束微笑
     "highlights": [(6,5),(10,5)],
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY},
]
for f in range(12):
    draw_fox(img, f * FRAME, 4 * FRAME, cell_oy=4 * FRAME, **success_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 5: error — 😭 大哭 + 双侧泪痕流动 + 抖动（12帧）
# ════════════════════════════════════════════════════════════════
error_shifts = [1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1]
error_eyes_base = {
    "eyes_white": [(4,4),(5,4),(6,4),(9,4),(10,4),(11,4)],
    "pupils": [(6,4),(9,4)],
    "highlights": [(5,4),(10,4)],
}
for f in range(12):
    dx = error_shifts[f]
    kwargs = dict(error_eyes_base)
    # 所有帧都带上泪痕，每帧长度不同产生流动感
    kwargs["cry_frame"] = f
    # 张嘴大哭 vs 瘪嘴哭交替
    if f in (0, 2, 4, 6, 8, 10):
        kwargs["mouth"] = [(6,7),(7,7),(8,7),(9,7)]  # 张嘴大哭
    else:
        kwargs["mouth"] = [(7,7),(8,7)]               # 瘪嘴抽泣
        kwargs["smile"] = [(6,8),(9,8)]                # 嘴角下拉 = 哭脸
    draw_fox(img, f * FRAME + dx, 5 * FRAME, cell_oy=5 * FRAME, **kwargs)

# ════════════════════════════════════════════════════════════════
# Row 6: sleeping — 趴睡（保留 idle 的头部/脸部/尾巴轮廓，闭眼+腮红+嘴角，无腿）
# ════════════════════════════════════════════════════════════════
# 趴睡关键：保留 idle Frame 6 的整体轮廓（耳朵 y=2~3、头 y=4~7、嘴 y=8~10、
# 尾巴 x=11~15 y=8~12），仅替换眼睛（睁→闭）、加嘴角弧线/腮红、去掉腿（y=11~13 DD→OO）。
sleep_dys = [0, -1, -1, 0, 1, 1, 0,   # 第一个呼吸周期（基准-吸起-吸满-回落-呼气-呼尽-基准）
             0, -1, -1, 0, 1, 1]      # 第二个呼吸周期（同样节奏，3 周期循环）

# 尾巴尖端 y 坐标（独立于身体 dy，按呼吸节奏切换）
tail_tip_y = [10, 9, 9, 10, 11, 11, 10,
              9, 9, 10, 11, 11, 10]

# 偶数帧添加腮红（6 帧有，6 帧无）
BLUSH_FRAMES = {0, 2, 4, 6, 8, 10}

def draw_fox_curled(canvas, ox, oy, frame, cell_oy):
    """绘制趴睡姿态：保留 idle 的头/脸/尾巴轮廓，闭眼+腮红+嘴角+无腿。

    与 idle Frame 6 (dy=0) 的差异：
    - 眼睛 y=6：睁眼 → 闭眼短横线（左右各 2px 黑）
    - 嘴角弧线 y=7：两侧嘴角上扬，软萌感
    - 腮红：偶数帧在脸两侧 y=7 加粉色小点
    - 腿消失：y=11~13 的 DD/MM → y=11~12 的 OO（趴下看不到腿）
    - 呼吸：dy 在 -1, 0, +1 间切换，尾巴尖独立抖动
    """
    _set_cell_rect(ox, cell_oy, ox + FRAME - 1, cell_oy + FRAME - 1)

    dy = sleep_dys[frame]
    tip_y = tail_tip_y[frame]

    # ── 耳朵（与 idle 一致：左耳 x=4~5, 右耳 x=10~11, y=2~3） ──
    for x, y in [(4, 2 + dy), (5, 2 + dy), (10, 2 + dy), (11, 2 + dy)]:
        p_px(canvas, ox, oy, x, y, ORANGE)
    # 内耳淡橙色（idle 风格：左耳 x=5, 右耳 x=10, y=3）
    p_px(canvas, ox, oy, 5, 3 + dy, ORANGE_LIGHT)
    p_px(canvas, ox, oy, 10, 3 + dy, ORANGE_LIGHT)
    # 内耳粉尖（idle 风格：左耳 x=4, 右耳 x=11, y=3）
    for x, y in [(4, 3 + dy), (11, 3 + dy)]:
        p_px(canvas, ox, oy, x, y, PINK_SOFT)

    # ── 头部（与 idle 一致：y=4~7 全宽橙色矩形，中部 cream 脸） ──
    for y in range(4 + dy, 8 + dy):
        for x in range(4, 12):
            p_px(canvas, ox, oy, x, y, ORANGE)
    for y in range(5 + dy, 8 + dy):
        for x in range(6, 10):
            p_px(canvas, ox, oy, x, y, CREAM)

    # ── 闭眼（y=6，左右各 2px 黑短横线） ──
    for x, y in [(6, 6 + dy), (7, 6 + dy), (9, 6 + dy), (10, 6 + dy)]:
        p_px(canvas, ox, oy, x, y, DARK)

    # ── 嘴角弧线（y=7，两侧嘴角上扬） ──
    for x, y in [(6, 7 + dy), (10, 7 + dy)]:
        p_px(canvas, ox, oy, x, y, DARK)

    # ── 鼻子（y=7 中间） ──
    p_px(canvas, ox, oy, 8, 7 + dy, DARK)

    # ── 腮红（偶数帧在脸两侧 y=7 加粉色小点） ──
    if frame in BLUSH_FRAMES:
        for x, y in [(5, 7 + dy), (10, 7 + dy)]:
            p_px(canvas, ox, oy, x, y, PINK_SOFT)

    # ── 嘴/下巴（y=8~10，与 idle 类似：橙色外框 + cream 中部） ──
    for y in range(8 + dy, 11 + dy):
        for x in range(4, 12):
            p_px(canvas, ox, oy, x, y, ORANGE)
    for y in range(8 + dy, 11 + dy):
        for x in range(6, 10):
            p_px(canvas, ox, oy, x, y, CREAM)

    # ── 身体底（y=11，全宽 ORANGE，取代 idle 的 MMMMMMMM 阴影行） ──
    for x in range(4, 12):
        p_px(canvas, ox, oy, x, 11 + dy, ORANGE)

    # ── 尾巴（与 idle TAIL_NORMAL 一致：右下角延伸，4 px 宽，加粗版） ──
    # 主体（橙色，y 随身体 dy 浮动）— 4 px 宽 × 5 行（含尖）
    for x, y in [(12, 8 + dy), (13, 8 + dy), (14, 8 + dy), (15, 8 + dy),
                 (12, 9 + dy), (13, 9 + dy), (14, 9 + dy), (15, 9 + dy),
                 (12, 10 + dy), (13, 10 + dy), (14, 10 + dy), (15, 10 + dy),
                 (12, 11 + dy), (13, 11 + dy), (14, 11 + dy), (15, 11 + dy),
                 (13, 12 + dy), (14, 12 + dy)]:
        p_px(canvas, ox, oy, x, y, ORANGE)

    # 尾巴尖（白色，2 px 宽，y 独立于身体 dy，按 tail_tip_y 切换）
    p_px(canvas, ox, oy, 14, tip_y, CREAM)
    p_px(canvas, ox, oy, 15, tip_y, CREAM)

    # ── 身体收尾（y=12 阴影，取代 idle 的 DD 腿） ──
    for x in range(5, 9):
        p_px(canvas, ox, oy, x, 12 + dy, ORANGE_MID)


for f in range(12):
    # 蜷睡帧循环：呼吸（dy 起伏）+ Zzz 从身侧（x=12-14）冒出
    draw_fox_curled(img, f * FRAME, 6 * FRAME, f, cell_oy=6 * FRAME)
    # Zzz 冒泡 — 从嘴部 (x=8,y=6) 冒出，逐渐变大飘向右上角
    zzz_seq_per_frame = [None, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, None]
    zz = zzz_seq_per_frame[f]
    if zz is not None:
        # Zzz 位置：(dx, dy, size) — 从嘴巴附近飘向右上角，逐渐变大
        zzz_positions = [(8, 6, 1), (9, 5, 1), (10, 4, 2), (11, 3, 2),
                         (12, 2, 3), (13, 1, 3), None, None,
                         None, None, None, None]
        entry = zzz_positions[zz] if zz < len(zzz_positions) else None
        if entry is not None:
            dx, dy_zzz, size = entry
            def draw_zzz_at(zx, zy, c):
                p_px(canvas=img, ox=f * FRAME, oy=6 * FRAME, x=dx + zx, y=dy_zzz + zy, color=c)
            if size == 1:
                for zx, zy in [(0, 0), (1, 0), (2, 0), (2, 1), (0, 2), (1, 2), (2, 2)]:
                    draw_zzz_at(zx, zy, BLUE)
            elif size == 2:
                for zx, zy in [(0, 0), (1, 0), (2, 0), (3, 0), (3, 1), (2, 2), (0, 3), (1, 3), (2, 3)]:
                    draw_zzz_at(zx, zy, BLUE)
            elif size == 3:
                for zx, zy in [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (4, 1),
                               (3, 2), (2, 3), (0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]:
                    draw_zzz_at(zx, zy, BLUE_DARK)

# ════════════════════════════════════════════════════════════════
# Row 7: writing — 写作状态 + 眼镜 + 笔尖点动（12帧）
# ════════════════════════════════════════════════════════════════
writing_frames = [
    {"glasses": True, "pen": True, "pen_frame": 0,                             # 0 写作中
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 1,                             # 1 笔动
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 2,                             # 2 笔动
     "eyes_white": [], "pupils": [], "highlights": []},
    {"glasses": True, "pen": True, "pen_frame": 3,                             # 3 笔动
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 4,                             # 4 抬头
     "pupils": [(6,5)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 5,                             # 5 继续写
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 6,                             # 6 笔动
     "eyes_white": [], "pupils": [], "highlights": []},
    {"glasses": True, "pen": True, "pen_frame": 7,                             # 7 笔动
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 8,                             # 8 思考停顿
     "pupils": [(10,4)], "highlights": [(10,5)]},
    {"glasses": True, "pen": True, "pen_frame": 9,                             # 9 继续写
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"glasses": True, "pen": True, "pen_frame": 10,                            # 10 笔动
     "eyes_white": [], "pupils": [], "highlights": []},
    {"glasses": True, "pen": True, "pen_frame": 11,                            # 11 停下看看
     "pupils": [(7,4)], "highlights": [(6,5)], "smile": [(6,8),(9,8)]},
]
for f in range(12):
    draw_fox(img, f * FRAME, 7 * FRAME, cell_oy=7 * FRAME, **writing_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 8: thinking_hard — 深度思考 + 流汗 + 问号旋转（12帧）
# ════════════════════════════════════════════════════════════════
thinking_hard_frames = [
    {"pupils": [(6,5),(9,5)], "highlights": [(7,5),(10,5)], "sweat": True, "sweat_frame": 0}, # 0 看左
    {"pupils": [(7,5),(10,5)], "highlights": [(6,5),(9,5)], "sweat": True, "sweat_frame": 1}, # 1 看右
    {"eyes_white": [], "pupils": [], "highlights": [], "sweat": True, "sweat_frame": 2}, # 2 闭眼
    {"pupils": [(6,5),(9,5)], "highlights": [(7,5),(10,5)], "sweat": True, "sweat_frame": 3}, # 3 看左
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)], "sweat": True, "sweat_frame": 4}, # 4 看上
    {"eyes_white": [], "pupils": [], "highlights": [],                               # 5 闭眼用力
     "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "sweat": True, "sweat_frame": 5},
    {"pupils": [(7,5),(10,5)], "highlights": [(6,5),(9,5)], "sweat": True, "sweat_frame": 6}, # 6 看右
    {"eyes_white": [], "pupils": [], "highlights": [],                               # 7 闭眼+耳动
     "ear_l": [(4,1),(5,1),(4,2),(5,2)], "sweat": True, "sweat_frame": 7},
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)], "sweat": True, "sweat_frame": 8}, # 8 看上
    {"pupils": [(7,4),(10,4)], "highlights": [(6,5),(9,5)], "sweat": True, "sweat_frame": 9}, # 9 看右上
    {"eyes_white": [], "pupils": [], "highlights": [], "sweat": True, "sweat_frame": 10}, # 10 闭眼
    {"pupils": [(7,4),(9,4)], "highlights": [(6,5),(10,5)], "sweat": True, "sweat_frame": 11}, # 11 看上
]
for f in range(12):
    draw_fox(img, f * FRAME, 8 * FRAME, cell_oy=8 * FRAME, **thinking_hard_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 9: excited — 跳跃 + 星星眼 + 音符 + 快速摆尾（12帧）
# ════════════════════════════════════════════════════════════════
excited_dys = [0, -2, -2, -1, 0, -2, -2, -1, 0, -2, -2, 0]
excited_frames = [
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 0
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 1 跳起
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(4,3),(12,3)], "music_notes": [(2,1)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 2 最高
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3,2),(13,2),(5,1),(11,1)], "hearts": [(4,4)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],      # 3 下落
     "highlights": [(6,5),(10,5)], "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(4,2),(12,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 4 落地
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 5 跳起
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(5,3),(11,3)], "music_notes": [(3,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 6 最高
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(2,2),(14,2),(4,1),(12,1)], "hearts": [(6,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],      # 7 下落
     "highlights": [(6,5),(10,5)], "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 8 落地
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3,3),(13,3)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 9 跳起
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "music_notes": [(2,2),(14,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 10 最高
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3,1),(13,1),(5,2),(11,2)], "hearts": [(4,3),(12,3)]},
    {"eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(7,5),(9,5)],      # 11 收尾
     "highlights": [(6,5),(10,5)], "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY},
]
for f in range(12):
    dy = excited_dys[f]
    draw_fox(img, f * FRAME, 9 * FRAME + dy, cell_oy=9 * FRAME, **excited_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 10: dragging — 挣扎（12帧：身体扭动 + 嘴张 + 眼睁大 + 四肢乱动）
# ════════════════════════════════════════════════════════════════
drag_dys = [-1, 1, 1, -1, -1, 1, 0, 0, 1, -1, 1, -1]  # 左右扭动

drag_frames = [
    # 0 起始：眼睁大、嘴微张、身体左倾
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 1 向右扭：dy=+1
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 2 继续右扭 + 嘴微张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 3 喊叫：嘴大张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)], "tail": TAIL_WAG_FAST},
    # 4 向左扭：dy=-1
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(9,4)], "highlights": [(5,4),(10,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 5 继续左扭
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(9,4)], "highlights": [(5,4),(10,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 6 四肢乱动：前腿前伸
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)], "leg_color": ORANGE_MID},
    # 7 后腿后蹬
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(9,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)], "leg_color": ORANGE_DARK},
    # 8 抖动循环：dy=+1 嘴张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(7,7),(8,7)]},
    # 9 抖动循环：dy=-1 嘴合
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(9,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 10 抖动循环：dy=+1
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 11 抖动循环：dy=-1
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(9,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(7,7),(8,7)]},
]
for f in range(12):
    dy = drag_dys[f]
    kwargs = dict(drag_frames[f])
    draw_fox(img, f * FRAME, 10 * FRAME + dy, cell_oy=10 * FRAME, **kwargs)

# ════════════════════════════════════════════════════════════════
# Row 11: warning — 警示（12帧：身体紧张、眼睁大、头顶 "!" 号闪烁）
# ════════════════════════════════════════════════════════════════

def draw_warning_sign(canvas, ox, oy, frame=0):
    """在头顶右侧绘制闪烁的 '!' 警示符号"""
    warn_patterns = [
        None, None, (10, 0), (10, 0), (10, 1), (10, 0),
        None, None, (10, 0), (10, 0), (10, 1), None,
    ]
    pos = warn_patterns[frame] if frame < len(warn_patterns) else None
    if pos is None:
        return
    wx, wy = pos
    # 感叹号主体
    for xy in [(wx, wy), (wx, wy+1), (wx, wy+2), (wx, wy+3)]:
        p_px(canvas, ox, oy, xy[0], xy[1], YELLOW_BRIGHT)
    # 感叹号下面的点
    p_px(canvas, ox, oy, wx, wy + 4, YELLOW_BRIGHT)

warning_frames = [
    # 0 警觉：眼睛睁大、耳朵竖立、身体直立、嘴微张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)],
     "ear_l": [(4,1),(5,1),(4,2),(5,2)], "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)]},
    # 1 紧张：张嘴、身体前倾
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 2 警示：头顶 "!" 闪烁
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 3 保持警觉
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(9,4)], "highlights": [(5,4),(10,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 4 快速左看
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(9,4)], "highlights": [(5,4),(10,4)],
     "mouth": [(7,7),(8,7)]},
    # 5 回正
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 6 眨眼一瞬
    {"eyes_white": [], "pupils": [], "highlights": [],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 7 睁大眼 + "!" 闪烁
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)]},
    # 8 快速右看
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(7,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(7,7),(8,7)]},
    # 9 嘴微张、紧张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)],
     "ear_l": [(4,1),(5,1),(4,2),(5,2)]},
    # 10 剧烈反应："!" + 嘴大张
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(6,7),(7,7),(8,7),(9,7)],
     "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)]},
    # 11 稍微放松但仍警觉
    {"eyes_white": [(5,4),(6,4),(7,4),(9,4),(10,4),(11,4)],
     "pupils": [(6,4),(10,4)], "highlights": [(5,4),(11,4)],
     "mouth": [(7,7),(8,7)]},
]
for f in range(12):
    kwargs = dict(warning_frames[f])
    # 非 None 的帧才画 "!" 警示符号
    warn_patterns = [None, None, 0, 0, 1, 0, None, 0, 0, 0, 1, None]
    if warn_patterns[f] is not None:
        draw_warning_sign(img, f * FRAME, 0 * FRAME, frame=f)
    draw_fox(img, f * FRAME, 11 * FRAME, cell_oy=11 * FRAME, **kwargs)

# ════════════════════════════════════════════════════════════════
# 保存
# ════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════
# 新增绘制辅助 — 耳机 / 玩耍球 / 音符飘动
# ════════════════════════════════════════════════════════════════

# 耳机配色（深灰机身 + 蓝色耳罩发光 + 橙色点缀）
HEADPHONE_BODY = (50, 55, 70)
HEADPHONE_CUP  = (70, 130, 220)
HEADPHONE_CUP_HI = (140, 200, 255)
HEADPHONE_PAD  = (35, 40, 55)

def draw_headphones(canvas, ox, oy, dy=0, bob=0):
    """在头顶绘制耳机：头梁横跨 + 两侧耳罩。

    dy: 身体上下浮动偏移；bob: 音乐律动额外偏移
    头梁 y=1~2 横跨 x=4~11；耳罩左右两侧 x=3~4 / x=11~12, y=3~5
    """
    bob_dy = dy + bob
    # ── 头梁（弧形，用两行像素表现）──
    for x in range(5, 11):
        p_px(canvas, ox, oy, x, 1 + bob_dy, HEADPHONE_BODY)
    for x in range(4, 12):
        p_px(canvas, ox, oy, x, 2 + bob_dy, HEADPHONE_BODY)
    # 头梁高光
    for x in range(6, 10):
        p_px(canvas, ox, oy, x, 1 + bob_dy, HEADPHONE_CUP_HI)

    # ── 左耳罩（x=3~4, y=3~5）──
    for x, y in [(3, 3 + bob_dy), (4, 3 + bob_dy),
                 (3, 4 + bob_dy), (4, 4 + bob_dy),
                 (3, 5 + bob_dy), (4, 5 + bob_dy)]:
        p_px(canvas, ox, oy, x, y, HEADPHONE_CUP)
    # 耳罩中心高光 + 耳垫
    p_px(canvas, ox, oy, 3, 4 + bob_dy, HEADPHONE_CUP_HI)
    p_px(canvas, ox, oy, 4, 5 + bob_dy, HEADPHONE_PAD)

    # ── 右耳罩（x=11~12, y=3~5）──
    for x, y in [(11, 3 + bob_dy), (12, 3 + bob_dy),
                 (11, 4 + bob_dy), (12, 4 + bob_dy),
                 (11, 5 + bob_dy), (12, 5 + bob_dy)]:
        p_px(canvas, ox, oy, x, y, HEADPHONE_CUP)
    p_px(canvas, ox, oy, 12, 4 + bob_dy, HEADPHONE_CUP_HI)
    p_px(canvas, ox, oy, 11, 5 + bob_dy, HEADPHONE_PAD)

def draw_play_ball(canvas, ox, oy, frame=0):
    """玩耍时的小球 — 在狐狸左侧弹跳，frame 控制高度。"""
    # 球的弹跳轨迹：高低高低
    ball_y = [11, 8, 5, 8, 11, 9, 6, 9, 11, 8, 5, 8]
    by = ball_y[frame] if frame < len(ball_y) else 11
    bx = 1  # 球在左侧 x=1~2
    # 球体（2×2 橙色）
    for x, y in [(bx, by), (bx + 1, by), (bx, by + 1), (bx + 1, by + 1)]:
        p_px(canvas, ox, oy, x, y, ORANGE_LIGHT)
    # 高光
    p_px(canvas, ox, oy, bx, by, YELLOW_BRIGHT)

def draw_music_notes_float(canvas, ox, oy, frame=0):
    """音乐模式下从头顶飘出的音符 — 两个音符交替升起。"""
    # 音符 A 升起轨迹（从右上耳罩上方飘向右上角）
    note_a = [(13, 2), (14, 1), (14, 0), (15, -1), None, None, None, None, None, None, None, None]
    # 音符 B 升起轨迹（从左上耳罩上方飘向左上角，错开节奏）
    note_b = [None, None, (1, 2), (0, 1), (0, 0), (-1, -1), None, None, None, None, None, None]
    # 第二轮音符
    note_a2 = [None, None, None, None, None, None, (13, 2), (14, 1), (14, 0), (15, -1), None, None]
    note_b2 = [None, None, None, None, None, None, None, None, (1, 2), (0, 1), (0, 0), (-1, -1)]

    pos_a = note_a[frame] if frame < len(note_a) else None
    pos_b = note_b[frame] if frame < len(note_b) else None
    pos_a2 = note_a2[frame] if frame < len(note_a2) else None
    pos_b2 = note_b2[frame] if frame < len(note_b2) else None

    for pos in (pos_a, pos_a2):
        if pos is not None:
            draw_music_note(canvas, ox, oy, pos[0], pos[1])
    for pos in (pos_b, pos_b2):
        if pos is not None:
            draw_music_note(canvas, ox, oy, pos[0], pos[1])

def draw_sound_wave(canvas, ox, oy, frame=0):
    """耳机两侧的小声波弧线 — 表现正在播放音乐。"""
    wave_patterns = [
        None, [(2, 4)], [(2, 4), (1, 5)], [(2, 4), (1, 5), (1, 3)],
        [(2, 4), (1, 5)], [(2, 4)], None, [(13, 4)],
        [(13, 4), (14, 5)], [(13, 4), (14, 5), (14, 3)], [(13, 4), (14, 5)], [(13, 4)],
    ]
    waves = wave_patterns[frame] if frame < len(wave_patterns) else None
    if waves is None:
        return
    for x, y in waves:
        p_px(canvas, ox, oy, x, y, HEADPHONE_CUP_HI)


# ════════════════════════════════════════════════════════════════
# Row 12: playing — 玩耍（追尾巴 + 跳跃 + 星星眼 + 球弹跳 + 火花，12帧）
# ════════════════════════════════════════════════════════════════
# 设计：身体上下跳跃律动 + 尾巴快速甩动 + 星星眼/开心眯眼交替 + 左侧小球弹跳 + 火花特效
playing_dys = [0, -2, -2, -1, 0, -2, -2, -1, 0, -2, -2, 0]
playing_frames = [
    # 0 起跳：星星眼 + 球在低位
    {"star_eyes": [(6, 5), (9, 5)], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST},
    # 1 最高点：眯眼笑 + 球弹起
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(4, 2), (12, 2)]},
    # 2 空中：星星眼 + 球到顶
    {"star_eyes": [(6, 5), (9, 5)], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3, 1), (13, 1), (5, 0), (11, 0)]},
    # 3 下落：睁眼 + 球回落
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(6, 5), (9, 5)],
     "highlights": [(7, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST},
    # 4 落地：张嘴笑 + 球低位
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)], "tail": TAIL_WAG_FAST,
     "sparkles": [(4, 3), (12, 3)]},
    # 5 再跳：星星眼 + 球弹起
    {"star_eyes": [(6, 5), (9, 5)], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "hearts": [(3, 3)]},
    # 6 最高：眯眼 + 球到顶 + 火花
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(2, 2), (14, 2), (4, 1), (12, 1)]},
    # 7 下落：睁眼追球
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(5, 5), (9, 5)],
     "highlights": [(6, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST},
    # 8 落地：张嘴 + 球低位
    {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3, 3), (13, 3)]},
    # 9 起跳：星星眼 + 球弹起
    {"star_eyes": [(6, 5), (9, 5)], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "hearts": [(12, 3)]},
    # 10 最高：眯眼 + 球到顶 + 双心
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3, 1), (13, 1), (5, 2), (11, 2)], "hearts": [(4, 3), (12, 3)]},
    # 11 收尾：睁眼 + 球低位
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(6, 5), (9, 5)],
     "highlights": [(7, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_HAPPY},
]
for f in range(12):
    dy = playing_dys[f]
    kwargs = dict(playing_frames[f])
    draw_fox(img, f * FRAME, 12 * FRAME + dy, cell_oy=12 * FRAME, **kwargs)
    # 左侧小球弹跳（独立于身体 dy）
    draw_play_ball(img, f * FRAME, 12 * FRAME, frame=f)

# ════════════════════════════════════════════════════════════════
# Row 13: music — 戴耳机听音乐（头顶耳机 + 音符飘出 + 身体律动，12帧）
# ════════════════════════════════════════════════════════════════
# 设计：头顶耳机（头梁+耳罩）+ 身体随节奏律动 + 音符从两侧飘出 + 闭眼享受/睁眼摇摆交替
# 律动 bob：轻微点头节奏（-1, 0, 1, 0 循环），叠加在身体 dy 上
music_bob = [0, -1, 1, 0, 0, -1, 1, 0, 0, -1, 1, 0]  # 点头律动
music_frames = [
    # 0 闭眼享受
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_UP},
    # 1 点头 + 眯眼
    {"eyes_white": [(6, 5), (7, 5)], "pupils": [(7, 5)], "highlights": [(6, 5)],
     "eyes_right_only": True, "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_DOWN},
    # 2 闭眼摇摆
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_UP},
    # 3 睁眼享受
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(7, 5), (9, 5)],
     "highlights": [(6, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_DOWN},
    # 4 闭眼陶醉
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_UP},
    # 5 点头 + 眯眼
    {"eyes_white": [(6, 5), (7, 5)], "pupils": [(7, 5)], "highlights": [(6, 5)],
     "eyes_right_only": True, "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_DOWN},
    # 6 闭眼 + 张嘴哼唱
    {"eyes_white": [], "pupils": [], "highlights": [],
     "mouth": [(7, 7), (8, 7), (9, 7)], "smile": [(6, 8)], "tail": TAIL_WAG_UP},
    # 7 睁眼 + 微笑
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(7, 5), (9, 5)],
     "highlights": [(6, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_DOWN},
    # 8 闭眼享受
    {"eyes_white": [], "pupils": [], "highlights": [],
     "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_UP},
    # 9 点头 + 眯眼
    {"eyes_white": [(6, 5), (7, 5)], "pupils": [(7, 5)], "highlights": [(6, 5)],
     "eyes_right_only": True, "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_DOWN},
    # 10 闭眼 + 哼唱
    {"eyes_white": [], "pupils": [], "highlights": [],
     "mouth": [(7, 7), (8, 7), (9, 7)], "smile": [(6, 8)], "tail": TAIL_WAG_UP},
    # 11 收尾：睁眼微笑
    {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(7, 5), (9, 5)],
     "highlights": [(6, 5), (10, 5)], "smile": [(6, 8), (9, 8)], "tail": TAIL_WAG_UP},
]
for f in range(12):
    bob = music_bob[f]
    kwargs = dict(music_frames[f])
    # 处理"仅右眼"眯眼（左眼闭、右眼睁）的临时 override
    if kwargs.pop("eyes_right_only", False):
        kwargs["eyes_white"] = [(9, 5), (10, 5)]
        kwargs["pupils"] = [(9, 5)]
        kwargs["highlights"] = [(10, 5)]
    draw_fox(img, f * FRAME, 13 * FRAME + bob, cell_oy=13 * FRAME, **kwargs)
    # 头顶耳机（跟随 bob 律动）
    draw_headphones(img, f * FRAME, 13 * FRAME, dy=0, bob=bob)
    # 音符飘出
    draw_music_notes_float(img, f * FRAME, 13 * FRAME, frame=f)
    # 声波弧线
    draw_sound_wave(img, f * FRAME, 13 * FRAME, frame=f)

# ════════════════════════════════════════════════════════════════
# Row 14: wakeup — 睡醒过渡（闭眼→半睁→全睁→哈欠→伸懒腰→清醒，12帧）
# ════════════════════════════════════════════════════════════════
# 设计：从 sleeping 姿态渐进苏醒，0-2 闭眼趴着 → 3-4 半睁眼 → 5-6 全睁惊讶
#       → 7-8 张大嘴哈欠 → 9-10 伸懒腰（身体抬起）→ 11 摇头清醒恢复站姿
# 前半段沿用 curled 趴姿，后半段切换为站立姿态（用 draw_fox）
wakeup_dys = [0, 0, -1, -1, 0, 0, -1, -2, -2, -1, 0, 0]

for f in range(12):
    ox = f * FRAME
    cell_oy = 14 * FRAME
    dy = wakeup_dys[f]

    if f <= 4:
        # 0-4：趴姿苏醒（沿用 draw_fox_curled 的轮廓，逐步睁眼）
        _set_cell_rect(ox, cell_oy, ox + FRAME - 1, cell_oy + FRAME - 1)
        oy = 14 * FRAME
        # 耳朵
        for x, y in [(4, 2 + dy), (5, 2 + dy), (10, 2 + dy), (11, 2 + dy)]:
            p_px(img, ox, oy, x, y, ORANGE)
        p_px(img, ox, oy, 5, 3 + dy, ORANGE_LIGHT)
        p_px(img, ox, oy, 10, 3 + dy, ORANGE_LIGHT)
        for x, y in [(4, 3 + dy), (11, 3 + dy)]:
            p_px(img, ox, oy, x, y, PINK_SOFT)
        # 头部
        for y in range(4 + dy, 8 + dy):
            for x in range(4, 12):
                p_px(img, ox, oy, x, y, ORANGE)
        for y in range(5 + dy, 8 + dy):
            for x in range(6, 10):
                p_px(img, ox, oy, x, y, CREAM)
        # 眼睛阶段：0-2 闭眼 / 3 半睁(细缝) / 4 睁开
        if f <= 2:
            for x, y in [(6, 6 + dy), (7, 6 + dy), (9, 6 + dy), (10, 6 + dy)]:
                p_px(img, ox, oy, x, y, DARK)
        elif f == 3:
            # 半睁：1px 细缝
            for x, y in [(7, 6 + dy), (9, 6 + dy)]:
                p_px(img, ox, oy, x, y, DARK)
        else:  # f == 4
            for x, y in [(6, 6 + dy), (7, 6 + dy), (9, 6 + dy), (10, 6 + dy)]:
                p_px(img, ox, oy, x, y, WHITE_PURE)
            p_px(img, ox, oy, 7, 6 + dy, DARK)
            p_px(img, ox, oy, 9, 6 + dy, DARK)
        # 嘴/鼻
        for x, y in [(6, 7 + dy), (10, 7 + dy)]:
            p_px(img, ox, oy, x, y, DARK)
        p_px(img, ox, oy, 8, 7 + dy, DARK)
        # 腮红
        if f >= 3:
            p_px(img, ox, oy, 5, 7 + dy, PINK_SOFT)
            p_px(img, ox, oy, 10, 7 + dy, PINK_SOFT)
        # 下半身
        for y in range(8 + dy, 11 + dy):
            for x in range(4, 12):
                p_px(img, ox, oy, x, y, ORANGE)
        for y in range(8 + dy, 11 + dy):
            for x in range(6, 10):
                p_px(img, ox, oy, x, y, CREAM)
        for x in range(4, 12):
            p_px(img, ox, oy, x, 11 + dy, ORANGE)
        # 尾巴
        for x, y in [(12, 8 + dy), (13, 8 + dy), (14, 8 + dy), (15, 8 + dy),
                     (12, 9 + dy), (13, 9 + dy), (14, 9 + dy), (15, 9 + dy),
                     (12, 10 + dy), (13, 10 + dy), (14, 10 + dy), (15, 10 + dy),
                     (12, 11 + dy), (13, 11 + dy), (14, 11 + dy), (15, 11 + dy)]:
            p_px(img, ox, oy, x, y, ORANGE)
        p_px(img, ox, oy, 14, 10 + dy, CREAM)
        p_px(img, ox, oy, 15, 10 + dy, CREAM)
        for x in range(5, 9):
            p_px(img, ox, oy, x, 12 + dy, ORANGE_MID)
    else:
        # 5-11：站立姿态苏醒（用 draw_fox）
        if f in (5, 6):
            # 全睁 + 惊讶
            kwargs = {"eyes_white": [(5, 4), (6, 4), (7, 4), (9, 4), (10, 4), (11, 4)],
                      "pupils": [(6, 4), (10, 4)], "highlights": [(5, 4), (11, 4)],
                      "mouth": [(7, 7), (8, 7)],
                      "ear_l": [(4, 1), (5, 1), (4, 2), (5, 2)],
                      "ear_r": [(10, 1), (11, 1), (10, 2), (11, 2), (9, 2)]}
        elif f in (7, 8):
            # 张大嘴哈欠
            kwargs = {"mouth": [(6, 7), (7, 7), (8, 7), (9, 7)],
                      "eyes_white": [], "pupils": [], "highlights": [],
                      "ear_l": [(4, 1), (5, 1), (4, 2), (5, 2)],
                      "ear_r": [(10, 1), (11, 1), (10, 2), (11, 2), (9, 2)],
                      "tail": TAIL_WAG_UP}
        elif f in (9, 10):
            # 伸懒腰：眯眼 + 微笑 + 尾巴翘
            kwargs = {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)],
                      "pupils": [(7, 5), (9, 5)], "highlights": [(6, 5), (10, 5)],
                      "smile": [(6, 8), (9, 8)], "tail": TAIL_HAPPY}
        else:  # f == 11
            # 清醒恢复：睁眼 + 微笑 + 摇头（耳朵抖动）
            kwargs = {"eyes_white": [(6, 5), (7, 5), (9, 5), (10, 5)], "pupils": [(7, 5), (9, 5)],
                      "highlights": [(6, 5), (10, 5)], "smile": [(6, 8), (9, 8)],
                      "ear_l": [(4, 1), (5, 1), (4, 2), (5, 2)],
                      "tail": TAIL_WAG_UP}
        draw_fox(img, ox, 14 * FRAME + dy, cell_oy=cell_oy, **kwargs)
        # 伸懒腰帧加一个上方小星星表示精神恢复
        if f == 10:
            draw_sparkle(img, ox, 14 * FRAME, 8, 0, YELLOW_BRIGHT)

# ════════════════════════════════════════════════════════════════
# 保存
# ════════════════════════════════════════════════════════════════
out_path = Path(__file__).parent / "icons" / "pet.png"
img.save(out_path)
print(f"✓ Spritesheet 已生成: {out_path} ({W}×{H}, {ROWS}行×{COLS}列)")
print(f"Done: {out_path}  ({W}×{H})")
