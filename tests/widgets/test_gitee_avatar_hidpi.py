# -*- coding: utf-8 -*-
"""_AvatarCircleWidget 高分屏渲染回归测试

背景（2026-10-27）：真实头像在高分屏（dpr>1）上发糊，修法是把缩放目标从
「逻辑尺寸」改成「物理尺寸 + setDevicePixelRatio(dpr)」。改动过程中踩到两个坑，
这两个坑都不会让代码报错、只会静默画错，故在此用像素级断言锁住：

1. 源矩形单位错配 —— drawPixmap(point, pm, sourceRect) 在 pm 带 dpr 时按**物理
   像素**读 sourceRect。传逻辑坐标（如 26×26）会被当成 26 个物理像素，只取到
   原图左上角一小块再放大铺满整圆，表现为「头像只剩左上角一点点」。
2. 缩放目标用逻辑尺寸 —— 等于先降采样再让 QPainter 的 DPR 变换放大回去，二次插值。

两个坑在非高分屏（dpr=1）下都不可见，测试必须显式构造 dpr>1 的场景。

断言手段：四象限染色图（左上红/右上绿/左下蓝/右下黄），渲染后统计每个象限的
主色。content-agnostic —— 不依赖具体头像资源。
"""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.widgets.cards.settings.gitee_card import _AvatarCircleWidget  # noqa: E402

_QCOLORS = {
    "TL": QColor(255, 0, 0),
    "TR": QColor(0, 255, 0),
    "BL": QColor(0, 0, 255),
    "BR": QColor(255, 255, 0),
}

_SRC_PX = 512


def _quadrant_source(aspect_divisor: int = 1) -> QPixmap:
    """四象限染色图。aspect_divisor=2 生成 2:1 宽图，用于 cover-crop 场景。"""
    w, h = _SRC_PX, _SRC_PX // aspect_divisor
    img = QImage(w, h, QImage.Format_RGB32)
    for y in range(h):
        for x in range(w):
            key = ("T" if y < h / 2 else "B") + ("L" if x < w / 2 else "R")
            img.setPixel(x, y, _QCOLORS[key].rgb())
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(1.0)
    return pm


def _dominant_quadrant_colors(pixmap: QPixmap) -> dict:
    """渲染结果每个象限的平均色（忽略近透明像素，避免圆外底色污染统计）"""
    img = pixmap.toImage()
    w, h = img.width(), img.height()
    acc = {k: [0, 0, 0, 0] for k in _QCOLORS}
    for y in range(h):
        for x in range(w):
            c = img.pixelColor(x, y)
            if c.alpha() < 128:
                continue
            k = ("T" if y < h / 2 else "B") + ("L" if x < w / 2 else "R")
            acc[k][0] += c.red()
            acc[k][1] += c.green()
            acc[k][2] += c.blue()
            acc[k][3] += 1
    out = {}
    for k, (r, g, b, n) in acc.items():
        if n == 0:
            out[k] = None
        else:
            out[k] = (round(r / n), round(g / n), round(b / n))
    return out


def _channel_of_dominant(rgb) -> str:
    """返回主色 dominant channel 名：红→r / 绿→g / 蓝→b / 黄→rg"""
    if rgb is None:
        return "none"
    r, g, b = rgb
    # 相对判定，容忍圆形边缘抗锯齿混色导致的整体亮度偏移
    thresh = max(r, g, b) * 0.55
    hits = "".join(c for c, v in (("r", r), ("g", g), ("b", b)) if v >= thresh)
    return hits


_EXPECTED_CHANNELS = {"TL": "r", "TR": "g", "BL": "b", "BR": "rg"}


@pytest.mark.parametrize("size", [28, 44])
def test_avatar_all_quadrants_rendered(size, qtbot):
    """四象限必须全部出现在渲染结果里

    这是坑①的回归：sourceRect 单位错配时只有左上象限有内容，
    其余象限被 widget 底色填充（颜色与任何象限色都不匹配）。

    注：此处未参数化 dpr —— widget.grab() 走真实屏幕 dpr，脚本里改不动。
    dpr 相关的行为由 test_scaled_for_targets_physical_pixels 用 fake device 覆盖。
    """
    widget = _AvatarCircleWidget("?")
    qtbot.addWidget(widget)
    widget.set_size(size)
    widget.set_image(_quadrant_source())

    rendered = widget.grab()
    got = _dominant_quadrant_colors(rendered)
    channels = {k: _channel_of_dominant(v) for k, v in got.items()}

    assert channels == _EXPECTED_CHANNELS, (
        f"size={size} 象限主色不符（疑似 sourceRect 单位错配）: "
        f"期望 {_EXPECTED_CHANNELS}，实际 {channels}；原始均值 {got}"
    )


@pytest.mark.parametrize("size", [28, 44])
def test_avatar_cover_crop_keeps_center(size, qtbot):
    """非正方形源按 cover 居中裁剪：横向居中的两个象限色都应可见

    2:1 宽图 cover 到正方形时应裁掉左右两侧、保留中央。均可-tolerant 用
    「上下两条带的主色」判定而非精确裁剪比例。
    """
    widget = _AvatarCircleWidget("?")
    qtbot.addWidget(widget)
    widget.set_size(size)
    widget.set_image(_quadrant_source(aspect_divisor=2))

    got = _dominant_quadrant_colors(widget.grab())
    for k, expect_ch in _EXPECTED_CHANNELS.items():
        assert _channel_of_dominant(got[k]) == expect_ch, (
            f"size={size} 象限 {k} 主色不符: 期望 {expect_ch}，实际 {got[k]}"
        )


def test_scaled_for_targets_physical_pixels(qtbot):
    """_scaled_for 的输出必须是物理像素且带 dpr（坑②的回归）

    dpr=2.25、逻辑边长 42 → 物理 94（或 95，取决于取整），且 pixmap 的
    devicePixelRatio 必须等于 2.25。若退回按逻辑尺寸缩放，宽会是 42 且 dpr=1。
    """

    class _FakeDevice:
        def __init__(self, r):
            self._r = r

        def devicePixelRatioF(self):
            return self._r

    widget = _AvatarCircleWidget("?")
    qtbot.addWidget(widget)
    widget.set_image(_quadrant_source())

    side = 42
    dpr = 2.25
    out = widget._scaled_for(QRectF(1, 1, side, side), _FakeDevice(dpr))

    assert out.width() != side, "缩放目标仍是逻辑尺寸 —— 高分屏会走二次插值"
    assert abs(out.width() - side * dpr) <= 2, f"物理尺寸偏差过大: {out.width()} != {side * dpr}"
    assert abs(out.devicePixelRatio() - dpr) < 1e-3, f"dpr 未标注到位: {out.devicePixelRatio()}"


def test_scaled_cache_keyed_by_dpr(qtbot):
    """缓存按 (边长, dpr) 键失效：换 dpr 必须重算，同参数必须命中"""

    class _FakeDevice:
        def __init__(self, r):
            self._r = r

        def devicePixelRatioF(self):
            return self._r

    widget = _AvatarCircleWidget("?")
    qtbot.addWidget(widget)
    widget.set_image(_quadrant_source())
    rect = QRectF(1, 1, 42, 42)

    first = widget._scaled_for(rect, _FakeDevice(2.25))
    assert widget._scaled_for(rect, _FakeDevice(2.25)) is first, "同参数未命中缓存"

    other = widget._scaled_for(rect, _FakeDevice(1.0))
    assert other is not first, "dpr 变化未使缓存失效"
    assert other.width() != first.width()

    widget.set_image(_quadrant_source())
    assert widget._scaled_key is None, "set_image 未清缓存"
