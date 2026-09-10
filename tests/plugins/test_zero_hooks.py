# -*- coding: utf-8 -*-
"""zero hook 组件（经 core_services 拿 hook_manager，成对可逆注册）机制测试。

用假 hook_manager 验证 zero 组件的注册姿势：
apply 注册 → ctx 托管 → dispose 自动反注册（skill 子域隔离）。
不 import app.*（hook_manager 真身需实机验证）。
"""

import sys
from pathlib import Path

import pytest

from zero import Context

BRIDGE_UI = Path(__file__).resolve().parents[2] / "plugins" / "zero-bridge" / "ui"
sys.path.insert(0, str(BRIDGE_UI))


class _FakeHookManager:
    """HookManager 成对 API 的最小替身（签名对齐真实实现）。"""

    def __init__(self) -> None:
        self.registered: list = []
        self.unregistered: list = []

    def register_hooks_from_json(self, skill_name, skill_root, hooks_config, config_file=None, is_system_plugin=False):
        self.registered.append((skill_name, skill_root, hooks_config))
        return 1

    def unregister_skill_hooks(self, skill_name: str):
        self.unregistered.append(skill_name)


@pytest.fixture
def pool():
    ctx = Context("root")
    hm = _FakeHookManager()
    ctx.set("hook_manager", hm)
    yield ctx, hm
    ctx.dispose()


def _apply_hooks_component(ctx):
    """执行 zero-bridge/zero/hooks_demo.py 的 apply（直接 import 组件源码）。"""
    import types

    source = (Path(__file__).resolve().parents[2] / "plugins" / "zero-bridge" / "zero" / "hooks_demo.py").read_text(
        encoding="utf-8"
    )
    module = types.ModuleType("hooks_demo")
    exec(compile(source, "hooks_demo.py", "exec"), module.__dict__)
    module.apply.__name__ = module.NAME  # 对齐 loader 行为：fork 名用组件 NAME
    fork = ctx.use(module.apply)
    return fork


def test_hook_component_registers_into_hook_manager(pool):
    ctx, hm = pool
    _apply_hooks_component(ctx)

    assert len(hm.registered) == 1
    skill, _root, rules = hm.registered[0]
    assert skill == "zero-bridge.zero"  # 子域，不与 hooks.json 冲突
    assert "Stop" in rules["hooks"]


def test_dispose_unregisters_skill(pool):
    ctx, hm = pool
    fork = _apply_hooks_component(ctx)

    fork.dispose()
    assert hm.unregistered == ["zero-bridge.zero"]  # 自动反注册


def test_reload_rolls_back_then_re_registers(pool):
    ctx, hm = pool
    fork = _apply_hooks_component(ctx)

    # 模拟热重载：dispose（回滚）→ 重新 use（重装）
    fork.dispose()
    _apply_hooks_component(ctx)

    assert hm.unregistered == ["zero-bridge.zero"]
    assert len(hm.registered) == 2  # 两次注册


def test_hook_component_survives_until_ctx_dispose(pool):
    ctx, hm = pool
    _apply_hooks_component(ctx)
    ctx.dispose()
    assert hm.unregistered == ["zero-bridge.zero"]  # 根销毁级联反注册
