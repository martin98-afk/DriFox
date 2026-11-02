# -*- coding: utf-8 -*-
"""回归测试：消息卡片页脚模型名点击切换 — 精确切回消息真实来源的服务商。

== 用户症状 ==
点击旧消息的模型名会切到错误的服务商，甚至该服务商根本没有那个模型。

== 根因 ==
1. `_append_assistant_message` 对无 config_id 的旧消息反查失败/多匹配时，
   兜底硬挂「当前窗口服务商」——不看归属，导致点击切错家。
2. `_on_footer_model_label_clicked` 丢了「服务商下没有该模型 → 拦截」校验。
3. 流式期间切换模型后，流结束/回传/stop/error 补写点用「结束时刻」的
   窗口选择覆盖卡片与消息归属。

== 修复语义 ==
- `_switch_to_message_model`：config_id 直查 + 归属校验 → (provider_hint,
  model_name) 反查 → 都失败返回 False（弹提示），绝不退而切当前服务商。
- `_model_belongs_to_provider`：「模型列表」非空且不含该模型 → False。
- `_round_model_snapshot`：卡片快照优先，防流式期间切换模型的竞态。
"""

import ast
from pathlib import Path
from types import SimpleNamespace

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


def _make_stub(valid_configs=None, card=None):
    """最小窗口桩：只装配模型解析/切换链路用到的状态。"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._valid_configs = dict(valid_configs or {})
    inst._current_provider_name = "cid-cur"
    inst._current_model_name = "model-cur"
    inst._display_to_config_id = {}  # __new__ 桩上 getattr 缺属性抛 RuntimeError，须预置
    inst._switch_calls = []  # 记录 _on_model_selected_from_popup 调用
    inst._current_assistant_card = card
    inst._on_model_selected_from_popup = lambda cid, model: inst._switch_calls.append((cid, model))
    inst._resolve_service_provider = lambda name: OpenAIChatToolWindow._resolve_service_provider(inst, name)
    inst._resolve_history_model_target = lambda m, p="", c="": OpenAIChatToolWindow._resolve_history_model_target(
        inst, m, p, c
    )
    return inst


_CFG = {
    "cid-cur": {"provider_name": "Cur", "display_name": "Cur", "模型名称": "model-cur", "模型列表": ["model-cur"]},
    "cid-a": {"provider_name": "Alpha", "display_name": "Alpha", "模型名称": "model-a", "模型列表": ["model-a"]},
    "cid-b": {"provider_name": "Beta", "display_name": "Beta #2", "模型名称": "model-b", "模型列表": ["model-b"]},
}


class TestModelBelongsToProvider:
    """_model_belongs_to_provider 的归属判定"""

    def test_matches_default_model_name(self):
        inst = _make_stub(_CFG)
        assert inst._model_belongs_to_provider("model-a", "cid-a") is True

    def test_matches_model_list(self):
        inst = _make_stub({"cid-x": {"模型名称": "other", "模型列表": ["m1", "m2"]}})
        assert inst._model_belongs_to_provider("m2", "cid-x") is True

    def test_rejects_when_list_nonempty_and_missing(self):
        """关键拦截：服务商模型列表非空且不含该模型 → False（防切错家）"""
        inst = _make_stub({"cid-x": {"模型名称": "other", "模型列表": ["m1", "m2"]}})
        assert inst._model_belongs_to_provider("model-a", "cid-x") is False

    def test_allows_when_list_empty(self):
        """列表为空/缺失 = 动态服务商，无法证伪 → 放行"""
        inst = _make_stub({"cid-x": {"模型名称": "other", "模型列表": []}})
        assert inst._model_belongs_to_provider("model-a", "cid-x") is True
        inst2 = _make_stub({"cid-y": {"模型名称": "other"}})
        assert inst2._model_belongs_to_provider("model-a", "cid-y") is True

    def test_model_list_as_string(self):
        inst = _make_stub({"cid-s": {"模型名称": "m0", "模型列表": "['m1', 'm2']"}})
        assert inst._model_belongs_to_provider("m2", "cid-s") is True
        assert inst._model_belongs_to_provider("zzz", "cid-s") is False

    def test_unknown_config_or_model(self):
        inst = _make_stub(_CFG)
        assert inst._model_belongs_to_provider("", "cid-a") is False
        assert inst._model_belongs_to_provider("model-a", "") is False
        assert inst._model_belongs_to_provider("model-a", "cid-gone") is False


class TestSwitchToMessageModel:
    """_switch_to_message_model：点击标签的精确切换"""

    def test_exact_config_id_switch(self):
        inst = _make_stub(_CFG)
        assert inst._switch_to_message_model("model-a", "cid-a", "") is True
        assert inst._switch_calls == [("cid-a", "model-a")]

    def test_wrong_provider_config_falls_back_to_hint(self):
        """用户主症状回归：config_id 指向的服务商没有该模型 → 用 provider_hint 反查，
        切到真实来源，而不是错误服务商"""
        cfgs = dict(_CFG)
        cfgs["cid-cur"] = {
            "provider_name": "Cur",
            "display_name": "Cur",
            "模型名称": "model-cur",
            "模型列表": ["model-cur", "model-z"],  # 不含 model-b
        }
        inst = _make_stub(cfgs)
        # 卡片挂着错误 config_id（当前服务商），但消息真实来源是 Beta
        assert inst._switch_to_message_model("model-b", "cid-cur", "Beta #2") is True
        assert inst._switch_calls == [("cid-b", "model-b")]

    def test_stale_config_id_resolved_by_hint(self):
        """config_id 失效（服务商被删/URL 改了）→ provider_hint 反查命中"""
        inst = _make_stub(_CFG)
        assert inst._switch_to_message_model("model-b", "cid-gone", "Beta") is True
        assert inst._switch_calls == [("cid-b", "model-b")]

    def test_unresolvable_returns_false_without_switch(self):
        """解析不到 → False，不切、不退化为当前服务商"""
        inst = _make_stub(_CFG)
        assert inst._switch_to_message_model("model-gone", "cid-gone", "Removed") is False
        assert inst._switch_calls == []

    def test_wrong_provider_without_hint_recovers_via_unique_match(self):
        """config_id 归属不成立、无 hint，但模型名全局唯一 → 反查命中真实服务商"""
        inst = _make_stub(_CFG)
        assert inst._switch_to_message_model("model-b", "cid-a", "") is True
        assert inst._switch_calls == [("cid-b", "model-b")]

    def test_wrong_provider_ambiguous_model_rejects(self):
        """config_id 归属不成立 + 模型名多匹配无 hint → 不明确，拒绝切换"""
        cfgs = {
            "cid-a": {"provider_name": "Alpha", "模型名称": "m-a", "模型列表": ["m-a"]},
            "cid-1": {"provider_name": "P1", "模型名称": "shared", "模型列表": ["shared"]},
            "cid-2": {"provider_name": "P2", "模型名称": "shared", "模型列表": ["shared"]},
        }
        inst = _make_stub(cfgs)
        assert inst._switch_to_message_model("shared", "cid-a", "") is False
        assert inst._switch_calls == []

    def test_empty_model_name(self):
        inst = _make_stub(_CFG)
        assert inst._switch_to_message_model("", "cid-a", "Alpha") is False
        assert inst._switch_calls == []


class TestRoundModelSnapshot:
    """_round_model_snapshot：补写元数据用卡片快照而非当前窗口状态"""

    def test_card_snapshot_wins(self):
        card = SimpleNamespace(model_name="model-a", _provider_config_id="cid-a")
        inst = _make_stub(_CFG, card=card)
        # 窗口当前已是别的选择，快照必须读卡片
        inst._current_model_name = "model-cur"
        inst._current_provider_name = "cid-cur"
        assert inst._round_model_snapshot() == ("model-a", "cid-a")

    def test_fallback_when_card_none(self):
        inst = _make_stub(_CFG, card=None)
        assert inst._round_model_snapshot() == ("model-cur", "cid-cur")

    def test_partial_card_snapshot(self):
        """卡片只有 config_id 没有 model_name（旧路径建卡）→ 部分快照仍优先"""
        card = SimpleNamespace(model_name="", _provider_config_id="cid-a")
        inst = _make_stub(_CFG, card=card)
        assert inst._round_model_snapshot() == ("", "cid-a")


class TestAstGuards:
    """AST 守卫：锁死修复语义，防回归"""

    def test_append_assistant_no_current_provider_fallback(self):
        """_append_assistant_message 不得硬挂当前服务商（根因 1）"""
        method = _method("_append_assistant_message")
        hits = [
            n
            for n in ast.walk(method)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "config_id" for t in n.targets)
            and isinstance(n.value, ast.Name)
            and n.value.id == "_current_provider_name"
        ]
        assert not hits, "config_id 反查失败时不得兜底挂 _current_provider_name"

    def test_footer_click_goes_through_switch_guard(self):
        """点击回调必须经 _switch_to_message_model（含归属校验），不得直切"""
        method = _method("_on_footer_model_label_clicked")
        assert any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_switch_to_message_model"
            for n in ast.walk(method)
        ), "_on_footer_model_label_clicked 必须调用 _switch_to_message_model"
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_on_model_selected_from_popup"
            for n in ast.walk(method)
        ), "点击回调不得绕过校验直调 _on_model_selected_from_popup"

    def test_finalize_writers_use_snapshot(self):
        """流结束/回传/stop/error 补写点必须走 _round_model_snapshot（根因 3）"""
        for name in ("_on_stream_finished", "_on_messages_updated", "_persist_stop_elapsed"):
            method = _method(name)
            assert any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_round_model_snapshot"
                for n in ast.walk(method)
            ), f"{name} 必须用 _round_model_snapshot 读本轮真实配置"

    def test_message_card_signal_carries_provider(self):
        """信号必须携带 provider_name，供 config_id 失效时反查"""
        card_src = (_REPO_ROOT / "app" / "widgets" / "message_card.py").read_text(encoding="utf-8")
        assert "pyqtSignal(str, str, str)  # model_name, config_id, provider_name" in card_src
        tree = ast.parse(card_src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_on_footer_model_clicked")
        emit = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "emit")
        assert len(emit.args) == 3, "modelLabelClicked.emit 必须传 (model_name, config_id, provider_name)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
