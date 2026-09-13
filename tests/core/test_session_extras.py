# -*- coding: utf-8 -*-
"""message_extras：UI 态字段剥离 / 读回 / 兼容 测试

覆盖 specs/2026-09-07-message-extras-offload-design.md §7：
- 新库自动建 session_msg_extras 表
- save 剥离保活窗外三字段 + 主 blob 轻量化 + _x_idx 哨兵
- 保活窗内字段保留、内存事实源不被污染
- load_msg_extras 读回 / get_full_messages 合并
- 老数据（主 blob 带字段）读时兼容，首次保存收敛
- delete 级联清理 extras、compaction 重写后 extras 对齐
"""

import pytest


def _fresh_store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    store = SessionStore(str(tmp_path))
    assert store.is_initialized, "SessionStore 应初始化成功"
    return store


@pytest.fixture
def store(tmp_path):
    s = _fresh_store(tmp_path)
    yield s
    try:
        s.close()
    except Exception:
        pass
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None


def _msg(role, content, **extra):
    m = {"role": role, "content": content, "timestamp": "2026-01-01 00:00:00"}
    m.update(extra)
    return m


def _session(session_id, messages):
    return {
        "session_id": session_id,
        "title": "t",
        "project": "默认项目",
        "messages": messages,
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": len(messages),
        "user_edited_title": False,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": len(messages),
    }


def test_extras_table_created(store):
    ok, rows = store._db.execute_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='session_msg_extras'")
    assert ok and rows, "session_msg_extras 表应自动创建"


def test_extras_index_created(store):
    ok, rows = store._db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_msg_extras_session'"
    )
    assert ok and rows, "idx_msg_extras_session 索引应自动创建"


def test_extras_table_schema(store):
    """表 schema：复合主键 (session_id, msg_idx, field) + value BLOB"""
    cols = store._db.get_table_info("session_msg_extras")
    col_names = {c["name"] for c in cols}
    assert col_names == {"session_id", "msg_idx", "field", "value"}, f"列名集合不符：{col_names}"
    # 主键集合检查
    pk_cols = [c["name"] for c in cols if c["pk"] > 0]
    assert pk_cols == ["session_id", "msg_idx", "field"], f"复合主键顺序不符：{pk_cols}"
    # value 类型应为 BLOB
    value_col = next(c for c in cols if c["name"] == "value")
    assert value_col["type"].upper() == "BLOB", f"value 应为 BLOB，实际 {value_col['type']}"


def test_save_offloads_fields_outside_keep_window(store):
    """保活窗外三字段剥离 + 主 blob 轻量化 + _x_idx 哨兵；内存事实源不被污染。"""
    messages = []
    for i in range(10):
        messages.append(_msg("assistant", f"a{i}", reasoning_content=f"think-{i}"))
        messages.append(_msg("tool", f"r{i}", arguments={"x": i}, diff="diff-text", name="edit", tool_call_id=f"c{i}"))
    store.save_session(_session("s1", messages))

    loaded = store.get_session("s1")
    msgs = loaded["messages"]
    first = msgs[0]
    assert "reasoning_content" not in first, "保活窗外字段应被剥离"
    assert first.get("_x_idx") == 0, "剥离消息应携带绝对索引哨兵"
    tool0 = msgs[1]
    assert "arguments" not in tool0 and "diff" not in tool0
    # 保活窗：尾部 3 条不剥离
    tail = msgs[-3:]
    for m in tail:
        assert "_x_idx" not in m, "保活窗内消息不应有哨兵"
    # 内存事实源不被污染（save 不就地修改入参）
    assert messages[0].get("reasoning_content") == "think-0"
    # extras 表落行验证（读回 API 在 Task 4 提供，此处直查表）
    ok, rows = store._db.execute_sql(
        "SELECT msg_idx, field FROM session_msg_extras WHERE session_id='s1' AND msg_idx=0"
    )
    assert ok and rows, "msg_idx=0 应有 extras 行"
    assert {r["field"] for r in rows} == {"reasoning_content"}


