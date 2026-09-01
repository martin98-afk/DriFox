# -*- coding: utf-8 -*-
"""assistant_hub hooks — 系统提示词注入（助手替换智能体身份）

核心设计（用户澄清后定稿）：
- **助手 ≠ 智能体**。助手是比智能体高一个维度的抽象，不注册到 AgentManager，
  不参与 subagent 调用。原 build 智能体定义保持不变、仍可被子智能体调用。
- **替换注入**：当某个助手被激活（``AssistantManager.active_id()``）时，
  ``BuildSystemPrompt`` 事件中原先注入的**智能体提示词**
  （``context["agent_identity_content"]``）被替换为**助手信息**
  （identity.md + AGENTS.md + 置顶记忆 + 当下记忆 + 长期记忆 + 专属技能）。
- 本插件自带 hook（hooks/hooks.json + 本文件），不改动系统 hooks.json。

实现说明：
- HookWorker 通过 ``spec_from_file_location`` 独立加载本文件（无 package
  上下文），因此**不能使用相对导入**。assistant_manager 模块改用
  importlib 按文件路径加载，并缓存在 sys.modules（进程内单例语义一致）。
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
    """按文件路径加载 assistant_manager.py（避免相对导入在 hook 独立加载时失败）。

    模块名固定为 ``assistant_hub_manager`` 并缓存到 sys.modules：
    多次加载共享同一份类定义，AssistantManager 类级单例语义保持一致。
    """
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        _MANAGER_MODULE_NAME,
        str(_PLUGIN_ROOT / "assistant_manager.py"),
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MANAGER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        logger.error(f"[assistant_hub.hooks] 加载 assistant_manager 失败: {e}")
        return None
    return module


def _get_manager():
    module = _ensure_manager_module()
    if module is None:
        return None
    return module.AssistantManager.get_instance()


def _assistant_prompt_block(aid: str) -> str:
    """组装助手信息块（身份 + 人格 + 记忆 + 技能），供替换注入。

    顺序参考 OpenHanako agent.ts 的 system prompt 组装：
    identity（身份）→ AGENTS（人格/行为准则）→ 记忆（置顶/当下/长期）→ 技能。
    """
    mgr = _get_manager()
    if mgr is None:
        return ""
    a = mgr.get(aid)
    if a is None:
        return ""

    parts: list[str] = []

    # 1. 身份简介（identity.md，缺失回落模板）
    identity, _from_template = mgr.read_identity_source(aid)
    if identity.strip():
        parts.append(identity.strip())

    # 2. 人格/行为准则（AGENTS.md，缺失回落模板）
    agents_md, _ = mgr.read_agents_md_source(aid)
    if agents_md.strip():
        parts.append(agents_md.strip())

    # 3. 记忆（仅当 memory_enabled）
    if a.memory_enabled:
        ctx = mgr.get_context(aid)
        ctx_block = ctx.to_prompt_block() if ctx else ""
        if ctx_block:
            parts.append(ctx_block)

    # 4. 专属技能（skills/*.md）
    skills = mgr.list_skills(aid)
    if skills:
        skill_parts = ["## 专属技能", ""]
        for sk in skills:
            content = mgr.read_skill(aid, sk["name"])
            if content.strip():
                skill_parts.append(f"### {sk['name']}\n{content.strip()}")
        parts.append("\n".join(skill_parts))

    if not parts:
        return ""

    # 头部声明助手身份（比"当前智能体身份"更精确的语义）
    header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}——一个由用户创建的专属 AI 助手。"
    return header + "\n\n" + "\n\n".join(parts)


def hook(event: str, context: Dict[str, Any]) -> str:
    """BuildSystemPrompt hook：激活助手时替换智能体身份注入。

    Args:
        event: 事件名（BuildSystemPrompt）
        context: 由 get_agent_system_prompt() 预取的上下文，含
            - agent_name / current_role（primary|subagent）
            - agent_identity_content（原智能体提示词，可被本 hook 替换）

    Returns:
        空字符串（修改 context 由后续 inject_agent_identity hook 输出）。
    """
    # 仅主智能体会话生效（子智能体保留系统定义，避免嵌套污染）
    if (context or {}).get("current_role") != "primary":
        return ""

    try:
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = mgr.active_id()
        if not aid:
            return ""

        block = _assistant_prompt_block(aid)
        if not block:
            return ""

        # ★ 替换注入：覆盖 context 中的智能体提示词，使系统 inject_agent_identity
        # hook 输出助手信息而非 build 等原始智能体定义。
        context["agent_identity_content"] = block
    except Exception as e:
        logger.warning(f"[assistant_hub.hooks] BuildSystemPrompt 处理失败: {e}")
    return ""
