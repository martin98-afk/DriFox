# -*- coding: utf-8 -*-
"""契约1：manifest schema 宽容校验。

四用例：最小 name/version 过 / 未知字段忽略+过 / version 类型错 warning+走缺省 /
缺 name 回退目录名+warning。
"""
from app.plugins.contracts.manifest_schema import validate_manifest


def test_minimal_name_version_passes():
    """最小合法清单：无 warning，version 缺省补齐。"""
    out, warnings = validate_manifest({"name": "plug-a"})
    assert warnings == []
    assert out["name"] == "plug-a"
    assert out["version"] == "0.0.0"  # 契约层缺省


def test_unknown_field_ignored_and_passes():
    """未知字段忽略（宽容扩展命名空间）：清单通过、无 warning、字段保留。"""
    raw = {"name": "plug-b", "version": "1.0", "future_extension": {"x": 1}}
    out, warnings = validate_manifest(raw)
    assert out["name"] == "plug-b"
    assert out["future_extension"] == {"x": 1}  # 忽略=不校验不删除
    assert warnings == []


def test_version_type_error_falls_back(tmp_path):
    """version:123（int）→ 类型不符 warning + 走缺省 "0.0.0"。"""
    out, warnings = validate_manifest({"name": "plug-c", "version": 123})
    assert out["version"] == "0.0.0"
    assert any("version" in w and "类型不符" in w for w in warnings)
    # api_version 传 bool（int 子类陷阱）同样按类型不符处理
    out2, warnings2 = validate_manifest({"name": "plug-c", "api_version": True})
    assert out2["api_version"] == 1
    assert any("api_version" in w for w in warnings2)


def test_missing_name_falls_back_to_dirname():
    """缺 name → 回退目录名 + warning。"""
    out, warnings = validate_manifest({"version": "1.0"}, fallback_name="dir-plug")
    assert out["name"] == "dir-plug"
    assert any("name 缺失" in w and "dir-plug" in w for w in warnings)
    # 空串 name 同样回退
    out2, warnings2 = validate_manifest({"name": "  "}, fallback_name="dir-plug")
    assert out2["name"] == "dir-plug"
    assert len(warnings2) == 1
