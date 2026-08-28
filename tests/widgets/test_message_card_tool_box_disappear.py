# -*- coding: utf-8 -*-
"""回归测试：编辑类工具（write/edit/multi_edit）运行框在"工具运行中→工具结束框"
之间偶尔消失。

根因（三个漏洞叠加）：
  A. 全量渲染后 _tool_dom_dirty 被**同步立即**清除（L5787/L6049 旧逻辑），但
     runJavaScript 是异步的——JS 未执行完、DOM 中运行框仍在时，下一次渲染已
     读不到 dirty → 裸 updateContent 重建 #content-placeholder → 抹掉 JS 注入
     的运行框，直到 append_tool_result 才重现。
  B. _inject_tool_streaming_html 的 preview 去重命中时直接 return，不重新设置
     dirty → dirty 被清后运行框永久失去 save/restore 保护。
  C. _build_save_and_restore_js 的 restore 只恢复 data-streaming="true" 的块；
     finish_tool_streaming 注入的完成态预览块（streaming=false）在
     append_tool_result 之前不在 markdown 中，save 后不 restore → 被抹掉。

修复：
  A → dirty 清除延后到 JS 回调（_clear_tool_dom_dirty_guarded），带 pending +
     代际双重守卫；
  B → 去重 return 前也置 dirty（代际递增）；
  C → restore 条件放宽为"未完成（结果未 append_tool_result）的块也恢复"，
     已完成块仍由 markdown 重新生成（不 restore 防重复）。

本测试用 stub viewer 验证 Python 侧状态机不变量（真实 WebEngine DOM 行为
需人工/集成验证，与 tests 既有风格一致）。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    pass


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    """记录 runJavaScript 调用与回调，支持手动 flush 模拟异步执行完成"""

    def __init__(self):
        self.js_calls = []
        self._callbacks = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)
        if callback:
            self._callbacks.append(callback)

    def flush(self):
        cbs, self._callbacks = self._callbacks, []
        for cb in cbs:
            cb(None)


class _StubViewer:
    """CodeWebViewer 桩：绑定真实渲染/守卫方法，提供最小接口（无 WebEngine）。

    直接绑定 CodeWebViewer 的真实方法，确保测试验证的是生产代码逻辑，
    而非测试内复刻的副本。
    """

    _clear_tool_dom_dirty_guarded = None  # 占位，构造后绑定

    def __init__(self):
        from types import MethodType

        from app.widgets.message_card import CodeWebViewer

        self._clear_tool_dom_dirty_guarded = MethodType(CodeWebViewer._clear_tool_dom_dirty_guarded, self)
        self._has_active_tool_dom = MethodType(CodeWebViewer._has_active_tool_dom, self)
        self._streaming = True
        self._restore_finished_ids = set()
        self._tool_target_id = "content-placeholder"
        self._tool_compact_mode = False
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()
        self._page = _StubPage()

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        pass


def _make_card():
    from app.widgets.message_card import MessageCard

    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


# ─────────────────────────────────────────────
# 漏洞 A：dirty 清除必须延后到 JS 回调 + 守卫
# ─────────────────────────────────────────────


def test_dirty_not_cleared_while_pending_tool_in_dom():
    """运行中工具（pending 非空）→ 渲染回调不得清除 dirty。

    旧逻辑在 runJavaScript 后同步清 dirty，下一次渲染误判无工具 DOM →
    裸 updateContent 抹掉运行框。修复后 pending 守卫阻止清除。
    """
    card = _make_card()
    v = card.viewer
    tid = "call_write_1"

    # 注入运行框：update_tool_streaming → _inject_tool_streaming_html
    v._tool_dom_dirty = True
    v._tool_dom_dirty_gen += 1
    v._injected_pending_tools.add(tid)
    v._page.runJavaScript('(注入运行框 data-tool-call-id="%s")' % tid)

    # 渲染完成回调触发（JS 已执行完，运行框仍在 DOM）
    gen = v._tool_dom_dirty_gen
    v._clear_tool_dom_dirty_guarded(gen)

    assert v._tool_dom_dirty is True, "有 pending 工具时 dirty 不得被清除（运行框仍需保护）"


def test_dirty_cleared_after_tool_finished():
    """工具完成（pending 空）→ 渲染回调可清除 dirty（代际匹配时）。"""
    card = _make_card()
    v = card.viewer
    tid = "call_write_2"

    v._tool_dom_dirty = True
    v._tool_dom_dirty_gen += 1
    v._injected_pending_tools.add(tid)
    # 工具完成：append_tool_result 移除 pending
    v._injected_pending_tools.discard(tid)
    v._tool_dom_dirty_gen += 1

    gen = v._tool_dom_dirty_gen
    v._clear_tool_dom_dirty_guarded(gen)
    assert v._tool_dom_dirty is False, "pending 空 + 代际匹配 → dirty 应清除"


def test_stale_render_callback_does_not_clear_fresh_dirty():
    """旧渲染回调（代际过期）不得清除新注入设置的 dirty。"""
    card = _make_card()
    v = card.viewer

    # 渲染1 排队，捕获 gen=1
    v._tool_dom_dirty = True
    v._tool_dom_dirty_gen = 1
    stale_gen = 1

    # 期间新注入（update_tool_streaming 继续到达）→ gen=2
    v._tool_dom_dirty = True
    v._tool_dom_dirty_gen = 2

    # 渲染1 回调触发：代际不匹配 → 不清除
    v._clear_tool_dom_dirty_guarded(stale_gen)
    assert v._tool_dom_dirty is True, "过期回调不得清除新 dirty"


# ─────────────────────────────────────────────
# 漏洞 B：preview 去重 return 前必须置 dirty
# ─────────────────────────────────────────────


def test_inject_tool_streaming_dedup_keeps_dirty():
    """preview 去重命中（不注入）时，dirty 必须保持/重设保护标记。

    旧逻辑去重直接 return，dirty 被渲染回调清除后运行框永久失去保护。
    """
    card = _make_card()
    v = card.viewer
    tid = "call_write_3"

    # 首次注入：登记 preview 缓存
    card.update_tool_streaming(tool_call_id=tid, tool_name="write", partial_args={"path": "a.py"})
    assert tid in v._injected_pending_tools
    assert v._tool_dom_dirty is True

    # 模拟渲染回调清 dirty（无 pending 的极端场景先清掉？不——pending 非空不会清。
    # 这里手动模拟"dirty 已被清"以验证去重分支会重设 dirty）
    v._tool_dom_dirty = False

    # 相同 preview 再次更新 → 去重命中 → 必须重设 dirty
    card.update_tool_streaming(tool_call_id=tid, tool_name="write", partial_args={"path": "a.py"})
    assert v._tool_dom_dirty is True, "去重 return 前必须重设 dirty（运行框仍需保护）"


def test_inject_tool_streaming_sets_dirty_before_schedule_render():
    """dirty 标记必须先于 _schedule_render 设置（completed=True 时 immediate 渲染
    会立即执行，若 dirty 还是旧值 → 裸 updateContent 抹掉旧运行框）。"""
    card = _make_card()
    v = card.viewer
    v._tool_dom_dirty = False

    # finish_tool_streaming（completed=True）→ _schedule_render(immediate=True)
    card.finish_tool_streaming(
        tool_call_id="call_write_4", tool_name="write", arguments={"path": "a.py", "content": "x"}
    )

    assert v._tool_dom_dirty is True, "completed 注入前 dirty 必须已置 True"


# ─────────────────────────────────────────────
# 漏洞 C：save/restore 必须恢复未完成的预览块
# ─────────────────────────────────────────────


def test_save_restore_js_restores_unfinished_blocks():
    """save/restore 模板：未完成（结果未 append_tool_result）的块必须恢复，
    即使 data-streaming="false"（finish_tool_streaming 完成态预览块）。"""
    from app.widgets.message_card import CodeWebViewer

    _ensure_qapp()
    v = _StubViewer()

    js = CodeWebViewer._build_save_and_restore_js(v, "<p>html</p>", finished_ids={"done_1"})
    # restore 条件：!isFinished（未完成块恢复），不只看 streaming
    assert "_isFinished=(_finishedSet.indexOf(b.id)!==-1)" in js
    assert "if(!_isFinished&&!document.querySelector" in js
    # 已完成集合注入 JS
    assert '"done_1"' in js


def test_save_restore_js_finished_blocks_not_restored():
    """已完成块（结果已 append_tool_result）不 restore——markdown 会重新生成，
    恢复会造成重复。"""
    from app.widgets.message_card import CodeWebViewer

    _ensure_qapp()
    v = _StubViewer()
    js = CodeWebViewer._build_save_and_restore_js(v, "<p>html</p>", finished_ids={"done_1", "done_2"})
    assert '"done_1"' in js and '"done_2"' in js
    # 未完成时也要恢复（防止回归到"只恢复 streaming=true"）
    assert "b.streaming==='true'" not in js.replace("data-streaming", "")
