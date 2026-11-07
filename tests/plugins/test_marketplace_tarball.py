# -*- coding: utf-8 -*-
"""plugin-marketplace tarball 分发通道回归（零网络）

覆盖：
- validate_marketplace_source 的 tarball 分支（https 直链通过 / http 拒绝 / sha256 格式）
- _preferred_sources：tarball 当前平台优先，失败候选含 git-subdir 回落
- extract_tarball_file：单插件包无顶层目录时正确解压（不剥首段）
"""
import io
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN = ROOT / "plugins" / "plugin-marketplace"
UI_DIR = PLUGIN / "ui"


@pytest.fixture(scope="module")
def ui_modules():
    """按插件 UI 包名加载 ui 子模块（与宿主加载方式一致）"""
    import importlib
    import importlib.util
    import sys

    pkg_name = "ui_plugin_plugin_marketplace"
    spec = importlib.util.spec_from_file_location(
        pkg_name, UI_DIR / "__init__.py", submodule_search_locations=[str(UI_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return {
        "mm": importlib.import_module(f"{pkg_name}.marketplace_manager"),
        "inst": importlib.import_module(f"{pkg_name}.installer"),
        "arch": importlib.import_module(f"{pkg_name}.archive_downloader"),
    }


def test_validate_tarball_source(ui_modules):
    mm = ui_modules["mm"]
    sha = "a" * 64
    ok, _ = mm.validate_marketplace_source(
        {"type": "tarball", "url": "https://github.com/o/r/releases/download/t/f.tar.gz", "sha256": sha}
    )
    assert ok is True

    ok, reason = mm.validate_marketplace_source(
        {"type": "tarball", "url": "https://github.com/o/r/f.tar.gz", "sha256": "not-a-sha"}
    )
    assert ok is False and "sha256" in reason

    ok, reason = mm.validate_marketplace_source({"type": "tarball", "url": "http://evil.com/f.tar.gz"})
    assert ok is False and "https" in reason


def test_preferred_sources_tarball_first(ui_modules, monkeypatch):
    inst_mod = ui_modules["inst"]
    inst = inst_mod.PluginInstaller.__new__(inst_mod.PluginInstaller)

    # 当前平台键：直接给 win32 条目，避免依赖宿主平台探测
    meta = {
        "name": "demo",
        "tarball": {
            "win32": {"url": "https://github.com/o/r/releases/download/t/demo-win32.tar.gz", "sha256": "b" * 64},
            "default": {"url": "https://github.com/o/r/releases/download/t/demo-all.tar.gz"},
        },
        "source": {"type": "git-subdir", "url": "https://github.com/o/r", "path": "plugins/demo"},
    }
    import app.plugins.deps_loader as dl

    monkeypatch.setattr(dl, "current_platform_key", lambda: "win32", raising=False)
    srcs = inst._preferred_sources(meta)
    assert srcs[0]["type"] == "tarball"
    assert srcs[0]["url"].endswith("demo-win32.tar.gz")
    assert srcs[1]["type"] == "git-subdir"

    # 无 tarball 字段 → 只有原 source
    srcs2 = inst._preferred_sources({"name": "x", "source": {"type": "git-subdir", "url": "u", "path": "p"}})
    assert len(srcs2) == 1 and srcs2[0]["type"] == "git-subdir"


def _make_plugin_tarball(path: Path) -> None:
    """构造无顶层目录的单插件包（模拟 build_plugin_tarballs.py 产物）"""
    files = {
        ".drifox-plugin/plugin.json": json.dumps({"name": "demo", "version": "1.0.0"}),
        "README.md": "demo",
        "deps/win32/lib.txt": "binary",
    }
    with tarfile.open(path, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_tarball_file_keeps_root(ui_modules, tmp_path):
    arch = ui_modules["arch"]
    tar_path = tmp_path / "demo-1.0.0-win32.tar.gz"
    _make_plugin_tarball(tar_path)

    out = tmp_path / "out"
    arch.extract_tarball_file(tar_path, "", out)
    assert (out / ".drifox-plugin" / "plugin.json").is_file()
    assert (out / "README.md").is_file()
    assert (out / "deps" / "win32" / "lib.txt").is_file()
    # 首段路径未被剥掉（曾因共享 strip_top 逻辑把 .drifox-plugin 剥成 drifox-plugin）
    assert not (out / "drifox-plugin").exists()
