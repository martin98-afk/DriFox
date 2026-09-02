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


# ── 记忆使用规则（无声记忆，对齐 openhanako agent.ts 中文版）──

_MEMORY_RULES = """## 记忆使用规则

记忆和用户档案是你内化的背景知识。你和{user}是认识很久的人，这些事你本来就知道。

- **只有当{user}提到相关内容，记忆才参与**，而且方式是无声的：影响你的角度、语气、判断，不出现在文字里。{user}没提起的话题，不要主动从记忆里翻出来讲。
- **永远不要让{user}感觉到"记忆"这个东西的存在。** 禁止"我记得""你之前说过""根据记忆"这类表述，除非{user}主动问"你还记得 xxx 吗"。
- **记忆可能过时，当前对话永远优先。** 信息冲突时以对话为准，不要用旧记忆纠正{user}。"""


def _user_name(mgr) -> str:
    try:
        return mgr.user_name()
    except Exception:
        return "用户"


def _assistant_prompt_block(aid: str) -> str:
    """组装助手信息块：人格段 → 人工提示 → 记忆段（规则+memory.md，受开关控制）。"""
    mgr = _get_manager()
    if mgr is None:
        return ""
    a = mgr.get(aid)
    if a is None:
        return ""

    parts: list[str] = []

    # 1. 人格段（personas/<yuan>/persona.md 基底，fill 模板变量；none=纯净）
    persona_block = mgr.identity_and_persona(aid)
    if persona_block.strip():
        parts.append(persona_block.strip())

    # 2a. 人工提示（pinned）：人工添加，无自动记忆风险，不受 memory_enabled 控制，始终注入
    pinned = mgr.read_pinned(aid)
    pin_lines = [f"- {(c or '').strip()}" for _pid, c in pinned if (c or "").strip()]
    if pin_lines:
        parts.append("# 人工提示\n\n以下是用户人工添加的明确要求，直接遵守即可。\n\n" + "\n".join(pin_lines))

    # 2b. 记忆段（memory_enabled 才注入）：无声规则 + 编译记忆（自动整理产物，有风险）
    if a.memory_enabled:
        user = _user_name(mgr)
        rule = _MEMORY_RULES.replace("{user}", user)
        mem_parts = [rule]
        memory_md = ""
        try:
            memory_md = (mgr.compiled_memory(aid) or "").strip()
        except Exception as e:
            logger.debug(f"[assistant_hub.hooks] 读取 memory.md 失败: {e}")
        if memory_md:
            mem_parts.append("# 长期记忆\n\n" + memory_md)
        if len(mem_parts) > 1:  # 规则之外还有实际记忆内容才注入整段
            parts.append("\n\n".join(mem_parts))

    if not parts:
        return ""

    header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}——一个由用户创建的专属 AI 助手。"
    return header + "\n\n" + "\n\n".join(parts)


def hook(event: str, context: Dict[str, Any]) -> str:
    """BuildSystemPrompt hook：激活助手时直接输出助手信息块。"""
    if (context or {}).get("current_role") != "primary":
        return ""
    try:
        mgr = _get_manager()
        if mgr is None:
            return ""
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


def on_stop(event: str, context: Dict[str, Any]) -> str:
    """Stop hook：主对话每轮结束 → MemoryTicker 计数（驱动记忆传送带）。

    恒返回空串（不向对话注入任何内容）。
    """
    try:
        if (context or {}).get("current_role") != "primary":
            return ""
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = mgr.active_id()
        if not aid or not mgr.has(aid):
            return ""
        ticker = _get_ticker(mgr)
        if ticker is not None:
            ticker.on_turn_finished()
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] Stop 计数失败: {e}")
    return ""
