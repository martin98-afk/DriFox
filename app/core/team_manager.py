# -*- coding: utf-8 -*-
"""
TeamManager — 多窗口团队协作管理（任务邮件版）

基于文件邮箱的任务分发系统。

数据结构：~/.drifox/teams/{team_name}/
  team.json              — 团队元信息 + 成员列表
  mailboxes/{window_id}/ — 每个成员的消息邮箱（JSON 文件）
"""

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def check_team_member(backend_or_window_id) -> bool:
    """检查窗口是否是团队成员（共用函數，供 ToolExecutor / chat_worker 調用）

    Args:
        backend_or_window_id: ChatBackend 实例或 window_id 字串

    Returns:
        True 若該窗口已加入團隊
    """
    try:
        if isinstance(backend_or_window_id, str):
            wid = backend_or_window_id
        else:
            wid = getattr(backend_or_window_id, "_window_id", None)
        if not wid:
            return False
        return TeamManager.get_instance().is_team_member(wid)
    except Exception:
        return False


class TeamManager:
    """团队协作管理器（单例）"""

    _instance: Optional["TeamManager"] = None
    _lock = threading.Lock()

    DEFAULT_TEAM = "default"

    def __init__(self):
        self._teams_dir = self._get_teams_dir()
        self._teams_dir.mkdir(parents=True, exist_ok=True)
        self._team_cache: Dict[str, Dict[str, Any]] = {}
        self._window_counter_file = self._teams_dir / ".window_counter"
        # 活跃窗口集合。None = 主窗口尚未同步过（状态未知），此时禁止任何清理。
        # 绝不能把「未知」和「空集合」混为一谈，否则会误删全部成员邮箱。
        self._active_window_ids: Optional[set] = None
        # 保护 _team_cache / 邮箱目录的读写（check_team_member 会在 worker 线程调用）
        self._data_lock = threading.RLock()
        self._init_team(self.DEFAULT_TEAM)

    # ── 单例 ─────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "TeamManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        cls._instance = None

    # ── 路径 ─────────────────────────────────────────

    @staticmethod
    def _get_teams_dir() -> Path:
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "teams"

    def _team_dir(self, team_name: str) -> Path:
        return self._teams_dir / team_name

    def _team_file(self, team_name: str) -> Path:
        return self._team_dir(team_name) / "team.json"

    def _mailboxes_dir(self, team_name: str) -> Path:
        return self._team_dir(team_name) / "mailboxes"

    def _mailbox_dir(self, team_name: str, window_id: str) -> Path:
        return self._mailboxes_dir(team_name) / window_id

    # ── 持久化窗口 ID ────────────────────────────────

    def generate_window_id(self) -> str:
        with self._lock:
            try:
                if self._window_counter_file.exists():
                    counter = int(self._window_counter_file.read_text().strip())
                else:
                    counter = 1
            except ValueError, FileNotFoundError:
                counter = 1

            window_id = f"win_{counter:02d}"
            self._window_counter_file.write_text(str(counter + 1))
            return window_id

    # ── 团队数据读写 ─────────────────────────────────

    def _init_team(self, team_name: str):
        team_dir = self._team_dir(team_name)
        team_dir.mkdir(parents=True, exist_ok=True)
        (team_dir / "mailboxes").mkdir(parents=True, exist_ok=True)

        team_file = self._team_file(team_name)
        if not team_file.exists():
            data = {
                "name": team_name,
                "created_at": time.time(),
                "members": {},
            }
            self._write_json(team_file, data)
            self._team_cache[team_name] = data
        else:
            self._team_cache[team_name] = self._read_json(team_file)
        # 尝试清理失效成员（活跃窗口集合未知时会自动跳过）
        self._cleanup_stale_members(team_name)

    def _get_team_data(self, team_name: str) -> Dict[str, Any]:
        """读取团队数据。

        注意：此处**不做**失效成员清理。
        清理只由 set_active_window_ids() 驱动（主窗口显式同步活跃窗口时），
        避免任意读取（如 worker 线程的 check_team_member）在活跃集合
        尚未就绪时误删全部成员及其邮箱目录。
        """
        if team_name not in self._team_cache:
            self._init_team(team_name)
        return self._team_cache[team_name]

    def _save_team_data(self, team_name: str):
        if team_name in self._team_cache:
            self._write_json(self._team_file(team_name), self._team_cache[team_name])

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError, json.JSONDecodeError:
            return {}

    @staticmethod
    def _write_json(path: Path, data: Any):
        """原子写入 JSON：先写同目录临时文件，再 os.replace 原子替换。

        🛡️ F5（T4 Bug6）：原实现直接 open(path, "w") 非原子——并发读取方
        （get_mailbox_mails / get_pending_tasks 在 worker 线程调用）可能在写入
        中间读到半截/损坏的 JSON（JSONDecodeError → 空 dict → 邮件状态丢失）。
        临时文件与目标同目录保证 os.replace 原子（同文件系统）；写完立即替换，
        无 tmp 残留。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            # 异常路径清理临时文件，避免残留
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    # ── 成员清理 ─────────────────────────────────────

    def set_active_window_ids(self, window_ids: set):
        """由主窗口同步当前真实的活跃窗口 ID 集合（跨 OpenAIChatToolWindow._instances 收集）

        🛡️ 空集合表示活跃窗口信息不可靠（窗口 __init__ 阶段自身还未注册进
        _instances、创建/销毁时序竞态等），既不覆盖已知的有效集合，也不触发清理。
        否则会把所有成员误判为失效并 rmtree 其邮箱目录 —— 这正是
        QFileSystemWatcher 报 FindNextChangeNotification failed + 成员无法交互的根因。
        """
        ids = set(window_ids or ())
        if not ids:
            return
        with self._data_lock:
            self._active_window_ids = ids
            # 同步清理所有团队中的失效成员
            for team_name in list(self._team_cache.keys()):
                self._cleanup_stale_members(team_name)

    def _get_active_windows(self) -> Optional[set]:
        """获取当前活跃窗口 ID 集合；无法确认时返回 None（调用方应保守跳过清理）"""
        if self._active_window_ids:
            return set(self._active_window_ids)
        # 兜底：读文件（历史上从未写入，保留兼容；文件不存在视为"未知"而非"空"，
        # 避免把所有成员误判为失效而全删邮箱目录）
        active_file = self._teams_dir / ".active_windows"
        try:
            if active_file.exists():
                data = json.loads(active_file.read_text())
                if data:
                    return set(data)
        except json.JSONDecodeError, FileNotFoundError, OSError, TypeError:
            pass
        return None

    def _cleanup_stale_members(self, team_name: str = DEFAULT_TEAM):
        """彻底清理失效成员：移除 member 记录 + 删除邮箱目录

        🛡️ 安全保护：活跃窗口集合为空/未知时跳过清理。
        历史 bug：窗口创建/销毁时序中 _active_window_ids 可能短暂为空集，
        导致所有成员被误判为失效，邮箱目录（含 QFileSystemWatcher 监视中的）
        被全部删除，成员无法交互。
        """
        active = self._get_active_windows()
        if not active:
            # 活跃窗口信息未知（未同步 / 空集），保守跳过清理，避免误删邮箱目录
            return

        with self._data_lock:
            data = self._team_cache.get(team_name)
            if not data:
                return
            members = data.get("members", {})
            if not members:
                return

            stale_ids = [wid for wid in members if wid not in active]

            for wid in stale_ids:
                members.pop(wid, None)
                self._remove_mailbox_dir(team_name, wid)

            # 清理孤立邮箱目录：必须同时满足「无 member 记录」且「窗口不活跃」。
            # 只判 members 会误删刚 mkdir、member 记录尚未落库的活跃窗口邮箱，
            # 而该目录正被 QFileSystemWatcher 监视 → FindNextChangeNotification failed。
            mailboxes_dir = self._mailboxes_dir(team_name)
            if mailboxes_dir.exists():
                try:
                    children = list(mailboxes_dir.iterdir())
                except OSError:
                    children = []
                for child in children:
                    if child.is_dir() and child.name not in members and child.name not in active:
                        self._remove_mailbox_dir(team_name, child.name)

            if stale_ids:
                data["members"] = members
                self._save_team_data(team_name)

    def _remove_mailbox_dir(self, team_name: str, window_id: str):
        """删除某个窗口的邮箱目录（容错，失败不抛异常）"""
        mailbox_dir = self._mailbox_dir(team_name, window_id)
        if mailbox_dir.exists():
            try:
                shutil.rmtree(str(mailbox_dir), ignore_errors=True)
            except Exception:
                pass

    # ── 成员管理 ─────────────────────────────────────

    def join_team(self, window_id: str, agent_name: str, team_name: str = DEFAULT_TEAM):
        """窗口加入团队

        🛡️ F5（T4 Bug6）：read-modify-write 全程持 _data_lock，防止多窗口并发
        join 时成员记录互相覆盖（lost update）。_data_lock 为 RLock，内部
        _get_team_data → _init_team → _cleanup_stale_members 的重入安全。

        🛡️ F9（能力自动登记）：join 时从 AgentManager 动态解析 agent 能力，
        写入成员 capability 快照（leader 派单前零手工可见成员能力）。延迟 import
        AgentManager（agent.py:701 反向引用 team_manager，顶层 import 会循环依赖）。

        ⚠️ F3 注意：后续任务 F3（手动成员快照）会在本方法基础上叠加
        join_team 写顶层 team_members，改动点即下方 `data["members"][window_id]`
        赋值处——请保持本方法结构不变，在其上扩展。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            data.setdefault("members", {})
            member = {
                "window_id": window_id,
                "agent_name": agent_name,
                "joined_at": time.time(),
            }
            # 🛡️ F9：capability 自动登记（动态解析，失败静默降级为无快照——
            # get_member_capability 会在无快照时重新动态解析，不破坏老数据）
            try:
                from app.core.agent import AgentManager  # 延迟 import 防循环依赖

                am = AgentManager.get_instance()
                agent = am.get_agent(agent_name) if am else None
                if agent is not None:
                    member["capability"] = self._derive_capability(agent)
            except Exception:
                pass
            data["members"][window_id] = member
            # 🛡️ F3（T2-P3 第 1 层）：顶层 team_members 快照——手动 /team --join
            # 的成员也持久化到 team.json 顶层（不被 _cleanup_stale_members 清理），
            # 恢复会话时即使成员无历史会话也能找回。dict: agent_name → 来源标记。
            data.setdefault("team_members", {})
            existing = data["team_members"].get(agent_name) or {}
            data["team_members"][agent_name] = {
                "source": existing.get("source", "manual"),
                "joined_at": existing.get("joined_at", time.time()),
            }
            self._save_team_data(team_name)
        self._mailbox_dir(team_name, window_id).mkdir(parents=True, exist_ok=True)

    def leave_team(self, window_id: str, team_name: str = DEFAULT_TEAM):
        """窗口离开团队（同时清理邮箱目录）

        🛡️ F5（T4 Bug6）：成员移除 + 落盘持 _data_lock，防止与并发 join /
        _cleanup_stale_members 互相覆盖；邮箱目录 rmtree 放锁外（幂等容错）。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            data.setdefault("members", {})
            if window_id in data["members"]:
                data["members"].pop(window_id, None)
                self._save_team_data(team_name)

        # 清理邮箱目录
        mailbox_dir = self._mailbox_dir(team_name, window_id)
        if mailbox_dir.exists():
            try:
                shutil.rmtree(str(mailbox_dir))
            except Exception:
                pass

    def get_members(self, team_name: str = DEFAULT_TEAM) -> List[Dict[str, Any]]:
        data = self._get_team_data(team_name)
        return list(data.get("members", {}).values())

    def get_member_by_agent(self, agent_name: str, team_name: str = DEFAULT_TEAM) -> Optional[Dict[str, Any]]:
        """通过智能体名查找成员（唯一匹配时返回；多个匹配返回第一个活跃的）"""
        members = self.get_members(team_name)
        matches = [m for m in members if m.get("agent_name") == agent_name]
        if not matches:
            return None
        # 多个同名时，返回第一个
        return matches[0]

    def find_member(self, identifier: str, team_name: str = DEFAULT_TEAM) -> Optional[Dict[str, Any]]:
        """通过标识符查找成员。

        支持格式：
        - agent_name：按 agent 名查找（单匹配直接返回，多匹配返回第一个）
        - agent_name@window_id：精确匹配某个窗口的成员
        """
        members = self.get_members(team_name)
        if "@" in identifier:
            # 精确匹配：agent@window_id
            parts = identifier.rsplit("@", 1)
            agent = parts[0]
            wid = parts[1]
            for m in members:
                if m.get("agent_name") == agent and m.get("window_id") == wid:
                    return m
            return None

        # 按 agent 名匹配
        return self.get_member_by_agent(identifier, team_name)

    def is_team_member(self, window_id: str, team_name: str = DEFAULT_TEAM) -> bool:
        data = self._get_team_data(team_name)
        return window_id in data.get("members", {})

    # ── 成员能力（F9：能力自动登记 + 派单前可见）──────────

    # task_tags 关键词推导规则（复用 team.md 权限推导关键词表）：
    # 编码/实现/builder→implement；规划/plan/只读→plan；审查/review/审计→review；
    # 诊断/debug/修复→diagnose；探索/explore→explore；统筹/leader→lead；
    # 默认按 can_write 兜底（可写→implement，否则按 role_desc）。
    _CAPABILITY_TAG_KEYWORDS = {
        "implement": ("编码", "实现", "builder", "开发", "写代码", "implement", "落地", "构建"),
        "plan": ("规划", "方案", "计划", "蓝图", "plan", "只读", "分析路径", "建模"),
        "review": ("审查", "审计", "review", "评审", "质检", "检查", "reviewer", "audit"),
        "diagnose": ("诊断", "debug", "修复", "复现", "定位根因", "bug", "回归"),
        "explore": ("探索", "explore", "理解代码", "调研", "调查", "扫描", "梳理"),
        "lead": ("统筹", "leader", "协调", "分发", "汇总", "监控进度", "lead"),
    }

    @staticmethod
    def _derive_capability(agent) -> Dict[str, Any]:
        """从 Agent 对象推导能力摘要（join 快照 + 动态解析共用）。

        Args:
            agent: app.core.agent.Agent 实例（name/description/mode/permission/tools）

        Returns:
            {
                "agent_name", "role_desc"(Agent.description), "mode",
                "permissions"(原始 permission dict),
                "can_write"(write/edit/multi_edit 是否 allow),
                "can_bash", "can_team"(question/team_* allow),
                "task_tags"(关键词推导的标签列表)
            }
        """
        name = str(getattr(agent, "name", "") or "")
        desc = str(getattr(agent, "description", "") or "")
        mode = getattr(agent, "mode", None)
        permission = dict(getattr(agent, "permission", None) or {})
        tools = dict(getattr(agent, "tools", None) or {})

        def _is_allowed(tool: str) -> bool:
            """判定工具是否 allow：agent.tools（白名单 dict）优先，permission 其次。"""
            tool_lower = tool.lower()
            if tools:
                # tools 白名单语义：tools 非空时未列出的工具全部拒绝
                return bool(tools.get(tool_lower, False))
            val = permission.get(tool_lower)
            if val is None:
                val = permission.get("*", "allow")
            return str(val).lower() == "allow"

        can_write = all(_is_allowed(t) for t in ("write", "edit", "multi_edit"))
        can_bash = _is_allowed("bash")
        can_team = all(_is_allowed(t) for t in ("question", "team_send_message", "team_list_members"))

        # task_tags：按关键词表匹配（name + description 全文），无匹配时按 can_write 兜底
        text = f"{name} {desc}".lower()
        tags: List[str] = []
        for tag, keywords in TeamManager._CAPABILITY_TAG_KEYWORDS.items():
            if any(k.lower() in text for k in keywords):
                tags.append(tag)
        if not tags:
            tags.append("implement" if can_write else "plan")

        return {
            "agent_name": name,
            "role_desc": desc,
            "mode": mode,
            "permissions": permission,
            "can_write": can_write,
            "can_bash": can_bash,
            "can_team": can_team,
            "task_tags": tags,
        }

    def get_member_capability(self, agent_name: str, team_name: str = DEFAULT_TEAM) -> Optional[Dict[str, Any]]:
        """获取成员能力：动态解析优先（读 AgentManager），join 快照兜底。

        🛡️ F9：老 team.json members 无 capability 字段 / agent 文件被删除时，
        用 .get() 兜底动态解析，不破坏老数据；动态解析失败返回 None（调用方
        显示"未知"）。
        """
        if not agent_name:
            return None
        # 1. 动态解析优先（agent 文件存在时能力最新）
        try:
            from app.core.agent import AgentManager  # 延迟 import 防循环依赖

            am = AgentManager.get_instance()
            agent = am.get_agent(agent_name) if am else None
            if agent is not None:
                return self._derive_capability(agent)
        except Exception:
            pass
        # 2. join 快照兜底（agent 文件删除 / 动态解析异常时用登记时的快照）
        with self._data_lock:
            data = self._get_team_data(team_name)
            for member in (data.get("members") or {}).values():
                if member.get("agent_name") == agent_name and member.get("capability"):
                    return dict(member["capability"])
        return None

    def match_members(self, task_type: str, team_name: str = DEFAULT_TEAM) -> List[Dict[str, Any]]:
        """按任务类型匹配成员：返回 task_tags 含该类型的成员列表（含 capability）。

        供 leader 派单前筛选可用成员。task_type 如 "implement"/"plan"/"review"/
        "diagnose"/"explore"/"lead"。无匹配返回空列表。
        """
        results: List[Dict[str, Any]] = []
        with self._data_lock:
            data = self._get_team_data(team_name)
            for member in (data.get("members") or {}).values():
                cap = member.get("capability") or {}
                tags = cap.get("task_tags") or []
                if task_type in tags:
                    results.append(dict(member))
        return results

    def get_team_member_snapshot(self, team_name: str = DEFAULT_TEAM) -> List[str]:
        """获取团队成员快照（去重有序）：模板 agents ∪ 手动 join 快照 team_members。

        🛡️ F3（T2-P3 第 1 层）：手动 /team --join 的成员记录在 team.json 顶层
        team_members 键（不受 _cleanup_stale_members 清理、不依赖当前活跃窗口），
        恢复会话时用本方法找回"无会话记录但确实加入过"的成员。

        Returns:
            agent_name 列表（模板 agents 在前，手动快照补充，去重）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            names: List[str] = []
            # 1. 模板 agents（模板定义的角色优先）
            template = data.get("template") or {}
            for item in template.get("agents") or []:
                if isinstance(item, dict) and item.get("agent_name"):
                    name = str(item["agent_name"]).strip()
                    if name and name not in names:
                        names.append(name)
            # 2. 手动 join 快照（补充模板之外的成员）
            for name in data.get("team_members") or {}:
                name = str(name).strip()
                if name and name not in names:
                    names.append(name)
            return names

    # ── 团队模板上下文 ────────────────────────────────

    def set_template(self, template_info: dict, team_name: str = DEFAULT_TEAM):
        """设置当前团队的模板上下文（/team --load=<name> 加载模板时写入）

        Args:
            template_info: {"name": ..., "description": ..., "agents": [...]}
                agents 每项为 {"agent_name": ..., "description": ...}（角色描述可为空，
                兼容旧模板 / 手动加入成员）
                供 SessionStart hook 注入团队描述 + 各成员角色描述
        """
        data = self._get_team_data(team_name)
        data["template"] = template_info
        self._save_team_data(team_name)

    def get_template(self, team_name: str = DEFAULT_TEAM) -> Optional[Dict[str, Any]]:
        """获取当前团队的模板上下文（无模板时返回 None）"""
        data = self._get_team_data(team_name)
        return data.get("template")

    # ── 团队运行标识（run_id，方案 A 团队会话恢复）──

    def start_team_run(self, team_name: str = DEFAULT_TEAM, force: bool = False) -> str:
        """开始一次团队运行：生成并持久化 run_id（幂等）

        方案 A 团队会话恢复：每次 /team --load 开始一次团队运行时，
        生成 uuid4 写入 team.json **顶层**（与 members 平级）——
        而非成员级字段。这样 _cleanup_stale_members 清理失效成员时
        只动 members，不会丢失 run_id，同一团队的所有成员共享同一 run_id。

        幂等语义：团队已存在 run_id 时直接复用（避免 /team --load 重复
        触发时刷新运行标识，导致历史会话与当前运行脱钩）；无 run_id 时
        生成新值并落盘。

        Args:
            force: True 时无条件生成新 run_id 并落盘（用于"一键恢复"——
                恢复是一次新的团队运行，必须与历史 run_id 区分，
                否则恢复出的新会话仍归属旧 run_id，导致历史面板
                分组/恢复再次串台）。

        Returns:
            run_id（uuid4 hex 字符串）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            run_id = data.get("run_id")
            if force or not run_id:
                run_id = uuid.uuid4().hex
                data["run_id"] = run_id
                self._save_team_data(team_name)
            return run_id

    def get_team_run_id(self, team_name: str = DEFAULT_TEAM) -> str:
        """获取当前团队的 run_id（未开始团队运行 / 老团队无 run_id 时返回空串）"""
        with self._data_lock:
            data = self._get_team_data(team_name)
            return data.get("run_id", "") or ""

    # ── 团队级统一项目（一人改项目全员同步）──

    def set_team_project(self, project: str, team_name: str = DEFAULT_TEAM):
        """设置团队级统一项目（写入 team.json **顶层**，与 run_id 平级）

        项目是团队共享状态：任一成员切换项目时写入，供同团队其他成员
        同步应用与恢复路径读取。放顶层而非成员级——_cleanup_stale_members
        清理失效成员时只动 members，不会丢失 project。

        Args:
            project: 项目名（空串表示清除）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            if data.get("project") == project:
                return
            data["project"] = project
            self._save_team_data(team_name)

    def get_team_project(self, team_name: str = DEFAULT_TEAM) -> str:
        """获取团队级统一项目（未设置 / 老团队无 project 时返回空串）"""
        with self._data_lock:
            data = self._get_team_data(team_name)
            return data.get("project", "") or ""

    # ── 邮件系统 ─────────────────────────────────────

    def _next_mail_id(self) -> str:
        return f"mail_{int(time.time() * 1000)}_{os.urandom(3).hex()}"

    def send_task(
        self,
        from_window: str,
        from_agent: str,
        to_identifier: str,  # agent_name 或 agent_name@window_id
        task_description: str,
        team_name: str = DEFAULT_TEAM,
    ) -> Optional[str]:
        """发送任务邮件给队友

        Returns:
            邮件 ID，失败返回 None
        """
        target = self.find_member(to_identifier, team_name)
        if not target:
            return None

        mail_id = self._next_mail_id()
        mail = {
            "id": mail_id,
            "type": "task",
            "team_name": team_name,  # 🛡️ F5（T4 Bug12）：邮件归属团队，多团队场景可追溯
            "from_window": from_window,
            "from_agent": from_agent,
            "to_window": target["window_id"],
            "to_agent": target["agent_name"],
            "subject": task_description[:80],
            "body": task_description,
            "status": "pending",
            "result": "",
            "created_at": time.time(),
        }

        mailbox_dir = self._mailbox_dir(team_name, target["window_id"])
        mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(mailbox_dir / f"{mail_id}.json", mail)
        return mail_id

    def send_reply(
        self,
        original_mail_id: str,
        from_window: str,
        from_agent: str,
        to_window: str,
        to_agent: str,
        result: str,
        success: bool = True,
        team_name: str = DEFAULT_TEAM,
    ) -> Optional[str]:
        """发送回复邮件"""
        mail_id = self._next_mail_id()
        mail = {
            "id": mail_id,
            "type": "reply",
            "team_name": team_name,  # 🛡️ F5（T4 Bug12）：邮件归属团队，多团队场景可追溯
            "reply_to": original_mail_id,
            "from_window": from_window,
            "from_agent": from_agent,
            "to_window": to_window,
            "to_agent": to_agent,
            "subject": f"Re: 任务结果 ({'完成' if success else '失败'})",
            "body": result,
            "status": "done",
            "result": result,
            "success": success,
            "created_at": time.time(),
        }

        mailbox_dir = self._mailbox_dir(team_name, to_window)
        mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(mailbox_dir / f"{mail_id}.json", mail)
        return mail_id

    def get_mailbox_mails(self, window_id: str, team_name: str = DEFAULT_TEAM) -> List[Dict[str, Any]]:
        """获取窗口邮箱中的所有邮件（按创建时间排序）"""
        mailbox_dir = self._mailbox_dir(team_name, window_id)
        if not mailbox_dir.exists():
            return []

        mails = []
        for f in sorted(mailbox_dir.glob("mail_*.json")):
            mail = self._read_json(f)
            if mail:
                mails.append(mail)
        return mails

    def get_pending_tasks(self, window_id: str, team_name: str = DEFAULT_TEAM) -> List[Dict[str, Any]]:
        """获取该成员待处理的任务邮件（串行：只取最早的一个 pending）"""
        mails = self.get_mailbox_mails(window_id, team_name)
        pending = [m for m in mails if m.get("type") == "task" and m.get("status") == "pending"]
        return pending[:1]  # 串行处理

    def get_running_tasks(self, window_id: str, team_name: str = DEFAULT_TEAM) -> List[Dict[str, Any]]:
        """获取该成员正在运行的任务邮件"""
        mails = self.get_mailbox_mails(window_id, team_name)
        return [m for m in mails if m.get("type") == "task" and m.get("status") == "running"]

    def get_member_busy_status(self, window_id: str, team_name: str = DEFAULT_TEAM) -> str:
        """返回成员的工作状态: 'busy'（有 running/pending 任务）| 'idle'（空闲）"""
        mails = self.get_mailbox_mails(window_id, team_name)
        for m in mails:
            if m.get("type") == "task" and m.get("status") in ("running", "pending"):
                return "busy"
        return "idle"

    def mark_mail_running(self, mail_id: str, window_id: str, team_name: str = DEFAULT_TEAM):
        self._update_mail_status(mail_id, window_id, "running", team_name)

    def mark_mail_done(self, mail_id: str, window_id: str, result: str, team_name: str = DEFAULT_TEAM):
        self._update_mail_status(mail_id, window_id, "done", team_name, result)

    def mark_mail_pending(self, mail_id: str, window_id: str, team_name: str = DEFAULT_TEAM):
        """将邮件状态回滚为 pending 并清空 result（修复 T23：收尾不误标 done）。

        done 是终态（get_pending_tasks 只取 status=="pending"，:470），流式收尾
        阶段被注入但未被 LLM 实际处理的邮件若被 mark_mail_done 会永久丢失。
        收尾检测到邮件未被响应时调用本方法回滚 pending，由流结束后的
        _check_and_process_pending 重新排队处理。

        🛡️ F5（T4 Bug6）：read-modify-write 持 _data_lock，防止与并发
        mark_mail_done / mark_mail_running 互相覆盖（lost update 导致
        T23 回滚失效、邮件永久丢失）。
        """
        with self._data_lock:
            mailbox_dir = self._mailbox_dir(team_name, window_id)
            mail_file = mailbox_dir / f"{mail_id}.json"
            if mail_file.exists():
                mail = self._read_json(mail_file)
                if mail:
                    mail["status"] = "pending"
                    mail["result"] = ""
                    self._write_json(mail_file, mail)

    def _update_mail_status(
        self,
        mail_id: str,
        window_id: str,
        status: str,
        team_name: str = DEFAULT_TEAM,
        result: str = "",
    ):
        """更新邮件状态（mark_mail_running/done 共用）。

        🛡️ F5（T4 Bug6）：read-modify-write 持 _data_lock，防止多线程
        （main_widget 主线程 + worker 线程）并发写同一邮件文件互相覆盖。
        """
        with self._data_lock:
            mailbox_dir = self._mailbox_dir(team_name, window_id)
            mail_file = mailbox_dir / f"{mail_id}.json"
            if mail_file.exists():
                mail = self._read_json(mail_file)
                if mail:
                    mail["status"] = status
                    if result:
                        mail["result"] = result
                    self._write_json(mail_file, mail)
