# -*- coding: utf-8 -*-
"""workflow 工具 — 受限 Python 脚本编排子智能体。

脚本在受限命名空间内 exec（containment，非安全边界），经钩子扇出子智能体；
agent() 走 SubAgentManager.execute_task + Event 同步等待，子任务自动进任务体系。
"""
from __future__ import annotations

import ast
import builtins
import datetime
import hashlib
import inspect
import json
import math
import re
import shutil
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import orjson
from loguru import logger

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.tools.result import ToolResult
from app.tools.registry import make_summarize_from_preview

PLUGIN_NAME = "workflow"

GROUP_SUBAGENT = "子智能体"

# 宿主 render_helpers._MAX_OUTPUT_CHARS：工具结果文本超过这个长度会被截断，
# 渲染闭包拿到的是截断后的字符串（JSON 不再合法）。这里留出余量做让位与抢救。
_HOST_RENDER_CAP = 5000


def _parse_aliases(raw: str | None) -> dict[str, str]:
    """解析 model_aliases 配置："别名=模型ID" 逗号分隔；残缺段跳过不报错。"""
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


class WorkflowError(Exception):
    """钩子误用 / 上限触发（杀全脚本，模型可修正后重发）"""


class WorkflowTimeoutError(WorkflowError):
    """run 总时长超限"""


# 钩子签名提示：TypeError 翻译层与工具 description 共用的契约文本
_HOOK_SIGNATURE_HINT = (
    "agent(prompt, agent=角色, phase=分组, label=标签, model=别名, schema=JSONSchema) / "
    "parallel([零参函数]) / pipeline(items, *stages) / phase(title, detail=None) / log(msg)"
)


def translate_type_error(e: TypeError, hook_names: tuple) -> str | None:
    """TypeError 含钩子痕迹（<lambda> 或钩子名）时翻译成人话；否则 None 交回原路径。

    钩子 def 化后报错自带函数名（phase() takes ...）；lambda 时期报错是
    _workflow_impl.<locals>.<lambda>——两种形态都要接住。返回文本必须让模型
    无需读源码即可自愈：点出肇事参数 + 正确签名 + 预置名单。
    """
    msg = str(e)
    named = [h for h in hook_names if h in msg]
    if "<lambda>" not in msg and not named:
        return None
    who = "/".join(named) if named else "某个钩子"
    return (
        f"钩子调用签名错误（{who}）: {msg}。正确签名: {_HOOK_SIGNATURE_HINT}。"
        "沙箱仅预置钩子与 json/math/re/statistics/datetime、args，"
        "钩子不接受 model 之外未知的关键字参数。"
    )


# 白名单式内置函数（containment：白名单外的名字一律 NameError）
_ALLOWED_BUILTINS: dict = {
    "None": None,
    "True": True,
    "False": False,
}
_ALLOWED_BUILTINS.update(
    (name, getattr(builtins, name))
    for name in (
        "NotImplemented", "Ellipsis",
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
        "callable", "chr", "dict", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "getattr", "hasattr", "hash", "hex", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "object", "oct", "ord", "pow", "range", "repr",
        "reversed", "round", "set", "setattr", "slice", "sorted", "str",
        "sum", "tuple", "type", "zip",
        "ArithmeticError", "AssertionError", "AttributeError", "Exception",
        "IndexError", "KeyError", "LookupError", "NameError", "TypeError",
        "ValueError", "ZeroDivisionError", "RuntimeError", "StopIteration",
    )
)

# 预置只读常用模块（脚本只做协调与数据变换，不开放 __import__）
def _restricted_datetime():
    """datetime 模块的受限副本：now/today/utcnow 抛 WorkflowError。

    CC 同款语义（Date.now()/Math.random() 直接 throw）：时间源不确定会让
    resume 重放时 agent 调用序列漂移。需要时间戳请通过 args 传入。
    """
    import types

    mod = types.ModuleType("datetime")
    mod.__dict__.update(datetime.__dict__)

    def _banned(*a, **kw):
        raise WorkflowError(
            "datetime.now/today/utcnow 已禁用（保证 resume 指纹确定性）；需要时间戳请通过 args 传入"
        )

    mod.datetime = type(
        "datetime",
        (datetime.datetime,),
        {"now": staticmethod(_banned), "today": staticmethod(_banned), "utcnow": staticmethod(_banned)},
    )
    return mod


_PRESET_MODULES: dict = {
    "json": json,
    "math": math,
    "re": re,
    "statistics": statistics,
    "datetime": _restricted_datetime(),
}


def _build_sandbox(args, hooks: dict | None = None) -> dict:
    """构建受限命名空间：白名单 builtins + 预置只读模块 + 钩子 + args。"""
    ns = {"__builtins__": dict(_ALLOWED_BUILTINS)}
    ns.update(_PRESET_MODULES)
    if hooks:
        ns.update(hooks)
    ns["args"] = args
    return ns


def _check_imports(script: str, preset_modules: dict) -> str | None:
    """exec 前拦截脚本 import：沙箱无 __import__，第一时间报人话并列出预置模块。

    import 预置模块（如 import json）单独点名：直接删掉 import 行即可用。
    """
    banned: set = set()
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.Import):
            banned.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            banned.add(node.module.split(".")[0])
    if not banned:
        return None
    preset_hits = sorted(banned & set(preset_modules))
    real_banned = sorted(banned - set(preset_modules))
    parts = []
    if preset_hits:
        parts.append(
            f"{', '.join(preset_hits)} 已预置（直接用，删掉 import 行即可）"
        )
    if real_banned:
        parts.append(f"{', '.join(real_banned)} 禁止 import（沙箱无 __import__）")
    return (
        f"脚本 import 问题: {'；'.join(parts)}。"
        f"全部可用预置模块: {', '.join(sorted(preset_modules))}；"
        "路径拼接用 f-string 或 '/'.join，脚本无文件/网络能力。"
    )


