# -*- coding: utf-8 -*-
"""模型/服务商枚举能力的共享实现（供 ui 与 tool 双 surface 注册同一份 provider）。

背景：插件里渲染「选服务商 / 选模型」下拉此前没有合规通道，四个插件各自
json.load(~/.drifox6/app.config) 的 LLM.SavedProviders 抄同一份样板（git-panel /
prompt-enhancer / chinese-chess / ip-switcher）。这里把它收成能力，让插件
不再自写路径判定与 JSON 结构假设。

为什么单独成文件：provider.list / model.enumerate 注册在 ui 与 tool 两个
surface，逻辑完全相同。放任一 *_core.py 里另一面就得跨面 import；复制两份
则会漂移。两个 provider 函数是纯函数（host 进、数据出），天然可共享。

数据来源的唯一真相是**窗口内存态** `_valid_configs`：密钥模式下磁盘
app.config 的 API_KEY 是密文/空串，且用户刚添加的服务商可能尚未落盘。
因此 hosts 为 tool surface 时自行解析活跃主窗口，解析不到则返回空列表
（不抛错，让调用方按「暂无候选」降级）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger


def _resolve_widget(host: Any) -> Any:
    """拿到承载模型配置的 MainWidget。

    host 是 MainWidget（ui surface）时直接用；否则（tool surface 的
    ToolExecutor / host=None）解析活跃主窗口。取不到返回 None。
    """
    if getattr(host, "_valid_configs", None) is not None:
        return host
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        return getattr(reg, "_main_widget", None) or next(iter(getattr(reg, "_window_main_widgets", {}).values()), None)
    except Exception:
        return None


def _models_for(widget: Any, config_id: str) -> List[str]:
    """指定服务商的可选模型名（复用主窗口的三重兜底实现）。"""
    get_list = getattr(widget, "_get_model_list_for_provider", None)
    if not callable(get_list):
        return []
    try:
        models = get_list(config_id)
    except Exception as e:  # noqa: BLE001 — 单窗口异常不应炸掉整个枚举
        logger.debug(f"[Capability] 取模型列表失败 cid={config_id}: {e}")
        return []
    return [str(m) for m in (models or []) if str(m).strip()]


def _display_name(config: Dict[str, Any], config_id: str) -> str:
    """实例显示名三级回落：display_name → provider_name → config_id。"""
    return str(config.get("display_name") or config.get("provider_name") or config_id or "")


def _models_dev_id(provider_name: str) -> str:
    """取 ProviderDef 声明的 models.dev provider id；查不到给空串。"""
    if not provider_name:
        return ""
    try:
        from app.plugins.registries.provider_registry import ProviderRegistry

        pdef = ProviderRegistry.get_instance().get(provider_name)
    except Exception:
        return ""
    return str(getattr(pdef, "models_dev_id", "") or "")


def provider_list(host: Any):
    """provider.list 的 provider：返回已配置服务商清单（不含密钥与 URL）。

    每项：id / provider_name / display_name / is_current / model_count /
    models_dev_id。
    ⚠️ 刻意不带 API_KEY 与 API_URL：前者属于 model.config 的职责；后者看着
    无害，配合社工手法可把插件流量引到别处。
    """

    def list_providers() -> List[Dict[str, Any]]:
        widget = _resolve_widget(host)
        valid = getattr(widget, "_valid_configs", None)
        if not isinstance(valid, dict) or not valid:
            return []
        current_id = str(getattr(widget, "_current_provider_name", "") or "")
        out: List[Dict[str, Any]] = []
        for config_id, config in valid.items():
            if not isinstance(config, dict):
                continue
            cid = str(config_id or "")
            pname = str(config.get("provider_name") or cid)
            out.append(
                {
                    "id": cid,
                    "provider_name": pname,
                    "display_name": _display_name(config, cid),
                    "is_current": cid == current_id,
                    "model_count": len(_models_for(widget, cid)),
                    "models_dev_id": _models_dev_id(pname),
                }
            )
        out.sort(key=lambda item: (item["display_name"] or "").lower())
        return out

    return list_providers


def model_enumerate(host: Any):
    """model.enumerate 的 provider：指定服务商的可选模型列表（含能力标记）。

    每项：name / is_current / supports_vision / supports_thinking /
    context_limit / note。

    provider 参数走主窗口 `_resolve_service_provider` 的五级匹配（config_id →
    display_name 缓存 → display_name 遍历 → provider_name 精确 → 大小写不敏感）；
    未知服务商返回空列表，**不静默串到别的服务商**。缺省 provider = 当前服务商。
    """

    def enumerate_models(provider: str = "") -> List[Dict[str, Any]]:
        widget = _resolve_widget(host)
        valid = getattr(widget, "_valid_configs", None)
        if not isinstance(valid, dict) or not valid:
            return []

        resolver = getattr(widget, "_resolve_service_provider", None)
        if str(provider or "").strip():
            if not callable(resolver):
                return []
            config_id = resolver(str(provider).strip())
            if not config_id or config_id not in valid:
                return []  # 显式指名的服务商不存在 → 不静默串到别的服务商
        else:
            config_id = str(getattr(widget, "_current_provider_name", "") or "") or next(iter(valid), "")
        if not config_id or config_id not in valid:
            return []

        config = valid[config_id] or {}
        models = _models_for(widget, config_id)
        if not models:
            return []

        is_root = config_id == str(getattr(widget, "_current_provider_name", "") or "")
        current_model = (
            str(getattr(widget, "_current_model_name", "") or "") if is_root else str(config.get("模型名称") or "")
        )
        pname = str(config.get("provider_name") or config_id)

        out: List[Dict[str, Any]] = []
        for name in models:
            out.append(
                {
                    "name": name,
                    "is_current": bool(current_model) and name == current_model,
                    **_model_caps(name, pname),
                }
            )
        return out

    return enumerate_models


def _model_caps(model: str, provider_name: str) -> Dict[str, Any]:
    """单模型的展示用能力字段（视觉/思考/上下文/说明）。

    走 resolve_model_capabilities 而非裸 get_model_capabilities：前者把
    「声明_*」六键放在 L0，用户对自家网关的显式声明优先于 models.dev 推断值
    ——第三方网关的额度与能力与官方 API 各自成立，不该被反向覆盖。
    """
    try:
        from app.plugins.sdk import resolve_model_capabilities

        caps = resolve_model_capabilities({"模型名称": model, "provider_name": provider_name}) or {}
    except Exception as e:  # noqa: BLE001 — 能力查询失败不该中断枚举
        logger.debug(f"[Capability] 取模型能力失败 {provider_name}/{model}: {e}")
        caps = {}
    return {
        "supports_vision": bool(caps.get("supports_vision")),
        "supports_thinking": bool(caps.get("supports_thinking")),
        "context_limit": _as_int_or_none(caps.get("context_limit")),
        "note": str(caps.get("note") or ""),
    }


def _as_int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except TypeError, ValueError:
        return None
