# -*- coding: utf-8 -*-
"""
Gateway 消息处理器

处理来自企业微信/钉钉的消息。
将所有消息（包括命令）委托给 process_message 回调。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    MessageEvent,
    Platform,
    SendResult,
)
from app.gateway.session_manager import GatewaySession, GatewaySessionManager



class MessageHandler:
    """
    Gateway 消息处理器

    负责：
    1. 接收平台消息
    2. 获取/创建 Gateway 会话
    3. 委托给 process_message 回调处理（含命令和 AI 对话）
    4. 发送响应
    """

    def __init__(
        self,
        session_manager: GatewaySessionManager,
        process_message_callback: Callable,
        send_message_callback: Callable[[Platform, str, str, Any], SendResult],
    ):
        """
        初始化消息处理器

        Args:
            session_manager: Gateway 会话管理器（管理 WeCom/钉钉用户映射）
            process_message_callback: 处理消息的回调（含命令），签名为：
                async def process(session_id: str, text: str, ...) -> str
            send_message_callback: 发送消息的回调
        """
        self._session_manager = session_manager
        self._process_message = process_message_callback
        self._send_message = send_message_callback

    async def handle(self, event: MessageEvent) -> None:
        """
        处理消息 — 统一入口，命令和文本都由主引擎处理

        Args:
            event: 消息事件
        """
        # 获取或创建 Gateway 会话
        session = self._session_manager.get_or_create_session(event)

        # 委托给主引擎处理（含命令解析和 AI 对话）
        try:
            response = await self._process_message(
                session_id=session.session_id,
                text=event.text,
                platform=event.platform,
                chat_id=event.chat_id,
                user_id=event.user_id,
                media_urls=event.media_urls,
            )

            if response:
                # 发送响应
                result = await self._send_message(
                    platform=event.platform,
                    chat_id=event.chat_id,
                    content=response,
                )

                if not result.success:
                    logger.warning(f"[MessageHandler] Failed to send response: {result.error}")

        except asyncio.TimeoutError:
            logger.error("[MessageHandler] AI processing timeout")
            await self._send_message(
                platform=event.platform,
                chat_id=event.chat_id,
                content="抱歉，AI 处理超时了。请稍后重试。",
            )
        except Exception as e:
            logger.error(f"[MessageHandler] Processing error: {e}", exc_info=True)
            await self._send_message(
                platform=event.platform,
                chat_id=event.chat_id,
                content=f"处理消息时出错: {str(e)[:200]}",
            )
