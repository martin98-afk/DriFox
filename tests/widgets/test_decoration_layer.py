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


class TestDecorationScaling:
    """fit_width/fit_height：装饰件按容器等比缩放，避免大图超出可视区域

    历史行为是永远按 pixmap 原尺寸摆放。1600×727 的角色图挂到 800 宽的
    对话区上会左右各溢出一半，用户侧表现为「人物跑出屏幕」。
    """

    @pytest.fixture()
    def small_host(self, host):
        """20×8 的小图宿主（复用外层 host fixture，图片本来就小）"""
        return host

    @pytest.fixture()
    def big_host(self, qapp, tmp_path):
        from PyQt5.QtGui import QImage
        from PyQt5.QtWidgets import QWidget

        host = QWidget()
        host.resize(1200, 900)
        host.chat_container = QWidget(host)
        host.chat_container.setGeometry(0, 0, 800, 600)
        host._input_card = QWidget(host)
        host._input_card.setGeometry(200, 700, 600, 120)

        png = tmp_path / "big.png"
        QImage(1600, 727, QImage.Format_ARGB32).save(str(png))

        deco = DecorationLayer(host, ref_resolver=lambda attr: getattr(host, attr, None))
        host._deco = deco
        host._png = str(png)
        yield host
        host.deleteLater()
        deco.deleteLater()

    def test_without_fit_keeps_original_size(self, big_host):
        """不写 fit 字段时保持历史行为：原尺寸，可能溢出"""
        deco = big_host._deco
        deco.apply_config(
            [{"id": "s", "image": big_host._png, "anchor": "scene_bottom"}],
            image_resolver=lambda img: big_host._png,
        )
        label = deco._items[0]["label"]
        assert (label.width(), label.height()) == (1600, 727)

    def test_fit_width_scales_down_to_ref_width(self, big_host):
        """fit_width=true：宽度收到 ref 宽（800），高度等比"""
        deco = big_host._deco
        deco.apply_config(
            [{"id": "s", "image": big_host._png, "anchor": "scene_bottom", "fit_width": True}],
            image_resolver=lambda img: big_host._png,
        )
        label = deco._items[0]["label"]
        assert label.width() == 800
        assert label.height() == int(727 * 800 / 1600)

    def test_fit_never_upscales_small_image(self, small_host):
        """原图比容器小时不放大（避免糊）"""
        deco = small_host._deco
        deco.apply_config(
            [{"id": "s", "image": small_host._png, "anchor": "scene_bottom", "fit_width": True}],
            image_resolver=lambda img: small_host._png,
        )
        label = deco._items[0]["label"]
        assert (label.width(), label.height()) == (20, 8)

    def test_fit_height_caps_to_ref_height(self, big_host):
        """fit_height=true：高度收到 ref 高，宽度等比"""
        deco = big_host._deco
        big_host.chat_container.setGeometry(0, 0, 800, 300)
        deco.apply_config(
            [{"id": "s", "image": big_host._png, "anchor": "scene_bottom", "fit_height": True}],
            image_resolver=lambda img: big_host._png,
        )
        label = deco._items[0]["label"]
        assert label.height() == 300
        assert label.width() == int(1600 * 300 / 727)

    def test_fit_width_centers_horizontally(self, big_host):
        """缩放后仍按 anchor 居中，左缘落在 ref 内"""
        deco = big_host._deco
        deco.apply_config(
            [{"id": "s", "image": big_host._png, "anchor": "scene_bottom", "fit_width": True}],
            image_resolver=lambda img: big_host._png,
        )
        label = deco._items[0]["label"]
        assert label.x() == 0
        assert label.x() + label.width() <= big_host.chat_container.width()

    def test_fit_width_stays_inside_after_resize(self, big_host):
        """容器变窄后重新定位，仍不溢出"""
        deco = big_host._deco
        big_host.chat_container.setGeometry(0, 0, 400, 600)
        deco.apply_config(
            [{"id": "s", "image": big_host._png, "anchor": "scene_bottom", "fit_width": True}],
            image_resolver=lambda img: big_host._png,
        )
        label = deco._items[0]["label"]
        assert label.width() == 400
        assert label.x() == 0
