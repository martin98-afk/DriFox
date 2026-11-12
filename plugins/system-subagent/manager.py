# -*- coding: utf-8 -*-
"""子智能体任务管理器（system-subagent 插件所有物）

从 ``app/core/workers/subagent_worker.py`` 迁出：批次编排、完成链（单条与批次
结果注入）、stall 检测、任务归档、任务状态查询。执行器 ``SubAgentExecutor``
仍留主程序 core，本模块只按公开原语驱动它（``cancel`` /
``respond_permission`` / ``get_logs`` / ``get_summary`` / 信号）。

宿主依赖（agent_manager / tool_executor / session_store / 模型解析回调）由
``ChatBackend`` 经工厂注入，插件不自行构造。
"""

import time
from typing import Any, Callable, Dict, List, Optional

import orjson
from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.core.workers.subagent_worker import SubAgentExecutor
from app.tools.result import ToolResult

# 批次汇总携带结果正文时的单任务截断上限（与 tools/subagent_tools 注入器对齐）
_MAX_BATCH_RESULT_CHARS = 4096

# [H5-2] 归档内存 result 上限：DB（storage.db）优先存全文，UI 读取走 DB；
# 内存归档仅作 DB 写失败时的回退与近期查询缓存。32KB 上限防 200 条
# _finished_tasks 常驻膨胀（T13 实测重度 793MB/窗口）。
_ARCHIVED_RESULT_MAX_CHARS = 32 * 1024

# [H5-3] 注入父对话上下文的 result 上限：长子智能体结果全量注入会灌爆主
# 对话上下文（独立于内存的功能缺陷），16KB 足够还原结论，超长内容应由
# 子任务落盘后用 read 取回。
_INJECTED_RESULT_MAX_CHARS = 16 * 1024


def _cap_text(text, limit, tag):
    """超长字符串截断并附原长标记（非字符串/短文本原样返回）。"""
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + f"\n…（{tag}已截断，全文 {len(text)} 字符）"
    return text


class _BoundedTaskDict(dict):
    """有界字典（H1）：超过容量上限时弹出最旧条目，防止 _finished_tasks 无限增长。

    重写 __setitem__，使全部 12+ 处 `self._finished_tasks[task_id] = {...}` 写入点
    自动受容量约束，无需逐个改写。读取（get/items/in/setdefault-on-value）行为不变。
    """

    def __init__(self, maxlen: int = 200):
        super().__init__()
        self._maxlen = maxlen

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        while len(self) > self._maxlen:
            oldest = next(iter(self))  # dict 保序：首个即最旧
            super().__delitem__(oldest)


