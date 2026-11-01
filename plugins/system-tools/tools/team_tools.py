# -*- coding: utf-8 -*-
"""系统工具插件 — 团队协作工具（自包含实现）

team_*：经 team.read / team.send / team.manage 能力调用主程序 TeamManager，
窗口团队上下文从 tool_ctx["team_window_id"]/["team_agent_name"] 获取。

#4（一句话组队）：team_manage 单工具 + action 分发（list/create/add/remove/leave），
team_only=False → 非团队窗口可发起组队；权限按 action 在 impl 内分层。
create 不再把调用窗口入队：leader 与成员全部新开窗，调用窗口保持非团队成员，
可直接用 team_send_message 给成员派任务（非成员发送标识为 user；群发需 leader）。

子智能体工具（subagent_para / subagent_status + 结果注入器）已迁
``plugins/system-subagent/tools/subagent_tools.py``。
"""

from typing import Any, Dict, List, Optional

import json

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

def _get_team_manage():
    """团队管理面（建团/入退队/移除成员/群发）。"""
    from app.plugins.sdk import caps_for

    manage = caps_for("team.manage")
    return manage() if manage is not None else None

def _resolve_window_id(tool_ctx) -> str:
    """解析调用窗口 ID（已入队窗口走 tool_ctx；降级链经 team.spawn.sync_caller_context）。"""
    return str(tool_ctx.get("team_window_id", "") or "")

def _spawn_facade() -> Optional[dict]:
    """取 team.spawn 能力面（pool 值即 provider 返回的 dict，非 api 闭包，勿再调用）。"""
    try:
        from app.plugins.sdk import caps_for

        sp = caps_for("team.spawn")
    except Exception:
        return None
    if sp is None:
        return None
    facade = sp
    return facade if isinstance(facade, dict) else None

def _leader_gate(tm, window_id: str) -> Optional[ToolResult]:
    """leader 门禁单点（remove_member / 群发；不进 PermissionResolver）。

    Returns:
        None=放行；否则返回 deny 结果。leader_window_id 为空（非
        team_manage(action=create) 建团的老团队）同样拒绝，避免管理操作悬空。
    """
    try:
        leader_wid = str(tm.get_leader_window_id() or "")
    except Exception:
        leader_wid = ""
    if not leader_wid:
        return ToolResult(False, error="当前团队未登记 leader（仅 team_manage(action=create) 建团支持管理操作），已拒绝")
    if window_id == leader_wid:
        return None
    leader_label = leader_wid
    try:
        name = tm.get_member_agent_name(leader_wid)
        if name:
            leader_label = f"{name}@{leader_wid}"
    except Exception:
        pass
    return ToolResult(False, error=f"仅团队 leader 可执行该操作（当前 leader: {leader_label}）")


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
        return ToolResult(True, content='当前没有团队成员。用 team_manage(action="create", team_name=..., team_role=...) 创建团队，或 /team --join=<agent> 加入。')

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
    _is_member_view = False
    for m in members:
        if m["window_id"] == window_id:
            my_run_id = m.get("run_id", "") or ""
            _is_member_view = True
            break

    # 🛡️ M1（S1 review 修复）：多团队判定——
    #   - 系统存在任何 run_id 成员 → 多团队
    #     ├─ 当前窗口有 run_id：本团队 = 该 run 成员 ∪ legacy；其他团队 = 其他 run
    #     └─ 当前窗口无 run_id：本团队 = 所有无归属成员（含自己）；其他团队 = 所有 run
    #   - 系统无任何 run_id（纯 legacy） → 单团队，全部视为本团队（向后兼容）
    # 🆕 非成员只读视角（外部窗口查看团队）：不做团队边界切分，全部成员
    # 按单团队展示（否则会全落进「其他团队」段，读起来像别人的团队）。
    multi_team = bool(by_run) and _is_member_view

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

    # P1③：pending 开窗回执（防模型把「团队 (1 人)」误判为全员就绪）
    try:
        pending_roles = _team_pending_window_snapshot(window_id)
    except Exception:
        pending_roles = []
    if pending_roles:
        lines.append("")
        lines.append(f"开窗中成员: [{', '.join(pending_roles)}]（本轮结束后拉起，尚不在上方成员表内）")

    return ToolResult(True, content="\n".join(lines))


