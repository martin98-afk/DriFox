# -*- coding: utf-8 -*-
"""inject_assistant.py — assistant_hub hooks（身份替换注入 + 轮次计数）

两个 hook：
1. BuildSystemPrompt（``hook``）：激活助手时**直接输出**助手信息块
   （人格段 persona → 记忆使用规则 → 置顶 → memory.md），
   并把 context 里预取的智能体提示词置空防重复。经验不注入 prompt
   （recall_experience 无参返回索引 = 渐进式披露）。
2. Stop（``on_stop``）：主对话每轮结束计数，交给 MemoryTicker 驱动
   记忆传送带（每 10 轮轻量链）。

实现说明：
- HookWorker 经 spec_from_file_location 独立加载本文件（无 package 上下文），
  禁止相对导入；assistant_manager 模块按路径加载并缓存 sys.modules。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict

from loguru import logger

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MANAGER_MODULE_NAME = "assistant_hub_manager"


def _ensure_manager_module():
    """按文件路径加载 assistant_manager.py（进程内单例语义一致）。

    mtime 自检：manager 模块名固定（ui+hooks 共享），不在主程序 UI 热重载
    清理前缀内；文件更新后此处重新 exec 替换，与 ui 侧同款自愈逻辑。
    """
    source = _PLUGIN_ROOT / "assistant_manager.py"
    try:
        mtime = source.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime:
        return mod
    spec = importlib.util.spec_from_file_location(
        _MANAGER_MODULE_NAME,
        str(source),
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MANAGER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        module._source_mtime = mtime
    except Exception as e:
        logger.error(f"[assistant_hub.hooks] 加载 assistant_manager 失败: {e}")
        return None
    return module


def _get_manager():
    module = _ensure_manager_module()
    if module is None:
        return None
    return module.AssistantManager.get_instance()


def _get_ticker(mgr):
    """取 MemoryTicker 单例（失败返回 None：ticker 不可用只影响传送带，不影响注入）。"""
    try:
        module = _ensure_manager_module()
        if module is None:
            return None
        return module.AssistantManager.get_instance().__class__ and _ticker_instance(mgr)
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] ticker 不可用: {e}")
        return None


def _ticker_instance(mgr):
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is None:
        return None
    return mod._load_core_module("memory.ticker", "memory/ticker.py").MemoryTicker.get(mgr)


def _assistant_prompt_block(aid: str) -> str:
    """组装助手信息块（人格段→人工提示→记忆段→技能段）。

    实现内聚在 manager.prompt_block（hooks 与 UI 欢迎卡统计共用，单一数据源）。
    """
    mgr = _get_manager()
    if mgr is None:
        return ""
    return mgr.prompt_block(aid)


def hook(event: str, context: Dict[str, Any]) -> str:
    """BuildSystemPrompt hook：激活助手时直接输出助手信息块。

    会话级临时助手（@提及）优先于全局 active_id：同一进程多会话
    各自看到各自的助手身份，互不影响。
    """
    if (context or {}).get("current_role") != "primary":
        return ""
    try:
        mgr = _get_manager()
        if mgr is None:
            return ""
        sid = str((context or {}).get("session_id") or "")
        aid = mgr.get_session_override(sid) if sid else ""
        if not aid:
            aid = mgr.active_id()
        if not aid or not mgr.has(aid):
            return ""
        block = _assistant_prompt_block(aid)
        if not block:
            return ""
        context["agent_identity_content"] = ""
        return block
    except Exception as e:
        logger.warning(f"[assistant_hub.hooks] BuildSystemPrompt 处理失败: {e}")
        return ""


# @提及助手检测：@ 后到下一个空白之间的 token（支持中文名）
_AT_MENTION_RE = re.compile(r"(?:^|\s)@([^\s@]+)")


def _match_assistant_by_name(mgr, token: str) -> str:
    """按名字/id 匹配助手，返回 aid；无匹配返回空串。"""
    if not token:
        return ""
    token_lower = token.lower()
    for a in mgr.list_assistants_sorted_by_stable():
        if a.name == token or a.id == token_lower:
            return a.id
    return ""


def on_pre_user(event: str, context: Dict[str, Any]) -> str:
    """PreUserMessage hook：检测消息中的 @助手名 → 设置会话级临时助手。

    恒返回空串（不向对话注入任何内容）。命中的变化是：
    session override 更新 + 该会话 system_prompt 缓存失效，
    使本轮 build_messages 即用新助手身份。
    """
    try:
        if (context or {}).get("current_role") != "primary":
            return ""
        message = str((context or {}).get("message") or "")
        sid = str((context or {}).get("session_id") or "")
        if not message or not sid:
            return ""
        # 只看第一个 @token（多个时取最先出现的）
        m = _AT_MENTION_RE.search(message)
        if not m:
            return ""
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = _match_assistant_by_name(mgr, m.group(1))
        if not aid:
            return ""
        if mgr.set_session_override(sid, aid):
            mgr._invalidate_session_prompt(sid)
            a = mgr.get(aid)
            logger.info(f"[assistant_hub] 会话 {sid[:8]} 临时切换助手: {a.name if a else aid}")
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] @提及检测失败: {e}")
    return ""


def on_stop(event: str, context: Dict[str, Any]) -> str:
    """Stop hook：主对话每轮结束 → MemoryTicker 计数（驱动记忆传送带）。

    归属：会话级临时助手优先（override），否则主助手；每轮把
    sid→aid 写入归属映射，供记忆传送带过滤素材，保证各助手
    记忆互相独立。

    恒返回空串（不向对话注入任何内容）。
    """
    try:
        if (context or {}).get("current_role") != "primary":
            return ""
        mgr = _get_manager()
        if mgr is None:
            return ""
        sid = str((context or {}).get("session_id") or "")
        aid = mgr.get_session_override(sid) if sid else ""
        if not aid:
            aid = mgr.active_id()
        if not aid or not mgr.has(aid):
            return ""
        if sid:
            mgr.record_session_aid(sid, aid)
        ticker = _get_ticker(mgr)
        if ticker is not None:
            ticker.on_turn_finished(aid)
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] Stop 计数失败: {e}")
    return ""
