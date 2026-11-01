# -*- coding: utf-8 -*-
"""系统工具插件 — 团队协作工具（自包含实现）

team_*：经 team.read / team.send 能力调用主程序 TeamManager，
窗口团队上下文从 tool_ctx["team_window_id"]/["team_agent_name"] 获取。
团队专用工具注册 team_only=True → 非团队成员从 schema 定义中过滤。

子智能体工具（subagent_para / subagent_status + 结果注入器）已迁
``plugins/system-subagent/tools/subagent_tools.py``。
"""

from loguru import logger

from app.plugins.sdk import ToolResult

GROUP_TEAM = "团队协作"


# ============================================================
# 团队服务（内联实现：TeamManager 为主程序平台能力，插件 import 调用）
# ============================================================

_STATUS_LABELS = {
    "busy": "🟡 执行任务中",
    "idle": "🟢 空闲",
    "running": "🟡 执行任务中",
    "pending": "⏳ 等待处理",
}


def _get_team_manager():
    """团队只读面（成员表 / 模板 / 运行状态）。"""
    from app.plugins.sdk import caps_for

    team = caps_for("team.read")
    return team() if team is not None else None


def _get_team_sender():
    """团队写面（仅派发任务邮件）。"""
    from app.plugins.sdk import caps_for

    sender = caps_for("team.send")
    return sender() if sender is not None else None


def _format_capability(cap: dict) -> str:
    """格式化能力摘要：如 `写✓/编✓/bash✓ | 标签: implement`。"""
    mark = lambda ok: "✓" if ok else "✗"
    parts = [
        f"写{mark(bool(cap.get('can_write')))}",
        f"bash{mark(bool(cap.get('can_bash')))}",
        # T16：团队成员恒具团队工具 → 恒 ✓（不读 can_team，防御老快照）
        "团队✓",
    ]
    tags = cap.get("task_tags") or []
    if tags:
        parts.append("标签: " + ",".join(tags))
    return " / ".join(parts)