def _team_send_message(tool_ctx, to_agent, message: str) -> ToolResult:
    """向团队成员发送任务邮件（to_agent: str | list[str]；"*"/["*"]=全队群发）

    单目标路径行为不变（向后兼容）；群发走 team.manage.send_task_broadcast
    （leader 门禁单点在本函数内），targets 原样透传（"*" 展开语义由
    TeamManager 侧负责，工具层不先展开，保住「未登记 leader → skipped」语义）。

    🛡️ M1（user 拍板放行+打标）：跨团队消息**不再硬拦截**，改为放行+打标——
    mail dict 写 from_run_id / to_run_id 字段，返回成功结果后追加「⚠ 跨团队
    消息已发送（run_a → run_b）」提示，由接收方/历史面板决定如何呈现。

    🆕 非团队成员（外部发起）也可发送：发送方标识记为 "user"，任务照常投递
    到成员邮箱并由成员 watcher 自动拉起执行；成员无法直接回复到外部窗口
    （成员只能发团队内部），执行状态用 team_manage(action="list") 查看。
    """
    window_id = str(tool_ctx.get("team_window_id", "") or "")
    from_agent = str(tool_ctx.get("team_agent_name", "") or "")
    if not window_id:
        # 兜底解析调用窗口身份（buildin_tools 未初始化时走 team.spawn 能力）
        facade0 = _spawn_facade()
        if facade0 is not None and "sync_caller_context" in facade0:
            try:
                _sr = facade0["sync_caller_context"]()
                if _sr.get("ok"):
                    window_id = str(_sr.get("window_id", "") or "")
            except Exception:
                pass
    if not window_id:
        return ToolResult(False, error="无法定位调用窗口（window_id 缺失）")

    tm = _get_team_manager()

    # 调用方是否团队成员：非成员允许发送（外部发起），发送方标识记为 user
    is_member = False
    try:
        is_member = bool(tm.is_team_member(window_id))
    except Exception:
        is_member = False
    if not is_member:
        from_agent = "user"

    # ── 群发分支：list 或 "*"（leader 门禁；单目标行为完全不变）──
    if isinstance(to_agent, list) or to_agent == "*":
        gate = _leader_gate(tm, window_id)
        if gate is not None:
            return gate
        mg = _get_team_manage()
        if mg is None:
            return ToolResult(False, error="团队管理能力不可用（缺少 team.manage 能力声明）")
        targets = [str(t) for t in to_agent] if isinstance(to_agent, list) else None
        text = str(message or "")
        result = mg.send_task_broadcast(subject=text[:80], body=text, targets=targets)
        sent = result.get("sent", []) or []
        skipped = result.get("skipped", []) or []
        lines = [f"群发完成：成功 {len(sent)} 封，跳过 {len(skipped)} 个目标。"]
        for s in sent:
            mark = " ⚠跨团队" if s.get("cross_team") else ""
            lines.append(f"  ✓ {s.get('identifier', '?')} (#{s.get('mail_id', '?')}){mark}")
        for ident, reason in skipped:
            lines.append(f"  ⊘ {ident}：{reason}")
        return ToolResult(True, content="\n".join(lines))

    # ── 单目标原路径（保持不变）──
    to_agent = str(to_agent or "")

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
        external_notice = ""
        if not is_member:
            external_notice = '\n⚠ 外部发起（你不在团队中）：成员无法直接回复到本窗口；执行状态用 team_manage(action="list") 查看。'
        return ToolResult(
            True,
            content=f"任务邮件已发送给「{target_label}」(#{mail_id}){cap_hint}{cross_team_notice}{external_notice}",
        )
    else:
        members = tm.get_members()
        available = ", ".join(f"{m['agent_name']}@{m['window_id']}" for m in members)
        return ToolResult(False, error=f"未找到目标「{to_agent}」。当前团队成员: {available}")

# ============================================================
# 管理团队工具 impl（#4：create / leave / add / remove）
# ============================================================

