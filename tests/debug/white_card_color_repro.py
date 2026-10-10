# -*- coding: utf-8 -*-
"""白卡排查：离线复算 ensure_bubble_contrast 在 azure 主题与模板串污染下的输出。

只做 QColor 数学计算，不建 QApplication、不碰运行中的实例。
"""
import sys

sys.path.insert(0, r"D:\work\DriFox")

from PyQt5.QtGui import QColor

from app.widgets.modules.identity_header import _qcolor
from app.widgets.modules.message_bubble import ensure_bubble_contrast

# ── azure 主题实际值 ──
AZ_ASSIST = "rgba(222, 236, 250, 180)"
AZ_CONTENT = "#d6e2f2"
AZ_CARD_BG_TPL = "rgba(240, 247, 254, {alpha})"   # Colors.CARD_BG 模板（azure）
PURE_WHITE_TPL = "rgba(255, 255, 255, {alpha})"   # qt 日志里出现的白色模板

print("== azure 正常链路 ==")
print("assistant 气泡:", ensure_bubble_contrast(AZ_ASSIST, AZ_CONTENT, "assistant"))
print("user 气泡:", ensure_bubble_contrast("rgba(205, 225, 248, 180)", AZ_CONTENT, "user"))

print()
print("== 模板串解析行为 ==")
for name, tpl in (("azure CARD_BG 模板", AZ_CARD_BG_TPL), ("纯白模板", PURE_WHITE_TPL)):
    c = _qcolor(tpl, QColor(0, 0, 0, 0))
    print(f"{name}: isValid={c.isValid()} alpha={c.alpha()}")

print()
print("== 模板串若被当气泡色 ==")
print("纯白模板经 ensure:", ensure_bubble_contrast(PURE_WHITE_TPL, AZ_CONTENT, "assistant"))
print("azure模板经 ensure:", ensure_bubble_contrast(AZ_CARD_BG_TPL, AZ_CONTENT, "assistant"))

print()
print("== set_bubble_color 拿到模板串的最终态 ==")
fallback = _qcolor(PURE_WHITE_TPL, QColor(0, 0, 0, 0))
print("气泡实际画的颜色: alpha =", fallback.alpha(), "（0 = paintEvent 直接 return，不画）")

print()
print("== QColor(Qt.transparent) 的 getHslF（backdrop 异常时 assistant 分支走向） ==")
t = QColor(0, 0, 0, 0)
print("transparent getHslF:", t.getHslF())
print("bl=0 时 assistant l =", 0 + 14 / 255.0, "→ 深色而非白色，排除该分支")
