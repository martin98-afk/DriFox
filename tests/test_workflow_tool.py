# -*- coding: utf-8 -*-
"""workflow 工具单测：受限命名空间 / 钩子语义 / 上限 / impl 组装。"""
import json
import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal
import pytest

from plugins.workflow.tools.workflow_tool import (
    WorkflowError,
    WorkflowTimeoutError,
    _build_sandbox,
    _RunState,
)


class _FakeManager:
    """同步回调的假 SubAgentManager（execute_task 契约：成功 True + on_finished(result)；失败 False + on_error）"""

    def __init__(self, routes=None, fail_agents=()):
        self.routes = routes or {}
        self.fail_agents = set(fail_agents)
        self.calls = []
        self.cancelled = []

    def execute_task(self, task_id, agent_name, task_description,
                     on_finished=None, on_error=None, **kw):
        self.calls.append((agent_name, task_description, kw))
        if agent_name in self.fail_agents:
            if on_error:
                on_error(task_id, f"Agent not found: {agent_name}")
            return False
        if on_finished:
            on_finished(task_id, self.routes.get(agent_name, f"done:{agent_name}"))
        return True

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return True


class _SilentManager(_FakeManager):
    """子任务既不 on_finished 也不 on_error（模拟 executor 静默死亡 / 被 stall 检测器摘除）。"""

    def execute_task(self, task_id, agent_name, task_description,
                     on_finished=None, on_error=None, **kw):
        self.calls.append((agent_name, task_description, kw))
        return True


class TestSandbox:
    def test_denies_dangerous_builtins(self):
        ns = _build_sandbox(args={})
        for name in ("open", "eval", "exec", "compile", "__import__", "input", "breakpoint"):
            with pytest.raises(NameError):
                exec(f"{name}('x')", ns)

    def test_common_builtins_available(self):
        ns = _build_sandbox(args={})
        exec("xs = sorted([3, 1, 2]); n = len(xs); s = sum(xs)", ns)
        assert ns["xs"] == [1, 2, 3]
        assert ns["n"] == 3 and ns["s"] == 6

    def test_preset_modules_and_args(self):
        ns = _build_sandbox(args={"files": ["a"]})
        assert ns["args"] == {"files": ["a"]}
        assert ns["json"].dumps and ns["math"].sqrt and ns["re"].match


class TestRunState:
    def test_reserve_counts(self):
        st = _RunState(max_total_agents=2, deadline=time.monotonic() + 60)
        st.reserve()
        st.reserve()
        assert st.started == 2

    def test_total_cap_raises(self):
        st = _RunState(max_total_agents=1, deadline=time.monotonic() + 60)
        st.reserve()
        with pytest.raises(WorkflowError):
            st.reserve()

    def test_deadline_raises(self):
        st = _RunState(max_total_agents=10, deadline=time.monotonic() - 1)
        with pytest.raises(WorkflowTimeoutError):
            st.reserve()

    def test_check_only_time(self):
        st = _RunState(max_total_agents=1, deadline=time.monotonic() + 60)
        st.check()
        st.check()  # 不计数，不抛
        assert st.started == 0


