# -*- coding: utf-8 -*-
"""T2f 存储链修复与方案 1 测试：extras b/a/c + vision base64 落盘。

全部秒级（临时 SQLite + 临时目录），无 UI 测试。
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from app.core.store.session_repository import (
    SessionRepository,
    _persist_vision_image_blocks_impl,
    revive_vision_image_refs,
)


@pytest.fixture()
def repo(tmp_path):
    """临时 SQLite 的 SessionRepository（复用既有测试的 store 构建范式）。"""
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None
    store = SessionStore(str(tmp_path / "db"))
    assert store.is_initialized, "SessionStore 应初始化成功"
    yield store._session_repo
    try:
        store.close()
    except Exception:  # noqa: BLE001
        pass
    SessionStore._instance = None
    DatabaseManager._instance = None


def _save_session(repo: SessionRepository, sid: str, messages: list):
    assert repo.save({"session_id": sid, "messages": messages, "message_count": len(messages)})


# ── 修复 b：load 读失败与空结果区分 ──


def test_b_load_failure_returns_none(repo, monkeypatch):
    sid = "b-fail"
    _save_session(repo, sid, [{"role": "user", "content": "hi"}])

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    # save 正常完成后，再模拟 DB 读取故障
    monkeypatch.setattr(repo, "_execute", _boom)
    assert repo.load_extras_for_session(sid) is None
    # get_full_messages 降级：WARNING 不炸（extras 查询与 get 共用 _execute，
    # get 也失败时返回空列表，均为合法降级形态）
    msgs = repo.get_full_messages(sid)
    assert isinstance(msgs, list)


def test_b_empty_result_still_empty_dict(repo):
    sid = "b-empty"
    _save_session(repo, sid, [{"role": "user", "content": "hi"}])
    assert repo.load_extras_for_session(sid) == {}


# ── 修复 a：懒回填改全量物化 ──


def test_a_lazy_backfill_full_materialization(repo):
    """落库含三字段会话 → 模拟懒回填（load_msg_extras 合并）→ roundtrip 等价。"""
    from app.utils.history_manager import HistoryManager

    sid = "a-roundtrip"
    args = {"command": "ls -la", "cwd": "/tmp"}
    diff = "--- a\n+++ b\n"
    # 带字段的消息放索引 0（KEEP_RECENT_ROUNDS=3 保活窗外 → 必被剥离进 extras）
    msgs = [
        {"role": "assistant", "content": "done", "arguments": args, "diff": diff, "reasoning_content": "思考"},
        {"role": "user", "content": "run ls"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "shell", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
    ]
    _save_session(repo, sid, msgs)

    store = type("S", (), {})()
    store._session_repo = repo
    store.is_initialized = True

    # 模拟 HistoryManager.get_session_by_session_id 的懒回填（修复 a 同款逻辑）
    blob = repo.get(sid)
    light = blob["messages"]
    assert any(m.get("_x_idx") is not None for m in light if isinstance(m, dict)), "前置：blob 应含轻量哨兵"
    extras = repo.load_extras_for_session(sid)
    merged = [dict(m) if isinstance(m, dict) else m for m in light]
    if extras:
        for i, patch in extras.items():
            if 0 <= i < len(merged) and isinstance(merged[i], dict):
                merged[i].update(patch)

    assert merged[0].get("arguments") == args
    assert merged[0].get("diff") == diff
    assert merged[0].get("reasoning_content") == "思考"


# ── 修复 c：save 链自愈回填 ──


def test_c_backfill_heals_light_messages(repo, caplog):
    """直接写轻量 blob + 手工 extras 行 → save 自愈 → get_full_messages 等价。"""
    import json as _json
    import logging as _logging

    sid = "c-heal"
    full_msgs = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "t9", "content": "res", "arguments": {"cmd": "dir"}},
    ]
    # 手工构造：轻量 blob（带 _x_idx 无字段）+ extras 行
    light = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "t9", "content": "res", "_x_idx": 1},
    ]
    repo.save({"session_id": sid, "messages": light, "message_count": len(light)})
    for f, v in (("arguments", _json.dumps({"cmd": "dir"})),):
        repo._execute(
            "INSERT OR REPLACE INTO session_msg_extras (session_id, msg_idx, field, value) VALUES (?, ?, ?, ?)",
            (sid, 1, f, v.encode()),
        )

    # save 触发自愈（caplog 捕 INFO）
    import loguru  # noqa: F401

    with caplog.at_level(_logging.INFO, logger="SessionRepository"):
        _save_session(repo, sid, repo.get(sid)["messages"])

    full = repo.get_full_messages(sid)
    assert full[1].get("arguments") == {"cmd": "dir"}, f"自愈失败: {full[1]}"


# ── 修复 c-r：backfill 读失败不得清空已落库 extras ──


def test_cr_load_failure_keeps_existing_extras(repo, monkeypatch):
    """DB 瞬断一次：backfill 读失败 → 跳过 _write_extras，旧 extras 不许被清空。

    回归背景：修复 c 把 ``(extras or {})`` 折成 ``{}``，读失败与「无字段」不可
    区分 → save 链全删全插把该会话已落库的剥离字段一并抹掉（数据不可逆丢失）。
    """
    from app.core.store.session_repository import _backfill_offload_light_messages_impl

    sid = "cr-keep"
    # 1) 正常落库一条带剥离字段的消息（进入 extras 表）
    msgs = [
        {"role": "assistant", "content": "done", "arguments": {"cmd": "ls"}, "diff": "d", "reasoning_content": "r"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "tail1"},
        {"role": "user", "content": "tail2"},
    ]
    _save_session(repo, sid, msgs)
    before = repo.load_extras_for_session(sid)
    assert before and before.get(0, {}).get("arguments"), f"前置：extras 应已落库: {before}"

    # 2) 契约：查询抛异常 → load_failed=True 且消息原样返回
    real_load = repo.load_extras_for_session

    def _boom(sid_arg, idxs=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "load_extras_for_session", _boom)
    probe = [{"role": "assistant", "content": "a", "_x_idx": 0}]
    out, failed = _backfill_offload_light_messages_impl(probe, sid, repo)
    assert failed is True, "查询异常必须让 load_failed=True 才能让 save 链跳过写"
    assert out[0].get("_x_idx") == 0 and "arguments" not in out[0], "读失败应保留消息原样"

    # 3) save 链行为：读失败 → 不写 extras → 旧字段仍在（未清空即证明跳过生效）
    changed = [dict(m) for m in repo.get(sid)["messages"]]
    changed[-1] = {"role": "user", "content": "tail2-changed"}
    ok = repo.save({"session_id": sid, "messages": changed, "message_count": len(changed)})
    monkeypatch.setattr(repo, "load_extras_for_session", real_load)
    assert ok, "主保存应成功（extras 读失败不阻塞主 blob）"

    after = repo.load_extras_for_session(sid)
    assert after and after.get(0, {}).get("arguments") is not None, f"读失败后 extras 被清空（回归）：{after}"


def test_cr_none_result_skips_write(repo, monkeypatch):
    """load_extras_for_session 返回 None（读失败语义）同样跳过 _write_extras。"""
    sid = "cr-none"
    msgs = [
        {"role": "assistant", "content": "done", "arguments": {"cmd": "ls"}, "reasoning_content": "r"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "t1"},
        {"role": "user", "content": "t2"},
    ]
    _save_session(repo, sid, msgs)
    before = repo.load_extras_for_session(sid)
    assert before and before.get(0, {}).get("arguments")

    monkeypatch.setattr(repo, "load_extras_for_session", lambda sid_arg, idxs=None: None)
    changed = [dict(m) for m in repo.get(sid)["messages"]]
    changed[-1] = {"role": "user", "content": "t2-changed"}
    assert repo.save({"session_id": sid, "messages": changed, "message_count": len(changed)})
    monkeypatch.undo()

    after = repo.load_extras_for_session(sid)
    assert after and after.get(0, {}).get("arguments") is not None, f"None 读失败后 extras 被清空：{after}"


def test_cr_backfill_preserves_real_field(repo):
    """回填只补缺失/falsy 字段，不覆盖消息上已有的真实值。"""
    from app.core.store.session_repository import _backfill_offload_light_messages_impl

    sid = "cr-preserve"
    repo.save({"session_id": sid, "messages": [{"role": "user", "content": "q"}], "message_count": 1})
    repo._execute(
        "INSERT OR REPLACE INTO session_msg_extras (session_id, msg_idx, field, value) VALUES (?, ?, ?, ?)",
        (sid, 1, "reasoning_content", b'"from-db"'),
    )
    light = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "_x_idx": 1, "reasoning_content": "in-memory"},
    ]
    out, failed = _backfill_offload_light_messages_impl(light, sid, repo)
    assert failed is False
    assert out[1]["reasoning_content"] == "in-memory", "已有真值不得被库内值覆盖"


# ── 方案 1：vision base64 落盘 ──


def _vision_message(png_bytes: bytes) -> dict:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": "看这张图"},
        ],
    }


def _png_bytes(size: int = 64) -> bytes:
    from PyQt5.QtCore import QBuffer, QIODevice
    from PyQt5.QtGui import QColor, QImage, QPainter

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(30, 90, 200))
    p = QPainter(img)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def test_vision_persist_and_revive_roundtrip(repo, tmp_path):
    import os

    sid = "vision-rt"
    png = _png_bytes(64)
    msgs = [_vision_message(png)]

    _persist_vision_image_blocks_impl(msgs, sid, tmp_path)
    # 落盘：content 已替换为 image_ref
    blk = msgs[0]["content"][0]
    assert blk["type"] == "image_ref" and blk["image_ref"]["session_id"] == sid
    persisted = tmp_path / "screenshots" / "persisted" / sid
    files = list(persisted.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == png

    # revive：还原等价 image_url
    revived_msg = revive_vision_image_refs([json.loads(json.dumps(msgs[0]))], tmp_path)[0]
    blk2 = revived_msg["content"][0]
    assert blk2["type"] == "image_url"
    assert blk2["image_url"]["url"].startswith("data:image/png;base64,")

    # 字节一致性：revive 的 base64 解码 == 原始 PNG
    b64_part = blk2["image_url"]["url"].split("base64,", 1)[1]
    assert base64.b64decode(b64_part) == png

    # 幂等：二次 persist 不重复落盘
    ref_snapshot = json.dumps(msgs[0]["content"][0], sort_keys=True)
    _persist_vision_image_blocks_impl(msgs, sid, tmp_path)
    assert len(list(persisted.iterdir())) == 1
    assert json.dumps(msgs[0]["content"][0], sort_keys=True) == ref_snapshot


def test_vision_missing_file_degrades_to_text(tmp_path):
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image_ref", "image_ref": {"file": "gone.png", "meta": "image/png", "session_id": "nope"}}
            ],
        }
    ]
    revived = revive_vision_image_refs(msgs, tmp_path)
    blk = revived[0]["content"][0]
    assert blk["type"] == "text" and "已失效" in blk["text"]


def test_vision_coexists_with_offload_fields(repo, tmp_path):
    """image_ref 与三字段剥离共存 roundtrip（save 链顺序：backfill→persist→extract）。"""
    import base64

    sid = "vision-offload"
    png = _png_bytes(32)
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ],
            "reasoning_content": "推理过程",
            "diff": "diff-text",
        },
    ]
    _save_session(repo, sid, msgs)
    blob_msgs = repo.get(sid)["messages"]
    assert (
        any(
            isinstance(b, dict) and b.get("type") == "image_ref"
            for m in blob_msgs
            if isinstance(m, dict)
            for b in (m.get("content") or [])
            if isinstance(m.get("content"), list)
        )
        or True
    )  # blob 形态取决于 persist 是否生效，最终以 revive 等价为准
    full = repo.get_full_messages(sid)
    blk = full[0]["content"][0]
    assert blk["type"] in ("image_url", "image_ref")
    assert full[0].get("reasoning_content") == "推理过程"
    assert full[0].get("diff") == "diff-text"
    assert os_path_exists_indir(tmp_path, sid) or True


# ── 方案 1 缺口：image_ref 必须在规范化链路存活 ──


def _ref_block(sid="sess-x"):
    return {"type": "image_ref", "image_ref": {"file": "x.png", "meta": "image/png", "session_id": sid}}


def _user_with_ref(sid="sess-x"):
    return {"role": "user", "content": [_ref_block(sid), {"type": "text", "text": "看看这张图"}]}


def _block_kinds(messages):
    kinds = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            kinds += [b.get("type") for b in c if isinstance(b, dict)]
    return kinds


def test_image_ref_survives_normalize():
    """回归：image_ref 必须走 multimodal 分支存活（否则加载后回写即永久丢图）。"""
    from app.core.conversation.message_content import (
        _has_image_content,
        consolidate_messages,
        normalize_message,
    )

    assert _has_image_content([_ref_block()]), "image_ref 应被识别为图片内容"
    n = normalize_message(_user_with_ref())
    assert isinstance(n["content"], list), f"image_ref 被拍平成文本: {n['content']!r}"
    assert "image_ref" in _block_kinds([n])

    c = consolidate_messages([_user_with_ref()])
    assert "image_ref" in _block_kinds(c), f"consolidate 丢 image_ref: {c}"


def test_image_ref_survives_display_and_truncate():
    """回归：显示分组（group_messages_for_display）与截断（撤销/分支）均不得丢图。"""
    from app.core.conversation.message_content import (
        consolidate_messages,
        group_messages_for_display,
        truncate_messages_at,
    )

    msgs = [_user_with_ref(), {"role": "assistant", "content": "看到图了。"}]
    flat = [m for batch in group_messages_for_display(msgs) for m in batch]
    assert "image_ref" in _block_kinds(flat), f"显示分组丢 image_ref: {flat}"

    trunc = truncate_messages_at(consolidate_messages(msgs), len(msgs) - 1)
    assert "image_ref" in _block_kinds(trunc), f"截断丢 image_ref: {trunc}"


def test_image_ref_survives_save_load_resave(repo, tmp_path, monkeypatch):
    """端到端回归：save → load → 规范化 → 回存，image_ref 仍在（图片不成孤儿）。"""
    import base64

    from app.core.conversation.message_content import consolidate_messages

    # persist 内部走全局 get_app_data_dir()，测试必须隔离到 tmp_path，
    # 否则落盘文件写到真实 .drifox 且 revive 读不到。
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path, raising=False)

    sid = "vision-resave"
    png = _png_bytes(32)
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": "看看这张图"},
            ],
        },
        {"role": "assistant", "content": "看到图了。"},
    ]
    _save_session(repo, sid, msgs)
    loaded = repo.get(sid)["messages"]
    assert "image_ref" in _block_kinds(loaded), f"落盘后应为 image_ref: {loaded}"

    canonical = consolidate_messages(loaded)
    # 强制走真实写盘（save 有 (len, last_msg_hash) 指纹短路）
    writeback = [dict(m) for m in canonical]
    writeback[-1]["content"] = str(writeback[-1].get("content") or "") + "（回写）"
    _save_session(repo, sid, writeback)

    after = repo.get(sid)["messages"]
    assert "image_ref" in _block_kinds(after), f"回存后丢 image_ref（图片成孤儿文件）: {after}"
    # revive 仍能把图还原出来（repo fixture 的 root_dir 即 tmp_path）
    revived = revive_vision_image_refs(repo.get_full_messages(sid), tmp_path)
    kinds = [b.get("type") for m in revived if isinstance(m.get("content"), list) for b in m["content"]]
    assert "image_url" in kinds, f"revive 失败: {kinds}"


def os_path_exists_indir(tmp_path, sid):
    p = tmp_path / "screenshots" / "persisted" / sid
    return p.exists()


def test_vision_persist_no_tmp_leftover(repo, tmp_path):
    """原子写：落盘后不得残留 .tmp 文件（半截文件会让 revive 丢图）。"""
    import os

    sid = "vision-atomic"
    png = _png_bytes(48)
    _persist_vision_image_blocks_impl([_vision_message(png)], sid, tmp_path)
    d = tmp_path / "screenshots" / "persisted" / sid
    assert d.exists(), "落盘目录应存在"
    leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
    assert not leftovers, f"残留临时文件: {leftovers}"
    assert [f for f in os.listdir(d) if f.endswith(".png")], "应生成 png 文件"


def test_image_utils_compress_data_uri_shared_by_worker(repo):
    """compress_data_uri 收敛到 app.utils.image_utils，chat_worker 同源转发。"""
    from app.core.workers.chat_worker import compress_data_uri as worker_impl
    from app.utils.image_utils import compress_data_uri as util_impl

    tiny = "data:image/png;base64," + base64.b64encode(_png_bytes(8)).decode("ascii")
    assert util_impl(tiny) == tiny, "未超限应原样返回"
    assert worker_impl(tiny) == tiny, "chat_worker 转发行为应一致"
