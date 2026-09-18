# -*- coding: utf-8 -*-
"""SandboxConfig：默认值 / 缺键深合并 / 原子保存 / 单例"""
import json

from app.tools.sandbox import DEFAULT_CONFIG, SandboxConfig


def test_defaults_complete():
    cfg = SandboxConfig(config_path=":memory:")
    assert cfg.get("sandbox_enabled") is True
    assert cfg.get("path.whitelist") == []
    assert cfg.get("job_limits.memory_mb") == 2048
    assert cfg.get("backup_limit_mb") == 3000


def test_deep_merge_fills_missing_keys(tmp_path):
    p = tmp_path / "sandbox_config.json"
    p.write_text(json.dumps({"sandbox_enabled": False}), encoding="utf-8")
    cfg = SandboxConfig(config_path=str(p))
    # 磁盘值优先
    assert cfg.get("sandbox_enabled") is False
    # 缺的键由默认补齐
    assert cfg.get("job_limits.active_process") == 64


def test_set_and_save_roundtrip(tmp_path):
    p = tmp_path / "sandbox_config.json"
    cfg = SandboxConfig(config_path=str(p))
    cfg.set("path.blacklist", ["D:/secrets"])
    cfg.save()
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["path"]["blacklist"] == ["D:/secrets"]
    # 重载一致
    assert SandboxConfig(config_path=str(p)).get("path.blacklist") == ["D:/secrets"]


def test_default_config_has_no_extra_top_keys():
    assert set(DEFAULT_CONFIG) == {
        "sandbox_enabled", "path", "command", "network",
        "sys_tools_bypass", "delete_protection", "job_limits", "backup_limit_mb",
    }
