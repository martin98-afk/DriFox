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
