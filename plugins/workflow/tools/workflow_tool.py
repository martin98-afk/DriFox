# -*- coding: utf-8 -*-
"""workflow 工具 — 受限 Python 脚本编排子智能体。

脚本在受限命名空间内 exec（containment，非安全边界），经钩子扇出子智能体；
agent() 走 SubAgentManager.execute_task + Event 同步等待，子任务自动进任务体系。
"""
from __future__ import annotations

import ast
import builtins
import datetime
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


class _RunState:
    """单次 run 的额度与时长状态（线程安全）。"""

    def __init__(self, max_total_agents: int, deadline: float):
        self._lock = threading.Lock()
        self._max_total = max_total_agents
        self._deadline = deadline
        self.started = 0

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


def _make_agent_hook(manager, session_id: str, state: _RunState, default_agent: str):
    """agent(prompt, agent=None, label=None, phase=None, share_context=False) -> str | None

    execute_task 异步派发 SubAgentExecutor；Event 在 executor 线程被回调置位。
    子任务失败（on_error / execute_task 返回 False）降级为 None，由脚本兜底。
    """

    def agent(prompt, agent=None, label=None, phase=None, share_context=False):
        # 参数名 agent 遮蔽外层函数名：本函数体内不再引用自身，合法且对模型最自然
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError("agent() 的 prompt 必须是非空字符串")
        state.reserve()
        name = agent or default_agent
        task_id = str(uuid.uuid4())
        done = threading.Event()
        box = {"result": None}

        def _on_finished(text):
            box["result"] = text
            done.set()

        def _on_error(err):
            logger.warning(f"[workflow] 子任务失败 ({label or name}): {err}")
            done.set()

        ok = manager.execute_task(
            task_id=task_id,
            agent_name=name,
            task_description=prompt,
            on_finished=_on_finished,
            on_error=_on_error,
            share_context=bool(share_context),
            session_id=session_id,
        )
        if not ok:
            return None
        done.wait()
        return box["result"]

    return agent


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
        "- agent(prompt, agent=角色, phase=分组): 跑一个子智能体到完成，返回最终文本，失败返回 None\n"
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
                "agent": _make_agent_hook(manager, session_id, state, default_agent),
                "parallel": parallel_hook,
                "pipeline": pipeline_hook,
                "phase": lambda title: phases.append(str(title)),
                "log": lambda msg: logs.append(str(msg)),
            },
        )
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
        if len(orjson.dumps(content).decode("utf-8")) > max_chars:
            content["logs"] = content["logs"][:10]
            result_note = (result_note + " 结果超长，logs 截断").strip()
        if result_note:
            content["_note"] = result_note
        return ToolResult(True, content=content)
    except WorkflowError as e:
        return ToolResult(False, error=f"workflow 执行中止: {e}")
    except Exception as e:
        return ToolResult(False, error=f"workflow 脚本异常: {type(e).__name__}: {e}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _preview_workflow(tool_args: dict) -> str:
    meta = tool_args.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = orjson.loads(meta)
        except Exception:
            meta = {}
    name = str(meta.get("name") or "").strip() or "workflow"
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
        summarize=make_summarize_from_preview(_preview_workflow),
        keep_in_content=True,
    )
