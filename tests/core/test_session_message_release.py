# -*- coding: utf-8 -*-
"""会话消息体惰性释放 / 重载（内存治理）回归测试。

对应实现：``app/core/chat_session.py``
- ``ChatSession.messages`` property（访问即重载）
- ``ChatSession.release_messages`` / ``ensure_messages``
- ``SessionManager._release_stale_messages``（只保留当前 + 最近 N 个常驻）

背景实测（dev 库 283MB / 1721 会话）：15 个会话全量常驻 = RSS +126.8MB，
单会话最大 +26.9MB；单会话重载 22-56ms。
"""

import pytest

from app.core.conversation.chat_session import ChatSession, SessionManager


def _msgs(tag: str, n: int = 5):
    return [{"role": "user", "content": f"{tag}-{i}"} for i in range(n)]


@pytest.fixture()
def manager():
    return SessionManager(keep_messages=2)


def test_no_release_without_loader(manager):
    """未注入 loader 时一律不释放：数据不可恢复就不释放。"""
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s1.set_messages(_msgs("a"))
    s2.set_messages(_msgs("b"))
    s3.set_messages(_msgs("c"))
    assert not s1.messages_released
    assert not s2.messages_released
    assert not s3.messages_released
    assert len(s1.messages) == 5


def test_release_inactive_beyond_keep(manager):
    """超出 keep 且最久未访问的非活跃会话被释放，当前与最近的保留。"""
    reloaded = []

    def loader(sid):
        reloaded.append(sid)
        return _msgs("reloaded", 2)

    manager.set_messages_loader(loader)
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s4 = manager.create_new_session()
    for s in (s1, s2, s3, s4):
        s.set_messages(_msgs(s.session_id[:4]))
    # 显式控制访问时间：s1 最久未访问
    manager._last_access[s1.session_id] = 1.0
    manager._last_access[s2.session_id] = 2.0
    manager._last_access[s3.session_id] = 3.0
    manager._release_stale_messages()

    assert s1.messages_released, "最久未访问的非活跃会话应被释放"
    assert not s2.messages_released
    assert not s3.messages_released
    assert not s4.messages_released, "当前会话永不释放"
    assert s1.message_count == 5, "释放后仍保留消息条数"


def test_access_triggers_reload(manager):
    """访问已释放会话的 messages 时自动重载，对调用方透明。"""
    manager.set_messages_loader(lambda sid: _msgs("reloaded", 3))
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s4 = manager.create_new_session()
    for s in (s1, s2, s3, s4):
        s.set_messages(_msgs("x"))
    manager._last_access[s1.session_id] = 1.0
    manager._release_stale_messages()
    assert s1.messages_released

    assert len(s1.messages) == 3
    assert s1.messages[0]["content"] == "reloaded-0"
    assert not s1.messages_released


def test_switch_to_session_reloads(manager):
    """switch_to_session 到已释放会话时立即重载。"""
    manager.set_messages_loader(lambda sid: _msgs("reloaded", 4))
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s4 = manager.create_new_session()
    for s in (s1, s2, s3, s4):
        s.set_messages(_msgs("x"))
    manager._last_access[s1.session_id] = 1.0
    manager._release_stale_messages()
    assert s1.messages_released

    manager.switch_to_session(0)
    assert manager.get_current_session() is s1
    assert not s1.messages_released
    assert len(s1.messages) == 4


def test_loader_unavailable_keeps_released(manager):
    """loader 返回 None（如 HistoryManager 未就绪）时保持已释放，不静默变空。"""
    manager.set_messages_loader(lambda sid: None)
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s4 = manager.create_new_session()
    for s in (s1, s2, s3, s4):
        s.set_messages(_msgs("x"))
    manager._last_access[s1.session_id] = 1.0
    manager._release_stale_messages()
    assert s1.messages_released

    # 重载器不可用：不能把消息写成空列表（空消息被 save 覆盖 SQLite 就是丢历史）
    assert s1.messages == []
    assert s1.messages_released, "重载失败必须保持已释放状态，下次访问再试"