def _team_create(tool_ctx, team_name: str, team_role: str, members) -> ToolResult:
    """一句话建团（team_only=False：任何窗口可发起「一句话组队」）

    新语义（全部重新创建，调用窗口不动）：
    - 建 run + 槽位；**调用窗口不加入团队**（不 join、不回填团队上下文、
      不迁移标签页），保持原非团队成员身份与提示词
    - 开窗清单 = ["leader"] + members 角色，全部新建窗口；leader 由新建的
      leader 窗口担任（开窗路径 join 后回写 leader_window_id）
    - 开窗实时生效：leader 与成员窗口在本轮对话进行中创建（team_spawn
      实时处理器，不等流结束）；当前窗口保持非团队成员、不切标签页
    - 窗口创建期间可用 team_manage(action=list) 查看已有成员
    """
    if not str(team_name or "").strip():
        return ToolResult(False, error="team_name 不能为空")

    # 成员清单规范化：[{agent_name, description}]；leader 由开窗路径固定创建，
    # 调用方重复传 leader 时过滤，防双 leader 窗口
    agents: list = []
    if isinstance(members, list):
        for item in members:
            if isinstance(item, dict) and str(item.get("agent_name", "") or "").strip():
                _name = str(item["agent_name"]).strip()
                if _name == "leader":
                    continue
                agents.append(
                    {
                        "agent_name": _name,
                        "description": str(item.get("description", "") or "").strip(),
                    }
                )

    mg = _get_team_manage()
    if mg is None:
        return ToolResult(False, error="团队管理能力不可用（缺少 team.manage 能力声明）")

    window_id = _resolve_window_id(tool_ctx)
    if not window_id:
        # 调用窗口身份解析降级走 team.spawn.sync_caller_context（仅解析，
        # agent_name 空串不触碰上下文；护栏禁止插件直碰 builtin_tools）
        facade0 = _spawn_facade()
        if facade0 is not None and "sync_caller_context" in facade0:
            try:
                _sr = facade0["sync_caller_context"]()
                if _sr.get("ok"):
                    window_id = str(_sr.get("window_id", "") or "")
            except Exception:
                pass

    # P0④：已在团队中 → 引导增补而非重复建团。
    # 判定依据：TeamManager.is_team_member(window_id) 以成员表为权威。
    if window_id:
        try:
            _tm0 = _get_team_manager()
            if _tm0.is_team_member(window_id):
                _label = _tm0.get_team_label()
                return ToolResult(
                    False,
                    error=f'你已在团队「{_label}」中，如需增补成员用 team_manage(action="add")；如需新建请先 team_manage(action="leave")',
                )
        except Exception:
            pass

    # 不登记 leader：调用窗口保持非团队成员，leader_window_id 由 leader 新窗口
    # 开窗路径回写（见 _spawn_team_member_window 的 is_leader 分支）。
    result = mg.create_team(
        team_label=str(team_name).strip(),
        team_role=str(team_role or "").strip(),
        agents=agents,
        leader_window_id="",
    )
    run_id = result.get("run_id", "")

    agent_lines = "\n".join(
        f"  - {a['agent_name']}: {a['description'] or '(无描述)'}" for a in agents
    ) or "  （仅有 leader，其他角色可用 team_manage(action=add) 增补）"

    # 全部重新创建：leader 首个入队（先建先 join，leader_window_id 立即可用），
    # 随后是成员；keep_current_active=True 保证搭建过程不抢当前焦点 tab。
    spawn_note = ""
    queued = 0
    pending_window: list = []
    spawn_roles = ["leader"] + [a["agent_name"] for a in agents]
    try:
        from app.plugins.sdk import caps_for as _cf

        _sp = _cf("team.spawn")
        # pool[cap_id] 即 provider(host) 返回值本身（dict），非 api 闭包，勿再调用
        _facade = _sp if _sp else None
        if isinstance(_facade, dict) and "spawn_members" in _facade:
            _receipt = _facade["spawn_members"](
                spawn_roles,
                run_id,
                team_label=str(team_name).strip(),
                leader_agent="leader",
                keep_current_active=True,
            )
            if _receipt.get("submitted"):
                queued = int(_receipt.get("queued", 0))
                pending_window = list(_receipt.get("pending_window") or [])
                spawn_note = "\nleader 与成员窗口已开始实时创建（本轮对话期间拉起；当前窗口保持非团队成员）。"
            else:
                spawn_note = f'\n⚠ note: "团队已建，窗口未拉起（{_receipt.get("error", "开窗请求被拒")}），稍后可用 action=add 重试"'
        else:
            spawn_note = '\n⚠ note: "团队已建，窗口未拉起（team.spawn 返回形态异常），稍后可用 action=add 重试"'
    except Exception as e:  # noqa: BLE001
        spawn_note = f'\n⚠ note: "团队已建，窗口未拉起（{e}），稍后可用 action=add 重试"'

    # leader 分组与邮箱监听由开窗路径自带（_spawn_team_member_window →
    # _join_new_window_for_template 的 _refresh_team_ui + _start_team_watcher），
    # 不再需要 ensure_leader_watch 迁移调用窗口。
    receipt_block = ""
    if queued or pending_window:
        receipt_block = f"\n回执: {json.dumps({'queued': queued, 'pending_window': pending_window}, ensure_ascii=False)}"
    return ToolResult(
        True,
        content=(
            f"团队「{team_name}」已创建（run_id={run_id}）。"
            f"leader 与成员全部新建窗口，当前窗口保持非团队成员（未加入、未切换）。"
            f"\n开窗清单：\n  - leader: 团队统筹（新建窗口）\n{agent_lines}"
            f"{spawn_note}{receipt_block}"
            f"\n窗口拉起后：用 team_manage(action=\"list\") 查看成员；"
            f"当前窗口可直接用 team_send_message 给成员派任务（成员自动开始执行）。"
        ),
    )

