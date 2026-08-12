# -*- coding: utf-8 -*-
"""plugin-marketplace 下载/更新记录存储测试

覆盖场景：
- 记录追加（最新在前）
- 失败记录保存 meta（供一键重试原动作）
- 超过上限（100 条）丢弃最旧
- 持久化（重建实例后数据仍在）
- 损坏文件容错（回退空列表）
"""

import json
import sys
import time
from pathlib import Path

# 让 pytest 能直接 import 插件源码
ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _store(file: Path):
    from ui.records import MarketplaceRecords

    return MarketplaceRecords(file=file)


def test_add_record_latest_first(tmp_path):
    """记录最新在前（insert(0)）"""
    s = _store(tmp_path / "records.json")
    s.add("install", "plugin-a", True)
    time.sleep(0.01)
    s.add("update", "plugin-b", False, error="git clone failed", meta={"name": "plugin-b"})

    records = s.get()
    assert len(records) == 2
    assert records[0]["name"] == "plugin-b"
    assert records[0]["action"] == "update"
    assert records[0]["success"] is False
    assert records[0]["error"] == "git clone failed"
    assert records[0]["meta"] == {"name": "plugin-b"}
    assert records[1]["name"] == "plugin-a"
    assert records[1]["success"] is True


def test_record_meta_kept_for_retry(tmp_path):
    """失败记录必须保留完整 plugin_meta（一键重试原动作依赖）"""
    s = _store(tmp_path / "records.json")
    meta = {
        "name": "demo-plugin",
        "source": {"source": "github", "repo": "owner/repo", "ref": "main"},
        "version": "1.0.0",
        "_marketplace_source": {"source": "url", "url": "https://example.com/marketplace.json"},
    }
    s.add("install", "demo-plugin", False, error="exit=128", meta=meta)

    rec = s.get()[0]
    assert rec["meta"] == meta


def test_max_records_cap(tmp_path):
    """超过 MAX(100) 条丢弃最旧，保持最新在前"""
    from ui.records import MarketplaceRecords

    s = _store(tmp_path / "records.json")
    for i in range(MarketplaceRecords.MAX + 20):
        s.add("install", f"plugin-{i}", True)

    records = s.get()
    assert len(records) == MarketplaceRecords.MAX
    # 最新（后写入）在前
    assert records[0]["name"] == f"plugin-{MarketplaceRecords.MAX + 19}"
    assert records[-1]["name"] == f"plugin-{20}"


def test_persist_across_instances(tmp_path):
    """记录写入文件后，重建实例仍能读取（跨会话可见）"""
    file = tmp_path / "records.json"
    s = _store(file)
    s.add("update", "persist-plugin", True)

    s2 = _store(file)
    records = s2.get()
    assert len(records) == 1
    assert records[0]["name"] == "persist-plugin"
    assert records[0]["success"] is True


def test_corrupted_file_falls_back_to_empty(tmp_path):
    """损坏的 records.json 应回退空列表而非崩溃"""
    file = tmp_path / "records.json"
    file.write_text("{ not valid json !!!", encoding="utf-8")

    s = _store(file)
    assert s.get() == []
    # 追加新记录不应抛异常（覆盖 _load 容错后写入路径）
    s.add("install", "after-corrupt", True)
    assert s.get()[0]["name"] == "after-corrupt"


def test_clear_records(tmp_path):
    """清空后 get 为空"""
    s = _store(tmp_path / "records.json")
    s.add("install", "x", True)
    s.clear()
    assert s.get() == []
    # 清空后文件应为空数组（不是残留旧数据）
    assert json.loads((tmp_path / "records.json").read_text(encoding="utf-8")) == []
