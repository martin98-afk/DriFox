# -*- coding: utf-8 -*-
"""
子任务 #4 语义回归：gitee 同步后模型跟随语义。

- 本窗口用户「手动选过模型」(_user_manually_selected_model=True) → 同步时保持自身选择，
  不跟随云端（_load_model_configs 走默认分支，保留窗口选择）。
- 本窗口「从未手动选过」→ 同步时跟随云端 SelectedModel（force_global=True）。

用 OpenAIChatToolWindow.__new__ stub 隔离，直接验证 _apply_synced_model_selection 的分支语义。
"""

from types import MethodType

from app.main_widget import OpenAIChatToolWindow


def _make_stub(manually_selected: bool):
    """构造最小窗口 stub：记录 _load_model_configs 的 force_global 参数。"""
    calls = []

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._is_destroyed = False
    inst._user_manually_selected_model = manually_selected

    def _log_load(self, force_global: bool = False):
        calls.append(("load_model_configs", force_global))

    def _log_btn(self):
        calls.append(("update_model_selector_btn", None))

    def _log_ring(self):
        calls.append(("refresh_context_usage_indicator", None))

    inst._load_model_configs = MethodType(_log_load, inst)
    inst._update_model_selector_btn = MethodType(_log_btn, inst)
    inst._refresh_context_usage_indicator = MethodType(_log_ring, inst)
    return inst, calls


def _apply(stub):
    OpenAIChatToolWindow._apply_synced_model_selection(stub)


def test_manual_window_keeps_selection_on_sync():
    """手动选过模型的窗口 → 同步时不跟随云端（force_global=False 保持自身选择）"""
    stub, calls = _make_stub(manually_selected=True)
    _apply(stub)
    assert ("load_model_configs", False) in calls, "手动选过的窗口应走默认分支（保持自身选择）"
    assert ("load_model_configs", True) not in calls, "不应强制跟随云端"


def test_never_selected_window_follows_cloud_on_sync():
    """从未手动选过的窗口 → 同步时跟随云端（force_global=True）"""
    stub, calls = _make_stub(manually_selected=False)
    _apply(stub)
    assert ("load_model_configs", True) in calls, "未选过的窗口应强制跟随云端 SelectedModel"


def test_apply_skips_destroyed_window():
    """已销毁窗口不处理（幂等防护）"""
    stub, calls = _make_stub(manually_selected=False)
    stub._is_destroyed = True
    _apply(stub)
    assert calls == [], "已销毁窗口不应触发任何刷新"


def test_apply_does_not_re_call_update_model_selector_btn():
    """回归：gitee 同步后 _update_model_selector_btn 只通过 _load_model_configs
    末尾调用一次（之前同步末尾又会冗余调一次 → 重复触发 _refresh_coding_plan
    → request_coding_plan 缓存命中 emit → [CodingPlan] 收到数据 日志刷屏）。

    注：测试使用 stub，_load_model_configs 不会真实调 _update_model_selector_btn；
    只能断言 _apply_synced_model_selection 自身不直接调用 _update_model_selector_btn。
    """
    stub, calls = _make_stub(manually_selected=False)
    _apply(stub)
    direct_btn_calls = [c for c in calls if c[0] == "update_model_selector_btn"]
    assert direct_btn_calls == [], (
        f"_apply_synced_model_selection 不应直接调用 _update_model_selector_btn"
        f"（_load_model_configs 末尾已调一次），实际调用: {direct_btn_calls}"
    )
