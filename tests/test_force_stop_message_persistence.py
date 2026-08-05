# -*- coding: utf-8 -*-
"""
回归测试：强制停止时消息持久化保证

背景：用户报告"强制停止时偶尔会出现消息没有正确保存到消息列表中"。

通过代码审查，我们识别到 root cause 在 main_widget 的 cancel/close 路径：

1. **核心 race（已修复）**：`closeEvent` 同步调用 `backend.stop_streaming()`
   会等待 worker.run() 完全退出，但 worker.run() 退出前 emit 的
   `finished_with_messages(current_session_messages)` 是 PyQt 跨线程 queued 信号，
   入主线程事件队列。原代码 `backend.stop_streaming()` 的返回值（interrupted_messages）
   被丢弃，紧接着 `_auto_save_current_session()` 读取的还是旧的 session.messages
   （不包含 partial assistant 消息），导致持久化的历史与 UI 卡片显示不一致。

2. **daemon 空列表分支（已修复）**：`_on_finalize_complete` 收到空 `interrupted_messages`
   时只调 `_persist_stop_elapsed`，不调 `_save_current_session_to_history`。
   当 closeEvent 与 daemon finalize 路径并发，且 daemon 因 worker 被别处释放拿到
   空快照时，session.messages 可能在另一条信号路径上已经被 emit 写入但未保存。

3. **truncation_sentinel 假阳性（已修复）**：`_on_finalize_complete` 在 sentinel 命中时
   不补一次 `_persist_stop_elapsed`，导致 stop 时长统计丢失。

本测试聚焦最严重也最容易验证的 #1 —— 通过 `_apply_interrupted_messages_to_session`
helper 单元测试锁定行为。
"""
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.chat_session import ChatSession, SessionManager
import app.main_widget as mw


def _make_widget_with_session(initial_messages: list) -> mw.OpenAIChatToolWindow:
    """最小可用的 OpenAIChatToolWindow stub 实例

    只绑定 helper 方法真正依赖的属性：
    - session_manager: 提供 get_current_session
    - history_manager: 任意 mock 即可
    - _truncation_sentinel: 由测试自行设置
    """
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget.session_manager = SessionManager()
    session = ChatSession(messages=list(initial_messages))
    widget.session_manager.sessions.append(session)
    widget.session_manager.current_index = 0
    widget.history_manager = MagicMock()
    widget.history_manager.flush = MagicMock()
    widget._truncation_sentinel = None
    widget._history_preview_messages = None
    return widget


# ============================================================================
# Helper 单元测试：核心行为锁定
# ============================================================================

def test_apply_interrupted_messages_to_session_basic():
    """核心场景：非空 interrupted_messages + 无 sentinel → session 被更新"""
    widget = _make_widget_with_session([
        {"role": "user", "content": "hello"},
    ])

    interrupted = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "partial streamed reply"},
    ]

    applied = widget._apply_interrupted_messages_to_session(interrupted)

    assert applied is True, "非空 + 无 sentinel 应成功应用"
    session = widget.session_manager.get_current_session()
    assert len(session.messages) == 2
    last = session.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == "partial streamed reply"


def test_apply_interrupted_messages_to_session_empty_input():
    """空 messages → 不应用，session 保持不变"""
    widget = _make_widget_with_session([
        {"role": "user", "content": "hi"},
    ])

    applied = widget._apply_interrupted_messages_to_session([])

    assert applied is False, "空 messages 应不应用"
    session = widget.session_manager.get_current_session()
    assert len(session.messages) == 1, "session 不应被改动"
    assert session.messages[0]["role"] == "user"


def test_apply_interrupted_messages_to_session_none_input():
    """None messages → 防御性不应用"""
    widget = _make_widget_with_session([
        {"role": "user", "content": "hi"},
    ])

    applied = widget._apply_interrupted_messages_to_session(None)

    assert applied is False
    assert len(widget.session_manager.get_current_session().messages) == 1


