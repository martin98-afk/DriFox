# -*- coding: utf-8 -*-
"""
Provider profile helpers.

These helpers intentionally derive capabilities from the application's
live provider config (`API_URL`, `模型名称`, `认证方式`) so the chatter layer
stays aligned with software-level provider settings.
"""

import hashlib
import uuid as _uuid
from typing import Any, Dict


def compute_provider_config_id(provider_info: Dict[str, Any]) -> str:
    """用 (API_URL, API_KEY) 的稳定 hash 作为 config_id（替代旧 uuid）。

    返回 SHA-256(f"{url}\\x00{apikey}") 的前 8 位 hex；
    url 和 apikey 都为空时兜底用随机 uuid。

    用 (URL, apikey) 而非纯 apikey 的目的：同一服务商常会有「coding plan
    vs 普通 plan」之类走不同 base_url 的配置，URL 参与 hash 才能正确区分。
    用 hash 而非 uuid 的目的：编辑同一 (URL, apikey) 的服务商时始终命中
    同一条目，避免「保存后产生重复条目」的 bug。
    """
    api_url = (provider_info.get("API_URL", "") or "").strip()
    api_key = (provider_info.get("API_KEY", "") or "").strip()
    if not api_url and not api_key:
        return _uuid.uuid4().hex[:8]
    return hashlib.sha256(f"{api_url}\x00{api_key}".encode("utf-8")).hexdigest()[:8]


def apply_provider_save(
    saved_providers: Dict[str, Dict[str, Any]],
    provider_info: Dict[str, Any],
    provider_name: str,
    is_new: bool = False,
) -> str:
    """把一次服务商保存应用到 saved_providers 字典上，返回新的 config_id。

    - 新建（is_new=True）或没有旧 config_id：按 (URL, apikey) hash 落新条目。
    - 编辑且 (URL, apikey) 没变：原位更新同一条目。
    - 编辑且 (URL, apikey) 变了：删旧条目，按新 (URL, apikey) 落新条目。
    - 兜底：若 provider_info 没带 config_id 字段（老数据 / 迁移期），
      按 (URL, apikey) 在 saved_providers 里反查；查到多条则保留第一条，其余删掉。

    该函数会就地修改 saved_providers 和 provider_info（写入 config_id 字段）。
    """
    new_config_id = compute_provider_config_id(provider_info)
    old_config_id = provider_info.get("config_id", "")
    api_url = (provider_info.get("API_URL", "") or "").strip()
    api_key = (provider_info.get("API_KEY", "") or "").strip()

    # 兜底：老数据里 value 没有 config_id 字段，按 (URL, apikey) 反查
    if not old_config_id:
        duplicates = [
            k
            for k, v in saved_providers.items()
            if (v.get("API_URL", "") or "").strip() == api_url
            and (v.get("API_KEY", "") or "").strip() == api_key
        ]
        if duplicates:
            old_config_id = duplicates[0]
            # 顺带清理同 (URL, apikey) 的重复条目
            for dup in duplicates[1:]:
                saved_providers.pop(dup, None)

    # 始终以新 hash 为准
    provider_info["config_id"] = new_config_id
    provider_info["provider_name"] = provider_name

    if is_new and old_config_id == new_config_id:
        # 新建流程但 (URL, apikey) 命中已有条目 → 走更新分支
        saved_providers[new_config_id] = provider_info
        return new_config_id

    if is_new or not old_config_id:
        saved_providers[new_config_id] = provider_info
        return new_config_id

    if old_config_id == new_config_id:
        saved_providers[new_config_id] = provider_info
        return new_config_id

    # (URL, apikey) 变了：旧条目删掉，按新组合落新位置
    if old_config_id in saved_providers:
        del saved_providers[old_config_id]
    saved_providers[new_config_id] = provider_info
    return new_config_id


# 合理的输出 token 上限（基于各模型已知的 API 限制）
# 这些值作为用户未指定 max_tokens 时的默认值，
# 不再作为硬性截断上限（具体截断逻辑在 chat_worker 中处理）
PROVIDER_CAPABILITIES = {
    "anthropic": {
        "context_limit": 200000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "openai": {
        "context_limit": 128000,
        "max_output_tokens": 16384,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "gemini": {
        "context_limit": 1000000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "dashscope": {
        "context_limit": 1000000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "zhipu": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": True,
        "thinking_param": "thinking",         # extra_body.thinking = {type}
        "reasoning_effort_param": None,
    },
    "deepseek": {
        "context_limit": 320000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": True,
        "thinking_param": "thinking",         # extra_body.thinking = {type}
        "reasoning_effort_param": "reasoning_effort",
    },
    "groq": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "minimax": {
        "context_limit": 1000000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "siliconflow": {
        "context_limit": 131072,
        "max_output_tokens": 16384,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": True,
        "thinking_param": "thinking_budget",  # 硅基流动用 thinking_budget 控制推理长度
        "reasoning_effort_param": None,
    },
    "baidu_qianfan": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "ollama": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "volcengine": {
        "context_limit": 1000000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "lmstudio": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": False,
        "thinking_param": None,
    },
    "custom": {
        "context_limit": 128000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": False,
        "supports_thinking": False,
        "thinking_param": None,
    },
}


def detect_provider_family(llm_config: Dict[str, Any]) -> str:
    api_url = str(llm_config.get("API_URL", "") or "").lower()
    model = str(llm_config.get("模型名称", "") or "").lower()
    auth = str(llm_config.get("认证方式", "") or "").lower()

    if "anthropic" in api_url or model.startswith("claude"):
        return "anthropic"
    if "generativelanguage.googleapis.com" in api_url or model.startswith("gemini"):
        return "gemini"
    if "dashscope.aliyuncs.com" in api_url or model.startswith("qwen"):
        return "dashscope"
    if "bigmodel.cn" in api_url or model.startswith("glm"):
        return "zhipu"
    if "deepseek.com" in api_url or model.startswith("deepseek"):
        return "deepseek"
    if "api.groq.com" in api_url or "groq/" in model:
        return "groq"
    if "siliconflow.cn" in api_url:
        return "siliconflow"
    if "minimax" in api_url or model.startswith("minimax"):
        return "minimax"
    if "volces.com" in api_url or "ark.cn-beijing" in api_url or model.startswith("doubao"):
        return "volcengine"
    if "qianfan.baidubce.com" in api_url or auth == "bce":
        return "baidu_qianfan"
    if "localhost:11434" in api_url or auth == "none":
        return "ollama"
    if "localhost:1234" in api_url:
        return "lmstudio"
    if "api.openai.com" in api_url or model.startswith(("gpt-", "o1", "o3")):
        return "openai"
    return "custom"


def get_provider_profile(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    family = detect_provider_family(llm_config)
    profile = dict(PROVIDER_CAPABILITIES.get(family, PROVIDER_CAPABILITIES["custom"]))
    profile["family"] = family
    profile["auth_type"] = str(llm_config.get("认证方式", "bearer") or "bearer").lower()
    return profile


def supports_vision(llm_config: Dict[str, Any]) -> bool:
    model = str(llm_config.get("模型名称", "") or "").lower()
    # 只有模型名称里包含视觉相关关键词时才返回 True，不要根据整个服务商判断
    vision_markers = ("vision", "vl", "llava", "glm-4v", "gpt-4o", "gpt-4o-mini", "claude-3")
    if any(marker in model for marker in vision_markers):
        return True
    return False
