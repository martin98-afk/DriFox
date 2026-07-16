# -*- coding: utf-8 -*-
"""
models.dev 模型元数据同步模块。

启动时从 https://models.dev/api.json 拉取最新模型列表与能力，
按 DriFox 支持的服务商白名单解析，并转换为本地模型能力格式。
拉取结果缓存到 config/models_dev_cache.json，TTL 24 小时。

本模块只负责"拉取 + 缓存 + 转换"，合并到硬编码配置的逻辑在调用方：
  - app/constants.py 的 get_merged_provider_models()
  - app/core/model_capabilities.py 的 get_merged_model_capabilities()
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# ============================================================
# 常量
# ============================================================
MODELS_DEV_API_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 24 * 3600  # 24 小时


def _default_cache_path() -> Path:
    """返回默认缓存文件路径。

    缓存放在 get_app_data_dir()/cache/ 下，与运行时数据一起，不纳入 git。
    这里延迟导入 app.utils.utils，避免模块顶层循环依赖。
    """
    try:
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "cache" / "models_dev_cache.json"
    except Exception:
        # 兜底：项目根目录下的 .drifox/cache/
        return Path(__file__).resolve().parent.parent.parent / ".drifox" / "cache" / "models_dev_cache.json"


# DriFox 服务商名 -> models.dev provider id
MODELS_DEV_PROVIDER_MAP = {
    "OpenAI": "openai",
    "Anthropic (Claude)": "anthropic",
    "Google Gemini": "google",
    "DeepSeek": "deepseek",
    "智谱AI": "zhipuai",
    "MiniMax": "minimax",
    "阿里云 (DashScope)": "alibaba",
    "SiliconFlow (硅基流动)": "siliconflow",
    "Groq": "groq",
    "Ollama": "ollama-cloud",
    "OpenCode Zen": "opencode",
    "OpenCode Go": "opencode-go",
}

# models.dev reasoning_options type -> DriFox thinking_param
REASONING_TYPE_TO_THINKING_PARAM = {
    "effort": "reasoning_effort",
    "toggle": "thinking",
    "budget_tokens": "thinking_budget",
}

# 当 models.dev 标记 reasoning=True 但未给出具体控制方式时，
# 默认按 toggle（thinking: enabled/disabled）处理。
# 这是保守推断：用户至少能开关思考；若实际 API 需要其他参数，
# 应由后续硬编码条目或 provider_profile 覆盖。
DEFAULT_REASONING_PARAM = "thinking"


# ============================================================
# 缓存操作
# ============================================================
def _get_cache_path() -> Path:
    """返回缓存文件路径。"""
    return _default_cache_path()


def _load_cache(cache_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """读取本地缓存。不存在或解析失败返回 None。"""
    path = cache_path or _get_cache_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            return None
        return cache
    except Exception as e:
        logger.warning(f"[models.dev] 读取缓存失败: {e}")
        return None


def _save_cache(data: Dict[str, Any], cache_path: Optional[Path] = None) -> None:
    """保存缓存到本地。"""
    path = cache_path or _get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[models.dev] 保存缓存失败: {e}")


def _is_cache_valid(cache: Optional[Dict[str, Any]]) -> bool:
    """检查缓存是否存在且未过期。"""
    if not cache:
        return False
    timestamp = cache.get("_cached_at")
    if not isinstance(timestamp, (int, float)):
        return False
    return (time.time() - timestamp) < CACHE_TTL_SECONDS


# ============================================================
# 网络拉取
# ============================================================
def _fetch_remote(url: str = MODELS_DEV_API_URL, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """从 models.dev 拉取 API 数据。失败返回 None。"""
    try:
        import httpx
    except ImportError:
        logger.warning("[models.dev] 未安装 httpx，跳过远程同步")
        return None

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            logger.warning("[models.dev] 返回数据格式异常")
            return None
        return data
    except Exception as e:
        logger.warning(f"[models.dev] 拉取失败: {e}")
        return None


# ============================================================
# 数据转换
# ============================================================
def _transform_model(provider_id: str, model_id: str, model_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 models.dev 单个模型条目转成 DriFox 能力 dict。

    返回 None 表示数据缺失或无法转换（例如没有 context limit）。
    """
    if not isinstance(model_info, dict):
        return None

    limit = model_info.get("limit") or {}
    if not isinstance(limit, dict):
        return None

    context_limit = limit.get("context")
    if context_limit is None:
        # 没有上下文长度的模型对 DriFox 没有意义，跳过
        return None

    try:
        context_limit = int(context_limit)
    except ValueError, TypeError:
        return None
    if context_limit <= 0:
        return None

    modalities = model_info.get("modalities") or {}
    input_modalities = modalities.get("input") or []
    supports_vision = "image" in input_modalities

    reasoning = bool(model_info.get("reasoning", False))
    reasoning_options = model_info.get("reasoning_options") or []
    reasoning_type = None
    # reasoning_options 为空 → 模型有思考能力但不可控，不暴露开关
    has_thinking_controls = bool(
        reasoning_options and isinstance(reasoning_options, list) and len(reasoning_options) > 0
    )
    if reasoning and has_thinking_controls:
        first_opt = reasoning_options[0]
        if isinstance(first_opt, dict):
            reasoning_type = first_opt.get("type")
        thinking_param = REASONING_TYPE_TO_THINKING_PARAM.get(reasoning_type) or DEFAULT_REASONING_PARAM
    else:
        thinking_param = None

    # 输出上限（可选）
    max_output_tokens = limit.get("output")
    if max_output_tokens is not None:
        try:
            max_output_tokens = int(max_output_tokens)
            if max_output_tokens <= 0:
                max_output_tokens = None
        except ValueError, TypeError:
            max_output_tokens = None

    result: Dict[str, Any] = {
        "context_limit": context_limit,
        "supports_vision": supports_vision,
        "supports_thinking": reasoning and has_thinking_controls,
        "source": "models.dev",
        "note": model_info.get("description", ""),
        "release_date": model_info.get("release_date"),
    }
    if thinking_param:
        result["thinking_param"] = thinking_param
    if max_output_tokens is not None:
        result["max_output_tokens"] = max_output_tokens

    return result