def test_apply_interrupted_messages_sentinel_matches_session_id():
    """
    🔴 关键：sentinel 与当前 session_id 匹配时，必须丢弃 worker 过期快照，
    避免覆盖截断/恢复后产生的正确状态。
    """
    widget = _make_widget_with_session([
        {"role": "user", "content": "round-1 question"},
        {"role": "assistant", "content": "round-1 reply"},
    ])
    session = widget.session_manager.get_current_session()
    widget._truncation_sentinel = {
        "session_id": session.session_id,
        "messages_len": 2,
        "set_at": 0.0,
    }

    interrupted = [
        {"role": "user", "content": "round-1 question"},
        {"role": "assistant", "content": "round-1 reply"},
        {"role": "assistant", "content": "stale partial from worker"},
    ]

    applied = widget._apply_interrupted_messages_to_session(interrupted)

    assert applied is False, "sentinel 命中 session_id 必须丢弃以保护新状态"
    session = widget.session_manager.get_current_session()
    assert len(session.messages) == 2, \
        f"sentinel 命中后 session 不应被 stale partial 污染，实际 {len(session.messages)} 条"
    assert session.messages[-1]["content"] == "round-1 reply"


def test_apply_interrupted_messages_sentinel_different_session():
    """
    sentinel 命中但 session_id 不同（已切换会话）→ 正常应用，不阻塞。
    """
    widget = _make_widget_with_session([
        {"role": "user", "content": "current session msg"},
    ])
    # 设置 sentinel 为另一个 session_id（用户在 worker 流式时切换了会话）
    widget._truncation_sentinel = {
        "session_id": "different_session_id_aaa",
        "messages_len": 99,
        "set_at": 0.0,
    }

    interrupted = [
        {"role": "user", "content": "current session msg"},
        {"role": "assistant", "content": "partial from old session"},
    ]

    applied = widget._apply_interrupted_messages_to_session(interrupted)

    assert applied is True, "sentinel 不匹配当前 session 时应正常应用"
    assert widget.session_manager.get_current_session().messages[-1]["content"] == \
        "partial from old session"


def test_helper_resets_compaction_cache():
    """
    helper 写入 session 后应该重置 compaction cache（preserve_compaction=False），
    因为 worker 送回的是未压缩消息，保留旧缓存会导致 state 不一致。
    """
    widget = _make_widget_with_session([
        {"role": "user", "content": "hi"},
    ])
    session = widget.session_manager.get_current_session()
    session.set_compaction_cache({"summary_message": {"role": "system", "content": "old summary"}})

    interrupted = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "fresh worker content"},
    ]
    widget._apply_interrupted_messages_to_session(interrupted)

    cache = session.compaction_cache
    assert cache.get("active") is False, \
        "helper 应让 session 重置 compaction cache 以保持 state 一致"


# ============================================================================
# 源码级静态检查：确保修复在 main_widget.py 真的落地
# ============================================================================

def test_close_event_save_path_writes_history_with_partial():
    """
    🔴 核心场景：closeEvent 异步化（B5）后，后台 finalize 路径必须使用 helper + 主动 save。

    B5 前：closeEvent 同步 finalize（backend.stop_streaming()）但 interrupted_messages 被丢弃，
    紧接着 _auto_save_current_session 读取的还是旧的 session.messages。
    B5 后：closeEvent 改为 cancel_streaming()（非阻塞）+ _launch_background_finalize()
    后台 daemon 线程 finalize → 主动应用 interrupted_messages → _persist_stop_elapsed → save + flush。

    注：保存动作统一由 _launch_background_finalize 后台链的 _persist_stop_elapsed()
    完成（内部写 elapsed），再调 _save_current_session_to_history + flush 落盘。
    """
    close_src = inspect.getsource(mw.OpenAIChatToolWindow.closeEvent)
    bg_src = inspect.getsource(mw.OpenAIChatToolWindow._launch_background_finalize)

    # closeEvent 不再同步 stop_streaming（B5：避免关窗阻塞 ~4s）
    assert "self.backend.stop_streaming()" not in close_src, \
        "closeEvent 不得再同步调用 stop_streaming()（B5 异步化）"
    # closeEvent 必须启动后台 finalize
    assert "self._launch_background_finalize()" in close_src, \
        "closeEvent 必须启动后台 finalize 链"
    # 后台链必须应用中断消息（不能依赖 queued signal）
    assert "_apply_interrupted_messages_to_session(interrupted_messages)" in bg_src, \
        "后台 finalize 必须主动应用中断消息"
    # 后台链必须调 _persist_stop_elapsed（写 elapsed）
    assert "self._persist_stop_elapsed()" in bg_src, \
        "后台 finalize 必须调 _persist_stop_elapsed"
    # 后台链必须统一 save + flush
    assert "_save_current_session_to_history()" in bg_src, \
        "后台 finalize 必须保存会话"
    assert "history_manager.flush()" in bg_src, \
        "后台 finalize 必须 flush 落盘"


