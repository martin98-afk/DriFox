# -*- coding: utf-8 -*-
"""回归测试：插件更新失败必须保留旧版（先下载、成功后再替换）

复现背景：原 update() 先 rmtree 旧版再下载，网络差时下载失败 →
插件落入未安装状态（UI 提示「旧版已移除，可点安装重试」），体验受损。

修复：update() 不再先删旧版，下载成功后在 _download_and_move 内
「旧版备份 → 新版落位 → 删备份」，失败回滚旧版。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

from ui import installer as inst  # noqa: E402


def _make_installer(tmp_path):
    """构造隔离 installer（不跑真 git/网络，不走 __init__ 的真实目录）"""
    inst.report_plugin_install = lambda name: None
    p = inst.PluginInstaller.__new__(inst.PluginInstaller)
    p._plugins_dir = tmp_path / "plugins"
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


def test_update_failure_keeps_old_version(monkeypatch, tmp_path):
    """下载失败 → update 返回 False，旧版目录与内容完好（未删除）"""
    p = _make_installer(tmp_path)
    target = p._plugins_dir / "demo"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_fail(self, url, subpath, ref, cache_dir, extra_args=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_fail)

    ok = p.update(_plugin_meta())
    assert ok is False, "下载失败时 update 应返回 False"
    assert target.exists(), "下载失败时旧版目录必须保留"
    assert (target / "old.txt").read_text(encoding="utf-8") == "old", "旧版内容必须完好"


def test_update_success_replaces_old_version(monkeypatch, tmp_path):
    """下载成功 → 新版替换旧版，旧备份被清理，无残留临时目录"""
    p = _make_installer(tmp_path)
    target = p._plugins_dir / "demo"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    ok = p.update(_plugin_meta())
    assert ok is True, "下载成功时 update 应返回 True"
    assert (target / "new.txt").read_text(encoding="utf-8") == "new", "新版必须已落位"
    assert not (target / "old.txt").exists(), "旧版文件必须被替换"
    # 无残留备份/临时目录
    leftovers = [d.name for d in p._cache_dir.iterdir()] if p._cache_dir.exists() else []
    assert leftovers == [], f"cache 残留临时目录: {leftovers}"
    assert not p._cache_dir.exists() or not any(p._cache_dir.iterdir()), "cache 应无残留"


def test_update_rollback_when_replace_fails(monkeypatch, tmp_path):
    """替换环节失败（新版移入目标失败）→ 回滚旧版，插件保持可用"""
    p = _make_installer(tmp_path)
    target = p._plugins_dir / "demo"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    real_move = inst.shutil.move

    def broken_move(src, dst):
        # 只破坏「新版移入目标」这一步（src 是下载缓存目录、dst 是目标）；
        # 回滚 move（src 是 *_old_* 备份目录）放行
        src_str = str(src)
        if str(dst) == str(target) and "_old_" not in src_str and str(p._cache_dir) in src_str:
            raise OSError("simulated replace failure")
        return real_move(src, dst)

    monkeypatch.setattr(inst.shutil, "move", broken_move)

    ok = p.update(_plugin_meta())
    assert ok is False, "替换失败时 update 应返回 False"
    assert target.exists(), "回滚后目标目录必须存在"
    assert (target / "old.txt").read_text(encoding="utf-8") == "old", "回滚后旧版内容必须完好"
    assert not (target / "new.txt").exists(), "回滚后不得残留新版文件"
