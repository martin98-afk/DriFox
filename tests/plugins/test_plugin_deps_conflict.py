# -*- coding: utf-8 -*-
"""L2 回归：跨插件依赖冲突门禁

背景：所有插件的 deps 目录都 insert 到同一个 sys.path，模块一旦进 sys.modules
就锁死。两个插件对同一个包要求不可调和的版本时，后加载者静默跑在对方版本上，
报错现场离根因隔几十层。

覆盖口径：
1. 冲突对里让出方被 load_blocked，deps 路径从 sys.path 摘除
2. 裁决优先级 system > claude > user，同类型按名字字典序（结果稳定可复现）
3. 冲突消失后二次裁决清空原因（热重载/卸载后不能永久拦截）
4. 裸包名 / 有交集的版本区间不算冲突（不误杀）
"""

import sys
from pathlib import Path

from app.plugins import deps_loader
from app.plugins.deps_loader import ensure_deps_on_path
from app.plugins.managers.plugin_manager import PluginInfo, PluginManager, _pick_deps_winner


def _info(tmp_path: Path, name: str, specs, plugin_type: str = "user") -> PluginInfo:
    """造一个带 deps 目录的 PluginInfo（deps 非空才会被注入 sys.path）。"""
    d = tmp_path / name
    (d / "deps").mkdir(parents=True, exist_ok=True)
    (d / "deps" / "marker.py").write_text("", encoding="utf-8")
    return PluginInfo(
        name=name,
        manifest={"name": name, "dependencies": {"pip": {"default": list(specs)}}},
        path=d,
        plugin_type=plugin_type,
    )


def _pm(plugins) -> PluginManager:
    pm = PluginManager.__new__(PluginManager)
    pm._plugins = {p.name: p for p in plugins}
    return pm


def _cleanup(*plugins: PluginInfo) -> None:
    for p in plugins:
        deps_loader.remove_deps_from_path(p.path)


class TestDepsConflictGate:
    def test_loser_blocked_and_detached(self, tmp_path: Path):
        a = _info(tmp_path, "aaa", ["httpx>=0.28"], "system")
        b = _info(tmp_path, "bbb", ["httpx<0.24"], "user")
        try:
            ensure_deps_on_path(a.path)
            ensure_deps_on_path(b.path)
            assert any(str(b.path) in p for p in sys.path)

            _pm([a, b])._apply_deps_conflict_gate({a.name: a, b.name: b})

            assert a.deps_conflict_reason == ""
            assert a.load_blocked is False
            assert "httpx" in b.deps_conflict_reason
            assert "aaa" in b.deps_conflict_reason
            assert b.load_blocked is True
            # 让出方 deps 路径已摘除，不再污染 sys.path
            assert not any(str(b.path) in p for p in sys.path)
            assert any(str(a.path) in p for p in sys.path)
        finally:
            _cleanup(a, b)

    def test_system_wins_over_claude_and_user(self, tmp_path: Path):
        s = _info(tmp_path, "zzz-system", ["pyyaml<6"], "system")
        c = _info(tmp_path, "aaa-claude", ["pyyaml>=6"], "claude")
        u = _info(tmp_path, "000-user", ["pyyaml>=6"], "user")
        try:
            _pm([s, c, u])._apply_deps_conflict_gate({x.name: x for x in (s, c, u)})
            assert s.deps_conflict_reason == ""
            assert c.deps_conflict_reason and u.deps_conflict_reason
        finally:
            _cleanup(s, c, u)

    def test_same_type_uses_stable_name_order(self, tmp_path: Path):
        a = _info(tmp_path, "alpha", ["pkg<1"], "user")
        b = _info(tmp_path, "beta", ["pkg>=1"], "user")
        try:
            _pm([a, b])._apply_deps_conflict_gate({a.name: a, b.name: b})
            # 字典序小者保留，保证每次启动裁决一致
            assert a.deps_conflict_reason == ""
            assert b.deps_conflict_reason != ""
            assert _pick_deps_winner({a.name: a, b.name: b}, "beta", "alpha") == ("alpha", "beta")
        finally:
            _cleanup(a, b)

    def test_reason_cleared_when_conflict_gone(self, tmp_path: Path):
        a = _info(tmp_path, "aaa", ["httpx>=0.28"])
        b = _info(tmp_path, "bbb", ["httpx<0.24"])
        try:
            plugins = {a.name: a, b.name: b}
            _pm([a, b])._apply_deps_conflict_gate(plugins)
            assert b.deps_conflict_reason

            # 用户升级 b 的声明 → 冲突消失，二次裁决必须清空（否则永久拦截）
            b.manifest = {"name": "bbb", "dependencies": {"pip": {"default": ["httpx>=0.28"]}}}
            _pm([a, b])._apply_deps_conflict_gate(plugins)
            assert b.deps_conflict_reason == ""
            assert b.load_blocked is False
        finally:
            _cleanup(a, b)

    def test_compatible_ranges_not_blocked(self, tmp_path: Path):
        a = _info(tmp_path, "aaa", ["httpx>=0.27"])
        b = _info(tmp_path, "bbb", ["httpx>=0.28", "pyyaml"])
        try:
            _pm([a, b])._apply_deps_conflict_gate({a.name: a, b.name: b})
            assert a.deps_conflict_reason == ""
            assert b.deps_conflict_reason == ""
        finally:
            _cleanup(a, b)

    def test_single_plugin_noop(self, tmp_path: Path):
        a = _info(tmp_path, "aaa", ["httpx>=0.28"])
        try:
            _pm([a])._apply_deps_conflict_gate({a.name: a})
            assert a.deps_conflict_reason == ""
        finally:
            _cleanup(a)