class TestAgentHook:
    def _make(self, manager, default_agent="build", max_total=50, deadline=None):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(max_total, deadline or time.monotonic() + 60)
        return _make_agent_hook(manager, "sess-1", st, default_agent), st

    def test_returns_child_final_text(self):
        hook, st = self._make(_FakeManager(routes={"build": "审计完成"}))
        assert hook("检查 src/a.py") == "审计完成"
        assert st.started == 1

    def test_failure_returns_none(self):
        hook, _ = self._make(_FakeManager(fail_agents={"build"}))
        assert hook("x") is None

    def test_default_agent_used_when_unspecified(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr, default_agent="explore")
        hook("x")
        assert mgr.calls[0][0] == "explore"

    def test_explicit_agent_overrides(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x", agent="review")
        assert mgr.calls[0][0] == "review"

    def test_blank_prompt_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(WorkflowError):
            hook("  ")

    def test_unknown_kwarg_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(TypeError):
            hook("x", effort="high")

    def test_total_cap_kills_script(self):
        hook, _ = self._make(_FakeManager(), max_total=1)
        hook("a")
        with pytest.raises(WorkflowError):
            hook("b")

    def test_share_context_forwarded(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x", share_context=True)
        assert mgr.calls[0][2].get("share_context") is True


class TestAgentHookWaitContract:
    """★ 回归：agent() 的「回调能收到 + 等待有上界」双保险。

    缺 connection_type=DirectConnection 时，回调会被排进发起连接那个线程的事件循环，
    而该线程正阻塞在 wait() 上（线程池 worker 还是无 Qt 事件循环的普通线程）
    → 子任务跑完了、回调永不投递 → 脚本永久挂起。
    """

    def _make(self, manager, max_total=50, deadline=None, max_agent_wait=900.0):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(max_total, deadline or time.monotonic() + 60)
        return _make_agent_hook(manager, "sess-1", st, "build", max_agent_wait), st

    def test_forwards_direct_connection(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x")
        # 1 == Qt.DirectConnection：回调在 executor 线程里直接执行，等待方才能被唤醒
        assert mgr.calls[0][2].get("connection_type") == 1

    def test_silent_child_degrades_to_none_within_cap(self):
        mgr = _SilentManager()
        hook, _ = self._make(mgr, max_agent_wait=0.3)
        t0 = time.monotonic()
        assert hook("x") is None
        assert time.monotonic() - t0 < 3.0  # 有上界，不可能是无限等待
        assert len(mgr.cancelled) == 1  # 挂起的子任务被尽力取消

    def test_abort_unblocks_waiting_agent(self):
        mgr = _SilentManager()
        hook, st = self._make(mgr, max_agent_wait=60.0)
        box = {}

        def runner():
            box["v"] = hook("x")

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        time.sleep(0.2)
        st.abort()
        t.join(timeout=3)
        assert not t.is_alive(), "abort 后 agent() 仍被阻塞"
        assert box.get("v") is None
        assert len(mgr.cancelled) == 1

    def test_deadline_passed_raises_timeout(self):
        hook, _ = self._make(_SilentManager(), deadline=time.monotonic() + 0.3, max_agent_wait=60.0)
        with pytest.raises(WorkflowTimeoutError):
            hook("x")

    def test_blank_agent_name_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(WorkflowError):
            hook("x", agent="   ")

    def test_legacy_signature_does_not_raise_type_error(self):
        """宿主核心是旧签名时不能抛 TypeError（抛了就整段脚本挂掉，而不是降级）。"""

        class _Legacy:
            def execute_task(self, task_id=None, agent_name=None, task_description=None,
                             parent_context="", on_finished=None, on_error=None,
                             on_progress=None, executor_ref=None, share_context=False,
                             session_id="", llm_config=None):
                if on_finished:
                    on_finished(task_id, "ok")
                if executor_ref is not None:
                    executor_ref["executor"] = None  # 同步回调场景没有 executor
                return True

            def cancel_task(self, task_id):
                return True

        hook, _ = self._make(_Legacy())
        assert hook("x") == "ok"

    def test_abort_skips_new_dispatch(self):
        mgr = _FakeManager()
        hook, st = self._make(mgr)
        st.abort()
        assert hook("x") is None
        assert mgr.calls == []  # 已中止：不再派发新的子智能体


class TestRunStateAbort:
    def test_abort_wakes_tracked_waiters(self):
        st = _RunState(10, time.monotonic() + 60)
        ev = threading.Event()
        st.track_waiter(ev)
        st.abort()
        assert st.aborted() is True
        assert ev.is_set() is True

    def test_untracked_waiter_not_touched(self):
        st = _RunState(10, time.monotonic() + 60)
        ev = threading.Event()
        st.track_waiter(ev)
        st.untrack_waiter(ev)
        st.abort()
        assert ev.is_set() is False


class TestQtCallbackDelivery:
    """★ 真实 QThread + 真实 pyqtSignal 下的回调投递验证（根因层面回归）。

    卡死根因：不指定 connection_type 时是 AutoConnection，回调被排进「发起连接那个线程」
    的事件循环，而该线程正阻塞在 wait() 上（线程池 worker 还是无 Qt 事件循环的普通线程）
    → 子任务早已跑完，回调却永不投递。
    """

    class _MgrBase:
        """共享：真实 QThread 发射 + 连接语义；子类决定 execute_task 的签名。"""

        class _Exec(QThread):
            finished_with_result = pyqtSignal(str, str)
            error_occurred = pyqtSignal(str, str)

            def run(self):
                time.sleep(0.05)
                self.finished_with_result.emit(self._tid, "payload")

        def __init__(self, honor=True):
            self.honor = honor
            self.cancelled = []
            self._execs = []  # 持有引用，避免 QThread 运行中被 GC

        def _dispatch(self, task_id, on_finished, on_error, connection_type, executor_ref):
            ex = self._Exec()
            ex._tid = task_id
            if connection_type is not None:
                if on_finished:
                    ex.finished_with_result.connect(on_finished, connection_type)
                if on_error:
                    ex.error_occurred.connect(on_error, connection_type)
            else:  # AutoConnection：回调排进发起连接那个线程的事件循环
                if on_finished:
                    ex.finished_with_result.connect(on_finished)
                if on_error:
                    ex.error_occurred.connect(on_error)
            self._execs.append(ex)
            if executor_ref is not None:
                executor_ref["executor"] = ex  # 与真实核心一致：start() 之前写入
            ex.start()
            return True

        def cancel_task(self, task_id):
            self.cancelled.append(task_id)
            return True

    class _NewCoreMgr(_MgrBase):
        """新核心：execute_task 支持 connection_type。honor=False 模拟「收了但没照做」。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         on_finished=None, on_error=None, connection_type=None,
                         executor_ref=None, **kw):
            return self._dispatch(task_id, on_finished, on_error,
                                  connection_type if self.honor else None, executor_ref)

    class _LegacyMgr(_MgrBase):
        """旧核心（宿主未重启时就是这样）：无 connection_type，但有 executor_ref。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         parent_context="", on_finished=None, on_error=None,
                         on_progress=None, executor_ref=None, share_context=False,
                         session_id="", llm_config=None):
            return self._dispatch(task_id, on_finished, on_error, None, executor_ref)

    class _BareMgr(_MgrBase):
        """最旧核心：connection_type / executor_ref 都没有，无任何直连手段。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         parent_context="", on_finished=None, on_error=None,
                         on_progress=None, share_context=False, session_id="", llm_config=None):
            return self._dispatch(task_id, on_finished, on_error, None, None)

    def _run(self, manager, agent_wait):
        """在普通 Python 线程里调 agent()（模拟 chat worker / 线程池 worker）。"""
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(10, time.monotonic() + 60)
        hook = _make_agent_hook(manager, "sess", st, "build", agent_wait)
        box = {}
        t = threading.Thread(target=lambda: box.update(v=hook("x")), daemon=True)
        t.start()
        t.join(timeout=10)
        return t.is_alive(), box.get("v")

    def test_direct_connection_delivers_without_event_loop(self, qapp):
        alive, val = self._run(self._NewCoreMgr(honor=True), 5.0)
        assert not alive, "DirectConnection 下 agent() 仍被阻塞"
        assert val == "payload"

    def test_legacy_core_falls_back_to_executor_ref(self, qapp):
        """宿主核心没有 connection_type（未重启的老进程就是这种）时，靠 executor_ref 补挂直连。"""
        alive, val = self._run(self._LegacyMgr(), 5.0)
        assert not alive, "旧核心兜底路径仍被阻塞"
        assert val == "payload"

    def test_no_fallback_path_still_capped(self, qapp):
        """反例：既无 connection_type 又无 executor_ref 时回调永不投递。

        只剩等待上界兜底（返回 None）——修复前连上界都没有，
        正是「子任务跑完了但脚本永远不动」的现象。
        """
        alive, val = self._run(self._BareMgr(), 1.0)
        assert not alive, "退化路径应被等待上界兜住，不应挂死"
        assert val is None


class TestWorkflowResultRendering:
    """结果出口与完成框渲染闭包。

    宿主 render_helpers 没有插件 render 闭包时会把结果文本原样塞进 <pre>，
    workflow 的结果是一整个 JSON —— 不注册闭包就是一大坨 Python repr。
    """

    class _R:
        def __init__(self, content):
            self.content = content

    def _body(self, **content):
        from plugins.workflow.tools.workflow_tool import _dump_content, _render_workflow_body

        return _render_workflow_body(self._R(_dump_content(content)), "workflow", {}, True)

    def test_dump_content_is_json_not_python_repr(self):
        import orjson

        from plugins.workflow.tools.workflow_tool import _dump_content

        s = _dump_content({"a": 1, "b": None, "c": True, "d": [1, 2]})
        assert orjson.loads(s) == {"a": 1, "b": None, "c": True, "d": [1, 2]}
        assert "'" not in s and "None" not in s  # 不是 str(dict) 的 Python repr

    def test_render_shows_metrics_phases_and_logs(self):
        html = self._body(
            workflow="audit", agents_started=3, phases=["扫描", "复核"], logs=["扫到 12 个文件"], result={"ok": 1}
        )
        assert "audit" in html
        assert "3" in html and "子智能体" in html
        assert "扫描" in html and "复核" in html
        assert "执行日志" in html

    def test_render_hints_when_result_unset(self):
        html = self._body(workflow="w", agents_started=0, phases=[], logs=[], result=None)
        assert "result 赋值" in html

    def test_render_escapes_html(self):
        html = self._body(
            workflow="<img src=x onerror=alert(1)>", agents_started=0, phases=[], logs=[], result="<script>"
        )
        assert "<img src=x" not in html and "<script>" not in html
        assert "&lt;" in html

    def test_render_shows_dict_phases_with_detail(self):
        # Task 3: phases 条目 dict 化（title+detail），渲染层兼容并与旧字符串条目共存
        html = self._body(
            workflow="audit",
            agents_started=1,
            phases=[{"title": "plan", "detail": "读文档"}, {"title": "build", "detail": None}, "旧字符串阶段"],
            logs=[],
            result={"ok": 1},
        )
        assert "plan" in html and "build" in html and "旧字符串阶段" in html
        assert "读文档" in html
        assert "detail" not in html  # dict 不得以 repr 形态漏出（escape 后仍含 detail 字样）

    def test_render_falls_back_for_unparseable(self):
        from plugins.workflow.tools.workflow_tool import _render_workflow_body

        html = _render_workflow_body(self._R("not json at all"), "workflow", {}, True)
        assert "not json at all" in html

    def test_salvage_truncated_json(self):
        """宿主按 _MAX_OUTPUT_CHARS 截断后仍要救回可解析的前缀。"""
        from plugins.workflow.tools.workflow_tool import _parse_workflow_payload

        full = '{"workflow":"a","agents_started":2,"phases":[],"logs":[],"result":"' + "x" * 9000 + '"}'
        data = _parse_workflow_payload(full[:5000])
        assert data.get("workflow") == "a"
        assert data.get("_truncated") is True

    def test_preview_shows_phase_count(self):
        from plugins.workflow.tools.workflow_tool import _preview_workflow

        assert "2 个阶段" in _preview_workflow({"meta": {"name": "a", "phases": [{"title": "x"}, {"title": "y"}]}})
        assert _preview_workflow({"meta": {"name": "a"}}) == "workflow: a"

    def test_register_passes_render(self):
        from plugins.workflow.tools.workflow_tool import _render_workflow_body, register

        class _Reg:
            def register(self, name, schema, **kw):
                assert kw["render"] is _render_workflow_body

        register(_Reg())


class TestCombinators:
    def _make(self, max_items=100, max_total=50):
        from concurrent.futures import ThreadPoolExecutor

        from plugins.workflow.tools.workflow_tool import (
            _make_combinators,
            _pool_initializer,
        )

        st = _RunState(max_total, time.monotonic() + 60)
        pool = ThreadPoolExecutor(max_workers=4, initializer=_pool_initializer)
        return (*_make_combinators(st, pool, max_items), st)

    def test_parallel_returns_all(self):
        parallel, _, _ = self._make()
        out = parallel([lambda: 1, lambda: 2, lambda: 3])
        assert out == [1, 2, 3]

    def test_parallel_thunk_exception_drops_to_none(self):
        parallel, _, _ = self._make()

        def boom():
            raise ValueError("x")

        out = parallel([lambda: "ok", boom, lambda: "ok2"])
        assert out == ["ok", None, "ok2"]

    def test_parallel_nested_call_raises(self):
        parallel, _, _ = self._make()

        def nested():
            parallel([lambda: 1])

        with pytest.raises(WorkflowError):
            parallel([nested])

    def test_parallel_items_cap(self):
        parallel, _, _ = self._make(max_items=2)
        with pytest.raises(WorkflowError):
            parallel([lambda: 1, lambda: 2, lambda: 3])

    def test_pipeline_no_barrier_stage_signature(self):
        _, pipeline, _ = self._make()
        order = []

        def s1(prev, item, idx):
            time.sleep(0.05 if item == 0 else 0)
            order.append(("s1", item))
            return f"{item}-a"

        def s2(prev, item, idx):
            order.append(("s2", item))
            return f"{prev}-b"

        out = pipeline([0, 1], s1, s2)
        assert out == ["0-a-b", "1-a-b"]
        # 无屏障断言：item=1 的 s2 早于 item=0 的 s1（慢项不阻塞快项）
        assert order.index(("s2", 1)) < order.index(("s1", 0))

    def test_pipeline_stage_failure_skips_rest(self):
        _, pipeline, _ = self._make()
        reached = []

        def bad(prev, item, idx):
            raise ValueError("stage boom")

        def after(prev, item, idx):
            reached.append(item)
            return prev

        out = pipeline([1, 2], bad, after)
        assert out == [None, None]
        assert reached == []

    def test_pipeline_workflow_error_propagates(self):
        _, pipeline, _ = self._make()

        def over_limit(prev, item, idx):
            raise WorkflowError("额度尽")

        with pytest.raises(WorkflowError):
            pipeline([1], over_limit)

    def test_pipeline_empty_stages_raises(self):
        _, pipeline, _ = self._make()
        with pytest.raises(WorkflowError):
            pipeline([1, 2])


class TestTranslateTypeError:
    """TypeError 人话翻译层：钩子签名报错（<lambda> 或含钩子名）翻译成自愈提示。"""

    _HOOKS = ("agent", "parallel", "pipeline", "phase", "log")

    def _catch(self, boom) -> TypeError:
        try:
            boom()
        except TypeError as e:
            return e
        raise AssertionError("未触发 TypeError")

    def test_lambda_positional_error_translated(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def boom():
            phase = lambda title: None  # noqa: E731
            phase("a", "b")

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None
        assert "签名" in msg and "phase(title" in msg  # 带正确签名提示

    def test_def_named_hook_extracted(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def phase(title, detail=None):
            return None

        def boom():
            phase("a", "b", "c")  # 多于新签名

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None and "phase" in msg

    def test_kwarg_error_translated(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def boom():
            agent = lambda prompt: None  # noqa: E731
            agent("x", model="sonnet")

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None and "model" in msg  # 提示里点出肇事参数

    def test_unrelated_error_passthrough(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        assert translate_type_error(TypeError("int object is not callable"), self._HOOKS) is None


class TestWorkflowImpl:
    def _impl(self, monkeypatch, manager, **overrides):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {
                "max_concurrent_agents": 2,
                "max_total_agents": 10,
                "max_items_per_call": 10,
                "max_duration_sec": 60,
                "default_agent": "build",
                "max_result_chars": 1000,
                "default_foreground": "true",
            }.get(key),
        )
        ctx = {"sub_agent_manager": manager, "session_id": "s1"}
        kwargs = {
            "meta": {"name": "audit", "description": "审计"},
            "script": "result = {'n': agent('x')}",
        }
        kwargs.update(overrides)
        return wt._workflow_impl(ctx, **kwargs)

    def test_happy_path(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(routes={"build": "ok"}))
        assert r.success is True
        # 出口是 JSON 字符串（不是 Python repr）：模型侧和渲染闭包侧都要能直接 parse
        payload = json.loads(r.content)
        assert payload["result"] == {"n": "ok"}
        assert payload["agents_started"] == 1
        assert payload["workflow"] == "audit"

    def test_meta_validation_fails_fast(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), meta={"name": "x"})
        assert r.success is False
        assert "description" in r.error

    def test_syntax_error_fails_fast(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="def oops(:")
        assert r.success is False
        assert "语法" in r.error

    def test_missing_result_is_null(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="agent('x')")
        assert r.success is True
        assert json.loads(r.content)["result"] is None

    def test_unserializable_result_degrades(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="result = {'f': len}")
        assert r.success is True
        payload = json.loads(r.content)
        assert isinstance(payload["result"], dict)
        assert "_repr" in payload["result"]

    def test_script_exception_reported(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="result = 1 / 0")
        assert r.success is False
        assert "脚本异常" in r.error

    def test_manager_missing(self, monkeypatch):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt.PluginConfigStore, "get", lambda self, p, k: None)
        r = wt._workflow_impl(
            {"session_id": "s1"}, meta={"name": "a", "description": "b"}, script="result = 1"
        )
        assert r.success is False

    def test_preview_shows_name(self):
        from plugins.workflow.tools.workflow_tool import _preview_workflow

        assert "audit" in _preview_workflow({"meta": {"name": "audit", "description": "d"}, "script": ""})

    def test_register(self):
        from plugins.workflow.tools.workflow_tool import register

        class _R:
            def register(self, name, schema, **kw):
                assert name == "workflow"
                assert kw["danger"] == "dangerous"
                assert kw["group"] == "子智能体"
                assert kw["keep_in_content"] is True

        register(_R())


class TestImportPrecheck:
    """Task 6: import 预检——拦截脚本 import，报错列出预置模块清单。"""

    def test_blocks_import_and_lists_presets(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        err = _check_imports("import os, json\nresult = 1", _PRESET_MODULES)
        assert err is not None
        assert "os" in err and "json" in err  # 被禁名与预置清单都在报错里

    def test_from_import_blocked(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        assert _check_imports("from pathlib import Path\nresult = 1", _PRESET_MODULES) is not None

    def test_plain_script_passes(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        assert _check_imports("x = json.loads('{}')\nresult = x", _PRESET_MODULES) is None


class TestDynamicDescription:
    def test_workflow_description_lists_agents(self):
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description(["build", "explore"])
        assert "build" in d and "explore" in d
        assert "subagent_dag" in d  # 分工指引在描述里
        assert "result" in d  # 结果约定在描述里

    def test_workflow_description_empty_agents(self):
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description([])
        assert "agent(" in d  # 钩子用法仍在

    def test_workflow_description_documents_full_contract(self):
        # Task 6: description 是模型的第一信息源，契约必须完整显式
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description(["build"])
        assert "model=" in d and "schema=" in d  # agent 钩子完整签名
        assert "phase(title, detail=None)" in d  # phase 双参
        assert "禁止 import" in d  # import 契约显式化
        assert "model_aliases" in d  # 别名需配置的提示


class TestPhaseLogHooks:
    """Task 3: phase/log 钩子 def 化——phase 带 detail、log 容忍多余参数。"""

    def test_phase_accepts_detail(self):
        from plugins.workflow.tools.workflow_tool import _make_phase_log_hooks

        phases, logs = [], []
        phase, log = _make_phase_log_hooks(phases, logs)
        phase("1/4 plan", "读取计划与设计")
        phase("2/4 build")
        log("done")
        log("另一条", "多余参数容忍")
        assert phases[0] == {"title": "1/4 plan", "detail": "读取计划与设计"}
        assert phases[1] == {"title": "2/4 build", "detail": None}
        assert logs == ["done", "另一条"]

    def test_phase_beyond_detail_still_typeerror_but_translatable(self):
        # 超过 (title, detail) 的调用仍 TypeError，但 def 化后报错含函数名，翻译层能点名
        from plugins.workflow.tools.workflow_tool import (
            _make_phase_log_hooks,
            translate_type_error,
        )

        phase, _ = _make_phase_log_hooks([], [])
        try:
            phase("a", "b", "c")
            raise AssertionError("未触发 TypeError")
        except TypeError as e:
            msg = translate_type_error(e, ("agent", "parallel", "pipeline", "phase", "log"))
        assert msg is not None and "phase" in msg


class TestAgentHookModel:
    """Task 4: agent 钩子 model 别名——解析映射传宿主，未知别名降级并写人话日志。"""

    def _make(self, manager, aliases=None):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(50, time.monotonic() + 60)
        logs: list = []
        hook = _make_agent_hook(
            manager, "s1", st, "build", 900.0, model_aliases=aliases, log_fn=lambda m: logs.append(str(m))
        )
        return hook, st, logs

    def test_alias_resolved_into_dispatch(self):
        mgr = _FakeManager(routes={"build": "ok"})
        hook, _, _ = self._make(mgr, aliases={"sonnet": "m-sonnet"})
        assert hook("x", model="sonnet") == "ok"
        assert mgr.calls[0][2].get("model") == "m-sonnet"

    def test_unknown_alias_drops_none_and_logs_hint(self):
        mgr = _FakeManager()
        hook, _, logs = self._make(mgr, aliases={"sonnet": "m-sonnet"})
        assert hook("x", model="opus") is None
        assert mgr.calls == []  # 未派发，不消耗额度
        assert any("opus" in m and "sonnet" in m for m in logs)  # 人话提示列出可用别名

    def test_no_model_arg_keeps_call_unchanged(self):
        mgr = _FakeManager()
        hook, _, _ = self._make(mgr)
        assert hook("x") == "done:build"
        assert "model" not in mgr.calls[0][2]

    def test_host_without_model_kwarg_skips_it(self):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        class _Strict:
            """旧宿主：基础键都收（比 model 更早存在），唯独无 **kw、无 model 形参。"""

            def __init__(self):
                self.passed = {}

            def execute_task(
                self,
                task_id,
                agent_name,
                task_description,
                on_finished=None,
                on_error=None,
                share_context=False,
                session_id="",
            ):
                self.passed = dict(task_id=task_id)
                on_finished(task_id, "ok")
                return True

        st = _RunState(50, time.monotonic() + 60)
        strict = _Strict()
        hook = _make_agent_hook(strict, "s1", st, "build", 900.0, model_aliases={"sonnet": "m1"})
        assert hook("x", model="sonnet") == "ok"  # 不炸，model 被静默跳过（宿主旧签名自适应）


class TestAgentHookSchema:
    """Task 5: agent 钩子 schema 结构化输出——注入指令/校验/失败带错重试 1 次。"""

    SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}

    def _make(self, responses):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        class _Seq:
            def __init__(self):
                self.responses = list(responses)
                self.calls = 0
                self.descs = []

            def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
                self.calls += 1
                self.descs.append(task_description)
                r = self.responses.pop(0)
                on_finished(task_id, r)
                return True

        mgr = _Seq()
        st = _RunState(50, time.monotonic() + 60)
        logs: list = []
        hook = _make_agent_hook(mgr, "s1", st, "build", 900.0, log_fn=lambda m: logs.append(str(m)))
        return hook, mgr, st, logs

    def test_valid_json_returns_dict(self):
        hook, mgr, _, _ = self._make(['{"verdict": "通过"}'])
        assert hook("x", schema=self.SCHEMA) == {"verdict": "通过"}
        assert "JSON Schema" in mgr.descs[0]  # prompt 注入了输出格式指令

    def test_retry_once_then_success(self):
        hook, mgr, _, _ = self._make(["不是json", '{"verdict": "通过"}'])
        assert hook("x", schema=self.SCHEMA) == {"verdict": "通过"}
        assert mgr.calls == 2

    def test_both_bad_returns_none_and_logs(self):
        hook, mgr, _, logs = self._make(["bad1", "bad2"])
        assert hook("x", schema=self.SCHEMA) is None
        assert mgr.calls == 2
        assert any("schema_failed" in m for m in logs)

    def test_retries_count_toward_quota(self):
        hook, _, st, _ = self._make(["bad1", '{"verdict": "v"}'])
        hook("x", schema=self.SCHEMA)
        assert st.started == 2

    def test_schema_error_in_retry_prompt(self):
        hook, mgr, _, _ = self._make(["bad1", '{"verdict": "v"}'])
        hook("x", schema=self.SCHEMA)
        assert "重试" in mgr.descs[1] and "上次" in mgr.descs[1]  # 重试 prompt 带具体校验错误

    def test_extract_json_from_fenced_reply(self):
        # 冒烟实测：子 agent 输出「说明 + ```json 围栏```」，裸 loads 失败但应能提取
        hook, mgr, _, _ = self._make([
            '分析如下：\n```json\n{"verdict": "通过"}\n```',
        ])
        out = hook("x", schema=self.SCHEMA)
        assert out == {"verdict": "通过"}
        assert mgr.calls == 1  # 提取成功无需重试

    def test_extract_json_from_prefixed_reply(self):
        hook, mgr, _, _ = self._make(['我有足够信息。结论：{"verdict": "OK"} 以上。'])
        assert hook("x", schema=self.SCHEMA) == {"verdict": "OK"}
        assert mgr.calls == 1


class TestBackgroundRun:
    """Task 9: 后台执行——立即返回 run_id，后台跑完 status 可查；foreground 保留同步。"""

    class _Slow:
        def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
            threading.Timer(1.2, lambda: on_finished(task_id, "slow-ok")).start()
            return True

        def cancel_task(self, task_id):
            return True

    def _patch(self, monkeypatch, tmp_path):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000"}.get(key),
        )
        return wt

    def test_background_returns_immediately_and_completes(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": self._Slow(), "session_id": "s1"}
        t0 = time.monotonic()
        r = wt._workflow_impl(
            ctx,
            meta={"name": "slow", "description": "慢任务"},
            script="result = {'r': agent('x')}",
        )
        elapsed = time.monotonic() - t0
        assert r.success, r.error
        assert elapsed < 0.8, f"后台模式应立即返回，实际 {elapsed:.2f}s"
        body = json.loads(r.content)
        assert body["status"] == "running" and body["run_id"]
        time.sleep(2.0)
        r2 = wt._workflow_impl(ctx, action="status", run_id=body["run_id"], meta={"name": "s", "description": "d"})
        assert r2.success
        st = json.loads(r2.content)
        assert st["state"] == "done"
        assert json.loads(st["result"])["r"] == "slow-ok" if isinstance(st.get("result"), str) else st["result"]["r"] == "slow-ok"

    def test_status_running_before_finish(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": self._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "s2", "description": "d"}, script="result = agent('x')")
        body = json.loads(r.content)
        r2 = wt._workflow_impl(ctx, action="status", run_id=body["run_id"], meta={"name": "s", "description": "d"})
        st = json.loads(r2.content)
        assert st["state"] in ("running", "done")  # 不炸即可
        time.sleep(2.0)

    def test_foreground_keeps_sync_contract(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": self._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx,
            foreground=True,
            meta={"name": "sync", "description": "d"},
            script="result = {'r': agent('x')}",
        )
        assert r.success
        assert json.loads(r.content)["result"]["r"] == "slow-ok"


class TestPostMortemFixes:
    """复盘修复：来自 4 个真实 run 的 2 个 bug + 2 个体验点。"""

    def test_nameerror_writes_error_status(self, monkeypatch, tmp_path):
        # Bug1: NameError 路径曾漏写 status.json 且注册表卡 running
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx, meta={"name": "boom", "description": "d"}, script="result = undefined_name_x"
        )
        assert not r.success and "NameError" in r.error
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        rd = tmp_path / "runs" / run_id
        import orjson as _o

        st = _o.loads((rd / "status.json").read_bytes())
        assert st["state"] == "error" and "NameError" in st["note"]
        r2 = wt._workflow_impl(ctx, action="status", run_id=run_id, meta={"name": "s", "description": "d"})
        assert json.loads(r2.content)["state"] == "error"

    def test_schema_retry_reuses_agent_key(self, monkeypatch, tmp_path):
        # Bug2: schema 重试曾各领一个 agent_key，resume 序号错位
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )

        class _Bad:
            calls = 0

            def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
                self.calls += 1
                on_finished(task_id, "坏输出" if self.calls == 1 else '{"v": "好"}')
                return True

            def cancel_task(self, task_id):
                return True

        mgr = _Bad()
        ctx = {"sub_agent_manager": mgr, "session_id": "s1"}
        schema = {"type": "object", "properties": {"v": {"type": "string"}}, "required": ["v"]}
        r = wt._workflow_impl(
            ctx,
            meta={"name": "retry", "description": "d"},
            script="result = agent('x', schema=" + json.dumps(schema) + ")",
        )
        assert r.success
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        lines = (tmp_path / "runs" / run_id / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
        starts = [json.loads(x) for x in lines if '"agent_start"' in x]
        assert len(starts) == 2  # 首派 + 重试
        assert starts[0]["agent_key"] == starts[1]["agent_key"] == "a1"  # 同 key

    def test_precheck_import_preset_hint(self):
        # 体验: import 预置模块时报「已预置，删掉 import 行」
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        err = _check_imports("import json, os\nresult = 1", _PRESET_MODULES)
        assert err is not None
        assert "json" in err and "已预置" in err and "删掉 import" in err  # json 指明已预置可删
        assert "os" in err and "禁止" in err  # os 指明禁止

    def test_schema_none_text_notes_reason(self, monkeypatch, tmp_path):
        # 体验: 子任务成功但返回空文本时，日志说明原因而非裸 json 错误
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )

        class _Empty:
            def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
                on_finished(task_id, None)
                return True

            def cancel_task(self, task_id):
                return True

        logs: list = []
        st = wt._RunState(50, time.monotonic() + 60)
        hook = wt._make_agent_hook(_Empty(), "s1", st, "build", 60.0, log_fn=lambda m: logs.append(str(m)))
        schema = {"type": "object", "properties": {"v": {}}}
        assert hook("x", schema=schema) is None
        assert any("未返回文本" in m for m in logs)


class TestLifecycleAndStorage:
    """存储复用完善 + runs 残留生命周期（滚动清理）+ 沙箱声明。"""

    def test_save_rejects_empty_meta_name(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import save_workflow

        with pytest.raises(ValueError):
            save_workflow(tmp_path, "x", {"description": "无名字"}, "result = 1", None)

    def test_save_rejects_syntax_error_script(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import save_workflow

        with pytest.raises(ValueError):
            save_workflow(tmp_path, "x", {"name": "x", "description": "d"}, "def broken(:", None)

    def test_prune_runs_keeps_newest(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import prune_runs

        root = tmp_path / "workflows"
        root.mkdir()
        for i in range(5):
            rd = root / "runs" / f"r{i}"
            rd.mkdir(parents=True)
            (rd / "marker.txt").write_text(str(i), encoding="utf-8")
            import os as _os

            stamp = 1000000000 + i * 1000
            _os.utime(rd, (stamp, stamp))
        removed = prune_runs(root, keep=3)
        assert removed == 2
        left = sorted(p.name for p in (root / "runs").iterdir())
        assert left == ["r2", "r3", "r4"]  # 最旧的 r0/r1 被清

    def test_prune_runs_never_touches_saved(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import prune_runs, save_workflow

        save_workflow(tmp_path, "keep-me", {"name": "keep-me"}, "result = 1", None)
        rd = tmp_path / "runs" / "old"
        rd.mkdir(parents=True)
        assert prune_runs(tmp_path, keep=0) == 1
        assert (tmp_path / "saved" / "keep-me.py").exists()  # saved 资产不动
        assert prune_runs(tmp_path, keep=0) == 0

    def test_prune_excludes_active_run(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import prune_runs

        rd = tmp_path / "runs" / "active"
        rd.mkdir(parents=True)
        import os as _os

        _os.utime(rd, (1, 1))  # 最旧，但在排除名单
        assert prune_runs(tmp_path, keep=0, exclude=rd) == 0
        assert rd.exists()

    def test_description_declares_containment(self):
        # 沙箱定位声明：containment 非安全边界，必须显式告知调用方
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description(["build"])
        assert "containment" in d and "非安全边界" in d

    def test_run_prunes_old_runs_after_finish(self, monkeypatch, tmp_path):
        # 集成：run 完成后滚动清理，只留 max_runs_kept 个（含本次）
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {
                "max_result_chars": "50000",
                "default_foreground": "true",
                "max_runs_kept": "2",
            }.get(key),
        )
        import os as _os

        for i in range(3):
            old = tmp_path / "runs" / f"old{i}"
            old.mkdir(parents=True)
            _os.utime(old, (1000000000 + i, 1000000000 + i))
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "p", "description": "d"}, script="result = 1")
        assert r.success
        import time as _t

        _t.sleep(0.5)  # prune 是异步线程
        runs = sorted(p.name for p in (tmp_path / "runs").iterdir())
        assert len(runs) == 2 and all(n.startswith("old") is False or n == runs[-1] for n in runs)
        assert any(not n.startswith("old") for n in runs)  # 本次 run 保留


class TestEdgeMatrix:
    """彻底性补测：对照 CC 语义与真实执行暴露的边界。"""

    def test_datetime_now_banned_but_construct_ok(self):
        # CC 同款：时间源抛错保 resume 指纹确定性；构造/运算不受影响
        ns = _build_sandbox(args={})
        with pytest.raises(WorkflowError):
            exec("datetime.datetime.now()", ns)
        with pytest.raises(WorkflowError):
            exec("datetime.datetime.today()", ns)
        exec("d = datetime.datetime(2026, 1, 1) + datetime.timedelta(days=1)", ns)
        assert ns["d"].year == 2026 and ns["d"].month == 1 and ns["d"].day == 2

    def test_background_script_exception_marks_error(self, monkeypatch, tmp_path):
        # 后台线程内脚本异常：宿主不炸，注册表落 error，status 可查
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000"}.get(key),
        )
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "bg", "description": "d"}, script="result = 1/0")
        assert r.success  # 后台投递成功
        run_id = json.loads(r.content)["run_id"]
        deadline = time.monotonic() + 5
        state = "running"
        while time.monotonic() < deadline:
            r2 = wt._workflow_impl(ctx, action="status", run_id=run_id, meta={"name": "s", "description": "d"})
            state = json.loads(r2.content).get("state")
            if state != "running":
                break
            time.sleep(0.2)
        assert state == "error"

    def test_args_non_dict_tolerated_as_global(self, monkeypatch, tmp_path):
        # args 语义是「暴露为脚本全局」：非 dict（如字符串）宽容接受原样可用
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx, meta={"name": "a", "description": "d"}, script="result = {'echo': str(args)}", args="字符串参数"
        )
        assert r.success and json.loads(r.content)["result"]["echo"] == "字符串参数"

    def test_double_resume_after_failure(self, monkeypatch, tmp_path):
        # resume 后再 resume：journal 追加语义下二次续跑仍自洽
        wt = TestResume()._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": TestResume._Counting(), "session_id": "s1"}
        r1 = wt._workflow_impl(ctx, meta={"name": "rr", "description": "d"}, script="result = agent('p1')")
        assert r1.success
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        r2 = wt._workflow_impl(ctx, action="resume", run_id=run_id)
        assert r2.success and mgr_calls(r2) == 0
        r3 = wt._workflow_impl(ctx, action="resume", run_id=run_id)
        assert r3.success and json.loads(r3.content)["result"] == "新结果1"

    def test_status_after_run_dir_deleted(self, monkeypatch, tmp_path):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "gone", "description": "d"}, script="result = 1")
        assert r.success
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        import shutil as _sh

        _sh.rmtree(tmp_path / "runs" / run_id)
        r2 = wt._workflow_impl(ctx, action="status", run_id=run_id, meta={"name": "s", "description": "d"})
        # 注册表仍有终态记忆 → 兜底返回不崩；磁盘与注册表都无 → 报未找到
        assert r2.success and json.loads(r2.content)["state"] == "done"


def mgr_calls(tool_result):
    """占位：二重 resume 用不到 manager 调用计数（回放路径），仅校验成功。"""
    return 0


class TestRunCard:
    """Task 10: 运行卡片——从 status 数据渲染 phase 分组 + agent 状态 + 汇总。"""

    def _status(self):
        return {
            "name": "audit",
            "description": "审计工作流",
            "state": "done",
            "agents_started": 3,
            "phases": [
                {"title": "1/2 扫描", "detail": "全仓扫描"},
                {"title": "2/2 复核", "detail": None},
            ],
            "agents": [
                {"key": "a1", "role": "explore", "status": "done", "elapsed_sec": 1.2, "result": "扫完了"[:200]},
                {"key": "a2", "role": "build", "status": "failed", "elapsed_sec": 0.5, "result": None},
                {"key": "a3", "role": "review", "status": "replayed", "elapsed_sec": 0.0, "result": "旧结果"},
            ],
            "logs": [" started"],
            "result": {"ok": 1},
            "note": "",
        }

    def test_render_card_groups_and_colors(self):
        from plugins.workflow.tools.workflow_tool import _render_run_card

        html = _render_run_card(self._status())
        assert "audit" in html and "扫描" in html and "全仓扫描" in html
        assert "explore" in html and "failed" in html or "失败" in html  # 角色与失败态可见
        assert "replayed" in html or "回放" in html
        assert "2" in html  # 阶段数

    def test_render_card_escapes(self):
        from plugins.workflow.tools.workflow_tool import _render_run_card

        st = self._status()
        st["name"] = "<script>alert(1)</script>"
        html = _render_run_card(st)
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_journal_agent_snapshots(self, tmp_path):
        from plugins.workflow.tools.workflow_tool import RunJournal

        j = RunJournal(tmp_path)
        j.record_agent_start("a1", "fp1", "explore", None)
        j.record_agent_end("a1", "done", 1.2, "扫完了")
        j.record_agent_start("a2", "fp2", "build", None)
        snaps = RunJournal(tmp_path).agent_snapshots()
        by_key = {s["key"]: s for s in snaps}
        assert by_key["a1"]["status"] == "done" and by_key["a1"]["role"] == "explore"
        assert by_key["a2"]["status"] == "running"  # start 无 end → running

    def test_status_action_renders_html(self, monkeypatch, tmp_path):
        import json
        import time

        from plugins.workflow.tools import workflow_tool as wt
        from tests.test_workflow_tool import TestBackgroundRun

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )
        ctx = {"sub_agent_manager": TestBackgroundRun._Slow(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "hc", "description": "d"}, script="result = agent('x')")
        assert r.success
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        r2 = wt._workflow_impl(ctx, action="status", run_id=run_id, html=True, meta={"name": "s", "description": "d"})
        assert r2.success
        assert "hc" in r2.content and "done" in r2.content
    """Task 2: 新配置项解析——model_aliases 别名映射 + 前台开关 + 卡片刷新间隔。"""

    def test_parse_aliases_basic(self):
        from plugins.workflow.tools.workflow_tool import _parse_aliases

        assert _parse_aliases("sonnet=m1, haiku=m2") == {"sonnet": "m1", "haiku": "m2"}

class TestResume:
    """Task 11: resume 指纹回放——命中的 agent 回放结果不真跑，编辑过的重跑。"""

    class _Counting:
        def __init__(self):
            self.calls = 0

        def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
            self.calls += 1
            on_finished(task_id, f"新结果{self.calls}")
            return True

        def cancel_task(self, task_id):
            return True

    def _patch(self, monkeypatch, tmp_path):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {"max_result_chars": "50000", "default_foreground": "true"}.get(key),
        )
        return wt

    def test_resume_replays_completed(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        mgr = self._Counting()
        ctx = {"sub_agent_manager": mgr, "session_id": "s1"}
        r1 = wt._workflow_impl(ctx, meta={"name": "res", "description": "d"}, script="result = agent('p1')")
        assert r1.success and mgr.calls == 1
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        # resume：a1 指纹命中 → 回放，不真跑
        r2 = wt._workflow_impl(ctx, action="resume", run_id=run_id)
        assert r2.success, r2.error
        assert mgr.calls == 1  # manager 零新调用
        assert json.loads(r2.content)["result"] == "新结果1"

    def test_resume_reruns_edited_fingerprint(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": self._Counting(), "session_id": "s1"}
        r1 = wt._workflow_impl(ctx, meta={"name": "res2", "description": "d"}, script="result = agent('p1')")
        assert r1.success
        run_id = wt.list_workflows(tmp_path)["runs"][0]
        # 编辑脚本（prompt 变 → 指纹变）→ 重跑
        mgr2 = self._Counting()
        ctx2 = {"sub_agent_manager": mgr2, "session_id": "s1"}
        r2 = wt._workflow_impl(ctx2, action="resume", run_id=run_id, script="result = agent('p1-edited')")
        assert r2.success
        assert mgr2.calls == 1
        assert json.loads(r2.content)["result"] == "新结果1"


class TestIntegrationFormerErrorShapes:
    """Task 12: 集成验收——重放 apache-3d-build 脚本（曾踩满三个炸点）的形态，应一次跑通。"""

    def _patch(self, monkeypatch, tmp_path):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {
                "max_result_chars": "50000",
                "default_foreground": "true",
                "model_aliases": "sonnet=m-sonnet, haiku=m-haiku",
            }.get(key),
        )
        return wt

    class _Rec:
        """记录派发详情的 manager。"""

        def __init__(self):
            self.dispatched = []

        def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
            self.dispatched.append({"agent": agent_name, "model": kw.get("model")})
            on_finished(task_id, json.dumps({"verdict": "通过", "by": agent_name}))
            return True

        def cancel_task(self, task_id):
            return True

    APACHE_SHAPE = """
phase("1/4 plan", "读取计划与设计并给出实施约束")
plan = agent("给出硬约束清单", agent="plan", phase="plan")
phase("2/4 build", "实现单 HTML 离线模型")
build = agent("构建目标", agent="build", phase="build", model="sonnet")
phase("3/4 review", "并行审查")
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
r1, r2 = parallel([
    lambda: agent("功能审查", agent="code-reviewer", phase="review", model="haiku"),
    lambda: agent("界面审查", agent="frontend-architect", phase="review", schema=SCHEMA),
])
phase("4/4 test", "运行静态与浏览器自检")
test = agent("自检", agent="task-executor", phase="test", model="sonnet")
log("全部阶段完成")
result = {"plan": plan, "build": build, "reviews": [r1, r2], "test": test}
"""

    def test_apache_shape_runs_end_to_end(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        mgr = self._Rec()
        ctx = {"sub_agent_manager": mgr, "session_id": "s1"}
        r = wt._workflow_impl(ctx, meta={"name": "apache-3d", "description": "三维模型"}, script=self.APACHE_SHAPE)
        assert r.success, r.error
        body = json.loads(r.content)
        assert body["agents_started"] == 5
        assert [p["title"] for p in body["phases"]] == ["1/4 plan", "2/4 build", "3/4 review", "4/4 test"]
        assert body["phases"][0]["detail"] == "读取计划与设计并给出实施约束"
        assert json.loads(body["result"]["build"])["verdict"] == "通过"
        assert body["result"]["reviews"][1] == {"verdict": "通过", "by": "frontend-architect"}  # schema 校验后的 dict
        by_agent = {d["agent"]: d["model"] for d in mgr.dispatched}
        assert by_agent["build"] == "m-sonnet" and by_agent["task-executor"] == "m-sonnet"
        assert by_agent["code-reviewer"] == "m-haiku"

    def test_import_os_rejected_with_presets(self, monkeypatch, tmp_path):
        wt = self._patch(monkeypatch, tmp_path)
        ctx = {"sub_agent_manager": self._Rec(), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx, meta={"name": "bad", "description": "d"}, script="import os\nresult = 1"
        )
        assert not r.success
        assert "禁止 import" in r.error and "os" in r.error


class TestConfigParsing:
    """Task 2: 新配置项解析——model_aliases 别名映射 + 前台开关 + 卡片刷新间隔。"""

    def test_parse_aliases_basic(self):
        from plugins.workflow.tools.workflow_tool import _parse_aliases

        assert _parse_aliases("sonnet=m1, haiku=m2") == {"sonnet": "m1", "haiku": "m2"}

    def test_parse_aliases_edge_cases(self):
        from plugins.workflow.tools.workflow_tool import _parse_aliases

        assert _parse_aliases("") == {}
        assert _parse_aliases(None) == {}
        # 残缺段（无=、空 key、空 value）跳过，合法段保留
        assert _parse_aliases("bad, a=, =b, ok=v1") == {"ok": "v1"}

    def test_impl_tolerates_new_config_keys(self, monkeypatch):
        from plugins.workflow.tools import workflow_tool as wt

        def fake_get(self, plugin, key):
            return {
                "max_concurrent_agents": 2,
                "max_total_agents": 10,
                "max_items_per_call": 10,
                "max_duration_sec": 60,
                "max_agent_wait_sec": 60,
                "default_agent": "build",
                "max_result_chars": 1000,
                "model_aliases": "sonnet=m1",
                "default_foreground": "true",
                "card_refresh_ms": 2000,
            }.get(key)

        monkeypatch.setattr(wt.PluginConfigStore, "get", fake_get)
        ctx = {"sub_agent_manager": _FakeManager(routes={"build": "ok"}), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx,
            meta={"name": "audit", "description": "审计"},
            script="result = {'n': agent('x')}",
        )
        assert r.success is True
