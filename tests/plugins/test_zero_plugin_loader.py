# -*- coding: utf-8 -*-
"""ZeroPluginLoader（DriFox 组件模式）测试。

zero 是 DriFox 的组件类型：插件目录 zero/ 下的 *.py 文件即插件。
覆盖：装载、拓扑顺序、重载（回滚重装 + 模块 purge + pyc 绕开）、卸载、
坏组件隔离、handle_reload 卸载分支、跨插件 key 前缀。
"""

import sys
from pathlib import Path

import pytest

from zero import create_context

PLUGIN_LOADER_DIR = Path(__file__).resolve().parents[2] / "plugins" / "zero-bridge" / "ui"
sys.path.insert(0, str(PLUGIN_LOADER_DIR))

from plugin_loader import ZeroPluginLoader  # noqa: E402


COMP_V1 = """
NAME = "counter"
def apply(ctx):
    ctx.set("counter.version", 1)
"""

COMP_V2 = """
NAME = "counter"
def apply(ctx):
    ctx.set("counter.version", 2)
"""

PROVIDER = """
NAME = "provider"
PROVIDES = ("svc",)
def apply(ctx):
    ctx.provide("svc", "up")
"""

CONSUMER = """
from zero import inject

NAME = "consumer"
@inject(required=["svc"])
def apply(ctx):
    ctx.set("consumer.got", ctx.require("svc"))
"""

BROKEN = """
NAME = "broken"
raise RuntimeError("boom at import")
"""

NO_APPLY = """
NAME = "noapply"
X = 1
"""


class _PluginInfo:
    """ReloadContext.plugin 的最小替身。"""

    def __init__(self, path: Path) -> None:
        self.path = path


class _ReloadCtx:
    """kernel.ReloadContext 的最小替身。"""

    def __init__(self, plugin_name: str, plugin):
        self.plugin_name = plugin_name
        self.plugin = plugin


@pytest.fixture
def ctx():
    c = create_context()
    yield c
    c.dispose()


def _write_component(zero_dir: Path, filename: str, content: str) -> None:
    zero_dir.mkdir(parents=True, exist_ok=True)
    (zero_dir / filename).write_text(content, encoding="utf-8")


def test_load_plugin_loads_zero_components(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "counter.py", COMP_V1)

    loader = ZeroPluginLoader(ctx)
    keys = loader.load_plugin("my-plugin", plugin_dir)

    assert keys == ["my-plugin.counter"]
    fork = ctx.registry.forks_of("my-plugin.counter")[0]
    assert fork.get("counter.version") == 1


def test_topological_order_across_components(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "z_consumer.py", CONSUMER)
    _write_component(plugin_dir / "zero", "a_provider.py", PROVIDER)

    loader = ZeroPluginLoader(ctx)
    loader.load_plugin("my-plugin", plugin_dir)

    consumer = ctx.registry.forks_of("my-plugin.consumer")[0]
    assert consumer.get("consumer.got") == "up"  # 提供者先加载


def test_reload_via_handle_reload_rolls_back_and_reinstalls(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    comp = plugin_dir / "zero" / "counter.py"
    _write_component(plugin_dir / "zero", "counter.py", COMP_V1)

    loader = ZeroPluginLoader(ctx)
    loader.load_plugin("my-plugin", plugin_dir)
    old_fork = ctx.registry.forks_of("my-plugin.counter")[0]

    comp.write_text(COMP_V2, encoding="utf-8")  # 模拟文件编辑
    ok = loader.handle_reload(_ReloadCtx("my-plugin", _PluginInfo(plugin_dir)))

    assert ok is True
    new_fork = ctx.registry.forks_of("my-plugin.counter")[0]
    assert new_fork is not old_fork  # 旧 fork 已回滚
    assert new_fork.get("counter.version") == 2  # 新代码生效（pyc 绕开验证）
    assert len(ctx.registry.forks_of("my-plugin.counter")) == 1


def test_unload_via_handle_reload_when_plugin_deleted(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "counter.py", COMP_V1)

    loader = ZeroPluginLoader(ctx)
    loader.load_plugin("my-plugin", plugin_dir)

    assert loader.handle_reload(_ReloadCtx("my-plugin", None)) is True  # plugin=None → 卸载
    assert ctx.registry.get("my-plugin.counter") is None
    assert "zero_plugin_counter" not in sys.modules


def test_broken_component_does_not_block_siblings(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "broken.py", BROKEN)
    _write_component(plugin_dir / "zero", "counter.py", COMP_V1)
    _write_component(plugin_dir / "zero", "_helper.py", "X = 1")  # _ 前缀忽略

    loader = ZeroPluginLoader(ctx)
    keys = loader.load_plugin("my-plugin", plugin_dir)

    assert keys == ["my-plugin.counter"]  # 坏组件被跳过
    assert ctx.registry.forks_of("my-plugin.counter")


def test_component_without_apply_is_skipped(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "noapply.py", NO_APPLY)

    loader = ZeroPluginLoader(ctx)
    assert loader.load_plugin("my-plugin", plugin_dir) == []


def test_missing_dependency_leaves_component_unloaded(ctx, tmp_path: Path):
    plugin_dir = tmp_path / "my-plugin"
    _write_component(plugin_dir / "zero", "lonely.py", CONSUMER)  # svc 无人提供

    loader = ZeroPluginLoader(ctx)
    assert loader.load_plugin("my-plugin", plugin_dir) == []
    assert ctx.registry.forks_of("my-plugin.lonely") == []


def test_keys_namespaced_by_plugin(ctx, tmp_path: Path):
    """两个插件的同名组件文件互不冲突（登记名带插件前缀）。"""
    for plugin in ("a", "b"):
        d = tmp_path / plugin
        _write_component(d / "zero", "counter.py", COMP_V1)
        ZeroPluginLoader(ctx).load_plugin(plugin, d)

    assert ctx.registry.get("a.counter") is not None
    assert ctx.registry.get("b.counter") is not None


def test_zero_dir_absent_is_noop(ctx, tmp_path: Path):
    loader = ZeroPluginLoader(ctx)
    assert loader.load_plugin("empty", tmp_path / "empty") == []
