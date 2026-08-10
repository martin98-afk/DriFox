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
ROOT = Path(__file__).resolve().parent.parent
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
