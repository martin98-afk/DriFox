# -*- coding: utf-8 -*-
"""P1-2：配置资源上限（manifest 256KB / fields 50 / default 8KB / icon 512 / pip 20）。

五用例：10MB manifest 拒载+报错 / 100 fields 截断 50+warning / 超长 default 丢弃 /
超长 icon 丢弃 + pip 截断 / 正常清单零影响。
全部 tmp_path。
"""
import json
from pathlib import Path

import pytest
from loguru import logger

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.managers.plugin_manager import (
    PluginManager,
    _enforce_manifest_limits,
    _load_manifest_file,
)


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _make_plugin(root: Path, name: str, manifest: dict):
    d = root / name / ".drifox-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root / name


def test_oversized_manifest_rejected(tmp_path, log_capture):
    """10MB manifest → 拒载（_load_manifest_file 返回 None）+ 明确报错。"""
    d = tmp_path / "fat-plug" / ".drifox-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("x" * (10 * 1024 * 1024), encoding="utf-8")
    assert _load_manifest_file(d / "plugin.json") is None
    # 集成：扫描层不发现该插件
    pm = PluginManager.__new__(PluginManager)
    assert pm._scan_plugins(tmp_path, "user") == []
    assert any("256KB" in r and "拒载" in r for r in log_capture)


def test_fields_over_50_truncated(tmp_path, log_capture):
    """100 个合法字段 → 截断到 50 + warning，不拒载。"""
    raw = {"title": "t", "fields": [{"key": f"f{i}", "type": "text"} for i in range(100)]}
    schema = parse_config_schema("limit-plug", raw)
    assert schema is not None
    assert len(schema.fields) == 50
    assert any("超过上限 50" in r and "已截断" in r for r in log_capture)


def test_oversized_default_dropped(tmp_path, log_capture):
    """default 序列化后超 8KB → 丢弃回退类型默认 + warning。"""
    raw = {"fields": [{"key": "big", "type": "text", "default": "x" * 9000}]}
    schema = parse_config_schema("default-plug", raw)
    assert schema is not None
    assert schema.fields[0].default == ""
    assert any("default" in r and "8KB" in r for r in log_capture)


def test_icon_and_pip_limits(tmp_path, log_capture):
    """manifest icon 字段 >512 字符丢弃；pip 声明 >20 条截断到 20。"""
    manifest = {"name": "cap-plug", "icon": "i" * 600, "pip": [f"pkg{i}" for i in range(25)]}
    cleaned = _enforce_manifest_limits(manifest, tmp_path / "plugin.json")
    assert "icon" not in cleaned
    assert len(cleaned["pip"]) == 20
    assert any("icon 字段" in r and "512" in r for r in log_capture)
    assert any("pip 声明 25 条" in r for r in log_capture)


def test_normal_manifest_untouched(tmp_path, log_capture):
    """正常清单/配置零影响：字段完整、无任何上限告警。"""
    manifest = {
        "name": "ok-plug",
        "icon": {"light": "a.svg", "dark": "b.svg"},
        "pip": ["requests", "httpx"],
    }
    cleaned = _enforce_manifest_limits(dict(manifest), tmp_path / "p.json")
    assert cleaned["icon"] == manifest["icon"] and cleaned["pip"] == manifest["pip"]
    raw = {
        "title": "设置",
        "fields": [
            {"key": "k1", "type": "text", "default": "v", "description": "d" * 100},
            {"key": "k2", "type": "bool", "default": True},
        ],
    }
    schema = parse_config_schema("ok-plug", raw)
    assert schema is not None and len(schema.fields) == 2
    assert schema.fields[0].description == "d" * 100
    assert not any("截断" in r or "已丢弃" in r or "超过上限" in r for r in log_capture)
