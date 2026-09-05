# -*- coding: utf-8 -*-
"""workflow 工具 — 受限 Python 脚本编排子智能体。

脚本在受限命名空间内 exec（containment，非安全边界），经钩子扇出子智能体；
agent() 走 SubAgentManager.execute_task + Event 同步等待，子任务自动进任务体系。
"""
from __future__ import annotations

import ast
import builtins
import datetime
import inspect
import json
import math
import re
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

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


class WorkflowError(Exception):
    """钩子误用 / 上限触发（杀全脚本，模型可修正后重发）"""


class WorkflowTimeoutError(WorkflowError):
    """run 总时长超限"""


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
_PRESET_MODULES: dict = {
    "json": json,
    "math": math,
    "re": re,
    "statistics": statistics,
    "datetime": datetime,
}


def _build_sandbox(args, hooks: dict | None = None) -> dict:
    """构建受限命名空间：白名单 builtins + 预置只读模块 + 钩子 + args。"""
    ns = {"__builtins__": dict(_ALLOWED_BUILTINS)}
    ns.update(_PRESET_MODULES)
    if hooks:
        ns.update(hooks)
    ns["args"] = args
    return ns


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
    manager, session_id: str, state: _RunState, default_agent: str, max_agent_wait: float = 900.0
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

    def agent(prompt, agent=None, label=None, phase=None, share_context=False):
        # 参数名 agent 遮蔽外层函数名：本函数体内不再引用自身，合法且对模型最自然
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError("agent() 的 prompt 必须是非空字符串")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            raise WorkflowError("agent() 的 agent 必须是非空字符串")
        if state.aborted():
            return None
        state.reserve()
        name = agent or default_agent
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
            "task_description": prompt,
            "on_finished": _on_finished,
            "on_error": _on_error,
            "share_context": bool(share_context),
            "session_id": session_id,
        }
        if supports_conn_type:
            call["connection_type"] = direct
        if supports_executor_ref:
            call["executor_ref"] = ref

        ok = manager.execute_task(**call)
        if not ok:
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
            return None
        return box["result"]

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
    """workflow 工具的动态描述：用法契约 + 可用角色列表（get_builtin_tools_schema 调用）。"""
    base = (
        "运行受限 Python 编排脚本，扇出子智能体。适合大规模多智能体编排（审计/迁移/多角度研究/对抗验证）；"
        "一两个委派用 subagent_para，固定依赖图用 subagent_dag。\n\n"
        "脚本是同步 Python，顶层直接执行，最终结果赋给 result 变量（未赋则为 null）。钩子：\n"
        "- agent(prompt, agent=角色, phase=分组): 跑一个子智能体到完成，返回最终文本，失败/超时返回 None\n"
        "- parallel([零参函数]): 并发执行并等全部（屏障）；异常项降 None；不支持嵌套\n"
        "- pipeline(items, *stages): 每项独立流过 stage(prev, item, index)，无屏障；阶段异常该项降 None 跳后续\n"
        "- phase(title)/log(msg): 进度记录；预置 json/math/re/statistics/datetime；无文件/网络能力\n"
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
            },
            "required": ["script", "meta"],
        },
    },
}


def _workflow_impl(tool_ctx, **kwargs):
    manager = tool_ctx.get("sub_agent_manager")
    if not manager:
        return ToolResult(False, error="子智能体管理器未初始化")

    meta = kwargs.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = orjson.loads(meta)
        except orjson.JSONDecodeError:
            return ToolResult(False, error="meta 不是合法 JSON")
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

    state = _RunState(max_total, time.monotonic() + max_duration)
    pool = ThreadPoolExecutor(
        max_workers=max(1, max_concurrent),
        initializer=_pool_initializer,
        thread_name_prefix="wf-agent",
    )
    session_id = tool_ctx.get("session_id", "")
    phases: list = []
    logs: list = []

    try:
        parallel_hook, pipeline_hook = _make_combinators(state, pool, max_items)
        ns = _build_sandbox(
            args=kwargs.get("args"),
            hooks={
                "agent": _make_agent_hook(manager, session_id, state, default_agent, max_agent_wait),
                "parallel": parallel_hook,
                "pipeline": pipeline_hook,
                "phase": lambda title: phases.append(str(title)),
                "log": lambda msg: logs.append(str(msg)),
            },
        )
        # 预检：宿主内部名引用在 exec 前拦截，避免 agent 扇出后才 NameError 白跑
        precheck_err = _check_underscore_names(script, ns)
        if precheck_err:
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
        if len(payload) > max_chars and content["logs"]:
            content["logs"] = content["logs"][:10]
            content["_note"] = (result_note + " 结果超长，logs 截断").strip()
            payload = _dump_content(content)
        # _HOST_RENDER_CAP：宿主渲染层对结果文本的硬上限（render_helpers._MAX_OUTPUT_CHARS）。
        # 超了会被截断、渲染闭包就解析不出 JSON；日志在 UI 里本来是收起的，再让一步。
        if len(payload) > _HOST_RENDER_CAP and content["logs"]:
            content["logs"] = content["logs"][:5]
            payload = _dump_content(content)
        return ToolResult(True, content=payload)
    except WorkflowError as e:
        return ToolResult(False, error=f"workflow 执行中止: {e}")
    except NameError as e:
        # 预检只拦 _ 开头的名字；其他未定义名（钩子拼错等）走到这里，附预置名清单让模型自愈
        return ToolResult(
            False,
            error=(
                f"workflow 脚本异常: NameError: {e}。"
                "沙箱仅预置 agent/parallel/pipeline/phase/log、json/math/re/statistics/datetime、args；"
                "宿主内部名不在脚本命名空间"
            ),
        )
    except Exception as e:
        return ToolResult(False, error=f"workflow 脚本异常: {type(e).__name__}: {e}")
    finally:
        # 收尾：先置中止标志唤醒所有仍在等子任务的 worker（含线程池里的），再关池。
        # 否则异常路径下这些线程会一直挂到各自的截止时间才退出。
        state.abort()
        pool.shutdown(wait=False, cancel_futures=True)


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

    fs = scale_font_size(12)
    fs_small = scale_font_size(11)

    name = str(data.get("workflow") or "workflow")
    agents = data.get("agents_started")
    phases = [str(p) for p in (data.get("phases") or [])]
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
        nodes = "".join(
            f'<div style="display:flex;align-items:flex-start;gap:8px;padding:3px 0;">'
            f'<span style="flex:0 0 auto;width:6px;height:6px;margin-top:6px;border-radius:50%;'
            f'background:var(--accent);"></span>'
            f'<span style="flex:1 1 auto;min-width:0;color:var(--text);font-size:{fs}px;'
            f'word-break:break-word;">{escape(p)}</span></div>'
            for p in phases
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
