# -*- coding: utf-8 -*-
"""
models.dev 模型元数据同步模块。

启动时优先从 DriFox 维护的镜像仓库（models-dev）拉取最新模型列表与能力，
镜像不可用时回退到 https://models.dev/api.json 官方源。
按 DriFox 支持的服务商白名单解析，并转换为本地模型能力格式。
拉取结果缓存到 config/models_dev_cache.json，TTL 24 小时。

本模块只负责"拉取 + 缓存 + 转换"，合并到硬编码配置的逻辑在调用方：
  - app/constants.py 的 get_merged_provider_models()
  - app/core/model_capabilities.py 的 get_merged_model_capabilities()
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

# ============================================================
# 常量
# ============================================================
# 优先源：DriFox 镜像仓库（过滤版，字段与官方 api.json 兼容：
# 剔除 id/provider 等冗余字段，description/cost/limit/modalities/reasoning 全保留）
MODELS_DEV_MIRROR_URL = "https://raw.githubusercontent.com/martin98-afk/models-dev/master/models-dev.json"
# 保底源：models.dev 官方 API
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
CACHE_CONTENT_VERSION = 4  # +1：修复启动早期空解析覆盖缓存的事故，已被写空的缓存需强制重拉


def _default_cache_path() -> Path:
    """返回默认缓存文件路径。

    缓存放在 get_app_data_dir()/cache/ 下，与运行时数据一起，不纳入 git。
    这里延迟导入 app.utils.utils，避免模块顶层循环依赖。
    """
    try:
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "cache" / "models_dev_cache.json"
    except Exception:
        # 兜底：项目根目录下的 .drifox6/cache/
        return Path(__file__).resolve().parent.parent.parent / ".drifox6" / "cache" / "models_dev_cache.json"


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
def _fetch_url(url: str, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """从单个 URL 拉取 JSON 数据。失败返回 None。"""
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
            logger.warning(f"[models.dev] 返回数据格式异常: {url}")
            return None
        return data
    except Exception as e:
        logger.warning(f"[models.dev] 拉取失败: {e}")
        return None


def _fetch_remote(timeout: float = 30.0) -> Tuple[Optional[Dict[str, Any]], str]:
    """多源顺序拉取 models.dev 数据：镜像仓库优先，官方源保底。

    返回 (data, 实际来源 URL)。全部源失败返回 (None, 最后尝试的 URL)。
    """
    sources = [
        (MODELS_DEV_MIRROR_URL, "镜像仓库"),
        (MODELS_DEV_API_URL, "官方源"),
    ]
    last_url = MODELS_DEV_API_URL
    for url, name in sources:
        data = _fetch_url(url, timeout)
        if data is not None:
            logger.info(f"[models.dev] 从{name}拉取成功: {url}")
            return data, url
        logger.warning(f"[models.dev] {name}拉取失败，尝试下一个源: {url}")
        last_url = url
    return None, last_url


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
            # 供 UI 渲染"思考等级"下拉框选项
            if reasoning_type == "effort":
                values = opt.get("values")
                if isinstance(values, list) and values:
                    reasoning_effort_values = [str(v) for v in values]
            if thinking_param is None:
                thinking_param = REASONING_TYPE_TO_THINKING_PARAM.get(reasoning_type or "")
        # effort 型优先：部分模型（如 deepseek-v4）的 reasoning_options 同时列出
        # toggle 与 effort（toggle 在前）。effort 是可调的细粒度控制且 values 已收集，
        # 统一走 reasoning_effort，避免 thinking_param="thinking" 与
        # reasoning_effort_values 并存导致 UI 不显示思考等级/强度、请求只发
        # thinking 布尔而强度不生效的自相矛盾。
        if reasoning_effort_values:
            thinking_param = "reasoning_effort"

    # 输出上限（可选）
    max_output_tokens = limit.get("output")
    if max_output_tokens is not None:
        try:
            max_output_tokens = int(max_output_tokens)
            if max_output_tokens <= 0:
                max_output_tokens = None
        except ValueError, TypeError:
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
# 服务商名 → models.dev provider id 映射
# ============================================================
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


def _parse_models_dev_data(
    data: Dict[str, Any],
) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Dict[str, Any]]]]:
    """解析 models.dev 数据，返回 (provider_models, model_capabilities, model_capabilities_partitioned)。

    只处理 providers 插件声明的 models.dev 白名单内的服务商。

    ``model_capabilities_partitioned`` 是 **provider 维度的嵌套索引**
    （provider_id → model_id → caps，**不合并**）：同一模型在不同 provider 下
    能力可能不同（同名模型跨服务商串味是已知缺陷），精确查必须先分区。
    旧的扁平 ``model_capabilities`` 保留不动（向后兼容 + 未提供 provider 时兜底）。
    """
    provider_map = _get_models_dev_map()
    provider_models: Dict[str, List[str]] = {name: [] for name in provider_map}
    model_capabilities: Dict[str, Dict[str, Any]] = {}
    model_capabilities_partitioned: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for dfox_name, provider_id in provider_map.items():
        provider_info = data.get(provider_id)
        if not isinstance(provider_info, dict):
            continue
        models = provider_info.get("models") or {}
        if not isinstance(models, dict):
            continue

        partition = model_capabilities_partitioned.setdefault(provider_id, {})
        for model_id, model_info in models.items():
            transformed = _transform_model(provider_id, model_id, model_info)
            if transformed is None:
                continue
            provider_models[dfox_name].append(model_id)
            # 分区索引：同 provider 内同名模型后到者覆盖（同一 provider 内不该重复）
            partition[model_id] = transformed
            existing = model_capabilities.get(model_id)
            if existing is None:
                model_capabilities[model_id] = transformed
            else:
                # 同名模型跨 provider 出现多次（如 kimi-k2.5 在 moonshotai / opencode-go）：
                # 取"更支持"的合并结果，防止某 provider 数据不全把能力降级。
                model_capabilities[model_id] = _merge_model_caps(existing, transformed)

    return provider_models, model_capabilities, model_capabilities_partitioned


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
    # provider 维度分区索引（provider_id → model_id → caps，不合并）；
    # 老缓存缺该键时为 {}（调用方降级到扁平索引）
    model_capabilities_partitioned: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
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
            remote_data, fetched_url = _fetch_remote()
            if remote_data is not None:
                provider_models, model_capabilities, model_capabilities_partitioned = _parse_models_dev_data(
                    remote_data
                )

                if not provider_models:
                    # 解析产出为空（如 providers 插件未就绪导致白名单映射为空）→
                    # 视为异常刷新，不覆盖缓存：空结果一旦写入，24h TTL 内"结构合法
                    # 内容全空"，费用/动态模型全部消失且重启无效。保留旧缓存更安全。
                    logger.warning("[models.dev] 远程拉取成功但解析结果为空（服务商白名单未就绪？），保留现有缓存")
                else:
                    cache = {
                        "_cached_at": time.time(),
                        "_url": fetched_url,
                        "_schema_version": CACHE_SCHEMA_VERSION,
                        "_content_version": CACHE_CONTENT_VERSION,
                        "provider_models": provider_models,
                        "model_capabilities": model_capabilities,
                        "model_capabilities_partitioned": model_capabilities_partitioned,
                    }
                    _save_cache(cache, path)
                    return DynamicModelsResult(
                        provider_models=provider_models,
                        model_capabilities=model_capabilities,
                        model_capabilities_partitioned=model_capabilities_partitioned,
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
            # 老缓存无分区键 → get 默认空 dict，调用方降级到扁平索引
            model_capabilities_partitioned=cache.get("model_capabilities_partitioned", {}),
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
