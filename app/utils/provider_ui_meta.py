# -*- coding: utf-8 -*-
"""服务商 UI 元数据取值基座（P1-18）。

统一「插件声明优先 → 内置/存档回退」的取值口径，供 UI 层消费。
背景：认证方式 / URL 预设 / family 三条链此前在 UI 层硬编码，与 providers
插件的声明形成双源且已漂移（火山插件是 ``api/coding/v3`` 而 UI 硬编码只给
``api/v3``；阿里云/智谱各有 2 个 URL）。

纪律：
- **函数内 lazy import** ``ProviderRegistry``（模块顶层禁 import）：本模块会被
  UI 早期路径导入，而注册表首次读取会触发插件扫描（相对较重）。
- 取不到定义时返回兜底值，绝不抛异常——UI 层不应因插件缺失而崩。
"""

from typing import List

from loguru import logger

# 兜底认证方式：插件未声明且存档无值时使用
_DEFAULT_AUTH_TYPE = "bearer"


def get_auth_type(provider_name: str, saved_auth_type: str = "") -> str:
    """解析服务商认证方式：插件声明 → 存档值 → bearer。

    Args:
        provider_name: 服务商名（ProviderDef.name）
        saved_auth_type: 服务商配置里已存的「认证方式」（用户编辑过的历史值）

    Returns:
        认证方式字符串（bearer / bce / none / anthropic …）
    """
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        p = ProviderRegistry.get_instance().get(provider_name)
    except Exception as e:
        logger.debug(f"[provider_ui_meta] get_auth_type 查注册表失败 provider={provider_name!r}: {e}")
        p = None
    if p is not None:
        declared = str(getattr(p, "auth_type", "") or "").strip()
        if declared:
            return declared
    saved = str(saved_auth_type or "").strip()
    if saved:
        return saved
    return _DEFAULT_AUTH_TYPE


def get_preset_urls(provider_name: str) -> List[str]:
    """解析服务商预设 API URL 列表：插件 preset_urls → [api_url] → []。

    返回的列表已去重且保序（插件声明序），可直接喂给 URL 下拉框。
    """
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        p = ProviderRegistry.get_instance().get(provider_name)
    except Exception as e:
        logger.debug(f"[provider_ui_meta] get_preset_urls 查注册表失败 provider={provider_name!r}: {e}")
        p = None
    if p is None:
        return []

    # strip 后滤空：与 fallback 分支口径一致（声明里可能混入空白串）
    urls: List[str] = [u.strip() for u in (getattr(p, "preset_urls", None) or []) if str(u or "").strip()]
    if not urls:
        fallback = str(getattr(p, "api_url", "") or "").strip()
        if fallback:
            urls = [fallback]
    return list(dict.fromkeys(urls))


def get_family(provider_name: str) -> str:
    """解析服务商能力族：插件声明 → ""（内置探测链由调用方兜底）。"""
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        p = ProviderRegistry.get_instance().get(provider_name)
    except Exception as e:
        logger.debug(f"[provider_ui_meta] get_family 查注册表失败 provider={provider_name!r}: {e}")
        p = None
    if p is None:
        return ""
    return str(getattr(p, "family", "") or "")
