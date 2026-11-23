# -*- coding: utf-8 -*-
"""[E12] 方案 α 前提与行为回归：流式拍 blob 存全量（跳过 extract+write_extras）。

方案 α（leader 批准）：流式拍（尾条 ``_partial`` 声明 + 窗口长度判定成立）跳过
extract+write_extras，blob 直接存全量消息；落定拍（无 ``_partial``）走完整链
恢复剥离。窗口内强退恢复由 blob 自含全量结构性保证（崩溃窗口上限 = 一个
流式拍周期，复用 main_widget 现有 25s partial flush，无新增定时器）。

三个附加验证：
1. merge_extras_into 对「blob 已含字段」幂等（α 成立前提）：重复 merge 不双写
   不冲突，结果与单次一致；extras 值与 blob 值相同（窗外未变判定保证）。
2. 净收益复测见 tmp 基准（serialize 膨胀 vs 跳过节省，汇报数字对照）。
3. finish 拍触发点 = 现有落定保存链（无 ``_partial`` 的普通 save 即恢复剥离）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.store.session_repository import merge_extras_into  # noqa: E402
from app.core.store.session_store import SessionStore  # noqa: E402
from app.utils.db_manager import DatabaseManager  # noqa: E402

OFFLOAD_KEYS = ("reasoning_content", "arguments", "diff")


@pytest.fixture()
def store(tmp_path):
    """临时 SQLite 的 SessionStore（单例逐例重建，对齐 E10 口径）。"""
    SessionStore._instance = None
    DatabaseManager._instance = None
    s = SessionStore(str(tmp_path / "db"))
    assert s.is_initialized
    yield s
    try:
        s.close()
    except Exception:
        pass
    SessionStore._instance = None
    DatabaseManager._instance = None


def _mk_msgs(n: int, partial_tail: bool = False) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    for i in range(n):
        if i % 2 == 0:
            msgs.append({"role": "user", "content": f"问题-{i} " + "上下文" * 20, "timestamp": "2026-01-01 00:00:00"})
        else:
            msgs.append(
                {
                    "role": "assistant",
                    "content": f"回答-{i} " + "正文" * 30,
                    "reasoning_content": f"思考-{i}",
                    "arguments": {"tool": f"t{i}", "data": list(range(8))},
                    "diff": f"diff-{i}",
                    "timestamp": "2026-01-01 00:00:00",
                }
            )
    if partial_tail:
        # 对齐 main_widget._maybe_flush_partial_reply：尾条 _partial 流式残缺回复
        msgs.append({"role": "assistant", "content": "半截回复…", "_partial": True, "timestamp": "2026-01-01 00:00:01"})
    return msgs


def _make_session(sid: str, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"session_id": sid, "name": f"会话-{sid}", "project": "默认项目", "messages": msgs, "message_count": len(msgs)}


def _roundtrip_ok(repo, sid: str, origin: List[Dict[str, Any]]) -> None:
    merged = repo.get_full_messages(sid)
    # 尾条 _partial 仅存在于流式拍输入，落定拍后消失，这里按非 partial 对齐
    core = [m for m in origin if not m.get("_partial")]
    assert len(merged) == len(core), f"条数不一致: {len(merged)} != {len(core)}"
    for i, (src, got) in enumerate(zip(core, merged)):
        assert got.get("content") == src.get("content"), f"msg[{i}] content 漂移"
        for f in OFFLOAD_KEYS:
            assert got.get(f) == src.get(f), f"msg[{i}] 字段 {f} 恢复不一致"


# ── 附加验证 1：merge_extras_into 幂等（blob 已含字段） ─────────────


def test_merge_extras_idempotent_when_blob_has_fields():
    """blob 已含字段 + extras 同值：重复 merge 不双写不冲突，与单次一致。"""
    msgs = _mk_msgs(6)
    extras = {
        1: {"reasoning_content": msgs[1]["reasoning_content"], "diff": msgs[1]["diff"], "arguments": msgs[1]["arguments"]},
        3: {"reasoning_content": msgs[3]["reasoning_content"]},
        5: {"reasoning_content": "blob 里没有的行（idx 5 不存在字段场景）"},
    }
    once = merge_extras_into([dict(m) for m in msgs], extras)
    twice = merge_extras_into([dict(m) for m in once], extras)
    assert once == twice, "重复 merge 结果必须与单次一致（幂等）"
    assert once[1]["reasoning_content"] == msgs[1]["reasoning_content"]
    assert once[1]["diff"] == msgs[1]["diff"]
    assert once[3]["reasoning_content"] == msgs[3]["reasoning_content"]
    # merge 不新增键（无双写）：extras 提供的键集合 == 消息上的键集合
    for i, patch in extras.items():
        assert set(once[i].keys()) == set(msgs[i].keys()) | set(patch.keys())


def test_merge_extras_blob_field_wins_when_equal():
    """blob 值与 extras 值相同（窗外未变判定保证）：merge 后取值不变（同值覆盖无感）。"""
    msgs = _mk_msgs(4)
    blob_has = [dict(m) for m in msgs]
    extras = {0: {}, 1: {"reasoning_content": blob_has[1]["reasoning_content"], "arguments": blob_has[1]["arguments"]}}
    merged = merge_extras_into(blob_has, extras)
    assert merged[1]["reasoning_content"] == msgs[1]["reasoning_content"]
    assert merged[1]["arguments"] == msgs[1]["arguments"]
    assert "diff" not in merged[1] or merged[1]["diff"] == msgs[1]["diff"]


# ── 附加验证 3：finish 拍恢复剥离 + α 流式拍行为 ────────────────────


def test_streaming_tick_stores_full_blob_and_finish_restores_offload(store):
    """α 主行为：流式拍（_partial 尾条）blob 全量 + extras 表不写；落定拍恢复剥离。

    崩溃窗口验证：流式拍后直接读库（不调落定 save），窗外字段从 blob 完整恢复
    （结构性保证，不依赖 extras 表）。
    """
    repo = store._session_repo
    sid = "s-alpha"
    # 落定拍：建基线（剥离生效）
    msgs = _mk_msgs(20)
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] > 0, "前置：落定拍应有 extras 行"
    # 流式拍：尾条 _partial（内容变化仅尾条，窗外不变）→ blob 全量、extras 表不动
    before_rows = rows[0]["n"]
    streaming = msgs + [{"role": "assistant", "content": "半截回复…", "_partial": True, "timestamp": "t"}]
    repo.save(_make_session(sid, streaming))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] == before_rows, "流式拍不应写 extras（跳过 write_extras）"
    # 崩溃模拟：流式拍后直接读库，窗外字段从 blob 完整恢复（零丢失）
    got = repo.get(sid)["messages"]
    assert got[1].get("reasoning_content") == msgs[1]["reasoning_content"], "流式拍 blob 应自含窗外字段"
    # 落定拍：无 _partial → 恢复剥离（extras 表与轻量 blob 回到稳态）
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] > 0, "落定拍应恢复 extras 剥离"
    light = repo.get(sid)["messages"]
    assert "reasoning_content" not in light[1], "落定拍 blob 应回到轻量形态"
    _roundtrip_ok(repo, sid, msgs)


def test_streaming_tick_new_messages_within_window_skips_offload(store):
    """流式拍新增消息仍在保活窗内（len 增 ≤ KEEP_RECENT_ROUNDS）→ 同样跳过。"""
    repo = store._session_repo
    sid = "s-win"
    msgs = _mk_msgs(20)
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    before = rows[0]["n"]
    # 追加 3 条（窗宽 3）+ partial 尾条：新增全部在窗内，窗外不变
    add = [
        {"role": "user", "content": "新问题", "timestamp": "t"},
        {"role": "assistant", "content": "新回答", "reasoning_content": "新思考", "timestamp": "t"},
        {"role": "assistant", "content": "半截…", "_partial": True, "timestamp": "t"},
    ]
    repo.save(_make_session(sid, msgs + add))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] == before, "窗内新增的流式拍不应写 extras"
    # 落定恢复
    repo.save(_make_session(sid, msgs + add[:2]))
    _roundtrip_ok(repo, sid, msgs + add[:2])


def test_streaming_tick_beyond_window_or_shrink_runs_full_chain(store):
    """超窗/截断的流式拍不跳过（走全链）：extras 变化必须落库。"""
    repo = store._session_repo
    sid = "s-guard"
    msgs = _mk_msgs(20)
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    before = rows[0]["n"]
    # 超窗：追加 5 条 + partial 尾条（len 增 6 > 窗宽 3 → 有消息滑出窗）
    add = _mk_msgs(5) + [{"role": "assistant", "content": "半截…", "_partial": True, "timestamp": "t"}]
    repo.save(_make_session(sid, msgs + add))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] != before, "超窗流式拍必须走全链落 extras"
    # 截断：流式尾条 + 消息变短（超窗守卫 → 全链落 extras）
    short = msgs[:10] + [{"role": "assistant", "content": "半截…", "_partial": True, "timestamp": "t"}]
    repo.save(_make_session(sid, short))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    # n=11，keep_from=8 → 窗外 idx 0..7，带字段的是 assistant idx 1,3,5,7 → 12 行
    assert rows[0]["n"] == 12, f"截断流式拍应走全链落 extras（预期 12 行，实得 {rows[0]['n']}）"
    got = repo.get_full_messages(sid)
    core = short[:10]
    for i, m in enumerate(got[:10]):
        for f in OFFLOAD_KEYS:
            assert m.get(f, None) == core[i].get(f, None), f"截断后 msg[{i}] 字段串扰"
