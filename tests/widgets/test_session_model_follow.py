# -*- coding: utf-8 -*-
"""回归测试：加载历史会话时自动跟随该会话最后使用的模型。

== 语义 ==
- 倒序扫 assistant 消息，取第一条带 model_name 的（最后使用的那次）
- 解析优先级：config_id 直查 → provider_name/display_name 反查 → 模型名唯一匹配
- 解析不到（模型已删 / 服务商已移除）→ 保持当前窗口选择，不切
- 只改窗口实例态，不写全局默认模型（不走 _on_model_selected_from_popup）
- 会话切换路径已接：_load_session_from_record / _switch_to_session_by_id
- 窗口早期 _valid_configs 未加载时暂存，_load_model_configs 就绪后补应用
"""

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_PATH = _REPO_ROOT / "app" / "main_widget.py"


def _tree() -> ast.Module:
    return ast.parse(_SRC_PATH.read_text(encoding="utf-8"), filename=str(_SRC_PATH))


def _method(name: str) -> ast.FunctionDef:
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {name}")


def _make_stub(valid_configs=None):
    """构造最小窗口 stub：只装配本功能用到的状态与依赖。

    `_resolve_history_model_target` 会调用 `_resolve_service_provider`，
    直接绑同类的真实实现（纯 _valid_configs 查表，无 Qt 依赖）。
    """
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._valid_configs = dict(valid_configs or {})
    inst._current_provider_name = "cid-a"
    inst._current_model_name = "model-a"
    inst._user_manually_selected_model = False
    inst._pending_model_restore = None
    inst._refresh_count = 0
    # PyQt5 未初始化 wrapper 上 getattr 缺失属性抛 RuntimeError 而非 AttributeError，
    # `_resolve_service_provider` 内部 getattr 兜底因此失效，必须预置进实例字典
    inst._display_to_config_id = {}
    # 避免真实刷新链（Qt 控件 / 网络）在单测里跑
    inst._update_model_selector_btn = lambda: None
    inst._refresh_context_usage_indicator = lambda: setattr(inst, "_refresh_count", inst._refresh_count + 1)
    inst._resolve_service_provider = lambda name: OpenAIChatToolWindow._resolve_service_provider(inst, name)
    return inst


_CFG = {
    "cid-a": {"provider_name": "Alpha", "display_name": "Alpha", "模型名称": "model-a", "模型列表": ["model-a"]},
    "cid-b": {"provider_name": "Beta", "display_name": "Beta #2", "模型名称": "model-b", "模型列表": ["model-b"]},
}


class TestResolveTarget:
    """_resolve_history_model_target 的匹配优先级"""

    def test_config_id_wins(self):
        inst = _make_stub(_CFG)
        assert inst._resolve_history_model_target("model-b", "", "cid-b") == ("cid-b", "model-b")

    def test_provider_hint_display_name(self):
        inst = _make_stub(_CFG)
        # 历史消息存的是 display_name（"Beta #2"），不是 provider_name
        assert inst._resolve_history_model_target("model-b", "Beta #2", "") == ("cid-b", "model-b")

    def test_provider_hint_provider_name(self):
        inst = _make_stub(_CFG)
        assert inst._resolve_history_model_target("model-b", "Beta", "") == ("cid-b", "model-b")

    def test_model_name_unique_match(self):
        inst = _make_stub(_CFG)
        assert inst._resolve_history_model_target("model-b", "", "") == ("cid-b", "model-b")

    def test_model_in_list_only(self):
        """模型只在「模型列表」里、不是 provider 当前「模型名称」也能匹配"""
        cfgs = {
            "cid-a": {"provider_name": "Alpha", "模型名称": "model-a", "模型列表": ["model-a"]},
            "cid-c": {"provider_name": "Gamma", "模型名称": "other", "模型列表": ["other", "model-x"]},
        }
        inst = _make_stub(cfgs)
        assert inst._resolve_history_model_target("model-x", "", "") == ("cid-c", "model-x")

    def test_model_list_as_string(self):
        """老记录里「模型列表」可能是 list 的字符串形态"""
        cfgs = {"cid-s": {"provider_name": "S", "模型名称": "m0", "模型列表": "['m1', 'm2']"}}
        inst = _make_stub(cfgs)
        assert inst._resolve_history_model_target("m2", "", "") == ("cid-s", "m2")

    def test_ambiguous_match_gives_up(self):
        """同名模型出现在多个服务商 → 不明确，不切换"""
        cfgs = {
            "cid-1": {"provider_name": "P1", "模型名称": "shared", "模型列表": ["shared"]},
            "cid-2": {"provider_name": "P2", "模型名称": "shared", "模型列表": ["shared"]},
        }
        inst = _make_stub(cfgs)
        assert inst._resolve_history_model_target("shared", "", "") is None

    def test_unknown_model_returns_none(self):
        inst = _make_stub(_CFG)
        assert inst._resolve_history_model_target("gone-model", "Removed", "cid-gone") is None

    def test_empty_configs_returns_none(self):
        inst = _make_stub({})
        assert inst._resolve_history_model_target("model-a", "Alpha", "cid-a") is None