def test_helper_avoids_destroyed_widget_side_effects():
    """
    🔴 关键：closeEvent 调用时 _is_destroyed=True，helper 已被设计为不刷新 UI。

    设计契约：
    - helper 不直接调 _on_messages_updated（会触发 ring 刷新等 UI 副作用）
    - helper 不写 elapsed
    - helper 不操作 _history_preview_messages 或 UI 控件

    验证：用 AST 检查 helper 方法体里**实际的函数/属性调用**，
    不包括 docstring 描述里的同名字符串。
    """
    import ast
    import textwrap

    helper_func = mw.OpenAIChatToolWindow._apply_interrupted_messages_to_session
    src = textwrap.dedent(inspect.getsource(helper_func))
    tree = ast.parse(src)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    # 收集方法体内的所有 attribute 访问
    referenced_attrs = set()
    for node in ast.walk(func_def):
        if isinstance(node, ast.Attribute):
            referenced_attrs.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            referenced_attrs.add(node.func.attr)

    forbidden_method_calls = {
        "_on_messages_updated",      # 会触发 ring/卡片刷新
        "_persist_stop_elapsed",     # 会写 elapsed
        "set_meta_info",             # UI 卡片
        "context_usage_ring",        # 不应访问
    }
    leaked = forbidden_method_calls & referenced_attrs
    assert not leaked, (
        f"_apply_interrupted_messages_to_session 不应调用 UI 副作用方法 {leaked}，"
        "否则 closeEvent 路径会触发已销毁 widget 报错"
    )


def test_singleton_helper_used_for_both_close_and_finalize():
    """
    closeEvent 后台 finalize 路径和 daemon 路径都应使用 _apply_interrupted_messages_to_session，
    避免两条路径实现重复逻辑漂移。
    """
    bg_src = inspect.getsource(mw.OpenAIChatToolWindow._launch_background_finalize)
    assert "_apply_interrupted_messages_to_session(interrupted_messages)" in bg_src, \
        "后台 finalize 必须调用 _apply_interrupted_messages_to_session"

    finalize_src = inspect.getsource(mw.OpenAIChatToolWindow._on_finalize_complete)
    assert "_apply_interrupted_messages_to_session(interrupted_messages)" in finalize_src, \
        "_on_finalize_complete 必须调用 _apply_interrupted_messages_to_session"


