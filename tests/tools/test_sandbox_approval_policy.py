# -*- coding: utf-8 -*-
"""approval 审核处理配置：默认值 / 旧配置合并 / 归一化读取"""

import json

import pytest


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    from app.tools.sandbox import SandboxConfig

    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=str(tmp_path / "sandbox_config.json"))
    monkeypatch.setattr(SandboxConfig, "_instance", cfg)
    yield cfg
    SandboxConfig.reset_instance()


def test_default_approval_node(isolated_cfg):
    from app.tools.sandbox import read_approval_policy

    p = read_approval_policy(isolated_cfg)
    assert p == {"mode": "manual", "timeout_sec": 120, "timeout_action": "deny", "ai_model": ""}


def test_old_config_without_approval_node_merges_defaults(tmp_path):
    """旧版配置文件无 approval 节点 → load 后补默认，不丢用户已有键"""
    from app.tools.sandbox import SandboxConfig, read_approval_policy

    f = tmp_path / "old.json"
    f.write_text(json.dumps({"sandbox_enabled": True}), encoding="utf-8")
    cfg = SandboxConfig(config_path=str(f))
    assert cfg.get("sandbox_enabled") is True
    assert read_approval_policy(cfg)["mode"] == "manual"


def test_policy_normalization(isolated_cfg):
    """非法取值/越界值一律收窄到白名单与范围"""
    from app.tools.sandbox import read_approval_policy

    isolated_cfg.set("approval", {"mode": "hack", "timeout_sec": -5, "timeout_action": "boom", "ai_model": 123})
    p = read_approval_policy(isolated_cfg)
    assert p["mode"] == "manual"
    assert p["timeout_sec"] == 0
    assert p["timeout_action"] == "deny"
    assert p["ai_model"] == ""

    isolated_cfg.set("approval", {"mode": "ai", "timeout_sec": 999999, "timeout_action": "ai", "ai_model": "A:b"})
    p = read_approval_policy(isolated_cfg)
    assert p == {"mode": "ai", "timeout_sec": 86400, "timeout_action": "ai", "ai_model": "A:b"}


def test_policy_node_not_dict(isolated_cfg):
    from app.tools.sandbox import read_approval_policy

    isolated_cfg.set("approval", ["bad"])
    assert read_approval_policy(isolated_cfg)["mode"] == "manual"
