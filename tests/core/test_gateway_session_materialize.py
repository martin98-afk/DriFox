# -*- coding: utf-8 -*-
"""[T3] 网关读侧副本化 + 旧 blob 防御迁移回归。

覆盖：
1. _materialize_session_messages 三层浅拷边界（record/消息/块独立，非 dict 原样）
2. get_session_by_session_id / get_session_by_index：返回副本含 data URI，
   常驻 record 未被修改（仍 image_ref 形态）
3. get_full_messages 旧 blob 防御迁移：后台线程落盘图 + DB 回写 ref，
   读路径原样返回（内存侧不换 ref）

无 QApplication 依赖（__new__ 桩 + monkeypatch get_app_data_dir）。
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from app.core.store.serde import deserialize, serialize
from app.core.store.session_repository import (
    SessionRepository,
    _persist_vision_image_blocks_impl,
)
from app.core.local_service.session_handler import APIHistoryManager

SESSION_ID = "t_materialize"


def _png_data_uri() -> str:
    """确定性 1x1 PNG（同 test_vision_revive_revoke，不依赖 Qt）。"""
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408d763f8cfc00000030101"
        "00184dfc900000000049454e44ae426082"
    )
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"


@pytest.fixture()
def vision_env(tmp_path, monkeypatch):
    """落盘根目录指向临时目录（迁移后台线程的延迟 import 同样命中 patch）。"""
    root = tmp_path / "appdata"
    root.mkdir()
    import app.utils.utils as uutils

    monkeypatch.setattr(uutils, "get_app_data_dir", lambda: str(root))
    yield root


@pytest.fixture()
def repo(tmp_path):
    """临时 SQLite 的 SessionRepository（复用 test_store_t2f 的构建范式）。"""
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


def _make_persisted_record(root) -> dict:
    """构造常驻真实形态：data URI 已被 persist 链换成 image_ref。"""
    record = {
        "session_id": SESSION_ID,
        "title": "t",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri()}},
                    {"type": "text", "text": "截图"},
                ],
            },
            {"role": "assistant", "content": "收到"},
        ],
    }
    record["messages"] = _persist_vision_image_blocks_impl(
        record["messages"], SESSION_ID, str(root)
    )
    assert record["messages"][0]["content"][0]["type"] == "image_ref"
    return record


def _first_block(record: dict) -> dict:
    return record["messages"][0]["content"][0]


def _stub_manager(record: dict) -> APIHistoryManager:
    mgr = APIHistoryManager.__new__(APIHistoryManager)
    mgr._session_store = None
    mgr._api_sessions = [record]
    return mgr


class TestMaterialize:
    def test_materialize_three_layer_copy(self):
        """record/消息/块三层独立；非 dict 消息与 str content 原样。"""
        record = {
            "session_id": "s",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "a"}, "raw"]},
                {"role": "assistant", "content": "plain str"},
                "legacy_non_dict_msg",
            ],
        }
        snap = json.dumps(record, ensure_ascii=False)
        out = APIHistoryManager._materialize_session_messages(record)
        # 源未被改
        assert json.dumps(record, ensure_ascii=False) == snap
        assert out is not record
        assert out["messages"] is not record["messages"]
        assert out["messages"][0] is not record["messages"][0]
        assert out["messages"][0]["content"][0] is not record["messages"][0]["content"][0]
        # 非 dict 原样
        assert out["messages"][0]["content"][1] == "raw"
        assert out["messages"][1]["content"] == "plain str"
        assert out["messages"][2] == "legacy_non_dict_msg"
        # slot 替换隔离：改副本块不影响源（revive 唯一写动作）
        out["messages"][0]["content"][0] = {"type": "image_url", "image_url": {"url": "x"}}
        assert record["messages"][0]["content"][0]["type"] == "text"

    def test_get_by_session_id_keeps_resident_ref(self, vision_env):
        """读全链：返回副本含 data URI，常驻 record 仍 ref 形态。"""
        record = _make_persisted_record(vision_env)
        mgr = _stub_manager(record)

        out = mgr.get_session_by_session_id(SESSION_ID)

        assert out is not record
        block = _first_block(out)
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")
        # 常驻未被 revive 污染
        assert _first_block(record)["type"] == "image_ref"
        assert "data:image" not in json.dumps(record["messages"], ensure_ascii=False)

    def test_get_by_index_keeps_resident_ref(self, vision_env):
        record = _make_persisted_record(vision_env)
        mgr = _stub_manager(record)

        msgs = mgr.get_session_by_index(0)

        block = msgs[0]["content"][0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")
        assert _first_block(record)["type"] == "image_ref"

    def test_get_by_index_missing_file_placeholders_copy_only(self, vision_env):
        """文件缺失 → 副本降级占位 text 块，常驻仍原样 ref。"""
        record = _make_persisted_record(vision_env)
        import shutil

        shutil.rmtree(vision_env / "screenshots" / "persisted" / SESSION_ID)
        mgr = _stub_manager(record)

        msgs = mgr.get_session_by_index(0)

        assert msgs[0]["content"][0]["type"] == "text"  # 占位降级只发生在副本
        assert _first_block(record)["type"] == "image_ref"


class TestLegacyBlobMigration:
    def test_full_messages_triggers_background_migration(self, repo, vision_env):
        """存量 data:image blob → 后台迁移：DB 变 ref + 文件落盘，读路径原样返回。"""
        sid = "legacy-mig"
        assert repo.save(
            {"session_id": sid, "messages": [{"role": "user", "content": "seed"}], "message_count": 1}
        )
        # 模拟存量：绕过 save 链 persist，直接把未迁移 blob 写进 messages 列
        legacy_msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri()}},
                    {"type": "text", "text": "截图"},
                ],
            },
            {"role": "assistant", "content": "收到"},
        ]
        blob = serialize(legacy_msgs)
        ok, _ = repo._execute("UPDATE sessions SET messages = ? WHERE session_id = ?", (blob, sid))
        assert ok

        # 读路径原样返回：内存侧不换 ref（UI 链由既有出口 revoke 兜底）
        out = repo.get_full_messages(sid)
        assert out[0]["content"][0]["type"] == "image_url"
        assert out[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")

        # 后台迁移收敛：DB blob 反序列化后首块变 image_ref + 图文件落盘（sha1 命名）
        deadline = time.time() + 10
        migrated = False
        while time.time() < deadline:
            ok, rows = repo._execute(
                "SELECT messages FROM sessions WHERE session_id = ?", (sid,)
            )
            raw = rows[0]["messages"] if hasattr(rows[0], "keys") else rows[0][0]
            work = deserialize(raw)
            if (
                isinstance(work, list)
                and work
                and isinstance(work[0].get("content", [None])[0], dict)
                and work[0]["content"][0].get("type") == "image_ref"
            ):
                migrated = True
                break
            time.sleep(0.05)
        assert migrated, "10s 内后台迁移未收敛"
        files = list((vision_env / "screenshots" / "persisted" / sid).glob("*.png"))
        assert len(files) == 1

    def test_clean_blob_no_thread_no_overhead(self, repo):
        """干净 ref blob：快筛不命中，DB 不被改写（零开销路径）。"""
        sid = "clean-blob"
        assert repo.save(
            {"session_id": sid, "messages": [{"role": "user", "content": "hi"}], "message_count": 1}
        )
        ok, rows_before = repo._execute(
            "SELECT messages FROM sessions WHERE session_id = ?", (sid,)
        )
        raw_before = rows_before[0]["messages"] if hasattr(rows_before[0], "keys") else rows_before[0][0]

        repo.get_full_messages(sid)
        time.sleep(0.2)  # 给误触发的后台线程留窗口

        ok, rows_after = repo._execute(
            "SELECT messages FROM sessions WHERE session_id = ?", (sid,)
        )
        raw_after = rows_after[0]["messages"] if hasattr(rows_after[0], "keys") else rows_after[0][0]
        assert raw_after == raw_before

    def test_migration_failure_keeps_blob_for_retry(self, repo, vision_env):
        """失败降级契约：UPDATE 抛异常 → blob 保留原样（不丢不半写），下次快筛仍触发重试。"""
        sid = "legacy-mig-fail"
        assert repo.save(
            {"session_id": sid, "messages": [{"role": "user", "content": "seed"}], "message_count": 1}
        )
        legacy_msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _png_data_uri()}},
                    {"type": "text", "text": "截图"},
                ],
            },
            {"role": "assistant", "content": "收到"},
        ]
        blob = serialize(legacy_msgs)
        ok, _ = repo._execute("UPDATE sessions SET messages = ? WHERE session_id = ?", (blob, sid))
        assert ok

        # 故障注入：仅 UPDATE 抛异常（SELECT 放行，读路径正常降级）
        real_execute = repo._execute
        state = {"boom": True}

        def _flaky_execute(sql, params=()):
            if state["boom"] and sql.strip().upper().startswith("UPDATE"):
                raise RuntimeError("disk gone")
            return real_execute(sql, params)

        repo._execute = _flaky_execute
        try:
            out = repo.get_full_messages(sid)
            # 读路径原样返回不受迁移故障影响
            assert out[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
            time.sleep(0.5)  # 给后台线程失败窗口
            ok, rows = real_execute("SELECT messages FROM sessions WHERE session_id = ?", (sid,))
            work = deserialize(rows[0]["messages"] if hasattr(rows[0], "keys") else rows[0][0])
            # blob 保留原样：未迁移、未半写
            assert work[0]["content"][0]["type"] == "image_url"
            assert work[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        finally:
            repo._execute = real_execute

        # 恢复后再次读：快筛再命中 → 后台迁移重试收敛
        repo.get_full_messages(sid)
        deadline = time.time() + 10
        migrated = False
        while time.time() < deadline:
            ok, rows = repo._execute("SELECT messages FROM sessions WHERE session_id = ?", (sid,))
            work = deserialize(rows[0]["messages"] if hasattr(rows[0], "keys") else rows[0][0])
            if (
                isinstance(work, list)
                and work
                and isinstance(work[0].get("content", [None])[0], dict)
                and work[0]["content"][0].get("type") == "image_ref"
            ):
                migrated = True
                break
            time.sleep(0.05)
        assert migrated, "重试后 10s 内迁移未收敛"