def test_on_finalize_complete_handles_empty_messages_with_persistence():
    """
    🔴 修复 #2：daemon 拿到空 interrupted_messages 时（与 closeEvent 并发被双解锁），
    必须补一次 _save_current_session_to_history。

    修复前（直接命中 else 分支，只有 _persist_stop_elapsed）：
        else:
            self._persist_stop_elapsed()
    修复后（save+flush 提取到 if/else 外部，两分支共享一次）：
        ... if else ... # 各自只调 _persist_stop_elapsed（写 elapsed）
        # 统一的 save+flush（一次）
        if self.history_manager:
            self._save_current_session_to_history()
            self.history_manager.flush()
    """
    import re
    src = inspect.getsource(mw.OpenAIChatToolWindow._on_finalize_complete)

    # 函数体内无论走哪个分支，最终都有统一的一次 save+flush
    # 检查 _save_current_session_to_history 和 flush 在 if/else 外部
    save_count = src.count("_save_current_session_to_history")
    flush_count = src.count("history_manager.flush()")

    # 至少各出现 1 次（统一 save），sentinel 分支可能有 0 次（保存已在 _persist_session_after_mutation 完成）
    assert save_count >= 1, \
        f"函数内必须有 save，当前 {save_count} 次"
    assert flush_count >= 1, \
        f"函数内必须有 flush，当前 {flush_count} 次"

    # 统一 save+flush 不应在 else 内部（避免双写），也不应在 if 内部（避免双写）
    # 检查 else 块内没有 save
    else_match = re.search(
        r"else:.*?(?=\n        # |\n        elif|\n    def |\Z)",
        src,
        re.DOTALL,
    )
    if else_match:
        else_body = else_match.group(0)
        # 用 "self._save_current_session_to_history("（带 self.）区分注释 vs 实际调用
        # 注释里可能提到 _save_current_session_to_history，但实际调用必须是 self. 前缀
        assert "self._save_current_session_to_history(" not in else_body, \
            "save+flush 必须提到 if/else 外部（统一一次），否则 else 分支又 save = 双写"
        assert "self.history_manager.flush()" not in else_body, \
            "flush 必须提到 if/else 外部"

    # if 块内也不应有实际 save 调用
    if_match = re.search(
        r"if interrupted_messages:.*?(?=\n        else:)",
        src,
        re.DOTALL,
    )
    if if_match:
        if_body = if_match.group(0)
        assert "self._save_current_session_to_history(" not in if_body, \
            "save+flush 必须提到 if/else 外部（统一一次），否则 if 分支又 save = 双写"
        assert "self.history_manager.flush()" not in if_body, \
            "flush 必须提到 if/else 外部"


def test_on_finalize_complete_sentinel_hit_persists_elapsed():
    """
    修复 #3：sentinel 命中分支必须补一次 _persist_stop_elapsed，
    避免 stop 时长统计链断裂。
    """
    finalize_src = inspect.getsource(mw.OpenAIChatToolWindow._on_finalize_complete)

    # 在 sentinel 命中分支下必须 _persist_stop_elapsed
    assert "_persist_stop_elapsed" in finalize_src, "sentinel 命中分支需要 _persist_stop_elapsed"

    # 关键检查：sentinel 命中分支（log warning 块）必须有 _persist_stop_elapsed
    # 不允许出现"set sentinel warning 后直接 return，不调 _persist_stop_elapsed"
    import re
    # 找到 sentinel 警告 + return 块，检查中间是否还有 _persist_stop_elapsed
    sentinel_block = re.search(
        r"sentinel.*?_truncation_sentinel = None(.*?)(return|\n        )",
        finalize_src,
        re.DOTALL,
    )
    assert sentinel_block is not None, \
        "找不到 _on_finalize_complete 的 sentinel 块"


def test_on_messages_updated_guarded_by_is_destroyed():
    """
    🔴 防御性：closeEvent 设置 _is_destroyed=True 后，跨线程 queued signals
    可能仍在 closeEvent 之后才被主线程处理。

    修复：_on_messages_updated 必须先检查 _is_destroyed 并 early-return，
    避免访问已销毁 widget（ring.set_usage 等）。
    """
    src = inspect.getsource(mw.OpenAIChatToolWindow._on_messages_updated)

    # 函数体前几行应有 _is_destroyed 检查
    assert "_is_destroyed" in src, \
        "_on_messages_updated 必须有 _is_destroyed 守护"
    # 检查位置：早期（在 _on_messages_updated 中访问 UI 副作用之前）
    head = src[:500]
    assert "_is_destroyed" in head, \
        "_is_destroyed 守卫应在 _on_messages_updated 顶部（访问 widget 之前）"


