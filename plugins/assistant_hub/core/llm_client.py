# -*- coding: utf-8 -*-
"""llm_client.py — 助手记忆/Dream/经验的一次性 LLM 调用（OpenAI 兼容直连）。

设计：
- 读全局 provider 配置（Settings.llm_selected_model → llm_saved_providers），
  与主对话共用当前选中的服务商；显式传 base_url/api_key/model 可覆盖。
- 同步阻塞调用（只允许在后台线程使用），超时 60s 默认。
- 空 API_KEY（免 key 匿名端点）不带 Authorization 头。
- 任何失败抛 LLMUnavailableError，由调用方（ticker/dream/experience）静默降级。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LLMUnavailableError(RuntimeError):
    """无可用服务商配置或请求失败。"""


def _settings():
    from app.utils.config import Settings

    return Settings.get_instance()


def resolve_model_config(config_id: str = "") -> Dict[str, str]:
    """解析模型配置；优先指定 config_id（助手记忆整理模型），缺省跟随全局选中项。"""
    cfg = _settings()
    saved = cfg.llm_saved_providers.value or {}
    info: Optional[Dict[str, Any]] = None
    if config_id:
        info = saved.get(config_id)
        if not isinstance(info, dict):
            raise LLMUnavailableError(f"记忆整理模型配置不存在: {config_id}")
    if not isinstance(info, dict):
        selected = cfg.llm_selected_model.value or ""
        info = saved.get(selected) if selected else None
    if not isinstance(info, dict):
        for v in saved.values():
            if isinstance(v, dict) and v.get("API_URL"):
                info = v
                break
    if not isinstance(info, dict) or not info.get("API_URL"):
        raise LLMUnavailableError("无可用服务商配置")
    return {
        "base_url": str(info["API_URL"]).rstrip("/"),
        "api_key": str(info.get("API_KEY") or ""),
        "model": str(info.get("模型名称") or ""),
        "provider_name": str(info.get("provider_name") or ""),
    }


def chat_once(
    messages: List[Dict[str, str]],
    *,
    model: str = "",
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: int = 60,
    base_url: str = "",
    api_key: str = "",
    config_id: str = "",
    model_config: Optional[Dict[str, Any]] = None,
) -> str:
    """单轮补全：返回助手文本；失败抛 LLMUnavailableError。

    config_id：指定 llm_saved_providers 键（空 = 跟随全局）。
    model_config：完整配置覆盖 dict（含 API_URL/模型名称，cron-tasks 同款
    model_config_override 模式），优先级高于 config_id。
    """
    if isinstance(model_config, dict) and model_config.get("API_URL"):
        cfg = {
            "base_url": str(model_config["API_URL"]).rstrip("/"),
            "api_key": str(model_config.get("API_KEY") or ""),
            "model": str(model_config.get("模型名称") or ""),
            "provider_name": str(model_config.get("provider_name") or ""),
        }
    else:
        cfg = resolve_model_config(config_id)
    url = (base_url or cfg["base_url"]) + "/chat/completions"
    key = api_key or cfg["api_key"]
    body = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except LLMUnavailableError:
        raise
    except Exception as e:
        logger.warning(f"[assistant_hub.llm] 请求失败: {e}")
        raise LLMUnavailableError(f"LLM 请求失败: {e}") from e
    if not content:
        raise LLMUnavailableError("LLM 返回空内容")
    return content