def test_keep_window_fields_retained(store):
    """保活窗内（尾部 3 条）字段原样保留在主 blob。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(6)]
    store.save_session(_session("s7", messages))
    msgs = store.get_session("s7")["messages"]
    assert "reasoning_content" in msgs[-1]
    assert "reasoning_content" in msgs[-2]
    assert "reasoning_content" in msgs[-3]
    assert "reasoning_content" not in msgs[0]


def test_compaction_rewrite_realigned(store):
    """消息变短（压缩重写）→ 全删全插让 extras 对齐新消息。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(10)]
    store.save_session(_session("s6", messages))
    assert store.load_msg_extras("s6"), "首次保存应有 extras"
    store.save_session(_session("s6", messages[:2]))
    assert store.load_msg_extras("s6") == {}, "截断后旧 extras 应被清空"


def test_repeated_save_idempotent(store):
    """重复保存内容不变时（增量指纹跳过），合并读回不重复累积。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(8)]
    s = _session("s8", messages)
    store.save_session(s)
    store.save_session(s)
    full = store.get_full_messages("s8")
    assert len(full) == 8
    assert full[0]["reasoning_content"] == "t0"


def test_get_full_messages_merges(store):
    """get_full_messages：主 blob + extras 按哨兵索引合并。"""
    messages = [_msg("tool", f"r{i}", arguments={"i": i}, name="edit", tool_call_id=f"c{i}") for i in range(6)]
    store.save_session(_session("s3", messages))
    full = store.get_full_messages("s3")
    assert len(full) == 6
    assert full[0]["arguments"] == {"i": 0}
    assert full[5]["arguments"] == {"i": 5}


def test_load_msg_extras_roundtrip_and_subset(store):
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"think-{i}") for i in range(8)]
    store.save_session(_session("s2", messages))
    patch = store.load_msg_extras("s2", [0, 1])
    assert patch[0]["reasoning_content"] == "think-0"
    assert patch[1]["reasoning_content"] == "think-1"
    assert 2 not in patch
    allp = store.load_msg_extras("s2")
    assert len(allp) == 5  # 8 条 - 保活窗 3


def test_legacy_blob_fields_tolerated(store):
    """老数据：字段在主 blob → 原样读出；首次保存后窗外条目收敛进 extras。

    保活窗内尾部 3 条按设计保留字段（DeepSeek thinking 连续推理依赖），仅验证窗外条目收敛。
    """
    # 6 条消息，窗外 3 条 + 保活窗 3 条
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"legacy-{i}") for i in range(6)]
    store.save_session(_session("s4", messages))
    # 手动还原旧形态：清 extras、主 blob 塞回带字段版本
    from app.core.store.serde import serialize

    store._db.execute_sql("DELETE FROM session_msg_extras WHERE session_id='s4'")
    store._db.execute_sql("UPDATE sessions SET messages=? WHERE session_id='s4'", (serialize(messages),))
    loaded = store.get_session("s4")
    # 窗外条目原样读出（兼容老数据）
    assert loaded["messages"][0].get("reasoning_content") == "legacy-0"
    assert loaded["messages"][1].get("reasoning_content") == "legacy-1"
    # 首次保存收敛：窗外条目剥离到 extras，主 blob 不再带字段
    store.save_session(_session("s4", loaded["messages"]))
    msgs_after = store.get_session("s4")["messages"]
    assert "reasoning_content" not in msgs_after[0]
    assert "reasoning_content" not in msgs_after[1]
    assert "reasoning_content" not in msgs_after[2]
    # 保活窗内尾部 3 条按设计保留
    assert msgs_after[3].get("reasoning_content") == "legacy-3"
    assert msgs_after[4].get("reasoning_content") == "legacy-4"
    assert msgs_after[5].get("reasoning_content") == "legacy-5"
    # 全量读回能拿回全部 6 条 reasoning_content
    full = store.get_full_messages("s4")
    assert len(full) == 6
    assert full[0]["reasoning_content"] == "legacy-0"
    assert full[5]["reasoning_content"] == "legacy-5"


def test_load_msg_extras_empty_idxs_loads_all(store):
    """固化行为：idxs=[] 走全量分支（与 None 同义）。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(8)]
    store.save_session(_session("s10", messages))
    patch = store.load_msg_extras("s10", [])
    assert len(patch) == 5  # 8 条 - 保活窗 3


