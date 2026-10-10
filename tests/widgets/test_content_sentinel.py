# -*- coding: utf-8 -*-
"""[T37] 内容哨兵回归测试：pywebview_height 第 5 字段解析 + 白屏自愈触发逻辑。

背景：真机长会话（多工具 + 长回复）出现「viewer 全白、高度只剩骨架默认值」，
复现探针（tests/debug/stream_blank_collapse_probe.py）证实内容停在 Python→JS
链路的某一层后再无任何恢复路径。哨兵借 reportHeight 高频回传被动巡检：
markdown 积压 >800 字符、cp 可见文本 <200、think 未闭合排除 → 强制全量补渲，
3s 冷却防风暴。覆盖一切"内容丢失"型白屏（无论根因在哪一层）。

这些测试不加载页面（不触发 Chromium 导航），可在无 GPU CI 上运行。
"""

from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def viewer(qapp):
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile()
    from app.widgets.card_viewers import CodeWebViewer

    v = CodeWebViewer()
    yield v
    v.deleteLater()


@pytest.fixture()
def page(qapp):
    from app.core.infra.webengine_profile import get_shared_web_profile, init_shared_web_profile

    init_shared_web_profile()
    from app.widgets.card_viewers import ConsoleMonitorPage

    p = ConsoleMonitorPage(get_shared_web_profile())
    yield p
    p.deleteLater()


class TestHeightPayloadParsing:
    """pywebview_height 载荷解析（纯函数）"""

    def test_full_five_fields(self):
        from app.widgets.card_viewers import _parse_height_payload

        assert _parse_height_payload("1200|0|800|0|4567") == (1200, 0, 800, False, 4567)

    def test_four_fields(self):
        from app.widgets.card_viewers import _parse_height_payload

        assert _parse_height_payload("1200|0|800|1") == (1200, 0, 800, True, None)

    def test_legacy_height_only(self):
        from app.widgets.card_viewers import _parse_height_payload

        assert _parse_height_payload("1200") == (1200, 0, 0, None, None)

    def test_invalid(self):
        from app.widgets.card_viewers import _parse_height_payload

        assert _parse_height_payload("abc") is None
        assert _parse_height_payload("") is None


class TestBridgeDispatch:
    """console 桥分发：第 5 字段入库 + 旧格式兼容"""

    def test_five_fields_stored(self, page):
        page.heightReported = MagicMock()
        page.bodyGeometryReported = MagicMock()
        page.javaScriptConsoleMessage(0, "pywebview_height:1200|0|800|0|4567", 0, "")
        assert page._dom_text_len == 4567
        assert page.heightReported.emit.call_args[0][0] == 1200
        assert page.bodyGeometryReported.emit.call_args[0] == (1200, 0, 800)

    def test_reading_flag_not_corrupted_by_5th_field(self, page):
        # ⚠️ 解析 maxsplit 必须是 4：3 会把第 5 字段并进 parts[3] 破坏阅读标志
        page.heightReported = MagicMock()
        page.bodyGeometryReported = MagicMock()
        page.cardReadingChanged = MagicMock()
        page._last_card_reading = False
        page.javaScriptConsoleMessage(0, "pywebview_height:1200|0|800|1|4567", 0, "")
        assert page._last_card_reading is True

    def test_legacy_four_fields_dom_unknown(self, page):
        page.heightReported = MagicMock()
        page.bodyGeometryReported = MagicMock()
        page._dom_text_len = 99
        page.javaScriptConsoleMessage(0, "pywebview_height:900|0|600|0", 0, "")
        assert page._dom_text_len == -1  # 未知：哨兵不动作

    def test_height_only_no_geometry_signal(self, page):
        page.heightReported = MagicMock()
        page.bodyGeometryReported = MagicMock()
        n = page.bodyGeometryReported.emit.call_count
        page.javaScriptConsoleMessage(0, "pywebview_height:900", 0, "")
        assert page.bodyGeometryReported.emit.call_count == n


class TestContentSentinel:
    """白屏自愈触发逻辑（读 page 的 _dom_text_len）"""

    def _arm(self, viewer):
        viewer._streaming = True
        viewer._is_js_ready = True
        viewer._markdown_text = "x" * 5000
        viewer._sentinel_last_ts = 0.0
        viewer._render_deferred = False
        viewer._schedule_render = MagicMock()
        viewer.page()._dom_text_len = 10

    def test_blank_content_triggers_full_render(self, viewer):
        self._arm(viewer)
        viewer._content_sentinel_check()
        assert viewer._schedule_render.called
        kwargs = viewer._schedule_render.call_args.kwargs or viewer._schedule_render.call_args[1]
        assert kwargs.get("immediate") is True

    def test_cooldown_suppresses_repeat(self, viewer):
        self._arm(viewer)
        viewer._content_sentinel_check()
        viewer._schedule_render.reset_mock()
        viewer._content_sentinel_check()
        assert not viewer._schedule_render.called  # 3s 冷却内

    def test_healthy_content_no_trigger(self, viewer):
        self._arm(viewer)
        viewer.page()._dom_text_len = 3000
        viewer._content_sentinel_check()
        assert not viewer._schedule_render.called

    def test_unclosed_think_silence_is_by_design(self, viewer):
        # think 未闭合期间正文静默累积是设计行为（防 spinner 闪烁），不得触发
        self._arm(viewer)
        viewer._markdown_text = "x" * 5000 + "<think>未闭合"
        viewer._content_sentinel_check()
        assert not viewer._schedule_render.called

    def test_unknown_dom_len_no_trigger(self, viewer):
        # 旧骨架（无第 5 字段 → -1）：哨兵失效但不误触发
        self._arm(viewer)
        viewer.page()._dom_text_len = -1
        viewer._content_sentinel_check()
        assert not viewer._schedule_render.called

    def test_not_streaming_no_trigger(self, viewer):
        self._arm(viewer)
        viewer._streaming = False
        viewer._content_sentinel_check()
        assert not viewer._schedule_render.called


class TestBlankContentCollapse:
    """[T38] 空正文折叠：长工具循环期间正文无内容时不得留一块"假白屏"空白"""

    def _core_text(self):
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_render_core.py"
        ).read_text(encoding="utf-8")

    def test_css_collapses_blank_content_in_dock(self):
        t = self._core_text()
        assert "body.streaming-dock.cp-blank #content-placeholder" in t, "坞态空正文折叠 CSS 缺失"
        assert "display: none" in t.split("cp-blank #content-placeholder")[1][:200]

    def test_report_height_maintains_blank_flag(self):
        from pathlib import Path

        t = (
            Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"
        ).read_text(encoding="utf-8")
        assert "cp-blank" in t, "reportHeight 未维护 cp-blank"
        # 必须有实体内容白名单（图片/公式/表格/图表/代码块），否则只有图片的正文会被误折叠
        assert "img,video,canvas,table,pre,svg" in t

    def test_reuse_reset_clears_blank_flag(self):
        # 池化复用必须复位 cp-blank，否则新卡片一开始就隐藏正文区
        t = self._core_text()
        reset = t.split("_RESET_CONTENT_FOR_REUSE_JS")[1][:2500]
        assert "cp-blank" in reset
