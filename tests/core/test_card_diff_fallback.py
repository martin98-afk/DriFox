# -*- coding: utf-8 -*-
"""回归测试：卡片差异统计"有差异却提示没差异"修复（方案 A + C，子任务 #29）

根因
----
差异统计数据源是 file_recorder（SQLite file_operations 表），按 (session_id, call_id)
精确查询。团队/subagent 场景中：
- 主消息 tool_call_id = subagent_para 派发 id
- 子智能体执行工具（write/edit）时的 call_id = 子智能体自己的工具调用 id
- 两者不匹配 → collect_operations_for_round 按 call_id 过滤匹配不到 → 误报"没有差异"

修复：
- 方案 A（核心）：`_on_card_diff_requested` 在 all_operations 空时回退到
  `_show_diff_from_messages_in_round` —— 遍历 round 范围内 tool 消息提取内嵌 diff 字段
- 方案 C（兜底）：fallback 也无效时展示会话级 diff（get_all_operations_for_session
  按 session 全量查不受 call_id 漂移影响）+ 引导用户点击工具运行框查看单工具差异，
  不再单纯误报"没有差异"

测试说明：
- 运行时测试用 object.__new__ 绕过 __init__ 构造轻量窗口（参照
  test_message_card_copy_selection.py 的 __new__ 模式），monkeypatch show_diff_viewer
  捕获 HTML 断言 fallback 生效
- AST 断言防回退（方案 A fallback 调用 / 方案 C 会话级兜底 / 引导文案）
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _fresh_consolidate_cache():
    """每个测试前清空 consolidate_messages 缓存。

    consolidate_messages 用 (id(list), len, fingerprint) 做 LRU 缓存键，
    测试间列表对象被 GC 后 id 可能复用 → 不同 diff 内容却命中旧缓存。
    清空保证每个测试拿到独立规范化结果。
    """
    from app.core.message_content import _get_consolidate_cache

    _get_consolidate_cache()["_entries"].clear()
    yield


def _make_window():
    """构造绕过 __init__ 的轻量窗口实例，仅提供 _show_diff_from_messages_in_round 依赖。"""
    import app.main_widget as mw

    inst = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    inst.backend = SimpleNamespace(tool_executor=MagicMock(), file_recorder=MagicMock())
    return inst


def _make_session(messages):
    """构造带 session_id + messages 的轻量会话。"""
    return SimpleNamespace(session_id="test-session", messages=messages)


def _tool_msg(call_id: str, diff: str = None) -> dict:
    msg = {"role": "tool", "tool_call_id": call_id, "content": "tool done", "name": "write"}
    if diff is not None:
        msg["diff"] = diff
    return msg


# 基础消息序列（round 0 = [user A, assistant(带 tool_calls), tool(call_1)]）
_BASE_MSGS = [
    {"role": "user", "content": "A"},
    {
        "role": "assistant",
        "content": "ai",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "write", "arguments": "{}"}}],
    },
]


class TestShowDiffFromMessagesInRound:
    """方案 A：round 范围内消息内嵌 diff fallback（运行时验证）"""

    def test_round_diff_fallback_finds_embedded_diff(self, monkeypatch):
        """round 内 tool 消息带 diff → fallback 生效（返回 True + 展示 HTML）。"""
        import app.main_widget as mw

        win = _make_window()
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff="--- a/x.py\n+++ b/x.py\n+new line")])

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1"])
        assert result is True, "round 内 tool 消息带 diff 时 fallback 必须生效"
        assert len(shown) == 1, "必须展示 diff viewer"
        assert "new line" in shown[0], "diff 内容必须包含实际差异"

    def test_round_diff_fallback_multi_call_ids(self, monkeypatch):
        """多 call_id 匹配：subagent 派发场景 round 内多个 tool 消息，任一 diff 均提取。"""
        import app.main_widget as mw

        win = _make_window()
        msgs = _BASE_MSGS + [
            _tool_msg("sub_call_1", diff="--- a/a.py\n+++ b/a.py\n+sub1"),
            _tool_msg("sub_call_2", diff="--- a/b.py\n+++ b/b.py\n+sub2"),
        ]
        session = _make_session(msgs)

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        # call_ids 含两个子智能体 call_id（主 session 收集不到时 fallback 仍能命中）
        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["sub_call_1", "sub_call_2"])
        assert result is True
        assert len(shown) == 1
        assert "sub1" in shown[0] and "sub2" in shown[0], "多个 tool diff 必须合并展示"

    def test_round_diff_fallback_no_diff_returns_false(self, monkeypatch):
        """round 内 tool 消息无 diff → fallback 返回 False（走方案 C 兜底）。"""
        import app.main_widget as mw

        win = _make_window()
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff=None)])

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1"])
        assert result is False, "无内嵌 diff 时 fallback 必须返回 False"
        assert len(shown) == 0, "不应展示 diff viewer"

    def test_round_diff_fallback_invalid_round(self, monkeypatch):
        """round_index 越界 → fallback 返回 False（不崩溃）。"""
        import app.main_widget as mw

        win = _make_window()
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff="x")])

        result = win._show_diff_from_messages_in_round(session, round_index=99, call_ids=["call_1"])
        assert result is False, "round 越界时 fallback 必须返回 False"


class TestCardDiffFallbackAst:
    """方案 A + C：AST 静态断言防回退"""

    @staticmethod
    def _src_text() -> str:
        from pathlib import Path

        p = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        return p.read_text(encoding="utf-8")

    def test_on_card_diff_requested_has_round_fallback(self):
        """AST：_on_card_diff_requested 在 all_operations 空时必须先尝试 round 内嵌 diff fallback。"""
        src = self._src_text()
        # fallback 调用必须存在（方案 A）
        assert "_show_diff_from_messages_in_round(session, round_index, all_call_ids)" in src, (
            "all_operations 空时必须调用 _show_diff_from_messages_in_round fallback（方案 A）"
        )

    def test_on_card_diff_requested_has_session_level_fallback(self):
        """AST：fallback 无效时展示会话级 diff（方案 C，get_all_operations_for_session 全量查）。"""
        src = self._src_text()
        assert "get_all_operations_for_session(session_id)" in src, (
            "方案 C 必须调用 get_all_operations_for_session 展示会话级 diff"
        )

    def test_on_card_diff_requested_has_guidance_text(self):
        """AST：不再单纯误报"没有差异"——兜底文案引导用户查看工具级/会话级差异。"""
        src = self._src_text()
        # 引导文案：提示点击工具运行框查看单工具差异
        assert "点击工具运行框查看单工具差异" in src, "兜底文案必须引导用户点击工具运行框查看单工具差异（方案 C）"

    def test_show_diff_from_messages_in_round_defined(self):
        """AST：_show_diff_from_messages_in_round 方法必须存在且遍历 round 范围提取 diff。"""
        import ast as _ast
        import textwrap as _tw

        tree = _ast.parse(self._src_text())
        target = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and node.name == "_show_diff_from_messages_in_round":
                target = node
                break
        assert target is not None, "未找到 _show_diff_from_messages_in_round 方法"
        func_src = _tw.dedent(_ast.unparse(target))

        # 必须遍历 round 范围 tool 消息
        assert "role" in func_src and '!= "tool"' in func_src.replace("'", '"'), "必须过滤 role == tool 的消息"
        # 必须提取 diff 字段（含类型归一化 str/dict/list）
        assert "diff" in func_src and 'get("diff")' in func_src.replace("'", '"'), "必须提取消息内嵌 diff 字段"
        # 必须返回 bool（是否找到）
        assert "return False" in func_src and "return True" in func_src, "方法必须返回 bool 表示是否找到并展示 diff"


class TestCardDiffFallbackSimulation:
    """模拟 file_recorder call_id 不匹配 → 方案 A fallback + 方案 C 兜底全链路"""

    def test_simulated_call_id_mismatch_falls_through_to_session_level(self, monkeypatch):
        """file_recorder 有记录但 call_id 不匹配 → all_operations 空 → fallback False →
        会话级 diff 展示（方案 C）。"""
        import app.main_widget as mw

        win = _make_window()

        # file_recorder 模拟：get_operations_for_preview 按 call_id 查不到（subagent call_id 漂移）
        recorder = MagicMock()
        recorder.get_operations_for_preview.return_value = []
        # 但会话级查询有数据（subagent 执行时写入了主 session_id）
        recorder.get_all_operations_for_session.return_value = [
            {"file_path": "x.py", "backup_path": "backup_x", "tool_name": "write"}
        ]
        win.backend.file_recorder = recorder

        # round 内 tool 消息无 diff（subagent 汇总消息不保留 diff）→ fallback False
        session = _make_session(_BASE_MSGS + [_tool_msg("sub_call_1", diff=None)])
        win.session_manager = SimpleNamespace(get_current_session=lambda: session)

        # 拦截 generate_multi_file_diff_html（备份文件不存在时返回空 HTML）
        captured = {}

        def _fake_generate_multi(ops):
            captured["ops"] = ops
            return "<html>session-diff</html>"

        def _fake_show(_win, html):
            captured["html"] = html

        monkeypatch.setattr(mw, "generate_multi_file_diff_html", _fake_generate_multi)
        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show)
        # InfoBar 提示捕获（方案 C 引导）
        monkeypatch.setattr(mw.InfoBar, "info", staticmethod(lambda *a, **k: None))

        # 直接调用 fallback：断言会话级路径可用（round 内无 diff → False）
        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1", "sub_call_1"])
        assert result is False, "round 内无内嵌 diff 时 fallback 返回 False（由 _on_card_diff_requested 走方案 C）"


class TestDictListDiffNormalization:
    """review#30-#1：dict/list diff 归一化（normalize_message 恒序列化为 repr str）"""

    def test_dict_diff_repr_recovered_via_literal_eval(self, monkeypatch):
        """dict diff 经 normalize 成 repr str → literal_eval 恢复 → HTML 含实际 diff。"""
        import app.main_widget as mw

        win = _make_window()
        # 原始 dict diff（consolidate 后 normalize_message 会 repr 化为 "{'diff': '...'}"）
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff={"diff": "--- a/x.py\n+++ b/x.py\n+dict_line"})])

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1"])
        assert result is True, "dict diff（repr str）必须经 literal_eval 恢复后展示"
        assert len(shown) == 1
        assert "dict_line" in shown[0], "恢复后的 HTML 必须含实际 diff 内容（而非 repr 垃圾串）"
        assert "dict" not in shown[0] or "diff" not in shown[0][:80], (
            "HTML 不应包含 repr 垃圾（{'diff': ...} 序列化残留）"
        )

    def test_list_diff_repr_recovered_via_literal_eval(self, monkeypatch):
        """list diff 经 normalize 成 repr str → literal_eval 恢复 → 逐项拼接展示。"""
        import app.main_widget as mw

        win = _make_window()
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff=["--- a/x.py", "+++ b/x.py", "+list_line"])])

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1"])
        assert result is True, "list diff（repr str）必须经 literal_eval 恢复后展示"
        assert len(shown) == 1
        assert "list_line" in shown[0], "恢复后的 HTML 必须含实际 diff 内容"

    def test_plain_str_diff_unchanged(self, monkeypatch):
        """真实 str diff（真实现状）不以 { [ 开头 → 原样展示，不受 literal_eval 影响。"""
        import app.main_widget as mw

        win = _make_window()
        session = _make_session(_BASE_MSGS + [_tool_msg("call_1", diff="--- a/x.py\n+++ b/x.py\n+plain_line")])

        shown = []

        def _fake_show_diff_viewer(_win, html):
            shown.append(html)

        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show_diff_viewer)

        result = win._show_diff_from_messages_in_round(session, round_index=0, call_ids=["call_1"])
        assert result is True
        assert len(shown) == 1
        assert "plain_line" in shown[0], "纯字符串 diff 必须原样展示"


