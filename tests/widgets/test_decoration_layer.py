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
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    host.resize(960, 720)
    host.chat_container = QWidget(host)
    host.chat_container.setGeometry(8, 8, 800, 500)
    host._input_card = QWidget(host)
    host._input_card.setGeometry(100, 520, 600, 120)

    # 有效图片（apply_config 需 pixmap 非 null 才会走到 _position_item）
    png = tmp_path / "dec.png"
    from PySide6.QtGui import QImage

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
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QWidget

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


class TestDecorationScrollAnchor:
    """scene_* 锚点必须相对滚动视口固定，而不是相对滚动的 chat_container

    历史实现直接对 chat_container（QScrollArea 的内容 widget）定位：
    - 消息越多 chat_container 越高，scene_bottom 的 y 跑到几千像素外
    - 滚动时 chat_container 相对 host 的位置变化，装饰件跟着上下移动
    用户侧表现：「装饰层老是乱跳」+「人物跑出可视区」。
    """

    @pytest.fixture()
    def scroll_host(self, qapp, tmp_path):
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QScrollArea, QWidget

        host = QWidget()
        host.resize(1000, 800)

        area = QScrollArea(host)
        area.setGeometry(20, 20, 600, 400)
        area.setWidgetResizable(True)
        content = QWidget()
        content.resize(600, 5000)  # 内容远高于视口，模拟长对话
        area.setWidget(content)
        host.show()
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()  # 让 QScrollArea 完成布局，viewport 才有真实尺寸
        host.chat_scroll_area = area
        host.chat_container = content
        host._input_card = QWidget(host)
        host._input_card.setGeometry(20, 500, 600, 120)

        png = tmp_path / "s.png"
        QImage(200, 100, QImage.Format_ARGB32).save(str(png))

        deco = DecorationLayer(host, ref_resolver=lambda attr: getattr(host, attr, None))
        host._deco = deco
        host._png = str(png)
        yield host
        host.deleteLater()
        deco.deleteLater()

    def test_scene_bottom_clamps_to_viewport_not_content(self, scroll_host):
        """scene_bottom 落在视口底部附近，而不是 5000px 内容底部"""
        deco = scroll_host._deco
        deco.apply_config(
            [{"id": "s", "image": scroll_host._png, "anchor": "scene_bottom"}],
            image_resolver=lambda img: scroll_host._png,
        )
        label = deco._items[0]["label"]
        viewport_h = scroll_host.chat_scroll_area.viewport().height()
        assert label.y() < 20 + viewport_h + 100
        assert label.y() > 0

    def test_scene_top_uses_viewport_top(self, scroll_host):
        """scene_top 贴视口顶部（允许 1px 边框误差），不受内容偏移影响"""
        deco = scroll_host._deco
        deco.apply_config(
            [{"id": "s", "image": scroll_host._png, "anchor": "scene_top"}],
            image_resolver=lambda img: scroll_host._png,
        )
        label = deco._items[0]["label"]
        assert abs(label.y() - 20) <= 2

    def test_scroll_does_not_move_decoration(self, scroll_host):
        """滚动后装饰件位置不变（相对视口固定）"""
        deco = scroll_host._deco
        deco.apply_config(
            [{"id": "s", "image": scroll_host._png, "anchor": "scene_bottom"}],
            image_resolver=lambda img: scroll_host._png,
        )
        label = deco._items[0]["label"]
        before = label.geometry()
        scroll_host.chat_scroll_area.verticalScrollBar().setValue(1200)
        deco.sync_now()
        assert label.geometry() == before

    def test_scene_anchor_sizes_to_viewport(self, scroll_host):
        """anchor=scene 撑满视口，而不是撑满 5000px 高内容"""
        deco = scroll_host._deco
        deco.apply_config(
            [{"id": "s", "image": scroll_host._png, "anchor": "scene"}],
            image_resolver=lambda img: scroll_host._png,
        )
        label = deco._items[0]["label"]
        vp = scroll_host.chat_scroll_area.viewport()
        assert label.height() == vp.height()
        assert label.width() == vp.width()


