# -*- coding: utf-8 -*-
"""ticker.py — 记忆调度器（turn-based 轻量链 + 逻辑日批，对齐 openhanako memory-ticker）。

触发机制：
- 每 10 轮（Stop hook 计数）：轻量链 = compile_today + assemble
- 逻辑日批（后台线程每 5 分钟检查日期变化，每逻辑日一次）：
  compile_daily → compile_today → roll_daily_window → compile_facts → assemble
  → Dream 自动（双水位）→ 经验反思（memory_enabled && experience_enabled）

并发模型：单工作线程 + 集合去重（进行中/已排队的调用合并）；全部后台执行不阻塞 UI/对话。
LLM 不可用：compile_chain 内部静默降级，日批后续步骤照常（各步独立）。
计数持久化：<state_dir>/turn-state.json（重启不清零）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

# 每隔多少轮触发一次轻量编译链（对齐 openhanako TURNS_PER_SUMMARY）
_TURNS_PER_CHAIN = 10
# 日批检查间隔（秒）
_DAILY_CHECK_INTERVAL = 300


def _load_session_store():
    """加载 session_store（importlib 手动注册）。

    mtime 自检：插件热重载不清理 assistant_hub_core.* 的 sys.modules 缓存，
    命中即返回会卡旧代码 → 比对源文件 mtime，更新则重新加载替换。
    """
    import importlib.util
    import sys

    key = "assistant_hub_core.session_store"
    path = Path(__file__).resolve().parent.parent / "session_store.py"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(key)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    mod._source_mtime = mtime
    return mod


class MemoryTicker:
    """记忆调度器（进程级单例，由 AssistantManager 持有）。"""

    _instance: Optional["MemoryTicker"] = None

    def __init__(self, manager: Any, state_dir: Optional[Path] = None, *, start_daemon: bool = True):
        self._mgr = manager
        self._state_dir = Path(state_dir) if state_dir else (manager.root / "_ticker")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._turn_state = self._load_turn_state()

        self._pending: Set[str] = set()
        self._pending_lock = threading.Lock()
        self._running = False
        self._work_queue: list = []
        self._work_cv = threading.Condition()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="assistant-ticker")
        self._worker.start()

        self._last_daily_date = self._turn_state.get("last_daily_date", "")
        self._daemon_stop = threading.Event()
        if start_daemon:
            self._daemon = threading.Thread(target=self._daily_loop, daemon=True, name="assistant-daily")
            self._daemon.start()

    # ── 单例 ──
    @classmethod
    def get(cls, manager: Any, state_dir: Optional[Path] = None) -> "MemoryTicker":
        if cls._instance is None:
            cls._instance = cls(manager, state_dir=state_dir)
        return cls._instance

    # ── 计数持久化 ──
    def _load_turn_state(self) -> Dict[str, Any]:
        try:
            data = json.loads((self._state_dir / "turn-state.json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"count": 0, "last_daily_date": ""}

    def _save_turn_state(self) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            (self._state_dir / "turn-state.json").write_text(
                json.dumps(self._turn_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ── 对外入口（hook 调用）──
    def on_turn_finished(self, aid: Optional[str] = None) -> None:
        """Stop hook：当前会话助手轮次 +1；每 10 轮触发轻量编译链。

        aid 由调用方按会话归属解析（override 优先，否则主助手）；
        缺省回落 active_id（兼容旧调用）。计数全局共享（只控制触发
        频率，不影响归属）。
        """
        if not aid:
            aid = self._mgr.active_id()
        if not aid or not self._mgr.has(aid):
            return
        count = int(self._turn_state.get("count", 0)) + 1
        if count >= _TURNS_PER_CHAIN:
            count = 0
            self._turn_state["count"] = count
            self._save_turn_state()
            self._enqueue(f"light:{aid}", self._run_light, aid)
        else:
            self._turn_state["count"] = count
            self._save_turn_state()

    def daily_maintenance(self, logical_date: str) -> None:
        """逻辑日批（daemon 线程检测日期变化后调用；每逻辑日一次）。

        遍历全部开启记忆的助手各跑一遍：各助手素材经归属过滤后
        互不重叠，无新增的助手由 compile_chain(require_new) 短路。
        """
        if self._last_daily_date == logical_date:
            return
        self._last_daily_date = logical_date
        self._turn_state["last_daily_date"] = logical_date
        self._save_turn_state()
        for a in self._mgr.list_assistants_sorted_by_stable():
            aid = getattr(a, "id", "")
            if not aid or not self._mgr.has(aid):
                continue
            if not getattr(a, "memory_enabled", False):
                continue
            self._enqueue(f"daily:{aid}", self._run_daily, aid, logical_date)

    # ── 工作项 ──
    def _run_light(self, aid: str) -> None:
        try:
            self._mgr.compile_chain(aid, light=True)
        except Exception:
            pass  # LLM 不可用等：静默（compile_chain 内部已降级）

    def _run_daily(self, aid: str, logical_date: str) -> None:
        today_changed = True  # LLM 不可用等异常时保持原行为：后续步骤照常尝试
        try:
            # 顺序铁律：compile_chain(False) 内部先 daily 后 today；
            # require_new=True：今日无新增时短路 facts（省 LLM 成本）
            chain = self._mgr.compile_chain(aid, light=False, require_new=True)
            if (chain or {}).get("ok") and chain.get("steps", {}).get("compile_today") is not None:
                today_changed = bool(chain["steps"]["compile_today"].get("changed"))
        except Exception:
            chain = {}
        a = self._mgr.get(aid)
        if a is None:
            return
        # 今日确无新增：素材无变化，dream/经验反思无需跑
        if not today_changed:
            return
        if getattr(a, "dream_auto_enabled", False):
            try:
                self._mgr.dream_start_auto_if_eligible(aid, logical_date)
            except Exception:
                pass
        if getattr(a, "memory_enabled", False) and getattr(a, "experience_enabled", False):
            try:
                self._mgr.experience_reflect(aid)
            except Exception:
                pass

    # ── 后台线程 ──
    def _enqueue(self, key: str, fn, *args) -> None:
        with self._pending_lock:
            if key in self._pending:
                return  # 去重：进行中/已排队的同类调用合并
            self._pending.add(key)
        with self._work_cv:
            self._work_queue.append((key, fn, args))
            self._work_cv.notify()

    def _worker_loop(self) -> None:
        while True:
            with self._work_cv:
                while not self._work_queue:
                    self._work_cv.wait()
                key, fn, args = self._work_queue.pop(0)
            self._running = True
            try:
                fn(*args)
            except Exception:
                pass
            finally:
                self._running = False
                with self._pending_lock:
                    self._pending.discard(key)

    def _daily_loop(self) -> None:
        """daemon：每 5 分钟比较逻辑日变化触发日批（每逻辑日一次）。"""
        while not self._daemon_stop.wait(_DAILY_CHECK_INTERVAL):
            try:
                today = self._logical_today()
                if today and today != self._last_daily_date:
                    # 存在助手才触发（内部再按 memory_enabled 过滤）
                    if self._mgr.list_assistants_sorted_by_stable():
                        self.daily_maintenance(today)
            except Exception:
                pass

    def _logical_today(self) -> str:
        """当前逻辑日（04:00 边界）；session_store 不可用返回空串。"""
        try:
            mod = _load_session_store()
            from datetime import datetime

            return mod.logical_day(datetime.now())
        except Exception:
            return ""

    def stop(self) -> None:
        self._daemon_stop.set()
