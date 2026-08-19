# -*- coding: utf-8 -*-
"""回归测试：切换项目/新建会话打断对话时，Tab 状态复位 + 会话完整保存

== 问题描述 ==
对话进行中切换项目（_on_project_selected → _create_new_session）时：

1. Tab 卡在"正在对话"模式：``_create_new_session`` 只调用
   ``self.backend.stop_streaming()`` 但没有主动 ``_set_ai_state("idle")``。
   stop 后 worker 的 stream_finished 会被 ``_on_worker_finished`` 因
   ``is_streaming=False`` 忽略 → ``_on_stream_finished`` 永不触发 →
   ``_set_ai_state("idle")`` 永不执行 → TabPanel 边框动画停留在 streaming。

2. 被打断的会话未完整保存：``stop_streaming()`` 返回的中断消息
   （``worker.get_interrupted_messages()`` 的 partial 回复快照）被丢弃，
   且 ``_session_switched=True`` 又拦截 worker 的 finished_with_messages
   回调 → ``_auto_save_current_session`` 保存的是旧消息，缺最后部分回复。

== 修复 ==
在 ``_create_new_session`` 的 streaming 分支中（对齐
``_on_auto_compact_requested`` / ``_on_stop_clicked`` 的成熟写法）：
1. 接收 ``stop_streaming()`` 返回值并 ``_apply_interrupted_messages_to_session``
2. 调用 ``_set_ai_state("idle")`` 复位 Tab 状态

== 回归测试 ==
- AST 静态检查：_create_new_session 必须包含 _set_ai_state("idle")
  与 _apply_interrupted_messages_to_session 调用（节点级，不依赖字符串格式）
- 行为测试：stub 窗口 + mock backend，验证 streaming 分支会应用中断消息
  并复位 ai_state
"""

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "app" / "main_widget.py"


def _load_module_ast() -> ast.Module:
    return ast.parse(_SRC_PATH.read_text(encoding="utf-8"), filename=str(_SRC_PATH))


def _get_class(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"未找到类 {class_name}")


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


def _method_calls(method: ast.FunctionDef, attr: str, arg: str = None) -> bool:
    """方法体内是否出现 ``self.<attr>(<arg>)`` 形式的调用（节点级匹配）

    Args:
        attr: 被调用的方法名（如 "_set_ai_state"）
        arg: 若提供，要求第一个位置参数是字符串常量且值等于 arg
    """
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not (isinstance(func, ast.Attribute) and func.attr == attr):
            continue
        if arg is not None:
            if not sub.args:
                continue
            a0 = sub.args[0]
            if not (isinstance(a0, ast.Constant) and a0.value == arg):
                continue
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# 1. AST 静态检查（防回归）
# ═══════════════════════════════════════════════════════════════