class TestDecorationHiddenHost:
    """宿主不可见（未 show / 切页后 hide）时仍必须按视口定位

    回归背景：_scene_ref_rect 曾用 `viewport.isVisible()` 判断是否可用视口
    矩形。isVisible() 在两种状态下为 False：
      - 窗口尚未 show（chat_area 的 QTimer.singleShot(0) 首刷早于首帧）
      - 切页后旧窗口被 hide
    两种情况都会退回 chat_container（QScrollArea 的内容 widget），而它的
    高度随消息增长，于是装饰件被摆到几千像素外或退化到错误尺寸。用户侧
    表现：「装了插件装饰层不立即显示」「新建会话位置是歪的」「切会话残留」。

    正确判据是 isVisibleTo()：它只看自身相对父级的可见性，不受祖先是否
    显示影响，hide 状态下布局尺寸依然有效。
    """

    @pytest.fixture()
    def hidden_host(self, qapp, tmp_path):
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QScrollArea, QWidget

        host = QWidget()
        host.resize(1000, 800)

        area = QScrollArea(host)
        area.setGeometry(20, 20, 600, 400)
        area.setWidgetResizable(True)
        content = QWidget()
        content.resize(600, 5000)
        area.setWidget(content)
        # 关键：不 show，模拟「首刷发生在首帧之前」
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        host.chat_scroll_area = area
        host.chat_container = content
        host._input_card = QWidget(host)
        host._input_card.setGeometry(20, 500, 600, 120)

        png = tmp_path / "h.png"
        QImage(200, 100, QImage.Format_ARGB32).save(str(png))

        deco = DecorationLayer(host, ref_resolver=lambda attr: getattr(host, attr, None))
        host._deco = deco
        host._png = str(png)
        yield host
        host.deleteLater()
        deco.deleteLater()

    def test_hidden_host_uses_viewport_not_content(self, hidden_host):
        """未 show 时 scene_bottom 仍落在视口底部附近，而不是 5000px 内容底部"""
        deco = hidden_host._deco
        deco.apply_config(
            [{"id": "s", "image": hidden_host._png, "anchor": "scene_bottom"}],
            image_resolver=lambda img: hidden_host._png,
        )
        label = deco._items[0]["label"]
        area = hidden_host.chat_scroll_area
        # 未 show 时 viewport 自身尺寸还是默认小值（实测 98x28），实现会用
        # QScrollArea.contentsRect 兜底，因此期望值也按 contentsRect 算。
        vp_top = area.viewport().mapTo(hidden_host, area.viewport().rect().topLeft()).y()
        cr = area.contentsRect()
        expected = vp_top + cr.height() - label.height()
        assert abs(label.y() - expected) <= 2, (
            f"未 show 时按内容容器定位了：label.y={label.y()} 期望≈{expected}"
        )

    def test_hidden_host_fit_width_uses_viewport_width(self, hidden_host):
        """未 show 时 fit_width 按视口宽缩放，不能退化成内容宽或 0"""
        deco = hidden_host._deco
        deco.apply_config(
            [
                {
                    "id": "s",
                    "image": hidden_host._png,
                    "anchor": "scene_bottom",
                    "fit_width": True,
                }
            ],
            image_resolver=lambda img: hidden_host._png,
        )
        label = deco._items[0]["label"]
        vp = hidden_host.chat_scroll_area.viewport()
        # 原图 200 宽 < 视口 600 宽，只缩不放 → 保持 200
        assert label.width() == 200

    def test_hide_after_show_keeps_viewport_basis(self, qapp, tmp_path):
        """show→hide 之后重新定位，仍以视口为基准（切页残留场景）"""
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QScrollArea, QWidget

        host = QWidget()
        host.resize(1000, 800)
        area = QScrollArea(host)
        area.setGeometry(20, 20, 600, 400)
        area.setWidgetResizable(True)
        content = QWidget()
        content.resize(600, 5000)
        area.setWidget(content)
        host.show()
        for _ in range(5):
            qapp.processEvents()

        host.chat_scroll_area = area
        host.chat_container = content
        png = tmp_path / "hh.png"
        QImage(1600, 727, QImage.Format_ARGB32).save(str(png))

        deco = DecorationLayer(host, ref_resolver=lambda attr: getattr(host, attr, None))
        cfg = [
            {
                "id": "s",
                "image": str(png),
                "anchor": "scene_bottom",
                "fit_width": True,
                "fit_height": True,
            }
        ]
        deco.apply_config(cfg, image_resolver=lambda img: str(png))
        visible_w = deco._items[0]["label"].width()

        host.hide()
        for _ in range(5):
            qapp.processEvents()
        deco.apply_config(cfg, image_resolver=lambda img: str(png))
        hidden_label = deco._items[0]["label"]

        vp = area.viewport()
        assert hidden_label.width() == min(1600, vp.width()), (
            f"hide 后宽度基准失效：{hidden_label.width()} vs 视口 {vp.width()}"
        )
        assert visible_w == hidden_label.width()


class TestDecorationClear:
    """重复 apply_config 时旧装饰件必须立刻不可见（切页残留场景）

    回归背景：_clear 只用 deleteLater 排队销毁，控件在事件循环处理前仍然
    可见。切页/主题刷新连发时新旧装饰件会叠在一起，用户侧表现为
    「切换其他会话装饰层还会残留」。
    """

    @pytest.fixture()
    def host(self, qapp, tmp_path):
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(600, 400)
        w.chat_container = QWidget(w)
        w.chat_container.setGeometry(0, 0, 600, 400)
        w._input_card = QWidget(w)
        w._input_card.setGeometry(0, 300, 600, 100)

        png = tmp_path / "c.png"
        QImage(100, 50, QImage.Format_ARGB32).save(str(png))

        deco = DecorationLayer(w, ref_resolver=lambda a: getattr(w, a, None))
        w._deco = deco
        w._png = str(png)
        yield w
        w.deleteLater()
        deco.deleteLater()

    def test_reapply_hides_old_labels_immediately(self, host):
        """重新 apply 后旧 label 立即 hide，不等 deleteLater"""
        deco = host._deco
        cfg = [{"id": "s", "image": host._png, "anchor": "scene_bottom"}]
        deco.apply_config(cfg, image_resolver=lambda img: host._png)
        old = deco._items[0]["label"]
        assert old.isVisible() or old.isHidden() is False

        deco.apply_config(cfg, image_resolver=lambda img: host._png)
        assert old.isHidden(), "旧装饰件未在重新 apply 时立即隐藏（会与新件叠加残留）"
        assert len(deco._items) == 1

    def test_empty_config_hides_and_clears(self, host):
        """清空配置后旧 label 立即 hide"""
        deco = host._deco
        deco.apply_config(
            [{"id": "s", "image": host._png, "anchor": "scene_bottom"}],
            image_resolver=lambda img: host._png,
        )
        old = deco._items[0]["label"]
        deco.apply_config([], image_resolver=lambda img: host._png)
        assert old.isHidden()
        assert deco._items == []

