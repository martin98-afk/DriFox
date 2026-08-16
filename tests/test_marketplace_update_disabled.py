# -*- coding: utf-8 -*-
"""回归测试：禁用中的插件更新后保持禁用（原地更新，无双副本）

复现背景：update() 的 target 恒为 plugins/<name>。禁用插件目录在
plugins-disabled/，target 不存在 → 走全新安装分支 → 新版装到 plugins/
（启用态）+ plugins-disabled/ 旧版残留 = 双副本 + 禁用状态被破坏。

修复：update() 检测目录在 plugins-disabled/ → 目标指向禁用目录原地更新，
更新后插件仍处于禁用态。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

# 唯一包名加载 plugin-marketplace/ui：避免与其他插件的 ui 包抢占 sys.modules["ui"]（T8）
if "pm_ui" not in sys.modules:
    import types

    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui import installer as inst  # noqa: E402


def _make_installer(tmp_path):
    """构造隔离 installer（不跑真 git/网络，不走 __init__ 的真实目录）"""
    inst.report_plugin_install = lambda name: None
    p = inst.PluginInstaller.__new__(inst.PluginInstaller)
    p._plugins_dir = tmp_path / "plugins"
    p._disabled_dir = tmp_path / "plugins-disabled"
    p._cache_dir = tmp_path / "cache"
    p._inst_map_cache = None
    p._inst_map_ts = 0.0
    p._status_map_cache = None
    p._status_map_ts = 0.0
    p._manifest_cache = {}
    return p


def _plugin_meta():
    return {
        "name": "demo",
        "version": "2.0.0",
        "source": {"source": "github", "repo": "owner/demo"},
    }


def test_update_disabled_plugin_stays_disabled(monkeypatch, tmp_path):
    """禁用插件更新：新版落到 plugins-disabled，plugins/ 不出现副本"""
    p = _make_installer(tmp_path)
    # 禁用态：目录在 plugins-disabled
    disabled_target = p._disabled_dir / "demo"
    disabled_target.mkdir(parents=True)
    (disabled_target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    ok = p.update(_plugin_meta())
    assert ok is True, "禁用插件更新应成功"
    # 新版在禁用目录原地替换
    assert (disabled_target / "new.txt").read_text(encoding="utf-8") == "new", "新版必须落到禁用目录"
    assert not (disabled_target / "old.txt").exists(), "旧版必须被替换"
    # 启用目录不得出现副本（禁用状态不被破坏）
    assert not (p._plugins_dir / "demo").exists(), "更新后不得在 plugins/ 产生副本（禁用被破坏）"
    # 无残留备份/临时目录
    leftovers = [d.name for d in p._cache_dir.iterdir()] if p._cache_dir.exists() else []
    assert leftovers == [], f"cache 残留临时目录: {leftovers}"


def test_update_disabled_plugin_failure_keeps_old(monkeypatch, tmp_path):
    """禁用插件更新失败：旧版保留在禁用目录，不产生副本"""
    p = _make_installer(tmp_path)
    disabled_target = p._disabled_dir / "demo"
    disabled_target.mkdir(parents=True)
    (disabled_target / "old.txt").write_text("old", encoding="utf-8")

    def fake_fail(self, url, subpath, ref, cache_dir, extra_args=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_fail)

    ok = p.update(_plugin_meta())
    assert ok is False, "下载失败时 update 应返回 False"
    assert (disabled_target / "old.txt").read_text(encoding="utf-8") == "old", "旧版必须完好"
    assert not (p._plugins_dir / "demo").exists(), "失败路径也不得产生启用副本"


def test_update_enabled_plugin_unaffected(monkeypatch, tmp_path):
    """启用插件更新：行为不变（目标仍是 plugins/），禁用目录不受影响"""
    p = _make_installer(tmp_path)
    target = p._plugins_dir / "demo"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    ok = p.update(_plugin_meta())
    assert ok is True
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (p._disabled_dir / "demo").exists(), "启用插件更新不得影响禁用目录"
