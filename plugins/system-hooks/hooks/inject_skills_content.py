# -*- coding: utf-8 -*-
"""
Hook 函数 — 注入「偏好技能」清单

事件分工（hooks.json 声明）：
- SessionStart：主对话入口，会话启动时注入一次进会话消息，不再占 system prompt。
- BuildSystemPrompt：兜底。子智能体 / Gateway 没有会话启动点，只能靠系统提示
  拿到清单；主对话若会话消息里已有清单则跳过，避免两份重复。

数据全部经能力池自取，主程序不做预取：
- 技能清单 → SDK ``get_local_skills``（含插件技能与项目动态技能源）
- 启用名单 → ``config.read`` 读 ``llm_enabled_skills``
- 已注入判定 → ``session.read`` 看会话消息有没有清单标记
"""

from loguru import logger

_HEADER = (
    "\n\n## 偏好技能\n"
    "下面的描述供选择技能。用户点名（含 /命令）或项目规则明确要求的技能应加载；"
    "其余按当前任务需要选用，常规问答和预期明确的修复可直接完成。\n"
)

_MARKERS = ("<enabled_skills>", "<available_subagents>")

def _session_has_inventory() -> bool:
    """会话消息里是否已带清单（SessionStart 注入过）。

    只在主对话 system prompt 链调用（context 带 session_id 时）。取不到会话
    返回 False，按"还没注入"处理，让系统提示兜底，宁可重复也不丢清单。
    """
    try:
        from app.plugins.sdk import caps_for

        read = caps_for("session.read", surface="ui")
        if not callable(read):
            return False
        messages = (read() or {}).get("messages") or []
    except Exception as e:
        logger.debug(f"[inject_skills_content] 读会话失败: {e}")
        return False

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("_hook_event") != "SessionStart":
            continue
        content = msg.get("content")
        texts = [content] if isinstance(content, str) else []
        if isinstance(content, list):
            texts = [
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
        for text in texts:
            if any(marker in text for marker in _MARKERS):
                return True
    return False

def _enabled_names() -> set:
    """用户显式启用的技能名集合（能力池读配置；该键非密钥类）。"""
    try:
        from app.plugins.sdk import caps_for

        read = caps_for("config.read", surface="ui")
        value = read("llm_enabled_skills", []) if callable(read) else []
        return set(value or [])
    except Exception as e:
        logger.debug(f"[inject_skills_content] 读启用名单失败: {e}")
        return set()

def _build_block(workdir: str) -> str:
    """按「偏好技能」段格式拼清单；无启用技能返回空串。

    两类进清单：启用名单内的技能、auto_enable=True 的条目（如项目技能）。
    """
    from app.plugins.sdk import get_local_skills

    all_skills = get_local_skills(project_dir=workdir or None) or []
    if not all_skills:
        return ""
    enabled = _enabled_names()
    parts = [_HEADER]
    for skill in all_skills:
        if skill.get("name") in enabled or skill.get("auto_enable"):
            display_name = skill.get("qualified_name", skill.get("name", ""))
            parts.append(f"\n### {display_name}\n{skill.get('description', '')}\n")
    return "\n".join(parts) if len(parts) > 1 else ""

def hook(event: str, context: dict) -> str:
    """输出 <enabled_skills> 包裹的技能清单

    Args:
        event: 事件名称（SessionStart / BuildSystemPrompt）
        context: 宿主上下文。project_root 用于动态技能源求值；
            session_id 标识主对话 system prompt 链（据此判定是否重复注入）

    Returns:
        <enabled_skills> 包裹的技能内容字符串，或空字符串
    """
    # 去重只对主对话生效：只有它的 system prompt 链会带 session_id。
    # 子智能体 / Gateway 不传 session_id，它们没有会话启动点，必须由这里兜底。
    if event == "BuildSystemPrompt" and (context or {}).get("session_id"):
        if _session_has_inventory():
            return ""
    try:
        content = _build_block(str((context or {}).get("project_root") or ""))
    except Exception as e:
        logger.warning(f"[inject_skills_content] 构建技能清单失败: {e}")
        return ""
    if not content:
        return ""
    return f"<enabled_skills>\n{content}\n</enabled_skills>"