def _team_leave(tool_ctx) -> ToolResult:
    """调用者退出当前团队（数据层直接改；无 leader 判定）"""
    window_id = tool_ctx.get("team_window_id", "")
    if not window_id:
        return ToolResult(False, error="当前不在团队上下文中，无需退出")

    tm = _get_team_manager()
    mg = _get_team_manage()
    if mg is None:
        return ToolResult(False, error="团队管理能力不可用（缺少 team.manage 能力声明）")

    was_leader = False
    try:
        was_leader = tm.get_leader_window_id() == window_id
    except Exception:
        pass

    mg.leave_team(window_id)

    # P0③：清调用窗口团队上下文（对齐 backend set_team_context 清空语义；
    # 经 team.spawn.clear_caller_context，护栏禁止插件直碰 builtin_tools）
    note = ""
    facade = _spawn_facade()
    if facade is not None and "clear_caller_context" in facade:
        result = facade["clear_caller_context"]()
        if not result.get("ok"):
            note = f"\n⚠ 窗口上下文清理失败（{result.get('error', '未知')}）"
    else:
        note = "\n⚠ 窗口上下文清理跳过（team.spawn 未就绪）"

    lines = ["已退出团队。"]
    if was_leader:
        try:
            new_leader = str(tm.get_leader_window_id() or "")
            if new_leader:
                lines.append(f"leader 已自动移交：{tm.get_member_agent_name(new_leader) or '?'}@{new_leader}")
            else:
                lines.append("团队已无存活成员，团队状态已清空。")
        except Exception:
            pass
    if note:
        lines.append(note)
    lines.append("提示：UI 侧完整退出（权限恢复/胶囊刷新）仍可用 /team --leave。")
    return ToolResult(True, content="\n".join(lines))

def _validate_agent_exists(tm, agent_name: str) -> Optional[ToolResult]:
    """校验角色存在（P0①：防 ghost 成员）。失败返回结构化错误，None=存在或通道不可用。"""
    try:
        from app.plugins.sdk import caps_for

        agent_read = caps_for("agent.read")
        if agent_read is None:
            return None  # 能力未授权时放行，由开窗侧兜底
        # agent.read 能力返回的就是裸 AgentManager（非 wrapper），勿再调
        # get_agent_manager（仅存在于 engine_host Protocol 声明，实际不存在）
        am = agent_read()
        if am is None:
            return None
        names = [
            a.name
            for a in am.list_agents(include_hidden=False)
            if getattr(a, "mode", "") in ("subagent", "all")
        ]
        if agent_name not in names:
            return ToolResult(
                False,
                error=f'{{"submitted": false, "error": "角色 {agent_name} 不存在", "available": {json.dumps(names[:20], ensure_ascii=False)}}}',
            )
    except Exception:
        return None  # 校验通道异常不阻塞（开窗侧有兜底）
    return None

