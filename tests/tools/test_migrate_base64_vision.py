# -*- coding: utf-8 -*-
"""test_migrate_base64_vision.py — 存量 base64→CAS 迁移器测试。

覆盖：dry-run 零副作用、迁移改写与 CAS 内容一致、幂等、备份失败中止、
单会话失败只回滚该会话、真实库安全闸。
"""

import argparse
import base64
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools" / "perf" / "migrate_base64_vision.py"

_spec = importlib.util.spec_from_file_location("_migrate_base64_vision", _TOOL)
mig = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("_migrate_base64_vision", mig)
_spec.loader.exec_module(mig)

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 64


def _messages(url: str):
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]


def _plain_messages():
    return [{"role": "assistant", "content": "hello"}]


def _make_db(path: Path, n_img: int = 1, n_plain: int = 1) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, messages BLOB)")
    for i in range(n_img):
        raw = PNG + bytes([i])
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        con.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (f"img{i:04d}", mig.serialize(_messages(url))),
        )
    for i in range(n_plain):
        con.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (f"plain{i:04d}", mig.serialize(_plain_messages())),
        )
    con.commit()
    con.close()


def _ns(db: Path, cas: Path, **kw):
    base = dict(
        db=str(db),
        cas_dir=str(cas),
        dry_run=False,
        limit=None,
        batch_size=20,
        backup_to=None,
        vacuum=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _blobs(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    try:
        return dict(con.execute("SELECT session_id, messages FROM sessions").fetchall())
    finally:
        con.close()


def test_dry_run_counts_without_side_effects(tmp_path):
    db = tmp_path / "sessions.db"
    _make_db(db)
    before = _sha256_file(db)

    rep = mig.migrate(_ns(db, tmp_path / "cas", dry_run=True))

    assert rep["sessions_scanned"] == 2
    assert rep["sessions_with_images"] == 1
    assert rep["blocks_replaced"] == 1
    assert rep["b64_bytes"] > 0
    assert rep["unique_images"] == 1
    assert _sha256_file(db) == before
    assert not (tmp_path / "cas").exists()


def test_migrate_rewrites_and_cas_content_matches(tmp_path):
    db = tmp_path / "sessions.db"
    cas = tmp_path / "cas"
    _make_db(db)

    rep = mig.migrate(_ns(db, cas, backup_to=str(tmp_path / "bak.db")))

    assert rep["sessions_migrated"] == 1
    assert rep["failed_sessions"] == []
    assert Path(rep["backup_path"]).is_file()

    msgs = mig.deserialize(_blobs(db)["img0000"])
    block = msgs[0]["content"][0]
    assert block["type"] == "image_ref"
    ref = block["image_ref"]
    assert ref["session_id"] == "img0000"

    fpath = cas / "screenshots" / "persisted" / "img0000" / ref["file"]
    assert fpath.is_file()
    expect = hashlib.sha256(PNG + b"\x00").hexdigest()
    assert hashlib.sha256(fpath.read_bytes()).hexdigest() == expect

    revived = mig.revive_vision_image_refs(msgs, root_dir=cas)
    url = revived[0]["content"][0]["image_url"]["url"]
    got = hashlib.sha256(base64.b64decode(url.split(",", 1)[1])).hexdigest()
    assert got == expect


def test_second_run_is_idempotent(tmp_path):
    db = tmp_path / "sessions.db"
    cas = tmp_path / "cas"
    _make_db(db)
    mig.migrate(_ns(db, cas, backup_to=str(tmp_path / "bak1.db")))

    rep2 = mig.migrate(_ns(db, cas, backup_to=str(tmp_path / "bak2.db")))

    assert rep2["sessions_migrated"] == 0
    assert rep2["blocks_replaced"] == 0
    assert rep2["sessions_with_images"] == 0
    assert rep2["sessions_already_ref"] == 1


def test_backup_failure_aborts_without_modifying_db(tmp_path):
    db = tmp_path / "sessions.db"
    _make_db(db)
    before = _sha256_file(db)

    with pytest.raises(SystemExit) as ei:
        mig.migrate(
            _ns(db, tmp_path / "cas", backup_to=str(tmp_path / "no_dir" / "bak.db"))
        )

    assert "备份失败" in str(ei.value)
    assert _sha256_file(db) == before


def test_single_session_failure_rolls_back_only_that_session(tmp_path):
    db = tmp_path / "sessions.db"
    cas = tmp_path / "cas"
    _make_db(db, n_img=2, n_plain=0)
    before = _blobs(db)

    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute(
        "CREATE TRIGGER fail_one BEFORE UPDATE ON sessions "
        "WHEN OLD.session_id = 'img0000' "
        "BEGIN SELECT RAISE(ABORT, 'boom-for-test'); END"
    )
    con.close()

    rep = mig.migrate(_ns(db, cas, backup_to=str(tmp_path / "bak.db")))

    assert [f["session_id"] for f in rep["failed_sessions"]] == ["img0000"]
    assert rep["sessions_migrated"] == 1
    after = _blobs(db)
    assert after["img0000"] == before["img0000"]  # 失败会话原样回滚
    assert after["img0001"] != before["img0001"]  # 其它会话正常迁移


def test_real_data_path_detection(tmp_path):
    assert mig.is_real_data_path(str(tmp_path / ".drifox" / "sessions.db")) is True
    assert mig.is_real_data_path(str(tmp_path / "copy" / "sessions.db")) is False