def test_load_msg_extras_out_of_range_idx(store):
    """越界 idx：无匹配行 → 空 patch，消费方按无 extras 降级。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(8)]
    store.save_session(_session("s11", messages))
    assert store.load_msg_extras("s11", [99999]) == {}


def test_load_msg_extras_unknown_session(store):
    """未保存过的 session_id：返回空 dict 而非抛错。"""
    assert store.load_msg_extras("no-such-session") == {}


def test_delete_cascades_extras(store):
    """删除会话应级联清理 session_msg_extras 子表。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(6)]
    store.save_session(_session("s5", messages))
    assert store.load_msg_extras("s5"), "前置：extras 应有数据"
    store.delete_session("s5")
    assert store.load_msg_extras("s5") == {}, "删除会话应级联清理 extras"


def test_force_cleanup_project_cascades_extras(store):
    """强制清理项目应级联清理 session_msg_extras 子表。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(6)]
    s = _session("s9", messages)
    s["project"] = "测试项目"
    store.save_session(s)
    assert store.load_msg_extras("s9"), "前置：extras 应有数据"
    store.force_cleanup_project("测试项目")
    assert store.load_msg_extras("s9") == {}, "强制清理项目应级联清理 extras"


def test_delete_session_without_extras_ok(store):
    """删除无 extras 的会话：子表 0 行受影响不影响主表删除成功。"""
    messages = [_msg("user", "hi")]
    store.save_session(_session("s12", messages))
    assert store.delete_session("s12")
    assert store.get_session("s12") is None


def test_delete_does_not_touch_other_sessions_extras(store):
    """删除 s5 不影响同库其它会话的 extras。"""
    m5 = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(6)]
    m6 = [_msg("assistant", f"b{i}", reasoning_content=f"u{i}") for i in range(6)]
    store.save_session(_session("s5", m5))
    store.save_session(_session("s6", m6))
    store.delete_session("s5")
    assert store.load_msg_extras("s6"), "其它会话 extras 不应被误删"


def test_archive_by_project_preserves_extras(store):
    """归档（改名非删除）不清理 extras，防止后续误加级联。"""
    messages = [_msg("assistant", f"a{i}", reasoning_content=f"t{i}") for i in range(6)]
    s = _session("s13", messages)
    s["project"] = "归档项目"
    store.save_session(s)
    store.archive_sessions_by_project("归档项目")
    assert store.load_msg_extras("s13"), "归档不应清理 extras"


def test_reload_then_group_preserves_tool_args_for_render(store):
    """回归（2026-09-08 工具折叠框预览全空）：

    保存剥离 arguments 进 extras → 重进软件加载轻量 blob（带 _x_idx）→
    渲染前 group_messages_for_display → normalize_message 白名单剥掉
    _x_idx → materialize_batch_with_extras 找不到哨兵跳过补回 → 折叠框
    预览参数全空。修复后 normalize_message 必须透传 _x_idx，渲染端才能
    按 extras 补回 arguments。
    """
    from app.core.message_content import group_messages_for_display
    from app.widgets.ui_helpers import materialize_batch_with_extras

    messages = []
    for i in range(4):
        messages.append(_msg("user", f"q{i}"))
        messages.append(
            _msg(
                "tool",
                f"result-{i}",
                name="read",
                tool_call_id=f"c{i}",
                arguments={"path": f"a{i}.py", "startline": i},
                success=True,
            )
        )
    store.save_session(_session("sx", messages))

    # 模拟重进软件后的渲染数据链
    loaded = store.get_session("sx")["messages"]
    batches = group_messages_for_display(loaded)

    # 还原消息绝对索引（哨兵在 group 前的位置即 session.messages 下标）
    idx_of = {id(m): i for i, m in enumerate(loaded)}
    seen_args = []
    for b in batches:
        patched = materialize_batch_with_extras(b, "sx", store.load_msg_extras)
        for m in patched:
            if isinstance(m, dict) and m.get("role") == "tool":
                seen_args.append(m.get("arguments"))

    assert len(seen_args) == 4, f"应渲染 4 条工具消息，实际 {len(seen_args)}"
    for i, args in enumerate(seen_args):
        assert args == {"path": f"a{i}.py", "startline": i}, (
            f"第 {i} 条工具消息 arguments 补回失败（渲染后预览将为空）: {args}"
        )


def test_reload_then_append_save_preserves_tool_args(store):
    """回归（2026-09-12 工具完成框描述全空且不可逆）：

    get_session_messages 返回轻量 blob（_x_idx、无 arguments）→ 加载历史
    会话后继续对话并保存 → normalize_message 给轻量消息伪造 arguments={}
    → extract_offload_fields 视为「无剥离字段」不产 extras 行 →
    _write_extras 全删旧 extras 后不插回 → 历史消息参数永久丢失。

    修复：加载路径必须返回全量消息（主 blob + extras 合并），使内存会话
    始终持有完整数据，后续保存的剥离-写回循环自洽。
    """
    from app.utils.history_manager import HistoryManager, merge_session_messages

    messages = [_msg("user", "q0")]
    for i in range(4):
        messages.append(
            _msg(
                "tool",
                f"result-{i}",
                name="grep",
                tool_call_id=f"c{i}",
                arguments={"pattern": f"p{i}"},
                success=True,
            )
        )
        messages.append(_msg("assistant", f"a{i}"))
    store.save_session(_session("sa", messages))

    ok, rows = store._db.execute_sql(
        "SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id='sa'"
    )
    assert rows[0]["n"] > 0, "前置：保存后应有 extras 行"

    # 模拟加载历史会话：get_session_messages 返回轻量形态（修复前）或全量（修复后）
    hm = HistoryManager()
    try:
        hm._session_store = store
        hm._use_sqlite = True
        loaded = hm.get_session_messages("sa")
    finally:
        pass

    # 断言 1：加载进内存的消息必须是全量（历史 tool 消息带 arguments）
    tool_args = [m.get("arguments") for m in loaded if m.get("role") == "tool"]
    assert all(a is not None for a in tool_args), (
        f"加载后历史消息 arguments 缺失（轻量形态泄漏进内存）: {tool_args}"
    )

    # 断言 2：加载后继续对话 → 再保存 → extras 与 blob 保持完整（循环自洽）
    loaded.append(_msg("user", "继续"))
    loaded.append(_msg("assistant", "好的"))
    merged = merge_session_messages(loaded)
    store.save_session(_session("sa", merged))

    ok, rows = store._db.execute_sql(
        "SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id='sa'"
    )
    assert rows[0]["n"] > 0, "再保存后 extras 行被清空（历史参数数据丢失）"

    full = store.get_full_messages("sa")
    roundtrip = {
        m.get("tool_call_id"): m.get("arguments")
        for m in full
        if m.get("role") == "tool"
    }
    for i in range(4):
        assert roundtrip.get(f"c{i}") == {"pattern": f"p{i}"}, (
            f"call_{i} arguments 丢失: {roundtrip.get(f'c{i}')}"
        )


def test_normalize_message_does_not_fake_empty_arguments():
    """轻量剥离消息（无 arguments 键）经 normalize 后不得伪造空 dict。

    伪造的 arguments={} 会让 extract_offload_fields 误判「无剥离字段」，
    掩盖轻量消息泄漏进保存链的事实，是 2026-09-12 数据丢失链的放大器。
    """
    from app.core.message_content import normalize_message

    light = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "grep",
        "content": "r",
        "_x_idx": 3,
    }
    item = normalize_message(light)
    assert item is not None
    assert "arguments" not in item, "normalize 不应为轻量消息伪造空 arguments"

    full = dict(light, arguments={"pattern": "p"})
    item2 = normalize_message(full)
    assert item2.get("arguments") == {"pattern": "p"}, "真实 arguments 必须透传"
