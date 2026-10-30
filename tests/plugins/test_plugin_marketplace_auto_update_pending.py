# -*- coding: utf-8 -*-
"""回归测试：自动更新完成后返回的 pending 必须是「更新后仍待更新」数

复现背景：run_auto_update_check 的 pending 是**更新前**的快照。execute=True
把可更新插件全部装完后仍原样返回旧值，_on_auto_update_done 用该值刷新侧栏
「插件市场 (N)」标题 → 自动更新成功后左侧栏 TabPanel 条目计数不变，要等
下一次 30 分钟 tick 才纠正（用户视角：更新完了文字没动）。

修复：execute 成功后按更新结果重算 pending（更新成功者剔除、失败者保留）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


class FakeMarketManager:
    """市场数据桩：两个已安装插件 a/b 都有新版"""

    def fetch_all(self, force: bool = False):
        return (
            [
                {"name": "a", "version": "2.0"},
                {"name": "b", "version": "2.0"},
            ],
            [],
            [],
        )


class FakeInstaller:
    """安装器桩：a 更新成功（本地版本升到 2.0），b 更新失败（保持 1.0）"""

    def __init__(self):
        self.local = {"a": "1.0", "b": "1.0"}

    def get_installed_map(self):
        return dict(self.local)

    def check_update(self, plugin_meta):
        name = plugin_meta.get("name", "")
        local = self.local.get(name)
        if local is None:
            return (False, None, None)
        remote = plugin_meta.get("version")
        return (local != remote, local, remote)

    def update(self, plugin_meta):
        if plugin_meta.get("name") == "a":
            self.local["a"] = "2.0"
            return True
        return False


def _install_stubs(monkeypatch):
    from ui import installer as installer_mod
    from ui import marketplace_manager as mm

    inst = FakeInstaller()
    monkeypatch.setattr(mm, "get_marketplace_manager", lambda: FakeMarketManager())
    monkeypatch.setattr(installer_mod, "get_installer", lambda: inst)
    return mm, inst


def test_pending_after_auto_update_excludes_updated(monkeypatch):
    """a 更新成功、b 失败 → pending 必须只剩 b"""
    mm, _inst = _install_stubs(monkeypatch)

    result = mm.run_auto_update_check(execute=True)

    assert result["updated"] == ["a"], f"更新结果异常: {result}"
    assert [n for n, _e in result["failed"]] == ["b"], f"失败清单异常: {result}"
    assert result["pending"] == 1, f"pending 应为更新后剩余 1 个，实际: {result['pending']}"
    assert result["pending_names"] == ["b"], f"pending_names 应只剩 b，实际: {result['pending_names']}"


def test_pending_zero_after_full_auto_update(monkeypatch):
    """全部更新成功 → pending 必须归零（侧栏计数才会清掉 (N)）"""
    from ui import installer as installer_mod
    from ui import marketplace_manager as mm

    class AllOkInstaller(FakeInstaller):
        def update(self, plugin_meta):
            self.local[plugin_meta["name"]] = "2.0"
            return True

    inst = AllOkInstaller()
    monkeypatch.setattr(mm, "get_marketplace_manager", lambda: FakeMarketManager())
    monkeypatch.setattr(installer_mod, "get_installer", lambda: inst)

    result = mm.run_auto_update_check(execute=True)

    assert result["updated"] == ["a", "b"], f"更新结果异常: {result}"
    assert result["pending"] == 0, f"全部更新成功后 pending 应为 0，实际: {result['pending']}"


def test_check_only_keeps_pending(monkeypatch):
    """execute=False（仅检查）不更新，pending 保持全量"""
    mm, _inst = _install_stubs(monkeypatch)

    result = mm.run_auto_update_check(execute=False)

    assert result["updated"] == [], f"仅检查不得执行更新: {result}"
    assert result["pending"] == 2, f"仅检查时 pending 应为 2，实际: {result['pending']}"
