# -*- coding: utf-8 -*-
"""回归测试：会话保存链路 + 团队会话 + 欢迎卡片过滤

== 修复背景（2026-08-04）==
排查"会话保存逻辑 + 团队会话 + 欢迎卡片"整体发现 4 个问题：

1. 欢迎卡片未过滤团队会话：_get_or_create_welcome_card 用
   get_history_list(project)（无 merge_team），团队会话（team_run_id 非空）
   逐条混入推荐列表，与历史面板 merge_team=True 语义不一致。
   → 修复：构造推荐列表前过滤 team_run_id 非空的条目。

2. _switch_to_session_by_id（欢迎卡片点会话/跨窗口切换）：
   - 保存顺序颠倒：先 _save_current_session_to_history() 再 stop_streaming()，
     保存的是旧消息（缺最后 partial 回复）
   - stop_streaming() 返回值丢弃 → 中断消息（partial）永久丢失
   - 未 _set_ai_state("idle") → TabPanel 边框停留在 streaming 动画
   → 修复：先 stop 并应用中断消息，再保存；补 _set_ai_state("idle")。

3. _switch_to_session_by_id 无 F4 团队标记同步：加载团队会话后
   _team_run_id / _team_name / _team_agent_name 未恢复 → 后续保存
   team_kwargs 守卫失效 → 团队会话被当普通会话保存，元数据被清空。
   → 修复：提取 _sync_team_markers_from_record 公共方法，两个加载路径共用。

4. _load_session_from_record 打断流式时 partial 丢失：
   _on_stop_clicked() 是异步两阶段（cancel + deferred finalize），
   deferred finalize 在 _session_switched 哨兵置位后才执行 →
   _on_finalize_complete 因哨兵丢弃中断消息。
   → 修复：_on_stop_clicked() 后同步 finalize_stop() 并应用中断消息，
     再 auto_save（幂等：_finalize_worker 单次消费，deferred 再跑无害）。

== 测试策略 ==
- AST 静态检查：保证关键修复点存在（过滤 / 顺序 / 团队标记同步）
- 行为测试：stub 窗口验证欢迎卡片过滤团队会话
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


def _source_contains(method: ast.FunctionDef, text: str) -> bool:
    return text in ast.unparse(method)


def _calls_with_arg(method: ast.FunctionDef, attr: str, arg: str) -> bool:
    """方法体内是否出现 self.<attr>("<arg>") 形式的调用（节点级匹配）"""
    for sub in ast.walk(method):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not (isinstance(func, ast.Attribute) and func.attr == attr):
            continue
        if sub.args and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == arg:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# 1. 欢迎卡片过滤团队会话
# ═══════════════════════════════════════════════════════════════


class TestWelcomeCardTeamFilter:
    """欢迎卡片推荐列表必须过滤团队会话"""

    def test_get_or_create_welcome_card_filters_team(self):
        """_get_or_create_welcome_card 构造推荐列表前过滤 team_run_id"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_get_or_create_welcome_card")
        assert _source_contains(method, "team_run_id"), (
            "_get_or_create_welcome_card 必须过滤 team_run_id 非空的团队会话，"
            "否则团队会话逐条混入欢迎卡片推荐列表。"
        )
        # 过滤必须发生在 recent_sessions 构造之前
        src = ast.unparse(method)
        filter_pos = src.find("team_run_id")
        recent_pos = src.find("recent_sessions = []")
        assert filter_pos != -1 and recent_pos != -1
        assert filter_pos < recent_pos, "团队会话过滤必须在 recent_sessions 构造之前"

    def test_switch_to_session_syncs_team_markers(self):
        """_switch_to_session_by_id 加载会话后必须同步团队标记（F4）"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_switch_to_session_by_id")
        assert _source_contains(method, "_sync_team_markers_from_record"), (
            "_switch_to_session_by_id 缺少 F4 团队标记同步，加载团队会话后"
            "_team_run_id 未恢复 → 后续保存把团队会话当普通会话，元数据被清空。"
        )

    def test_switch_to_session_stops_before_save(self):
        """_switch_to_session_by_id 必须先停止流式（应用中断消息）再保存"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_switch_to_session_by_id")
        assert _source_contains(method, "interrupted = self.backend.stop_streaming()"), (
            "_switch_to_session_by_id 必须接收 stop_streaming() 返回值（中断消息），"
            "否则切换会话时被打断的回复丢失。"
        )
        assert _source_contains(method, "self._apply_interrupted_messages_to_session(interrupted)"), (
            "_switch_to_session_by_id 必须把中断消息应用回 session。"
        )
        assert _calls_with_arg(method, "_set_ai_state", "idle"), (
            "_switch_to_session_by_id 必须 _set_ai_state('idle')，否则 TabPanel "
            "边框停留在 streaming 动画。"
        )
        # 顺序：stop_streaming 在 _save_current_session_to_history 之前
        src = ast.unparse(method)
        stop_pos = src.find("self.backend.stop_streaming()")
        save_pos = src.find("self._save_current_session_to_history()")
        assert stop_pos != -1 and save_pos != -1
        assert stop_pos < save_pos, (
            "必须先停止流式（应用中断消息）再保存，否则保存的是缺 partial 的旧消息。"
        )

    def test_sync_team_markers_method_exists(self):
        """_sync_team_markers_from_record 公共方法存在"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_sync_team_markers_from_record")
        assert method is not None, "缺少 _sync_team_markers_from_record 公共方法"


class TestLoadSessionFromRecordInterrupt:
    """_load_session_from_record 打断流式必须同步应用中断消息"""

    def test_sync_finalize_after_stop_clicked(self):
        """_on_stop_clicked() 后必须同步 finalize_stop 应用中断消息"""
        tree = _load_module_ast()
        cls = _get_class(tree, "OpenAIChatToolWindow")
        method = _get_method(cls, "_load_session_from_record")
        assert _source_contains(method, "self.backend.finalize_stop()"), (
            "_load_session_from_record 打断流式时必须同步 finalize_stop() 拿中断消息，"
            "否则 deferred finalize 被 _session_switched 哨兵拦截 → partial 丢失。"
        )
        # 顺序：同步 finalize 在 _session_switched = True 之前
        src = ast.unparse(method)
        fin_pos = src.find("self.backend.finalize_stop()")
        sentinel_pos = src.find("self._session_switched = True")
        assert fin_pos != -1 and sentinel_pos != -1
        assert fin_pos < sentinel_pos, "同步 finalize 必须在哨兵置位之前"


# ═══════════════════════════════════════════════════════════════
# 2. 行为测试
# ═══════════════════════════════════════════════════════════════


class TestWelcomeCardBehavior:
    """行为验证：欢迎卡片推荐列表过滤团队会话"""

    @patch("PyQt5.sip.isdeleted", return_value=False)
    def test_recent_sessions_exclude_team(self, _mock_isdeleted):
        """recent_sessions 不应包含 team_run_id 非空的会话"""
        from app.main_widget import OpenAIChatToolWindow

        inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        inst._window_id = "win_test"
        inst._welcome_card_cache = {}
        inst._current_project = "默认项目"
        inst._current_agent = "default"
        inst.backend = MagicMock()
        agent = MagicMock()
        agent.name = "测试Agent"
        agent.description = "desc"
        inst.backend.get_agent.return_value = agent

        # 3 条普通 + 3 条团队会话（团队会话 last_time 更新 → 若未过滤会占位）
        inst.history_manager = MagicMock()
        inst.history_manager.get_history_list.return_value = [
            {"session_id": "t1", "title": "团队1", "last_time": "2026-08-04 12:00", "message_count": 50, "team_run_id": "run1"},
            {"session_id": "t2", "title": "团队2", "last_time": "2026-08-04 11:00", "message_count": 40, "team_run_id": "run2"},
            {"session_id": "t3", "title": "团队3", "last_time": "2026-08-04 10:00", "message_count": 30, "team_run_id": "run3"},
            {"session_id": "n1", "title": "普通1", "last_time": "2026-08-04 09:00", "message_count": 10},
            {"session_id": "n2", "title": "普通2", "last_time": "2026-08-04 08:00", "message_count": 5},
        ]

        from app.widgets.message_card import create_welcome_card
        from unittest.mock import patch as _patch

        # stub create_welcome_card 捕获传入的 recent_sessions
        captured = {}

        def _fake_create_welcome_card(parent, agent_name, agent_desc, recent, top, **kwargs):
            captured["recent"] = recent
            captured["top"] = top
            card = MagicMock()
            card._is_welcome = True
            return card

        with _patch("app.main_widget.create_welcome_card", side_effect=_fake_create_welcome_card):
            inst._get_or_create_welcome_card()

        assert captured["recent"], "应构造 recent_sessions"
        recent_ids = [s["session_id"] for s in captured["recent"]]
        assert "t1" not in recent_ids and "t2" not in recent_ids and "t3" not in recent_ids, (
            f"欢迎卡片 recent_sessions 不应包含团队会话: {recent_ids}"
        )
        assert recent_ids == ["n1", "n2"], f"普通会话应保留（按时间排序）: {recent_ids}"
        top_ids = [s["session_id"] for s in captured["top"]]
        assert "t1" not in top_ids, f"欢迎卡片 top_by_count 不应包含团队会话: {top_ids}"
        assert "n1" in top_ids, "普通会话应进入 top_by_count"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
