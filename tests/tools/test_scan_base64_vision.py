# -*- coding: utf-8 -*-
"""test_scan_base64_vision.py — 全库扫描器回归。

核心回归：serde BLOB 带 5 字节 `ZSTD\\x01` 头，旧实现直接交给 zstd 解压，
在有图库上永远报 0。同时锁定「全文本口径」与「块级口径（vision_blocks）」
的区别，以及块级口径与 migrate_base64_vision --dry-run 的数字一致性。
"""

import argparse
import base64
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


scan = _load("_scan_base64_vision_mod", "tools/perf/scan_base64_vision.py")
mig = _load("_migrate_base64_vision_scan_test", "tools/perf/migrate_base64_vision.py")
common = _load("_vision_scan_common_test", "tools/perf/vision_scan_common.py")

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 64
B64 = base64.b64encode(PNG).decode()
URL = "data:image/png;base64," + B64


def _make_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, messages BLOB)")
    vision = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": URL}}]}]
    # 非块级副本：HTML <img src>，全文本口径能发现，块级口径不算
    html = [{"role": "tool", "content": f'<img src="{URL}">'}]
    con.execute("INSERT INTO sessions VALUES (?, ?)", ("s-vision", mig.serialize(vision)))
    con.execute("INSERT INTO sessions VALUES (?, ?)", ("s-html", mig.serialize(html)))
    con.commit()
    con.close()


def _dry_run_args(db: Path, tmp_path: Path):
    return argparse.Namespace(
        db=str(db),
        cas_dir=str(tmp_path / "cas"),
        dry_run=True,
        limit=None,
        batch_size=20,
        backup_to=None,
        vacuum=False,
    )


def test_serde_blob_is_decoded(tmp_path):
    """serde 头必须被正确剥掉：旧实现此处报 0。"""
    db = tmp_path / "sessions.db"
    _make_db(db)

    rep = scan.scan(str(db))

    msg = rep["tables"]["sessions"]["messages"]
    assert msg["rows"] == 2
    assert msg["blocks"] == 2
    assert msg["b64_bytes"] == 2 * len(B64)
    assert msg["unique_images"] == 1  # 同一张图两处出现
    assert msg["sessions"] == 2


def test_block_level_matches_migrate_dry_run(tmp_path):
    """vision_blocks 四项数字必须与迁移器 dry-run 完全一致。"""
    db = tmp_path / "sessions.db"
    _make_db(db)

    rep = scan.scan(str(db))
    vb = rep["tables"]["sessions"]["messages"]["vision_blocks"]
    mrep = mig.migrate(_dry_run_args(db, tmp_path))

    assert vb["sessions"] == 1
    assert vb["blocks"] == 1
    assert vb["b64_bytes"] == len(B64)
    assert vb["unique_images"] == 1
    assert (
        vb["sessions"],
        vb["blocks"],
        vb["b64_bytes"],
        vb["unique_images"],
    ) == (
        mrep["sessions_with_images"],
        mrep["blocks_replaced"],
        mrep["b64_bytes"],
        mrep["unique_images"],
    )


def test_load_blob_handles_unheaded_json_and_plain_zstd_frame(tmp_path):
    """无头旧 JSON 与裸 zstd 帧都要能解析（格式 C / 历史数据兜底）。"""
    msgs = [{"role": "user", "content": "hi"}]
    raw_json = json.dumps(msgs).encode("utf-8")
    assert common.load_blob(raw_json) == msgs

    from compression import zstd

    compressor = zstd.ZstdCompressor()
    compressor.set_pledged_input_size(len(raw_json))
    frame = compressor.compress(raw_json) + compressor.flush()
    assert common.load_blob(frame) == msgs