class TestCreateNewSessionStreamingBranch:
    """_create_new_session 打断对话时必须复位 Tab 状态 + 保存中断消息"""

    def test_must_set_ai_state_idle(self):
        """_create_new_session 必须调用 _set_ai_state('idle')"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_create_new_session")
        assert _method_calls(method, "_set_ai_state", "idle"), (
            "_create_new_session 停止流式后必须调用 _set_ai_state('idle')，"
            "否则 TabPanel 边框动画停留在 streaming（切换项目后仍显示'正在对话'）。"
        )

    def test_must_apply_interrupted_messages(self):
        """_create_new_session 必须接收 stop_streaming 返回值并应用中断消息"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_create_new_session")
        # 检查 stop_streaming 返回值被赋值（interrupted = self.backend.stop_streaming()）
        found_assign = False
        for sub in ast.walk(method):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and sub.targets[0].id == "interrupted"
                and isinstance(sub.value, ast.Call)
            ):
                found_assign = True
                break
        assert found_assign, (
            "_create_new_session 必须接收 stop_streaming() 返回值（interrupted），否则被打断的会话缺最后部分回复。"
        )
        assert _method_calls(method, "_apply_interrupted_messages_to_session"), (
            "_create_new_session 必须把中断消息应用回 session，否则 _auto_save_current_session 保存的是旧消息。"
        )

    def test_stop_streaming_before_session_switch(self):
        """中断消息应用必须在 _session_switched 哨兵置位之前（时序守卫）"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_create_new_session")

        def _pos(pred) -> tuple:
            """返回 (lineno, col_offset)；用源码坐标判断先后，避免 ast.walk 乱序"""
            for node in ast.walk(method):
                if pred(node):
                    return (node.lineno, node.col_offset)
            return None

        apply_pos = _pos(
            lambda n: (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_apply_interrupted_messages_to_session"
            )
        )
        sentinel_pos = _pos(
            lambda n: (
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and n.targets[0].attr == "_session_switched"
            )
        )
        assert apply_pos is not None and sentinel_pos is not None
        # 中断消息必须属于旧会话，须在哨兵置位之前应用（create_session 在其后）
        assert apply_pos < sentinel_pos, "中断消息应用必须在 _session_switched 哨兵置位之前，避免污染新会话。"


# ═══════════════════════════════════════════════════════════════
# 2. 行为测试（stub 窗口 + mock backend）
# ═══════════════════════════════════════════════════════════════


def _make_stub(streaming: bool, interrupted_messages=None):
    """构造最小窗口 stub，真实执行 _create_new_session 的 streaming 分支

    - backend.chat_engine 非空 → 走 stop_streaming 分支
    - 后续 UI 操作全部 MagicMock 替换，避免依赖真实 Qt 控件
    """
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)

    # 会话切换哨兵
    inst._session_switched = False
    inst._pending_session_hook = False
    inst._is_streaming = streaming
    inst._is_auto_loop_running = False
    inst._is_destroyed = False
    inst._topic_summary_cancelled = False
    inst._session_dirty = True  # 发送消息时已置脏

    # backend
    backend = MagicMock()
    backend.chat_engine = MagicMock() if streaming else None
    backend.stop_streaming.return_value = interrupted_messages or []
    session = MagicMock()
    session.session_id = "new-session-001"
    backend.create_session.return_value = session
    backend.set_session_context = MagicMock()
    backend.clear_todo_list = MagicMock()
    inst.backend = backend

    # 关键 spy
    inst._apply_interrupted_messages_to_session = MagicMock(return_value=True)
    inst._set_ai_state = MagicMock()

    # 后续 UI 依赖（全部 mock 掉）
    inst._invalidate_welcome_card = MagicMock()
    inst._toggle_send_stop = MagicMock()
    inst._sub_agent_compact_widget = None
    inst._auto_save_current_session = MagicMock()
    inst._cache_current_session_cards = MagicMock()
    inst._batch_cards = []
    inst._message_batch = []
    inst._visible_batch_start = 0
    inst._visible_batch_end = 0
    inst._virtual_scroll_timer = MagicMock()
    inst._history_preview_messages = None
    inst._clear_chat_area = MagicMock()
    inst.title_edit = MagicMock()
    inst.node_preview = MagicMock()
    inst._update_history_questions_badge = MagicMock()
    inst._todo_floating_widget = None
    inst._question_floating_widget = None
    inst._question_tool_call_id = None
    inst._load_agent_list = MagicMock()
    inst._release_inactive_session_messages = MagicMock()
    inst._sync_dialog_title = MagicMock()
    inst._show_initial_welcome = MagicMock()
    inst._refresh_context_usage_indicator = MagicMock()
    inst._safe_timer_call = MagicMock()
    inst.session_manager = MagicMock()
    inst.history_manager = MagicMock()
    # 🆕 会话数据变更统一入口 mock 掉：本测试焦点是 streaming 中断保存链路，
    # 历史刷新/欢迎卡片失效由 test_history_panel_refresh_on_save 覆盖
    inst._notify_history_data_changed = MagicMock()
    # 🆕 历史面板刷新链路的 stub（_create_new_session 末尾会调用
    # refresh_history_card_if_visible）：设 None 走短路，避免 QWidget
    # 未初始化访问属性触发 RuntimeError
    inst._history_card = None
    inst._refresh_history_toggle_panel = MagicMock()
    return inst


class TestStreamingInterruptBehavior:
    """行为验证：streaming 中调用 _create_new_session 的行为"""

    @patch("PyQt5.sip.isdeleted", return_value=False)
    def test_stop_streaming_returns_applied_to_session(self, _mock_isdeleted):
        """streaming 分支：stop_streaming 返回值必须应用回 session"""
        interrupted = [{"role": "assistant", "content": "partial 回复"}]
        stub = _make_stub(streaming=True, interrupted_messages=interrupted)
        stub._create_new_session()
        stub.backend.stop_streaming.assert_called_once()
        stub._apply_interrupted_messages_to_session.assert_called_once_with(interrupted)

    @patch("PyQt5.sip.isdeleted", return_value=False)
    def test_set_ai_state_idle_called(self, _mock_isdeleted):
        """streaming 分支：必须调用 _set_ai_state('idle') 复位 Tab 状态"""
        stub = _make_stub(streaming=True, interrupted_messages=[{"role": "assistant", "content": "x"}])
        stub._create_new_session()
        stub._set_ai_state.assert_any_call("idle")

    @patch("PyQt5.sip.isdeleted", return_value=False)
    def test_no_streaming_skips_stop(self, _mock_isdeleted):
        """非 streaming 分支：不调用 stop_streaming / 不应用中断消息"""
        stub = _make_stub(streaming=False)
        stub._create_new_session()
        stub.backend.stop_streaming.assert_not_called()
        stub._apply_interrupted_messages_to_session.assert_not_called()

    @patch("PyQt5.sip.isdeleted", return_value=False)
    def test_empty_interrupted_skips_apply(self, _mock_isdeleted):
        """streaming 但中断消息为空：不调用 _apply_interrupted_messages_to_session"""
        stub = _make_stub(streaming=True, interrupted_messages=[])
        stub._create_new_session()
        stub.backend.stop_streaming.assert_called_once()
        stub._apply_interrupted_messages_to_session.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
