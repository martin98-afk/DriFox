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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

# ============================================================
# 常量
# ============================================================
MODELS_DEV_API_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 24 * 3600  # 24 小时
# 缓存数据结构版本：字段新增（如 cost）或语义变更（如 supports_thinking
# 改为"有明确 reasoning_options 才为 True"）时 +1，旧版本缓存视为无效触发重拉
CACHE_SCHEMA_VERSION = 3
# 缓存「内容版本」：与 schema 版本（数据结构）分离。
# 当你改了"产出内容"的代码——解析/映射逻辑、服务商白名单、免费模型源、
# 默认值或合并规则等非结构变更——就把本值 +1。任何本地缓存的 _content_version
# 低于此值的，即便仍在 24h TTL 内也视为过期、强制向 models.dev 重新拉取，
# 让用户及时用上新逻辑，而不必等 TTL 自然到期。
CACHE_CONTENT_VERSION = 1


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
# 已迁移至 providers 插件（ProviderDef.models_dev_id），运行时从注册表动态获取：
# 见 _get_models_dev_map()。此处仅保留 reasoning type 映射常量。

# models.dev reasoning_options type -> DriFox thinking_param
REASONING_TYPE_TO_THINKING_PARAM = {
    "effort": "reasoning_effort",
    "toggle": "thinking",
    "budget_tokens": "thinking_budget",
}


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
    """检查缓存结构是否有效（schema 版本匹配且未超过 24h TTL）。

    只管"本地能否安全服务"，不管内容版本。内容版本偏低由刷新决策
    （_is_content_version_stale + load_dynamic_models 的重拉判定）单独处理，
    这样旧缓存仍可临时服务主线程，后台刷新再把它升级到最新内容。
    """
    if not cache:
        return False
    if cache.get("_schema_version") != CACHE_SCHEMA_VERSION:
        # schema 升级（如新增 cost 字段）→ 旧缓存结构失效
        return False
    timestamp = cache.get("_cached_at")
    if not isinstance(timestamp, (int, float)):
        return False
    return (time.time() - timestamp) < CACHE_TTL_SECONDS


def _is_content_version_stale(cache: Optional[Dict[str, Any]]) -> bool:
    """缓存内容版本是否低于当前代码。

    开发者改了"产出内容"的代码（解析/映射/免费模型源/默认值等）→ 把
    CACHE_CONTENT_VERSION +1，所有本地缓存的 _content_version 偏低者即便仍在
    24h TTL 内也判定为"内容过期"，触发后台强制重拉，让用户及时体验最新效果。
    完全缺失 _content_version 的旧缓存（本功能上线前的缓存）同样视为偏低。
    """
    if not cache:
        return False
    return cache.get("_content_version") != CACHE_CONTENT_VERSION


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
    except (ValueError, TypeError):
        return None
    if context_limit <= 0:
        return None

    modalities = model_info.get("modalities") or {}
    input_modalities = modalities.get("input") or []
    supports_vision = "image" in input_modalities

    reasoning = bool(model_info.get("reasoning", False))
    reasoning_options = model_info.get("reasoning_options") or []
    # 思考开关 ≠ 会思考：只有 models.dev 明确给出可控的 reasoning_options
    # （toggle / effort / budget_tokens）才算"支持思考开关"。
    # reasoning=True 但 options 为空 / type 缺失 → 思考不可控或数据未知，
    # 不显示思考开关（保守原则：未知不误报，宁可少显示不可错显示）。
    thinking_param = None
    reasoning_effort_values: Optional[List[str]] = None
    if reasoning and isinstance(reasoning_options, list):
        for opt in reasoning_options:
            if not isinstance(opt, dict):
                continue
            reasoning_type = opt.get("type")
            # 收集 effort 可选值（如 models.dev 的 ["high", "max"]），
            # 供 UI 渲染"思考等级"下拉框选项；首个可映射 type 决定 thinking_param
            if reasoning_type == "effort":
                values = opt.get("values")
                if isinstance(values, list) and values:
                    reasoning_effort_values = [str(v) for v in values]
            if thinking_param is None:
                thinking_param = REASONING_TYPE_TO_THINKING_PARAM.get(reasoning_type or "")

    # 输出上限（可选）
    max_output_tokens = limit.get("output")
    if max_output_tokens is not None:
        try:
            max_output_tokens = int(max_output_tokens)
            if max_output_tokens <= 0:
                max_output_tokens = None
        except (ValueError, TypeError):
            max_output_tokens = None

    # cost（$/M tokens，原样保留不换算；缺失字段为 None）
    cost = model_info.get("cost") or {}
    cost_result: Dict[str, Any] = {
        "input": cost.get("input"),
        "output": cost.get("output"),
        "cache_read": cost.get("cache_read"),
        "cache_write": cost.get("cache_write"),
    }

    result: Dict[str, Any] = {
        "context_limit": context_limit,
        "supports_vision": supports_vision,
        # 支持思考开关 = 有明确可控的 reasoning_options（控制方式未知不算）
        "supports_thinking": thinking_param is not None,
        "source": "models.dev",
        "note": model_info.get("description", ""),
        "release_date": model_info.get("release_date"),
        "cost": cost_result,
    }
    if thinking_param:
        result["thinking_param"] = thinking_param
    if reasoning_effort_values:
        result["reasoning_effort_values"] = reasoning_effort_values
    if max_output_tokens is not None:
        result["max_output_tokens"] = max_output_tokens

    return result


