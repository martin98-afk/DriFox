# -*- coding: utf-8 -*-
"""DecorationLayer 回归测试

核心回归：_position_item 曾用 ref.mapTo(self, ...)，而 self（DecorationLayer）
与 ref（chat_container/_input_card）是兄弟节点（同为 host 子 widget），
违反 QWidget::mapTo 的祖先契约 → Qt 5.15 下 access violation 进程闪退
（aurora 主题首次启用 decorations 后，新建会话/新建窗口触发
_apply_chat_backgrounds 即崩）。修复为 ref.mapTo(self._host, ...)。
"""

import pytest

from app.widgets.decoration_layer import DecorationLayer


@pytest.fixture()
def host(qapp, tmp_path):
    """模拟 chat_area_module 的挂载结构：chat_container/_input_card 是 host 的子 widget（兄弟节点）"""
    from PyQt5.QtWidgets import QWidget

    host = QWidget()
    host.resize(960, 720)
    host.chat_container = QWidget(host)
    host.chat_container.setGeometry(8, 8, 800, 500)
    host._input_card = QWidget(host)
    host._input_card.setGeometry(100, 520, 600, 120)

    # 有效图片（apply_config 需 pixmap 非 null 才会走到 _position_item）
    png = tmp_path / "dec.png"
    from PyQt5.QtGui import QImage

    QImage(20, 8, QImage.Format_ARGB32).save(str(png))

    deco = DecorationLayer(host, ref_resolver=lambda attr: getattr(host, attr, None))
    host._deco = deco
    host._png = str(png)
    yield host
    host.deleteLater()
    deco.deleteLater()


class TestDecorationPositioning:
    def test_anchor_input_bottom_no_crash(self, host):
        """兄弟 ref（input_bottom）定位不崩溃且坐标正确（修复 mapTo 契约违规）"""
        deco = host._deco
        deco.apply_config(
            [{"id": "glow", "image": host._png, "anchor": "input_bottom", "offset": [0, -4], "opacity": 0.75}],
            image_resolver=lambda img: host._png,
        )
        assert len(deco._items) == 1
        label = deco._items[0]["label"]
        # input_card 底边(520+120=640) - 图片高 8 + offset -4，水平居中
        assert label.y() == 640 - 8 - 4
        assert label.x() == 100 + (600 - 20) // 2

    def test_anchor_scene_bottom_no_crash(self, host):
        """兄弟 ref（scene_bottom，ref=chat_container）定位不崩溃"""
        deco = host._deco
        deco.apply_config(
            [{"id": "s", "image": host._png, "anchor": "scene_bottom", "offset": [0, 0]}],
            image_resolver=lambda img: host._png,
        )
        label = deco._items[0]["label"]
        # chat_container 底边(8+500=508) - 图片高 8
        assert label.y() == 508 - 8

    def test_reapply_and_resize(self, host):
        """重应用 + resize（新建会话路径）不崩溃，位置随 ref 变化同步"""
        deco = host._deco
        cfg = [{"id": "glow", "image": host._png, "anchor": "input_bottom", "offset": [0, -4]}]
        deco.apply_config(cfg, image_resolver=lambda img: host._png)
        host._input_card.move(100, 540)
        deco.apply_config(cfg, image_resolver=lambda img: host._png)
        label = deco._items[0]["label"]
        assert label.y() == 540 + 120 - 8 - 4
