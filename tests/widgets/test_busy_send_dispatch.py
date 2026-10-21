# -*- coding: utf-8 -*-
"""繁忙发送派发加固测试（2026-09-13）

场景：UI 的 _is_streaming 在流式收尾阶段领先于 worker（worker 已退出、
finished_with_content 尚未派发）。此时按「繁忙」处理会把消息放进无人消费的
队列——插话进 hook 队列无人消费、排队等不到 _on_stream_finished 的续发调度，
表现为「发了消息 AI 无响应」。

加固：_dispatch_busy_send 延迟一 tick 用 worker 真实状态（engine.is_streaming）
复核，worker 已退出时复位滞后标志并改走新一轮发送。
"""

from app.main_widget import OpenAIChatToolWindow


def _make_widget(*, ui_streaming: bool, engine_streaming):
    """构造最小 widget：__new__ 绕过 Qt 初始化，仅注入判定链路依赖的属性"""
    w = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    engine = None if engine_streaming is None else type("E", (), {"is_streaming": engine_streaming})()
    w.backend = type("B", (), {"chat_engine": engine})()
    w._is_streaming = ui_streaming
    w._is_destroyed = False
    w._toggle_send_stop = lambda flag: None
    w._clear_input_area = lambda: None
    w._clear_attachments = lambda: None
    w._pending_message_queue = []
    return w


class TestWorkerActuallyBusy:
    def test_engine_streaming_counts_as_busy(self):
        w = _make_widget(ui_streaming=False, engine_streaming=True)
        assert OpenAIChatToolWindow._worker_actually_busy(w) is True

    def test_stale_ui_flag_is_not_busy(self):
        """worker 已退出（engine 不忙）→ 非繁忙，即便 UI 标志仍为 True"""
        w = _make_widget(ui_streaming=True, engine_streaming=False)
        assert OpenAIChatToolWindow._worker_actually_busy(w) is False

    def test_missing_engine_falls_back_to_ui_flag(self):
        """engine 不可用 → 退回 UI 标志（保守判繁忙）"""
        w = _make_widget(ui_streaming=True, engine_streaming=None)
        assert OpenAIChatToolWindow._worker_actually_busy(w) is True


class TestScheduleBusySend:
    def test_single_shot_receives_wrapped_callable(self, monkeypatch):
        """QTimer.singleShot 参数形式回归（2026-09-13 实测 TypeError）

        PySide6 的 singleShot 只接受 (msec, receiver, callable)，业务参数直传会抛
        「singleShot expected at most 4 arguments, got 6」——必须包在 lambda/partial 内。
        """
        import app.main_widget as mw

        captured: dict = {}

        def fake_single_shot(msec, callable_obj=None, *extra):
            captured["msec"] = msec
            captured["fn"] = callable_obj
            captured["extra"] = extra

        monkeypatch.setattr(mw, "QTimer", type("FakeQTimer", (), {"singleShot": staticmethod(fake_single_shot)}))
        w = _make_widget(ui_streaming=True, engine_streaming=True)

        OpenAIChatToolWindow._schedule_busy_send(w, "文本", ["a.png"], "queue", True)

        assert captured["msec"] == 0
        assert captured["extra"] == (), "业务参数不能作为 singleShot 位置参数直传"
        assert callable(captured["fn"])


class TestDispatchBusySend:
    def test_idle_worker_routes_to_new_round_with_flag_reset(self):
        """worker 已结束：复位滞后标志，消息按新一轮发送（而非入队孤儿）"""
        w = _make_widget(ui_streaming=True, engine_streaming=False)
        calls: dict = {}
        w._continue_from_entry = lambda entry: calls.setdefault("entry", entry)
        w._enqueue_pending_message = lambda t, i: calls.setdefault("queue", (t, i))
        w._interject_message = lambda t, i: calls.setdefault("interject", (t, i))

        OpenAIChatToolWindow._dispatch_busy_send(w, "迟到的消息", ["a.png"], "queue", True)

        assert calls["entry"]["text"] == "迟到的消息"
        assert calls["entry"]["image_paths"] == ["a.png"]
        assert "queue" not in calls
        assert "interject" not in calls
        assert w._is_streaming is False

    def test_busy_worker_keeps_queue_route(self):
        """worker 真忙：按设置项路由（queue → 排队）"""
        w = _make_widget(ui_streaming=True, engine_streaming=True)
        calls: dict = {}
        w._continue_from_entry = lambda entry: calls.setdefault("entry", entry)
        w._enqueue_pending_message = lambda t, i: calls.setdefault("queue", (t, i))

        OpenAIChatToolWindow._dispatch_busy_send(w, "排队消息", [], "queue", False)

        assert calls["queue"][0] == "排队消息"
        assert "entry" not in calls

    def test_busy_worker_keeps_interject_route(self):
        """worker 真忙：interject → 插话注入"""
        w = _make_widget(ui_streaming=True, engine_streaming=True)
        calls: dict = {}
        w._continue_from_entry = lambda entry: calls.setdefault("entry", entry)
        w._interject_message = lambda t, i: calls.setdefault("interject", (t, i))

        OpenAIChatToolWindow._dispatch_busy_send(w, "插话消息", [], "interject", True)

        assert calls["interject"][0] == "插话消息"
        assert "entry" not in calls
