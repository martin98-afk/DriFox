# -*- coding: utf-8 -*-
"""
Slack 平台适配器（系统插件，万物即插件 Phase E）

使用 Slack Web API 进行消息收发。

本文件原位于 app/gateway/adapters/extra.py（E2 Task 5 迁入）。
适配器实现保持原状（SDK 延迟导入纪律：模块顶层不 eager import 平台 SDK）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    SendResult,
)


class SlackAdapter(BasePlatformAdapter):
    """
    Slack 适配器

    使用 Slack WebSocket 模式进行消息收发。
    """

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SLACK)

        self._bot_token = config.extra.get("bot_token") or os.getenv("SLACK_BOT_TOKEN")
        self._app_token = config.extra.get("app_token") or os.getenv("SLACK_APP_TOKEN")
        self._client = None

    async def connect(self) -> bool:
        """连接到 Slack"""
        if not self._bot_token:
            logger.error("[Slack] bot_token is required")
            return False

        try:
            from slack_sdk import WebClient

            # 创建 Web API 客户端
            self._client = WebClient(token=self._bot_token)

            # 验证 token
            self._client.auth_test()

            self._connected = True
            logger.info("[Slack] Connected successfully")
            return True

        except Exception as e:
            logger.error("[Slack] Failed to connect: %s", e)
            return False

    async def disconnect(self) -> None:
        """断开连接"""
        if self._client:
            try:
                if hasattr(self._client, "_session"):
                    # 清理资源
                    pass
            except Exception as e:
                logger.warning("[Slack] Error during disconnect: %s", e)

        self._connected = False
        logger.info("[Slack] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送消息"""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # 格式化内容 (使用 Block Kit 格式)
            formatted = self._format_slack_text(content)

            # 分割长消息
            if len(formatted) > self.MAX_MESSAGE_LENGTH:
                chunks = self._split_message(formatted)
            else:
                chunks = [formatted]

            message_ids = []

            for i, chunk in enumerate(chunks):
                kwargs = {
                    "channel": chat_id,
                    "text": chunk,
                }

                # 添加线程回复
                if reply_to and i == 0:
                    kwargs["thread_ts"] = reply_to

                response = self._client.chat_postMessage(**kwargs)
                message_ids.append(response["ts"])

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )

        except Exception as e:
            logger.error("[Slack] Send failed: %s", e)
            return SendResult(success=False, error=str(e))

    def _format_slack_text(self, content: str) -> str:
        """格式化 Slack 文本"""
        if not content:
            return content

        # Slack 文本格式
        result = content
        # 转换 Markdown 到 Slack 格式
        result = result.replace("**", "*")  # 粗体
        result = result.replace("__", "_")  # 斜体

        return result

    def _split_message(self, content: str) -> List[str]:
        """分割消息"""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]

        chunks = []
        paragraphs = content.split("\n\n")
        current = ""

        for para in paragraphs:
            if len(current) + len(para) + 2 <= self.MAX_MESSAGE_LENGTH:
                current += ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                current = para

        if current:
            chunks.append(current)

        return chunks if chunks else [content]

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送图片"""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # 上传图片到 Slack
            if image_url.startswith("http"):
                # 下载并上传
                import os
                import tempfile

                import httpx

                async with httpx.AsyncClient() as client:
                    response = await client.get(image_url, timeout=30.0)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                        f.write(response.content)
                        temp_path = f.name

                try:
                    response = self._client.files_upload_v2(
                        channel=chat_id,
                        file=temp_path,
                        title=caption or "Image",
                    )
                    return SendResult(
                        success=True,
                        message_id=response.get("file", {}).get("id", ""),
                    )
                finally:
                    os.unlink(temp_path)
            else:
                # 本地文件
                response = self._client.files_upload_v2(
                    channel=chat_id,
                    file=image_url,
                    title=caption or "Image",
                )
                return SendResult(
                    success=True,
                    message_id=response.get("file", {}).get("id", ""),
                )

        except Exception as e:
            logger.error("[Slack] Send image failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """发送 typing 状态"""
        if self._client:
            try:
                self._client.api_call(
                    "chat.postMessage",
                    channel=chat_id,
                    text="...",
                    unflink=True,
                )
            except Exception:
                pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """获取聊天信息"""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}

        try:
            response = self._client.conversations_info(channel=chat_id)
            channel = response.get("channel", {})
            return {
                "name": channel.get("name", chat_id),
                "type": "dm" if channel.get("is_im") else "group",
            }
        except Exception as e:
            logger.error("[Slack] get_chat_info failed: %s", e)
            return {"name": str(chat_id), "type": "dm"}


# ── Phase E 插件注册 ────────────────────────────────────
# 配置读写回调走主程序 Settings（存量用户配置零迁移；Task 5 统一切 E1
# PluginConfigStore 时仅改本块闭包，调用方不动）。闭包内延迟 import
# Settings/PlatformConfig，避免模块顶层触发 PyQt5 / Settings 副作用。
#
# check_requirements：原 extra.py 无 Slack 依赖检查函数（构造时缺包由
# __init__ 兜底为 None + try/except 容忍），此处按主控指令内联 lambda: True；
# 缺包时由 connect() 内 try/except 自然失败。


def _build_config() -> "PlatformConfig":
    """读主程序 Settings 构造 Slack 配置（存量用户配置零迁移）"""
    from app.gateway.base import Platform, PlatformConfig
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    return PlatformConfig(
        enabled=cfg.gateway_slack_enabled.value,
        platform=Platform.SLACK,
        extra={
            "bot_token": cfg.gateway_slack_bot_token.value,
            "app_token": cfg.gateway_slack_app_token.value,
        },
    )


def _write_config(config: "PlatformConfig") -> None:
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    cfg.set(cfg.gateway_slack_enabled, config.enabled, save=True)
    if config.extra:
        if config.extra.get("bot_token") is not None:
            cfg.set(cfg.gateway_slack_bot_token, config.extra["bot_token"], save=True)
        if config.extra.get("app_token") is not None:
            cfg.set(cfg.gateway_slack_app_token, config.extra["app_token"], save=True)


def _build_config_values(values: dict, old_config) -> "PlatformConfig":
    """设置卡保存回调：表单值 → PlatformConfig（对齐旧 _on_save SLACK 分支）"""
    from app.gateway.base import Platform, PlatformConfig

    extra = dict(old_config.extra) if old_config and old_config.extra else {}
    if "bot_token" in values:
        extra["bot_token"] = values.get("bot_token") or ""
    if "app_token" in values:
        extra["app_token"] = values.get("app_token") or ""
    return PlatformConfig(
        enabled=bool(values.get("enabled", False)),
        platform=Platform.SLACK,
        extra=extra,
    )


def register(registry) -> None:
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="slack",
            display_name="Slack",
            adapter_factory=lambda cfg: SlackAdapter(cfg),
            check_requirements=lambda: True,
            config_builder=_build_config,
            config_writer=_write_config,
            build_config_values=_build_config_values,
            validate_config=lambda cfg: (
                bool(cfg.extra.get("bot_token")),
                "Bot Token 未配置",
            ),
            ui_order=60,
        )
    )