def _team_pending_window_snapshot(window_id: str) -> list:
    """读该窗口 pending 开窗角色清单（action.queue.peek 只读；经能力池）。"""
    try:
        from app.plugins.sdk import caps_for

        cap = caps_for("action.queue")
        if cap is None:
            return []
        queue = cap()
        items = queue.peek(window_id) or []
        roles: list = []
        for item in items:
            if not isinstance(item, dict) or item.get("action") != "team_spawn":
                continue
            payload = item.get("payload") or {}
            for a in payload.get("agents") or []:
                # spawn_members 入队的 agents 是角色名字符串；兼容 dict 形态
                if isinstance(a, str) and a:
                    roles.append(a)
                elif isinstance(a, dict) and a.get("agent_name"):
                    roles.append(str(a["agent_name"]))
        return roles
    except Exception:
        return []

def _team_add_member(tool_ctx, agent_name: str, role: str) -> ToolResult:
    """拉人入队（入队开窗请求语义；成员可拉人，无 leader 限制）

    - 不自己 join_team（新成员的 members 写入由开窗路径前置完成，防孤儿记录）
    - 校验角色存在（P0①）→ 校验调用者已入队 → 调 team.spawn 能力入队开窗
    - team.spawn 未注册（本轮）→ 结构化「能力未就绪」，不写 members/槽位
    """
    window_id = tool_ctx.get("team_window_id", "")
    if not window_id:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")
    agent_name = str(agent_name or "").strip()
    if not agent_name:
        return ToolResult(False, error="agent_name 不能为空")

    tm = _get_team_manager()

    # P0①：角色存在校验（失败直接拒绝，不入队）
    invalid = _validate_agent_exists(tm, agent_name)
    if invalid is not None:
        return invalid

    # 调用者必须已入队（成员表核查）
    try:
        if not tm.is_team_member(window_id):
            return ToolResult(False, error="调用者不在团队成员表中，请先 team_manage(action=create) 建团或 /team --join 入队")
    except Exception:
        return ToolResult(False, error="成员表不可用，无法校验调用者身份")

    # team.spawn 能力（#10 注册；本轮未注册 → 结构化未就绪，不产生假成功）
    try:
        from app.plugins.sdk import caps_for

        spawn = caps_for("team.spawn")
    except Exception:
        spawn = None
    if spawn is None:
        return ToolResult(
            False,
            error='{"submitted": false, "error": "team.spawn 能力未授权或未注册，成员未加入"}',
        )
    try:
        # pool[cap_id] 即 provider(host) 返回的 dict 本身（非 api 闭包），勿再调用
        facade = spawn
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[team_manage] team.spawn 取值异常: {e}")
        return ToolResult(
            False,
            error='{"submitted": false, "error": "team.spawn 能力取值失败，成员未加入"}',
        )
    if not isinstance(facade, dict) or "spawn_members" not in facade:
        logger.warning(f"[team_manage] team.spawn 返回形态异常: {type(facade).__name__}")
        return ToolResult(
            False,
            error='{"submitted": false, "error": "team.spawn 返回形态异常（预期 dict.spawn_members），成员未加入"}',
        )

    run_id = ""
    team_label = ""
    try:
        run_id = tm.get_member_run_id(window_id) or tm.get_team_run_id()
        team_label = (tm.get_team_label_by_run(run_id) if run_id else "") or tm.get_team_label()
    except Exception:
        pass

    try:
        receipt = facade["spawn_members"](
            [{"agent_name": agent_name, "description": str(role or "").strip()}], run_id, team_label
        )
    except TypeError as e:
        # 只兜 provider 签名漂移；形态错误已在上方 isinstance 拦截并明示
        logger.warning(f"[team_manage] team.spawn.spawn_members 调用签名不匹配: {e}")
        return ToolResult(
            False,
            error=f'{{"submitted": false, "error": "team.spawn 调用签名不匹配（{e}），成员未加入"}}',
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, error=f'{{"submitted": false, "error": "开窗请求提交失败: {e}"}}')
    if not isinstance(receipt, dict) or not receipt.get("submitted"):
        err = (receipt or {}).get("error", "开窗请求被拒")
        return ToolResult(False, error=f'{{"submitted": false, "error": "{err}"}}')

    # P0②：状态回执（pending_window 由 provider 从 action_queue.peek 现算）
    receipt["note"] = receipt.get("note") or "窗口正在实时创建（本轮对话期间拉起），下一轮用 team_manage(action=list) 验证。"
    lines = [
        f"开窗请求已提交：{agent_name}" + (f"（{role}）" if str(role or "").strip() else ""),
        f"回执: {json.dumps(receipt, ensure_ascii=False)}",
    ]
    return ToolResult(True, content="\n".join(lines))