def _check_underscore_names(script: str, ns: dict) -> str | None:
    """exec 前拦截脚本对宿主内部名（_ 开头）的引用，返回错误描述或 None。

    模型上下文里见过宿主变量（如 _HOST_RENDER_CAP）就可能写进脚本；沙箱没有这个名字，
    NameError 要等脚本跑到引用点才炸——此前扇出的 agent 全部白跑。预检把失败提前到零成本。
    保守策略：只拦 _ 开头的 Load 名；绑定收集从宽（赋值/参数/导入/except as 全算已定义），
    宁可漏报不误报。reserved 集合直接从沙箱 ns 推导，钩子增减无漂移。
    """
    bound: set = set()
    loads: set = set()
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.Name):
            (loads if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    reserved = set(ns) - {"__builtins__"}
    unknown = sorted(n for n in loads if n.startswith("_") and n not in bound and n not in reserved)
    if not unknown:
        return None
    return (
        f"脚本引用了沙箱不存在的名字: {', '.join(unknown)}。"
        f"沙箱预置: {', '.join(sorted(reserved))}；"
        "宿主内部变量（_ 开头）不在脚本命名空间，result 超长由宿主自行截断，脚本无需处理。"
    )


class _RunState:
    """单次 run 的额度、时长与中止状态（线程安全）。"""

    def __init__(self, max_total_agents: int, deadline: float):
        self._lock = threading.Lock()
        self._max_total = max_total_agents
        self._deadline = deadline
        self._abort = threading.Event()
        self._waiters: set = set()
        self.started = 0

    @property
    def deadline(self) -> float:
        """run 总截止时间（time.monotonic 基准）。"""
        return self._deadline

    def _check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise WorkflowTimeoutError("workflow 超过总时长上限（deadline 已过）")

    def check(self) -> None:
        """只查时长，不计数（parallel/pipeline 入口用）。"""
        self._check_deadline()

    def reserve(self) -> None:
        """检查点：时长超限抛 WorkflowTimeoutError；总数超限抛 WorkflowError；通过则原子计数。"""
        self._check_deadline()
        with self._lock:
            if self.started >= self._max_total:
                raise WorkflowError(f"子智能体总数超上限（{self._max_total}）")
            self.started += 1

    # ---- 中止：run 被杀时立刻唤醒所有阻塞中的 agent()，避免线程悬挂到 deadline ----

    def aborted(self) -> bool:
        return self._abort.is_set()

    def abort(self) -> None:
        self._abort.set()
        with self._lock:
            waiters = list(self._waiters)
        for ev in waiters:
            ev.set()

    def track_waiter(self, ev: threading.Event) -> None:
        with self._lock:
            self._waiters.add(ev)

    def untrack_waiter(self, ev: threading.Event) -> None:
        with self._lock:
            self._waiters.discard(ev)


class RunJournal:
    """单次 run 的运行日志：journal.jsonl（事件流）+ status.json（卡片数据源，原子写）。

    resume 靠 journal 回放：agent 指纹 = sha256(prompt+role+model+schema)，
    指纹命中且状态 done 的子任务直接回放结果不再真跑。
    """

    def __init__(self, run_dir):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._agent_seq = 0

    # ---- 事件流 ----

    def append(self, type: str, **fields) -> dict:
        """追加一行事件（线程安全）；返回写入的完整记录。"""
        with self._lock:
            self._seq += 1
            rec = {"ts": round(time.time(), 3), "seq": self._seq, "type": type, **fields}
            with open(self.dir / "journal.jsonl", "a", encoding="utf-8") as f:
                f.write(orjson.dumps(rec).decode() + "\n")
            return rec

    def record_phase(self, title: str, detail: str | None = None) -> None:
        self.append("phase", title=str(title), detail=None if detail is None else str(detail))

    def record_agent_start(self, agent_key: str, fingerprint: str, role: str, model: str | None) -> None:
        self.append("agent_start", agent_key=agent_key, fingerprint=fingerprint, role=role, model=model)

    def record_agent_end(self, agent_key: str, status: str, elapsed_sec: float, result) -> None:
        self.append("agent_end", agent_key=agent_key, status=status, elapsed_sec=round(elapsed_sec, 3), result=result)

    # ---- 指纹与回放 ----

    @staticmethod
    def fingerprint(prompt: str, role: str, model: str | None, schema) -> str:
        payload = orjson.dumps(
            {"p": prompt, "r": role, "m": model, "s": schema},
            option=orjson.OPT_SORT_KEYS,
        )
        return hashlib.sha256(payload).hexdigest()[:16]

    def completed_map(self) -> dict:
        """{agent_key: {fingerprint, status, result}}：仅含已终结（done/replayed）的 agent。"""
        jf = self.dir / "journal.jsonl"
        if not jf.exists():
            return {}
        out: dict = {}
        with open(jf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if rec.get("type") == "agent_start":
                    out.setdefault(rec["agent_key"], {})['fingerprint'] = rec.get("fingerprint")
                elif rec.get("type") == "agent_end" and rec.get("status") in ("done", "replayed"):
                    out.setdefault(rec["agent_key"], {}).update(
                        {"status": rec["status"], "result": rec.get("result")}
                    )
        return {k: v for k, v in out.items() if "result" in v and "fingerprint" in v}

    # ---- 卡片数据源 ----

    def next_agent_key(self) -> str:
        """脚本内 agent() 调用序号（1 起）：resume 回放的回放定位锚。"""
        with self._lock:
            self._agent_seq += 1
            return f"a{self._agent_seq}"

    def agent_snapshots(self) -> list:
        """从 journal 聚合每 agent 最新状态：[{key, role, status, elapsed_sec, result}]。

        start 无 end → running；卡片数据源与 resume 诊断共用。
        """
        jf = self.dir / "journal.jsonl"
        if not jf.exists():
            return []
        order: list = []
        by_key: dict = {}
        with open(jf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                key = rec.get("agent_key")
                if not key:
                    continue
                if rec.get("type") == "agent_start":
                    if key not in by_key:
                        order.append(key)
                        by_key[key] = {
                            "key": key,
                            "role": rec.get("role"),
                            "status": "running",
                            "elapsed_sec": None,
                            "result": None,
                        }
                elif rec.get("type") == "agent_end" and key in by_key:
                    by_key[key]["status"] = rec.get("status")
                    by_key[key]["elapsed_sec"] = rec.get("elapsed_sec")
                    by_key[key]["result"] = rec.get("result")
        return [by_key[k] for k in order]

    def write_status(self, payload: dict) -> None:
        """原子写 status.json：tmp+replace，卡片侧永远读到完整 JSON。"""
        with self._lock:
            tmp = self.dir / f"status.json.tmp.{threading.get_ident()}"
            tmp.write_bytes(orjson.dumps(payload))
            tmp.replace(self.dir / "status.json")


def _direct_connection() -> int:
    """Qt.DirectConnection 的值（1）。延迟导入，保持本模块导入期不依赖 Qt。"""
    try:
        from PyQt5.QtCore import Qt

        return int(Qt.DirectConnection)
    except Exception:  # pragma: no cover - Qt 不可用时退回字面量
        return 1


# 等待切片：即使没有中止事件也按此间隔醒一次，兼顾 deadline 检查与响应速度
_WAIT_SLICE = 0.5


def _manager_supports_kwarg(manager, name: str) -> bool:
    """宿主 execute_task 是否接受某个关键字参数。

    核心代码（app/）不走插件热重载：宿主任进程若在我方改核心之前启动，内存里仍是旧签名，
    直接传新参数会抛 TypeError 而不是降级。插件必须自适应宿主版本。
    """
    try:
        params = inspect.signature(manager.execute_task).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _make_agent_hook(
    manager, session_id: str, state: _RunState, default_agent: str, max_agent_wait: float = 900.0,
    model_aliases: dict | None = None, log_fn=None, journal: "RunJournal | None" = None,
    resume_map: dict | None = None,
):
    """agent(prompt, agent=None, label=None, phase=None, share_context=False) -> str | None

    execute_task 异步派发 SubAgentExecutor；Event 在 executor 线程被回调置位。
    子任务失败（on_error / execute_task 返回 False / 等待超时）降级为 None，由脚本兜底。

    ★ 两个必须同时成立的防挂条件（缺一必现「子任务跑完了但脚本永远不动」）：
      1) connection_type=DirectConnection。默认 AutoConnection 会把回调排进「发起连接那个
         线程」的事件循环，而该线程此刻正阻塞在 wait() 上；parallel/pipeline 的 worker 还
         是普通 Python 线程、根本没有 Qt 事件循环 → 回调永不投递 → 永久死锁。
      2) 等待必须有截止时间。子任务被 stall 检测器取消、或 executor 异常退出时，不会发射
         finished_with_result，无超时就永远等不到。超时按「子任务失败」处理，降 None。
    """
    direct = _direct_connection()
    # 宿主核心版本自适应：app/ 核心不走插件热重载，宿主任进程可能还跑在旧签名上，
    # 直接传新参数会抛 TypeError（而不是降级），所以先探再传。
    supports_conn_type = _manager_supports_kwarg(manager, "connection_type")
    supports_executor_ref = _manager_supports_kwarg(manager, "executor_ref")
    supports_model = _manager_supports_kwarg(manager, "model")
    if not supports_conn_type and not supports_executor_ref:
        logger.warning(
            "[workflow] 宿主 SubAgentManager.execute_task 既不支持 connection_type 也不支持 "
            "executor_ref：子任务回调大概率无法投递，将依赖等待上界降级（建议重启宿主）"
        )
    elif not supports_conn_type:
        logger.warning(
            "[workflow] 宿主 SubAgentManager.execute_task 不支持 connection_type，"
            "改用 executor_ref 事后补挂直连回调兜底（建议重启宿主以加载最新核心）"
        )

    def agent(prompt, agent=None, label=None, phase=None, share_context=False, model=None, schema=None):
        # 参数名 agent 遮蔽外层函数名：本函数体内不再引用自身，合法且对模型最自然
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError("agent() 的 prompt 必须是非空字符串")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            raise WorkflowError("agent() 的 agent 必须是非空字符串")
        # model 别名 → 实际模型 ID；未注册别名写人话日志并降级 None（模型可读 logs 自愈），
        # 不消耗 run 额度
        resolved_model = None
        if model is not None:
            resolved_model = (model_aliases or {}).get(str(model))
            if resolved_model is None:
                available = ", ".join(sorted(model_aliases)) if model_aliases else "（未配置 model_aliases）"
                hint = f"[workflow] 未知模型别名: {model}。可用别名: {available}"
                logger.warning(hint)
                if log_fn:
                    log_fn(hint)
                return None
        if state.aborted():
            return None
        name = agent or default_agent
        # agent_key 按 agent() 调用序号分配（入口处一次）：schema 重试共用同 key，
        # 否则 resume 按序号回放时会错位
        agent_key = journal.next_agent_key() if journal else ""

        def _dispatch(effective_prompt: str) -> str | None:
            """派发一个子任务并同步等待：额度→派发→等待→超时/中止降级。schema 重试复用。"""
            state.reserve()
            fp = journal.fingerprint(effective_prompt, name, resolved_model, schema) if journal else ""
            t0 = time.monotonic()
            status, out = "failed", None
            if journal:
                journal.record_agent_start(agent_key, fp, name, resolved_model)
                # resume 回放：同序号 + 指纹一致（prompt/角色/model/schema 未变）→ 直接回放不真跑
                hit = (resume_map or {}).get(agent_key)
                if hit and hit.get("fingerprint") == fp:
                    journal.record_agent_end(agent_key, "replayed", 0.0, hit.get("result"))
                    logger.info(f"[workflow] resume 回放 {agent_key}（指纹命中，跳过真跑）")
                    return hit.get("result")
            task_id = str(uuid.uuid4())
            done = threading.Event()
            box: dict = {"result": None, "settled": False}

            def _on_finished(tid, text):
                # 信号签名 (task_id, result)：PyQt 双参 emit，回调签名必须双参否则静默 TypeError
                box["result"] = text
                box["settled"] = True
                done.set()

            def _on_error(tid, err):
                # 回调可能被挂两次（核心 connection_type + executor_ref 兜底），只记第一次
                if not box["settled"]:
                    logger.warning(f"[workflow] 子任务失败 ({label or name}): {err}")
                box["settled"] = True
                done.set()

            def _attach_direct(executor):
                """再补挂一组 DirectConnection 回调（对旧核心是唯一救命手段）。

                真实核心的 execute_task 在 start() 之前就把 executor 写进 executor_ref，
                子智能体跑完通常要数秒，而 start() 到本行只有毫秒级，竞态窗口可忽略；
                真撞上了还有下面的等待上界兜底。
                """
                if executor is None:
                    return
                for sig_name, cb in (("finished_with_result", _on_finished), ("error_occurred", _on_error)):
                    sig = getattr(executor, sig_name, None)
                    if sig is None:
                        continue
                    try:
                        sig.connect(cb, direct)
                    except Exception as e:  # 补挂失败不影响降级路径
                        logger.debug(f"[workflow] 补挂 {sig_name} 直连回调失败: {e}")

            ref: dict = {}
            call: dict = {
                "task_id": task_id,
                "agent_name": name,
                "task_description": effective_prompt,
                "on_finished": _on_finished,
                "on_error": _on_error,
                "share_context": bool(share_context),
                "session_id": session_id,
            }
            if supports_conn_type:
                call["connection_type"] = direct
            if supports_executor_ref:
                call["executor_ref"] = ref
            # 旧宿主无 model 形参时静默跳过（与 connection_type 同款自适应），不白跑也不炸
            if resolved_model is not None and supports_model:
                call["model"] = resolved_model

            ok = manager.execute_task(**call)
            if not ok:
                if journal:
                    journal.record_agent_end(agent_key, "failed", time.monotonic() - t0, None)
                return None
            _attach_direct(ref.get("executor"))

            # 截止时间 = min(run 总 deadline, 单 agent 等待上限)，杜绝无限期阻塞
            deadline = min(state.deadline, time.monotonic() + max_agent_wait)
            state.track_waiter(done)
            try:
                while not state.aborted() and not done.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done.wait(min(remaining, _WAIT_SLICE))
            finally:
                state.untrack_waiter(done)

            if not box["settled"]:
                if not state.aborted() and time.monotonic() > state.deadline:
                    _cancel(manager, task_id)
                    raise WorkflowTimeoutError("workflow 超过总时长上限（等待子任务时 deadline 已过）")
                logger.warning(
                    f"[workflow] 子任务未返回结果 ({label or name})："
                    f"{'run 已中止' if state.aborted() else f'等待超过 {max_agent_wait:.0f}s'}，取消并降级 None"
                )
                _cancel(manager, task_id)
                if journal:
                    journal.record_agent_end(agent_key, "failed", time.monotonic() - t0, None)
                return None
            status, out = "done", box["result"]
            if journal:
                journal.record_agent_end(agent_key, status, time.monotonic() - t0, out)
            return box["result"]

        # ---- schema 结构化输出：注入指令 → 校验 → 失败带错重试 1 次 → 仍失败降 None ----

        def _validate_schema(s: dict, text: str):
            import jsonschema  # 延迟导入：插件加载期不付成本（dingtalk_stream 教训）

            data = _extract_json(text)
            if data is None:
                raise ValueError("回复中未找到可解析的 JSON（已尝试整体/围栏/首尾大括号提取）")
            jsonschema.validate(data, s)
            return data

        def _dispatch_with_schema(p: str, s: dict):
            base = p + (
                "\n\n[输出格式硬约束] 只输出一个符合以下 JSON Schema 的 JSON 对象，"
                "禁止任何多余文本、代码围栏或解释：\n" + json.dumps(s, ensure_ascii=False)
            )
            text = _dispatch(base)
            if text is None:
                # 子任务本身失败/返回空：重试注定同样结果，注明原因直接降级
                if log_fn:
                    log_fn("[workflow] schema_failed: 子任务未返回文本，无法校验")
                return None
            try:
                return _validate_schema(s, text)
            except Exception as first_err:
                retry = base + f"\n\n[重试] 上次输出未通过校验: {first_err}。严格遵守 JSON Schema 重新输出。"
                text2 = _dispatch(retry)
                err = first_err
                if text2 is not None:
                    try:
                        return _validate_schema(s, text2)
                    except Exception as second_err:
                        err = second_err
                logger.warning(f"[workflow] schema 校验失败降级 None: {err}")
                if log_fn:
                    log_fn(f"[workflow] schema_failed: {err}")
                return None

        if schema is None:
            return _dispatch(prompt)
        return _dispatch_with_schema(prompt, schema)

    return agent


def _cancel(manager, task_id: str) -> None:
    """尽力取消子任务；取消失败不影响降级路径。"""
    try:
        manager.cancel_task(task_id)
    except Exception as e:
        logger.debug(f"[workflow] cancel_task({str(task_id)[:8]}) 失败: {e}")


_COMBINATOR_LOCAL = threading.local()


def _pool_initializer():
    """线程池 worker 打标：池内任务禁止再调 parallel/pipeline（防池耗尽死锁）。"""
    _COMBINATOR_LOCAL.in_pool_worker = True


def _make_combinators(state: _RunState, pool: ThreadPoolExecutor, max_items: int):
    """parallel / pipeline 钩子工厂。共享线程池；并发上限 = 池大小。"""

    def _pool_guard():
        if getattr(_COMBINATOR_LOCAL, "in_pool_worker", False):
            raise WorkflowError("池内任务禁止调用 parallel/pipeline（防死锁），thunk 内只调 agent()")

    def parallel(callables):
        _pool_guard()
        items = list(callables)
        if len(items) > max_items:
            raise WorkflowError(f"parallel 单次项数超上限（{max_items}）")
        state.check()  # 只查时长；agent 额度在各 thunk 内的 agent() 里计
        futs = [pool.submit(c) for c in items]
        out = []
        for f in futs:
            try:
                out.append(f.result())
            except WorkflowError:
                raise
            except Exception as e:
                logger.warning(f"[workflow] parallel 项异常降级 None: {e}")
                out.append(None)
        return out

    def pipeline(items, *stages):
        _pool_guard()
        if not stages:
            raise WorkflowError("pipeline 至少需要一个 stage")
        item_list = list(items)
        if len(item_list) > max_items:
            raise WorkflowError(f"pipeline 单次项数超上限（{max_items}）")
        state.check()

        def _run_item(item):
            prev = item
            for idx, stage in enumerate(stages):
                if state.aborted():
                    return None
                try:
                    prev = stage(prev, item, idx)
                except WorkflowError:
                    raise
                except Exception as e:
                    logger.warning(f"[workflow] pipeline 阶段 {idx} 异常，该项降级 None: {e}")
                    return None
            return prev

        futs = [pool.submit(_run_item, it) for it in item_list]
        out = []
        for f in futs:
            try:
                out.append(f.result())
            except WorkflowError:
                raise
            except Exception as e:
                logger.warning(f"[workflow] pipeline 项异常降级 None: {e}")
                out.append(None)
        return out

    return parallel, pipeline


# ============================================================
# schema / impl / register
# ============================================================

def _workflow_description(subagent_names: list) -> str:
    """workflow 工具的动态描述：完整用法契约 + 可用角色列表（get_builtin_tools_schema 调用）。

    description 是模型写脚本的第一信息源：钩子签名、沙箱边界、result 约定必须全部显式，
    否则模型按直觉猜 API（phase 双参/model 参数/import）→ 每次报错。
    """
    base = (
        "运行受限 Python 编排脚本，扇出子智能体。适合大规模多智能体编排（审计/迁移/多角度研究/对抗验证）；"
        "一两个委派用 subagent_para，固定依赖图用 subagent_dag。\n\n"
        "脚本是同步 Python，顶层直接执行，最终结果赋给 result 变量（未赋则为 null）。\n\n"
        "钩子（def 定义，签名严格）：\n"
        "- agent(prompt, agent=角色, phase=分组, label=标签, model=别名, schema=JSONSchema) -> str|None："
        "跑一个子智能体到完成，返回最终文本，失败/超时返回 None；"
        "model 需在插件配置 model_aliases 里注册别名（如 sonnet=模型ID），未注册别名降 None 并在日志提示；"
        "schema 传 JSON Schema dict 时返回校验通过的 dict（失败自动带错重试 1 次，仍失败降 None）\n"
        "- parallel([零参函数]): 并发执行并等全部（屏障）；异常项降 None；不支持嵌套\n"
        "- pipeline(items, *stages): 每项独立流过 stage(prev, item, index)，无屏障；阶段异常该项降 None 跳后续\n"
        "- phase(title, detail=None): 进度分组，detail 上卡片副标题\n"
        "- log(msg): 进度消息\n\n"
        "复用与后台：action=save/list/load 管理可复用 workflow（from_saved 直接跑已存档）；"
        "默认后台运行立即返回 run_id，用 action=status 查进度；foreground=true 同步等结果；"
        "action=resume + run_id 从中断处续跑（prompt 拼时间戳/随机数会指纹漂移导致全量重跑，保持 prompt 确定性）。\n\n"
        "沙箱定位：containment（防误用围栏），非安全边界——不要把不可信内容交给脚本处理；"
        "历史 run 目录按 max_runs_kept 滚动清理。\n\n"
        "沙箱边界：禁止 import（预置 json/math/re/statistics/datetime）；无文件/网络能力；"
        "宿主内部变量（_ 开头）不在脚本命名空间。\n"
        "误用钩子或超上限会中止整个脚本；子任务失败只降 None。"
    )
    if subagent_names:
        base += "\n\n可用角色: " + ", ".join(subagent_names)
    return base


_WORKFLOW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow",
        # description 运行时由 get_builtin_tools_schema 用 _workflow_description 动态覆盖（含角色列表）
        "description": _workflow_description([]),
        "parameters": {
            "type": "object",
            "properties": {
                "meta": {
                    "type": "object",
                    "description": "工作流身份块（纯 JSON）：name(kebab-case 必填) + description(必填)",
                    "properties": {
                        "name": {"type": "string", "description": "短名，kebab-case"},
                        "description": {"type": "string", "description": "一句话说明这个工作流做什么"},
                    },
                    "required": ["name", "description"],
                },
                "script": {
                    "type": "string",
                    "description": "受限 Python 脚本体：顶层直接执行，最终结果赋给 result 变量。"
                    "钩子：agent(prompt, agent=角色, phase=分组) 跑子智能体返最终文本（失败 None）；"
                    "parallel([零参函数]) 并发等全部；pipeline(items, *stages) 无屏障流水线；"
                    "phase(title)/log(msg) 记进度。预置 json/math/re/statistics/datetime，"
                    "无文件/网络能力。脚本必须通过钩子干活。",
                },
                "args": {
                    "type": "object",
                    "description": "可选 JSON 输入，暴露为脚本全局 args",
                },
                "action": {
                    "type": "string",
                    "enum": ["run", "save", "list", "load", "status", "resume"],
                    "description": "默认 run 执行脚本；save 存为可复用 workflow（需 save_as）；list 列已存与最近 runs；load 读存档（需 name）；status 查任务（需 run_id）；resume 从原 run 回放续跑（已完成且指纹一致的 agent 不重跑，编辑脚本后只重跑变化部分）",
                },
                "foreground": {
                    "type": "boolean",
                    "description": "同步等待到完成。仅当用户配置允许时生效；用户关闭同步执行（default_foreground=false）后传 true 会被拒绝——此时省略本参数用后台模式 + action=status 查询即可",
                },
                "from_saved": {
                    "type": "string",
                    "description": "运行已存 workflow 的名字（代替 script），args 可覆盖存档默认值",
                },
                "save_as": {
                    "type": "string",
                    "description": "action=save 时的存档名",
                },
                "name": {
                    "type": "string",
                    "description": "action=load 时的存档名",
                },
                "run_id": {
                    "type": "string",
                    "description": "action=status 时的运行 ID",
                },
            },
            "required": ["script", "meta"],
        },
    },
}


def _make_phase_log_hooks(phases: list, logs: list, journal: "RunJournal | None" = None):
    """phase/log 钩子工厂：def 化（报错自带函数名，翻译层可点名）。

    phase(title, detail=None)：detail 上卡片副标题，兼容旧单参调用；
    条目统一为 {title, detail} dict（detail 可为 None）。log(msg, *_) 容忍多余参数。
    传入 journal 时同步落盘。
    """

    def phase(title, detail=None):
        entry = {"title": str(title), "detail": None if detail is None else str(detail)}
        phases.append(entry)
        if journal:
            journal.record_phase(entry["title"], entry["detail"])

    def log(msg, *_):
        logs.append(str(msg))
        if journal:
            journal.append("log", msg=str(msg))

    return phase, log


def _norm_phases(raw) -> list[tuple[str, str | None]]:
    """渲染层 phases 归一化：兼容 dict 条目（title+detail）与旧字符串条目。"""
    out: list[tuple[str, str | None]] = []
    for p in raw or []:
        if isinstance(p, dict):
            out.append((str(p.get("title") or ""), None if p.get("detail") is None else str(p.get("detail"))))
        else:
            out.append((str(p), None))
    return out


# ============================================================
# 存档层：saved / runs 目录管理
# ============================================================


def wf_root() -> Path:
    """workflow 存档根：<app_data_dir>/workflows/（saved/ + runs/）。"""
    from app.utils.utils import get_app_data_dir  # 延迟导入：与插件加载器同款

    return get_app_data_dir() / "workflows"


def _saved_path(root: Path, name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    return root / "saved" / f"{safe or 'unnamed'}.py"


def save_workflow(root: Path, name: str, meta: dict, script: str, args: dict | None) -> Path:
    """保存可复用 workflow：头注释行存 meta+args JSON，正文是脚本原文。同名覆盖。

    存一个跑不了的脚本没有意义：meta.name 必须非空，script 必须过语法检查。
    """
    if not isinstance(meta, dict) or not str(meta.get("name") or "").strip():
        raise ValueError("保存 workflow 需要 meta.name 非空")
    try:
        ast.parse(script)
    except SyntaxError as e:
        raise ValueError(f"脚本语法错误，不予保存: {e}") from e
    path = _saved_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = orjson.dumps({"meta": meta, "args": args}).decode()
    path.write_text(f"# workflow-meta: {header}\n{script}", encoding="utf-8")
    return path


def prune_runs(root: Path, keep: int, exclude: Path | None = None) -> int:
    """滚动清理 runs/ 目录：按 mtime 降序只保留最新 keep 个（saved/ 资产不碰）。

    keep<=0 表示全清（测试用）；exclude 用于保护正在写入的 run。
    """
    runs_dir = root / "runs"
    if not runs_dir.is_dir():
        return 0
    dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for d in dirs[max(0, keep):]:
        # 活跃 run 保护：占保留名额但跳过删除（总数仍 ≤ keep）
        if exclude is not None and d.resolve() == exclude.resolve():
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except Exception as e:  # 单个删除失败不影响其余
            logger.debug(f"[workflow] 清理 run 目录失败 {d.name}: {e}")
    return removed


def load_workflow(root: Path, name: str) -> dict:
    path = _saved_path(root, name)
    if not path.exists():
        raise KeyError(f"workflow 不存在: {name}")
    text = path.read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    envelope: dict = {}
    if first.startswith("# workflow-meta:"):
        try:
            envelope = orjson.loads(first[len("# workflow-meta:") :].strip())
        except orjson.JSONDecodeError:
            envelope = {}
    return {"name": name, "meta": envelope.get("meta") or {}, "args": envelope.get("args"), "script": body}


def list_workflows(root: Path) -> dict:
    saved_dir = root / "saved"
    runs_dir = root / "runs"
    saved = sorted(p.stem for p in saved_dir.glob("*.py")) if saved_dir.is_dir() else []
    runs = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            if d.is_dir():
                runs.append(d.name)
    return {"saved": saved, "runs": runs}


def new_run_dir(root: Path, meta: dict, script: str, args) -> Path:
    """创建 runs/<run_id>/ 并写入脚本与 meta 快照；返回 run 目录。"""
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    rd = root / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "script.py").write_text(script, encoding="utf-8")
    (rd / "meta.json").write_bytes(orjson.dumps({"meta": meta, "args": args}))
    return rd



_ACTIVE_RUNS: dict = {}
_RUNS_LOCK = threading.Lock()


def _execute_run(cfg: dict) -> ToolResult:
    """执行一次 workflow 脚本（前台/后台共用）：沙箱→预检→exec→payload。

    journal 存在时同步落盘：phases/logs/agent start/end → journal.jsonl；
    终态写 status.json（卡片数据源）与 result.json。
    """
    meta: dict = cfg["meta"]
    script: str = cfg["script"]
    journal: RunJournal | None = cfg.get("journal")
    phases: list = []
    logs: list = []

    def _finish_status(ok: bool, result, note: str) -> None:
        with _RUNS_LOCK:
            rec = _ACTIVE_RUNS.get(cfg.get("run_id") or "")
            if rec is not None:
                rec["state"] = "done" if ok else "error"
        if not journal:
            return
        journal.write_status({
            "name": meta.get("name"),
            "description": meta.get("description"),
            "state": "done" if ok else "error",
            "agents_started": state.started,
            "phases": phases,
            "logs": logs,
            "agents": journal.agent_snapshots(),
            "result": result,
            "note": note,
        })
        if ok:
            (cfg["run_dir"] / "result.json").write_bytes(orjson.dumps(result))

    state = _RunState(cfg["max_total"], time.monotonic() + cfg["max_duration"])
    pool = ThreadPoolExecutor(
        max_workers=max(1, cfg["max_concurrent"]),
        initializer=_pool_initializer,
        thread_name_prefix="wf-agent",
    )
    phases: list = []
    logs: list = []
    phase_hook, log_hook = _make_phase_log_hooks(phases, logs, journal)

    try:
        parallel_hook, pipeline_hook = _make_combinators(state, pool, cfg["max_items"])
        ns = _build_sandbox(
            args=cfg["args"],
            hooks={
                "agent": _make_agent_hook(
                    cfg["manager"],
                    cfg["session_id"],
                    state,
                    cfg["default_agent"],
                    cfg["max_agent_wait"],
                    model_aliases=cfg["model_aliases"],
                    log_fn=log_hook,
                    journal=journal,
                    resume_map=cfg.get("resume_map"),
                ),
                "parallel": parallel_hook,
                "pipeline": pipeline_hook,
                "phase": phase_hook,
                "log": log_hook,
            },
        )
        # 预检：import 与宿主内部名引用在 exec 前拦截，避免 agent 扇出后才炸白跑
        precheck_err = _check_imports(script, _PRESET_MODULES) or _check_underscore_names(script, ns)
        if precheck_err:
            _finish_status(False, None, f"预检失败: {precheck_err}")
            return ToolResult(False, error=f"脚本预检失败: {precheck_err}")
        exec(compile(script, "<workflow>", "exec"), ns)  # noqa: S102 - 受限命名空间，containment 定位
        result = ns.get("result")
        result_note = ""
        try:
            orjson.dumps(result)
        except (TypeError, ValueError):
            result = {"_repr": str(result)}
            result_note = "result 不可 JSON 序列化，已转为字符串"

        content = {
            "workflow": meta.get("name"),
            "agents_started": state.started,
            "phases": phases,
            "logs": logs,
            "result": result,
        }
        if result_note:
            content["_note"] = result_note
        payload = _dump_content(content)
        # max_chars：给模型的预算，超了先砍日志
        if len(payload) > cfg["max_chars"] and content["logs"]:
            content["logs"] = content["logs"][:10]
            content["_note"] = (result_note + " 结果超长，logs 截断").strip()
            payload = _dump_content(content)
        # _HOST_RENDER_CAP：宿主渲染层对结果文本的硬上限（render_helpers._MAX_OUTPUT_CHARS）。
        # 超了会被截断、渲染闭包就解析不出 JSON；日志在 UI 里本来是收起的，再让一步。
        if len(payload) > _HOST_RENDER_CAP and content["logs"]:
            content["logs"] = content["logs"][:5]
            payload = _dump_content(content)
        _finish_status(True, result, result_note)
        return ToolResult(True, content=payload)
    except WorkflowError as e:
        _finish_status(False, None, f"执行中止: {e}")
        return ToolResult(False, error=f"workflow 执行中止: {e}")
    except NameError as e:
        # 预检只拦 _ 开头的名字；其他未定义名（钩子拼错等）走到这里，附预置名清单让模型自愈
        _finish_status(False, None, f"NameError: {e}")
        return ToolResult(
            False,
            error=(
                f"workflow 脚本异常: NameError: {e}。"
                "沙箱仅预置 agent/parallel/pipeline/phase/log、json/math/re/statistics/datetime、args；"
                "宿主内部名不在脚本命名空间"
            ),
        )
    except Exception as e:
        _finish_status(False, None, f"{type(e).__name__}: {e}")
        return ToolResult(False, error=f"workflow 脚本异常: {type(e).__name__}: {e}")
    finally:
        # 收尾：先置中止标志唤醒所有仍在等子任务的 worker（含线程池里的），再关池。
        # 否则异常路径下这些线程会一直挂到各自的截止时间才退出。
        state.abort()
        pool.shutdown(wait=False, cancel_futures=True)
        # 滚动清理旧 runs（异步）：journal 结果全文较大，不滚动会无限增长
        keep = int(cfg.get("max_runs_kept") or 0)
        if keep > 0 and cfg.get("prune_root"):
            threading.Thread(
                target=prune_runs,
                args=(Path(cfg["prune_root"]), keep, Path(cfg["run_dir"])),
                daemon=True,
                name=f"wf-prune-{cfg.get('run_id')}",
            ).start()


_RUN_STATE_LABEL = {
    "done": "完成",
    "replayed": "回放",
    "failed": "失败",
    "running": "运行中",
    "schema_failed": "校验失败",
    "none": "降级",
    "error": "异常",
}
_RUN_STATE_COLOR = {
    "done": "#2ea44f",
    "replayed": "#8250df",
    "failed": "#cf222e",
    "running": "#0969da",
    "schema_failed": "#bf8700",
    "none": "var(--text-secondary)",
    "error": "#cf222e",
}


def _render_run_card(status: dict) -> str:
    """运行状态卡：phase 分组时间线 + agent 状态行 + 汇总（action=status, html=true 用）。"""
    from app.widgets.render_helpers import escape, scale_font_size

    fs = scale_font_size(12)
    fs_small = scale_font_size(11)
    name = str(status.get("name") or "workflow")
    state = str(status.get("state") or "running")
    phases = _norm_phases(status.get("phases"))
    agents = status.get("agents") or []

    parts = [
        f'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px;">'
        f'<b style="font-size:{scale_font_size(13)}px;color:var(--text);">{escape(name)}</b>'
        f'<span style="font-size:{fs_small}px;color:{_RUN_STATE_COLOR.get(state, "var(--text-secondary)")};">'
        f'{_RUN_STATE_LABEL.get(state, state)}</span></div>'
    ]

    def _pill(label: str, color: str) -> str:
        return (
            f'<span style="display:inline-flex;align-items:center;padding:1px 8px;margin:0 6px 4px 0;'
            f'border:1px solid var(--border);border-radius:999px;font-size:{fs_small}px;color:{color};">'
            f'{escape(label)}</span>'
        )
    if phases:
        rows = ""
        for title, detail in phases:
            d_html = (
                f'<span style="color:var(--text-secondary);font-size:{fs_small}px;"> — {escape(detail)}</span>'
                if detail
                else ""
            )
            rows += (
                f'<div style="display:flex;align-items:baseline;gap:6px;padding:2px 0;">'
                f'<span style="flex:0 0 auto;width:6px;height:6px;border-radius:50%;'
                f'background:var(--accent);margin-top:6px;"></span>'
                f'<span style="color:var(--text);font-size:{fs}px;">{escape(title)}{d_html}</span></div>'
            )
        parts.append(
            f'<div style="border-left:1px solid var(--border);padding-left:8px;margin:6px 0 8px 2px;">{rows}</div>'
        )
    if agents:
        rows = ""
        for a in agents:
            st = str(a.get("status") or "running")
            label = _RUN_STATE_LABEL.get(st, st)
            color = _RUN_STATE_COLOR.get(st, "var(--text-secondary)")
            elapsed = a.get("elapsed_sec")
            t_html = f" · {elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""
            rows += (
                f'<div style="display:flex;align-items:baseline;gap:6px;padding:1px 0;font-size:{fs_small}px;">'
                f'<span style="width:8px;height:8px;border-radius:50%;flex:0 0 auto;'
                f'background:{color};margin-top:4px;"></span>'
                f'<span style="color:var(--text);">{escape(str(a.get("role") or ""))}</span>'
                f'<span style="color:{color};">{escape(label)}{t_html}</span></div>'
            )
        parts.append(f'<div style="margin:4px 0 8px 2px;">{rows}</div>')
    counters: dict = {}
    for a in agents:
        st = str(a.get("status") or "running")
        counters[st] = counters.get(st, 0) + 1
    if counters:
        pills = "".join(
            _pill(f"{_RUN_STATE_LABEL.get(k, k)} × {v}", _RUN_STATE_COLOR.get(k, "var(--text-secondary)"))
            for k, v in counters.items()
        )
        parts.append(f'<div style="margin-top:2px;">{pills}</div>')
    note = str(status.get("note") or "")
    if note:
        parts.append(
            f'<div style="color:var(--text-secondary);font-size:{fs_small}px;margin-top:4px;">{escape(note)}</div>'
        )
    return (
        f'<div class="wf-run-card" style="padding:2px 0;">{"".join(parts)}</div>'
    )


def _extract_json(text: str):
    """从模型回复提取 JSON：整体解析 → 剥 markdown 围栏 → 首个{到最后一个}块。

    子 agent 输出「说明文字 + ```json 围栏```」时裸 json.loads 必失败，
    重试也难改输出习惯——提取容错比重试省钱。
    """
    s = (text or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i : j + 1])
        except Exception:
            return None
    return None