def test_to_dict_reloads_before_export(manager):
    """to_dict（保存路径）在已释放时先重载，不会导出空消息覆盖库。"""
    manager.set_messages_loader(lambda sid: _msgs("reloaded", 6))
    s1 = manager.create_new_session()
    s2 = manager.create_new_session()
    s3 = manager.create_new_session()
    s4 = manager.create_new_session()
    for s in (s1, s2, s3, s4):
        s.set_messages(_msgs("x"))
    manager._last_access[s1.session_id] = 1.0
    manager._release_stale_messages()

    d = s1.to_dict()
    assert len(d["messages"]) == 6
    assert d["message_count"] == 6


def test_clear_resets_released_flag(manager):
    """clear() 后 released 标记复位，避免空列表被误判为已释放。"""
    s = ChatSession(name="t", messages=_msgs("y", 2))
    s._messages_loader = lambda sid: _msgs("z", 1)
    assert s.release_messages() == 2
    s.clear()
    assert not s.messages_released
    assert s.messages == []


# ══ 批 IV：evict hook（_on_evict）══
def test_hook_none_by_default(manager):
    """未注入 set_on_evict 时 _on_evict 为 None，淘汰路径静默跳过回调。"""
    assert manager._on_evict is None
    for _ in range(manager.max_cached_sessions + 3):
        s = manager.create_new_session()
        s.set_messages(_msgs("x", 2))
    # 不抛异常即通过（回调分支被 None 守卫跳过）


def test_on_evict_receives_evicted_sid(manager):
    """淘汰发生时回调收到被淘汰会话的 session_id。"""
    evicted = []
    manager.set_on_evict(evicted.append)
    first = manager.create_new_session()
    first.set_messages(_msgs("first", 2))
    manager._last_access[first.session_id] = 1.0
    for _ in range(manager.max_cached_sessions + 1):
        s = manager.create_new_session()
        s.set_messages(_msgs("x", 2))
    assert first.session_id in evicted, "最久未访问的会话应被淘汰并通知"


def test_on_evict_exception_isolated(manager):
    """回调抛异常不影响淘汰流程本身（异常被隔离记日志）。"""
    def boom(_sid):
        raise RuntimeError("hook boom")

    manager.set_on_evict(boom)
    first = manager.create_new_session()
    first.set_messages(_msgs("first", 2))
    manager._last_access[first.session_id] = 1.0
    for _ in range(manager.max_cached_sessions + 1):
        s = manager.create_new_session()
        s.set_messages(_msgs("x", 2))
    assert first.session_id not in [s.session_id for s in manager.sessions], "淘汰仍执行"
    assert manager.current_index >= 0, "索引一致性与主流程不受影响"


def test_evict_cleans_history_dict(manager):
    """端到端：evict 回调接 HistoryManager 权威释放，被淘汰 sid 的消息被置空。"""
    from app.utils.history_manager import HistoryManager

    hm = HistoryManager()
    hm._history_loaded = True
    hm._history_sessions = []
    active = {s.session_id for s in manager.sessions}
    manager.set_on_evict(lambda sid: hm.release_messages_except(active))

    first = manager.create_new_session()
    first_sid = first.session_id
    first.set_messages(_msgs("first", 3))
    # 预置 HistoryManager 记录（模拟历史面板已有的该会话记录）
    hm._history_sessions.append({
        "session_id": first_sid,
        "title": "t",
        "messages": [{"role": "user", "content": "cached"}],
    })

    manager._last_access[first_sid] = 1.0
    for _ in range(manager.max_cached_sessions + 1):
        s = manager.create_new_session()
        s.set_messages(_msgs("x", 2))

    rec = next(r for r in hm._history_sessions if r["session_id"] == first_sid)
    assert rec["messages"] == [], "evict 后 HistoryManager 记录的 messages 应被权威入口置空"
    assert rec["_messages_released"] is True
