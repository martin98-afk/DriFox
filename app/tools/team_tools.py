# -*- coding: utf-8 -*-
"""
团队协作工具集 — 基于任务邮件系统

工具：
- team_list_members: 列出团队成员（含 agent_name@window_id 标识符及工作状态）
- team_send_message: 向队友发送任务邮件（支持 agent_name 或 agent_name@window_id）
"""

from app.tools.result import ToolResult


class TeamTools:
    """团队协作工具（由 BuiltinTools 注册）"""

    _STATUS_LABELS = {
        "busy": "🟡 执行任务中",
        "idle": "🟢 空闲",
        "running": "🟡 执行任务中",
        "pending": "⏳ 等待处理",
    }

    def __init__(self, builtin_tools):
        self._builtin_tools = builtin_tools

    # ── 辅助 ────────────────────────────────────────

    def _get_team_manager(self):
        from app.core.team_manager import TeamManager

        return TeamManager.get_instance()

    def _get_window_context(self) -> tuple:
        bt = self._builtin_tools
        window_id = getattr(bt, "_team_window_id", None)
        agent_name = getattr(bt, "_team_agent_name", None)
        return window_id, agent_name

    # ── 工具方法 ────────────────────────────────────

    def team_list_members(self) -> ToolResult:
        """列出团队中的所有成员，返回 agent_name@window_id 格式的标识符、工作状态及角色描述

        🛡️ F9：每成员行追加权限摘要（写/编辑/bash/团队工具 allow 标记 + task_tags），
        leader 派单前一眼可见各成员能做什么。
        """
        window_id, agent_name = self._get_window_context()
        if not window_id:
            return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

        tm = self._get_team_manager()
        members = tm.get_members()
        if not members:
            return ToolResult(True, content="当前没有团队成员。使用 /team --join=<agent> 加入团队。")

        # 角色描述映射：来自团队模板上下文（agent_name → description），
        # 手动加入的成员 / 旧模板无描述 → 不显示（兼容）
        role_descs = {}
        try:
            template = tm.get_template()
            for item in (template or {}).get("agents") or []:
                if isinstance(item, dict) and item.get("agent_name"):
                    desc = str(item.get("description") or "").strip()
                    if desc:
                        role_descs[item["agent_name"]] = desc
        except Exception:
            role_descs = {}

        lines = [f"团队成员 ({len(members)} 人):"]
        for m in members:
            suffix = ""
            if m["window_id"] == window_id and m["agent_name"] == agent_name:
                suffix = " ← 你"
            status = tm.get_member_busy_status(m["window_id"])
            # 细化状态：区分 running 和 pending，显示任务摘要
            running_tasks = tm.get_running_tasks(m["window_id"])
            pending_tasks = tm.get_pending_tasks(m["window_id"])
            if running_tasks:
                status_label = self._STATUS_LABELS.get("running", "🟡 执行任务中")
                task_summary = running_tasks[0].get("subject", "")[:40]
                if task_summary:
                    status_label += f" 「{task_summary}」"
            elif pending_tasks:
                status_label = self._STATUS_LABELS.get("pending", "⏳ 等待处理")
                task_summary = pending_tasks[0].get("subject", "")[:40]
                if task_summary:
                    status_label += f" 「{task_summary}」"
            else:
                status_label = self._STATUS_LABELS.get("idle", "🟢 空闲")
            line = f"  - {m['agent_name']}@{m['window_id']}  {status_label}{suffix}"
            # 角色描述（存在才显示，兼容手动加入成员/旧模板）
            desc = role_descs.get(m["agent_name"], "")
            if desc:
                line += f"\n      角色: {desc}"
            # 🛡️ F9：权限摘要（动态解析优先，快照兜底；无能力信息时静默跳过）
            cap = None
            try:
                cap = tm.get_member_capability(m["agent_name"])
            except Exception:
                cap = None
            if cap:
                line += f"\n      权限: {self._format_capability(cap)}"
            lines.append(line)
        return ToolResult(True, content="\n".join(lines))

    @staticmethod
    def _format_capability(cap: dict) -> str:
        """格式化能力摘要：如 `写✓/编✓/bash✓ | 标签: implement`。

        can_write 是 write/edit/multi_edit 三合一（全部 allow 才 ✓）；
        can_bash / can_team 单独标记；task_tags 逗号拼接。
        """
        mark = lambda ok: "✓" if ok else "✗"
        parts = [
            f"写{mark(bool(cap.get('can_write')))}",
            f"bash{mark(bool(cap.get('can_bash')))}",
            f"团队{mark(bool(cap.get('can_team')))}",
        ]
        tags = cap.get("task_tags") or []
        if tags:
            parts.append("标签: " + ",".join(tags))
        return " / ".join(parts)

    def team_send_message(self, to_agent: str, message: str) -> ToolResult:
        """向团队中的另一个智能体发送任务邮件。

        对方收到后会串行处理（当前任务完成后再处理下一封），
        完成后自动回复结果到你的邮箱。

        Args:
            to_agent: 目标成员标识，支持两种格式：
                      - agent_name（如 build）— 按名称匹配
                      - agent_name@window_id（如 build@win_02）— 精确匹配
                      可通过 team_list_members 查看所有成员标识
            message: 任务描述
        """
        window_id, from_agent = self._get_window_context()
        if not window_id or not from_agent:
            return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

        tm = self._get_team_manager()
        mail_id = tm.send_task(
            from_window=window_id,
            from_agent=from_agent,
            to_identifier=to_agent,  # 支持 agent_name 或 agent_name@window_id
            task_description=message,
        )
        if mail_id:
            target = tm.find_member(to_agent)
            target_label = to_agent
            if target:
                target_label = f"{target['agent_name']}@{target['window_id']}"
            # 🛡️ F9：发送前查目标能力，结果行附提示（仅提示不拦截）
            cap_hint = ""
            target_agent_name = (target or {}).get("agent_name", "")
            try:
                cap = tm.get_member_capability(target_agent_name) if target_agent_name else None
                if cap:
                    cap_hint = f"\n      目标能力: {self._format_capability(cap)}"
            except Exception:
                cap_hint = ""
            return ToolResult(True, content=f"任务邮件已发送给「{target_label}」(#{mail_id}){cap_hint}")
        else:
            # 找不到——列出所有可用成员
            members = tm.get_members()
            available = ", ".join(f"{m['agent_name']}@{m['window_id']}" for m in members)
            return ToolResult(False, error=f"未找到目标「{to_agent}」。当前团队成员: {available}")
