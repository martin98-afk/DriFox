"""通过宿主增量入口验证组件注册、失败恢复与同名插件回退。"""

import json
import shutil
import sys
from pathlib import Path

import pytest

from app.plugins.loaders import runtime_component_loader as runtime
from app.plugins.registries.context_policy_registry import ContextPolicyRegistry
from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from tools.eol_guard import write_text_keep_eol


def _policy(directory, name, version):
    (directory / "loop_policies").mkdir(parents=True, exist_ok=True)
    write_text_keep_eol(
        directory / "loop_policies" / f"{name}.py",
        f"class Policy:\n    id = {name!r}\n    def version(self): return {version!r}\n"
        "def register(registry):\n    registry.register(Policy())\n",
    )


def _manifest(directory, name):
    manifest = directory / ".drifox-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    write_text_keep_eol(manifest, json.dumps({"name": name, "version": "1.0.0"}))


@pytest.fixture
def isolated_policies(plugin_reload_host, monkeypatch):
    pm, svc = plugin_reload_host
    roots = [pm._SYSTEM_PLUGIN_DIR, pm._app_data_dir / "plugins"]
    registry = LoopPolicyRegistry()
    loader = runtime.RuntimeComponentLoader("loop_policies", registry)
    loader.scan_roots(roots)
    watcher = runtime._RuntimeWatcher(loader, "loop_policies")
    monkeypatch.setattr(runtime, "ensure_loop_policy_watcher", lambda: watcher)
    yield roots, registry, svc
    loader.unload_plugin("reload-example")


def test_partial_registration_failure_and_retry(isolated_policies):
    roots, reg, svc = isolated_policies
    name = "reload-example"
    plugin = roots[0] / name
    _manifest(plugin, name)
    for key in ("first", "second"):
        _policy(plugin, key, "v1")
    assert svc._reload_single_plugin(name, "__manifest__")["loop_policies"]
    original = reg.policies()
    module_name = "drifox_rt_loop_policies_reload-example_first"
    old_module = sys.modules[module_name]
    _policy(plugin, "first", "v2")
    bad = plugin / "loop_policies" / "second.py"
    write_text_keep_eol(bad, bad.read_text() + '    raise RuntimeError("registration failed")\n')
    assert svc._reload_single_plugin(name, "loop_policies")["loop_policies"] is False
    assert sys.modules[module_name] is old_module
    assert all(reg.policies()[key] is obj and obj.version() == "v1" for key, obj in original.items())
    _policy(plugin, "second", "v2")
    assert svc._reload_single_plugin(name, "loop_policies")["loop_policies"]
    assert [reg.policies()[key].version() for key in original] == ["v2", "v2"]


@pytest.mark.parametrize("allow", [True, False])
def test_selected_copy_install_uninstall_and_restart(isolated_policies, plugin_reload_host, monkeypatch, allow):
    roots, registry, svc = isolated_policies
    pm, _ = plugin_reload_host
    monkeypatch.setattr(pm, "_allow_user_override", lambda: allow)
    name = "reload-example"
    system, user = (root / name for root in roots)
    _manifest(system, name)
    _policy(system, "selected", "system")
    assert svc._reload_single_plugin(name, "__manifest__")["loop_policies"]
    _manifest(user, name)
    _policy(user, "selected", "user")
    assert svc._reload_single_plugin(name, "__manifest__")["loop_policies"]
    expected = user if allow else system
    assert pm.get_plugin(name).path == expected
    assert registry.policies()["selected"].version() == ("user" if allow else "system")
    cold, _ = pm._scan_selected_plugins()
    assert cold[name].path == expected
    pm._save_discovery_cache()
    assert pm._try_load_from_discovery_cache()
    assert pm.get_plugin(name).path == expected
    shutil.rmtree(user)
    assert svc._reload_single_plugin(name, "__manifest__")["loop_policies"]
    assert pm.get_plugin(name).path == system
    assert registry.policies()["selected"].version() == "system"


def test_context_component_manifest_and_removal(plugin_reload_host, monkeypatch):
    pm, svc = plugin_reload_host
    name = "system-context"
    plugin = pm._SYSTEM_PLUGIN_DIR / name
    shutil.copytree(Path(__file__).resolve().parents[2] / "plugins" / name, plugin)
    registry = ContextPolicyRegistry()
    monkeypatch.setattr(ContextPolicyRegistry, "get_instance", lambda: registry)
    monkeypatch.setattr(runtime, "_plugin_roots", lambda: [pm._SYSTEM_PLUGIN_DIR])
    # 还原模块级持久 loader 和会话 fixture 已加载的内置模块。
    for attr in ("_context_tier_loader", "_budget_resolver_loader"):
        monkeypatch.setattr(runtime, attr, None)
    for key, module in sys.modules.copy().items():
        if key.startswith(("drifox_rt_context_tiers_system-context_", "drifox_rt_budget_resolvers_system-context_")):
            monkeypatch.setitem(sys.modules, key, module)
    for change in ("__manifest__", "context_tiers", "budget_resolvers", "__manifest__"):
        result = svc._reload_single_plugin(name, change)
        assert result["context_tiers" if change == "__manifest__" else change] is True
        assert {tier.order for tier in registry.tiers().values()} == {12, 15, 18, 20}
        assert [tier.order for tier in registry.resolve_chain("send")] == [12, 18, 20]
        assert registry.get_budget_resolver().id == "builtin"
    shutil.rmtree(plugin / "budget_resolvers")
    svc._reload_single_plugin(name, "__manifest__")
    assert len(registry.tiers()) == 4
    assert registry.get_budget_resolver() is None


def test_ui_reload_failure_reaches_host(plugin_reload_host, monkeypatch):
    pm, svc = plugin_reload_host
    name = "reload-example"
    plugin = pm._SYSTEM_PLUGIN_DIR / name
    _manifest(plugin, name)
    (plugin / "ui").mkdir()
    write_text_keep_eol(plugin / "ui" / "__init__.py", 'raise RuntimeError("broken ui")\n')
    ui = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "get_instance", lambda: ui)
    assert svc._reload_single_plugin(name, "ui")["ui"] is False
