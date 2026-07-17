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


# ============================================================
# OpenCode Zen 免费模型同步
# ============================================================
# OpenCode Zen 的 /v1/models 端点会返回用户账号下可用的模型列表，
# 其中包括带 -free 后缀的免费模型。这里单独拉取并合并到 models.dev 数据中，
# 比仅靠 models.dev 更实时。
def _fetch_opencode_zen_free_models(api_key: str = "") -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """从 OpenCode Zen /v1/models 获取带 -free 后缀的免费模型列表。

    返回 (free_model_names, model_capabilities)。
    网络失败或 API 异常时返回空，不影响主流程。
    """
    if not api_key:
        try:
            from app.constants import OPENCODE_SHARED_API_KEY

            api_key = OPENCODE_SHARED_API_KEY
        except Exception:
            logger.warning("[models.dev] 无法获取 OpenCode 共享 API Key，跳过免费模型同步")
            return [], {}

    url = "https://opencode.ai/zen/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[models.dev] OpenCode Zen API 调用失败: {e}")
        return [], {}

    # OpenAI-compatible 格式: {data: [{id: "model-name", ...}, ...]}
    models_data: list = []
    if isinstance(data, dict):
        if "data" in data:
            models_data = data["data"]
        elif "models" in data:
            models_data = data["models"]
    elif isinstance(data, list):
        models_data = data

    free_models: List[str] = []
    caps: Dict[str, Dict[str, Any]] = {}
    for item in models_data:
        if isinstance(item, dict):
            model_id = item.get("id", "") or item.get("name", "")
        elif isinstance(item, str):
            model_id = item
        else:
            continue

        model_id = model_id.strip()
        if not model_id or not model_id.endswith("-free"):
            continue

        free_models.append(model_id)
        # 简单模式：只写基础能力，具体值由 family 兜底（PROVIDER_CAPABILITIES["opencode"]）
        caps[model_id] = {
            "context_limit": 200000,
            "supports_thinking": True,
            "thinking_param": "reasoning_effort",
            "source": "opencode_api",
            "note": f"OpenCode Zen 免费模型（via {url}）",
        }

    if free_models:
        logger.info(f"[models.dev] OpenCode Zen 免费模型: {free_models}")

    return free_models, caps


# ============================================================
# OpenCode 免费模型：按服务商实例异步刷新
# ============================================================
# 与上面 _fetch_opencode_zen_free_models 的区别：
#   - 上面那个用共享 key 拉统一的 OpenCode Zen 端点，目的是补齐"模型能力元数据"；
#   - 下面这个按每个服务商实例各自的 API_URL / API_KEY 去拉，目的是刷新
#     "该实例可用的免费模型列表"（支持 OpenCode Zen #2/#3 等多实例）。
# 网络 + 解析逻辑放 core 层，线程调度与 UI 刷新由调用方（main_widget）负责。
def fetch_opencode_free_models_for_providers(
    targets: List[Tuple[str, str, str]],
    timeout: float = 15.0,
) -> Dict[str, List[str]]:
    """按服务商实例批量拉取 OpenCode 免费模型列表（-free 后缀）。

    参数 targets: [(config_id, api_url, api_key), ...]，每个元素对应一个
        服务商实例（可能多个 OpenCode Zen #2/#3 等）。
    返回: {config_id: [free_model_names]}，只含成功获取且非空的实例。

    同步执行（配合 threading 调用，不阻塞主线程）。单个实例失败不影响其他。
    """
    from app.constants import OPENCODE_SHARED_API_KEY

    results: Dict[str, List[str]] = {}
    try:
        import httpx
    except ImportError:
        logger.warning("[models.dev] 未安装 httpx，跳过实例级免费模型刷新")
        return results

    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        for cid, base_url, key in targets:
            if not base_url:
                continue
            if not key:
                key = OPENCODE_SHARED_API_KEY
            try:
                url = f"{base_url.rstrip('/')}/models"
                headers = {"Authorization": f"Bearer {key}"}
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except Exception as e:
                logger.debug(f"[OpenCode] 实例 {cid[:12]}... 获取免费模型失败: {e}")
                continue

            # 解析 OpenAI-compatible 响应
            raw_ids: List[str] = []
            if isinstance(data, dict):
                if "data" in data:
                    raw_ids = [
                        m.get("id", "") or m.get("name", "")
                        for m in data["data"]
                        if isinstance(m, dict)
                    ]
                elif "models" in data:
                    raw_ids = [
                        m.get("id", "") or m.get("name", "")
                        for m in data["models"]
                        if isinstance(m, dict)
                    ]
            elif isinstance(data, list):
                raw_ids = [m if isinstance(m, str) else "" for m in data]

            free_models = [m.strip() for m in raw_ids if m.strip().endswith("-free")]
            if free_models:
                results[cid] = free_models
                logger.info(f"[OpenCode] 实例 {cid[:12]}... 免费模型: {free_models}")

    return results


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
    """加载 models.dev 动态模型配置（含 OpenCode Zen 免费模型同步）。

    逻辑：
      1. 若 force=True 或缓存不存在/过期，尝试远程拉取。
      2. 远程拉取成功：解析 models.dev 数据，再叠加 OpenCode Zen 免费模型，
         合入缓存后返回。
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

            # ── 叠加 OpenCode Zen 免费模型 ──
            opencode_free_models, opencode_free_caps = _fetch_opencode_zen_free_models()
            if opencode_free_models:
                if "OpenCode Zen" not in provider_models:
                    provider_models["OpenCode Zen"] = []
                seen = {m.strip().lower() for m in provider_models["OpenCode Zen"]}
                for model in opencode_free_models:
                    key = model.strip().lower()
                    if key and key not in seen:
                        provider_models["OpenCode Zen"].append(model)
                        seen.add(key)
                model_capabilities.update(opencode_free_caps)

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