def _team_list_members(tool_ctx) -> ToolResult:
    """列出团队所有成员（🛡️ M1：按 run 分组，单团队/多团队自适应）。

    - 单团队（无 run_id 成员 / 仅 1 个 run）：输出本团队明细，无其他团队段
    - 多团队并存：本团队明细 + 「其他团队 (N 个):」概要 + 跨团队沟通提示
    """
    window_id = tool_ctx.get("team_window_id", "")
    agent_name = tool_ctx.get("team_agent_name", "")
    if not window_id:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

    tm = _get_team_manager()
    members = tm.get_members()
    if not members:
        return ToolResult(True, content="当前没有团队成员。使用 /team --join=<agent> 加入团队。")

    # 🛡️ M1 防御性调用：旧测试用 _FakeTM mock 可能不实现新增方法，
    # getattr fallback 让老测试零改动仍工作。
    def _safe_team_label() -> str:
        getter = getattr(tm, "get_team_label", None)
        if callable(getter):
            try:
                return getter() or "default"
            except Exception:
                return "default"
        return "default"

    def _safe_team_label_by_run(rid: str) -> str:
        getter = getattr(tm, "get_team_label_by_run", None)
        if callable(getter) and rid:
            try:
                return getter(rid) or rid
            except Exception:
                return rid
        return rid

    role_descs = {}
    try:
        # 🛡️ M1'：按本窗口所属 run_id 读专属模板槽（templates_by_run_id），
        # 防多团队并存时顶层 template 单槽互相覆盖；getattr 回退保证
        # 老测试 mock 未实现新方法时零改动仍工作（与 _safe_team_label 同模式）。
        run_id = ""
        _run_getter = getattr(tm, "get_member_run_id", None)
        if callable(_run_getter) and window_id:
            run_id = _run_getter(window_id) or ""
        _tpl_getter = getattr(tm, "get_template_for_run_id", None)
        if callable(_tpl_getter) and run_id:
            template = _tpl_getter(run_id)
        else:
            template = tm.get_template()
        for item in (template or {}).get("agents") or []:
            if isinstance(item, dict) and item.get("agent_name"):
                desc = str(item.get("description") or "").strip()
                if desc:
                    role_descs[item["agent_name"]] = desc
    except Exception:
        role_descs = {}

    # 🛡️ M1：按 run_id 分组——当前窗口所属 run 视为「本团队」，其他 run
    # 折叠到「其他团队」概要；无 run_id 老成员并入「本团队」（向后兼容）。
    from collections import OrderedDict

    by_run: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    legacy_members: list = []
    for m in members:
        rid = m.get("run_id", "") or ""
        if rid:
            slot = by_run.get(rid)
            if slot is None:
                slot = {"label": (m.get("team_label") or ""), "members": []}
                by_run[rid] = slot
            elif not slot["label"] and m.get("team_label"):
                slot["label"] = m["team_label"]
            slot["members"].append(m)
        else:
            legacy_members.append(m)

    # 当前窗口所属 run（决定「本团队」边界）
    my_run_id = ""
    for m in members:
        if m["window_id"] == window_id:
            my_run_id = m.get("run_id", "") or ""
            break

    # 🛡️ M1（S1 review 修复）：多团队判定——
    #   - 系统存在任何 run_id 成员 → 多团队
    #     ├─ 当前窗口有 run_id：本团队 = 该 run 成员 ∪ legacy；其他团队 = 其他 run
    #     └─ 当前窗口无 run_id：本团队 = 所有无归属成员（含自己）；其他团队 = 所有 run
    #   - 系统无任何 run_id（纯 legacy） → 单团队，全部视为本团队（向后兼容）
    multi_team = bool(by_run)

    if multi_team:
        if my_run_id:
            # 当前窗口有 run：本团队 = 该 run 的成员 ∪ legacy 成员
            my_slot = by_run.get(my_run_id, {"label": "", "members": []})
            my_members = list(my_slot["members"]) + list(legacy_members)
            my_label = my_slot.get("label") or _safe_team_label_by_run(my_run_id) or _safe_team_label()
            other_teams = [(rid, info) for rid, info in by_run.items() if rid != my_run_id]
        else:
            # 🛡️ S1（review 建议修）：当前窗口无 run_id 但系统存在 run 团队 →
            # 本团队折叠为「未归属」组（所有无 run_id 成员），有 run_id 的成员
            # 按 label 折叠进「其他团队」概要——避免误判为单团队把所有人混在一起。
            my_members = list(legacy_members)
            my_label = "未归属"
            other_teams = list(by_run.items())
    else:
        # 单团队（含 legacy 兼容）：全部视为本团队
        my_members = list(members)
        my_label = _safe_team_label()
        other_teams = []

    def _render_member(m: dict) -> str:
        suffix = ""
        if m["window_id"] == window_id and m["agent_name"] == agent_name:
            suffix = " ← 你"
        status = tm.get_member_busy_status(m["window_id"])
        running_tasks = tm.get_running_tasks(m["window_id"])
        pending_tasks = tm.get_pending_tasks(m["window_id"])
        if running_tasks:
            status_label = _STATUS_LABELS.get("running", "🟡 执行任务中")
            task_summary = running_tasks[0].get("subject", "")[:40]
            if task_summary:
                status_label += f" 「{task_summary}」"
        elif pending_tasks:
            status_label = _STATUS_LABELS.get("pending", "⏳ 等待处理")
            task_summary = pending_tasks[0].get("subject", "")[:40]
            if task_summary:
                status_label += f" 「{task_summary}」"
        else:
            status_label = _STATUS_LABELS.get(status, "🟢 空闲")
        # 🛡️ M1：行格式「agent_name: agent_name@window_id」便于按角色扫读；
        # 同时保留 @window_id 以满足现有按 window_id 标识成员的调用方式。
        line = f"  - {m['agent_name']}: {m['agent_name']}@{m['window_id']}  {status_label}{suffix}"
        desc = role_descs.get(m["agent_name"], "")
        if desc:
            line += f"\n      角色: {desc}"
        cap = None
        try:
            cap = tm.get_member_capability(m["agent_name"])
        except Exception:
            cap = None
        if cap:
            line += f"\n      权限: {_format_capability(cap)}"
        return line

    lines: list = [f"团队「{my_label}」 ({len(my_members)} 人):"]
    for m in my_members:
        lines.append(_render_member(m))

    if other_teams:
        lines.append("")
        lines.append(f"其他团队 ({len(other_teams)} 个):")
        for rid, info in other_teams:
            label = info.get("label") or _safe_team_label_by_run(rid) or rid
            lines.append(f"  团队「{label}」({len(info['members'])} 人):")
            for m in info["members"]:
                lines.append(_render_member(m))
        lines.append("")
        lines.append("⚠ 团队间沟通：你只能向本团队成员发送任务；如需联系其他团队，只通过各自 leader 传递团队间消息。")

    return ToolResult(True, content="\n".join(lines))


