# -*- coding: utf-8 -*-
"""同步记账器绑定时序回归。

事故复现（2026-11-01）：用户在 ConfigSyncService.enable() 之前切换主题，改动
只落盘、没打 Lamport 时间戳。随后 initial_sync 合并时，云端上次会话上传的
「更高 t 的旧内容」赢得 LWW，界面上的主题被静默刷回旧值。

两个必须守住的约束：
1. Settings 加载完成即绑定记账器（早于 enable），消灭「改配置不打 t」的窗口；
2. Settings.save() 落盘时用记账器的条目表，不能用内存里的启动快照，
   否则任意一次保存都会把条目表回滚到旧版本。
"""

import json
from unittest.mock import MagicMock

import pytest

from app.core.sync.meta_recorder import (
    SyncMetaRecorder,
    bind_settings,
    get_bound_recorder,
    reset_recorder_singleton,
)

@pytest.fixture
def clean_recorder(monkeypatch):
    """隔离全局单例，避免测试间串味"""
    import app.core.sync.meta_recorder as mr

    monkeypatch.setattr(mr, "_recorder_singleton", None)
    yield mr
    monkeypatch.setattr(mr, "_recorder_singleton", None)

class TestSettingsSaveUsesRecorderEntries:
    """Settings.save() 必须落地记账器当前的条目表"""

    def test_save_writes_recorder_entries_not_memory_snapshot(self, clean_recorder, tmp_path, monkeypatch):
        """内存 SyncMeta 是启动快照，记账器 tick 后必须覆盖它

        回归：切主题 → 记账器打 t=200 → 后续任意 save() 用内存里的旧条目表
        （t=100）落盘 → 条目表回滚 → 下次合并被云端旧 t 覆盖。
        """
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        # 内存里的旧快照：ThemeStyle 指向 lumia，t=100
        stale = json.dumps({"version": 1, "clock": 100, "entries": {"UI.ThemeStyle": {"h": "old", "t": 100}}})
        cfg.sync_meta.value = stale
        config_file = tmp_path / "app.config"
        monkeypatch.setattr(cfg, "file", config_file)

        rec = SyncMetaRecorder("testbox-abcd")
        rec.attach(cfg.toDict(serialize=False), stale)
        rec.seed_missing()
        # 模拟用户切主题：记账器打新 t
        rec.on_item_changed("UI.ThemeStyle", "lumia", "forest-sunset")
        new_ts = rec.entries()["UI.ThemeStyle"]["t"]
        assert new_ts > 100
        clean_recorder._recorder_singleton = rec

        cfg.save()

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        saved_meta = json.loads(saved["System"]["SyncMeta"])
        assert saved_meta["entries"]["UI.ThemeStyle"]["t"] == new_ts
        assert saved_meta["clock"] >= new_ts

    def test_save_without_recorder_keeps_memory_value(self, clean_recorder, tmp_path, monkeypatch):
        """未绑定记账器（单测/降级场景）→ 沿用内存值，不阻塞保存"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        raw = json.dumps({"version": 1, "clock": 5, "entries": {}})
        cfg.sync_meta.value = raw
        config_file = tmp_path / "app.config"
        monkeypatch.setattr(cfg, "file", config_file)

        cfg.save()

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert json.loads(saved["System"]["SyncMeta"])["clock"] == 5

class TestEarlyBinding:
    """Settings 加载即绑定，早于 ConfigSyncService.enable()"""

    def test_bind_settings_is_idempotent(self, clean_recorder):
        """重复绑定返回同一实例：enable() 不能换掉早期已挂好信号的那个"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        first = bind_settings(cfg, device="dev-aaaa")
        second = bind_settings(cfg, device="dev-bbbb")
        assert first is second
        assert get_bound_recorder() is first

    def test_set_device_updates_later_tag(self, clean_recorder):
        """设备标识在 enable() 时才算出 → 补设后新条目带上正确 dv"""
        rec = SyncMetaRecorder("")
        rec.attach({"UI": {"FontSize": "12"}}, "{}")
        rec.seed_missing()
        rec.set_device("mading-59f2")
        rec.on_item_changed("UI.FontSize", "12", "14")
        assert rec.entries()["UI.FontSize"]["dv"] == "mading-59f2"

    def test_set_device_ignores_empty(self, clean_recorder):
        """空标识不得覆盖已有设备标识"""
        rec = SyncMetaRecorder("mading-59f2")
        rec.attach({"UI": {"FontSize": "12"}}, "{}")
        rec.set_device("")
        rec.on_item_changed("UI.FontSize", "12", "14")
        assert rec.entries()["UI.FontSize"]["dv"] == "mading-59f2"

