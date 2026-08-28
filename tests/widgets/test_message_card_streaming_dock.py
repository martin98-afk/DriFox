# -*- coding: utf-8 -*-
"""流式活动坞（Streaming Dock）测试：骨架资产 + Python 状态同步。

说明：测试环境无法创建 QWebEngineView（需 Qt.AA_ShareOpenGLContexts
在 QCoreApplication 创建前设置），因此骨架验证采用
"模块级资产常量 + inspect 校验骨架模板引用"的方式，
覆盖 (1) 资产内容正确 (2) 资产确实接入骨架模板 两条不变量。
"""

import inspect
import re
import sys

from PyQt5.QtWidgets import QApplication

from app.widgets import message_card as mc
from app.widgets.message_card import CodeWebViewer, MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_streaming_dock_css_content():
    """坞态 CSS 必须包含：flex 调换、order 沉底、110px 限高。"""
    css = mc._STREAMING_DOCK_CSS
    assert "body.streaming-dock" in css
    assert "flex-direction: column" in css
    assert "body.streaming-dock #tool-section" in css
    assert "order: 2" in css
    assert "body.streaming-dock #tool-content" in css
    assert "max-height: 110px" in css


def test_streaming_dock_content_no_horizontal_scrollbar():
    """回归：坞态正文容器不得出现横向滚动条。

    单轴 overflow-y:auto 时另一轴 visible 被计算为 auto → 长行（URL/无空格
    长 token）超宽出现容器级横向滚动条。必须 overflow-x:hidden +
    overflow-wrap:break-word（强制换行，避免 hidden 只裁切看不到尾巴）。
    """
    css = mc._STREAMING_DOCK_CSS
    # 提取坞态正文容器规则块
    m = re.search(r"body\.streaming-dock #content-placeholder \{(.*?)\}", css, re.S)
    assert m, "坞态正文容器规则必须存在"
    rule = m.group(1)
    assert "overflow-x: hidden" in rule, "坞态正文容器必须禁横向滚动"
    assert "overflow-wrap: break-word" in rule, "坞态正文容器必须强制换行（超宽长词断行）"


def test_streaming_dock_js_content():
    """坞态 JS 必须包含：_setStreamingDock 函数、流式标志、简洁模式守卫、滚动补偿。"""
    js = mc._STREAMING_DOCK_JS
    assert "function _setStreamingDock" in js
    assert "window._streamingActive" in js
    # 仅简洁模式启用坞态
    assert "_toolCompactMode" in js
    # 归位滚动补偿（防阅读位置跳动）
    assert "scrollTop" in js


def test_skeleton_template_includes_dock_assets():
    """骨架模板必须引用坞态资产常量（防止"定义了但没接进去"）。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    assert "_STREAMING_DOCK_CSS" in src
    assert "_STREAMING_DOCK_JS" in src


def test_content_autoscroll_pure_geometric():
    """回归：自动滚底必须是纯几何判定语义（仅接近底部才跟随）。

    历史方案用 _userScrolledWithin/_userScrolledUp/_progScroll/_suppressScrollEvent
    等标志位 + delta 启发式推断"用户是否滚过"，但内容钳制/anchor 补偿/程序置底
    产生的 scroll 事件与用户滚动无法区分，标志位必然误置/漏置，导致
    "跟随中断漂移"或"无条件拉底覆盖阅读位置"（输出固定到莫名其妙的位置）。
    新语义：读取几何位置 |scrollHeight - scrollTop - clientHeight| < 阈值，
    接近底部才滚底，否则零干预——几何位置本身就是用户意图的可靠信号。
    """
    js = mc._CONTENT_AUTOSCROLL_JS
    # 核心判定函数必须存在
    assert "function _nearBottom(" in js, "必须有几何判定函数"
    assert "function _maybeScrollToBottom(" in js
    assert "Math.abs(max - el.scrollTop) <= threshold" in js, "必须用几何位置判定接近底部"
    assert "max <= 0) return true" in js, "无溢出容器视为在底部（无需处理）"
    # _autoScrollStreamingBody 必须走几何判定，禁止无条件置底
    assert "document.body.scrollTop = document.body.scrollHeight" not in js, (
        "禁止无条件拉底 body（旧实现跟随态标志缺失时强制置底的根因）"
    )
    assert "_maybeScrollToBottom(document.body" in js
    # 坞态正文容器仅接近底部才跟随
    assert "_maybeScrollToBottom(_cp" in js
    # 禁止任何标志位/事件跟踪残留
    for forbidden in ("_userScrolledWithin", "_userScrolledUp", "_progScroll", "_suppressScrollEvent"):
        assert forbidden not in js, f"禁止残留标志位机制: {forbidden}"
    for forbidden_event in ("addEventListener('wheel'", "addEventListener('scroll'"):
        assert forbidden_event not in js, f"禁止滚动跟踪监听器: {forbidden_event}"


def test_tool_scroll_pure_geometric():
    """工具区/任务列表滚动同样必须是纯几何判定，无 scroll 跟踪监听器。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    # 工具区滚底函数走几何判定
    assert "function _scrollToolContentToBottom" in src
    assert "_maybeScrollToBottom(tc, 30)" in src, "工具区必须几何判定滚底"
    # 旧式跟踪监听器必须移除（body/_cp/tool-content/todo-content）
    for forbidden in (
        "window._userScrolledWithin",
        "_userScrolledUp",
        "_progScroll",
        "_suppressScrollEvent",
        "_prevScrollTop",
    ):
        assert forbidden not in src, f"骨架不得残留旧滚动标志: {forbidden}"