def _team_remove_member(tool_ctx, agent_name: str) -> ToolResult:
    """按 agent_name 移除成员（leader 门禁；多同名全移并在返回里说明）"""
    window_id = tool_ctx.get("team_window_id", "")
    if not window_id:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")
    agent_name = str(agent_name or "").strip()
    if not agent_name:
        return ToolResult(False, error="agent_name 不能为空")

    tm = _get_team_manager()
    gate = _leader_gate(tm, window_id)
    if gate is not None:
        return gate
    mg = _get_team_manage()
    if mg is None:
        return ToolResult(False, error="团队管理能力不可用（缺少 team.manage 能力声明）")

    # 防呆：不能移除自己（退出用 team_manage(action=leave)）
    try:
        if tm.get_member_agent_name(window_id) == agent_name:
            return ToolResult(False, error="不能移除自己；如需退出请用 team_manage(action=leave)")
    except Exception:
        pass

    removed = mg.remove_member(agent_name=agent_name)
    if not removed:
        return ToolResult(False, error=f"未找到名为「{agent_name}」的团队成员")

    lines = [f"已移除 {len(removed)} 个成员窗口：" if len(removed) > 1 else "已移除成员："]
    for r in removed:
        lines.append(f"  - {r['agent_name']}@{r['window_id']}")
    try:
        new_leader = str(tm.get_leader_window_id() or "")
        if new_leader and new_leader != window_id:
            lines.append(f"leader 已自动接任: {tm.get_member_agent_name(new_leader) or '?'}@{new_leader}")
    except Exception:
        pass
    return ToolResult(True, content="\n".join(lines))


# ============================================================
# schema
# ============================================================

_TEAM_SEND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_send_message",
        "description": "向团队中的智能体发送任务邮件（团队成员与非团队成员均可发送；对方会串行处理任务并自动开始执行）。非团队成员发送时标识为 user；成员无法直接回复外部窗口，执行状态用 team_manage(action=list) 查看。群发为 leader 专属。",
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": "目标成员标识。单个成员名（string，任何成员可用，可私聊任意队友）或成员名数组/[\"*\"]（群发，需 leader，[\"*\"] 表示全队并自动跳过你自己）。单个支持 agent_name 或 agent_name@window_id，通过 team_manage(action=list) 查看",
                },
                "message": {"type": "string", "description": "消息内容"},
            },
            "required": ["to_agent", "message"],
        },
    },
}


_TEAM_MANAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_manage",
        "description": (
            "团队管理统一入口（action 分发）。"
            "list=查看团队成员（任何窗口可用，非团队成员也可只读查看，返回 agent_name@window_id 标识）；"
            "create=创建团队（任何窗口可用；team_name/team_role 必填，members 可选）。"
            "leader 与成员全部新建窗口（leader 角色自动创建），当前窗口保持非团队成员、不切换标签页；"
            "create 后即可用 team_send_message 给成员派任务，成员自动开始执行；"
            "add=拉成员入队，提交开窗请求本轮结束后由主线程开窗（须已在团队中，成员可调用无需 leader；"
            "agent_name/role 必填，下一轮用 list 验证）；"
            "remove=将成员移出团队（仅 leader；agent_name 必填，同名全部移除，不能移除自己）；"
            "leave=退出当前团队（须已在团队中；leader 退出时自动移交）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "add", "remove", "leave"],
                    "description": "操作类型",
                },
                "team_name": {"type": "string", "description": "action=create 必填：团队名（对外展示标签）"},
                "team_role": {"type": "string", "description": "action=create 必填：团队定位（一句话职责与目标）"},
                "members": {
                    "type": "array",
                    "description": "action=create 可选：初始成员角色清单 [{agent_name, description}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_name": {"type": "string", "description": "角色名（系统中存在的子智能体名）"},
                            "description": {"type": "string", "description": "该角色在团队中的职责描述"},
                        },
                        "required": ["agent_name"],
                    },
                },
                "agent_name": {"type": "string", "description": "action=add/remove 必填：角色名（系统中存在的子智能体名）"},
                "role": {"type": "string", "description": "action=add 必填：该成员在本次任务中的具体分工，会用于权限推导与上下文注入"},
            },
            "required": ["action"],
        },
    },
}


