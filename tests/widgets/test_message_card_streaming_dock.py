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
    """坞态 CSS 必须包含：flex 调换、order 沉底、限高。"""
    css = mc._STREAMING_DOCK_CSS
    assert "body.streaming-dock" in css
    assert "flex-direction: column" in css
    assert "body.streaming-dock #tool-section" in css
    assert "order: 2" in css
    assert "body.streaming-dock #tool-content" in css
    # 工具区限高：110→220（原 3-4 行看不见进度，放宽到 ≈8 行）
    assert "max-height: 220px" in css
    # 正文限高：330→450→600（流式长回复展示更多正文）
    assert "max-height: 600px" in css


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


def test_content_autoscroll_respects_user_scroll():
    """回归：工具/思考区更新不得拉底正文容器（区域独立）。

    坞态下 #content-placeholder（正文）与 #tool-content（工具与思考）是两个
    独立内滚动容器。_autoScrollStreamingBody 被工具/思考更新路径共用（流式块
    注入/完成块替换/_apply_viewer_height 高度回调），原实现无条件
    _cp.scrollTop = _cp.scrollHeight 置底正文——而 _userScrolledWithin 只由
    body 的 scroll 事件置位，用户滚正文容器时 body 不滚，保护恒失效，
    工具区每来新内容就把正文拉底打断阅读。
    """
    js = mc._CONTENT_AUTOSCROLL_JS
    # 置底正文容器前必须检查用户上滚标志（与 _scrollToolContentToBottom 同款）
    assert "if (!_cp._userScrolledUp)" in js, "正文容器置底必须尊重用户上滚"
    # 程序置底必须打 _progScroll 标记（防 scroll 事件误判为用户滚动）
    assert "_cp._progScroll = true" in js, "程序置底必须打 _progScroll 标记"
    # 正文容器必须有 scroll 监听跟踪用户滚动（滚回底部附近恢复跟随）
    assert "getElementById('content-placeholder')?.addEventListener('scroll'" in js, "正文容器必须有独立 scroll 监听"
    # 监听内恢复跟随：位置判定（接近底部=跟随，离开=用户阅读）
    assert "cp._userScrolledUp = !atBottom" in js, "滚回底部附近必须恢复自动跟随（位置判定）"
    # DOM 操作期间程序性 scroll 必须忽略（防误标正文上滚→置顶），与 body 监听对称
    assert "if (window._suppressScrollEvent) return;" in js, "DOM 操作期间的程序 scroll 必须忽略"
    # 程序滚动事件吞掉（不误标用户）
    assert "if (cp._progScroll) { cp._progScroll = false; return; }" in js


def test_content_autoscroll_marks_user_scroll_via_wheel():
    """回归：用户上滚意图必须由 wheel 事件同步标记，不得依赖 scroll 事件推断。

    根因（流式滚动位置被拉到固定偏上位置）：_userScrolledUp 原由 scroll 事件
    （异步派发）的 atBottom 推断置位。两条失效链：
    1. 竞争窗口：用户滚轮后 scroll 事件尚未派发（标志仍 false），流式渲染
       JS（_autoScrollStreamingBody 无参调用）抢先执行 → 无条件拉底覆盖
       用户位置；后续 updateContent 保存被污染的 _cpPrevTop → 每次恢复到
       同一错误值 → 表现为"正文更新时滚轮跳到固定偏上位置"。
    2. 钳制误标：innerHTML 重建/高度回调使内容变短 → scrollTop 被浏览器
       钳制 → 触发 scroll 事件 → atBottom 误判 false → userUp 误置 true
       → 停止跟随、位置自行漂移。
    wheel 事件同步派发且仅由用户触发（滚轮/触控板），无程序来源，用它标记
    上滚意图可同时消除两条失效链。
    """
    js = mc._CONTENT_AUTOSCROLL_JS
    # wheel 上滚同步置位（deltaY < 0）
    assert "addEventListener('wheel'" in js, "必须有 wheel 监听同步标记用户上滚"
    assert "deltaY < 0" in js, "wheel 上滚方向判定（deltaY<0）必须存在"
    assert "this._userScrolledUp = true" in js, "wheel 上滚必须同步置 _userScrolledUp"
    # scroll 监听只做恢复跟随（位置判定 atBottom → 清标志），不得置位（防钳制 scroll 误标）
    # 注：置位唯一入口是 wheel 同步标记；scroll 监听内的位置判定赋值是恢复跟随语义。
    # wheel 监听必须是 passive（不阻断浏览器原生滚动）
    assert "{passive: true}" in js, "wheel 监听必须 passive"
    # 🐛 回归（工具折叠框展开→视口弹到随机位置）：
    # wheel 置位必须门控"容器实际可滚"——无溢出时 wheel 属冒泡残留
    # （本应转发外层聊天列表），误置位会锁死正文跟随；
    assert "scrollHeight > this.clientHeight" in js, "wheel 置位必须检查 cp 实际可滚（防冒泡残留误置位）"
    # 历史注：原实现要求 _lastUserWheelAt/800ms 时间窗门控 scroll 清标志；
    # 现实现改为位置判定（离开底部=阅读，滚回底部=恢复）+ _progScroll 排除，
    # 消除了"下滚回底不刷新时间戳 → 标志卡死为已离开 → 跟随失效弹回中间"的根因。
    assert "_suppressScrollEvent" in js, "DOM 操作期间的程序 scroll 必须忽略"


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
        # 同步 CodeWebViewer 后续演进新增的属性（缺失会 AttributeError）：
        self.viewer = None  # finish_streaming 的 hasattr 守卫分支
        self._light_skeleton = False  # 欢迎卡片不进坞态守卫
        self._needs_full_render = False
        self._stable_html = ""
        self._stable_md_len = 0
        self._render_pending = None
        self._tool_dom_dirty = False
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        self._think_text_streaming_started = False
        self._reasoning_streaming_started = False

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