def _team_send_message(tool_ctx, to_agent: str, message: str) -> ToolResult:
    """向团队成员发送任务邮件（支持 agent_name 或 agent_name@window_id）

    🛡️ M1（user 拍板放行+打标）：跨团队消息**不再硬拦截**，改为放行+打标——
    mail dict 写 from_run_id / to_run_id 字段，返回成功结果后追加「⚠ 跨团队
    消息已发送（run_a → run_b）」提示，由接收方/历史面板决定如何呈现。
    """
    window_id = tool_ctx.get("team_window_id", "")
    from_agent = tool_ctx.get("team_agent_name", "")
    if not window_id or not from_agent:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

    tm = _get_team_manager()

    # 🐛 发件人角色兜底（B1 修复后保留作最后防线）：tool_ctx["team_agent_name"]
    # 来自 BuiltinTools 团队上下文，若 join 时序晚于 chat_engine 就绪会滞留
    # 默认值 "build"。这里以 TeamManager 成员表为权威反查并覆盖，保证邮件
    # 发件人显示实际成员名。B1 修复后正常路径不应触发，触发即说明存在别的
    # tool_ctx 漏传问题——保留以便定位而不至于发件人字段错乱。
    try:
        for _m in tm.get_members() or []:
            if _m.get("window_id") == window_id and _m.get("agent_name"):
                if _m["agent_name"] != from_agent:
                    logger.info(
                        f"[TeamMail] 发件人角色校正 {from_agent} → {_m['agent_name']} "
                        f"(tool_ctx 与成员表不一致，以成员表为准)"
                    )
                from_agent = _m["agent_name"]
                break
    except Exception:
        pass  # 成员表不可用时保持 tool_ctx 原值

    # 🛡️ M1（user 拍板放行+打标）：跨团队判定用于附加提示，不再拦截发件。
    # 双方 run_id 均非空且不同 → 跨团队消息，追加「⚠ 跨团队消息已发送」提示。
    from_run_id: str = ""
    to_run_id: str = ""
    cross_team_notice: str = ""
    try:
        from_run_id = tm.get_member_run_id(window_id) or ""
        target_pre = tm.find_member(to_agent)
        to_run_id = ((target_pre or {}).get("run_id") or "") if target_pre else ""
        if from_run_id and to_run_id and from_run_id != to_run_id:
            cross_team_notice = f"\n⚠ 跨团队消息已发送（{from_run_id} → {to_run_id}）：建议经双方 leader 协调任务边界"
    except Exception:
        # 跨团队判定失败不影响正常发件（兜底：仅放行不报）
        pass

    sender = _get_team_sender()
    if sender is None:
        return ToolResult(False, error="团队派单能力不可用（缺少 team.send 能力声明）")

    mail_id = sender.send_task(
        from_window=window_id,
        from_agent=from_agent,
        to_identifier=to_agent,
        task_description=message,
        from_run_id=from_run_id,
        to_run_id=to_run_id,
    )
    if mail_id:
        target = tm.find_member(to_agent)
        target_label = to_agent
        if target:
            target_label = f"{target['agent_name']}@{target['window_id']}"
        cap_hint = ""
        target_agent_name = (target or {}).get("agent_name", "")
        try:
            cap = tm.get_member_capability(target_agent_name) if target_agent_name else None
            if cap:
                cap_hint = f"\n      目标能力: {_format_capability(cap)}"
        except Exception:
            cap_hint = ""
        return ToolResult(True, content=f"任务邮件已发送给「{target_label}」(#{mail_id}){cap_hint}{cross_team_notice}")
    else:
        members = tm.get_members()
        available = ", ".join(f"{m['agent_name']}@{m['window_id']}" for m in members)
        return ToolResult(False, error=f"未找到目标「{to_agent}」。当前团队成员: {available}")


# ============================================================
# schema
# ============================================================

_TEAM_SEND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_send_message",
        "description": "向团队中的另一个智能体发送任务邮件（仅团队模式下可用）。对方会串行处理任务，完成后自动回复结果到你的邮箱。",
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "目标成员标识，支持 agent_name（如 build）或 agent_name@window_id（如 build@win_02），通过 team_list_members 查看",
                },
                "message": {"type": "string", "description": "消息内容"},
            },
            "required": ["to_agent", "message"],
        },
    },
}

_TEAM_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_list_members",
        "description": "列出团队中的所有成员（返回 agent_name@window_id 格式的唯一标识符，如 build@win_02）。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ============================================================
# impl
# ============================================================


def _team_send_impl(tool_ctx, **kwargs):
    return _team_send_message(tool_ctx, kwargs.get("to_agent", ""), kwargs.get("message", ""))


def _team_list_impl(tool_ctx, **kwargs):
    return _team_list_members(tool_ctx)


def register(registry):
    # 团队专用工具：team_only=True → 非团队成员从 schema 定义中过滤（LLM 不可见）
    registry.register(
        "team_send_message",
        _TEAM_SEND_SCHEMA,
        impl=_team_send_impl,
        danger="safe",
        icon="邮件-发送",
        cn_name="发送邮件",
        group=GROUP_TEAM,
        description="向团队成员发送任务",
        team_only=True,
    )
    registry.register(
        "team_list_members",
        _TEAM_LIST_SCHEMA,
        impl=_team_list_impl,
        danger="safe",
        icon="团队",
        cn_name="团队成员",
        group=GROUP_TEAM,
        description="列出团队成员",
        team_only=True,
    )
