# -*- coding: utf-8 -*-
"""回归守卫：history-manager 全面走能力池，不得反射宿主私有成员。

历史背景（两轮）：
1. 2026-10-18：迁移时把 `_active_window()` 改成走 window.current（语义快照
   dict），但 9 个调用点做方法调用 → `AttributeError: 'dict' object has no
   attribute '_on_history_session_selected'`，历史会话切换失效。
2. 2026-10-29：能力池全量收敛。插件原先
   - 直连 `app.utils.history_manager.HistoryManager` 单例取历史数据
   - 用 `_call_window("_on_xxx", ...)` 反射宿主窗口 17 个私有方法
   - 用 `getattr(win, "_current_project"/"_current_session_id"/"_window_id")`
     读宿主私有字段
   - 自建 `host=None` 能力池并自报 `declared={cap_id, ...}`（等于自己给自己
     发权限，manifest 声明恒不生效）

现约定：数据面走 `session.history` / `session.history.write` /
`session.manage` / `project.read` / `project.write` / `team.session`，
窗口语义走 `window.current` / `window.host` / `window.events`。
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "history-manager"

# 宿主私有方法/字段名：插件源码里一律不得出现（含 getattr 字符串形式）
_FORBIDDEN_CALL_WINDOW = "_call_window"
_FORBIDDEN_FIELDS = (
    "_current_project",
    "_current_session_id",
    "_window_id",
    "_build_project_meta_map",
    "_build_project_root_dir_map",
)


def _plugin_sources():
    for path in sorted(PLUGIN.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_no_private_history_manager_import():
    """插件不得直连 HistoryManager 单例（数据面必须经 session.history 能力）。"""
    bad = [p.name for p, src in _plugin_sources() if "history_manager import" in src or "HistoryManager.get_instance" in src]
    assert not bad, f"仍直连 HistoryManager: {bad}"


def test_no_call_window_reflection():
    """不得用 _call_window 反射宿主私有方法（依赖面在代码里不可见）。"""
    bad = [p.name for p, src in _plugin_sources() if _FORBIDDEN_CALL_WINDOW in src.split("原实现用")[0]]
    assert not bad, f"仍用 _call_window 反射: {bad}"


def test_no_private_host_field_access():
    """不得读写宿主私有字段（项目名/会话 id/窗口 id 走 window.current 快照）。"""
    bad = []
    for path, src in _plugin_sources():
        for field in _FORBIDDEN_FIELDS:
            # getattr(win, "_xxx") 与 win._xxx 两种形态都算
            if f'"{field}"' in src or f"'{field}'" in src:
                bad.append(f"{path.name}: attr {field}")
    assert not bad, "仍访问宿主私有字段: " + ", ".join(bad)


def test_no_self_granted_capability_pool():
    """不得自建 host=None 池并自报 declared（等于插件自己发权限）。"""
    bad = []
    for path, src in _plugin_sources():
        if "build_pool(" in src and "declared=" in src:
            bad.append(path.name)
    assert not bad, f"仍自建能力池自报声明: {bad}"


def test_caps_uses_sdk_caps_for():
    """两处 _caps 必须收敛到 sdk.caps_for（host 由宿主解析）。"""
    for name in ("history_page.py", "history_card.py"):
        src = (PLUGIN / "ui" / name).read_text(encoding="utf-8")
        idx = src.find("def _caps")
        assert idx > 0, f"{name} 缺少 _caps"
        body = src[idx : idx + 800]
        assert "from app.plugins.sdk import caps_for" in body, f"{name}._caps 未走 sdk.caps_for"
        assert "return caps_for(cap_id)" in body, f"{name}._caps 未直接返回 caps_for"


def test_declared_capabilities_match_usage():
    """manifest 声明的能力必须覆盖源码里用到的全部 _caps("x") 字面量。"""
    import json

    manifest = json.loads((PLUGIN / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("capabilities") or [])
    assert manifest.get("api_version") == 2
    assert declared, "必须显式声明能力清单"

    used = set()
    for path, src in _plugin_sources():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_caps"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                used.add(node.args[0].value)

    missing = used - declared
    assert not missing, f"用到但未声明: {sorted(missing)}"


@pytest.mark.parametrize(
    "cap_id",
    ["session.history", "session.history.write", "session.manage", "project.read", "project.write", "team.session"],
)
def test_new_history_capabilities_registered(cap_id):
    """新增的六条能力必须在 catalog 登记并在 ui surface 注册、可构造。"""
    from unittest.mock import MagicMock

    from app.plugins.capabilities import bootstrap, catalog
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    try:
        reg = CapabilityRegistry.get_instance()
        bootstrap.ensure_registered("ui")
        assert catalog.get(cap_id) is not None, f"{cap_id} 未在 catalog 登记"
        assert reg.has(cap_id, "ui"), f"{cap_id} 未在 ui surface 注册"
        pool = reg.build_pool(surface="ui", host=MagicMock(), plugin="probe", declared={cap_id})
        value = pool[cap_id]
        assert value is not None, f"{cap_id} provider 返回 None"
    finally:
        CapabilityRegistry.reset_instance()
        bootstrap.reset_bootstrap()