# ============================================================
# impl
# ============================================================


def _team_send_impl(tool_ctx, **kwargs):
    return _team_send_message(tool_ctx, kwargs.get("to_agent", ""), kwargs.get("message", ""))


def _team_manage_list(tool_ctx):
    """team_manage(action="list")：任何窗口可调（含非团队成员只读查看）。

    非团队成员（例如 team_manage(create) 发起组队的原窗口）也能查看团队
    全部成员与开窗回执——发送任务后靠它跟踪执行状态。
    """
    tm = _get_team_manager()
    wid = _resolve_window_id(tool_ctx)
    try:
        if not wid:
            return ToolResult(True, content='无法定位当前窗口；请直接使用 team_manage(action="create") 创建团队。')
        if not tm.is_team_member(wid):
            if not tm.get_members():
                return ToolResult(
                    True,
                    content='当前没有团队成员。用 team_manage(action="create", team_name=..., team_role=...) 创建团队。',
                )
            # 非成员只读视角：复用成员列表渲染（_team_list_members 内部按
            # 「不在成员表」走全量只读分组），窗口身份透传用于开窗回执查询。
            _ctx = dict(tool_ctx)
            _ctx["team_window_id"] = wid
            return _team_list_members(_ctx)
    except Exception:
        pass
    return _team_list_members(tool_ctx)


def _team_manage_impl(tool_ctx, **kwargs):
    """team_manage 统一入口：按 action 分发到内部 impl（权限分层见各函数）。"""
    action = str(kwargs.get("action", "") or "").strip().lower()
    if action == "list":
        return _team_manage_list(tool_ctx)
    if action == "create":
        return _team_create(tool_ctx, kwargs.get("team_name", ""), kwargs.get("team_role", ""), kwargs.get("members"))
    if action == "add":
        return _team_add_member(tool_ctx, kwargs.get("agent_name", ""), kwargs.get("role", ""))
    if action == "remove":
        return _team_remove_member(tool_ctx, kwargs.get("agent_name", ""))
    if action == "leave":
        return _team_leave(tool_ctx)
    return ToolResult(False, error=f'未知 action: {action!r}。可用: list / create / add / remove / leave')


def register(registry):
    # team_manage：team_only=False —— 非团队窗口（主窗口）可 action=create/list
    # 发起「一句话组队」，否则会被 agent.py 的 team_only schema 过滤拦在第一步；
    # 权限按 action 在 impl 内分层（add 须成员、remove 须 leader、leave 须成员）
    registry.register(
        "team_manage",
        _TEAM_MANAGE_SCHEMA,
        impl=_team_manage_impl,
        danger="safe",
        icon="团队",
        cn_name="团队管理",
        group=GROUP_TEAM,
        description="团队管理统一入口（查看/建团/拉人/踢人/退出）",
    )
    # team_send_message：team_only=False —— 非团队成员（组队发起窗口等外部
    # 会话）也要能向成员派任务，成员收到后由邮箱 watcher 自动拉起执行；
    # 群发仍由 impl 内的 leader 门禁拦截。
    registry.register(
        "team_send_message",
        _TEAM_SEND_SCHEMA,
        impl=_team_send_impl,
        danger="safe",
        icon="邮件-发送",
        cn_name="发送邮件",
        group=GROUP_TEAM,
        description="向团队成员发送任务（非成员也可发送；数组/[\"*\"] 群发仅 leader）",
    )
