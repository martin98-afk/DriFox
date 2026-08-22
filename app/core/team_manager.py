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
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


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
        # 🛡️ T26（U3）：延迟保存挂起集合（_save_team_data(save_now=False) 标记，
        # flush_pending_saves() 批量落盘）
        self._pending_saves: set = set()
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

    def _save_team_data(self, team_name: str, save_now: bool = True):
        """保存团队数据。

        🛡️ T26（U3）：新增批量/延迟保存支持——save_now=False 时仅挂起
        （标记 pending，不落盘），由 flush_pending_saves() 显式批量落盘，
        供上层合并多次写盘为一次（如解散流程连续 leave_team）。默认
        save_now=True 行为与旧版完全一致（立即原子写盘），不破坏现有调用。
        """
        if team_name not in self._team_cache:
            return
        if save_now:
            self._write_json(self._team_file(team_name), self._team_cache[team_name])
        else:
            with self._data_lock:
                self._pending_saves.add(team_name)

    def flush_pending_saves(self):
        """批量落盘所有挂起的团队数据（合并多次写盘为一次）。

        🛡️ T26（U3）：与 _save_team_data(save_now=False) 配套使用。

        🛡️ W3b-R2（devil-advocate 挑刺）：写盘必须持 _data_lock——原先取
        快照后锁外读 `_team_cache[team_name]` 写盘，与 join_team 持锁内
        修改同一 dict + 锁内写盘存在竞态：flush dump 期间 join 修改 dict
        → flush 用旧/半新状态覆盖 join 的新写（状态丢失）。json.dump+
        os.replace 为毫秒级，且 join/leave 本来就持锁写盘，持锁语义一致。
        """
        with self._data_lock:
            pending = set(self._pending_saves)
            self._pending_saves.clear()
            for team_name in pending:
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

    def set_active_window_ids(self, window_ids: set, team_name: Optional[str] = None):
        """由主窗口同步当前真实的活跃窗口 ID 集合（跨 OpenAIChatToolWindow._instances 收集）

        🛡️ 空集合表示活跃窗口信息不可靠（窗口 __init__ 阶段自身还未注册进
        _instances、创建/销毁时序竞态等），既不覆盖已知的有效集合，也不触发清理。
        否则会把所有成员误判为失效并 rmtree 其邮箱目录 —— 这正是
        QFileSystemWatcher 报 FindNextChangeNotification failed + 成员无法交互的根因。

        🛡️ T26（U3）：新增可选参数 team_name 限缩清理范围——
        传 None（默认，向后兼容）时遍历清理所有团队，行为与旧版完全一致；
        传 team_name 时只清理目标团队，跳过其他团队扫描（解散 A 团队时
        不再扫 B/C 团队，团队越多收益越大——根因 1 补充）。
        """
        ids = set(window_ids or ())
        if not ids:
            return
        with self._data_lock:
            self._active_window_ids = ids
            if team_name is not None:
                # 限缩：只清理目标团队，跳过其他团队扫描
                self._cleanup_stale_members(team_name)
            else:
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

    def _cleanup_team_members_snapshot(self, data: dict, active: set) -> bool:
        """清理顶层 team_members 快照中失效窗口记录（T18 兜底）。

        仅清理新格式记录（key=window_id，value 为含 agent_name 字段的 dict）；
        老格式记录（key=agent_name，value 无 agent_name 字段）保留——老格式
        本身按 agent_name 覆盖写不累积、且需兼容读取，不参与 window 维度清理。

        Args:
            data: 团队数据 dict（原地修改）
            active: 当前活跃窗口 ID 集合

        Returns:
            bool: 是否有变更（调用方决定是否落盘）
        """
        tm_snap = data.get("team_members") or {}
        stale_keys = [
            key for key, val in tm_snap.items() if isinstance(val, dict) and val.get("agent_name") and key not in active
        ]
        if not stale_keys:
            return False
        for key in stale_keys:
            tm_snap.pop(key, None)
        data["team_members"] = tm_snap
        return True

    def _cleanup_stale_members(self, team_name: str = DEFAULT_TEAM):
        """彻底清理失效成员：移除 member 记录 + 删除邮箱目录

        🛡️ 安全保护：活跃窗口集合为空/未知时跳过清理。
        历史 bug：窗口创建/销毁时序中 _active_window_ids 可能短暂为空集，
        导致所有成员被误判为失效，邮箱目录（含 QFileSystemWatcher 监视中的）
        被全部删除，成员无法交互。

        🛡️ T18：同步清理顶层 team_members 快照中失效窗口记录（T3 key 改为
        window_id 后只增不删 → 快照累积 → 恢复窗口数线性膨胀）。members 为
        空时也执行快照清理（解散团队后残留 wid 会在下次恢复前被清除）。
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
                if self._cleanup_team_members_snapshot(data, active):
                    self._save_team_data(team_name)
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
            # 🛡️ T18：失效窗口的顶层 team_members 快照同步清理（兜底——
            # 异常关闭未走 leave_team 的窗口 wid 会残留，快照累积膨胀）。
            tm_changed = self._cleanup_team_members_snapshot(data, active)
            if stale_ids or tm_changed:
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

    def join_team(
        self,
        window_id: str,
        agent_name: str,
        team_name: str = DEFAULT_TEAM,
        run_id: str = "",
        team_label: str = "",
    ):
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

        🛡️ M1（多团队归属）：run_id / team_label 为空时不写字段（兼容老调用），
        非空时写入 `data["members"][wid]` 与 `data["team_members"][wid]` 两处
        快照，确保 _cleanup_stale_members 清理后存活成员仍可追溯归属（top-level
        team_members 不被清理）。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            data.setdefault("members", {})
            member = {
                "window_id": window_id,
                "agent_name": agent_name,
                "joined_at": time.time(),
            }
            # 🛡️ M1：多团队归属（仅显式传入时写入；空串保留为未归属，便于
            # 旧调用方/无 run 场景的成员继续按"本团队"规则分组）。
            if run_id:
                member["run_id"] = run_id
            if team_label:
                member["team_label"] = team_label
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
            # 恢复会话时即使成员无历史会话也能找回。
            # T3：key 从 agent_name 改为 window_id——同角色多成员（如两个 build）
            # 各占一条记录，不再互相覆盖；记录内显式存 agent_name 字段。
            data.setdefault("team_members", {})
            existing = data["team_members"].get(window_id) or {}
            snap_entry = {
                "agent_name": agent_name,
                "source": existing.get("source", "manual"),
                "joined_at": existing.get("joined_at", time.time()),
            }
            # 🛡️ M1：顶层快照同步归属字段，便于恢复路径（rebuild/join 链）回填。
            if run_id:
                snap_entry["run_id"] = run_id
            if team_label:
                snap_entry["team_label"] = team_label
            data["team_members"][window_id] = snap_entry
            self._save_team_data(team_name)
        self._mailbox_dir(team_name, window_id).mkdir(parents=True, exist_ok=True)

    def leave_team(self, window_id: str, team_name: str = DEFAULT_TEAM, save_now: bool = True):
        """窗口离开团队（同时清理邮箱目录）

        🛡️ F5（T4 Bug6）：成员移除 + 落盘持 _data_lock，防止与并发 join /
        _cleanup_stale_members 互相覆盖；邮箱目录 rmtree 放锁外（幂等容错）。

        🛡️ T26（U3）：邮箱目录 rmtree 改为后台线程执行——团队运行越久
        邮件越多，Windows 上逐文件删除 + 杀软扫描可耗时数百毫秒~秒级，
        原同步删除会阻塞 UI 主线程（解散团队卡顿根因 3）。rmtree 幂等
        （ignore_errors=True）、下次 join 前 mkdir(parents=True, exist_ok=True)
        幂等，后台删除安全；内存态成员清理照旧即时完成。

        🛡️ W3b-1（W2 联调审查问题 2 + devil-advocate U3-Y1）：新增 save_now
        透传——批量解散场景（循环调用 leave_team）可传 save_now=False 挂起
        落盘，循环结束后统一 flush_pending_saves() 合并为一次写盘（n 次
        json.dump+os.replace → 1 次）。默认 True 保持向后兼容，所有现有
        调用点行为完全不变（立即写盘）。save_now=False 时：
        - 内存态成员/快照清理照旧即时完成（延迟的只是落盘）
        - 落盘挂起到 _pending_saves（U3 机制），由 flush_pending_saves() 批量落盘
        - 中途异常也不丢团队状态：内存态已更新（下次 flush/join 前的
          _get_team_data 读到最新）；flush 时以 _team_cache 为准全量写盘
          （写的是最新内存态，而非各次 leave 的中间态）
        ⚠️ 契约：save_now=False 的调用方**必须随后调用 flush_pending_saves()**，
        否则该团队的解散/成员变更不会落盘（崩溃丢状态）。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            data.setdefault("members", {})
            if window_id in data["members"]:
                data["members"].pop(window_id, None)
                # 🛡️ T18：同步清理该窗口的顶层 team_members 快照记录。
                # T3 将快照 key 改为 window_id 后只增不删（leave/_cleanup 只清
                # members）→ 恢复路径每次 join 新窗口 wid 累积旧记录 → 快照
                # 计数膨胀 → 恢复窗口数随恢复次数线性膨胀（2→4→6…）。
                # leave 即该窗口退出团队，快照记录同步删除，恢复窗口数收敛。
                tm_snap = data.get("team_members") or {}
                if window_id in tm_snap:
                    tm_snap.pop(window_id, None)
                    data["team_members"] = tm_snap
                self._save_team_data(team_name, save_now=save_now)

        # 清理邮箱目录（后台线程，主线程不再同步阻塞在磁盘删除上）
        mailbox_dir = self._mailbox_dir(team_name, window_id)
        if mailbox_dir.exists():
            self._rmtree_async(mailbox_dir)

    @staticmethod
    def _rmtree_async(path: Path):
        """后台线程删除目录树（幂等容错），不阻塞调用线程。

        🛡️ T26（U3）：leave_team 的邮箱目录删除从主线程移到后台——
        rmtree 是纯磁盘操作，与内存态/_data_lock 无共享状态，放后台
        线程安全；leave 时 watcher 已停，目录删除不会触发重建。

        🛡️ F3（devil-advocate D1 U3-R1）：消除"leave 后快速 rejoin 同目录"
        竞态。时序：leave_team → 本方法启动后台 rmtree → 用户立即重新加入
        同一团队（window_id 复用）→ join 的 mkdir 建新目录 → 若 rmtree 直接
        删原路径，后到的删除会误删新目录，邮箱功能失效。
        修复：**先把原目录原子改名为唯一临时名**（同父目录 rename 原子且
        快，主线程仅此一步、不阻塞），后台线程只删临时目录——join 的
        mkdir 在原路径创建全新目录，与后台删除的临时名互不干扰，竞态消除。
        正常路径（leave 后不立即重进）零额外开销；rename 失败（目录被并发
        删除/权限）时回退直接删原路径（幂等容错，ignore_errors=True）。
        """
        parent = path.parent
        tmp_path = parent / f".{path.name}.rmtree.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            path.rename(tmp_path)  # 原子改名：原路径立即释放，后续 mkdir 安全
        except OSError:
            # rename 失败（目录可能已被并发删除）：回退直接删原路径（幂等）
            tmp_path = path
        target = str(tmp_path)

        def _do_rmtree():
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:
                pass

        threading.Thread(target=_do_rmtree, daemon=True, name=f"team-rmtree-{path.name}").start()

    def get_members(
        self,
        team_name: str = DEFAULT_TEAM,
        run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取团队成员列表。

        🛡️ M1（多团队归属过滤）：
        - run_id=None  → 返回全部成员（向后兼容）
        - run_id=""    → 仅返回无归属成员（m.get("run_id","")==""）
        - run_id="x"  → 仅返回归属该 run 的成员

        过滤在内存中按成员 dict 的 run_id 字段判定，不依赖持久化键。
        """
        members = list(self._get_team_data(team_name).get("members", {}).values())
        if run_id is None:
            return members
        if run_id == "":
            return [m for m in members if not m.get("run_id")]
        return [m for m in members if m.get("run_id") == run_id]

    def get_member_run_id(
        self,
        window_id: str,
        team_name: str = DEFAULT_TEAM,
    ) -> str:
        """🛡️ M1：反查成员所属 run_id（多团队拦截关键 API）。

        非成员 / 老成员（无 run_id 字段）均返回空串。优先取顶层
        `team_members` 快照（_cleanup_stale_members 不清理），其次回退
        到 `members`，保证恢复路径与活跃成员两种视角一致。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            snap = (data.get("team_members") or {}).get(window_id)
            if isinstance(snap, dict) and snap.get("run_id"):
                return snap["run_id"]
            member = (data.get("members") or {}).get(window_id)
            if isinstance(member, dict) and member.get("run_id"):
                return member["run_id"]
        return ""

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
                "can_bash", "can_team"(团队工具能力，团队成员恒 True——T16),
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

        def _group_tools(group_name: str) -> list:
            """按注册表分组取工具名（能力判定不写死工具名，随插件 group 声明）"""
            try:
                from app.tools.registry import ToolRegistry

                reg = ToolRegistry.get_instance()
                return [n for n in reg.names() if reg.get(n).group == group_name]
            except Exception:
                return []

        # can_write：注册表"文件写入"分组的工具（write/edit/multi_edit）全部 allow
        write_tools = _group_tools("文件写入")
        can_write = all(_is_allowed(t) for t in write_tools) if write_tools else False
        # can_bash：单工具能力判定（bash 即能力名，非写死集合）
        can_bash = _is_allowed("bash")
        # 🛡️ T16：can_team 对团队成员恒真——团队工具（team_send_message /
        # team_list_members）的实际可用性由 schema 层按 is_in_team 判定
        # （agent.py get_agent_tools_schema：仅团队成员可见），与 agent 静态
        # permission/tools 白名单无关。review 等只读角色 permission 不含
        # team_* 条目 → 旧逻辑按静态权限判定得 False，误显示"团队✗"。
        # 本函数仅在团队成员上下文调用（join 快照 / get_member_capability
        # 动态解析），团队成员必然可用团队工具 → 恒 True。
        can_team = True

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

    def rebuild_team_members_snapshot(self, entries: List[Tuple[str, str]], team_name: str = DEFAULT_TEAM):
        """重建顶层 team_members 快照（T18：恢复路径专用，窗口数防膨胀）。

        恢复窗口每次都是全新 window_id，join 只增不删会累积历史 wid →
        快照计数膨胀 → 下次恢复窗口数随恢复次数线性膨胀（2→4→6…）。
        重建 = 清空旧记录，仅保留本次实际恢复的 (window_id, agent_name)，
        多次恢复窗口数恒定（= 成员数）。

        保持 T3 语义：同角色多成员（异 wid）各保留一条；模板 agents 不在
        team_members 中（get_team_member_snapshot 并集时模板优先）。

        🛡️ M1：entries 中单项支持 2 元组 (wid, agent) 或 4 元组
        (wid, agent, run_id, team_label)——后者把多团队归属一并写入快照，
        用于恢复路径直接把窗口绑回原 run。

        Args:
            entries: [(window_id, agent_name), ...] 或
                [(window_id, agent_name, run_id, team_label), ...]
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            snapshot = {}
            for entry in entries:
                # 🛡️ M1：兼容 2 元组（wid, agent）与 4 元组（wid, agent, run_id, team_label）
                wid = str(entry[0] or "").strip() if len(entry) >= 1 else ""
                agent = str(entry[1] or "").strip() if len(entry) >= 2 else ""
                run_id = str(entry[2]).strip() if len(entry) >= 3 and entry[2] is not None else ""
                team_label = str(entry[3]).strip() if len(entry) >= 4 and entry[3] is not None else ""
                if wid and agent:
                    rec = {
                        "agent_name": agent,
                        "source": "restore",
                        "joined_at": time.time(),
                    }
                    if run_id:
                        rec["run_id"] = run_id
                    if team_label:
                        rec["team_label"] = team_label
                    snapshot[wid] = rec
            data["team_members"] = snapshot
            self._save_team_data(team_name)

    def get_team_member_snapshot(self, team_name: str = DEFAULT_TEAM, run_id: str = "") -> List[Dict[str, Any]]:
        """获取团队成员快照（去重有序）：模板 agents ∪ 手动 join 快照 team_members。

        🛡️ F3（T2-P3 第 1 层）：手动 /team --join 的成员记录在 team.json 顶层
        team_members 键（不受 _cleanup_stale_members 清理、不依赖当前活跃窗口），
        恢复会话时用本方法找回"无会话记录但确实加入过"的成员。

        🛡️ M1-r（run 过滤）：传入 run_id 时只返回该团队运行（run）的成员——
        M1 多团队并存时多个 run 的成员同存于 default team.json 的 team_members，
        不过滤会把其他团队的成员混入会话快照（会话保存"成员污染"根因）。
        模板 agents 无 run 归属（模板是全局的，可能已被其他团队覆盖），
        指定 run_id 时**不包含**模板 agents——模板加载路径所有角色都会
        join_team 写入 team_members（带 run_id），该 run 的模板角色必然在
        快照记录中；模板 agents 兜底仅用于不指定 run 的旧语义（F3 找回）。

        🛡️ M1-r'（多团队并存严格匹配）：run_id 非空时**严格匹配** rec_run，
        空 rec_run（无归属字段，常见于 M1 前老数据 / 其他路径未传 run_id 的
        join_team）不再兜底——多团队并存下若不过滤会把"无主"成员算入所有
        run 的快照，污染合并条目的成员列表（"多了其他团队的成员"bug 根因）。
        老成员找回路径改走 `rebuild_team_members_snapshot` 或 `get_members()`，
        不再依赖本方法的"无主兜底"。

        T3：返回成员记录列表（含 window_id），支持同角色多成员（两个 build
        各占一条记录）。team_members 兼容双形态 key：
        - 新格式：key = window_id，value 含 agent_name 字段
        - 老格式：key = agent_name，value 无 agent_name 字段（T3 前落库数据）

        Args:
            run_id: 可选。指定时仅返回该 run 的成员（rec_run 必须严格匹配；
                空串时不过滤，兼容旧语义——单团队时代"无 run_id 即本团队"）。

        Returns:
            成员记录列表 [{"agent_name": ..., "window_id": ...}]（模板 agents 在前，
            手动快照补充，按 (agent_name, window_id) 去重）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            records: List[Dict[str, Any]] = []
            seen: set = set()

            def _add(agent_name: str, window_id: str = ""):
                agent_name = str(agent_name or "").strip()
                window_id = str(window_id or "").strip()
                key = (agent_name, window_id)
                if agent_name and key not in seen:
                    seen.add(key)
                    records.append({"agent_name": agent_name, "window_id": window_id})

            # 1. 模板 agents（模板定义的角色优先，无 window_id）。
            #    仅不指定 run_id 时包含（模板无 run 归属，多团队并存时可能
            #    是其他团队覆盖后的模板——指定 run 时混入会污染本 run 快照）。
            if not run_id:
                template = data.get("template") or {}
                for item in template.get("agents") or []:
                    if isinstance(item, dict) and item.get("agent_name"):
                        _add(item["agent_name"])
            # 2. 手动 join 快照（补充模板之外的成员；兼容新老 key 形态）
            for key, value in (data.get("team_members") or {}).items():
                # 🛡️ M1-r'：run_id 非空时严格匹配 rec_run——
                # 空 rec_run（无主成员）不算入任何 run 的快照，避免污染合并条目。
                rec_run = ""
                if isinstance(value, dict):
                    rec_run = str(value.get("run_id") or "").strip()
                if run_id:
                    if rec_run != run_id:
                        continue
                # rec_run == run_id（或 run_id 为空）时保留
                if isinstance(value, dict) and value.get("agent_name"):
                    # 新格式：key = window_id，value 含 agent_name
                    _add(value["agent_name"], key)
                else:
                    # 老格式：key = agent_name，value 无 agent_name 字段
                    _add(key)
            return records

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

    # ── 团队标签（M1 多团队并存：人名/label）──

    def get_team_label(self, team_name: str = DEFAULT_TEAM) -> str:
        """🛡️ M1：当前团队对外展示的标签。

        优先级：模板 `template["name"]` > 团队目录名 > `"default"`。
        模板加载（/team --load）后会写入 template.name，作为用户在
        `team_list_members` 等场景看到的"团队名"。
        """
        data = self._get_team_data(team_name)
        template = data.get("template") or {}
        name = template.get("name") if isinstance(template, dict) else None
        if name:
            return str(name)
        return team_name or "default"

    def get_team_label_by_run(
        self,
        run_id: str,
        team_name: str = DEFAULT_TEAM,
    ) -> str:
        """🛡️ M1：按 run_id 反查对应的 team_label。

        跨团队沟通提示中需要展示"目标团队 X"——扫描 team_members 快照
        找到 run_id 命中任一记录的 team_label；无则回退 run_id 本身
        （仍可识别但不友好）。
        """
        if not run_id:
            return ""
        with self._data_lock:
            data = self._get_team_data(team_name)
            for entry in (data.get("team_members") or {}).values():
                if isinstance(entry, dict) and entry.get("run_id") == run_id:
                    label = entry.get("team_label")
                    if label:
                        return str(label)
            for member in (data.get("members") or {}).values():
                if isinstance(member, dict) and member.get("run_id") == run_id:
                    label = member.get("team_label")
                    if label:
                        return str(label)
        return run_id

    def update_member_team(
        self,
        window_id: str,
        run_id: str = "",
        team_label: str = "",
        team_name: str = DEFAULT_TEAM,
    ) -> bool:
        """🛡️ M1：补写成员的 run_id / team_label（恢复路径专用）。

        用于历史会话恢复时把 run 归属回填到老成员记录——
        非成员静默跳过（返回 False）；至少一个非空字段才落盘，避免
        无意义写盘；同步更新顶层 `team_members` 快照。
        """
        wid = str(window_id or "").strip()
        if not wid:
            return False
        with self._data_lock:
            data = self._get_team_data(team_name)
            members = data.setdefault("members", {})
            if wid not in members:
                return False
            if not run_id and not team_label:
                return False
            member = members[wid]
            if run_id:
                member["run_id"] = run_id
            if team_label:
                member["team_label"] = team_label
            # 顶层快照同步
            tm_snap = data.setdefault("team_members", {})
            snap = tm_snap.get(wid)
            if isinstance(snap, dict):
                if run_id:
                    snap["run_id"] = run_id
                if team_label:
                    snap["team_label"] = team_label
                tm_snap[wid] = snap
            self._save_team_data(team_name)
        return True

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

    def get_team_run_ids(self, team_name: str = DEFAULT_TEAM) -> List[str]:
        """🛡️ M1：列出当前团队中所有出现过的 run_id（去重，按首次出现顺序）。

        来源：顶层 `team_members` 快照 ∪ 活跃 `members`——前者覆盖 _cleanup
        后仍存活的恢复成员，后者覆盖尚未触发 _cleanup 的活跃窗口。
        无归属成员（run_id 为空）不计入。多团队并存判断（>1）由调用方进行。
        """
        seen: List[str] = []
        seen_set = set()
        with self._data_lock:
            data = self._get_team_data(team_name)
            for entry in (data.get("team_members") or {}).values():
                rid = entry.get("run_id") if isinstance(entry, dict) else None
                if rid and rid not in seen_set:
                    seen_set.add(rid)
                    seen.append(rid)
            for member in (data.get("members") or {}).values():
                rid = member.get("run_id") if isinstance(member, dict) else None
                if rid and rid not in seen_set:
                    seen_set.add(rid)
                    seen.append(rid)
        return seen

    # ── 团队级统一项目（一人改项目全员同步）──

    def set_team_project(self, project: str, team_name: str = DEFAULT_TEAM):
        """设置团队级统一项目（写入 team.json **顶层**，与 run_id 平级）

        项目是团队共享状态：任一成员切换项目时写入，供同团队其他成员
        同步应用与恢复路径读取。放顶层而非成员级——_cleanup_stale_members
        清理失效成员时只动 members，不会丢失 project。

        🛡️ 弃用警告：team_name 粒度无法区分同模板多次加载产生的多个团队
        运行（每次 /team --load 都有独立 run_id），多团队并存时 set_team_project
        会互相覆盖。新代码请改用 set_project_for_run_id（按 run_id 粒度）。
        本接口保留仅为向后兼容单团队旧调用方。

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
        """获取团队级统一项目（未设置 / 老团队无 project 时返回空串）

        🛡️ 弃用警告：team_name 粒度无法区分多 run_id 团队，新代码请改用
        get_project_for_run_id（按 run_id 粒度）。本接口保留仅为向后兼容。
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            return data.get("project", "") or ""

    # ── 团队级统一项目（按 run_id 粒度，#5a-fix Plan C）──

    def set_project_for_run_id(self, project: str, run_id: str, team_name: str = DEFAULT_TEAM):
        """按 run_id 粒度设置团队级项目（写入 team.json **顶层**
        ``projects_by_run_id[run_id]``，与 ``run_id`` 平级）。

        设计动机：tab_panel 团队框以 run_id 分组，而旧 set_team_project 以
        team_name（默认 DEFAULT_TEAM）粒度存储——同模板多次加载产生多个
        run_id 时所有团队共享 DEFAULT_TEAM.project，导致多团队并存时
        ``后建团队切项目 → 之前团队框 header icon 被覆盖`` 的 bug。

        本接口以 run_id 粒度存储，与 tab_panel 分组维度对齐；旧 set_team_project
        保留（team_name 粒度）仅为兼容老调用方。

        Args:
            project: 项目名（空串表示清除该 run_id 的项目）
            run_id: 团队运行标识（uuid hex，空串时静默 no-op）
            team_name: 团队名（仅用于定位 team.json 路径，默认 DEFAULT_TEAM）
        """
        if not run_id:
            return
        with self._data_lock:
            data = self._get_team_data(team_name)
            mapping = data.get("projects_by_run_id") or {}
            if mapping.get(run_id) == project:
                return
            # 复制 + 写回，避免多线程下直接改 dict 引发的并发问题
            new_mapping = dict(mapping)
            new_mapping[run_id] = project
            data["projects_by_run_id"] = new_mapping
            self._save_team_data(team_name)

    def get_project_for_run_id(self, run_id: str, team_name: str = DEFAULT_TEAM) -> str:
        """按 run_id 粒度读取团队级项目（未设置时回退顶层 ``project`` 字段）

        回退策略：``projects_by_run_id`` 不存在该 run_id 时返回顶层 ``project``
        字段值（兼容仅用旧 set_team_project 写入的老团队）。读不到任何值时
        返回空串（与旧 get_team_project 行为一致）。

        Args:
            run_id: 团队运行标识（uuid hex）
            team_name: 团队名（默认 DEFAULT_TEAM）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            mapping = data.get("projects_by_run_id") or {}
            if run_id and run_id in mapping:
                return mapping[run_id] or ""
            # 回退：未注册 run_id 或老数据无 projects_by_run_id 时返回顶层 project
            return data.get("project", "") or ""

    # ── 团队级统一工作目录/工作树（一人改工作目录全员同步）──

    def set_team_workdir(self, workdir: str, team_name: str = DEFAULT_TEAM):
        """设置团队级统一工作目录/工作树（写入 team.json **顶层**，与 project 平级）

        工作目录/工作树是团队共享状态：任一成员切换工作目录或 git worktree 时
        写入，供同团队其他成员同步应用与恢复路径读取。

        Args:
            workdir: 工作目录绝对路径（空串表示清除）
        """
        with self._data_lock:
            data = self._get_team_data(team_name)
            if data.get("workdir") == workdir:
                return
            data["workdir"] = workdir
            self._save_team_data(team_name)

    def get_team_workdir(self, team_name: str = DEFAULT_TEAM) -> str:
        """获取团队级统一工作目录/工作树（未设置 / 老团队无 workdir 时返回空串）"""
        with self._data_lock:
            data = self._get_team_data(team_name)
            return data.get("workdir", "") or ""

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
        from_run_id: str = "",
        to_run_id: str = "",
    ) -> Optional[str]:
        """发送任务邮件给队友

        🛡️ M1（user 拍板放行+打标）：from_run_id / to_run_id 写入 mail dict
        以便跨团队消息在 UI/历史面板可追溯；老调用不传 → 空串（向后兼容）。

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
            # 🛡️ M1：跨团队消息追溯字段——非空时写 mail dict，便于历史面板
            # / 接收方 UI 展示「⚠ 跨团队消息」标识。
            "from_run_id": from_run_id,
            "to_run_id": to_run_id,
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
        """返回成员的工作状态: 'busy'（有 running/pending 任务或实时忙碌）| 'idle'（空闲）

        查询合并：邮件派生状态为「空闲」时追加查 runtime 状态（流式输出/思考/提问
        不落邮件，由 main_widget._set_ai_state 实时同步），保证忙碌成员不被误报为空闲。
        """
        mails = self.get_mailbox_mails(window_id, team_name)
        for m in mails:
            if m.get("type") == "task" and m.get("status") in ("running", "pending"):
                return "busy"
        return self.get_member_runtime_status(window_id, team_name)

    # ── 成员实时工作状态（runtime status，流式输出/思考同步）──

    def set_member_runtime_status(self, window_id: str, status: str, team_name: str = DEFAULT_TEAM):
        """设置成员实时工作状态（busy/idle），写入 team.json members[wid]

        与邮件状态互补：邮件状态反映任务生命周期（pending/running/done），
        runtime 状态反映流式输出/思考/提问等实时状态（由 main_widget
        ._set_ai_state 同步，供 team_list_members 查询路径可见）。
        非团队成员静默跳过（不落盘）；成员被清理时随 members 一并删除。
        """
        if status not in ("busy", "idle"):
            return
        with self._data_lock:
            data = self._get_team_data(team_name)
            member = (data.get("members") or {}).get(window_id)
            if not member:
                return
            if member.get("runtime_status") == status:
                return
            member["runtime_status"] = status
            # 🛡️ 落盘失败兜底：runtime 状态由 _set_ai_state 在 UI 线程流式回调
            # 中同步，磁盘只读/满等写失败不应抛 OSError 中断流式收尾。
            # 先更新内存态（本行上方已生效），落盘失败仅告警；下次状态
            # 变化会再次尝试落盘（正常路径行为不变）。
            try:
                self._save_team_data(team_name)
            except OSError as e:
                logger.warning(
                    f"[TeamManager] 成员 {window_id} runtime 状态落盘失败 (status={status}, team={team_name}): {e}"
                )

    def get_member_runtime_status(self, window_id: str, team_name: str = DEFAULT_TEAM) -> str:
        """读取成员实时工作状态（无记录/非团队成员返回 'idle'）"""
        with self._data_lock:
            data = self._get_team_data(team_name)
            member = (data.get("members") or {}).get(window_id)
            if not member:
                return "idle"
            return member.get("runtime_status", "idle")

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