# ============================================================
# OpenCode Zen 免费模型同步
# ============================================================
# OpenCode Zen 的 /v1/models 端点会返回用户账号下可用的模型列表，
# 其中包括带 -free 后缀的免费模型。这里单独拉取并合并到 models.dev 数据中，
# 比仅靠 models.dev 更实时。
def _fetch_opencode_zen_free_models(
    api_key: str = "",
    models_dev_caps: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """从 OpenCode Zen /v1/models 获取带 -free 后缀的免费模型列表。

    对每个 -free 模型，优先从 models_dev_caps 中查找 base 模型（去掉 -free 后缀）
    的能力数据，让 thinking_param / supports_vision / description 与 models.dev
    保持一致；查不到才回退到硬编码默认值。

    返回 (free_model_names, model_capabilities)。
    网络失败或 API 异常时返回空，不影响主流程。
    """
    url = "https://opencode.ai/zen/v1/models"
    # 免 key 匿名调用：空 key 时不发送 Authorization 头
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

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

        # 去掉 -free 后缀查 base 模型在 models.dev 中的能力数据
        base_name = model_id[:-5]  # 去掉末尾 "-free"
        base_caps: Optional[Dict[str, Any]] = None
        if models_dev_caps and base_name:
            base_caps = models_dev_caps.get(base_name)

        # 从 models.dev 的 base 模型继承能力；找不到就不写，让 family 默认值兜底
        if base_caps:
            caps[model_id] = {
                "context_limit": base_caps.get("context_limit", 200000),
                # 继承 base 的思考开关能力；未知默认 False（宁可少显示，不可误显示）
                "supports_thinking": base_caps.get("supports_thinking", False),
                "thinking_param": base_caps.get("thinking_param", "reasoning_effort"),
                "supports_vision": base_caps.get("supports_vision", False),
                "source": "models.dev",
                "note": base_caps.get("note", ""),
            }
            if "thinking_enable_value" in base_caps:
                caps[model_id]["thinking_enable_value"] = base_caps["thinking_enable_value"]
            if base_caps.get("reasoning_effort_values"):
                caps[model_id]["reasoning_effort_values"] = base_caps["reasoning_effort_values"]

    if free_models:
        logger.info(f"[models.dev] OpenCode Zen 免费模型: {free_models}")
        logger.debug(
            f"[models.dev] OpenCode Zen 免费模型能力来源: "
            f"{sum(1 for v in caps.values() if v.get('source') == 'models.dev')} 个来自 models.dev, "
            f"{sum(1 for v in caps.values() if v.get('source') == 'opencode_api')} 个回退硬编码"
        )

    return free_models, caps


# ============================================================
# OpenCode 免费模型：按服务商实例异步刷新
# ============================================================
# 与上面 _fetch_opencode_zen_free_models 的区别：
#   - 上面那个用共享 key 拉统一的 OpenCode Zen 端点，目的是补齐"模型能力元数据"；
#   - 下面这个按每个服务商实例各自的 API_URL / API_KEY 去拉，目的是刷新
#     "该实例可用的免费模型列表"（支持 OpenCode Zen #2/#3 等多实例）。
# 网络 + 解析逻辑放 core 层，线程调度与 UI 刷新由调用方（main_widget）负责。
#
# 防重复刷新：新建多个标签页（每个窗口初始化后都会触发一次异步刷新）时，
# 同一实例会在短时间窗口内被重复拉取。这里做两层去重：
#   1. 模块级时间窗口缓存：同一实例 N 秒内命中缓存直接返回，不发网络请求；
#   2. in-flight 去重：同一实例并发请求只发一个，其余等待同一结果。
# 缓存键 = (config_id, base_url, api_key) 三元组：用户修改实例 API_URL/API_KEY
# 后不再命中旧缓存；仅成功结果才写入缓存，失败可重试。
_OPENCODE_FREE_CACHE_LOCK = threading.Lock()
_OPENCODE_FREE_CACHE: Dict[Tuple[str, str, str], Tuple[float, List[str]]] = {}  # key -> (fetched_at, free_models)
_OPENCODE_FREE_INFLIGHT: Dict[Tuple[str, str, str], threading.Event] = {}  # key -> 请求完成事件
_OPENCODE_FREE_CACHE_TTL = 300.0  # 秒，时间窗口内同一实例只拉一次


def _opencode_free_cache_key(cid: str, base_url: str, key: str) -> Tuple[str, str, str]:
    """构造实例级缓存键：config_id + 实例参数（URL/Key）三元组。"""
    return (cid, base_url, key)


def _cleanup_opencode_free_cache(now: float) -> None:
    """惰性清理过期缓存条目（调用方需持锁）。"""
    expired = [k for k, (ts, _m) in _OPENCODE_FREE_CACHE.items() if (now - ts) >= _OPENCODE_FREE_CACHE_TTL]
    for k in expired:
        _OPENCODE_FREE_CACHE.pop(k, None)


def fetch_opencode_free_models_for_providers(
    targets: List[Tuple[str, str, str]],
    timeout: float = 15.0,
) -> Dict[str, List[str]]:
    """按服务商实例批量拉取 OpenCode 免费模型列表（-free 后缀）。

    参数 targets: [(config_id, api_url, api_key), ...]，每个元素对应一个
        服务商实例（可能多个 OpenCode Zen #2/#3 等）。
    返回: {config_id: [free_model_names]}，只含成功获取且非空的实例。

    同步执行（配合 threading 调用，不阻塞主线程）。单个实例失败不影响其他。
    同一实例在 _OPENCODE_FREE_CACHE_TTL 窗口内只发一次网络请求（缓存 + in-flight 去重）。
    """
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
            # 免 key 匿名调用：空 key 时不发送 Authorization 头

            cache_key = _opencode_free_cache_key(cid, base_url, key)

            # ── 1. 时间窗口缓存：命中直接返回 ──
            with _OPENCODE_FREE_CACHE_LOCK:
                _cleanup_opencode_free_cache(time.time())
                cached = _OPENCODE_FREE_CACHE.get(cache_key)
                if cached and (time.time() - cached[0]) < _OPENCODE_FREE_CACHE_TTL:
                    results[cid] = cached[1]
                    continue

                # ── 2. in-flight 去重：已有请求在途则等待其结果 ──
                inflight = _OPENCODE_FREE_INFLIGHT.get(cache_key)
                if inflight is None:
                    inflight = threading.Event()
                    _OPENCODE_FREE_INFLIGHT[cache_key] = inflight
                    is_owner = True
                else:
                    is_owner = False

            if not is_owner:
                # 有在途请求：等待完成后读缓存（锁外等待，避免阻塞其他实例）
                inflight.wait(timeout=timeout)
                with _OPENCODE_FREE_CACHE_LOCK:
                    cached = _OPENCODE_FREE_CACHE.get(cache_key)
                    if cached and (time.time() - cached[0]) < _OPENCODE_FREE_CACHE_TTL:
                        results[cid] = cached[1]
                continue

            # ── 3. 本实例首次请求：真正发起网络拉取（锁外执行） ──
            free_models: List[str] = []
            try:
                free_models = _fetch_instance_free_models(client, base_url, key)
            finally:
                # 兜底：无论请求成败/异常，必须清理 in-flight 并唤醒等待者，
                # 防止事件残留导致后续请求永久等待（P1-2 防御）。
                with _OPENCODE_FREE_CACHE_LOCK:
                    _OPENCODE_FREE_INFLIGHT.pop(cache_key, None)
                    if free_models:
                        _OPENCODE_FREE_CACHE[cache_key] = (time.time(), free_models)
                        results[cid] = free_models
                        logger.info(f"[OpenCode] 实例 {cid[:12]}... 免费模型: {free_models}")
                    inflight.set()

    return results


def _fetch_instance_free_models(client, base_url: str, key: str) -> List[str]:
    """拉取单个服务商实例的免费模型列表（-free 后缀）。

    网络失败/非 200/无免费模型时返回空列表，不抛异常。
    """
    try:
        url = f"{base_url.rstrip('/')}/models"
        # 免 key 匿名调用：空 key 时不发送 Authorization 头
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        logger.debug(f"[OpenCode] 实例 {base_url[:12]}... 获取免费模型失败: {e}")
        return []

    # 解析 OpenAI-compatible 响应
    raw_ids: List[str] = []
    if isinstance(data, dict):
        if "data" in data:
            raw_ids = [m.get("id", "") or m.get("name", "") for m in data["data"] if isinstance(m, dict)]
        elif "models" in data:
            raw_ids = [m.get("id", "") or m.get("name", "") for m in data["models"] if isinstance(m, dict)]
    elif isinstance(data, list):
        raw_ids = [m if isinstance(m, str) else "" for m in data]

    return [m.strip() for m in raw_ids if m.strip().endswith("-free")]


def _get_models_dev_map() -> Dict[str, str]:
    """运行时从 ProviderRegistry 获取 服务商名 -> models.dev provider id。

    models.dev 白名单由 providers 插件声明（models_dev_id 字段），
    插件卸载/热重载后本映射随之变化（每次调用动态读取）。
    """
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        return ProviderRegistry.get_instance().models_dev_map()
    except Exception:
        return {}


def _parse_models_dev_data(data: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """解析 models.dev 数据，返回 (provider_models, model_capabilities)。

    只处理 providers 插件声明的 models.dev 白名单内的服务商。
    """
    provider_map = _get_models_dev_map()
    provider_models: Dict[str, List[str]] = {name: [] for name in provider_map}
    model_capabilities: Dict[str, Dict[str, Any]] = {}

    for dfox_name, provider_id in provider_map.items():
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
            existing = model_capabilities.get(model_id)
            if existing is None:
                model_capabilities[model_id] = transformed
            else:
                # 同名模型跨 provider 出现多次（如 kimi-k2.5 在 moonshotai / opencode-go）：
                # 取"更支持"的合并结果，防止某 provider 数据不全把能力降级。
                model_capabilities[model_id] = _merge_model_caps(existing, transformed)

    return provider_models, model_capabilities


def _merge_model_caps(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """合并同名模型（跨 provider）的能力数据，取"更支持"的值。

    规则：
    - supports_thinking：两者 OR（任一 True → True）。防止某 provider 的
      reasoning_options 为空（如 opencode-go 的 kimi-k2.5）把"支持思考"
      降为"不支持"。
    - cost：字段级合并——new 非 None 覆盖，None 保留 existing 对应字段。
    - 其余字段：new 有值取 new，否则保留 existing。
    """
    merged = dict(existing)
    for key, value in new.items():
        if key == "supports_thinking":
            merged[key] = bool(existing.get(key)) or bool(value)
        elif key == "cost" and isinstance(value, dict):
            old_cost = merged.get("cost") or {}
            merged[key] = {**old_cost, **{k: v for k, v in value.items() if v is not None}}
        elif value is not None:
            merged[key] = value
    return merged


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
    allow_network: bool = True,
) -> DynamicModelsResult:
    """加载 models.dev 动态模型配置（含 OpenCode Zen 免费模型同步）。

    逻辑：
      1. 重拉判定（need_refresh）：force=True / 缓存缺失或结构失效（schema 不符
         或超 24h TTL）/ 内容版本偏低（CACHE_CONTENT_VERSION 已 +1，开发者更新了
         产出逻辑）。满足其一则尝试远程拉取（allow_network=False 时跳过网络，
         直接走步骤 4/5，保证调用线程零阻塞）。
      2. 远程拉取成功：解析 models.dev 数据，再叠加 OpenCode Zen 免费模型，
         合入缓存后返回。
      3. 远程拉取失败：若缓存存在（即使过期），用缓存；否则返回空结果。
      4. 若 force=False 且缓存结构有效且内容版本匹配：直接读取缓存。
      5. 无网络且缓存无效：返回空结果。

    注意：内容版本偏低只驱动"重拉"，不使本地缓存"不可用"——结构有效的旧缓存
    仍可临时服务主线程，后台刷新再把它升级到最新内容，避免 UI 短暂空白。

    allow_network=False 用于主线程安全读取（只读文件缓存，毫秒级）；
    真正的网络刷新应通过 refresh_dynamic_models_async() 在后台线程执行。
    网络失败不会抛异常，而是回退到缓存/空数据。
    """
    path = cache_path or _get_cache_path()
    cache = _load_cache(path)

    # 重拉判定：force / 结构失效 / 内容版本偏低（开发者更新了产出逻辑）
    need_refresh = force or (not _is_cache_valid(cache)) or _is_content_version_stale(cache)

    if (not allow_network) or need_refresh:
        if allow_network:
            logger.info(f"[models.dev] 尝试同步最新模型元数据... (content_version={CACHE_CONTENT_VERSION})")
            remote_data = _fetch_remote()
            if remote_data is not None:
                provider_models, model_capabilities = _parse_models_dev_data(remote_data)

                # ── 叠加 OpenCode Zen 免费模型 ──
                opencode_free_models, opencode_free_caps = _fetch_opencode_zen_free_models(
                    models_dev_caps=model_capabilities,
                )
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
                    "_schema_version": CACHE_SCHEMA_VERSION,
                    "_content_version": CACHE_CONTENT_VERSION,
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
        logger.info(
            f"[models.dev] 使用本地缓存 (content_version={cache.get('_content_version')}, code={CACHE_CONTENT_VERSION})"
        )
        return DynamicModelsResult(
            provider_models=cache.get("provider_models", {}),
            model_capabilities=cache.get("model_capabilities", {}),
            from_cache=True,
            fetched_at=cache.get("_cached_at"),
        )

    return DynamicModelsResult(provider_models={}, model_capabilities={}, from_cache=False, fetched_at=None)


# 模块级缓存，避免同一次运行中重复加载
# 锁保护：get_dynamic_models（主线程/任意线程）与 refresh_dynamic_models_async
# （后台线程）可能并发读写该缓存。
_CACHE_LOCK = threading.Lock()
_dynamic_models_cache: Optional[DynamicModelsResult] = None


def get_dynamic_models(force: bool = False) -> DynamicModelsResult:
    """带模块级内存缓存的 load_dynamic_models。

    线程安全（UI 主线程可安全调用）。**本函数永不发起网络请求**：
    只读内存缓存/本地文件缓存，缓存无效时返回空结果（调用方回退硬编码），
    不会阻塞。网络刷新由 refresh_dynamic_models_async() 在后台线程完成，
    完成后自动填充本缓存，UI 侧经回调/信号刷新即可看到最新数据。
    """
    global _dynamic_models_cache
    with _CACHE_LOCK:
        if _dynamic_models_cache is None or force:
            _dynamic_models_cache = load_dynamic_models(force=force, allow_network=False)
        return _dynamic_models_cache


# ----------------------------
# 后台异步刷新（单飞去重）
# ----------------------------
_REFRESH_LOCK = threading.Lock()
# in-flight 标记：None=无在途刷新；Event 对象=已有刷新在途（忽略新请求）
_REFRESH_INFLIGHT: Optional[threading.Event] = None


def refresh_dynamic_models_async(
    force: bool = False,
    on_done: Optional[Callable[[Optional[DynamicModelsResult]], None]] = None,
) -> bool:
    """后台线程刷新 models.dev 动态数据，完成后回调。

    - force=False（默认）：本地文件缓存有效则直接复用（无网络请求），
      无效/过期才网络拉取——适合启动预热，冷启动不浪费一次网络。
    - force=True：总是向 models.dev 网络拉取——适合手动"立即刷新"。
    - 单飞去重：已有刷新在途时忽略新请求（多窗口/多触发点安全，只发一路网络）。
    - on_done 在后台线程回调；UI 调用方需自行转回主线程（如 Qt 跨线程信号）。
    - 刷新成功后自动填充 get_dynamic_models 的内存缓存。
    - 网络失败不外抛：按 load_dynamic_models 语义回退到过期缓存/空数据。

    返回 True 表示本次实际启动了刷新线程，False 表示已有在途刷新被忽略。
    """
    global _REFRESH_INFLIGHT
    with _REFRESH_LOCK:
        if _REFRESH_INFLIGHT is not None:
            return False
        _REFRESH_INFLIGHT = threading.Event()

    def _worker() -> None:
        global _dynamic_models_cache, _REFRESH_INFLIGHT
        result: Optional[DynamicModelsResult] = None
        try:
            result = load_dynamic_models(force=force, allow_network=True)
            with _CACHE_LOCK:
                _dynamic_models_cache = result
        except Exception:
            logger.exception("[models.dev] 后台刷新异常")
        finally:
            with _REFRESH_LOCK:
                _REFRESH_INFLIGHT = None
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                logger.exception("[models.dev] 刷新完成回调异常")

    threading.Thread(target=_worker, daemon=True).start()
    return True


def clear_memory_cache() -> None:
    """清空模块级内存缓存，下次调用 get_dynamic_models 时从文件重新加载。"""
    global _dynamic_models_cache
    with _CACHE_LOCK:
        _dynamic_models_cache = None