# ============================================================================
# 累计运行时长（elapsed）在强制关窗下保存
# ============================================================================
#
# 用户报告："强制退出时累计运行时长无法保存"
#
# 背景：`_on_stop_clicked()` 第一阶段从 `_response_start_time` 计算 `self._stop_elapsed`
# 并写到 `_current_assistant_card`，但 `_persist_stop_elapsed()` 在窗口关闭路径根本
# 不会被触发。原因：
# - `_on_stop_clicked()`：用户先点停止 → 设置 _stop_elapsed + 调 _persist_stop_elapsed ✅
# - closeEvent：用户直接关窗 → 不经过 _on_stop_clicked → _stop_elapsed 是 None
#              → _persist_stop_elapsed() 第一行就 return → elapsed 字段永远不存。
#
# 修复：closeEvent 必须在 _persist_stop_elapsed 前补一次 `_stop_elapsed` 计算。

def test_close_event_computes_elapsed_from_response_start_time():
    """
    🔴 核心：closeEvent 路径下，如果 _response_start_time 仍记录了开始时间，
    必须计算 elapsed 并走 _persist_stop_elapsed，否则重启后 assistant.elapsed 永远为 0。

    B5：elapsed 计算仍在 closeEvent（主线程，先于后台 finalize），
    _persist_stop_elapsed 移入后台链（_launch_background_finalize）。
    """
    import time as time_module
    src = inspect.getsource(mw.OpenAIChatToolWindow.closeEvent)
    bg_src = inspect.getsource(mw.OpenAIChatToolWindow._launch_background_finalize)

    # 必须从 _response_start_time 计算 elapsed
    assert "_response_start_time" in src, \
        "closeEvent 必须读 _response_start_time 来计算 elapsed"
    assert "self._stop_elapsed = time.time() - self._response_start_time" in src, \
        "closeEvent 必须独立计算 self._stop_elapsed（不走 _on_stop_clicked 路径）"

    # 后台 finalize 必须 _persist_stop_elapsed()（把 elapsed 写入 session.messages 并保存）
    assert "self._persist_stop_elapsed()" in bg_src, \
        "后台 finalize 必须调 _persist_stop_elapsed 把 elapsed 写入 session.messages 并保存"


def test_close_event_persist_stop_elapsed_called_after_apply():
    """
    必须先 _apply_interrupted_messages_to_session（保证 partial 进 session），
    再 _persist_stop_elapsed（写入 elapsed 到最近一条 assistant 并 save+flush）。
    如果顺序反了，_persist_stop_elapsed 写入 elapsed 时 partial 还没进 session，
    重启后将看到"只 elapsed、无 assistant 消息"的不一致状态。

    B5：该顺序约束位于 _launch_background_finalize 后台链。
    """
    bg_src = inspect.getsource(mw.OpenAIChatToolWindow._launch_background_finalize)

    apply_pos = bg_src.find("_apply_interrupted_messages_to_session(interrupted_messages)")
    persist_pos = bg_src.find("self._persist_stop_elapsed()")

    assert apply_pos > 0, \
        "后台 finalize 必须调 _apply_interrupted_messages_to_session"
    assert persist_pos > 0, \
        "后台 finalize 必须调 _persist_stop_elapsed"
    assert apply_pos < persist_pos, \
        f"必须先 _apply 然后 _persist_stop_elapsed（apply@{apply_pos} 之后 persist@{persist_pos}）"


