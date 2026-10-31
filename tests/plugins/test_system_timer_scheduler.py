# -*- coding: utf-8 -*-
"""system-timer 调度器测试：纯逻辑 tick / 窗口定位 / QTimer 壳 / provider 真链路 / drain 信号桥接

T2 实际 drain 修法为候选 b「入队信号桥接」（action_queue 入队观察者 +
MainWidget._action_enqueued 信号 + _on_action_enqueued 槽判忙守卫），
本文件组 5 测该槽而非直调 _drain_action_queue。
"""

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

pytest.importorskip("PyQt5.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_UI_MODULE = "ui_plugin_system_timer"


def _drain(ms):
    """推进 Qt 事件循环 ms 毫秒（禁 app.exec_()，防离屏退出竞态 0xC0000409）"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def _load_ui_module():
    """按 ui loader 同款方式加载 ui/__init__.py（相对导入依赖 __init__ 包语义）"""
    p = _ROOT / "plugins" / "system-timer" / "ui" / "__init__.py"
    for name in list(sys.modules):
        if name == _UI_MODULE or name.startswith(_UI_MODULE + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(_UI_MODULE, p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_UI_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


def _scheduler_mod(timer_mod):
    """取 scheduler 子模块（TimerScheduler/_TICK_MS 都在子模块命名空间）。

    ui/__init__.py 顶层不 import scheduler（在 register_ui 内），故未调用过
    register_ui 时 sys.modules 无子模块：此处按文件路径补加载到同一模块名，
    保证与 register_ui 相对导入拿到的是同一个模块对象。
    """
    sub = sys.modules.get(_UI_MODULE + ".scheduler")
    if sub is None:
        p = _ROOT / "plugins" / "system-timer" / "ui" / "scheduler.py"
        spec = importlib.util.spec_from_file_location(_UI_MODULE + ".scheduler", p)
        assert spec is not None and spec.loader is not None
        sub = importlib.util.module_from_spec(spec)
        sys.modules[_UI_MODULE + ".scheduler"] = sub
        spec.loader.exec_module(sub)
    return sub


@pytest.fixture(scope="module")
def timer_mod(qapp):
    """加载 ui 组件模块（qapp 保证 Qt 可用），返回模块对象"""
    return _load_ui_module()


@pytest.fixture()
def fresh_scheduler(timer_mod):
    """每用例独立 TimerScheduler（不 start，纯逻辑用例不创建 QTimer）"""
    s = _scheduler_mod(timer_mod).TimerScheduler()
    yield s
    s.stop()  # 幂等兜底：清理可能被用例启动的 QTimer


# ═══════════════════════════════════════════════════════════════
# 组 1：纯逻辑 tick（now 注入，零 Qt 对象）
# ═══════════════════════════════════════════════════════════════


class _SendRecorder:
    """_send/_find_window 桩：记录调用并可控送达"""

    def __init__(self, delivered=True, on_send=None):
        self.calls = []
        self.delivered = delivered
        self.on_send = on_send

    def find_window(self, session_id):
        return "w-rec"

    def send(self, text, window_id):
        self.calls.append((window_id, text))
        if self.on_send is not None:
            self.on_send()
        return self.delivered


def _arm(s, recorder):
    """把桩挂到 scheduler 实例（绕开 UIPluginRegistry/CapabilityRegistry 依赖）"""
    s._find_window = recorder.find_window
    s._send = recorder.send


class TestPureLogic:
    def test_once_fires_and_leaves_table(self, fresh_scheduler):
        s = fresh_scheduler
        rec = _SendRecorder(delivered=True)
        _arm(s, rec)
        base = time.monotonic()
        task = s.add_task("s1", "提醒喝水", delay_seconds=10)
        s.tick(base)  # 未到期
        assert rec.calls == []
        assert len(s.list_tasks()) == 1
        s.tick(base + 11)  # 到期
        assert rec.calls == [("w-rec", "⏰ 定时任务触发：提醒喝水")]
        assert s.list_tasks() == [], "一次性任务触发后应出表"
        assert task["id"]

    def test_not_due_skipped(self, fresh_scheduler):
        s = fresh_scheduler
        rec = _SendRecorder(delivered=True)
        _arm(s, rec)
        base = time.monotonic()
        s.add_task("s1", "还早", delay_seconds=3600)
        s.tick(base)
        s.tick(base + 1)
        assert rec.calls == []
        assert len(s.list_tasks()) == 1

    def test_loop_reschedules_exact_interval(self, fresh_scheduler):
        """送达后 fire_at 精确 += interval（以 fire_at 自身为 tick 基准，零误差）"""
        s = fresh_scheduler
        rec = _SendRecorder(delivered=True)
        _arm(s, rec)
        task = s.add_task("s1", "整点汇报", interval_minutes=1)
        first_fire = s._tasks[task["id"]]["fire_at"]  # add_task 返回快照，内部值从表读
        s.tick(first_fire)  # 精确到期时刻（fire_at <= now）
        assert len(rec.calls) == 1
        assert s._tasks[task["id"]]["fire_at"] == pytest.approx(first_fire + 60.0), "循环任务应精确顺延一个 interval"
        second_fire = s._tasks[task["id"]]["fire_at"]
        s.tick(second_fire)
        assert len(rec.calls) == 2
        assert s._tasks[task["id"]]["fire_at"] == pytest.approx(second_fire + 60.0)

    def test_missed_once_terminates_loop_defers(self, fresh_scheduler):
        """未送达：一次性 missed 终结出表；循环跳过本轮顺延下轮"""
        s = fresh_scheduler
        rec = _SendRecorder(delivered=False)
        _arm(s, rec)
        base = time.monotonic()
        once = s.add_task("s1", "错过就没了", delay_seconds=5)
        loop = s.add_task("s1", "周期任务", interval_minutes=1)
        now = time.monotonic()
        s.tick(now + 10)
        assert once["id"] not in {t["id"] for t in s.list_tasks()}, "错过的一次性任务应终结"
        remaining = {t["id"]: t for t in s.list_tasks()}
        assert loop["id"] in remaining, "循环任务应保留"
        assert remaining[loop["id"]]["next_fire_seconds"] > 55.0, "循环任务应从现在起算下轮"

    def test_send_side_cancel_no_refire(self, fresh_scheduler):
        """send 桩内 cancel 同任务 / 重入 tick：触发中标记防重复触发"""
        s = fresh_scheduler
        rec = _SendRecorder(delivered=True)
        base = time.monotonic()
        task = s.add_task("s1", "只触发一次", delay_seconds=5)

        def _cancel_inside():
            s.cancel(task_id=task["id"])  # 模拟 send 处理期间任务被取消

        rec.on_send = _cancel_inside
        _arm(s, rec)
        s.tick(base + 10)
        assert len(rec.calls) == 1
        assert s.list_tasks() == []

        # firing 标记重入：send 桩内再 tick（模拟嵌套推进），firing=True 拦住同任务
        s2 = fresh_scheduler
        rec2 = _SendRecorder(delivered=True)
        s2.add_task("s1", "防重入", delay_seconds=5)

        def _retick():
            s2.tick(time.monotonic() + 100)  # 嵌套 tick，同任务处于 firing 中

        rec2.on_send = _retick
        _arm(s2, rec2)
        s2.tick(time.monotonic() + 100)
        assert len(rec2.calls) == 1, "firing 标记未拦住嵌套 tick 重复触发"

    def test_same_tick_multiple_tasks_fire_in_order(self, fresh_scheduler):
        """同 tick 到期的多任务按 fire_at 先后触发（非插入序）"""
        s = fresh_scheduler
        rec = _SendRecorder(delivered=True)
        _arm(s, rec)
        s.add_task("s1", "晚到期", delay_seconds=10)  # 先登记、晚到期
        s.add_task("s1", "早到期", delay_seconds=2)  # 后登记、早到期
        s.tick(time.monotonic() + 11)  # 以真实基准到期（add_task 内部取 monotonic）
        assert [text for _wid, text in rec.calls] == [
            "⏰ 定时任务触发：早到期",
            "⏰ 定时任务触发：晚到期",
        ], f"触发序错乱：{rec.calls}"

    def test_concurrent_add_thread_safety(self, fresh_scheduler):
        """worker 线程并发 add（工具 impl 线程纪律）：50 并发不丢不崩"""
        s = fresh_scheduler
        s._find_window = lambda sid: None
        s._send = lambda text, wid: False

        def _adder():
            for _ in range(50):
                s.add_task("s-worker", "x", delay_seconds=3600)

        threads = [threading.Thread(target=_adder) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(s.list_tasks()) == 200

    def test_fire_exception_recovers_next_tick(self, fresh_scheduler):
        """_fire 锁外抛异常：except 分支复位 firing，下一 tick 该任务仍可触发

        锁 T4R 复审残留：无复位时任务滞留 firing=True 被后续 tick 静默跳过（单任务停摆）。
        """
        s = fresh_scheduler
        task = s.add_task("s1", "异常后恢复", delay_seconds=1)
        calls = []
        state = {"boom": True}

        def _send(text, window_id):
            calls.append(text)
            if state["boom"]:
                state["boom"] = False
                raise RuntimeError("send 桩首调炸")
            return True

        s._find_window = lambda sid: "w-rec"
        s._send = _send
        s.tick(time.monotonic() + 2)  # 首触发：send 抛异常
        assert len(calls) == 1
        assert s._tasks[task["id"]]["firing"] is False, "异常后 firing 未复位，任务将永久停摆"
        s.tick(time.monotonic() + 2.001)  # 下一 tick 仍可正常触发
        assert len(calls) == 2
        assert s.list_tasks() == [], "第二次送达后一次性任务应正常出表"


# ═══════════════════════════════════════════════════════════════
# 组 2：窗口定位（UIPluginRegistry mock，先例 test_welcome_card_plugin_context）
# ═══════════════════════════════════════════════════════════════


class _StubCapRegistry:
    """CapabilityRegistry 替身：捕获 build_pool 的 host 并注入 send 桩（monkeypatch 类整体）"""

    def __init__(self):
        self.sent = []  # [(window_id, text)]
        self.hosts = []

    def get_instance(self):
        return self

    def build_pool(self, *, surface, host, plugin=""):
        self.hosts.append({"surface": surface, "wid": getattr(host, "_window_id", ""), "plugin": plugin})
        sentinel = self

        class _Pool(dict):
            def __contains__(self, key):
                return key == "session.send"

            def __getitem__(self, key):
                # 对齐真实 CapabilityPool 语义：session.send provider 返回裸可调用
                # （tool_core._session_send 直返 _session_control(host)["send"]），非 dict
                assert key == "session.send"
                return lambda text, **kw: sentinel.sent.append((host._window_id, text)) or True

        return _Pool()


class TestWindowResolve:
    @pytest.fixture(autouse=True)
    def _isolated_registry(self):
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        # reset() 会置空单例（_instance=None）→ 必须重解析后再 mount，
        # 否则 _find_window 里 get_instance() 拿到的是另一个空实例
        UIPluginRegistry.get_instance().reset()
        self._reg = UIPluginRegistry.get_instance()
        yield
        UIPluginRegistry.get_instance().reset()

    def _mount_window(self, wid: str, session_id: str):
        win = MagicMock()
        win._window_id = wid
        win.session_manager.get_current_session.return_value.session_id = session_id
        self._reg._window_main_widgets = {wid: win}
        return win

    def test_send_goes_to_matched_window(self, timer_mod, monkeypatch):
        """session_id 命中 → 经该窗口能力池 session.send，prompt 文本与 wid 正确"""
        s = _scheduler_mod(timer_mod).TimerScheduler()
        stub = _StubCapRegistry()
        monkeypatch.setattr("app.plugins.capabilities.registry.CapabilityRegistry", stub)
        self._mount_window("w1", "s-target")
        s.add_task("s-target", "定点提醒", delay_seconds=1)
        s.tick(time.monotonic() + 5)
        assert stub.sent == [("w1", "⏰ 定时任务触发：定点提醒")]
        assert stub.hosts == [{"surface": "tool", "wid": "w1", "plugin": "system-timer"}]
        assert s.list_tasks() == []  # 一次性送达出表
        s.stop()

    def test_session_mismatch_skips(self, timer_mod, monkeypatch):
        """会话切走（session 不匹配）：一次性 missed 终结，循环顺延"""
        s = _scheduler_mod(timer_mod).TimerScheduler()
        stub = _StubCapRegistry()
        monkeypatch.setattr("app.plugins.capabilities.registry.CapabilityRegistry", stub)
        self._mount_window("w1", "s-elsewhere")
        once = s.add_task("s-mine", "会错过的", delay_seconds=1)
        loop = s.add_task("s-mine", "继续的", interval_minutes=1)
        s.tick(time.monotonic() + 5)
        assert stub.sent == [], "不匹配窗口不得串发"
        ids = {t["id"] for t in s.list_tasks()}
        assert once["id"] not in ids
        assert loop["id"] in ids
        s.stop()

    def test_destroyed_window_session_raises_skips(self, timer_mod, monkeypatch):
        """已销毁窗口（session_manager 访问抛 RuntimeError）：跳过不崩"""
        s = _scheduler_mod(timer_mod).TimerScheduler()
        stub = _StubCapRegistry()
        monkeypatch.setattr("app.plugins.capabilities.registry.CapabilityRegistry", stub)
        win = MagicMock()
        win._window_id = "w-dead"
        win.session_manager.get_current_session.side_effect = RuntimeError("wrapped C/C++ object deleted")
        self._reg._window_main_widgets = {"w-dead": win}
        loop = s.add_task("s1", "窗口已死", interval_minutes=1)
        s.tick(time.monotonic() + 5)
        assert stub.sent == []
        assert loop["id"] in {t["id"] for t in s.list_tasks()}
        s.stop()


# ═══════════════════════════════════════════════════════════════
# 组 3：QTimer 壳（qapp + _drain，先例 test_deferred_task_queue）
# ═══════════════════════════════════════════════════════════════


class TestQTimerShell:
    def test_start_stop_idempotent(self, timer_mod):
        s = _scheduler_mod(timer_mod).TimerScheduler()
        try:
            s.start()
            t1 = s._timer
            assert t1.isActive()
            s.start()  # 幂等：不重复创建
            assert s._timer is t1
            s.add_task("s1", "x", delay_seconds=3600)
            s.stop()
            assert not t1.isActive()
            assert s.list_tasks() == []  # stop 清表
            s.stop()  # 幂等：二连 stop 不崩
        finally:
            s.stop()

    def test_real_timer_drives_tick(self, timer_mod, monkeypatch):
        """真 QTimer 推进：10ms interval 端到端送达一条一次性任务"""
        monkeypatch.setattr(_scheduler_mod(timer_mod), "_TICK_MS", 10)
        s = _scheduler_mod(timer_mod).TimerScheduler()
        rec = _SendRecorder(delivered=True)
        _arm(s, rec)
        try:
            s.start()
            s.add_task("s1", "真定时器", delay_seconds=1)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not rec.calls:
                _drain(50)
            assert len(rec.calls) == 1, f"真 QTimer 未推进任务：{rec.calls}"
            assert s.list_tasks() == []
        finally:
            s.stop()

    def test_unload_ui_stops_and_register_revives(self, timer_mod, qapp):
        """unload_ui：QTimer 停 + 表空 + 单例置 None；重新 register_ui 可复活"""
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        sched = _scheduler_mod(timer_mod)
        UIPluginRegistry.get_instance().reset()
        try:
            timer_mod.register_ui(None)
            s1 = sched.get_scheduler()
            assert s1 is not None and s1._timer.isActive()
            s1.add_task("s1", "将被清空", delay_seconds=3600)
            timer_mod.unload_ui(None)
            assert sched.get_scheduler() is None, "unload 后单例必须置 None（防热重载双调度器）"
            assert s1._timer is None and s1.list_tasks() == []  # stop 置 None + 清表
            timer_mod.register_ui(None)  # 热重载复活
            s2 = sched.get_scheduler()
            assert s2 is not None and s2 is not s1
            assert s2._timer.isActive() and s2.list_tasks() == []
        finally:
            timer_mod.unload_ui(None)
            UIPluginRegistry.get_instance().reset()

    def test_tools_resolve_scheduler_via_ui_module(self, qapp):
        """锁「定时调度器未就绪」断裂：ui 真实加载路径下工具侧解析必须命中单例

        回归：ui/__init__.py 曾把 from .scheduler import 放在 register_ui 函数体内
        （局部绑定，父模块命名空间无 get_scheduler），tools 侧 _resolve_scheduler
        经 sys.modules[ui 父模块] 解析恒 None → create 全灭。本用例按宿主真实
        顺序（ui loader 加载 + register_ui → tool loader 加载工具模块）全链路验证。
        """
        p = _ROOT / "plugins" / "system-timer" / "tools" / "timer_tasks.py"
        spec = importlib.util.spec_from_file_location("_plugin_tool_system_timer_timer_tasks", p)
        assert spec is not None and spec.loader is not None
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)

        mod = _load_ui_module()  # 清前缀缓存后按 ui loader 方式重载
        mod.register_ui(None)
        try:
            sched = tool._resolve_scheduler()
            assert sched is not None, "工具侧解析不到调度器单例（ui 模块未暴露 get_scheduler？）"
            assert sched is _scheduler_mod(mod).get_scheduler()
            res = tool._timer_impl({"session_id": "s-wire"}, action="create", prompt="接线验证", delay_seconds=60)
            assert res.success is True, f"真实链路 create 失败：{res.error}"
            assert sched.cancel(all_tasks=True, session_id="s-wire") == 1
        finally:
            mod.unload_ui(None)


# ═══════════════════════════════════════════════════════════════
# 组 4：provider 真链路（先例 test_capability_tool_host_compat）
# ═══════════════════════════════════════════════════════════════


class _Registry:
    def __init__(self):
        self.providers = {}

    def register(self, cap_id, provider, **kwargs):
        self.providers[cap_id] = provider


class _Host:
    def __init__(self, window_id: str):
        self._window_id = window_id


class TestProviderRealChain:
    WID = "w-t3-provider"

    def test_session_send_provider_enqueues(self):
        """B2 形状锁：session.send provider 返回裸可调用（非 dict），直调即入队"""
        from app.core.conversation.action_queue import get_action_queue
        from app.plugins.capabilities.providers import tool_core

        q = get_action_queue()
        q.clear(self.WID)
        reg = _Registry()
        tool_core.register(reg)
        send = reg.providers["session.send"](_Host(self.WID))
        assert callable(send), "session.send 形状回归：应为裸可调用"
        assert send("⏰ 定时任务触发：真链路") is True
        pending = q.peek(self.WID)
        assert [item["action"] for item in pending] == ["send"]
        assert pending[0]["payload"] == {"text": "⏰ 定时任务触发：真链路", "ephemeral": False}
        q.clear(self.WID)

    def test_scheduler_send_real_chain_enqueues(self, timer_mod, monkeypatch):
        """scheduler._send 全真链路：真 build_pool + 真 provider → 入队可见

        测试环境 PluginManager 无 system-timer 注册 → 声明按兼容期全量放行，
        session.send 可用（宿主环境则由 manifest capabilities=["session.send"] 授权）。
        """
        from app.core.conversation.action_queue import get_action_queue
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        q = get_action_queue()
        q.clear(self.WID)
        # reset() 置空单例 → 重解析后再 mount（与 _find_window 里 get_instance 同源）
        UIPluginRegistry.get_instance().reset()
        reg = UIPluginRegistry.get_instance()
        s = _scheduler_mod(timer_mod).TimerScheduler()  # 构造无副作用，try 外建供 finally 兑底
        try:
            self_win = MagicMock()
            self_win._window_id = self.WID
            self_win.session_manager.get_current_session.return_value.session_id = "s-real"
            reg._window_main_widgets = {self.WID: self_win}
            s = _scheduler_mod(timer_mod).TimerScheduler()
            s.add_task("s-real", "全真链路", delay_seconds=1)
            s.tick(time.monotonic() + 5)
            pending = q.peek(self.WID)
            assert [item["action"] for item in pending] == ["send"]
            assert "⏰ 定时任务触发：全真链路" in pending[0]["payload"]["text"]
            assert s.list_tasks() == []
        finally:
            s.stop()
            q.clear(self.WID)
            reg.reset()


# ═══════════════════════════════════════════════════════════════
# 组 5：drain 信号桥接（T2 修法=候选 b：入队观察者 + MainWidget 信号 + 槽）
# ═══════════════════════════════════════════════════════════════


class _Bridge(QObject):
    """main_widget.__init__ 观察者闭包的信号侧替身（emit 线程安全 → 主线程槽）"""

    _action_enqueued = pyqtSignal(str)


class TestDrainSignalBridge:
    WID = "w-t3-bridge"

    @pytest.fixture()
    def wired(self, qapp):
        """真 queue + 真 provider + 桥接信号 + mock win（unbound 槽入口）"""
        from app.core.conversation.action_queue import add_enqueue_observer, get_action_queue
        from app.core.conversation.action_queue import remove_enqueue_observer
        from app.main_widget import OpenAIChatToolWindow
        from app.plugins.capabilities.providers import tool_core

        q = get_action_queue()
        q.clear(self.WID)
        reg = _Registry()
        tool_core.register(reg)
        send_fn = reg.providers["session.send"](_Host(self.WID))
        win = MagicMock()
        win._window_id = self.WID
        win._worker_actually_busy.return_value = False
        # 槽内 self._drain_action_queue() 必须走真实现（MagicMock 自动属性会吞掉调用），
        # 只把最末端 _on_send_clicked 留给 mock 记录
        win._drain_action_queue = lambda: OpenAIChatToolWindow._drain_action_queue(win)
        bridge = _Bridge()
        bridge._action_enqueued.connect(lambda wid: OpenAIChatToolWindow._on_action_enqueued(win, wid))

        def observer(wid: str):
            bridge._action_enqueued.emit(str(wid))

        add_enqueue_observer(observer)

        class _Wiring:
            queue = q
            window = win
            send = staticmethod(lambda text: send_fn(text))
            observer: Any = None  # 类级声明；实例上覆盖为真实回调供用例摘取

        wiring = _Wiring()
        wiring.observer = observer

        yield wiring
        remove_enqueue_observer(observer)
        q.clear(self.WID)

    def test_idle_enqueue_consumed_via_signal(self, wired, qapp):
        """空闲期入队：enqueue → 观察者 → 信号 → 槽 → drain → send handler"""
        wired.send("⏰ 定时任务触发：桥接链路")
        qapp.processEvents()
        wired.window._on_send_clicked.assert_called_once_with(
            "⏰ 定时任务触发：桥接链路", preserve_input=True, ephemeral=False
        )
        assert wired.queue.peek(self.WID) == [], "drain 后队列应清空"

    def test_busy_enqueue_not_consumed_early(self, wired, qapp):
        """busy 期入队：判忙守卫拦住，维持「本轮结束 drain」原契约"""
        wired.window._worker_actually_busy.return_value = True
        wired.send("⏰ busy 期入队")
        qapp.processEvents()
        # busy 期不得提前消费（assert_not_called 不收 message 参，说明放注释）
        wired.window._on_send_clicked.assert_not_called()
        assert len(wired.queue.peek(self.WID)) == 1, "动作应保留待 _on_stream_finished 消费"

    def test_wid_mismatch_no_drain(self, wired):
        """其他窗口的入队不触发本窗口 drain（多窗口隔离）

        须先摘观察者再入队：否则 enqueue 自身经观察者→信号→槽立即 drain
        （这正是产品行为，会消费掉断言用的队列条目）。
        """
        from app.core.conversation.action_queue import add_enqueue_observer, remove_enqueue_observer
        from app.main_widget import OpenAIChatToolWindow

        remove_enqueue_observer(wired.observer)
        try:
            wired.queue.enqueue(self.WID, "send", {"text": "待消费"})
            OpenAIChatToolWindow._on_action_enqueued(wired.window, "w-other")
            assert len(wired.queue.peek(self.WID)) == 1, "wid 不匹配时不得 drain 本窗口队列"
        finally:
            add_enqueue_observer(wired.observer)
            wired.queue.clear(self.WID)

    def test_observer_lifecycle_and_dedup(self):
        """观察者注册去重 / 反注册后不再收到 / 未注册回调反注册幂等"""
        from app.core.conversation.action_queue import (
            add_enqueue_observer,
            get_action_queue,
            remove_enqueue_observer,
        )

        q = get_action_queue()
        q.clear("w-t3-obs")
        got = []
        observer = lambda wid: got.append(wid)
        add_enqueue_observer(observer)
        add_enqueue_observer(observer)  # 去重
        assert q.enqueue("w-t3-obs", "send", {"text": "x"}) is True
        assert got == ["w-t3-obs"]
        remove_enqueue_observer(observer)
        assert q.enqueue("w-t3-obs", "send", {"text": "y"}) is True
        assert got == ["w-t3-obs"], "反注册后不应再收到回调"
        remove_enqueue_observer(observer)  # 幂等：重复反注册不崩
        q.clear("w-t3-obs")

    def test_observer_exception_does_not_break_enqueue(self):
        """单个观察者异常被吞：enqueue 仍返回 True（入队主链路不受污染）"""
        from app.core.conversation.action_queue import (
            add_enqueue_observer,
            get_action_queue,
            remove_enqueue_observer,
        )

        q = get_action_queue()
        q.clear("w-t3-boom")

        def _boom(wid):
            raise RuntimeError("observer boom")

        add_enqueue_observer(_boom)
        try:
            assert q.enqueue("w-t3-boom", "send", {"text": "x"}) is True
            assert len(q.peek("w-t3-boom")) == 1
        finally:
            remove_enqueue_observer(_boom)
            q.clear("w-t3-boom")

    def test_signal_wired_to_slot_in_init(self):
        """B1 防回归：__init__ 里信号必须连接到槽

        PyQt5 未连接信号 emit 是空操作——缺这条接线时 _on_action_enqueued
        成死代码，空闲期入队无人消费（挂死），且组5 其余用例抓不住
        （它们绕过信号直调槽）。接线发生在实例 __init__，无法离屏实例化
        真 MainWidget，故以源码断言锁接线（照 B1 事故现场的修复行）。
        """
        import inspect

        from app.main_widget import OpenAIChatToolWindow

        source = inspect.getsource(OpenAIChatToolWindow.__init__)
        assert "self._action_enqueued.connect(self._on_action_enqueued)" in source, (
            "_action_enqueued 信号未连接 _on_action_enqueued 槽：空闲期入队将无人消费（B1 回归）"
        )
