# -*- coding: utf-8 -*-
"""回归测试：gateway 开关泄漏（一条消息回复 N 遍）。

历史 bug：_stop_platform_async / _stop_all_async 带 is_connected 前置检查，
泄漏实例（_running=True 但 _connected=False，如长轮询退避重试中）被跳过
stop → poll task 残留 → 重复开关后同平台多实例长轮询同一账号
→ 一条消息被处理/回复多遍。

修复点（对齐 stop_plugin_platforms 的既有修复）：
1. _stop_platform_async / _stop_all_async 无条件 stop（base.stop 幂等）
2. _ensure_adapter 重建路径 pop 旧实例前调度 stop，防 poll task 泄漏
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.gateway.manager import PlatformManager


def _make_manager() -> PlatformManager:
    """轻量构造：不起后台 loop 线程、不加载 registry"""
    mgr = PlatformManager.__new__(PlatformManager)
    mgr._config = MagicMock()
    mgr._adapters = {}
    mgr._running = False
    mgr._loop = None
    mgr._loop_thread = None
    mgr._session_manager = MagicMock()
    mgr._message_handler = None
    mgr._status_callbacks = []
    return mgr


def _make_leaked_adapter(platform: str):
    """泄漏实例特征：poll loop 还活着（_running=True）但 _connected=False"""
    a = MagicMock()
    a.platform = platform
    a.is_connected = False
    a._running = True
    a._last_error = "poll failed x3"  # 促使 _ensure_adapter 走重建分支
    a.stop = AsyncMock()
    return a


class TestStopLeakedAdapter:
    """场景：关开关时实例处于「断连但 poll loop 存活」→ 必须真 stop"""

    def test_stop_platform_stops_disconnected_running_adapter(self):
        mgr = _make_manager()
        leak = _make_leaked_adapter("wechat")
        mgr._adapters["wechat"] = leak

        asyncio.run(mgr._stop_platform_async("wechat"))

        leak.stop.assert_awaited_once(), "泄漏实例（_running=True/_connected=False）被跳过 stop → poll task 残留"

    def test_stop_all_stops_disconnected_running_adapter(self):
        mgr = _make_manager()
        mgr._running = True
        leak = _make_leaked_adapter("wechat")
        mgr._adapters["wechat"] = leak

        asyncio.run(mgr._stop_all_async())

        leak.stop.assert_awaited_once(), "stop_all 的 is_connected 前置检查跳过泄漏实例"

    def test_stop_platform_noop_without_adapter(self):
        mgr = _make_manager()
        asyncio.run(mgr._stop_platform_async("nonexistent"))  # 不应抛异常


class TestEnsureAdapterRebuildStopsOld:
    """场景：再开开关时配置已补齐走重建 → 旧实例必须被 stop，不能裸 pop"""

    def _patch_registry(self):
        return patch(
            "app.plugins.registries.gateway_platform_registry.GatewayPlatformRegistry"
        )

    def test_rebuild_schedules_stop_of_old_instance(self):
        mgr = _make_manager()
        old = _make_leaked_adapter("wechat")
        mgr._adapters["wechat"] = old

        scheduled = []
        mgr._schedule_coro = lambda coro: scheduled.append(coro)

        reg_def = MagicMock()
        reg_def.validate_config = MagicMock(return_value=(True, None))
        reg_def.config_builder = None
        reg_def.adapter_factory = MagicMock(return_value=MagicMock())
        reg_def.check_requirements = MagicMock(return_value=True)

        reg = MagicMock()
        reg.get = MagicMock(return_value=reg_def)
        reg.list_platforms = MagicMock(return_value=[reg_def])

        with self._patch_registry() as cls:
            cls.get_instance.return_value = reg
            result = mgr._ensure_adapter("wechat")

        assert result is not old, "应丢弃旧实例走重建"
        assert mgr._adapters["wechat"] is not old
        assert len(scheduled) == 1, "重建前必须调度 stop 旧实例（防 poll task 泄漏双收）"
        for coro in scheduled:
            coro.close()  # 消防未消费协程 warning