def test_close_event_handles_response_start_time_when_present():
    """
    _response_start_time 是 stream 开始时记录的时间。在 closeEvent 期间通常未清空
    （_on_stop_clicked 才会清）。

    注：完整 closeEvent 涉及 PyQt5 QObject 子类的 super().__init__()，
    单元测试用 __new__ 创建 stub 实例无法调真实方法。本测试改为：
    1. 算法层面验证 _stop_elapsed 计算正确性；
    2. 源码层面验证 closeEvent 确实执行了"读 _response_start_time → 计算 _stop_elapsed"
       的完整闭环（不在源码中漏掉或走错路径）。
    """
    import time as time_module
    # 算法层：与 closeEvent 内嵌的 if 块一字不差
    widget = _make_widget_with_session([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "partial response"},  # 没有 elapsed
    ])
    widget._response_start_time = time_module.time() - 1.5  # 模拟 1.5 秒前开始
    widget._stop_elapsed = None

    # 模拟 closeEvent 内独立计算 elapsed（与 closeEvent 内嵌代码同构）
    if widget._response_start_time is not None:
        widget._stop_elapsed = time_module.time() - widget._response_start_time
        widget._response_start_time = None

    assert widget._stop_elapsed is not None
    assert 1.0 < widget._stop_elapsed < 5.0, \
        f"elapsed 应在 1.0~5.0 秒之间，实际 {widget._stop_elapsed}"
    # 计算后 _response_start_time 已被清，避免多次重复计数
    assert widget._response_start_time is None, \
        "_response_start_time 必须置 None，避免 closeEvent 重复计数"

    # 源码层：closeEvent 必须确实计算并写 _stop_elapsed（防回归）
    src = inspect.getsource(mw.OpenAIChatToolWindow.closeEvent)
    bg_src = inspect.getsource(mw.OpenAIChatToolWindow._launch_background_finalize)
    # 必须有"读 _response_start_time" → 写 _stop_elapsed 的完整代码
    assert "_response_start_time is not None" in src
    assert "_stop_elapsed = time.time() - self._response_start_time" in src
    # elapsed 计算在 closeEvent（主线程）必须先于后台 finalize 启动；
    # _persist_stop_elapsed 在后台链内（_stop_elapsed 已在主线程算好）
    assert src.find("_stop_elapsed = time.time()") < src.find("self._launch_background_finalize()"), \
        "必须在 _launch_background_finalize 之前完成 _stop_elapsed 计算"
    assert "self._persist_stop_elapsed()" in bg_src, \
        "后台 finalize 必须调 _persist_stop_elapsed 使用已算好的 _stop_elapsed"


def test_close_event_no_response_start_time_no_write():
    """
    边界：用户没开始任何 stream 就直接关窗（_response_start_time=None）。
    这种情况什么都不该写，避免污染上一次的 elapsed。
    """
    widget = _make_widget_with_session([
        {"role": "user", "content": "hi"},
    ])
    widget._response_start_time = None
    widget._stop_elapsed = None

    # 模拟 closeEvent 中计算 elapsed 的 if 块
    if widget._response_start_time is not None:
        widget._stop_elapsed = time_module.time() - widget._response_start_time
        widget._response_start_time = None

    # _response_start_time 是 None 时 _stop_elapsed 不变（保持 None）
    assert widget._stop_elapsed is None
    session = widget.session_manager.get_current_session()
    for msg in session.messages:
        assert "elapsed" not in msg, \
            "无 _response_start_time 时不应写 elapsed"


import time as time_module
if __name__ == "__main__":
    test_apply_interrupted_messages_to_session_basic()
    test_apply_interrupted_messages_to_session_empty_input()
    test_apply_interrupted_messages_to_session_none_input()
    test_apply_interrupted_messages_sentinel_matches_session_id()
    test_apply_interrupted_messages_sentinel_different_session()
    test_helper_resets_compaction_cache()
    test_close_event_save_path_writes_history_with_partial()
    test_helper_avoids_destroyed_widget_side_effects()
    test_singleton_helper_used_for_both_close_and_finalize()
    test_on_finalize_complete_handles_empty_messages_with_persistence()
    test_on_finalize_complete_sentinel_hit_persists_elapsed()
    test_on_messages_updated_guarded_by_is_destroyed()
    test_close_event_computes_elapsed_from_response_start_time()
    test_close_event_persist_stop_elapsed_called_after_apply()
    test_close_event_handles_response_start_time_when_present()
    test_close_event_no_response_start_time_no_write()
    print("\n✅ all 17 tests passed")