class TestChangeBeforeEnableSurvivesMerge:
    """端到端语义：enable 之前改的配置不会被云端旧内容回滚

    场景与事故一致：本地把 lumia 改成 forest-sunset，云端是「上次会话上传的
    旧内容」且 clock 更高（本地 clock 未落盘导致断层）。
    """

    def test_local_edit_before_enable_wins_over_stale_remote(self, clean_recorder, tmp_path):
        """走真实合并路径：本次会话改过的条目必须压过云端更高 t、更低版本的旧值"""
        from app.core.sync import config_sync as cs

        cfg_file = tmp_path / "app.config"
        local_cfg = {
            "UI": {"ThemeStyle": "forest-sunset"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 1114, "entries": {"UI.ThemeStyle": {"h": "lumia-h", "t": 1114}}}
                )
            },
        }
        cfg_file.write_text(json.dumps(local_cfg), encoding="utf-8")

        # 记账器在 Settings 加载后即绑定，用户切主题时正常打 t（t=1115）
        rec = SyncMetaRecorder("mading-59f2")
        rec.attach(local_cfg, local_cfg["System"]["SyncMeta"])
        rec.seed_missing()
        rec.on_item_changed("UI.ThemeStyle", "lumia", "forest-sunset")
        clean_recorder._recorder_singleton = rec

        svc = cs.ConfigSyncService.get_instance()
        svc._config_path = cfg_file
        svc._meta_recorder = rec
        svc._merge_bases = {"app.config"}
        svc._device_tag = "mading-59f2"
        svc._sync_base_dir = tmp_path / "cache" / "sync_base"
        svc._sha_cache_path = tmp_path / ".sync_shas.json"
        svc._reloadSettings = MagicMock()

        # 云端：值还是 lumia，但 clock=1129（高于本地 1114），t=1129
        remote = {
            "UI": {"ThemeStyle": "lumia"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 1129, "entries": {"UI.ThemeStyle": {"h": "old-h", "t": 1129}}}
                )
            },
        }
        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["UI"]["ThemeStyle"] == "forest-sunset", "本次会话的改动必须赢过云端旧值"
        entries = json.loads(data["System"]["SyncMeta"])["entries"]
        assert int(entries["UI.ThemeStyle"]["t"]) > 1129

    def test_untouched_items_still_defer_to_remote(self, clean_recorder, tmp_path):
        """本次会话没碰过的条目仍按正常 LWW：云端更高 t 的改动照常生效"""
        from app.core.sync import config_sync as cs

        cfg_file = tmp_path / "app.config"
        local_cfg = {
            "UI": {"ThemeStyle": "forest-sunset", "FontSize": "12"},
            "System": {
                "SyncMeta": json.dumps(
                    {
                        "version": 1,
                        "clock": 1114,
                        "entries": {
                            "UI.ThemeStyle": {"h": "lumia-h", "t": 1114},
                            "UI.FontSize": {"h": "fs-local", "t": 1114},
                        },
                    }
                )
            },
        }
        cfg_file.write_text(json.dumps(local_cfg), encoding="utf-8")

        rec = SyncMetaRecorder("mading-59f2")
        rec.attach(local_cfg, local_cfg["System"]["SyncMeta"])
        rec.seed_missing()
        rec.on_item_changed("UI.ThemeStyle", "lumia", "forest-sunset")
        clean_recorder._recorder_singleton = rec

        svc = cs.ConfigSyncService.get_instance()
        svc._config_path = cfg_file
        svc._meta_recorder = rec
        svc._merge_bases = {"app.config"}
        svc._device_tag = "mading-59f2"
        svc._sync_base_dir = tmp_path / "cache" / "sync_base"
        svc._sha_cache_path = tmp_path / ".sync_shas.json"
        svc._reloadSettings = MagicMock()

        remote = {
            "UI": {"ThemeStyle": "forest-sunset", "FontSize": "20"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 1129, "entries": {"UI.FontSize": {"h": "fs-h", "t": 1129}}}
                )
            },
        }
        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "20", "没动过的条目仍听云端的"

    def test_without_recorder_local_edit_loses(self, clean_recorder, tmp_path):
        """反证：没有记账器时本地改动没有 t，云端旧内容胜出（事故原貌）

        这条锁定「为什么必须早期绑定」——去掉绑定就退化成这个结果。
        """
        from app.core.sync import config_sync as cs

        cfg_file = tmp_path / "app.config"
        local_cfg = {
            "UI": {"ThemeStyle": "forest-sunset"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 1114, "entries": {"UI.ThemeStyle": {"h": "lumia-h", "t": 1114}}}
                )
            },
        }
        cfg_file.write_text(json.dumps(local_cfg), encoding="utf-8")

        svc = cs.ConfigSyncService.get_instance()
        svc._config_path = cfg_file
        svc._meta_recorder = None
        svc._merge_bases = {"app.config"}
        svc._device_tag = "mading-59f2"
        svc._sync_base_dir = tmp_path / "cache" / "sync_base"
        svc._sha_cache_path = tmp_path / ".sync_shas.json"
        svc._reloadSettings = MagicMock()

        remote = {
            "UI": {"ThemeStyle": "lumia"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 1129, "entries": {"UI.ThemeStyle": {"h": "lumia-h2", "t": 1129}}}
                )
            },
        }
        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["UI"]["ThemeStyle"] == "lumia"

    def test_unbound_recorder_loses_to_stale_remote(self, clean_recorder):
        """反证：没有记账器时本地改动没有 t，远端旧内容胜出（事故原貌）

        这条锁定「为什么必须早期绑定」——若把上面的绑定去掉，就会变成这个结果。
        """
        from app.core.sync.lww import merge_config_lww

        # 本地刚改成 forest-sunset，但条目表还是上次的 lumia@1114
        local_cfg = {"UI": {"ThemeStyle": "forest-sunset"}}
        local_entries = {"UI.ThemeStyle": {"h": "lumia-h", "t": 1114, "dv": "mading-98f5"}}
        remote_cfg = {"UI": {"ThemeStyle": "lumia"}}
        remote_entries = {"UI.ThemeStyle": {"h": "remote-lumia-h", "t": 1129, "dv": "mading-98f5"}}

        merged, _, _ = merge_config_lww(local_cfg, local_entries, remote_cfg, remote_entries)
        assert merged["UI"]["ThemeStyle"] == "lumia"