class TestFollowSessionModel:
    """_follow_session_model 的行为"""

    def test_follows_last_assistant_model(self):
        inst = _make_stub(_CFG)
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "model_name": "model-a", "provider_name": "Alpha", "config_id": "cid-a"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "model_name": "model-b", "provider_name": "Beta #2", "config_id": "cid-b"},
        ]
        inst._follow_session_model(messages)
        assert (inst._current_provider_name, inst._current_model_name) == ("cid-b", "model-b")

    def test_skips_trailing_assistant_without_model(self):
        """末尾 assistant 无 model_name（如中断的半条）→ 继续往前找"""
        inst = _make_stub(_CFG)
        messages = [
            {"role": "assistant", "model_name": "model-b", "provider_name": "Beta #2", "config_id": "cid-b"},
            {"role": "assistant", "content": "无元数据"},
        ]
        inst._follow_session_model(messages)
        assert inst._current_model_name == "model-b"

    def test_no_model_metadata_keeps_current(self):
        inst = _make_stub(_CFG)
        inst._follow_session_model([{"role": "user", "content": "hi"}])
        assert (inst._current_provider_name, inst._current_model_name) == ("cid-a", "model-a")
        assert inst._refresh_count == 0

    def test_unresolvable_model_keeps_current(self):
        """模型已被删除 → 不切换，也不报错"""
        inst = _make_stub(_CFG)
        inst._follow_session_model(
            [{"role": "assistant", "model_name": "removed-model", "provider_name": "Gone", "config_id": "cid-gone"}]
        )
        assert (inst._current_provider_name, inst._current_model_name) == ("cid-a", "model-a")

    def test_same_model_short_circuits(self):
        """已经是该模型 → 不重复触发刷新"""
        inst = _make_stub(_CFG)
        inst._follow_session_model(
            [{"role": "assistant", "model_name": "model-a", "provider_name": "Alpha", "config_id": "cid-a"}]
        )
        assert inst._refresh_count == 0

    def test_marks_manual_selection(self):
        """跟随后的模型不被云端配置同步覆盖"""
        inst = _make_stub(_CFG)
        inst._follow_session_model(
            [{"role": "assistant", "model_name": "model-b", "provider_name": "Beta #2", "config_id": "cid-b"}]
        )
        assert inst._user_manually_selected_model is True

    def test_pending_when_configs_not_loaded(self):
        """窗口早期 _valid_configs 为空 → 暂存待补，不静默丢弃"""
        inst = _make_stub({})
        inst._follow_session_model(
            [{"role": "assistant", "model_name": "model-b", "provider_name": "Beta #2", "config_id": "cid-b"}]
        )
        assert inst._pending_model_restore == ("model-b", "Beta #2", "cid-b")
        assert inst._current_model_name == "model-a"

    def test_pending_applied_after_configs_loaded(self):
        """配置就绪后补应用暂存模型"""
        inst = _make_stub(_CFG)
        inst._pending_model_restore = ("model-b", "Beta #2", "cid-b")
        inst._user_manually_selected_model = False
        # 复用 _load_model_configs 末尾的补应用逻辑：直接调 _follow_session_model
        pending = inst._pending_model_restore
        inst._pending_model_restore = None
        inst._follow_session_model(
            [{"role": "assistant", "model_name": pending[0], "provider_name": pending[1], "config_id": pending[2]}]
        )
        assert inst._current_model_name == "model-b"

    def test_bad_input_silent(self):
        inst = _make_stub(_CFG)
        inst._follow_session_model(None)
        inst._follow_session_model("not-a-list")
        inst._follow_session_model([None, 42, {"role": "user"}])
        assert inst._current_model_name == "model-a"


class TestWiring:
    """AST：两条历史会话加载路径必须调用 _follow_session_model"""

    @pytest.mark.parametrize("method_name", ["_load_session_from_record", "_switch_to_session_by_id"])
    def test_load_paths_follow_model(self, method_name):
        method = _method(method_name)
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_follow_session_model"
            for node in ast.walk(method)
        )
        assert called, f"{method_name} 必须调用 _follow_session_model"

    def test_load_model_configs_applies_pending(self):
        method = _method("_load_model_configs")
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_follow_session_model"
            for node in ast.walk(method)
        )
        assert called, "_load_model_configs 必须在配置就绪后补应用 _pending_model_restore"

    def test_does_not_use_popup_selection(self):
        """不得走 _on_model_selected_from_popup（会 save=True 污染全局默认模型）"""
        method = _method("_follow_session_model")
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_on_model_selected_from_popup"
            for node in ast.walk(method)
        )
        assert not called, "_follow_session_model 不得调用 _on_model_selected_from_popup"
