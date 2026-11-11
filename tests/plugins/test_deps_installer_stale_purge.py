# -*- coding: utf-8 -*-
"""DepsInstaller 重装前清理旧分发的回归测试。

背景：uv/pip 的 --target 是覆盖写入，不删旧版本文件。声明 httpx>=0.28 而
deps 里装着 0.20 时（CI 打的是旧包、manifest 后来改了），L1 版本校验会判
缺失并触发重装；若不先清理，旧 dist-info 与旧模块文件会和新版共存，
import 可能命中旧代码。

覆盖口径：
1. 按 RECORD 精确删除旧文件 + dist-info + 回收空目录
2. RECORD 里的越界路径（../）不删 target 外文件
3. 不在声明范围内的分发不动（避免误删共存依赖）
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))
if "pm_ui" not in sys.modules:
    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui import deps_installer  # noqa: E402


def _make_dist(target: Path, dist: str, version: str, files: list) -> Path:
    """在 target 下造一份带 RECORD 的分发。files 为相对 target 的路径。"""
    di = target / f"{dist}-{version}.dist-info"
    di.mkdir(parents=True, exist_ok=True)
    (di / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n", encoding="utf-8"
    )
    records = []
    for rel in files:
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("OLD", encoding="utf-8")
        records.append(f"{rel},,")
    records.append(f"{di.name}/METADATA,,")
    (di / "RECORD").write_text("\n".join(records) + "\n", encoding="utf-8")
    return di


@pytest.fixture
def installer():
    return deps_installer.DepsInstaller.__new__(deps_installer.DepsInstaller)


class TestPurgeStaleDists:
    def test_removes_old_files_and_dist_info(self, tmp_path: Path, installer):
        target = tmp_path / "deps"
        _make_dist(target, "httpx", "0.20.0", ["httpx/__init__.py", "httpx/sub/mod.py"])
        purged = installer._purge_stale_dists(target, ["httpx>=0.28"])
        assert purged == ["httpx-0.20.0.dist-info"]
        assert not (target / "httpx" / "__init__.py").exists()
        assert not (target / "httpx" / "sub").exists()  # 空目录已回收
        assert not (target / "httpx-0.20.0.dist-info").exists()

    def test_does_not_touch_undeclared_dists(self, tmp_path: Path, installer):
        target = tmp_path / "deps"
        _make_dist(target, "httpx", "0.20.0", ["httpx/__init__.py"])
        _make_dist(target, "pyyaml", "6.0.1", ["yaml/__init__.py"])
        purged = installer._purge_stale_dists(target, ["httpx>=0.28"])
        assert purged == ["httpx-0.20.0.dist-info"]
        assert (target / "yaml" / "__init__.py").exists()
        assert (target / "pyyaml-6.0.1.dist-info" / "METADATA").exists()

    def test_record_escape_path_ignored(self, tmp_path: Path, installer):
        target = tmp_path / "deps"
        outside = tmp_path / "escape.txt"
        outside.write_text("keep", encoding="utf-8")
        di = _make_dist(target, "httpx", "0.20.0", ["httpx/__init__.py"])
        with (di / "RECORD").open("a", encoding="utf-8") as fh:
            fh.write("../escape.txt,,\n")
        installer._purge_stale_dists(target, ["httpx>=0.28"])
        assert outside.exists()
        assert outside.read_text(encoding="utf-8") == "keep"

    def test_no_matching_dist_noop(self, tmp_path: Path, installer):
        target = tmp_path / "deps"
        target.mkdir()
        assert installer._purge_stale_dists(target, ["httpx>=0.28"]) == []

    def test_missing_target_noop(self, tmp_path: Path, installer):
        assert installer._purge_stale_dists(tmp_path / "nope", ["httpx>=0.28"]) == []