def test_skeleton_template_includes_content_autoscroll():
    """骨架模板必须接入 _CONTENT_AUTOSCROLL_JS（防止定义了没接进去）。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    assert "_CONTENT_AUTOSCROLL_JS" in src


class _StubPage:
    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


class _ViewerStub:
    """CodeWebViewer 桩：绑定真实方法，提供最小接口（无 WebEngine）。"""

    _sync_streaming_dock = CodeWebViewer._sync_streaming_dock
    finish_streaming = CodeWebViewer.finish_streaming
    _auto_collapse_tool_section = CodeWebViewer._auto_collapse_tool_section

    def __init__(self):
        self._is_js_ready = True
        self._page = _StubPage()
        self._streaming = True
        self.render_calls = 0
        # 与 CodeWebViewer._init_render_state 同语义：渲染序号，finish_streaming
        # 递增使在途线程池任务过期（9c76d04f 新增，stub 需同步）
        self._render_seq: int = 0

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        self.render_calls += 1


def test_sync_streaming_dock_injects_js():
    """_sync_streaming_dock 必须注入 _setStreamingDock(true/false)。"""
    stub = _ViewerStub()
    stub._sync_streaming_dock(True)
    assert "_setStreamingDock(true)" in stub._page.js_calls[-1]
    stub._sync_streaming_dock(False)
    assert "_setStreamingDock(false)" in stub._page.js_calls[-1]


def test_sync_streaming_dock_skips_when_js_not_ready():
    """JS 未就绪时不注入（_on_js_ready 会兜底同步）。"""
    stub = _ViewerStub()
    stub._is_js_ready = False
    stub._sync_streaming_dock(True)
    assert stub._page.js_calls == []


def test_finish_streaming_turns_dock_off():
    """finish_streaming 必须关闭坞态并触发最终渲染。"""
    stub = _ViewerStub()
    stub.finish_streaming()
    assert stub._streaming is False
    assert any("_setStreamingDock(false)" in js for js in stub._page.js_calls)
    assert stub.render_calls >= 1


class _StubViewerForCard:
    """MessageCard 用 viewer 桩（参照 test_message_card_tool_streaming 模式）。"""

    def __init__(self):
        self._streaming = False
        self.dock_calls = []

    def _sync_streaming_dock(self, active):
        self.dock_calls.append(active)


def test_start_streaming_anim_turns_dock_on():
    """MessageCard.start_streaming_anim 必须对 viewer 开启坞态。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewerForCard()
    card.start_streaming_anim()
    assert card.viewer.dock_calls == [True]


# ──────────────────────────────────────────────
# F2：dock 状态机完善（S1 延迟归位 + S2 竞态兜底）
# ──────────────────────────────────────────────


