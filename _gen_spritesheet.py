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
ROWS = 10  # idle, thinking, streaming, question, success, error, sleeping, writing, thinking_hard, excited
W = FRAME * COLS  # 192
H = FRAME * ROWS  # 160

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
    """思考过度流汗"""
    sweat_pos = [None, (3,2), (2,2), (3,3), None, (2,1), (3,2), (2,3), None, (3,2), (2,2), None]
    pos = sweat_pos[frame] if frame < len(sweat_pos) else None
    if pos:
        p_px(canvas, ox, oy, pos[0], pos[1], TEAR_BLUE)

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
    # 9  半闭眼（补充帧）
    {"eyes_white": [(6,5),(7,5),(9,5)], "pupils": [(7,5)], "highlights": [(6,5)]},
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
    {"pupils": [(7,4)], "highlights": [(6,5)]},                               # 0 正常
    {"pupils": [(10,4)], "highlights": [(10,5)], "dot_frame": 1},             # 1 看右
    {"eyes_white": [], "pupils": [], "highlights": [], "dot_frame": 2},       # 2 闭眼思考
    {"pupils": [(6,5)], "highlights": [(6,5)], "dot_frame": 3},              # 3 看左
    {"pupils": [(7,4)], "highlights": [(6,5)]},                               # 4 正常
    {"eyes_white": [], "pupils": [], "highlights": [],                        # 5 歪头思考
     "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "dot_frame": 5},
    {"pupils": [(10,5)], "highlights": [(10,5)], "dot_frame": 6},             # 6 看右上
    {"eyes_white": [], "pupils": [], "highlights": [],                        # 7 闭眼+耳动
     "ear_l": [(4,1),(5,1),(4,2),(5,2)], "dot_frame": 7},
    {"pupils": [(7,4)], "highlights": [(6,5)], "dot_frame": 8},              # 8 正常+远点
    {"pupils": [(6,4)], "highlights": [(6,5)], "dot_frame": 9},              # 9 看左上
    {"eyes_white": [], "pupils": [], "highlights": [], "dot_frame": 10},      # 10 闭眼+两点
    {"pupils": [(7,4)], "highlights": [(6,5)], "dot_frame": 11},             # 11 回来
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
     "highlights": [(6,5),(9,5)], "tail": TAIL_WAG_DOWN},
    {"mouth": [(7,7),(8,7),(9,7)], "tail": TAIL_WAG_UP},                     # 2 中嘴
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 3 张嘴+闭右眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [], "tail": TAIL_WAG_DOWN},
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 4 大张嘴
     "tail": TAIL_WAG_UP, "tail_tip": [(15,7)]},
    {"mouth": [(7,7),(8,7)],                                                  # 5 小嘴+睁大眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [(6,5),(9,5)], "tail": TAIL_WAG_DOWN},
    {"mouth": [(6,7),(7,7),(8,7),(9,7)], "tail": TAIL_WAG_UP},               # 6 中张嘴
    {"mouth": [(7,7),(8,7)], "smile": [(6,8),(9,8)]},                        # 7 小嘴+微笑
    {"mouth": [(6,7),(7,7),(8,7),(9,7)],                                      # 8 补充：大张嘴眨眼
     "eyes_white": [(6,5),(7,5),(9,5),(10,5)], "pupils": [(6,5),(9,5)],
     "highlights": [(6,5),(9,5)]},
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
    {"q_frame": 0, "pupils": [(7,4)], "highlights": [(6,5)]},                  # 0
    {"q_frame": 1, "mouth": [(6,7),(7,7),(8,7),(9,7)]},                       # 1 好奇张嘴
    {"q_frame": 2, "eyes_white": [], "pupils": [], "highlights": []},          # 2 闭眼疑惑
    {"q_frame": 3, "pupils": [(6,5)], "highlights": [(6,5)]},                 # 3 左看
    {"q_frame": 4, "pupils": [(7,4)], "highlights": [(6,5)]},                  # 4 归位
    {"q_frame": 5, "pupils": [(10,4)], "highlights": [(10,5)]},                # 5 右看
    {"q_frame": 6, "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)],              # 6 歪头
     "pupils": [(10,4)], "highlights": [(10,5)]},
    {"q_frame": 7, "mouth": [(6,7),(7,7),(8,7),(9,7)],                        # 7 张嘴
     "pupils": [(7,4)], "highlights": [(6,5)]},
    {"q_frame": 8, "pupils": [(6,5)], "highlights": [(6,5)]},                 # 8 补充
    {"q_frame": 9, "eyes_white": [], "pupils": [], "highlights": []},          # 9 补充
    {"q_frame": 10, "pupils": [(10,4)], "highlights": [(10,5)]},               # 10 补充
    {"q_frame": 11, "pupils": [(7,4)], "highlights": [(6,5)]},                 # 11 结束
]
for f in range(12):
    draw_fox(img, f * FRAME, 3 * FRAME, cell_oy=3 * FRAME, **question_frames[f])

