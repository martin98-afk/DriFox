# -*- coding: utf-8 -*-
"""
Gateway 平台管理器

管理所有平台适配器。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    SendResult,
)
from app.gateway.adapters import (
    WeComAdapter,
    DingTalkAdapter,
    check_wecom_requirements,
    check_dingtalk_requirements,
)
from app.gateway.config import get_gateway_config
from app.gateway.message_handler import MessageHandler
from app.gateway.session_manager import GatewaySessionManager, GatewaySession

logger = logging.getLogger(__name__)


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
        self._adapters: Dict[Platform, BasePlatformAdapter] = {}
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
        """加载平台适配器"""
        # 企业微信
        if check_wecom_requirements():
            wecom_config = self._config.get_platform_config(Platform.WECOM)
            self._adapters[Platform.WECOM] = WeComAdapter(wecom_config)
            logger.info("[PlatformManager] WeCom adapter loaded")
        else:
            logger.info("[PlatformManager] WeCom adapter skipped (missing dependencies)")
        
        # 钉钉
        if check_dingtalk_requirements():
            dingtalk_config = self._config.get_platform_config(Platform.DINGTALK)
            self._adapters[Platform.DINGTALK] = DingTalkAdapter(dingtalk_config)
            logger.info("[PlatformManager] DingTalk adapter loaded")
        else:
            logger.info("[PlatformManager] DingTalk adapter skipped (missing dependencies)")
    
    def get_adapter(self, platform: Platform) -> Optional[BasePlatformAdapter]:
        """获取平台适配器"""
        return self._adapters.get(platform)
    
    @property
    def adapters(self) -> Dict[Platform, BasePlatformAdapter]:
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
        """
        后台启动所有启用的平台
        """
        if self._running:
            logger.warning("[PlatformManager] Already running")
            return {p: True for p in self._adapters}
        
        self._running = True
        results = {}
        
        # 设置消息处理器
        if self._message_handler:
            for adapter in self._adapters.values():
                adapter.set_message_handler(self._message_handler.handle)
        
        # 启动每个启用的平台
        for platform in [Platform.WECOM, Platform.DINGTALK]:
            adapter = self._adapters.get(platform)
            if not adapter:
                continue
            
            config = self._config.get_platform_config(platform)
            if not config.enabled:
                logger.info("[PlatformManager] %s not enabled, skipping", platform.value)
                continue
            
            # 检查配置
            if platform == Platform.WECOM:
                if not config.bot_id or not config.secret:
                    logger.error("[PlatformManager] WeCom bot_id and secret are required")
                    adapter._last_error = "bot_id and secret are required"
                    results[platform] = False
                    continue
                adapter._bot_id = config.bot_id
                adapter._secret = config.secret
            elif platform == Platform.DINGTALK:
                if not config.client_id or not config.client_secret:
                    logger.error("[PlatformManager] DingTalk client_id and client_secret are required")
                    adapter._last_error = "client_id and client_secret are required"
                    results[platform] = False
                    continue
                adapter._client_id = config.client_id
                adapter._client_secret = config.client_secret
            
            try:
                success = await adapter.start()
                results[platform] = success
                
                if success:
                    logger.info("[PlatformManager] %s started", platform.value)
                else:
                    logger.warning("[PlatformManager] %s failed to start", platform.value)
                    
            except Exception as e:
                logger.error("[PlatformManager] %s start error: %s", platform.value, e, exc_info=True)
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
                    logger.info("[PlatformManager] %s stopped", platform.value)
                except Exception as e:
                    logger.error("[PlatformManager] %s stop error: %s", platform.value, e, exc_info=True)
        
        self._notify_status()
    
    def start_platform(self, platform: Platform) -> bool:
        """启动指定平台（同步，等待结果）"""
        return self._run_coro(self._start_platform_async(platform))
    
    def start_platform_async(self, platform: Platform) -> None:
        """启动指定平台（异步，不等待结果）"""
        self._schedule_coro(self._start_platform_async(platform))
    
    def stop_platform(self, platform: Platform) -> None:
        """停止指定平台"""
        self._schedule_coro(self._stop_platform_async(platform))
    
    async def _start_platform_async(self, platform: Platform) -> bool:
        """在后台事件循环上启动平台"""
        adapter = self._adapters.get(platform)
        if not adapter:
            logger.error("[PlatformManager] No adapter for %s", platform.value)
            return False
        
        config = self._config.get_platform_config(platform)
        
        if platform == Platform.WECOM:
            if not config.bot_id or not config.secret:
                logger.error("[PlatformManager] WeCom bot_id and secret are required")
                adapter._last_error = "bot_id and secret are required"
                self._notify_status()
                return False
            adapter._bot_id = config.bot_id
            adapter._secret = config.secret
        elif platform == Platform.DINGTALK:
            if not config.client_id or not config.client_secret:
                logger.error("[PlatformManager] DingTalk client_id and client_secret are required")
                adapter._last_error = "client_id and client_secret are required"
                self._notify_status()
                return False
            adapter._client_id = config.client_id
            adapter._client_secret = config.client_secret
        
        if self._message_handler:
            adapter.set_message_handler(self._message_handler.handle)
        
        success = await adapter.start()
        self._notify_status()
        return success
    
    async def _stop_platform_async(self, platform: Platform) -> None:
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
        
        for platform in [Platform.WECOM, Platform.DINGTALK]:
            adapter = self._adapters.get(platform)
            config = self._config.get_platform_config(platform)
            
            platforms[platform.value] = {
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
                logger.warning("[PlatformManager] Status callback error: %s", e)


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