# -*- coding: utf-8 -*-
"""回归测试：插件根目录删除事件的磁盘二次核实。

背景（2026-08-29 卡死排查）：watchfiles 在 Windows 上对仍存在的插件目录误报
Deleted（杀毒扫描/索引服务批量触碰目录句柄，表现为多个插件根在数百 ms 内连续
"被删除"）。旧逻辑信任事件直接全组件卸载 → 目录实际仍在 → 后续变更又触发重载
→ 主线程同步重建全部 UI 组件（设置卡/欢迎卡/输入按钮）→ 整软件卡死。
修复：删除事件先经 os.path.isdir 磁盘核实，目录仍在则忽略。
"""

from app.core.plugin_host_service import PluginHostService

# watchfiles.Change.deleted 的数值（枚举 added=1, modified=2, deleted=3）
_DELETED = 3
_MODIFIED = 2


def _prefixes(*paths: str) -> dict:
    return {p.lower().rstrip("\\/"): f"plugin-{i}" for i, p in enumerate(paths)}


class TestConfirmPluginRootDeleted:
    def test_misreported_delete_ignored_when_dir_still_exists(self, monkeypatch):
        """误报场景：事件称根目录被删，但磁盘上目录仍在 → False（不卸载）"""
        monkeypatch.setattr("app.core.plugin_host_service.os.path.isdir", lambda p: True)
        prefixes = _prefixes("C:/u/.drifox/plugins/prompt-enhancer")
        changes = [
            (_DELETED, "C:/u/.drifox/plugins/prompt-enhancer"),
            (_MODIFIED, "C:/u/.drifox/plugins/prompt-enhancer/ui/x.py"),
        ]
        assert PluginHostService._confirm_plugin_root_deleted("plugin-0", prefixes, changes) is False

    def test_real_delete_confirmed_when_dir_gone(self, monkeypatch):
        """真实删除：磁盘上目录确实不存在 → True（安全触发卸载）"""
        monkeypatch.setattr("app.core.plugin_host_service.os.path.isdir", lambda p: False)
        prefixes = _prefixes("C:/u/.drifox/plugins/prompt-enhancer")
        changes = [(_DELETED, "C:/u/.drifox/plugins/PROMPT-ENHANCER")]
        assert PluginHostService._confirm_plugin_root_deleted("plugin-0", prefixes, changes) is True

    def test_no_delete_event_returns_false(self, monkeypatch):
        """无删除事件（仅普通文件修改）→ False，且不触碰磁盘核实"""
        called = []

        def _fail(p):
            called.append(p)
            return False

        monkeypatch.setattr("app.core.plugin_host_service.os.path.isdir", _fail)
        prefixes = _prefixes("C:/u/.drifox/plugins/browser")
        changes = [(_MODIFIED, "C:/u/.drifox/plugins/browser/ui/main.py")]
        assert PluginHostService._confirm_plugin_root_deleted("plugin-0", prefixes, changes) is False
        assert called == []

    def test_other_plugin_delete_not_matched(self, monkeypatch):
        """删除事件属于其他插件根 → 本插件返回 False"""
        monkeypatch.setattr("app.core.plugin_host_service.os.path.isdir", lambda p: False)
        prefixes = _prefixes("C:/u/.drifox/plugins/a", "C:/u/.drifox/plugins/b")
        changes = [(_DELETED, "C:/u/.drifox/plugins/b")]
        assert PluginHostService._confirm_plugin_root_deleted("plugin-0", prefixes, changes) is False