# ════════════════════════════════════════════════════════════════
# Row 4: success — 笑眼 + 星星眼 + 烟花散开 + 小心心（12帧）
# ════════════════════════════════════════════════════════════════
success_frames = [
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 0
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
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 5
     "smile": [(6,8),(9,8)], "sparkles": [(3,2),(13,2),(6,3),(10,3)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 6 尾巴翘起
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY, "sparkles": [(4,3),(12,3)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 7
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                            # 8 补充
     "smile": [(6,8),(9,8)], "sparkles": [(5,2),(11,2)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 9 补充
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY, "hearts": [(4,3)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 10
     "smile": [(6,8),(9,8)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],   # 11 结束微笑
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
# Row 6: sleeping — 闭眼 + 呼吸 + Zzz 浮动（12帧）
# ════════════════════════════════════════════════════════════════
sleep_eyes = {"eyes_white": [(6,5),(7,5)], "pupils": [(6,5),(7,5)], "highlights": []}
zzz_seq_per_frame = [None, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, None]
for f in range(12):
    kwargs = dict(sleep_eyes)
    zz = zzz_seq_per_frame[f]
    if zz is not None: kwargs["zzz_seq"] = zz
    draw_fox(img, f * FRAME, 6 * FRAME, cell_oy=6 * FRAME, **kwargs)

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
    {"pupils": [(6,4)], "highlights": [(6,5)], "sweat": True, "sweat_frame": 0}, # 0
    {"pupils": [(10,4)], "highlights": [(10,5)], "sweat": True, "sweat_frame": 1}, # 1
    {"eyes_white": [], "pupils": [], "highlights": [], "sweat": True, "sweat_frame": 2}, # 2
    {"pupils": [(6,5)], "highlights": [(6,5)], "sweat": True, "sweat_frame": 3}, # 3
    {"pupils": [(7,4)], "highlights": [(6,5)], "sweat": True, "sweat_frame": 4}, # 4 休息
    {"eyes_white": [], "pupils": [], "highlights": [],                               # 5 闭眼用力
     "ear_r": [(10,1),(11,1),(10,2),(11,2),(9,2)], "sweat": True, "sweat_frame": 5},
    {"pupils": [(10,5)], "highlights": [(10,5)], "sweat": True, "sweat_frame": 6}, # 6
    {"eyes_white": [], "pupils": [], "highlights": [],                               # 7 闭眼+耳动
     "ear_l": [(4,1),(5,1),(4,2),(5,2)], "sweat": True, "sweat_frame": 7},
    {"pupils": [(7,4)], "highlights": [(6,5)], "sweat": True, "sweat_frame": 8}, # 8
    {"pupils": [(10,4)], "highlights": [(10,5)], "sweat": True, "sweat_frame": 9}, # 9
    {"eyes_white": [], "pupils": [], "highlights": [], "sweat": True, "sweat_frame": 10}, # 10
    {"pupils": [(7,4)], "highlights": [(6,5)], "sweat": True, "sweat_frame": 11}, # 11
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
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],      # 3 下落
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(4,2),(12,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 4 落地
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 5 跳起
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(5,3),(11,3)], "music_notes": [(3,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 6 最高
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(2,2),(14,2),(4,1),(12,1)], "hearts": [(6,3)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],      # 7 下落
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 8 落地
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3,3),(13,3)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 9 跳起
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "music_notes": [(2,2),(14,2)]},
    {"star_eyes": [(6,5),(9,5)], "highlights": [],                               # 10 最高
     "smile": [(6,8),(9,8)], "tail": TAIL_WAG_FAST,
     "sparkles": [(3,1),(13,1),(5,2),(11,2)], "hearts": [(4,3),(12,3)]},
    {"eyes_white": [(6,5),(7,5)], "pupils": [(7,5)], "highlights": [(6,5)],      # 11 收尾
     "smile": [(6,8),(9,8)], "tail": TAIL_HAPPY},
]
for f in range(12):
    dy = excited_dys[f]
    draw_fox(img, f * FRAME, 9 * FRAME + dy, cell_oy=9 * FRAME, **excited_frames[f])

# ════════════════════════════════════════════════════════════════
# 保存
# ════════════════════════════════════════════════════════════════
out_path = Path(__file__).parent / "icons" / "pet.png"
img.save(out_path)
print(f"Done: {out_path}  ({W}×{H})")