def _workflow_impl(tool_ctx, **kwargs):
    # ---- action 分发：存档管理类动作不需要 manager ----
    action = str(kwargs.get("action") or "run")
    meta = kwargs.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = orjson.loads(meta)
        except orjson.JSONDecodeError:
            return ToolResult(False, error="meta 不是合法 JSON")
    if action in ("list", "load", "save", "status"):
        root = wf_root()
        if action == "list":
            return ToolResult(True, content=_dump_content(list_workflows(root)))
        if action == "load":
            name = str(kwargs.get("name") or "")
            if not name:
                return ToolResult(False, error="action=load 需要 name 参数")
            try:
                wf = load_workflow(root, name)
            except KeyError as e:
                return ToolResult(False, error=str(e).strip("'\""))
            return ToolResult(True, content=_dump_content(wf))
        if action == "save":
            save_as = str(kwargs.get("save_as") or "")
            script_str = str(kwargs.get("script") or "")
            if not save_as or not script_str:
                return ToolResult(False, error="action=save 需要 save_as 与 script 参数")
            save_workflow(root, save_as, meta if isinstance(meta, dict) else {}, script_str, kwargs.get("args"))
            return ToolResult(True, content={"saved": save_as})
        run_id = str(kwargs.get("run_id") or "")
        if not run_id:
            return ToolResult(False, error="action=status 需要 run_id 参数")
        with _RUNS_LOCK:
            live = _ACTIVE_RUNS.get(run_id)
        sf = root / "runs" / run_id / "status.json"
        if live is None and not sf.exists():
            return ToolResult(False, error=f"未找到 run: {run_id}")
        if live is not None and live.get("state") == "running":
            return ToolResult(
                True,
                content=_dump_content(
                    {"run_id": run_id, "name": live.get("name"), "state": "running", "run_dir": live.get("run_dir")}
                ),
            )
        if sf.exists():
            payload_status = orjson.loads(sf.read_bytes())
            # 磁盘是事实源：终态已落盘则注册表不再需要驻留，防 _ACTIVE_RUNS 无限增长
            with _RUNS_LOCK:
                _ACTIVE_RUNS.pop(run_id, None)
            return ToolResult(True, content=_dump_content(payload_status))
        return ToolResult(True, content=_dump_content({"run_id": run_id, "state": live.get("state")}))
    if action not in ("run", "resume"):
        return ToolResult(False, error=f"未知 action: {action}（run/resume/save/list/load/status）")

    manager = tool_ctx.get("sub_agent_manager")
    if not manager:
        return ToolResult(False, error="子智能体管理器未初始化")

    # ---- from_saved：读存档补齐 script/meta/args（显式传入的 args/script 优先）----
    from_saved = str(kwargs.get("from_saved") or "")
    if from_saved:
        try:
            wf_saved = load_workflow(wf_root(), from_saved)
        except KeyError as e:
            return ToolResult(False, error=str(e).strip("'\""))
        if not (isinstance(meta, dict) and meta):
            meta = wf_saved["meta"] or {"name": from_saved, "description": f"已存 workflow: {from_saved}"}
        if kwargs.get("args") is None:
            kwargs["args"] = wf_saved["args"]
        if not str(kwargs.get("script") or "").strip():
            kwargs["script"] = wf_saved["script"]

    # ---- resume：从原 run 目录复用 journal，已完成且指纹一致的 agent 直接回放 ----
    resume_map = None
    resume_dir = None
    if action == "resume":
        resume_run_id = str(kwargs.get("run_id") or "")
        resume_dir = wf_root() / "runs" / resume_run_id
        if not resume_run_id or not resume_dir.exists():
            return ToolResult(False, error=f"未找到 run: {resume_run_id}")
        resume_map = RunJournal(resume_dir).completed_map()
        meta_file = resume_dir / "meta.json"
        env = orjson.loads(meta_file.read_bytes()) if meta_file.exists() else {}
        if not (isinstance(meta, dict) and meta):
            meta = env.get("meta") or {"name": resume_run_id, "description": f"resume: {resume_run_id}"}
        if kwargs.get("args") is None:
            kwargs["args"] = env.get("args")
        if not str(kwargs.get("script") or "").strip():
            kwargs["script"] = (resume_dir / "script.py").read_text(encoding="utf-8")

    if (
        not isinstance(meta, dict)
        or not str(meta.get("name") or "").strip()
        or not str(meta.get("description") or "").strip()
    ):
        return ToolResult(False, error="meta 必须包含非空 name 与 description")

    script = kwargs.get("script", "")
    try:
        ast.parse(script)
    except SyntaxError as e:
        return ToolResult(False, error=f"脚本语法错误: {e}")

    # 配置三级链（env → 存储 → 默认）；number 字段读回为 str，须显式转换
    store = PluginConfigStore()
    max_concurrent = int(store.get(PLUGIN_NAME, "max_concurrent_agents") or 4)
    max_total = int(store.get(PLUGIN_NAME, "max_total_agents") or 50)
    max_items = int(store.get(PLUGIN_NAME, "max_items_per_call") or 100)
    max_duration = float(store.get(PLUGIN_NAME, "max_duration_sec") or 1800)
    max_agent_wait = float(store.get(PLUGIN_NAME, "max_agent_wait_sec") or 900)
    default_agent = str(store.get(PLUGIN_NAME, "default_agent") or "build")
    max_chars = int(store.get(PLUGIN_NAME, "max_result_chars") or 50000)
    model_aliases = _parse_aliases(store.get(PLUGIN_NAME, "model_aliases"))
    default_foreground = str(store.get(PLUGIN_NAME, "default_foreground") or "").lower() == "true"
    card_refresh_ms = int(float(store.get(PLUGIN_NAME, "card_refresh_ms") or 1000))
    max_runs_kept = int(float(store.get(PLUGIN_NAME, "max_runs_kept") or 30))

    # ---- 前台/后台分流：默认后台投递 daemon 线程立即返回，foreground=true 同步等结果 ----
    # 用户显式关闭同步执行（default_foreground=false）= 禁用前台：模型的显式 foreground=true
    # 视为违背用户配置，拒绝并引导走后台 + status 查询。未配置（None/空）仅是默认后台，不禁。
    raw_fg = store.get(PLUGIN_NAME, "default_foreground")
    explicitly_disabled = raw_fg is not None and str(raw_fg).strip() != "" and str(raw_fg).lower() != "true"
    default_foreground = str(raw_fg or "").lower() == "true"
    foreground = kwargs.get("foreground")
    if foreground is None:
        foreground = default_foreground
    elif foreground and explicitly_disabled:
        return ToolResult(
            False,
            error=(
                "前台同步已被用户配置禁用（default_foreground=false）。"
                "请省略 foreground 以后台模式发起任务，随后用 action=status, run_id=... 查询进度与结果；"
                "中断的任务用 action=resume 续跑。"
            ),
        )
    root = wf_root()
    session_id = tool_ctx.get("session_id", "")
    if resume_dir is not None:
        run_dir = resume_dir
        run_id = resume_dir.name
    else:
        run_dir = new_run_dir(root, meta, script, kwargs.get("args"))
        run_id = run_dir.name
    journal = RunJournal(run_dir)
    with _RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = {"state": "running", "run_dir": str(run_dir), "name": meta.get("name")}
    cfg = {
        "manager": manager,
        "session_id": session_id,
        "meta": meta,
        "script": script,
        "args": kwargs.get("args"),
        "max_concurrent": max_concurrent,
        "max_total": max_total,
        "max_items": max_items,
        "max_duration": max_duration,
        "max_agent_wait": max_agent_wait,
        "default_agent": default_agent,
        "max_chars": max_chars,
        "model_aliases": model_aliases,
        "run_dir": run_dir,
        "journal": journal,
        "run_id": run_id,
        "resume_map": resume_map,
        "max_runs_kept": max_runs_kept,
        "prune_root": root,
    }
    if foreground:
        return _execute_run(cfg)

    def _bg():
        _execute_run(cfg)

    threading.Thread(target=_bg, daemon=True, name=f"workflow-{run_id}").start()
    return ToolResult(
        True,
        content=_dump_content({
            "run_id": run_id,
            "status": "running",
            "run_dir": str(run_dir),
            "name": meta.get("name"),
            "hint": "后台运行中：完成后用 action=status, run_id=... 查询进度与结果",
        }),
    )