def _parse_models_dev_data(data: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """解析 models.dev 数据，返回 (provider_models, model_capabilities)。

    只处理 MODELS_DEV_PROVIDER_MAP 白名单内的服务商。
    """
    provider_models: Dict[str, List[str]] = {name: [] for name in MODELS_DEV_PROVIDER_MAP}
    model_capabilities: Dict[str, Dict[str, Any]] = {}

    for dfox_name, provider_id in MODELS_DEV_PROVIDER_MAP.items():
        provider_info = data.get(provider_id)
        if not isinstance(provider_info, dict):
            continue
        models = provider_info.get("models") or {}
        if not isinstance(models, dict):
            continue

        for model_id, model_info in models.items():
            transformed = _transform_model(provider_id, model_id, model_info)
            if transformed is None:
                continue
            provider_models[dfox_name].append(model_id)
            model_capabilities[model_id] = transformed

    return provider_models, model_capabilities


# ============================================================
# 对外 API
# ============================================================
@dataclass
class DynamicModelsResult:
    """models.dev 同步结果。"""

    provider_models: Dict[str, List[str]]
    model_capabilities: Dict[str, Dict[str, Any]]
    from_cache: bool = False
    fetched_at: Optional[float] = None


def load_dynamic_models(
    force: bool = False,
    cache_path: Optional[Path] = None,
) -> DynamicModelsResult:
    """加载 models.dev 动态模型配置。

    逻辑：
      1. 若 force=True 或缓存不存在/过期，尝试远程拉取。
      2. 远程拉取成功：更新缓存，解析返回。
      3. 远程拉取失败：若缓存存在（即使过期），用缓存；否则返回空结果。
      4. 若 force=False 且缓存有效：直接读取缓存。

    该函数尽量轻量；网络失败不会抛异常，而是回退到缓存/空数据。
    """
    path = cache_path or _get_cache_path()
    cache = _load_cache(path)

    if force or not _is_cache_valid(cache):
        logger.info("[models.dev] 尝试同步最新模型元数据...")
        remote_data = _fetch_remote()
        if remote_data is not None:
            provider_models, model_capabilities = _parse_models_dev_data(remote_data)
            cache = {
                "_cached_at": time.time(),
                "_url": MODELS_DEV_API_URL,
                "provider_models": provider_models,
                "model_capabilities": model_capabilities,
            }
            _save_cache(cache, path)
            return DynamicModelsResult(
                provider_models=provider_models,
                model_capabilities=model_capabilities,
                from_cache=False,
                fetched_at=cache["_cached_at"],
            )
        # 远程失败：回退到缓存（即使过期）
        if cache is not None:
            logger.info("[models.dev] 远程拉取失败，使用过期缓存")
        else:
            logger.warning("[models.dev] 远程拉取失败且无缓存，返回空动态数据")

    if cache is not None:
        return DynamicModelsResult(
            provider_models=cache.get("provider_models", {}),
            model_capabilities=cache.get("model_capabilities", {}),
            from_cache=True,
            fetched_at=cache.get("_cached_at"),
        )

    return DynamicModelsResult(provider_models={}, model_capabilities={}, from_cache=False, fetched_at=None)


# 模块级缓存，避免同一次运行中重复加载
_dynamic_models_cache: Optional[DynamicModelsResult] = None


def get_dynamic_models(force: bool = False) -> DynamicModelsResult:
    """带模块级内存缓存的 load_dynamic_models。"""
    global _dynamic_models_cache
    if _dynamic_models_cache is None or force:
        _dynamic_models_cache = load_dynamic_models(force=force)
    return _dynamic_models_cache


def clear_memory_cache() -> None:
    """清空模块级内存缓存，下次调用 get_dynamic_models 时重新加载。"""
    global _dynamic_models_cache
    _dynamic_models_cache = None