def test_has_active_tools_behavior():
    """_has_active_tools()：登记未完成=True、完成后=False、空=False。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    # 空：无任何登记 → False
    assert card._has_active_tools() is False
    # 登记但未完成 → True
    card._tool_call_order["t1"] = 0
    assert card._has_active_tools() is True
    # 完成后 → False
    card._finished_streaming_ids.add("t1")
    assert card._has_active_tools() is False
    # 多个：部分完成仍 True
    card._tool_call_order["t2"] = 1
    assert card._has_active_tools() is True
    card._finished_streaming_ids.add("t2")
    assert card._has_active_tools() is False


def test_finish_streaming_keep_dock_skips_dock_off():
    """finish_streaming(keep_dock=True) 不得注入 _setStreamingDock(false)。"""
    stub = _ViewerStub()
    stub.finish_streaming(keep_dock=True)
    assert stub._streaming is False
    assert not any("_setStreamingDock(false)" in js for js in stub._page.js_calls), (
        "keep_dock=True 时不应注入 _setStreamingDock(false)"
    )


def test_finish_streaming_default_keep_dock_false():
    """finish_streaming() 无参调用（keep_dock 默认 False）必须关闭坞态（向后兼容）。"""
    stub = _ViewerStub()
    stub.finish_streaming()
    assert any("_setStreamingDock(false)" in js for js in stub._page.js_calls), (
        "无参调用默认 keep_dock=False，必须注入 _setStreamingDock(false)"
    )


def test_append_tool_result_dock_off_guard_for_stub_viewer():
    """append_tool_result 的归位触发必须 hasattr 守卫（stub viewer 无 _sync_streaming_dock 不抛异常）。

    #P2 要求：stub viewer 无 _sync_streaming_dock 方法，若不加守卫会 AttributeError。
    """
    from app.widgets.message_card import MessageCard as _MC

    class _NoDockViewer:
        """无 _sync_streaming_dock 的 stub viewer（模拟测试桩）。"""

        def __init__(self):
            self._streaming = False
            self.js_calls = []

        def _schedule_render(self, immediate=False):
            pass

        def page(self):
            return self

        def runJavaScript(self, js_code):
            self.js_calls.append(js_code)

    _ensure_qapp()
    card = _MC(role="assistant")
    card._lazy_rendered = True
    card.viewer = _NoDockViewer()
    # 登记工具 → 完成 → 触发 append_tool_result 全路径，不得抛 AttributeError
    card._tool_call_order["call_guard"] = 0
    card.append_tool_result(
        tool_name="read_file",
        arguments={"path": "x.py"},
        result="hello",
        success=True,
        tool_call_id="call_guard",
    )
    assert "call_guard" in card._finished_streaming_ids


class _DockRecordingViewer:
    """带 _sync_streaming_dock 记录的 viewer 桩（S1 正向测试用）。"""

    def __init__(self):
        self._streaming = True  # 初始流式中（与真实 viewer 流式态一致）
        self.dock_calls = []
        self.js_calls = []
        self._tool_compact_mode = False
        self._tool_target_id = "tool-content"
        self._tool_dom_dirty = False
        self._restore_finished_ids = set()

    def _sync_streaming_dock(self, active):
        self.dock_calls.append(active)

    def _schedule_render(self, immediate=False):
        pass

    def page(self):
        return self

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


def test_s1_dock_returns_after_last_tool_result():
    """S1 正向：finish_streaming(keep_dock=True) → 最后一个工具完成 → 归位触发。

    #F3 回归（#R1 P1）：F2 归位兜底条件用 `not getattr(self.viewer, "_streaming")`，
    但 append_tool_result 中段「就近恢复 viewer 流式模式」把 viewer._streaming 无条件
    置 True → 归位条件恒 False → QTimer 永不注册 → 会话末轮 dock 永久沉底。
    修复：改用 MessageCard 层 self._streaming（stop_streaming_anim 置 False）判据。
    """
    from PyQt5.QtCore import QTimer

    from app.widgets.message_card import MessageCard as _MC

    _ensure_qapp()
    card = _MC(role="assistant")
    card._lazy_rendered = True
    card.viewer = _DockRecordingViewer()
    # 模拟流式流程：登记工具 → 流式结束（keep_dock=True，仍有活跃工具）→ 工具完成
    card._tool_call_order["s1_tool"] = 0
    # 流式结束：MessageCard.finish_streaming 传 keep_dock=_has_active_tools()=True
    # （这里直接模拟状态，不调真实 finish_streaming 以免依赖 anim timer）
    card._streaming = False  # 等价于 stop_streaming_anim 后的状态
    # 最后一个工具完成 → 归位兜底应注册 QTimer → 事件循环推进后 dock off
    card.append_tool_result(
        tool_name="read_file",
        arguments={"path": "x.py"},
        result="hello",
        success=True,
        tool_call_id="s1_tool",
    )
    # 推进事件循环让 singleShot(0) 执行
    QTimer.singleShot(10, lambda: None)
    QApplication.processEvents()
    QApplication.processEvents()
    assert any(call is False for call in card.viewer.dock_calls), (
        f"最后一个工具完成后 dock 应归位（_sync_streaming_dock(False)），实际 dock_calls={card.viewer.dock_calls}"
    )


# ──────────────────────────────────────────────
# F4：PlainTextViewer 无 keep_dock 参数回归（1828e2f7 引入 TypeError）
# ──────────────────────────────────────────────


def test_user_card_finish_streaming_with_plain_text_viewer():
    """user 角色卡片（PlainTextViewer）调用 finish_streaming 不抛 TypeError。

    #F4 回归（1828e2f7）：MessageCard.finish_streaming 统一以
    keep_dock=self._has_active_tools() 调用 viewer.finish_streaming，但
    PlainTextViewer.finish_streaming 无 keep_dock 参数 → 发送用户消息时
    （main_widget._append_user_message → card.finish_streaming）TypeError 崩溃。
    修复：PlainTextViewer.finish_streaming 增加 keep_dock 参数（接口对齐，
    PlainTextViewer 无 dock 概念则忽略）。
    """
    from app.widgets.message_card import MessageCard as _MC, PlainTextViewer

    _ensure_qapp()
    # 用真实 PlainTextViewer（user 角色默认 viewer）
    card = _MC(role="user")
    card._lazy_rendered = True
    card.viewer = PlainTextViewer(card)
    # 不抛异常即通过（修复前 TypeError: unexpected keyword argument 'keep_dock'）
    card.finish_streaming()
    assert card.viewer is not None


def test_plain_text_viewer_finish_streaming_accepts_keep_dock():
    """PlainTextViewer.finish_streaming 必须接受 keep_dock 参数（接口与 CodeWebViewer 对齐）。"""
    from inspect import signature

    from app.widgets.message_card import PlainTextViewer

    sig = signature(PlainTextViewer.finish_streaming)
    assert "keep_dock" in sig.parameters, f"PlainTextViewer.finish_streaming 必须声明 keep_dock 参数，实际签名 {sig}"


# ──────────────────────────────────────────────
# 区域独立 II：工具/思考更新不得触碰正文容器滚动位置
#
# P005 修复（_userScrolledUp 保护）后残余两处根因：
# 1. 工具完成块注入 / 工具流式块注入 / _apply_viewer_height 高度回调仍共用
#    _autoScrollStreamingBody()——用户未上滚（跟随态）时正文容器仍被拉底。
# 2. updateContent 全量重写 #content-placeholder innerHTML 把 scrollTop 归 0，
#    只恢复 body 的 scrollTop：思考更新触发全量渲染时正文跳顶（上滚过）/
#    跳底（跟随态被置底）——即"工具与思考更新时正文滚到固定位置"。
# ──────────────────────────────────────────────


def test_autoscroll_body_only_param():
    """_autoScrollStreamingBody 必须支持 bodyOnly：true 时只滚 body 不碰正文容器。"""
    js = mc._CONTENT_AUTOSCROLL_JS
    assert "function _autoScrollStreamingBody(bodyOnly)" in js, "必须声明 bodyOnly 参数"
    assert "if (!bodyOnly &&" in js, "正文容器置底必须可被 bodyOnly 跳过"


def test_tool_paths_do_not_scroll_content():
    """工具完成块/流式块注入与高度回调只滚 body（bodyOnly=true），不置底正文容器。"""
    for fn in (
        MessageCard.append_tool_result,
        MessageCard._inject_tool_streaming_html,
        MessageCard._apply_viewer_height,
    ):
        src = inspect.getsource(fn)
        assert "_autoScrollStreamingBody(true)" in src, (
            f"{fn.__name__} 必须传 bodyOnly=true（工具/思考更新不碰正文滚动）"
        )
        assert "_autoScrollStreamingBody()" not in src, f"{fn.__name__} 不得存在无参调用（会置底正文容器）"


def test_update_content_preserves_content_scroll():
    """updateContent 全量重写 innerHTML 后必须恢复正文容器阅读位置。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    assert "_cpPrevTop" in src, "必须保存正文容器 scrollTop"
    assert "Math.min(_cpPrevTop" in src, "必须在 DOM 操作完成后恢复（钳制到新 max）"