# ──────────────────────────────────────────────
# force_dock_off：打断/错误收尾强制归位（流式结构残留 bug 回归）
#
# 根因：打断路径（手动停止/自动压缩/引擎错误）worker 已终止，活跃工具结果
# 永不到达 → append_tool_result 的 F2 兜底归位永不触发；若仍按 S1 语义
# keep_dock=True，坞态永久沉底、正文限矮（卡片保持流式结构）。
# 引擎错误路径还有一层：update_content 在 _streaming=False 时经
# start_streaming_anim 重开流式态与坞态，收尾必须在其后强制关坞。
# ──────────────────────────────────────────────


class _ForceDockViewer:
    """模拟 CodeWebViewer.finish_streaming 坞态行为的 viewer 桩。

    finish_streaming(keep_dock) 时记录 keep_dock 并在 keep_dock=False 时
    关坞（与 CodeWebViewer.finish_streaming 内部 `_sync_streaming_dock(False)`
    行为一致），供断言 MessageCard 层参数传递是否正确。
    """

    def __init__(self):
        self._streaming = True
        self.dock_calls = []
        self.finish_keep_dock = None

    def _sync_streaming_dock(self, active):
        self.dock_calls.append(active)

    def finish_streaming(self, keep_dock=False):
        self.finish_keep_dock = keep_dock
        if not keep_dock:
            self._sync_streaming_dock(False)


def test_finish_streaming_force_dock_off_overrides_active_tools():
    """force_dock_off=True：即使有活跃工具也必须关坞态（打断/错误收尾语义）。"""
    from app.widgets.message_card import MessageCard as _MC

    _ensure_qapp()
    card = _MC(role="assistant")
    card._lazy_rendered = True
    card.viewer = _ForceDockViewer()
    # 打断时刻仍有活跃工具（登记未完成）
    card._tool_call_order["t1"] = 0
    card._streaming = True
    card.finish_streaming(force_dock_off=True)
    assert card._streaming is False
    assert card.viewer.finish_keep_dock is False, "force_dock_off=True 必须覆盖活跃工具判据"
    assert card.viewer.dock_calls == [False], (
        f"force_dock_off=True 必须关坞态，实际 dock_calls={card.viewer.dock_calls}"
    )


def test_finish_streaming_without_force_keeps_s1_dock_semantics():
    """无 force_dock_off 时保持 S1 语义：有活跃工具 → keep_dock=True（坞态保留）。"""
    from app.widgets.message_card import MessageCard as _MC

    _ensure_qapp()
    card = _MC(role="assistant")
    card._lazy_rendered = True
    card.viewer = _ForceDockViewer()
    card._tool_call_order["t1"] = 0
    card._streaming = True
    card.finish_streaming()
    assert card.viewer.finish_keep_dock is True, "S1 语义：活跃工具时 keep_dock=True"
    assert card.viewer.dock_calls == [], (
        f"S1 语义：活跃工具时不得关坞（等工具完成兜底归位），实际 dock_calls={card.viewer.dock_calls}"
    )


def test_finish_streaming_force_dock_off_no_tools_still_docks_off():
    """force_dock_off=True 且无活跃工具：正常关坞（与默认行为一致）。"""
    from app.widgets.message_card import MessageCard as _MC

    _ensure_qapp()
    card = _MC(role="assistant")
    card._lazy_rendered = True
    card.viewer = _ForceDockViewer()
    card._streaming = True
    card.finish_streaming(force_dock_off=True)
    assert card.viewer.finish_keep_dock is False
    assert card.viewer.dock_calls == [False]


# ──────────────────────────────────────────────
# 重建流式态同步：后台标签页切回后卡片重现流式结构（多 tab 并行高发）
#
# 根因：已结束卡片（_streaming_finished=True）经虚拟滚动/配额回收重建时，
# ensure_rendered 只在 _is_history=True（磁盘历史）分支同步 viewer 非流式态；
# 本轮已结束对话 _is_history=False → 新建 viewer 的 _streaming=True 初始值
# 残留（池化实例经 reset_for_reuse 已置 False，两路径行为不一致）→ 渲染走
# 流式分支，且 _on_js_ready 旧兜底 `_setStreamingDock(!!_act||!_co)` 因新骨架
# 无 data-collapsed 属性（_co=false）误开坞态 → 工具区沉底 + 正文限矮。
# ──────────────────────────────────────────────


def test_ensure_rendered_syncs_streaming_false_for_finished_cards():
    """重建时卡片非流式中必须同步 viewer._streaming=False（Qt/池化两个分支都要）。"""
    src = inspect.getsource(MessageCard.ensure_rendered)
    assert src.count("if not self._streaming:") >= 2, (
        "ensure_rendered 的 Qt 渲染器分支与池化/新建分支都必须同步 viewer._streaming，"
        "否则新建重建的已结束卡片残留流式态"
    )


def test_on_js_ready_dock_sync_uses_python_streaming_flag():
    """_on_js_ready 坞态兜底必须以 Python 端 _streaming 判定，禁止「未折叠 → 开坞」推导。"""
    src = inspect.getsource(CodeWebViewer._on_js_ready)
    # 剥离注释后断言（根因说明的注释里会引用旧表达式原文）
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "!!_act||!_co" not in code, "不得保留 !_co（未折叠）开坞推导：新骨架无 data-collapsed 会误开坞态"
    assert "_dock_on" in code, "必须用 Python 端 _streaming 真值参与坞态判定"
