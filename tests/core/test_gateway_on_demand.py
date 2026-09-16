# -*- coding: utf-8 -*-
"""GatewayService 按需构造契约测试（PERF T34）

背景（T19 定案）：GatewayService.ensure_started 原用
`QTimer.singleShot(4000, self._ensure_engine)` 无条件预热引擎，代价：
- 不用网关的用户白付 4s 时间点的主线程 ~370ms 尖峰 + 20-50MB 常驻
- `_ensure_components` 覆盖全局单例 `AgentManager._builtin_tools`，污染窗口
  fallback 并造成关闭泄漏

T34 改为按需：
- 4s 检查点走 `_maybe_prebuild_engine`：无启用平台直接跳过
- 有启用平台则后台 daemon 线程预热 import 链 + 组件（双检锁保护竞争），
  完成后 QTimer.singleShot(0) 回主线程建引擎
- 删除 `_builtin_tools` 覆盖行

本测试锁定上述契约。

运行: python -m pytest tests/core/test_gateway_on_demand.py -v
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.gateway_service import GatewayService  # noqa: E402


@pytest.fixture()
def fresh_service():
    """独立 GatewayService 实例（绕过单例，避免测试间互踩）"""
    saved = GatewayService._instance
    GatewayService._instance = None
    try:
        svc = GatewayService.__new__(GatewayService)
        from PyQt5.QtCore import QObject

        QObject.__init__(svc)
        svc._manager = None
        svc._engine = None
        svc._tool_executor = None
        svc._agent_manager = None
        svc._initialized = False
        svc._last_selfheal_ts = 0.0
        import threading

        svc._components_lock = threading.Lock()
        yield svc
    finally:
        GatewayService._instance = saved


class TestNoEnabledPlatform:
    """①无启用平台 → 4s 检查点后 _engine 仍 None（不预热）"""

    def test_no_enabled_platform_skips_prebuild(self, fresh_service, monkeypatch):
        monkeypatch.setattr(fresh_service, "_has_enabled_platform", lambda: False)
        called = {"prebuild": 0}
        monkeypatch.setattr(
            fresh_service,
            "_prebuild_components_background",
            lambda: called.__setitem__("prebuild", called["prebuild"] + 1),
        )

        fresh_service._maybe_prebuild_engine()

        assert fresh_service._engine is None, "无启用平台不得建引擎"
        assert called["prebuild"] == 0, "无启用平台不得触发后台预热"

    def test_has_enabled_platform_false_on_registry_error(self, fresh_service, monkeypatch):
        """registry 异常 → 返回 False（保守跳过）+ 不抛"""
        import app.plugins.registries.gateway_platform_registry as gpr

        def _boom():
            raise RuntimeError("registry 未就绪")

        monkeypatch.setattr(gpr.GatewayPlatformRegistry, "get_instance", staticmethod(_boom))
        assert fresh_service._has_enabled_platform() is False


class TestEnabledPlatform:
    """②启用平台 → 预热后 _engine 就绪"""

    def test_enabled_platform_triggers_prebuild(self, fresh_service, monkeypatch):
        monkeypatch.setattr(fresh_service, "_has_enabled_platform", lambda: True)
        called = {"prebuild": 0}
        monkeypatch.setattr(
            fresh_service,
            "_prebuild_components_background",
            lambda: called.__setitem__("prebuild", called["prebuild"] + 1),
        )

        fresh_service._maybe_prebuild_engine()
        assert called["prebuild"] == 1, "有启用平台应触发后台预热"

    def test_prebuild_then_engine_ready(self, fresh_service, monkeypatch):
        """后台预热完成后（模拟）引擎就绪；_maybe_prebuild_engine 幂等不重复触发"""
        monkeypatch.setattr(fresh_service, "_has_enabled_platform", lambda: True)
        calls = {"n": 0}

        def _fake_prebuild():
            calls["n"] += 1
            # 模拟后台线程完成 + 回主线程建好引擎
            fresh_service._engine = MagicMock(is_active=True)

        monkeypatch.setattr(fresh_service, "_prebuild_components_background", _fake_prebuild)

        fresh_service._maybe_prebuild_engine()
        assert fresh_service._engine is not None
        # 引擎已活 → 再调用应直接返回（不再预热）
        fresh_service._maybe_prebuild_engine()
        assert calls["n"] == 1, "引擎已活时不得重复预热"

    def test_prebuild_uses_daemon_thread(self, fresh_service, monkeypatch):
        """_prebuild_components_background 必须用 daemon 线程（不阻塞退出）"""
        captured = {}

        class _FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                captured["name"] = name
                captured["daemon"] = daemon
                captured["target"] = target

            def start(self):
                captured["started"] = True

        import app.core.gateway_service as gs_mod

        monkeypatch.setattr(gs_mod.threading, "Thread", _FakeThread)
        fresh_service._prebuild_components_background()

        assert captured.get("started") is True, "应启动线程"
        assert captured.get("daemon") is True, "预热线程必须是 daemon"


class TestSelfHealPath:
    """③引擎未建时首条消息自愈建引擎正常"""

    def test_ensure_engine_builds_when_missing(self, fresh_service, monkeypatch):
        """自愈路径：_ensure_engine 在无组件时能构造（不依赖 4s 预热）"""
        fake_executor = MagicMock()
        fake_executor._builtin_tools = MagicMock()
        fake_am = MagicMock()
        fake_engine = MagicMock(is_active=True)

        monkeypatch.setattr(fresh_service, "_ensure_components", lambda: True)
        fresh_service._tool_executor = fake_executor
        fresh_service._agent_manager = fake_am

        import app.core.engines.gateway as gw_mod

        monkeypatch.setattr(
            gw_mod.GatewayEngine,
            "get_instance",
            staticmethod(lambda **kw: fake_engine),
        )
        monkeypatch.setattr(fresh_service, "_get_session_store", staticmethod(lambda: MagicMock()))

        assert fresh_service._ensure_engine() is True
        assert fresh_service._engine is fake_engine, "消息自愈路径应建好引擎"

    def test_ensure_engine_idempotent_when_active(self, fresh_service):
        fresh_service._engine = MagicMock(is_active=True)
        assert fresh_service._ensure_engine() is True


class TestBuiltinToolsNotOverwritten:
    """④AgentManager._builtin_tools 不被 gateway ensure 覆盖"""

    def test_build_components_does_not_overwrite_builtin_tools(self, fresh_service, monkeypatch):
        """T34 核心：删掉覆盖行后，全局 AgentManager 的 _builtin_tools 不被改写"""
        import app.core.agent as agent_mod

        sentinel = object()  # 模拟「窗口已设置的」_builtin_tools
        fake_am = MagicMock()
        fake_am._builtin_tools = sentinel

        monkeypatch.setattr(
            agent_mod.AgentManager,
            "get_instance",
            classmethod(lambda cls, *a, **k: fake_am),
        )
        fake_executor = MagicMock()
        fake_executor._builtin_tools = MagicMock(name="gateway_bt")

        import app.core.tool_executor as te_mod

        monkeypatch.setattr(te_mod, "ToolExecutor", lambda backend=None: fake_executor)

        assert fresh_service._ensure_components() is True
        assert fake_am._builtin_tools is sentinel, (
            "gateway ensure 不得覆盖全局 AgentManager._builtin_tools（T19 实证无功能依赖，"
            "覆盖只造成窗口 fallback 污染 + 关闭泄漏）"
        )

    def test_late_window_schema_still_works(self, fresh_service, monkeypatch):
        """后建窗口的 schema 生成不受影响（显式传表路径仍可用）"""
        from app.core.agent import AgentManager

        am = AgentManager.get_instance(None, None)
        # 显式传入 builtin_tools（GatewayEngine engine.py:768/773 的做法）
        bt = MagicMock()
        schemas = am.get_agent_tools_schema("build", builtin_tools=bt)
        assert isinstance(schemas, list)


class TestComponentsLock:
    """附加：_ensure_components 双检锁在并发下只构造一次"""

    def test_concurrent_ensure_components_single_build(self, fresh_service, monkeypatch):
        import threading as _th

        builds = {"n": 0}

        def _fake_build():
            builds["n"] += 1
            import time as _t

            _t.sleep(0.05)  # 放大竞争窗口
            fresh_service._tool_executor = MagicMock()
            fresh_service._agent_manager = MagicMock()
            return True

        monkeypatch.setattr(fresh_service, "_build_components_locked", _fake_build)

        results = []
        threads = [_th.Thread(target=lambda: results.append(fresh_service._ensure_components())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert all(results), "全部调用应返回 True"
        assert builds["n"] == 1, f"并发下应只构造一次，实际 {builds['n']} 次"

    def test_fast_path_no_lock_when_ready(self, fresh_service, monkeypatch):
        """已就绪时走无锁快路径（不调用 _build_components_locked）"""
        fresh_service._tool_executor = MagicMock()
        fresh_service._agent_manager = MagicMock()
        called = {"n": 0}
        monkeypatch.setattr(
            fresh_service, "_build_components_locked", lambda: called.__setitem__("n", called["n"] + 1) or True
        )
        assert fresh_service._ensure_components() is True
        assert called["n"] == 0, "已就绪应走快路径，不进构造段"
