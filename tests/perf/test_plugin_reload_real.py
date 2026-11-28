"""热重载基准，实际执行磁盘上的工具与运行时模块，计时从宿主收到组件事件开始。"""

import json
import statistics
from time import perf_counter

import pytest
from PyQt5.QtCore import QEventLoop, QTimer

from app.plugins.loaders import plugin_tool_loader as tools
from app.plugins.loaders import runtime_component_loader as runtime
from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
from app.tools.registry import ToolRegistry
from tools.eol_guard import write_text_keep_eol


@pytest.mark.perf
@pytest.mark.parametrize("change", ["tools", "__manifest__"], ids=["tool-file", "manifest"])
def test_plugin_reload_real(change, plugin_reload_host, monkeypatch, record_property):
    pm, svc = plugin_reload_host
    name = "reload-benchmark"
    directory = pm._SYSTEM_PLUGIN_DIR / name
    manifest = directory / ".drifox-plugin" / "plugin.json"
    tool = directory / "tools" / "example.py"
    policy = directory / "loop_policies" / "example.py"
    for path in (manifest, tool, policy):
        path.parent.mkdir(parents=True)
    tool_source = (
        "def register(registry):\n"
        "    registry.register('benchmark_tool', {'description': 'bench', 'parameters': "
        "{'type': 'object', 'properties': {}}}, impl=lambda: VERSION, danger='safe')\n"
    )
    write_text_keep_eol(tool, tool_source + "VERSION = 0\n")
    write_text_keep_eol(manifest, json.dumps({"name": name, "version": "1.0.0"}))
    write_text_keep_eol(
        policy, "class Policy:\n    id = 'bench'\ndef register(registry):\n    registry.register(Policy())\n"
    )
    registry = ToolRegistry()
    watcher = tools.PluginToolWatcher(registry, roots=[pm._SYSTEM_PLUGIN_DIR])
    monkeypatch.setattr(tools, "ensure_plugin_tool_watcher", lambda: watcher)
    loader = runtime.RuntimeComponentLoader("loop_policies", LoopPolicyRegistry())
    monkeypatch.setattr(runtime, "_plugin_roots", lambda: [pm._SYSTEM_PLUGIN_DIR])
    runtime_watcher = runtime._RuntimeWatcher(loader, "loop_policies")
    monkeypatch.setattr(runtime, "ensure_loop_policy_watcher", lambda: runtime_watcher)
    timings, stalls, counts = [], [], []
    modules = 0
    load_tool = tools._load_module
    load_runtime = runtime.RuntimeComponentLoader._load_module

    def count_tool(*args, **kwargs):
        nonlocal modules
        module = load_tool(*args, **kwargs)
        modules += 1
        return module

    def count_runtime(*args, **kwargs):
        nonlocal modules
        ok = load_runtime(*args, **kwargs)
        modules += int(ok)
        return ok

    try:
        warm = svc._do_single_reload(name, "__manifest__")
        assert warm["tools"] and warm["loop_policies"] and registry.get("benchmark_tool").impl() == 0
        monkeypatch.setattr(tools, "_load_module", count_tool)
        monkeypatch.setattr(runtime.RuntimeComponentLoader, "_load_module", count_runtime)
        for iteration in range(1, 26):
            if change == "tools":
                write_text_keep_eol(tool, tool_source + f"VERSION = {iteration}\n")
            else:
                write_text_keep_eol(manifest, json.dumps({"name": name, "version": f"1.0.{iteration}"}))
            modules = 0
            loop = QEventLoop()
            QTimer.singleShot(0, loop.quit)
            start = perf_counter()
            result = svc._do_single_reload(name, change)
            timings.append((perf_counter() - start) * 1000)
            loop.exec_()
            stalls.append((perf_counter() - start) * 1000)
            counts.append(modules)
            assert result["tools"]
            if change == "tools":
                assert registry.get("benchmark_tool").impl() == iteration
            else:
                assert result["loop_policies"] and pm.get_plugin(name).version == f"1.0.{iteration}"
    finally:
        watcher.unload_plugin(name)
        loader.unload_plugin(name)
    metrics = {
        "p50_ms": statistics.median(timings),
        "p95_ms": statistics.quantiles(timings, n=100, method="inclusive")[94],
        "qt_max_delay_ms": max(stalls),
        "module_loads_min": min(counts),
        "module_loads_max": max(counts),
    }
    assert set(counts) == {1 if change == "tools" else 2}
    for key, value in metrics.items():
        record_property(key, value)
    print(f"{change} " + " ".join(f"{key}={value:.2f}" for key, value in metrics.items()))
