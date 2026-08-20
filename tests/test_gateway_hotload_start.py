# -*- coding: utf-8 -*-
"""
回归测试：Gateway 平台热注册后启用无需重启 manager。

历史背景（2026-08-20）：
- bug：PlatformManager._adapters 只在 __init__ 的 _load_adapters 加载一次；
  随后（运行时安装/热加载）注册到 GatewayPlatformRegistry 的新平台不会进入
  _adapters。用户于系统配置卡打开该平台 enabled 开关时，
  _apply_gateway_toggle → start_platform_async → _start_platform_async 在
  _adapters 缺失该平台时静默 return False —— 开启没反应，必须重启应用
  （manager 重建、_load_adapters 此时已含该平台）才生效。
- 全量重载分支（reload_plugin_subsystems）也未分派 gateways reloader，
  无法补加载 adapter。
- 修复：manager._ensure_adapter 在 adapter 缺失时按 registry def 动态加载，
  使「启用即加载即启动」对任意热注册时序都成立，彻底消除「重启才生效」。
"""

import pytest

from app.gateway.base import BasePlatformAdapter, PlatformConfig, SendResult
from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry
import app.gateway.manager as mgr_mod


@pytest.fixture
def isolated_singletons(monkeypatch):
    """重置 manager 与 registry 的全局单例，避免测试间相互污染。"""
    monkeypatch.setattr(mgr_mod, "_manager_instance", None)
    import app.plugins.registries.gateway_platform_registry as reg_mod

    monkeypatch.setattr(reg_mod, "_instance", None)
    yield
    monkeypatch.setattr(mgr_mod, "_manager_instance", None)
    monkeypatch.setattr(reg_mod, "_instance", None)


class _FakeAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config)
        self.started = False

    async def start(self):
        self._connected = True
        self.started = True
        return True

    async def stop(self):
        self._connected = False

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, **kwargs):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str):
        from app.gateway.base import ChatInfo

        return ChatInfo()


def _make_def(platform_id: str = "testgw", source: str = "plugin:testgw"):
    def factory(cfg):
        return _FakeAdapter(cfg)

    def cfg_builder():
        return PlatformConfig(enabled=True, platform=platform_id)

    return (
        GatewayPlatformDef(
            platform_id=platform_id,
            display_name="TestGW",
            adapter_factory=factory,
            check_requirements=lambda: True,
            config_builder=cfg_builder,
            ui_order=10,
        ),
        source,
    )


def test_hotload_platform_starts_without_restart(isolated_singletons):
    """修复前：manager 创建后（_adapters 空）再注册新平台，启用静默失败。

    修复后：start_platform 动态加载 adapter 并成功启动，无需重启 manager。
    """
    reg = GatewayPlatformRegistry.get_instance()

    # manager 在 registry 为空时创建（模拟启动期早于插件扫描注册）
    mgr = mgr_mod.create_platform_manager(lambda *a, **k: "ok", lambda *a, **k: "ok")
    assert mgr.adapters == {}

    # 随后插件安装 → registry 注册新平台
    gw_def, source = _make_def()
    reg.register(gw_def, source=source)
    assert reg.get("testgw") is not None

    # 用户启用该平台（对应 _apply_gateway_toggle → start_platform_async）
    result = mgr.start_platform("testgw")

    assert result is True
    assert "testgw" in mgr.adapters
    assert mgr.adapters["testgw"].is_connected is True


def test_hotload_via_start_all_async_also_loads(isolated_singletons):
    """全量启动路径（start_all_async）同样应动态补加载热注册平台。"""
    reg = GatewayPlatformRegistry.get_instance()
    mgr = mgr_mod.create_platform_manager(lambda *a, **k: "ok", lambda *a, **k: "ok")
    assert mgr.adapters == {}

    gw_def, source = _make_def()
    reg.register(gw_def, source=source)

    # start_all_async 为 fire-and-forget 调度到后台事件循环；用 _run_coro 同步等待完成
    mgr._run_coro(mgr._start_all_async())

    assert "testgw" in mgr.adapters
    assert mgr.adapters["testgw"].is_connected is True
