# -*- coding: utf-8 -*-
"""契约3：min_host_version 格式非法改拒载（原为放行+warning）。

四用例：合法 semver 过 / 垃圾串拒 / 注入串拒 / 未声明过。
宿主版本通过 monkeypatch version_gate.host_version 固定，避免依赖真实 Settings。
"""
import pytest

from app.plugins import version_gate
from app.plugins.version_gate import check_host_version


@pytest.fixture()
def host_ver(monkeypatch):
    """固定宿主版本为 0.5.8，隔离真实配置。"""

    def _set(v: str):
        monkeypatch.setattr(version_gate, "host_version", lambda: v)

    return _set


def test_valid_semver_passes(host_ver):
    """合法 semver 且宿主满足 → 放行。"""
    host_ver("0.5.8")
    ok, reason = check_host_version({"min_host_version": "0.5.7"}, "plug-a")
    assert ok is True and reason == ""


def test_garbage_version_rejected(host_ver):
    """垃圾串（解析失败）→ 拒载，reason 含格式非法与原文。"""
    host_ver("0.5.8")
    ok, reason = check_host_version({"min_host_version": "not-a-version"}, "plug-b")
    assert ok is False
    assert "min_host_version 格式非法" in reason and "not-a-version" in reason


def test_injection_string_rejected(host_ver):
    """注入串（分号命令拼接）非 semver → 拒载，原文不得进入放行链路。"""
    host_ver("0.5.8")
    payload = "0.5.7; rm -rf /"
    ok, reason = check_host_version({"min_host_version": payload}, "plug-c")
    assert ok is False
    assert "格式非法" in reason


def test_absent_declaration_passes(host_ver):
    """未声明 min_host_version → 放行（老插件零改动）。"""
    host_ver("0.5.8")
    ok, reason = check_host_version({}, "plug-d")
    assert ok is True and reason == ""
    ok2, _ = check_host_version({"min_host_version": ""}, "plug-d")
    assert ok2 is True