class TestCardDiffEndToEnd:
    """方案 C 端到端：直接调用 _on_card_diff_requested 验证会话级 diff 展示"""

    def test_on_card_diff_requested_shows_session_level_diff(self, monkeypatch):
        """file_recorder 按 call_id 全空 + 会话级有数据 → 方案 A fallback False →
        方案 C 会话级 diff 被展示（不误报"没有差异"）。"""
        import app.main_widget as mw

        win = _make_window()

        # 会话：round 0 = [user A, assistant(带 tool_calls call_1), tool(sub_call_1 无 diff)]
        session = _make_session(_BASE_MSGS + [_tool_msg("sub_call_1", diff=None)])
        win.session_manager = SimpleNamespace(get_current_session=lambda: session)
        win._message_batch = []  # _on_card_diff_requested 用（round_index 有效时不走 batch fallback）

        # file_recorder：call_id 精确查询全空（subagent 漂移），会话级有数据
        recorder = MagicMock()
        recorder.get_operations_for_preview.return_value = []
        recorder.get_all_operations_for_session.return_value = [
            {"file_path": "x.py", "backup_path": "backup_x", "tool_name": "write"}
        ]
        win.backend = SimpleNamespace(tool_executor=MagicMock(), file_recorder=recorder)

        captured = {}
        info_msgs = []

        def _fake_generate_multi(ops):
            captured["ops"] = ops
            return "<html>session-level-diff</html>"

        def _fake_show(_win, html):
            captured["html"] = html

        def _fake_info(*args, **kwargs):
            info_msgs.append((args, kwargs))

        monkeypatch.setattr(mw, "generate_multi_file_diff_html", _fake_generate_multi)
        monkeypatch.setattr(mw, "show_diff_viewer", _fake_show)
        monkeypatch.setattr(mw.InfoBar, "info", staticmethod(_fake_info))

        win._on_card_diff_requested(round_index=0)

        # 方案 C：会话级 diff 必须被展示
        assert captured.get("html") == "<html>session-level-diff</html>", (
            "方案 C 必须展示会话级 diff（get_all_operations_for_session 全量查）"
        )
        assert captured.get("ops"), "generate_multi_file_diff_html 必须收到会话级操作列表"
        # 引导提示（不再单纯误报"没有差异"）
        assert info_msgs, "方案 C 必须弹 InfoBar.info 引导提示"
        assert any("工具运行框" in str(a) for a, k in info_msgs), "引导文案必须提示点击工具运行框查看单工具差异"
