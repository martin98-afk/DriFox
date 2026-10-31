# -*- coding: utf-8 -*-
"""inject_assistant.py — assistant_hub hooks（身份+记忆注入 + @提及）

Hook 清单（hooks.json 对应）：
1. BuildSystemPrompt（``hook``）：激活助手时输出 助手身份块 → 记忆体系块
   （记忆说明书 + 全局记忆 + 项目记忆 + 日志索引，全部进系统提示词，
   照抄 WorkBuddy 注入结构）；并把 context 里预取的智能体提示词置空防重复。
2. SessionStart（``on_session_start``）：注入人工提示（pinned）到会话消息。
   记忆内容不进会话消息（单一通道：系统提示词，写后失效缓存即可刷新）。
3. PreUserMessage（``on_pre_user``）：@提及检测，切会话级临时助手。
4. Stop（``on_stop``）：记录会话归属映射（供工具按会话解析记忆归属）。

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


def _resolve_aid(mgr, context: Dict[str, Any]) -> str:
    """会话生效助手：会话级临时助手（@提及）优先，否则主助手；无则空串。"""
    sid = str((context or {}).get("session_id") or "")
    aid = mgr.get_session_override(sid) if sid else ""
    if not aid:
        aid = mgr.active_id()
    return aid if aid and mgr.has(aid) else ""


def hook(event: str, context: Dict[str, Any]) -> str:
    """BuildSystemPrompt hook：身份块 + 记忆体系块（均进系统提示词）。

    记忆单一通道：说明书 + <global_memory> + <project_memory> + <daily_logs>
    全在系统提示词里；写回后由 memory_write 失效 prompt 缓存，下轮重建。
    会话级临时助手（@提及）优先于全局 active_id。
    """
    if (context or {}).get("current_role") != "primary":
        return ""
    try:
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = _resolve_aid(mgr, context or {})
        if not aid:
            return ""
        parts = [f"<assistant_identity>\n{mgr.identity_block(aid)}\n</assistant_identity>"]
        workdir = str((context or {}).get("project_root") or "")
        parts.append(mgr.memory_system_block(aid, workdir))
        block = "\n\n".join(p for p in parts if p)
        if not block:
            return ""
        context["agent_identity_content"] = ""
        return block
    except Exception as e:
        logger.warning(f"[assistant_hub.hooks] BuildSystemPrompt 处理失败: {e}")
        return ""


def on_session_start(event: str, context: Dict[str, Any]) -> str:
    """SessionStart hook：注入人工提示（pinned）到 session.messages。

    由主程序 _inject_hook_to_session 包装为 hook 消息注入会话消息列表
    （带 <session-start-hook> / <system-reminder> 标记）。记忆内容不走
    此通道（系统提示词注入，写后失效缓存刷新）；pinned 是人工要求，
    会话启动/清空/压缩时注入一次即可。
    """
    try:
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = _resolve_aid(mgr, context or {})
        if not aid:
            return ""
        return mgr.pinned_block(aid) or ""
    except Exception as e:
        logger.warning(f"[assistant_hub.hooks] SessionStart 处理失败: {e}")
        return ""


# @提及助手检测：按助手名单在消息里定位「@名字」。
# 边界口径与输入框胶囊还原（bottom_input_area._find_assistant_mention_spans）
# 一致：前边界只挡 ASCII 字母数字与 @ _（邮箱/路径里的 @），中文紧邻放行；
# 后边界要求串尾/空白/占位符起始/常见中文标点。旧实现要求 @ 前必为串首或
# 空白，导致「你好@毒蛇妹」能插入胶囊却切不了助手。
_AT_AFTER_CHARS = '，。！？；：、)）】」》"\''
_AT_BEFORE_BLOCK = frozenset("@_")

def _mention_bounds_ok(message: str, start: int, end: int) -> bool:
    """@名字 前后边界是否符合胶囊还原口径。"""
    if start > 0:
        prev = message[start - 1]
        if prev.isascii() and (prev.isalnum() or prev in _AT_BEFORE_BLOCK):
            return False
    if end < len(message):
        nxt = message[end]
        if not (nxt.isspace() or nxt == "[" or nxt in _AT_AFTER_CHARS):
            return False
    return True

def _find_assistant_mention(mgr, message: str) -> str:
    """在消息中定位 @助手名，返回 aid；无匹配返回空串（长名优先，防前缀误匹配）。"""
    if not message or "@" not in message:
        return ""
    labels = []
    for a in mgr.list_assistants_sorted_by_stable():
        for label in (a.name, a.id):
            label = (label or "").strip()
            if label:
                labels.append((label, a.id))
    for label, aid in sorted(labels, key=lambda x: len(x[0]), reverse=True):
        for m in re.finditer("@" + re.escape(label), message):
            if _mention_bounds_ok(message, m.start(), m.end()):
                return aid
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
        mgr = _get_manager()
        if mgr is None:
            return ""
        aid = _find_assistant_mention(mgr, message)
        if not aid:
            return ""
        changed = mgr.set_session_override(sid, aid)
        # 命中即失效缓存（不只 changed 时）：UI 上下文统计等旁路会把 system_prompt
        # 重新填成主助手身份，override 数值未变也会出现陈旧身份，命中即重建最稳。
        mgr._invalidate_session_prompt(sid)
        if changed:
            a = mgr.get(aid)
            logger.info(f"[assistant_hub] 会话 {sid[:8]} 临时切换助手: {a.name if a else aid}")
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] @提及检测失败: {e}")
    return ""


def on_stop(event: str, context: Dict[str, Any]) -> str:
    """Stop hook：记录 sid→aid 归属映射（供记忆工具按会话解析助手归属）。

    归属：会话级临时助手优先（override），否则主助手。恒返回空串。
    """
    try:
        if (context or {}).get("current_role") != "primary":
            return ""
        mgr = _get_manager()
        if mgr is None:
            return ""
        sid = str((context or {}).get("session_id") or "")
        aid = _resolve_aid(mgr, context or {})
        if sid and aid:
            mgr.record_session_aid(sid, aid)
    except Exception as e:
        logger.debug(f"[assistant_hub.hooks] Stop 归属记录失败: {e}")
    return ""