def _dump_content(content: dict) -> str:
    """结果序列化为 JSON 字符串，而不是回 dict。

    ToolResult.__str__ 走的是 str(content)：content 是 dict 时会输出 Python repr
    （单引号 / None / True 大小写），模型读着别扭，渲染闭包也没法可靠解析。
    统一出口为 JSON，模型侧和 UI 侧两头都干净。
    """
    try:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")
    except (TypeError, ValueError):
        return json.dumps(content, ensure_ascii=False, default=str)


def _salvage_truncated_json(raw: str):
    """宿主按 _MAX_OUTPUT_CHARS 截断后 JSON 不再合法：从尾部按逗号回退，救回可解析的前缀。

    救回来时打上 _truncated 标记，渲染时如实提示「只显示可解析部分」，
    总好过整块退化成原始 <pre>。
    """
    body = raw
    for _ in range(200):
        cut = body.rfind(",")
        if cut <= 1:
            return None
        body = body[:cut]
        try:
            data = orjson.loads(body + "}")
        except Exception:
            continue
        if isinstance(data, dict):
            data["_truncated"] = True
            return data
    return None


def _parse_workflow_payload(raw) -> dict:
    """把工具结果字符串还原成 dict（新格式 JSON；老消息的 Python repr 兜底）。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = orjson.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        salvaged = _salvage_truncated_json(raw)
        if salvaged is not None:
            return salvaged
    try:
        data = ast.literal_eval(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


# 正文里单段文本超过这个长度就折叠，避免一张卡顶掉整屏
_WF_TEXT_COLLAPSE_CHARS = 1200
_WF_LOG_COLLAPSE_CHARS = 600


def _wf_escape_pre(text: str) -> str:
    from app.widgets.render_helpers import escape

    return escape(text)


def _wf_pre(text: str, extra_style: str = "") -> str:
    from app.widgets.render_helpers import escape, scale_font_size

    return (
        f'<pre class="wf-pre" style="margin:0;padding:8px 10px;background:var(--panel-soft);'
        f'border:1px solid var(--border);border-radius:6px;color:var(--text);'
        f'font-size:{scale_font_size(12)}px;line-height:1.55;white-space:pre-wrap;'
        f'word-break:break-word;max-height:320px;overflow:auto;{extra_style}">{escape(text)}</pre>'
    )


def _wf_collapsible_text(text: str) -> str:
    """长文本：先给截断预览，再用原生 <details> 挂全文（不需要 JS）。"""
    from app.widgets.render_helpers import escape, scale_font_size

    if len(text) <= _WF_TEXT_COLLAPSE_CHARS:
        return _wf_pre(text)
    head = text[:_WF_TEXT_COLLAPSE_CHARS]
    rest_n = len(text) - _WF_TEXT_COLLAPSE_CHARS
    return (
        f'{_wf_pre(head + " …")}'
        f'<details class="wf-details" style="margin-top:6px;">'
        f'<summary style="cursor:pointer;color:var(--accent);font-size:{scale_font_size(11)}px;">'
        f'展开剩余 {rest_n} 字符</summary>'
        f'<div style="margin-top:6px;">{_wf_pre(text)}</div></details>'
    )


def _wf_render_value(value, depth: int = 0) -> str:
    """按类型渲染 result：str→等宽块 / list→条目列表 / dict→键值表 / None→提示。"""
    from app.widgets.render_helpers import escape, scale_font_size

    fs = scale_font_size(12)
    if value is None:
        return (
            f'<span style="color:var(--text-muted);font-style:italic;font-size:{fs}px;">'
            f'脚本没有给 result 赋值</span>'
        )
    if isinstance(value, str):
        return _wf_collapsible_text(value) if value.strip() else (
            f'<span style="color:var(--text-muted);font-style:italic;font-size:{fs}px;">空字符串</span>'
        )
    if isinstance(value, (int, float, bool)):
        return f'<span style="font-size:{fs}px;">{escape(str(value))}</span>'
    if isinstance(value, list):
        if not value:
            return f'<span style="color:var(--text-muted);font-style:italic;font-size:{fs}px;">空列表</span>'
        items = "".join(
            f'<li style="margin:0 0 8px 0;list-style:none;">'
            f'<span style="display:inline-block;min-width:20px;color:var(--text-muted);'
            f'font-size:{scale_font_size(11)}px;">{i + 1}.</span>'
            f'<span style="display:inline-block;width:calc(100% - 24px);vertical-align:top;">'
            f'{_wf_render_value(v, depth + 1)}</span></li>'
            for i, v in enumerate(value)
        )
        return f'<ul style="margin:0;padding:0;">{items}</ul>'
    if isinstance(value, dict):
        if not value:
            return f'<span style="color:var(--text-muted);font-style:italic;font-size:{fs}px;">空对象</span>'
        rows = "".join(
            f'<div style="display:flex;gap:10px;padding:4px 0;'
            f'border-top:1px solid var(--border);">'
            f'<span style="flex:0 0 auto;min-width:72px;max-width:140px;color:var(--text-secondary);'
            f'font-size:{scale_font_size(11)}px;word-break:break-word;">{escape(str(k))}</span>'
            f'<span style="flex:1 1 auto;min-width:0;">{_wf_render_value(v, depth + 1)}</span></div>'
            for k, v in value.items()
        )
        return f'<div style="display:flex;flex-direction:column;">{rows}</div>'
    return f'<span style="font-size:{fs}px;">{escape(str(value))}</span>'


def _render_workflow_body(result, tool_name, tool_args, success) -> str:
    """workflow 完成框渲染闭包：概览 + 阶段时间线 + 日志 + 结构化结果。

    不注册闭包的话，结果会落到 render_helpers 的通用兜底 —— 一大坨 JSON/Python repr
    原样塞进 <pre>，既看不出阶段进展，也读不出子智能体各自返回了什么。
    """
    from app.widgets.render_helpers import escape, scale_font_size

    raw = getattr(result, "content", "") or ""
    data = _parse_workflow_payload(raw)
    if not data:
        # 解析不出来（旧消息 / 异常）也别丢内容，退化成纯文本块
        return _wf_pre(str(raw))
    # 运行状态卡（action=status 的产出）：识别后走专用渲染，不走工作流结果卡
    if "state" in data and "agents" in data:
        return _render_run_card(data)

    fs = scale_font_size(12)
    fs_small = scale_font_size(11)

    name = str(data.get("workflow") or "workflow")
    agents = data.get("agents_started")
    phases = _norm_phases(data.get("phases"))
    logs = [str(x) for x in (data.get("logs") or [])]
    note = str(data.get("_note") or "")
    if data.get("_truncated"):
        note = (note + "；结果超出宿主渲染上限，仅显示可解析部分").strip("；")
    payload = data.get("result")

    # ── 概览指标 ──
    metrics = []
    if isinstance(agents, int):
        metrics.append((str(agents), "子智能体"))
    if phases:
        metrics.append((str(len(phases)), "阶段"))
    if logs:
        metrics.append((str(len(logs)), "条日志"))
    metrics_html = ""
    if metrics:
        chips = "".join(
            f'<span style="display:inline-flex;align-items:baseline;gap:4px;padding:2px 9px;'
            f'margin:0 6px 0 0;border:1px solid var(--border);border-radius:999px;'
            f'background:var(--panel-soft);font-size:{fs_small}px;">'
            f'<b style="font-size:{fs}px;font-weight:600;">{escape(v)}</b>'
            f'<span style="color:var(--text-secondary);">{escape(k)}</span></span>'
            for v, k in metrics
        )
        metrics_html = f'<div style="display:flex;flex-wrap:wrap;align-items:center;">{chips}</div>'

    # ── 阶段时间线 ──
    phases_html = ""
    if phases:
        nodes = ""
        for title, detail in phases:
            detail_html = (
                f'<div style="color:var(--text-secondary);font-size:{fs_small}px;margin-top:1px;">'
                f'{escape(detail)}</div>'
                if detail
                else ""
            )
            nodes += (
                f'<div style="display:flex;align-items:flex-start;gap:8px;padding:3px 0;">'
                f'<span style="flex:0 0 auto;width:6px;height:6px;margin-top:6px;border-radius:50%;'
                f'background:var(--accent);"></span>'
                f'<span style="flex:1 1 auto;min-width:0;color:var(--text);font-size:{fs}px;'
                f'word-break:break-word;">{escape(title)}{detail_html}</span></div>'
            )
        phases_html = (
            f'<div style="margin-top:10px;">'
            f'<div style="color:var(--text-secondary);font-size:{fs_small}px;margin-bottom:2px;">阶段</div>'
            f'<div style="padding-left:2px;border-left:1px solid var(--border);margin-left:2px;">'
            f'<div style="padding-left:8px;">{nodes}</div></div></div>'
        )

    # ── 日志（默认收起）──
    logs_html = ""
    if logs:
        log_text = "\n".join(f"· {x}" for x in logs)
        inner = _wf_pre(log_text) if len(log_text) <= _WF_LOG_COLLAPSE_CHARS else (
            _wf_pre(log_text[:_WF_LOG_COLLAPSE_CHARS] + " …")
            + f'<details class="wf-details" style="margin-top:6px;">'
            f'<summary style="cursor:pointer;color:var(--accent);font-size:{fs_small}px;">展开全部 {len(logs)} 条</summary>'
            f'<div style="margin-top:6px;">{_wf_pre(log_text)}</div></details>'
        )
        logs_html = (
            f'<details class="wf-details" style="margin-top:10px;">'
            f'<summary style="cursor:pointer;color:var(--text-secondary);font-size:{fs_small}px;">'
            f'执行日志（{len(logs)} 条）</summary>'
            f'<div style="margin-top:6px;">{inner}</div></details>'
        )

    # ── 结果区 ──
    result_html = (
        f'<div style="margin-top:10px;">'
        f'<div style="color:var(--text-secondary);font-size:{fs_small}px;margin-bottom:4px;">result</div>'
        f'<div>{_wf_render_value(payload)}</div></div>'
    )

    note_html = ""
    if note:
        note_html = (
            f'<div style="margin-top:8px;color:var(--text-muted);font-size:{fs_small}px;'
            f'font-style:italic;">{escape(note)}</div>'
        )

    return (
        f'<div class="wf-block" style="padding:2px 0;">'
        f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">'
        f'<span style="font-size:{fs}px;font-weight:600;color:var(--text);">{escape(name)}</span>'
        f'<span style="font-size:{fs_small}px;color:var(--text-muted);">工作流</span>'
        f"</div>"
        f"{metrics_html}{phases_html}{result_html}{logs_html}{note_html}</div>"
    )


def _preview_workflow(tool_args: dict) -> str:
    meta = tool_args.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = orjson.loads(meta)
        except Exception:
            meta = {}
    name = str(meta.get("name") or "").strip() or "workflow"
    # 阶段数直接显示在折叠头上：不用展开就知道这个工作流跑了几步
    declared = meta.get("phases")
    if isinstance(declared, list) and declared:
        return f"workflow: {name} · {len(declared)} 个阶段"
    return f"workflow: {name}"


def register(registry):
    registry.register(
        "workflow",
        _WORKFLOW_SCHEMA,
        impl=_workflow_impl,
        danger="dangerous",
        icon="workflow",
        cn_name="工作流编排",
        group=GROUP_SUBAGENT,
        description="受限 Python 脚本编排子智能体",
        aliases=["workflow-orchestrate", "Wf"],
        preview=_preview_workflow,
        render=_render_workflow_body,
        summarize=make_summarize_from_preview(_preview_workflow),
        keep_in_content=True,
    )
