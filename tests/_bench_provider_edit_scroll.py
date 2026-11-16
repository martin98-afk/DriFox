# -*- coding: utf-8 -*-
"""[bench] 服务商编辑卡内部滚轮滚动成本基线（离屏）。

度量三个口径：
1. 每滚动步耗时（ms）—— 直接反映滚轮手感
2. 每步 paint 事件数 —— 全视口重绘时每步几十次
3. 每步重绘像素 / 视口面积（倍数）—— blit 生效时 ≈ 0.2，退化时 ≈ 1.0+

配置对比：
  A 现状（content_widget QSS transparent + make_scroll_content_opaque）
  B 清掉 content_widget 的 QSS 透明声明（让 palette 实底接管）
  C B + 滚动区换原生滚动条（隐藏 qfw 自绘条 + SmoothScrollDelegate）

运行：python tests/_bench_provider_edit_scroll.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402


class ProbeApp(QApplication):
    """统计所有 Paint 事件的数量与总面积。"""

    def __init__(self, argv):
        super().__init__(argv)
        self.paints = 0
        self.paint_px = 0
        self.paint_widgets = {}

    def notify(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Paint:
            try:
                r = ev.region().boundingRect()
                area = max(0, r.width()) * max(0, r.height())
            except Exception:
                area = 0
            self.paints += 1
            self.paint_px += area
            key = type(obj).__name__
            self.paint_widgets[key] = self.paint_widgets.get(key, 0) + 1
        return super().notify(obj, ev)

    def reset(self):
        self.paints = 0
        self.paint_px = 0
        self.paint_widgets = {}


def build_card(app):
    from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
    from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

    card = BaseSettingsCard("服务商配置", icon_svg="大模型")
    card.resize(760, 640)
    popup = ProviderEditCard(parent=card, is_new=True)
    card.content_layout.addWidget(popup)
    card.show()
    app.processEvents()
    return card, popup


def drain(app, ms=120):
    """跑事件循环 ms 毫秒（推进平滑滚动动画）。"""
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) * 1000 < ms:
        app.processEvents()
        time.sleep(0.001)


def measure_steps(app, card, steps=25, step_px=48):
    """程序化步进滚动，统计每步 paint 与耗时。"""
    sa = card.scroll_area
    sb = sa.verticalScrollBar()
    vp = sa.viewport()
    vp_area = max(1, vp.width() * vp.height())
    sb.setValue(0)
    drain(app, 80)
    app.reset()

    t0 = time.perf_counter()
    paints = 0
    px = 0
    done = 0
    for _ in range(steps):
        v = sb.value() + step_px
        if v > sb.maximum():
            break
        sb.setValue(v)
        app.processEvents()
        done += 1
    dt = (time.perf_counter() - t0) * 1000
    paints = app.paints
    px = app.paint_px
    per_step_ms = dt / max(1, done)
    return {
        "steps": done,
        "ms_per_step": per_step_ms,
        "paints_per_step": paints / max(1, done),
        "px_ratio": px / max(1, vp_area * done),
        "vp": (vp.width(), vp.height()),
        "range": (sb.value(), sb.maximum()),
        "widgets": dict(sorted(app.paint_widgets.items(), key=lambda kv: -kv[1])[:6]),
    }


def measure_wheel(app, card, notches=8):
    """真实滚轮事件，统计一屏滚动的总耗时与 paint 成本。"""
    sa = card.scroll_area
    vp = sa.viewport()
    vp_area = max(1, vp.width() * vp.height())
    sa.verticalScrollBar().setValue(0)
    drain(app, 200)
    app.reset()

    t0 = time.perf_counter()
    for _ in range(notches):
        pos = QPointF(vp.width() / 2.0, vp.height() / 2.0)
        ev = QWheelEvent(
            pos,
            QPointF(vp.mapToGlobal(QPoint(int(pos.x()), int(pos.y())))),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        app.sendEvent(vp, ev)
        drain(app, 60)
    drain(app, 400)  # 等平滑滚动收尾
    dt = (time.perf_counter() - t0) * 1000
    touched = sa.verticalScrollBar().value()
    return {
        "ms_total": dt,
        "paints": app.paints,
        "px_ratio": app.paint_px / vp_area,
        "moved": touched,
        "widgets": dict(sorted(app.paint_widgets.items(), key=lambda kv: -kv[1])[:6]),
    }


def variant_current(card, app):
    return "A 现状"


def variant_drop_qss(card, app):
    """清掉内容层 QSS 透明声明，重新刷 palette 实底。"""
    from app.utils.design_tokens import Colors, make_scroll_content_opaque

    cw = card.content_widget
    cw.setStyleSheet("")
    make_scroll_content_opaque(cw, Colors.CARD_BG.format(alpha=255), 230, Colors.CONTENT_BG)
    app.processEvents()
    return "B 去 content QSS 透明"


def variant_native_bar(card, app):
    """再换回原生滚动条（绕开 qfw SmoothScrollDelegate 自绘条）。"""
    from PyQt5.QtWidgets import QAbstractScrollArea, QScrollBar

    sa = card.scroll_area
    QAbstractScrollArea.setVerticalScrollBar(sa, QScrollBar(Qt.Vertical, sa))
    app.processEvents()
    return "C B+原生滚动条"


def main():
    app = ProbeApp(sys.argv)
    rows = []

    for mut in (variant_current, variant_drop_qss, variant_native_bar):
        card, popup = build_card(app)
        name = mut(card, app)
        drain(app, 150)
        s = measure_steps(app, card)
        w = measure_wheel(app, card)
        rows.append((name, s, w))
        card.setVisible(False)
        card.deleteLater()
        app.processEvents()

    print("=" * 78)
    for name, s, w in rows:
        print(f"[{name}]")
        print(
            f"  步进: {s['steps']} 步  每步 {s['ms_per_step']:.2f}ms  "
            f"paint/步 {s['paints_per_step']:.1f}  重绘面积/视口 {s['px_ratio']:.2f}x  "
            f"viewport={s['vp']}  range={s['range']}"
        )
        print(f"        top paint: {s['widgets']}")
        print(
            f"  滚轮: {w['ms_total']:.0f}ms  paint {w['paints']} 次  "
            f"重绘面积/视口 {w['px_ratio']:.2f}x  位移 {w['moved']}px"
        )
        print(f"        top paint: {w['widgets']}")
    print("=" * 78)


if __name__ == "__main__":
    main()