class SubAgentTaskStore:
    """任务日志持久化（插件自有表 ``plugin_system_subagent_task``）。

    与宿主 SessionStore 解耦：表在插件命名空间内（经 ``storage.db`` 受限代理），
    主程序不再需要暴露子智能体存储方法。
    """

    TABLE_SUFFIX = "task"

    def __init__(self, db: Any):
        self._db = db
        self._table = db.table(self.TABLE_SUFFIX) if db is not None else ""
        self._ready = False
        self._warned = False

    def ensure(self) -> bool:
        if self._ready:
            return True
        if self._db is None:
            if not self._warned:
                logger.warning("[SubAgentManager] storage.db 不可用，任务日志不落库")
                self._warned = True
            return False
        ok, _ = self._db.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "task_id TEXT PRIMARY KEY, agent_name TEXT, task_description TEXT, "
            "session_id TEXT, status TEXT, result TEXT, error TEXT, logs TEXT, "
            "summary TEXT, created_at TEXT, updated_at TEXT)"
        )
        self._ready = bool(ok)
        return self._ready

    @staticmethod
    def _now() -> str:
        from datetime import datetime

        return datetime.now().isoformat()

    def save(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        status: str = "running",
        result: Optional[str] = None,
        error: Optional[str] = None,
        logs: Optional[List[Dict]] = None,
        summary: Optional[Dict] = None,
        session_id: str = "",
    ) -> None:
        """全量 upsert（与旧 SessionStore.save_subagent_task 同语义）。"""
        if not self.ensure():
            return
        now = self._now()
        ok, err = self._db.execute(
            f"INSERT INTO {self._table} (task_id, agent_name, task_description, session_id, status, "
            "result, error, logs, summary, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET agent_name=excluded.agent_name, "
            "task_description=excluded.task_description, session_id=excluded.session_id, "
            "status=excluded.status, result=excluded.result, error=excluded.error, "
            "logs=excluded.logs, summary=excluded.summary, updated_at=excluded.updated_at",
            (
                task_id,
                agent_name or "",
                task_description or "",
                session_id or "",
                status or "",
                result or "",
                error or "",
                orjson.dumps(logs or []).decode(),
                orjson.dumps(summary or {}).decode(),
                now,
                now,
            ),
        )
        if not ok:
            logger.warning(f"[SubAgentManager] 任务落库失败 {task_id}: {err}")

    def update_status(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
        logs: Optional[List[Dict]] = None,
        summary: Optional[Dict] = None,
    ) -> None:
        """部分更新（None 字段不覆盖，与旧语义一致）。"""
        if not self.ensure():
            return
        columns = {"result": result, "error": error}
        sets = ["status = ?", "updated_at = ?"]
        params: List[Any] = [status or "", self._now()]
        if logs is not None:
            sets.append("logs = ?")
            params.append(orjson.dumps(logs).decode())
        if summary is not None:
            sets.append("summary = ?")
            params.append(orjson.dumps(summary).decode())
        for name, value in columns.items():
            if value is not None:
                sets.append(f"{name} = ?")
                params.append(value)
        params.append(task_id)
        ok, affected = self._db.execute(f"UPDATE {self._table} SET {', '.join(sets)} WHERE task_id = ?", tuple(params))
        if not ok:
            logger.warning(f"[SubAgentManager] 任务状态落库失败 {task_id}: {affected}")
        elif affected == 0:
            logger.warning(f"[SubAgentManager] 零行更新（行不存在或已被并发清除）task_id={task_id} status={status}")

    def get(self, task_id: str) -> Optional[Dict]:
        if not self.ensure():
            return None
        rows = self._db.query(f"SELECT * FROM {self._table} WHERE task_id = ?", (task_id,))
        return self._row_to_task(rows[0]) if rows else None

    def latest_for_session(self, session_id: str) -> Optional[Dict]:
        """该会话最近更新的任务（会话卡显示时恢复内容用）。"""
        if not self.ensure() or not session_id:
            return None
        rows = self._db.query(
            f"SELECT * FROM {self._table} WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        )
        return self._row_to_task(rows[0]) if rows else None

    def clear_old(self, days: int = 7) -> int:
        if not self.ensure():
            return 0
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        ok, count = self._db.execute(f"DELETE FROM {self._table} WHERE updated_at < ?", (cutoff,))
        return int(count) if ok and isinstance(count, int) else 0

    @staticmethod
    def _row_to_task(row: Dict) -> Dict:
        def _load(raw: Any, default: Any) -> Any:
            if isinstance(raw, (str, bytes)):
                try:
                    return orjson.loads(raw)
                except Exception:
                    return default
            return raw if isinstance(raw, type(default)) else default

        return {
            "task_id": row.get("task_id", ""),
            "agent_name": row.get("agent_name", ""),
            "task_description": row.get("task_description", ""),
            "session_id": row.get("session_id", ""),
            "status": row.get("status", ""),
            "result": row.get("result", ""),
            "error": row.get("error", ""),
            "logs": _load(row.get("logs"), []),
            "summary": _load(row.get("summary"), {}),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    def import_legacy(self) -> int:
        """把旧表 ``sub_agent_logs`` 一次性导入插件表；插件表非空则跳过。"""
        if not self.ensure():
            return 0
        if self._db.query(f"SELECT COUNT(*) AS c FROM {self._table}"):
            return 0
        try:
            from app.utils.db_manager import DatabaseManager

            ok, rows = DatabaseManager().execute_sql("SELECT * FROM sub_agent_logs")
        except Exception as e:  # noqa: BLE001 - 旧表不存在是正常态
            logger.debug(f"[SubAgentManager] 旧任务表不可读，跳过导入: {e}")
            return 0
        if not ok or not rows:
            return 0
        imported = 0
        for row in rows:
            task = self._row_to_task(row)
            if not task.get("task_id"):
                continue
            self.save(
                task["task_id"],
                task.get("agent_name", ""),
                task.get("task_description", ""),
                task.get("status", "finished"),
                task.get("result", ""),
                task.get("error", ""),
                task.get("logs", []),
                task.get("summary", {}),
                session_id=task.get("session_id", ""),
            )
            imported += 1
        logger.info(f"[SubAgentManager] 旧任务表导入完成: {imported} 条")
        return imported


class SubAgentManager(QObject):
    """子智能体管理器 - 管理子智能体任务分发"""

    task_started = pyqtSignal(str, str, str)  # task_id, agent_name, task_description
    task_finished = pyqtSignal(str, str)  # task_id, result
    batch_finished = pyqtSignal(str, int)  # callback_content, failed（批次全部完成时触发）
    # ★ T24：子智能体 ask 权限请求转发（window_id, task_id, tool_name, arguments）→ 主线程弹窗
    permission_requested = pyqtSignal(str, str, str, dict)

    # [B2-3] 运行中任务硬上限：防御 exec 线程卡死（isFinished 恒 False）
    # 导致 _running_tasks 条目只增不减。超限**拒绝新任务**，而非挤出旧条目——
    # 旧条目挂着真实线程，挤出会丢掉它唯一的取消入口（cancel_all /
    # cancel_task 均按本 dict 定位），反而放大泄漏。正常并发远低于此值。
    # [T36] 200 → 64：与 _finished_tasks 的 maxlen=200 拉开距离。两者相等时，
    # 大量运行中任务拒绝记录会挤掉 _finished_tasks 里真实完成的结果。
    _MAX_RUNNING_TASKS = 64

    def __init__(self, agent_manager, tool_executor, get_llm_config: Callable):
        super().__init__()
        self._agent_manager = agent_manager
        self._tool_executor = tool_executor
        self._get_llm_config = get_llm_config
        self._running_tasks: Dict[str, SubAgentExecutor] = {}
        # H1：有界字典，防止 _finished_tasks 无限增长（进程生命周期内巨漏）；上限 200，超出弹最旧
        self._finished_tasks = _BoundedTaskDict(
            maxlen=200
        )  # task_id -> {"result": str, "error": str, "session_id": str}
        # 结算幂等标记：同一 task_id 只进一次完成链。executor 线程的两种出口
        # 信号（finished_with_result / error_occurred）与 stall 路径可能对同一
        # 任务先后到达，重复结算会让 _batch_completed 超发、下一批提前汇总。
        self._settled_tasks: set = set()
        self._task_store: Optional[SubAgentTaskStore] = None
        # 批次计数：本次启动的任务总数
        self._batch_total = 0
        self._batch_completed = 0
        # 当前批次的任务ID集合，用于回调时只通知本批次完成的任务
        self._batch_task_ids: set = set()
        # 已查询过的任务ID集合（按 session 隔离），避免重复返回结果浪费上下文
        # Dict[session_id, Set[task_id]]
        self._queried_tasks: Dict[str, set] = {}
        # 获取主智能体历史消息的回调（由外部设置）
        self._get_history_messages: Optional[Callable[[], List[Dict]]] = None
        # session.send（由宿主机从能力池注入）
        self._session_send: Optional[Callable[..., Any]] = None
        # 当前会话 ID（用于隔离不同会话的子智能体任务）
        self._current_session_id: str = ""
        # 最近一次派发任务所属的会话：交接切到新会话后，无参 subagent_status
        # 在当前会话无任务时回退到此，取回上一任的后台结果。
        self._last_task_session_id: str = ""

        # ========== 日志活力度 Stall 检测 ==========
        self._stall_timeout: int = 300  # 日志静默超时秒数（默认 5 分钟）
        # 单次 API/工具在途硬上限：超过即真死锁，避免豁免变成永久泄漏
        self.IN_FLIGHT_MAX_SECONDS = 1800
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(10000)  # 每 10 秒检查一次
        self._stall_timer.timeout.connect(self._check_stalled_tasks)

    def set_current_session_id(self, session_id: str):
        """设置当前会话 ID，用于会话隔离"""
        self._current_session_id = session_id

    def set_history_getter(self, getter: Callable[[], List[Dict]]):
        """设置获取主智能体历史消息的回调"""
        self._get_history_messages = getter

    def set_task_store(self, task_store: Optional[SubAgentTaskStore]):
        """注入任务日志存储（插件自有表，未注入则不落库）"""
        self._task_store = task_store

    def _save_task_to_store(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        status: str = "running",
        result: str = None,
        error: str = None,
        logs: List[Dict] = None,
        summary: Dict = None,
        session_id: str = "",
    ):
        """保存任务到数据库（插件表），携带会话 ID 以实现会话隔离"""
        if self._task_store is None:
            return
        try:
            if logs is None or summary is None:
                executor = self._running_tasks.get(task_id)
                if executor and hasattr(executor, "get_logs"):
                    logs = executor.get_logs()
                if executor and hasattr(executor, "get_summary"):
                    summary = executor.get_summary()
            self._task_store.save(
                task_id,
                agent_name,
                task_description,
                status,
                result,
                error,
                logs or [],
                summary or {},
                session_id=session_id or self._current_session_id,
            )
        except Exception as e:
            logger.error(f"[SubAgentManager] 保存任务到数据库失败: {e}")

    # ========== 日志活力度 Stall 检测 ==========

    def start_stall_detector(self):
        """启动 stall 检测定时器（默认在 SubAgentManager 初始化时未启动，由外部调用）"""
        if not self._stall_timer.isActive():
            self._stall_timer.start()
            logger.info("[SubAgentManager] Stall 检测器已启动")

    def stop_stall_detector(self):
        """停止 stall 检测定时器"""
        if self._stall_timer.isActive():
            self._stall_timer.stop()
            logger.info("[SubAgentManager] Stall 检测器已停止")

    def _check_stalled_tasks(self):
        """
        检查所有运行中任务的最后活跃时间，如果日志静默超过 stall_timeout 则 cancel。
        由 QTimer 定时触发（默认每 10 秒）。

        「日志静默」不等于卡死：长思考流式聚合、API 重试退避、长工具执行期间
        都不产日志。executor 暴露在途阶段（get_in_flight_duration），在途且未超
        单次硬上限时豁免；超过硬上限才按真死锁杀，防豁免变成永久线程泄漏。
        """
        import time

        now = time.time()
        for task_id in list(self._running_tasks.keys()):
            executor = self._running_tasks.get(task_id)
            if not executor or not executor.isRunning():
                continue

            last_activity = executor.get_last_activity_time()
            if not last_activity:
                continue

            idle_time = now - last_activity
            if idle_time <= self._stall_timeout:
                continue

            # 在途判定：有阻塞 API/工具在跑 → 合法静默，除非已超硬上限
            get_in_flight = getattr(executor, "get_in_flight_duration", None)
            inflight = get_in_flight() if callable(get_in_flight) else None
            if inflight is not None and inflight <= self.IN_FLIGHT_MAX_SECONDS:
                logger.debug(
                    f"[SubAgentManager] Task {task_id[:8]} 静默 {int(idle_time)}s "
                    f"但操作在途 {int(inflight)}s，豁免 stall"
                )
                continue

            if inflight is not None:
                error_msg = (
                    f"Task stalled: in-flight operation exceeded "
                    f"{self.IN_FLIGHT_MAX_SECONDS}s (no log activity for {int(idle_time)}s)"
                )
            else:
                error_msg = f"Task stalled: no log activity for {int(idle_time)}s (timeout={self._stall_timeout}s)"
            logger.warning(
                f"[SubAgentManager] ⚠️ Task {task_id[:8]} ({executor.agent_name}) "
                f"stalled for {int(idle_time)}s, cancelling ({error_msg})"
            )

            # 1. Cancel executor（透传真因，供 worker 终态归档/信号使用）
            try:
                executor.cancel(error_msg)
            except TypeError:  # 兼容旧签名调用方（无 reason 参数）
                executor.cancel()

            # 2. 写入 finished_tasks
            agent_name = executor.agent_name
            task_description = executor.task_description
            logs = executor.get_logs()
            task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "task_description": task_description,
                "session_id": task_session_id,
                "logs": logs,
            }

            # 3. 保存数据库（status="stalled"）
            self._save_task_to_store(
                task_id,
                agent_name,
                task_description,
                "stalled",
                "",
                error_msg,
                logs,
                session_id=task_session_id,
            )

            # 4. 从 running_tasks 移除（避免 get_finished_tasks 再处理一次）
            #    注意：executor 线程可能还在运行（卡在 API 调用中），
            #    但已经从管理器角度"移除"了，后续 finished_with_result 回调
            #    会因 task_id 不在 running_tasks 而被安全忽略。
            if task_id in self._running_tasks:
                del self._running_tasks[task_id]

            # 5. 统一结算：emit task_finished（UI 归档）+ 批次计数 + 全完成判定。
            #    此前 stall 路径只归档不计数，批次内一个任务 stall 即令批次汇总
            #    永久缺席（执行完/报错都无注入消息的直接成因之一）。线程随后
            #    真正退出再发 error_occurred 时由 _settled_tasks 幂等拦住。
            self._on_executor_finished(task_id, "")

    @staticmethod
    def _connect_cb(signal, callback, connection_type: int = None):
        """连接回调；connection_type 为 None 时沿用 Qt 默认（AutoConnection）。

        抽成方法是为了让「同步等待型」调用方能显式指定 DirectConnection——
        详见 execute_task 的 connection_type 文档。
        """
        if connection_type is None:
            signal.connect(callback)
        else:
            signal.connect(callback, connection_type)

    # ========== 完成链（manager 自持：计数/流式注入/全完成判定） ==========

    def _is_engine_streaming(self) -> bool:
        """主对话是否正在流式（经 tool_executor._backend.chat_engine 判定）。"""
        try:
            engine = getattr(getattr(self._tool_executor, "_backend", None), "chat_engine", None)
            return bool(engine and engine.is_streaming)
        except Exception:  # noqa: BLE001 - 状态读取失败按空闲处理
            return False

    def _resolve_result_injector(self):
        """从工具注册表取插件注入器（本插件 tools/subagent_tools 经 metadata 注册）。"""
        try:
            from app.tools.registry import ToolRegistry

            entry = ToolRegistry.get_instance().get("subagent_para")
            injector = (getattr(entry, "metadata", None) or {}).get("subagent_result_injector")
            if callable(injector):
                return injector
        except Exception as e:  # noqa: BLE001 - registry 异常时注入降级为跳过
            logger.warning(f"[SubAgentManager] 注入器查询失败，跳过结果注入: {e}")
            return None
        logger.warning("[SubAgentManager] subagent_para 未注册注入器（本插件 tools 缺失/旧版），跳过结果注入")
        return None

    def _get_session_send(self):
        """session.send 裸 callable（由窗口宿主从能力池注入）。"""
        if self._session_send is None:
            logger.warning("[SubAgentManager] session.send 不可用，跳过结果注入")
            return None
        return self._session_send

    def set_session_send(self, send: Optional[Callable[..., Any]]) -> None:
        """注入 session.send（host 从能力池取；不注入则结果注入降级为跳过）。"""
        self._session_send = send

    def _dispatch_result_injection(
        self, task_id: str, result: str, success: bool, agent_name: str, task_description: str
    ) -> None:
        """单条结果经插件注入器发送。

        mode=hook：流式中经 hook 通道静默注入当前对话上下文（不打断）；空闲时
        session.send 自动降级为普通发送开新轮。文案构建与通道选择均为插件所有物。
        """
        injector = self._resolve_result_injector()
        send = self._get_session_send()
        if injector is None or send is None:
            return
        try:
            delivered = injector(send, task_id, result, success, agent_name, task_description, mode="hook")
        except Exception as e:  # noqa: BLE001 - 插件异常不拖垮完成链
            logger.warning(f"[SubAgentManager] 注入器异常: {e}")
            return
        logger.debug(f"[SubAgentManager] 结果注入: task_id={task_id[:12]}, delivered={delivered}")

    def _batch_task_info(self, task_id: str) -> dict:
        """批次任务的完成信息：优先归档表，回退运行中 executor（同拍未归档场景）。"""
        info = self._finished_tasks.get(task_id) or {}
        if info:
            return info
        ex = self._running_tasks.get(task_id)
        if ex is not None:
            return {
                "result": getattr(ex, "_last_result", "") or "",
                "error": getattr(ex, "_execution_error", "") or "",
                "agent_name": getattr(ex, "agent_name", ""),
                "task_description": getattr(ex, "task_description", ""),
                "session_id": getattr(ex, "_task_session_id", ""),
            }
        return {}

    def _on_executor_error(self, task_id: str, error: str):
        """executor error 出口统一结算。

        run() 的 error_occurred（执行异常 / 被 cancel / agent 丢失）此前无人
        监听（subagent_para 传 on_error=None），_batch_completed 永久缺口 →
        批次汇总与单条注入永不触发，面板行也收不了尾。全部转入完成链：
        归档 + 批次计数 + 全完成判定。若线程之后又发 finished_with_result，
        由 _settled_tasks 拦住。
        """
        ex = self._running_tasks.get(task_id)
        if ex is not None and not getattr(ex, "_execution_error", None):
            ex._execution_error = error or "SubAgent execution error"
        self._on_executor_finished(task_id, "")

    def _on_executor_finished(self, task_id: str, result: str):
        """executor 完成回调（manager 内部接线，AutoConnection 落主线程）。

        职责：广播完成（面板收尾 + 归档）→ 单任务流式注入 → 批次计数 →
        全完成时汇总注入（流式）或广播 batch_finished（非流式，由 UI 层开新轮）。

        ``task_finished`` 必须在正常完成路径发射：面板行靠它打完成标记、
        窗口宿主靠它把任务归档进 _finished_tasks/插件表。此前只有 stalled /
        cancel 路径发射，行会永远停在「执行中」且计时器无限累加。
        """
        # 结算幂等：同一 task_id 只进一次完成链（error 与 finished 信号可能
        # 先后到达；stall 路径与线程退出信号亦然）。超发会令批次提前汇总。
        if task_id in self._settled_tasks:
            return
        self._settled_tasks.add(task_id)
        while len(self._settled_tasks) > 512:  # uuid 全局唯一，有界防涨
            self._settled_tasks.discard(next(iter(self._settled_tasks)))
        executor = self._running_tasks.get(task_id)
        success = not (getattr(executor, "_execution_error", None) or "")
        agent_name = getattr(executor, "agent_name", "") if executor else ""
        task_description = getattr(executor, "task_description", "") if executor else ""

        try:
            self.task_finished.emit(task_id, result)
        except Exception as e:  # noqa: BLE001 - 广播失败不阻断注入/批次汇总
            logger.error(f"[SubAgentManager] task_finished.emit 失败 (finished path): {e}")

        # 引擎状态在整条完成链内只判一次：单任务注入与批次汇总必须用同一口径，
        # 否则极端时序下会既逐条注入又带全文汇总，重复灌上下文。
        streaming = self._is_engine_streaming()
        # 流式中逐条静默注入；空闲时不逐条注入（避免 N 个任务开 N 轮对话），
        # 结果正文统一由下面的批次汇总承载。
        if streaming:
            self._dispatch_result_injection(task_id, result, success, agent_name, task_description)

        # 批次计数与全完成判定
        self._batch_completed += 1
        if self._batch_total <= 0 or self._batch_completed < self._batch_total:
            return

        batch_tasks = []
        for tid in list(self._batch_task_ids):
            info = self._batch_task_info(tid)
            error_text = info.get("error", "") or ""
            batch_tasks.append(
                {
                    "task_id": tid,
                    "agent_name": info.get("agent_name", "") or "",
                    "task_description": info.get("task_description", "") or "",
                    "success": not error_text,
                    "result": info.get("result", "") or "(无输出)",
                    "error": error_text,
                }
            )
        total = len(batch_tasks)
        failed = sum(1 for t in batch_tasks if not t["success"])
        task_lines = []
        for t in batch_tasks:
            status_icon = "✅" if t["success"] else "❌"
            desc_preview = t["task_description"][:50] + ("..." if len(t["task_description"]) > 50 else "")
            task_lines.append(f"- {status_icon} [{t['agent_name']}] {desc_preview} (id: {t['task_id']})")
        task_list_text = "\n".join(task_lines) if task_lines else "- (无任务详情)"
        callback_content = (
            f"子智能体全部完成。\n"
            f"共 {total} 个，成功 {total - failed} 个，失败 {failed} 个。\n"
            f"本次完成的任务：\n{task_list_text}"
        )
        if streaming:
            # 流式：单任务已逐条注入，汇总保持精简
            callback_content += "\n可使用 subagent_status 查询详细结果。"
        else:
            # 非流式（含交接切会话后的空闲窗口）：单任务 hook 注入未触发，
            # 结果正文必须随汇总这一轮带过去，否则新会话只看到任务清单。
            result_lines = []
            for t in batch_tasks:
                body = t["error"] if not t["success"] and t["error"] else t["result"]
                if len(body) > _MAX_BATCH_RESULT_CHARS:
                    body = body[:_MAX_BATCH_RESULT_CHARS] + "\n[... 已省略 ...]"
                result_lines.append(
                    f"### [{t['agent_name']}] {t['task_description'][:50]} (id: {t['task_id']})\n{body}"
                )
            callback_content += "\n\n各任务执行结果：\n" + "\n\n".join(result_lines)

        self._batch_total = 0
        self._batch_completed = 0
        self._batch_task_ids = set()

        if streaming:
            # 流式：汇总静默注入，不中断
            self._dispatch_result_injection("__batch_summary__", callback_content, failed == 0, "", "")
        else:
            # 非流式：广播给 UI 层开新轮（不中断语义已无意义，UI 决定呈现）
            try:
                self.batch_finished.emit(callback_content, failed)
            except Exception as e:
                logger.error(f"[SubAgentManager] batch_finished.emit failed: {e}")

    def execute_task(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        parent_context: str = "",
        on_finished: Callable[[str], None] = None,
        on_error: Callable[[str], None] = None,
        on_progress: Callable[[str], None] = None,
        executor_ref: Dict = None,
        share_context: bool = False,  # 是否共享主智能体上下文
        session_id: str = "",  # 所属会话 ID（任务创建时锁定，避免跨会话覆盖）
        llm_config: Dict = None,  # 可选：预解析的 LLM 配置（支持覆盖模型）
        connection_type: int = None,  # 回调连接方式；None=沿用默认 AutoConnection
    ) -> bool:
        """执行子智能体任务

        Args:
            session_id: 所属会话 ID。任务创建时即锁定该值，
                        后续回调不再读取全局 _current_session_id，
                        避免同一窗口内切换会话后异步回调用错 session_id。
            llm_config: 预解析的 LLM 配置（可选）。传入时跳过内部 _get_llm_config() 调用，
                        用于 --model=xxx 覆盖模型/服务商的场景。
            connection_type: on_finished/on_error/on_progress 的连接方式（Qt.ConnectionType）。
                默认 None 表示沿用 Qt 默认（AutoConnection）。

                ★ 调用方若要在回调里做「同步等待」（典型：workflow 插件 agent() 派发后
                用 threading.Event.wait() 阻塞等结果），必须显式传 Qt.DirectConnection(1)：
                AutoConnection 会把回调排进**发起连接那个线程**的事件循环，而该线程此刻正
                阻塞在 wait() 上——若它是普通 Python 线程（无 Qt 事件循环）更永不会投递，
                结果是子任务早已跑完、回调永不触发的**永久死锁**（无超时则永远不返回）。
                DirectConnection 让回调直接在 executor 线程里执行，等待方即可被唤醒。
        """
        # 在任务创建时即锁定 session_id，不依赖后续的全局状态
        task_session_id = session_id or self._current_session_id

        # 验证 agent 是否存在且是有效的子智能体类型
        agent = self._agent_manager.get_agent(agent_name)
        if not agent:
            error_msg = f"Agent not found: {agent_name}"
            logger.error(f"[SubAgentManager] {error_msg}")
            # 立即触发完成信号，让任务不卡在 running 状态
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "session_id": task_session_id,
            }
            if on_error:
                on_error(error_msg)
            return False

        # 只允许 mode 为 subagent 或 all 的 agent 作为子智能体
        if not agent.is_subagent():
            error_msg = f"Agent '{agent_name}' cannot be used as subagent (mode: {agent.mode})"
            logger.error(f"[SubAgentManager] {error_msg}")
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "session_id": task_session_id,
            }
            if on_error:
                on_error(error_msg)
            return False

        try:
            # [B2-3] 运行中任务硬上限：超限拒绝新任务并告警。
            # 拒绝而非挤出旧条目——旧条目挂着真实线程，挤出会丢失唯一取消入口。
            if len(self._running_tasks) >= self._MAX_RUNNING_TASKS:
                error_msg = (
                    f"Too many running subagent tasks: {len(self._running_tasks)}"
                    f" >= {self._MAX_RUNNING_TASKS}, task {task_id} rejected"
                )
                logger.warning(f"[SubAgentManager] {error_msg}")
                self._finished_tasks[task_id] = {
                    "result": "",
                    "error": error_msg,
                    "agent_name": agent_name,
                    "session_id": task_session_id,
                }
                if on_error:
                    on_error(error_msg)
                return False

            if llm_config is None:
                llm_config = self._get_llm_config()
            elif not isinstance(llm_config, dict):
                # 防御：非 dict 的 llm_config（可能从 main_widget 传入的异常值）
                logger.warning(
                    f"[SubAgentManager] execute_task llm_config is not a dict: {type(llm_config).__name__}={llm_config!r}, falling back to default"
                )
                llm_config = self._get_llm_config()
            if not llm_config:
                if on_error:
                    on_error("No LLM config available")
                return False

            # 如果启用共享上下文，将获取完整的上下文信息
            full_context = parent_context
            if share_context and self._get_history_messages:
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        # 格式化历史消息
                        history_lines = []
                        max_chars = 1000  # 完整上下文字符限制
                        for msg in history_messages:
                            role = msg.get("role", "user")
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                            if content:
                                truncated = content[:max_chars] + ("..." if len(content) > max_chars else "")
                                history_lines.append(f"**{role}**: {truncated}")

                        if history_lines:
                            history_section = "\n\n## 主智能体完整上下文\n" + "\n".join(history_lines)
                            if parent_context:
                                full_context = f"{parent_context}{history_section}"
                            else:
                                full_context = history_section.lstrip("\n\n")
                except Exception as e:
                    logger.warning(f"[SubAgentManager] 获取上下文失败: {e}")

            # 轮数上限：agent.steps 显式声明优先（None=激活策略兜底，默认 subagent 策略 30）
            max_iterations = agent.steps

            executor = SubAgentExecutor(
                task_id=task_id,
                agent_name=agent_name,
                task_description=task_description,
                llm_config=llm_config,
                agent_manager=self._agent_manager,
                tool_executor=self._tool_executor,
                parent_context=full_context,
                is_subagent_call=True,  # 标记为被主智能体调用
                max_iterations=max_iterations,  # 传递最大迭代次数限制
            )
            # 在 executor 上存储 session_id，供后续回调使用
            executor._task_session_id = task_session_id

            # 设置日志存储回调（用 lambda 默认参数在定义时锁定 session_id，避免闭包 late-binding）
            if self._task_store is not None:
                executor.set_log_store_callback(
                    lambda *args, _sid=task_session_id: self._save_task_to_store(*args, session_id=_sid)
                )

            # 【新增】设置历史消息获取回调
            if self._get_history_messages:
                executor.set_history_getter(self._get_history_messages)

            if executor_ref is not None:
                executor_ref["executor"] = executor

            if on_finished:
                self._connect_cb(executor.finished_with_result, on_finished, connection_type)
            if on_error:
                self._connect_cb(executor.error_occurred, on_error, connection_type)
            if on_progress:
                self._connect_cb(executor.progress_updated, on_progress, connection_type)

            # 完成链内部接线：manager 自持完成处理（批次计数/流式注入/全完成判定），
            # 不再绕道 main_widget 转发。恒 AutoConnection（回调落主线程，
            # 不跟随调用方 connection_type——同步等待型调用方的 DirectConnection
            # 会让本回调在 executor 线程执行，与主线程 UI 归档槽产生时序竞争）。
            executor.finished_with_result.connect(self._on_executor_finished)
            # error 出口同样进完成链：此前 error_occurred 无人监听（on_error
            # 为 None 的 subagent_para 路径），批次计数永久缺口 → 批次汇总/
            # 单条注入永不触发，模型永远等不到 [后台任务状态] 通知。
            executor.error_occurred.connect(self._on_executor_error)

            # ★ T24：转发子智能体 ask 权限请求到主线程（带 window_id 供多窗口定位弹窗）
            executor.permission_requested.connect(self._forward_permission_request)

            self._running_tasks[task_id] = executor
            executor.start()

            # 记录最近派发任务所属会话，供交接后 subagent_status 回退查询
            if task_session_id:
                self._last_task_session_id = task_session_id

            # 批次计数：本次启动的任务数
            self._batch_total += 1
            self._batch_task_ids.add(task_id)

            # 保存到数据库（传入锁定的 task_session_id）
            self._save_task_to_store(task_id, agent_name, task_description, "running", session_id=task_session_id)

            try:
                self.task_started.emit(task_id, agent_name, task_description)
            except Exception as e:
                logger.error(f"[SubAgentManager] task_started.emit failed: {e}", exc_info=True)

            logger.info(f"[SubAgentManager] Started task {task_id} with agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"[SubAgentManager] Failed to execute task: {e}")
            if on_error:
                on_error(str(e))
            return False

    def _forward_permission_request(self, task_id: str, tool_name: str, arguments: dict):
        """转发 executor 的权限请求到主线程（SubAgentManager → main_widget）。

        携带 window_id（来自 executor 所在窗口的 backend），供多窗口场景
        精确定位弹窗归属。
        """
        executor = self._running_tasks.get(task_id)
        window_id = ""
        if executor is not None and executor.tool_executor is not None:
            backend = getattr(executor.tool_executor, "_backend", None)
            if backend is not None:
                window_id = getattr(backend, "_window_id", "") or ""
        self.permission_requested.emit(window_id, task_id, tool_name, arguments)

    def respond_permission(self, task_id: str, allow: bool):
        """主线程响应用户决策 → 转交 executor（ask 分支继续/拒绝）。"""
        executor = self._running_tasks.get(task_id)
        if executor is not None:
            executor.respond_permission(allow)
        else:
            logger.warning(f"[SubAgentManager] respond_permission: task {task_id} 不存在")

    def cancel_task(self, task_id: str) -> bool:
        """取消子智能体任务"""
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]
            return True
        return False

    def get_running_tasks(self) -> List[str]:
        """获取正在运行的任务ID列表"""
        return list(self._running_tasks.keys())

    def _prune_queried_tasks(self) -> None:
        """惰性清理 _queried_tasks（原只增不删：session 键 + task_id 集合无界）。

        对账规则：queried 集合仅对 finished 任务标记，task_id 不在
        _finished_tasks（被 _BoundedTaskDict maxlen=200 挤出）即视为已消费
        → 移除；集合空则删除 session 键。与 _finished_tasks 同生命周期，
        无新线程/定时器。挂载点 get_finished_tasks 覆盖高频路径；判据与
        get_all_active_tasks_with_details 遍历源同一字典，对两个入口都安全。
        """
        for sid in list(self._queried_tasks.keys()):
            queried = self._queried_tasks.get(sid)
            if not queried:
                self._queried_tasks.pop(sid, None)
                continue
            # 对账：不在 _finished_tasks 的 task_id 已被挤出上限 → 移除
            for tid in [t for t in queried if t not in self._finished_tasks]:
                queried.discard(tid)
            if not queried:
                self._queried_tasks.pop(sid, None)

    def get_finished_tasks(self) -> List[str]:
        """获取已完成的任务ID列表（清理已完成的从running列表）"""
        self._prune_queried_tasks()
        finished = []
        for task_id in list(self._running_tasks.keys()):
            executor = self._running_tasks[task_id]
            if executor.isFinished():
                # 通过公共属性获取结果
                result = executor.last_result or ""
                error = executor.execution_error or ""
                agent_name = executor.agent_name
                task_description = executor.task_description
                logs = executor.get_logs()
                tool_call_count = executor.tool_call_count
                elapsed = int(time.time() - executor.start_time) if executor.start_time else 0

                task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
                # 如果 _on_sub_agent_task_finished 已经写入过，避免覆盖已有字段
                if task_id not in self._finished_tasks:
                    self._finished_tasks[task_id] = {
                        # [H5-2] 兕底归档路径（executor 完成信号未走 host 归档时
                        # 在此落归档）与 mark_task_finished 同标准截 32KB；
                        # _save_task_to_store 仍传全文（DB 全文优先，见下方调用）。
                        "result": _cap_text(result, _ARCHIVED_RESULT_MAX_CHARS, "归档结果"),
                        "error": error,
                        "agent_name": agent_name,
                        "task_description": task_description,
                        "session_id": task_session_id,
                        "logs": logs,
                        "tool_call_count": tool_call_count,
                        "elapsed_seconds": elapsed,
                    }
                else:
                    # 更新关键字段，保留 session_id 等已有数据
                    self._finished_tasks[task_id].setdefault("session_id", task_session_id)
                    self._finished_tasks[task_id].setdefault("logs", logs)
                    self._finished_tasks[task_id].setdefault("tool_call_count", tool_call_count)
                    self._finished_tasks[task_id].setdefault("elapsed_seconds", elapsed)
                    # 总是更新 result/error（_on_sub_agent_task_finished 可能拿到更准的数据）
                    if result is not None:
                        # [H5-2] 同上：兕底归档路径同标准截 32KB，DB 全文见下方
                        self._finished_tasks[task_id]["result"] = _cap_text(
                            result, _ARCHIVED_RESULT_MAX_CHARS, "归档结果"
                        )
                    if error is not None:
                        self._finished_tasks[task_id]["error"] = error

                # 更新数据库（传入锁定的 session_id）
                self._save_task_to_store(
                    task_id, agent_name, task_description, "finished", result, error, session_id=task_session_id
                )

                del self._running_tasks[task_id]
                finished.append(task_id)
        return finished

    def cancel_all(self):
        """取消所有运行中的子智能体任务 + 停止 Stall 检测器

        用于窗口关闭时清理当前窗口的所有子智能体任务，防止线程泄漏。
        """
        # 先停止 stall 检测器，避免在取消过程中触发额外的回调
        self.stop_stall_detector()

        # 取消所有运行中的任务
        for task_id in list(self._running_tasks.keys()):
            try:
                executor = self._running_tasks[task_id]
                agent_name = executor.agent_name
                task_description = executor.task_description
                logs = executor.get_logs()
                task_session_id = getattr(executor, "_task_session_id", self._current_session_id)

                # 写入 finished_tasks（与 _check_stalled_tasks / cancel_task 一致）
                error_msg = "Task cancelled: window closed"
                # 标记取消并透传真因（线程在下一次检查点退出）
                try:
                    executor.cancel(error_msg)
                except TypeError:
                    executor.cancel()
                self._finished_tasks[task_id] = {
                    "result": "",
                    "error": error_msg,
                    "agent_name": agent_name,
                    "task_description": task_description,
                    "session_id": task_session_id,
                    "logs": logs,
                }
                # 保存到数据库
                self._save_task_to_store(
                    task_id,
                    agent_name,
                    task_description,
                    "cancelled",
                    "",
                    error_msg,
                    logs,
                    session_id=task_session_id,
                )
                # 发送完成信号让 UI 知道
                try:
                    self.task_finished.emit(task_id, "")
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[SubAgentManager] cancel_all: 取消任务 {task_id} 时出错: {e}")

        self._running_tasks.clear()

    def mark_task_finished(self, task_id: str, result: str, error: str = "") -> Dict:
        """任务完成归档：写入内存 _finished_tasks，并把数据库状态推进到 finished。

        数据库在运行期间由 executor 实时日志回调持续写 running 状态；本方法是
        正常完成路径上唯一把 DB 推进到 finished 的入口。若缺失，get_task_logs
        查库命中 running 即返回，会话卡片（SubAgentSessionCard）会永远显示
        「执行中」，工具数/耗时冻结在最后一条日志时刻。

        Returns:
            Dict: {agent_name, task_description, session_id} 供批次汇总/流式注入使用
        """
        executor = self._running_tasks.pop(task_id, None)
        if executor is not None:
            agent_name = executor.agent_name
            task_description = executor.task_description
            task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
            logs = executor.get_logs()
            summary = executor.get_summary()
            final_error = error or ""
        else:
            # executor 已被其他路径归档（get_finished_tasks / DAG 提前删除），
            # 从已有条目恢复字段；条目可能已带更准的 error（如 DAG 跳过信息），不覆盖
            existing = self._finished_tasks.get(task_id, {})
            agent_name = existing.get("agent_name", "")
            task_description = existing.get("task_description", "")
            task_session_id = existing.get("session_id", "")
            logs = existing.get("logs")
            summary = None
            final_error = existing.get("error") or (error or "")

        if task_id in self._finished_tasks:
            entry = self._finished_tasks[task_id]
            # [H5-2] 内存归档截 32KB；DB 侧 update_status 仍传全文（见下），
            # UI 读取 DB 优先无感。唯一退化场景：DB 写失败时 UI 回退到内存
            # 归档，「查看结果」会变短（32KB + 提示文案）——此为本改动唯一
            # 行为差异，仅在 storage.db 不可用/写失败时出现。
            entry["result"] = _cap_text(result, _ARCHIVED_RESULT_MAX_CHARS, "归档结果")
            if not entry.get("error"):
                entry["error"] = final_error
            entry.setdefault("agent_name", agent_name)
            entry.setdefault("task_description", task_description)
            entry.setdefault("session_id", task_session_id)
            if logs:
                entry.setdefault("logs", logs)
        else:
            self._finished_tasks[task_id] = {
                # [H5-2] 同上：内存归档截 32KB，DB 全文无感
                "result": _cap_text(result, _ARCHIVED_RESULT_MAX_CHARS, "归档结果"),
                "error": final_error,
                "agent_name": agent_name,
                "task_description": task_description,
                "session_id": task_session_id,
                "logs": logs or [],
                "tool_call_count": (summary or {}).get("tool_call_count", 0),
                "elapsed_seconds": (summary or {}).get("elapsed_seconds", 0),
            }

        # 落库用部分更新语义（None 字段不覆盖）：executor 缺席时 summary 传 None，
        # 保留运行期最后写入的 tool_call_count/elapsed_seconds，避免被清空
        if self._task_store is not None:
            try:
                self._task_store.update_status(task_id, "finished", result, final_error, logs, summary)
            except Exception as e:
                logger.warning(f"[SubAgentManager] mark_task_finished 落库失败: {e}")

        return {
            "agent_name": agent_name,
            "task_description": task_description,
            "session_id": task_session_id,
        }

    def get_task_logs(self, task_id: str) -> Dict:
        """
        获取指定任务的完整日志和摘要。

        Returns:
            Dict: {
                "summary": {...},  # 任务摘要
                "logs": [...],      # 日志列表
                "found": bool       # 是否找到任务
            }
        """
        # 先归档已完成的任务（会同步把 DB 状态推进到 finished），再查库。
        # 顺序很关键：executor 运行期间实时日志回调持续写 DB running 状态，
        # 若先查库命中即返回，get_finished_tasks 永不执行，DB 状态永挂 running，
        # 会话卡片（SubAgentSessionCard）会永远显示「执行中」且工具数/时间冻结。
        self.get_finished_tasks()

        # 从数据库获取
        if self._task_store is not None:
            db_task = self._task_store.get(task_id)
            if db_task:
                summary = db_task.get("summary", {})
                # 确保 task_description 在 summary 中
                if not summary.get("task_description"):
                    summary["task_description"] = db_task.get("task_description", "")
                # 确保 task_id 在 summary 中
                if not summary.get("task_id"):
                    summary["task_id"] = task_id
                return {
                    "task_id": task_id,
                    "summary": summary,
                    "logs": db_task.get("logs", []),
                    "found": True,
                    "status": db_task.get("status", "unknown"),
                    "agent_name": db_task.get("agent_name", ""),
                    "task_description": db_task.get("task_description", ""),
                    "session_id": db_task.get("session_id", ""),
                    "result": db_task.get("result", ""),
                    "error": db_task.get("error", ""),
                }

        # 检查运行中的任务
        if task_id in self._running_tasks:
            executor = self._running_tasks[task_id]
            return {
                "summary": executor.get_summary(),
                "logs": executor.get_logs(),
                "found": True,
                "status": "running",
                "session_id": getattr(executor, "_task_session_id", ""),
            }

        # 检查已完成的任务（内存）
        if task_id in self._finished_tasks:
            task_info = self._finished_tasks[task_id]
            return {
                "summary": {
                    "task_id": task_id,
                    "agent_name": task_info.get("agent_name", ""),
                    "task_description": task_info.get("task_description", ""),
                    "result": task_info.get("result", ""),
                    "error": task_info.get("error", ""),
                    "tool_call_count": task_info.get("tool_call_count", 0),
                    "elapsed_seconds": task_info.get("elapsed_seconds", 0),
                },
                "logs": task_info.get("logs", []),
                "found": True,
                "status": "finished",
                "session_id": task_info.get("session_id", ""),
            }

        return {"summary": {}, "logs": [], "found": False, "status": "unknown"}

    def latest_task_for_session(self, session_id: str) -> Dict:
        """该会话最近一个任务（会话卡显示时恢复内容）。

        优先级：插件表（更新最全，跨重启仍在）> 内存运行中 > 内存归档。
        返回值与 ``get_task_logs`` 同形，可直接灌给会话卡。
        """
        if session_id and self._task_store is not None:
            row = self._task_store.latest_for_session(session_id)
            if row and row.get("task_id"):
                summary = dict(row.get("summary") or {})
                summary.setdefault("task_id", row["task_id"])
                summary.setdefault("agent_name", row.get("agent_name", ""))
                summary.setdefault("task_description", row.get("task_description", ""))
                summary.setdefault("result", row.get("result", ""))
                summary.setdefault("error", row.get("error", ""))
                return {
                    "found": True,
                    "task_id": row["task_id"],
                    "agent_name": row.get("agent_name", ""),
                    "logs": row.get("logs", []),
                    "summary": summary,
                    "status": row.get("status", ""),
                    "session_id": row.get("session_id", ""),
                }

        for task_id, executor in reversed(list(self._running_tasks.items())):
            if session_id and getattr(executor, "_task_session_id", "") not in (session_id, ""):
                continue
            return {
                "found": True,
                "task_id": task_id,
                "agent_name": getattr(executor, "agent_name", ""),
                "logs": executor.get_logs(),
                "summary": executor.get_summary(),
                "status": "running",
                "session_id": getattr(executor, "_task_session_id", ""),
            }

        for task_id, info in reversed(list(self._finished_tasks.items())):
            if session_id and info.get("session_id", "") != session_id:
                continue
            return {
                "found": True,
                "task_id": task_id,
                "agent_name": info.get("agent_name", ""),
                "logs": info.get("logs", []),
                "summary": {
                    "task_id": task_id,
                    "agent_name": info.get("agent_name", ""),
                    "task_description": info.get("task_description", ""),
                    "result": info.get("result", ""),
                    "error": info.get("error", ""),
                    "tool_call_count": info.get("tool_call_count", 0),
                    "elapsed_seconds": info.get("elapsed_seconds", 0),
                },
                "status": "finished",
                "session_id": info.get("session_id", ""),
            }

        return {"found": False}

    def get_tasks_status_with_details(
        self, task_ids: List[str], with_log: bool = False, with_result: bool = True, session_id: str = None
    ) -> ToolResult:
        """获取指定任务的详细状态（按 task_id 精确查询，无会话限制）

        与 get_all_active_tasks_with_details 不同，本方法用于按 explicit task_id
        查询特定任务，因此不执行会话隔离。只要 task_id 存在就能查到结果。

        Args:
            session_id: 保留参数（不再用于会话隔离），兼容历史调用方
        """
        tasks_info = []
        for tid in task_ids:
            task_data = self.get_task_logs(tid)
            if not task_data.get("found"):
                tasks_info.append(
                    {
                        "task_id": tid,
                        "status": "unknown",
                        "agent": "",
                    }
                )
                continue

            status = task_data.get("status", "unknown")
            task_info = {
                "task_id": tid,
                "status": status,
                "agent": task_data.get("summary", {}).get("agent_name", task_data.get("agent_name", "")),
            }

            # 显式按 ID 查询始终返回完整结果（不应用 _already_queried 限制）
            # _already_queried 仅在 get_all_active_tasks_with_details 无条件返回时生效

            # running / finishing：任务还没结束，注入 _hint 提醒调用方不要重复查询
            if status in ("running"):
                summary = task_data.get("summary", {}) or {}
                elapsed = summary.get("elapsed_seconds", 0) or 0
                tool_calls = summary.get("tool_call_count", 0) or 0
                task_info["tool_call_count"] = tool_calls
                task_info["_hint"] = (
                    f"⏳ 该任务还在后台运行中（已用时 {elapsed}s，已调用 {tool_calls} 次工具）。"
                    "**请勿重复调用 subagent_status 等待结果**——"
                    "任务完成后系统会自动通过 `[后台任务状态]` 用户消息通知，"
                    "届时再调用本工具获取详细结果。"
                )

            # 是否包含结果
            if with_result:
                result = task_data.get("result") or task_data.get("summary", {}).get("result", "")
                error = task_data.get("error") or task_data.get("summary", {}).get("error", "")
                if result:
                    # [H5-3] 本路径结果直接注入父对话上下文（LLM 可见），无上限
                    # 会灌爆主对话；16KB 足够还原结论，全文应落盘后用 read 取回。
                    task_info["result"] = _cap_text(result, _INJECTED_RESULT_MAX_CHARS, "注入上下文的结果")
                if error:
                    task_info["error"] = error

            # 是否包含日志
            if with_log:
                logs = task_data.get("logs", [])
                if logs:
                    task_info["logs"] = logs

            tasks_info.append(task_info)

        return ToolResult(True, content={"tasks": tasks_info})

    def get_all_active_tasks_with_details(
        self, with_log: bool = False, with_result: bool = True, session_id: str = None
    ) -> ToolResult:
        """获取当前会话任务的详细状态，同时包含运行中与已完成。

        默认按会话隔离；当前会话无任务且存在最近派发会话（交接切会话场景）时，
        回退到该会话取回后台结果，当前会话一旦有任务即恢复严格隔离。

        Args:
            with_log: 是否包含执行日志
            with_result: 是否包含执行结果
            session_id: 会话 ID，None 时使用 _current_session_id

        Returns:
            ToolResult: content={"tasks": [...]}
                - finished 任务：按"会话内只查一次"策略返回完整 result/error
                - running/finishing 任务：附带 _hint 提醒调用方不要重复查询
        """
        target_session = session_id or self._current_session_id
        # 交接切会话后：新会话还没有自己的任务时，回退到最近派发任务的会话，
        # 让无参 subagent_status 能取回上一任的后台结果（同一段工作的延续）。
        # 当前会话一旦有任务即恢复严格隔离，不混入旧会话任务。
        if target_session and self._last_task_session_id != target_session:
            has_own = any(
                info.get("session_id", "") == target_session for info in self._finished_tasks.values()
            ) or any(
                getattr(ex, "_task_session_id", "") == target_session for ex in self._running_tasks.values()
            )
            if not has_own and self._last_task_session_id:
                target_session = self._last_task_session_id
        tasks_info = []

        for task_id, task_info in self._finished_tasks.items():
            # 会话隔离：只返回当前会话的任务
            task_session = task_info.get("session_id", "")
            # 注意: 当 task_session 为空（旧记录/边缘情况）时，也视为不属于当前会话
            if target_session and task_session != target_session:
                continue

            # 跳过当前正在运行的任务（它们应该由 _running_tasks 处理）
            if task_id in self._running_tasks:
                continue

            task_entry = {
                "task_id": task_id,
                "session_id": task_session,
                "status": "finished",
                "agent": task_info.get("agent_name", ""),
                "task_description": task_info.get("task_description", ""),
            }

            # 完成或失败只能查一次（按 session 隔离）
            session_queried = self._queried_tasks.get(target_session, set())
            if task_id in session_queried:
                task_entry["_already_queried"] = True
                task_entry["_message"] = "已查询过结果，可通过 id 再次查询"
                tasks_info.append(task_entry)
                continue

            session_queried.add(task_id)
            self._queried_tasks[target_session] = session_queried

            if with_result:
                result = task_info.get("result", "") or ""
                error = task_info.get("error", "") or ""
                if result:
                    # [H5-3] 与 get_tasks_status_with_details 同标准：本路径结果
                    # 直接注入父对话上下文（LLM 可见），16KB 上限防灌爆。
                    # [T26] 输入是内存归档文本（H5-2 已截 32KB），其「原长」是
                    # 下界非真值——文案必须让 LLM 知道这点，否则会低估全文规模
                    # 而放弃 read 落盘文件。
                    if len(result) > _INJECTED_RESULT_MAX_CHARS:
                        task_entry["result"] = (
                            result[:_INJECTED_RESULT_MAX_CHARS]
                            + f"\n…[结果已截断：归档层原长 {len(result)} 字符，原始全文可能更长]"
                        )
                    else:
                        task_entry["result"] = result
                if error:
                    task_entry["error"] = error
            if with_log:
                logs = task_info.get("logs", [])
                if logs:
                    task_entry["logs"] = logs

            tasks_info.append(task_entry)

        # 补充：运行中的任务也一并返回，附带 _hint 提醒调用方不要重复轮询
        for task_id, executor in self._running_tasks.items():
            # 会话隔离
            if target_session:
                task_session = getattr(executor, "_task_session_id", "")
                if task_session != target_session:
                    continue

            try:
                summary = executor.get_summary() or {}
            except Exception:
                summary = {}
            elapsed = summary.get("elapsed_seconds", 0) or 0
            tool_calls = summary.get("tool_call_count", 0) or 0
            status = "running" if executor.isRunning() else "finishing"

            task_entry = {
                "task_id": task_id,
                "session_id": getattr(executor, "_task_session_id", ""),
                "status": status,
                "agent": summary.get("agent_name", ""),
                "task_description": summary.get("task_description", ""),
                "elapsed_seconds": elapsed,
                "tool_call_count": tool_calls,
                "_hint": (
                    f"⏳ 该任务还在后台运行中（已用时 {elapsed}s，已调用 {tool_calls} 次工具）。"
                    "**请勿重复调用 subagent_status 等待结果**——"
                    "任务完成后系统会自动通过 `[后台任务状态]` 用户消息通知，"
                    "届时再调用本工具获取详细结果。"
                )
                if status == "running"
                else "",
            }

            if with_log:
                try:
                    logs = executor.get_logs()
                except Exception:
                    logs = []
                if logs:
                    task_entry["logs"] = logs

            tasks_info.append(task_entry)

        return ToolResult(True, content={"tasks": tasks_info})


def create_manager(backend: Any, caps: Any = None) -> SubAgentManager:
    """宿主工厂入口：窗口宿主 ``SubAgentHost`` 调用本函数建实例。

    依赖全部取自宿主公开面：``agent_manager`` / ``tool_executor``（backend 属性）
    与能力池（模型解析）。子智能体默认模型的解析属业务，留在本插件内。
    """

    def _get_llm_config() -> Dict:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.llm_subagent_default_model.value
        if caps is not None:
            if saved and "model.config_for" in caps:
                try:
                    resolved = caps["model.config_for"](saved)
                except Exception as e:  # noqa: BLE001 - 解析失败回退当前模型
                    logger.warning(f"[SubAgentManager] 子智能体默认模型解析失败: {e}")
                    resolved = None
                if resolved:
                    return resolved
            if "model.config" in caps:
                try:
                    current = caps["model.config"]()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[SubAgentManager] 当前模型配置读取失败: {e}")
                    current = None
                if current:
                    return current
        return {}

    return SubAgentManager(
        agent_manager=getattr(backend, "agent_manager", None),
        tool_executor=getattr(backend, "tool_executor", None),
        get_llm_config=_get_llm_config,
    )
