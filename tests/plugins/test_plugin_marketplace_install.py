# -*- coding: utf-8 -*-
"""plugin-marketplace installer 回归测试

覆盖场景：
- 只读 ``.git/objects/pack/*.idx`` 文件（git clone 默认属性）在 Windows 上
  ``shutil.rmtree`` 会抛 WinError 5。修复后应能正常删除。
"""

import os
import shutil
import stat
import sys
from pathlib import Path

# 让 pytest 能直接 import 插件源码
ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _make_readonly(p: Path) -> None:
    """将文件设为只读（模拟 git pack 文件）"""
    os.chmod(p, stat.S_IREAD)


def _build_fake_plugin(root: Path, name: str) -> Path:
    """构造一个含只读 .git/objects/pack/*.idx 的假插件目录"""
    plugin = root / name
    pack = plugin / ".git" / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    (plugin / "__init__.py").write_text("# fake")
    (pack / "pack-abc123.idx").write_text("idx-content")
    (pack / "pack-abc123.pack").write_text("pack-content")
    (pack / "pack-abc123.rev").write_text("rev-content")
    for f in pack.iterdir():
        _make_readonly(f)
    return plugin


def test_rmtree_readonly_handles_readonly_pack_files(tmp_path):
    """只读 .git/objects/pack/*.idx 必须能被强制删除（不再抛 WinError 5）"""
    from ui.installer import _rmtree_readonly

    plugin = _build_fake_plugin(tmp_path, "fake-plugin-readonly")
    # 验证文件确实是只读
    idx = plugin / ".git" / "objects" / "pack" / "pack-abc123.idx"
    assert not os.access(idx, os.W_OK), "前置条件：测试文件必须是只读"

    ok = _rmtree_readonly(plugin)
    assert ok is True, "_rmtree_readonly 应返回 True"
    assert not plugin.exists(), "插件目录必须被完全删除"


def test_rmtree_readonly_normal_dir(tmp_path):
    """普通目录（无只读文件）也应正常删除"""
    from ui.installer import _rmtree_readonly

    d = tmp_path / "normal"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    sub = d / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")

    assert _rmtree_readonly(d) is True
    assert not d.exists()


def test_rmtree_readonly_missing_path(tmp_path):
    """不存在的目录应返回 True（视作删除成功）"""
    from ui.installer import _rmtree_readonly

    missing = tmp_path / "never-existed"
    assert _rmtree_readonly(missing) is True


def test_remove_plugin_with_readonly_git_pack(tmp_path, monkeypatch):
    """PluginInstaller.remove() 必须能删除含只读 pack 的插件

    通过 monkeypatch 把 installer 内部路径重定向到 tmp_path，模拟真实卸载流程。
    """
    from ui.installer import PluginInstaller

    # 构造两个 base 目录，模拟 _plugins_dir 和 _disabled_dir
    plugins_dir = tmp_path / "plugins"
    disabled_dir = tmp_path / "plugins-disabled"
    plugins_dir.mkdir()
    disabled_dir.mkdir()

    installer = PluginInstaller.__new__(PluginInstaller)
    installer._plugins_dir = plugins_dir
    installer._disabled_dir = disabled_dir
    # _purge_plugin_module_cache 不依赖具体路径，可保持默认

    plugin = _build_fake_plugin(plugins_dir, "base44")
    idx = plugin / ".git" / "objects" / "pack" / "pack-abc123.idx"
    assert not os.access(idx, os.W_OK)

    result = installer.remove("base44")
    assert result is True, "remove() 必须返回 True"
    assert not plugin.exists(), "插件目录必须被完全删除"


# ---------- manifest 校验守卫（G1-4b）：无 manifest 内容拒装 + 更新回滚 ----------


def _make_bare_installer(tmp_path):
    """隔离 installer（不跑真 git/网络，不走 __init__ 的真实目录）"""
    import types

    if "pm_ui" not in sys.modules:
        _pkg = types.ModuleType("pm_ui")
        _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
        _pkg.__package__ = "pm_ui"
        sys.modules["pm_ui"] = _pkg
    from pm_ui import installer as inst

    inst.report_plugin_install = lambda name: None
    p = inst.PluginInstaller.__new__(inst.PluginInstaller)
    p._plugins_dir = tmp_path / "plugins"
    p._cache_dir = tmp_path / "cache"
    p._inst_map_cache = None
    p._inst_map_ts = 0.0
    p._status_map_cache = None
    p._status_map_ts = 0.0
    p._manifest_cache = {}
    return p, inst


def test_install_rejects_non_plugin_content(monkeypatch, tmp_path):
    """下载内容缺 .drifox-plugin/plugin.json → 拒装（False + last_error 含 manifest + 目录不残留）"""
    p, inst = _make_bare_installer(tmp_path)

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "junk.txt").write_text("not a plugin", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    target = p._plugins_dir / "bad-plug"
    ok = p._download_and_move("bad-plug", "https://github.com/x/bad.git", ".", "main", target)
    assert ok is False
    assert "plugin.json" in (p.last_error or "")
    assert not target.exists(), "拒装后不得残留无效插件目录"


def test_update_rejects_non_plugin_content_restores_old(monkeypatch, tmp_path):
    """更新场景下载到无 manifest 内容 → 回滚旧版（旧版文件完好）"""
    p, inst = _make_bare_installer(tmp_path)
    target = p._plugins_dir / "demo"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "junk.txt").write_text("not a plugin", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    ok = p.update({"name": "demo", "version": "2.0.0", "source": {"source": "github", "repo": "o/d"}})
    assert ok is False
    assert (target / "old.txt").read_text(encoding="utf-8") == "old", "旧版必须被还原"
    assert not (target / "junk.txt").exists(), "无效内容不得残留"


def test_update_builtin_installs_to_user_root(monkeypatch, tmp_path):
    """内置插件更新：一律装用户根（plugins/<name>），不原地改写主仓 plugins/

    回归锁定：旧实现 system_dir 重定向会把新版原地覆盖项目根 plugins/<name>。
    user>system 加载优先级下用户根副本生效；卸载影子副本即回退内置版。
    """
    p, inst = _make_bare_installer(tmp_path)
    # 模拟内置插件已在主仓（system_dir 只读探测，不参与落位）
    system_dir = tmp_path / "repo-plugins"
    builtin = system_dir / "builtin-demo"
    builtin.mkdir(parents=True)
    (builtin / "builtin.txt").write_text("builtin", encoding="utf-8")
    p._system_dir = system_dir

    def fake_clone(self, url, subpath, ref, cache_dir, extra_args=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "new.txt").write_text("new", encoding="utf-8")
        md = cache_dir / ".drifox-plugin"
        md.mkdir(parents=True, exist_ok=True)
        (md / "plugin.json").write_text('{"name": "builtin-demo", "version": "2.0.0"}', encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_clone)

    ok = p.update({"name": "builtin-demo", "version": "2.0.0", "source": {"source": "github", "repo": "o/d"}})
    assert ok is True
    # 新版落用户根；主仓目录未被改写
    assert (p._plugins_dir / "builtin-demo" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (system_dir / "builtin-demo" / "builtin.txt").read_text(encoding="utf-8") == "builtin"
