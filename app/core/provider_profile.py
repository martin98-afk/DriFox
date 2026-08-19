# -*- coding: utf-8 -*-
"""
Provider profile helpers.

These helpers intentionally derive capabilities from the application's
live provider config (`API_URL`, `模型名称`, `认证方式`) so the chatter layer
stays aligned with software-level provider settings.
"""

import hashlib
import uuid as _uuid
from typing import Any, Dict, List


class ProviderConfigCollision(Exception):
    """保存时 (API_URL, API_KEY) 组合与已有条目冲突"""

    def __init__(self, existing_config_id: str, existing_name: str = ""):
        self.existing_config_id = existing_config_id
        self.existing_name = existing_name
        super().__init__(
            f"该 (API_URL, API_KEY) 组合已被「{existing_name or existing_config_id}」占用，请修改后重试"
        )


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

    - 新建（is_new=True）：按 (URL, apikey) hash 落新条目；如果 hash 撞到已有条目则拒绝。
    - 编辑且 (URL, apikey) 没变：原位更新同一条目。
    - 编辑且 (URL, apikey) 变了：删旧条目，按新组合落新位置（新 hash 撞到别的条目则拒绝）。
    - 编辑模式 + 兜底（provider_info 没带 config_id 字段的老数据）：
      按 (URL, apikey) 在 saved_providers 里反查，查到多条则合并，查不到则落新位置。

    该函数会就地修改 saved_providers 和 provider_info（写入 config_id 字段）。
    发生冲突时抛出 ProviderConfigCollision。
    """
    new_config_id = compute_provider_config_id(provider_info)
    old_config_id = provider_info.get("config_id", "")

    # ---- 兜底：编辑模式 + 老数据（无 config_id 字段）按 (URL, KEY) 反查 ----
    # 新建模式不走此兜底（正常情况新建表单不带 config_id）
    if not old_config_id and not is_new:
        api_url = (provider_info.get("API_URL", "") or "").strip()
        api_key = (provider_info.get("API_KEY", "") or "").strip()
        duplicates: List[str] = [
            k
            for k, v in saved_providers.items()
            if (v.get("API_URL", "") or "").strip() == api_url
            and (v.get("API_KEY", "") or "").strip() == api_key
        ]
        if duplicates:
            old_config_id = duplicates[0]
            for dup in duplicates[1:]:
                saved_providers.pop(dup, None)

    # ---- 写入 hash 到 provider_info ----
    provider_info["config_id"] = new_config_id
    provider_info["provider_name"] = provider_name

    # ---- 撞 id 冲突检测 ----
    # new_config_id 已经是别人家的 key 了 → 拒绝保存，抛出异常
    if new_config_id != old_config_id and new_config_id in saved_providers:
        existing = saved_providers[new_config_id]
        existing_name = (
            existing.get("name", "")
            or existing.get("provider_name", "")
            or new_config_id
        )
        raise ProviderConfigCollision(new_config_id, existing_name)

    # ---- 写入 saved_providers ----
    if is_new and old_config_id == new_config_id:
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
# 注：PROVIDER_CAPABILITIES 已迁移至 providers 插件
# （ProviderDef.capabilities / family 字段），运行时经 ProviderRegistry
# 的 family_capabilities() 聚合，不再存在硬编码能力表。


def detect_provider_family(llm_config: Dict[str, Any]) -> str:
    api_url = str(llm_config.get("API_URL", "") or "").lower()
    model = str(llm_config.get("模型名称", "") or "").lower()
    auth = str(llm_config.get("认证方式", "") or "").lower()

    if "anthropic" in api_url or model.startswith("claude"):
        return "anthropic"
    if "generativelanguage.googleapis.com" in api_url or model.startswith("gemini"):
        return "gemini"
    if "dashscope.aliyuncs.com" in api_url or "maas.aliyuncs.com" in api_url or model.startswith("qwen"):
        return "dashscope"
    if "bigmodel.cn" in api_url or model.startswith("glm"):
        return "zhipu"
    if "opencode.ai" in api_url:
        return "opencode"
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

    # family 能力由 providers 插件聚合（ProviderRegistry.family_capabilities），
    # 插件未声明的 family（如自定义 lmstudio）回退内置 custom 兜底
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        capabilities = ProviderRegistry.get_instance().family_capabilities(family)
    except Exception:
        capabilities = {}
    profile = dict(capabilities)
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
