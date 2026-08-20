# -*- coding: utf-8 -*-
"""
Gateway 平台管理器

管理所有平台适配器。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    SendResult,
)
from app.gateway.config import get_gateway_config
from app.gateway.message_handler import MessageHandler
from app.gateway.session_manager import GatewaySession, GatewaySessionManager


class PlatformManager:
    """
    平台管理器

    负责：
    1. 加载和创建平台适配器
    2. 启动/停止平台连接
    3. 统一的消息路由
    """

    def __init__(
        self,
        config: "GatewayConfigHelper",
        process_message_callback: Optional[Callable] = None,
        send_message_callback: Optional[Callable] = None,
    ):
        """
        初始化平台管理器

        Args:
            config: Gateway 配置
            process_message_callback: 处理消息回调
            send_message_callback: 发送消息回调
        """
        self._config = config
        self._adapters: Dict[str, BasePlatformAdapter] = {}
        self._running = False

        # 持久事件循环（后台线程运行）
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # 会话管理器
        self._session_manager = GatewaySessionManager()

        # 消息处理器
        self._message_handler: Optional[MessageHandler] = None
        if process_message_callback and send_message_callback:
            self._message_handler = MessageHandler(
                session_manager=self._session_manager,
                process_message_callback=process_message_callback,
                send_message_callback=send_message_callback,
            )

        # 状态回调
        self._status_callbacks: List[Callable] = []

        # 加载适配器
        self._load_adapters()

    def _run_loop(self) -> None:
        """持久事件循环"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro) -> Any:
        """在后台事件循环上执行协程，返回结果"""
        import concurrent.futures

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            logger.error("[PlatformManager] Coroutine timeout")
            return False

    def _schedule_coro(self, coro) -> None:
        """在后台事件循环上调度协程，不等待结果"""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _load_adapters(self) -> None:
        """加载平台适配器（Phase E / E2 Task 5：纯 registry 分派，无内置回退段）"""
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        for d in GatewayPlatformRegistry.get_instance().list_platforms():
            try:
                if not d.check_requirements():
                    logger.info(f"[PlatformManager] {d.display_name} adapter skipped (missing dependencies)")
                    continue
                cfg = d.config_builder() if d.config_builder else None
                self._adapters[d.platform_id] = d.adapter_factory(cfg)
                logger.info(f"[PlatformManager] {d.display_name} adapter loaded (plugin)")
            except Exception as e:
                logger.warning(f"[PlatformManager] {d.display_name} 插件加载失败: {e}")

    def _ensure_adapter(self, platform: str) -> Optional[BasePlatformAdapter]:
        """确保某平台 adapter 已加载到 _adapters；缺失则按 registry def 动态加载。

        解决“热安装/热注册平台晚于 manager 单例创建”时 _adapters 不更新、
        start 静默失败（必须重启 manager 才生效）的根因：启用即加载即启动。
        adapter 已存在时直接返回（不重建，保留热更新重建的实例）。
        """
        existing = self._adapters.get(platform)
        if existing is not None:
            return existing
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        d = GatewayPlatformRegistry.get_instance().get(platform)
        if d is None:
            logger.warning(f"[PlatformManager] {platform} 未注册，无法动态加载 adapter")
            return None
        if not d.check_requirements():
            logger.warning(f"[PlatformManager] {platform} 依赖不满足，跳过加载")
            return None
        cfg = d.config_builder() if d.config_builder else None
        try:
            adapter = d.adapter_factory(cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[PlatformManager] {platform} adapter 动态加载失败: {e}")
            return None
        if self._message_handler is not None:
            adapter.set_message_handler(self._message_handler.handle)
        self._adapters[platform] = adapter
        logger.info(f"[PlatformManager] {platform} adapter 已动态加载（热注册自愈）")
        return adapter

    def get_adapter(self, platform: Platform) -> Optional[BasePlatformAdapter]:
        """获取平台适配器"""
        from app.gateway.base import _platform_key

        return self._adapters.get(_platform_key(platform))

    @property
    def adapters(self) -> Dict[str, BasePlatformAdapter]:
        """所有适配器"""
        return self._adapters.copy()

    @property
    def session_manager(self) -> GatewaySessionManager:
        """会话管理器"""
        return self._session_manager

    def set_process_callback(
        self,
        process_message: Callable,
        send_message: Callable[[Platform, str, str, Any], SendResult],
    ) -> None:
        """设置消息处理回调"""
        self._message_handler = MessageHandler(
            session_manager=self._session_manager,
            process_message_callback=process_message,
            send_message_callback=send_message,
        )

        # 设置所有适配器的消息处理器
        for adapter in self._adapters.values():
            adapter.set_message_handler(self._message_handler.handle)

    def start_all(self) -> Dict[Platform, bool]:
        """
        启动所有启用的平台（同步，等待结果）

        Returns:
            启动结果: platform -> success
        """
        return self._run_coro(self._start_all_async())

    def start_all_async(self) -> None:
        """
        启动所有启用的平台（纯异步，不等待结果）

        避免 WebSocket 连接慢时卡住调用线程。
        """
        self._schedule_coro(self._start_all_async())

    def stop_all(self) -> None:
        """停止所有平台"""
        self._schedule_coro(self._stop_all_async())

    async def _start_all_async(self) -> Dict[Platform, bool]:
        """后台启动所有启用的平台（registry 驱动，无平台 if-elif）"""
        if self._running:
            logger.warning("[PlatformManager] Already running")
            return {p: True for p in self._adapters}

        self._running = True
        results: Dict[Platform, bool] = {}

        # 设置消息处理器
        if self._message_handler:
            for adapter in self._adapters.values():
                adapter.set_message_handler(self._message_handler.handle)

        # 启动列表 = registry（不再硬编码 7 项）
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        all_platforms = [d.platform_id for d in GatewayPlatformRegistry.get_instance().list_platforms()]

        for platform in all_platforms:
            adapter = self._ensure_adapter(platform)
            if adapter is None:
                results[platform] = False
                continue

            config = self._config.get_platform_config(platform)
            if not config.enabled:
                logger.info(f"[PlatformManager] {platform} not enabled, skipping")
                continue

            # 配置校验：各 adapter 自带 validate_config（registry 分派，无平台分支）
            from app.plugins.registries.gateway_platform_registry import (
                GatewayPlatformRegistry as _Reg,
            )

            d = _Reg.get_instance().get(platform)
            if d is not None and d.validate_config is not None:
                ok, err = d.validate_config(config)
                if not ok:
                    logger.error(f"[PlatformManager] {platform} 配置校验失败: {err}")
                    adapter._last_error = err
                    results[platform] = False
                    continue

            try:
                success = await adapter.start()
                results[platform] = success

                if success:
                    logger.info(f"[PlatformManager] {platform} started")
                else:
                    logger.warning(f"[PlatformManager] {platform} failed to start")

            except Exception as e:
                logger.error(f"[PlatformManager] {platform} start error: {e}")
                results[platform] = False

        self._notify_status()
        return results

    async def _stop_all_async(self) -> None:
        """后台停止所有平台"""
        if not self._running:
            return

        self._running = False

        for platform, adapter in self._adapters.items():
            if adapter.is_connected:
                try:
                    await adapter.stop()
                    logger.info(f"[PlatformManager] {platform} stopped")
                except Exception as e:
                    logger.error(f"[PlatformManager] {platform} stop error: {e}", exc_info=True)

        self._notify_status()

    def start_platform(self, platform: Platform) -> bool:
        """启动指定平台（同步，等待结果）"""
        from app.gateway.base import _platform_key

        return self._run_coro(self._start_platform_async(_platform_key(platform)))

    def start_platform_async(self, platform: Platform) -> None:
        """启动指定平台（异步，不等待结果）"""
        from app.gateway.base import _platform_key

        self._schedule_coro(self._start_platform_async(_platform_key(platform)))

    def stop_platform(self, platform: Platform) -> None:
        """停止指定平台"""
        from app.gateway.base import _platform_key

        self._schedule_coro(self._stop_platform_async(_platform_key(platform)))

    def stop_plugin_platforms(self, plugin_name: str, wait: bool = False) -> None:
        """停止并移除某插件注册的全部 gateway 平台（卸载/热更新清理前置）

        1. 从 registry 查出 source=plugin:<name> 的 platform_id 列表（unregister 前查）
        2. 逐个停止连接（异步调度到后台事件循环，不阻塞调用线程）
        3. 立即从 _adapters 摘除实例引用，使其不再被消息路由命中

        Args:
            plugin_name: 插件名
            wait: True 时阻塞等待 stop 协程完成（调用方需要确保 SDK/依赖
                释放后再删文件时使用，如市场卸载/残留清理）；默认 False 保持
                热重载路径的非阻塞行为。
        """
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        pids = GatewayPlatformRegistry.get_instance().get_platform_ids_by_source(f"plugin:{plugin_name}")
        futures = []
        for pid in pids:
            adapter = self._adapters.get(pid)
            if adapter is not None:
                try:
                    if getattr(adapter, "is_connected", False):
                        # 直接调度 adapter.stop()（持实例引用），不经过
                        # _stop_platform_async 的 _adapters 二次查找 —— 后者会因
                        # 下方立即 pop 而查到 None，导致 stop 永不执行（连接泄漏，
                        # 且 adapter 依赖的 SDK/.pyd 句柄不释放 → 卸载残留 deps）。
                        futures.append(asyncio.run_coroutine_threadsafe(self._stop_adapter_async(adapter), self._loop))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[PlatformManager] stop {pid} 失败: {e}")
                self._adapters.pop(pid, None)
        if pids:
            logger.info(f"[PlatformManager] 已停止并摘除插件 [{plugin_name}] 的 {len(pids)} 个平台: {pids}")
        if wait:
            for fut in futures:
                try:
                    fut.result(timeout=5)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[PlatformManager] 等待平台 stop 超时/失败: {e}")

    async def _stop_adapter_async(self, adapter: BasePlatformAdapter) -> None:
        """停止单个 adapter 实例（引用已捕获，不查 _adapters，避免 pop 竞态）"""
        try:
            if adapter.is_connected:
                await adapter.stop()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[PlatformManager] adapter stop error: {e}")
        self._notify_status()

    def rebuild_plugin_platforms(self, plugin_name: str, restart_if_running: bool = True) -> None:
        """用 registry 当前 def 重建某插件的 adapter（热更新后用新 adapter_factory）

        热重载路径在 stop_plugin_platforms + scan_now(unregister 旧/注册新) 之后调用：
        从 registry 读取最新 def（含新 adapter_factory），重建 adapter 实例放入
        _adapters；若此前整个 gateway 在运行且平台启用，则重新 start 使新代码生效。
        """
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        reg = GatewayPlatformRegistry.get_instance()
        pids = reg.get_platform_ids_by_source(f"plugin:{plugin_name}")
        for pid in pids:
            d = reg.get(pid)
            if d is None or not d.check_requirements():
                logger.info(f"[PlatformManager] {pid} 重建跳过（def 缺失/依赖不满足）")
                continue
            cfg = d.config_builder() if d.config_builder else None
            try:
                self._adapters[pid] = d.adapter_factory(cfg)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[PlatformManager] {pid} adapter 重建失败: {e}")
                continue
            if self._message_handler:
                self._adapters[pid].set_message_handler(self._message_handler.handle)
            if restart_if_running and self._running:
                config = self._config.get_platform_config(pid)
                if config and getattr(config, "enabled", False):
                    self._schedule_coro(self._start_platform_async(pid))
        if pids:
            logger.info(f"[PlatformManager] 已重建插件 [{plugin_name}] 的 {len(pids)} 个平台: {pids}")

    async def _start_platform_async(self, platform: str) -> bool:
        """在后台事件循环上启动平台（registry 驱动，无平台 if-elif）"""
        adapter = self._ensure_adapter(platform)
        if adapter is None:
            logger.error(f"[PlatformManager] No adapter for {platform}")
            return False

        config = self._config.get_platform_config(platform)

        # 配置校验：registry def.validate_config（无则跳过）
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry as _Reg,
        )

        d = _Reg.get_instance().get(platform)
        if d is not None and d.validate_config is not None:
            ok, err = d.validate_config(config)
            if not ok:
                logger.error(f"[PlatformManager] {platform} 配置校验失败: {err}")
                adapter._last_error = err
                self._notify_status()
                return False

        if self._message_handler:
            adapter.set_message_handler(self._message_handler.handle)

        success = await adapter.start()
        self._notify_status()
        return success

    async def _stop_platform_async(self, platform: str) -> None:
        """在后台事件循环上停止平台"""
        adapter = self._adapters.get(platform)
        if adapter and adapter.is_connected:
            await adapter.stop()
        self._notify_status()

    def get_status(self) -> Dict[str, Any]:
        """
        获取状态

        Returns:
            状态信息
        """
        platforms = {}

        # 启动列表 = registry（不再硬编码 7 项）
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        all_platforms = [d.platform_id for d in GatewayPlatformRegistry.get_instance().list_platforms()]

        for platform in all_platforms:
            adapter = self._adapters.get(platform)
            config = self._config.get_platform_config(platform)

            platforms[platform] = {
                "enabled": config.enabled,
                "connected": adapter.is_connected if adapter else False,
                "error": adapter.last_error if adapter else None,
                "available": platform in self._adapters,
            }

        return {
            "running": self._running,
            "platforms": platforms,
            "session_count": self._session_manager.session_count,
        }

    def get_sessions(self, platform: Optional[Platform] = None) -> List[GatewaySession]:
        """获取会话列表"""
        return self._session_manager.list_sessions(platform)

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        return self._session_manager.delete_session(session_id)

    def on_status_change(self, callback: Callable[[Dict], None]) -> None:
        """注册状态变化回调"""
        self._status_callbacks.append(callback)

    def _notify_status(self) -> None:
        """通知状态变化"""
        status = self.get_status()
        for callback in self._status_callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.warning(f"[PlatformManager] Status callback error: {e}")


# 全局实例
_manager_instance: Optional[PlatformManager] = None


def create_platform_manager(
    process_message: Callable,
    send_message: Callable[[Platform, str, str, Any], SendResult],
) -> PlatformManager:
    """
    创建或获取平台管理器（全局单例）

    Args:
        process_message: 处理消息回调
        send_message: 发送消息回调

    Returns:
        PlatformManager
    """
    global _manager_instance

    if _manager_instance is not None:
        return _manager_instance

    # 使用全局单例，确保 UI 保存的配置能被读取
    config = get_gateway_config()
    _manager_instance = PlatformManager(
        config=config,
        process_message_callback=process_message,
        send_message_callback=send_message,
    )

    logger.info("[PlatformManager] Created singleton instance")

    return _manager_instance


def get_platform_manager() -> Optional[PlatformManager]:
    """获取全局平台管理器"""
    return _manager_instance
