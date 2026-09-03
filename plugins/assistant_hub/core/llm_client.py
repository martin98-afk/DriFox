# -*- coding: utf-8 -*-
"""llm_client.py — 助手记忆/Dream/经验的一次性 LLM 调用（走主对话引擎 + 单回合循环策略）。

历史（已废弃）
==============
旧实现是 urllib 直连 OpenAI 兼容端点：自己拼 base_url/Authorization、自己解析
choices[0].message.content。这条路与主对话引擎**完全脱钩** —— 模型参数
（思考模式/温度/最大 Token）、provider 适配（ Responses API / thinking 参数 /
认证方式）、重试与流式容错、消息序列化格式全部要另维护一份，结果就是
"助手里的大模型功能完全用不了"（空响应、参数不兼容、reasoning 额度被思考段吃光）。

现实现
======
经 ``services["create_engine_session"]`` 驱动主对话引擎（与 cron-tasks 同款 EP3
原语），把模型配置、provider 适配、序列化、重试全部交回引擎；本插件只负责
**把引擎钳制成单回合**，靠自定义循环策略 ``assistant_hub_single_turn``
（见 ``plugins/assistant_hub/loop_policies/single_turn.py``，max_rounds=1 +
should_continue 恒 STOP）。

关键约定
========
- 同步阻塞：只允许在后台线程调用（ticker 的 daemon 线程 / Dream 的 QThread）。
- 循环策略经 ``loop_policy_id=`` **按 id 直接取对象**，不调用
  ``LoopPolicyRegistry.set_active`` → 不影响主对话的全局激活槽。
- hook_policy=none / permission_strategy=auto_deny / tools=[] —— 记忆整理是纯
  文本进出，不触发全局 hooks，不给任何工具。
- 会话按「模型覆盖配置」分池复用（槽位 busy 时自动新建），避免每轮重建
  ConversationCore；跨线程复用安全（会话本身无 QObject，worker QThread 在
  execute() 内随调用线程创建）。
- 任何失败抛 ``LLMUnavailableError``，由调用方（ticker/dream/experience）静默降级。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from loguru import logger

# ── 常量 ──────────────────────────────────────────────────────────

ENGINE_NAME = "assistant-hub"

# 单回合循环策略 id（与 plugins/assistant_hub/loop_policies/single_turn.py 的
# LOOP_POLICY_ID 必须一致；二者以字面量各自声明，避免插件包导入约束，
# 一致性由 tests/plugins/assistant_hub/test_llm_client.py 守卫。
SINGLE_TURN_LOOP_POLICY_ID = "assistant_hub_single_turn"

DEFAULT_TIMEOUT_SECONDS = 240  # 单回合上限（reasoning 模型整理长记忆需留足）
UTILITY_TEMPERATURE = 0.3  # 记忆整理要稳定复述，不要发散
DEFAULT_RETRIES = 1  # 空响应/瞬时错误重试次数


class LLMUnavailableError(RuntimeError):
    """无可用对话引擎 / 引擎返回错误 / 超时 / 空响应。"""


def _strip_think(text: str) -> str:
    """剥掉 <think>...</think> 思考块，只留正文。

    与 core/session_store.py 同语义（两处独立加载解耦，各自持有一份）。
    思考模型（R1/Qwen3/GLM 等）经引擎会把 reasoning_content 内联成
    <think> 块混进正文，记忆整理入库前必须清掉。
    """
    if "<think>" not in text:
        return text
    out, rest = [], text
    while True:
        head, sep, rest = rest.partition("<think>")
        out.append(head)
        if not sep:
            break
        _, sep2, rest = rest.partition("</think>")
        if not sep2:  # 未闭合：其后内容全部按推理丢弃
            break
    return "".join(out)


# ── services 解析（create_engine_session / get_model_config）──────


def _resolve_services() -> Dict[str, Any]:
    """取主窗口 services。

    三级兜底，保证后台线程（无卡片 context）也能拿到引擎：
      1. ``set_services()`` 注入的缓存（卡片拿到 context 时推入）
      2. UIPluginRegistry 活跃窗口 context 的 ``services``
      3. 直接找 main_widget 现算 ``_build_ui_services()``
    """
    with _services_lock:
        if callable(_services.get("create_engine_session")):
            return _services

    reg = None
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception as e:
        logger.debug(f"[assistant_hub.llm] UIPluginRegistry 不可用: {e}")
        return {}

    # 2) 活跃窗口 context（含 services 全量）
    provider = None
    try:
        provider = reg._resolve_active_window_provider()
    except Exception:
        provider = None
    if provider is None:
        provider = getattr(reg, "_context_provider", None)
    if callable(provider):
        try:
            services = (provider() or {}).get("services")
            if isinstance(services, dict) and callable(services.get("create_engine_session")):
                set_services(services)
                return services
        except Exception as e:
            logger.debug(f"[assistant_hub.llm] context provider 取 services 失败: {e}")

    # 3) 兜底：main_widget 现算
    mw = getattr(reg, "_main_widget", None)
    if mw is None:
        try:
            mw = next(iter(reg._window_main_widgets.values()), None)
        except Exception:
            mw = None
    if mw is not None:
        try:
            services = mw._build_ui_services()
            if isinstance(services, dict) and callable(services.get("create_engine_session")):
                set_services(services)
                return services
        except Exception as e:
            logger.debug(f"[assistant_hub.llm] main_widget 现算 services 失败: {e}")

    return {}


def set_services(services: Optional[Dict[str, Any]]) -> None:
    """注入主窗口 services（含 create_engine_session 才生效）。"""
    if not isinstance(services, dict) or not callable(services.get("create_engine_session")):
        return
    with _services_lock:
        _services.clear()
        _services.update(services)


_services_lock = threading.RLock()
_services: Dict[str, Any] = {}


# ── 模型覆盖参数（工具型调用：关思考 + 低温度）─────────────────────


def _current_model_name() -> str:
    """当前全局选中的模型名（用于判断是否需要显式关思考）。"""
    get_cfg = _resolve_services().get("get_model_config")
    if not callable(get_cfg):
        return ""
    try:
        return str((get_cfg() or {}).get("模型名称") or "")
    except Exception:
        return ""


def _supports_thinking(model: str) -> bool:
    """模型是否支持思考（不支持时下发 thinking 参数会被部分网关拒绝）。"""
    if not model:
        return False
    try:
        from app.core.model_capabilities import get_model_capabilities

        return bool((get_model_capabilities(model) or {}).get("supports_thinking", False))
    except Exception:
        return False


def build_utility_override(
    model_config: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """生成引擎的模型配置覆盖（在窗口当前配置之上叠加）。

    - 温度：显式传参优先，否则 0.3（记忆整理要稳定，不要发散）
    - 思考：仅对**支持思考**的模型显式关闭。不支持思考的模型（含多数自建
      OpenAI 兼容网关）下发 ``thinking`` 参数会直接 400，故不能无条件关。
    """
    ov: Dict[str, Any] = dict(model_config) if isinstance(model_config, dict) else {}
    if temperature is not None:
        ov["温度"] = temperature
    else:
        ov.setdefault("温度", UTILITY_TEMPERATURE)
    model = str(ov.get("模型名称") or "") or _current_model_name()
    if _supports_thinking(model):
        ov["思考模式"] = False
    return ov


# ── 会话池（按覆盖配置分池；busy 槽自动新建）───────────────────────


class _SessionSlot:
    """一个引擎会话及其占用标记"""

    __slots__ = ("session", "busy")

    def __init__(self, session: Any):
        self.session = session
        self.busy = False


_pool_lock = threading.RLock()
_pool: Dict[str, List[_SessionSlot]] = {}


def _config_key(override: Optional[Dict[str, Any]]) -> str:
    if not override:
        return "global"
    try:
        return json.dumps(override, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(sorted(override.items(), key=lambda kv: str(kv[0])))


def _acquire_slot(override: Optional[Dict[str, Any]]) -> _SessionSlot:
    """取一个空闲会话槽；全忙或首次则新建。"""
    key = _config_key(override)
    with _pool_lock:
        slots = _pool.setdefault(key, [])
        for slot in slots:
            if not slot.busy:
                slot.busy = True
                return slot

        create = _resolve_services().get("create_engine_session")
        if not callable(create):
            raise LLMUnavailableError("主程序未提供 create_engine_session 服务（对话引擎不可用）")
        session = create(
            ENGINE_NAME,
            model_config_override=override or None,
            loop_policy_id=SINGLE_TURN_LOOP_POLICY_ID,
            hook_policy="none",
            permission_strategy="auto_deny",
        )
        slot = _SessionSlot(session)
        slot.busy = True
        slots.append(slot)
        return slot


def _release_slot(slot: _SessionSlot) -> None:
    slot.busy = False


def reset_sessions() -> None:
    """丢弃全部缓存会话（模型切换 / 插件热重载 / 超时出错后自愈）。"""
    with _pool_lock:
        slots = [s for lst in _pool.values() for s in lst]
        _pool.clear()
    for slot in slots:
        try:
            slot.session.cleanup()
        except Exception as e:
            logger.debug(f"[assistant_hub.llm] 会话清理失败（忽略）: {e}")


# ── 对外主入口 ────────────────────────────────────────────────────


def chat_once(
    messages: List[Dict[str, str]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    temperature: Optional[float] = None,
    model_config: Optional[Dict[str, Any]] = None,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """单回合补全：返回助手文本；失败抛 LLMUnavailableError。

    Args:
        messages: OpenAI 格式消息列表（system/user）
        timeout: 等待上限秒数
        temperature: 覆盖温度（None = 用 UTILITY_TEMPERATURE）
        model_config: 完整模型配置覆盖（含 API_URL/模型名称，cron-tasks 同款
            model_config_override 模式），None = 跟随窗口当前模型
        retries: 空响应/瞬时错误的重试次数
    """
    if not messages:
        raise LLMUnavailableError("chat_once 需要非空 messages")

    override = build_utility_override(model_config, temperature)
    reason = ""
    for attempt in range(retries + 1):
        slot = _acquire_slot(override)
        fatal = False
        try:
            result = slot.session.turn(messages=list(messages), tools=[], timeout=timeout)
        except LLMUnavailableError:
            raise
        except Exception as e:
            # 会话可能处于中间态（worker 残留），整体丢弃后自愈重来
            reset_sessions()
            raise LLMUnavailableError(f"对话引擎调用失败: {type(e).__name__}: {e}") from e
        finally:
            _release_slot(slot)

        if getattr(result, "timed_out", False):
            reason = f"引擎超时（>{timeout}s）"
            fatal = True  # 卡死会话直接丢弃，避免下轮复用
        elif getattr(result, "cancelled", False):
            reason = "引擎调用被取消"
        elif getattr(result, "error", None):
            reason = str(result.error)
            # 引擎报错多半伴随残留状态（worker 未启动/上轮未收尾），
            # 丢弃会话池重建后再试，比复用可能已污染的会话更稳。
            fatal = True
        else:
            text = (getattr(result, "text", "") or "").strip()
            if text:
                # 思考模型正文可能内联 <think> 块，剥掉再入库；剥后为空按空响应重试
                cleaned = _strip_think(text).strip()
                if cleaned:
                    return cleaned
                reason = "引擎返回内容仅含思考块"
            else:
                reason = "引擎返回空内容"

        if fatal:
            reset_sessions()
        if attempt < retries:
            logger.warning(f"[assistant_hub.llm] {reason}，重试 {attempt + 1}/{retries}")
    raise LLMUnavailableError(reason or "LLM 返回空内容")
