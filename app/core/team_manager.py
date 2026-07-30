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
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        # 每次加载都清理失效成员
        self._cleanup_stale_members(team_name)

    def _get_team_data(self, team_name: str) -> Dict[str, Any]:
        if team_name not in self._team_cache:
            self._init_team(team_name)
        # 懒清理：每次读取数据前清理失效成员
        self._cleanup_stale_members(team_name)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 成员清理 ─────────────────────────────────────

    def set_active_window_ids(self, window_ids: set):
        """由主窗口同步当前真实的活跃窗口 ID 集合（跨 OpenAIChatToolWindow._instances 收集）"""
        self._active_window_ids = set(window_ids)
        # 同步清理所有团队中的失效成员
        for team_name in list(self._team_cache.keys()):
            self._cleanup_stale_members(team_name)

    def _get_active_windows(self) -> set:
        """获取当前活跃窗口 ID（优先用主窗口同步的集合）"""
        if hasattr(self, "_active_window_ids") and self._active_window_ids:
            return self._active_window_ids
        # 兜底：读文件
        active_file = self._teams_dir / ".active_windows"
        try:
            if active_file.exists():
                return set(json.loads(active_file.read_text()))
        except json.JSONDecodeError, FileNotFoundError:
            pass
        return set()

    def _cleanup_stale_members(self, team_name: str = DEFAULT_TEAM):
        """彻底清理失效成员：移除 member 记录 + 删除邮箱目录"""
        data = self._team_cache.get(team_name)
        if not data:
            return
        members = data.get("members", {})
        if not members:
            return

        active = self._get_active_windows()
        stale_ids = [wid for wid in members if wid not in active]

        for wid in stale_ids:
            members.pop(wid, None)
            # 删除该成员的邮箱目录
            mailbox_dir = self._mailbox_dir(team_name, wid)
            if mailbox_dir.exists():
                try:
                    shutil.rmtree(str(mailbox_dir))
                except Exception:
                    pass

        # 也清理没有对应 member 记录的孤立邮箱目录
        mailboxes_dir = self._mailboxes_dir(team_name)
        if mailboxes_dir.exists():
            for child in mailboxes_dir.iterdir():
                if child.is_dir() and child.name not in members:
                    try:
                        shutil.rmtree(str(child))
                    except Exception:
                        pass

        if stale_ids:
            data["members"] = members
            self._save_team_data(team_name)

    # ── 成员管理 ─────────────────────────────────────

    def join_team(self, window_id: str, agent_name: str, team_name: str = DEFAULT_TEAM):
        """窗口加入团队"""
        data = self._get_team_data(team_name)
        data.setdefault("members", {})
        data["members"][window_id] = {
            "window_id": window_id,
            "agent_name": agent_name,
            "joined_at": time.time(),
        }
        self._save_team_data(team_name)
        self._mailbox_dir(team_name, window_id).mkdir(parents=True, exist_ok=True)

    def leave_team(self, window_id: str, team_name: str = DEFAULT_TEAM):
        """窗口离开团队（同时清理邮箱目录）"""
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

    def _update_mail_status(
        self,
        mail_id: str,
        window_id: str,
        status: str,
        team_name: str = DEFAULT_TEAM,
        result: str = "",
    ):
        mailbox_dir = self._mailbox_dir(team_name, window_id)
        mail_file = mailbox_dir / f"{mail_id}.json"
        if mail_file.exists():
            mail = self._read_json(mail_file)
            if mail:
                mail["status"] = status
                if result:
                    mail["result"] = result
                self._write_json(mail_file, mail)
