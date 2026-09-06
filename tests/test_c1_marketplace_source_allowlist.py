# -*- coding: utf-8 -*-
"""C1：市场源白名单（T8.1 行 6 口径）。

七用例：repo 正则拒 ../、http 拒、非白名单 host 拒、file:// 拒、ssh:// 拒、
Settings 扩展 host 过、install 前二次校验拒非白名单 URL 且 git 子进程未被调用。
全程 tmp_path；git 子进程用 monkeypatch 拦截，零网络零真实目录写入。
"""
import sys
import types
from pathlib import Path

import pytest
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

# 唯一包名加载 plugin-marketplace/ui（避免与其他插件的 ui 包抢占 sys.modules["ui"]）
if "pm_ui" not in sys.modules:
    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui import installer as installer_mod  # noqa: E402
from pm_ui import marketplace_manager as mm  # noqa: E402

validate_marketplace_source = mm.validate_marketplace_source
MarketplaceSourceManager = mm.MarketplaceSourceManager
Installer = installer_mod.PluginInstaller


@pytest.fixture()
def log_capture():
    """loguru WARNING+ 捕获为文本列表。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def test_github_repo_regex_rejects_traversal(log_capture):
    """github repo 必须是 owner/name 形态：路径穿越/缺名/嵌套路径全拒。"""
    for bad_repo in ["../../etc/passwd", "owner", "owner/name/extra", "", "a/..", "/abs"]:
        ok, reason = validate_marketplace_source({"source": "github", "repo": bad_repo})
        assert ok is False, bad_repo
        assert "repo 不合法" in reason
    # 正向：标准 owner/name 放行
    ok, _ = validate_marketplace_source({"source": "github", "repo": "martin98-afk/drifox-plugins"})
    assert ok is True


def test_http_scheme_rejected(log_capture):
    """url 类型 scheme 必须 https：http 拒。"""
    ok, reason = validate_marketplace_source({"source": "url", "url": "http://github.com/a/b.json"})
    assert ok is False
    assert "https" in reason


def test_non_allowlisted_host_rejected(log_capture):
    """非白名单 git host 拒（内置五家之外）。"""
    ok, reason = validate_marketplace_source(
        {"source": "url", "url": "https://evil.example.com/malware/marketplace.json"}
    )
    assert ok is False
    assert "非白名单" in reason and "evil.example.com" in reason


def test_file_and_ssh_urls_rejected(log_capture):
    """file:// 与 ssh:// 一律拒（scheme 白名单外）。"""
    for bad in [
        {"source": "url", "url": "file:///C:/Windows/system32/config"},
        {"source": "url", "url": "ssh://git@github.com/owner/repo"},
        {"source": "git", "path": "/some/local/path"},
        {"source": "url", "url": "git@github.com:owner/repo.git"},
    ]:
        ok, _reason = validate_marketplace_source(bad)
        assert ok is False, bad


def test_settings_extension_host_allowed(tmp_path, log_capture):
    """Settings.marketplace_allowed_git_hosts 扩展的内网 git 源放行。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.marketplace_allowed_git_hosts.value or [])
    cfg.marketplace_allowed_git_hosts.value = saved + ["git.internal.corp"]
    try:
        ok, reason = validate_marketplace_source(
            {"source": "url", "url": "https://git.internal.corp/team/plugins.git"}
        )
        assert ok is True, reason
    finally:
        cfg.marketplace_allowed_git_hosts.value = saved


def test_install_precheck_rejects_and_skips_git(tmp_path, log_capture, monkeypatch):
    """install 分发前二次校验：非白名单 URL 拒，且 git 子进程完全未被调用。"""

    def _boom(*a, **k):
        raise AssertionError("git subprocess must never be spawned for non-allowlisted source")

    monkeypatch.setattr(installer_mod.subprocess, "Popen", _boom)
    monkeypatch.setattr(installer_mod.subprocess, "run", _boom)

    installer = Installer()
    result = installer._install_by_source(
        "evil-plug",
        {"source": "url", "url": "https://evil.example.com/malware/plug.git"},
        tmp_path,
    )
    assert result is False
    assert any(
        "[MarketplaceGuard]" in r and "二次校验" in r and "evil.example.com" in r for r in log_capture
    )


def test_add_source_rejects_and_legacy_tolerated(tmp_path, log_capture):
    """add_source 强校验返 False + [MarketplaceGuard] 日志；存量非法条目加载仅告警不阻断。"""
    mgr = MarketplaceSourceManager.__new__(MarketplaceSourceManager)
    mgr._sources_file = tmp_path / "sources.json"
    # 存量非法条目（手改文件模拟）
    import json

    legacy = [
        {"name": "legacy-bad", "source": {"source": "url", "url": "http://evil.example.com/x.json"}, "auto_update": False}
    ]
    mgr._sources_file.write_text(json.dumps(legacy), encoding="utf-8")
    sources = mgr.get_sources()  # 加载不阻断
    assert len(sources) == 1
    assert any("存量市场源未过白名单" in r for r in log_capture)
    # 新增强校验
    assert mgr.add_source("bad-new", {"source": "url", "url": "https://evil.example.com/y.json"}) is False
    assert any("[MarketplaceGuard] 拒绝添加市场源" in r for r in log_capture)
    # 合法新增成功
    assert (
        mgr.add_source("ok-new", {"source": "github", "repo": "martin98-afk/drifox-plugins"}) is True
    )
