# -*- coding: utf-8 -*-
"""云同步 LWW 合并与内容指纹测试。

覆盖三条核心约束：
1. 内容没变 → 零网络请求，不产生空 commit；
2. 远端被别的设备改过 → 先合并，绝不覆盖；
3. 冲突 → 本地优先 + 远端副本留档，双方内容都不丢。

合并算法细节见 tests/core/test_sync_lww.py 与 tests/core/test_sync_meta_recorder.py。

Run: pytest tests/core/test_config_sync_merge.py -v
"""

import base64
import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.sync import fingerprint as fp


# =============================================================================
# 1. 内容指纹与确定性打包
# =============================================================================


class TestFingerprint:
    def test_dir_fingerprint_ignores_mtime(self, tmp_path):
        """内容相同、mtime 不同 → 指纹相同（watchfiles 的 touch 不应触发上传）"""
        d = tmp_path / "d"
        d.mkdir()
        (d / "a.txt").write_text("hello", encoding="utf-8")
        first = fp.dir_fingerprint(d)

        time.sleep(0.01)
        (d / "a.txt").write_text("hello", encoding="utf-8")
        assert fp.dir_fingerprint(d) == first

    def test_dir_fingerprint_changes_with_content(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        f = d / "a.txt"
        f.write_text("v1", encoding="utf-8")
        first = fp.dir_fingerprint(d)
        f.write_text("v2", encoding="utf-8")
        assert fp.dir_fingerprint(d) != first

    def test_excludes_runtime_junk(self, tmp_path):
        """__pycache__ / .DS_Store / *.pyc 不进清单，也不会污染跨平台同步"""
        d = tmp_path / "d"
        (d / "__pycache__").mkdir(parents=True)
        (d / "keep.txt").write_text("x", encoding="utf-8")
        (d / "__pycache__" / "m.cpython-314.pyc").write_bytes(b"junk")
        (d / ".DS_Store").write_bytes(b"junk")
        (d / "debug.log").write_text("junk", encoding="utf-8")
        manifest = fp.dir_manifest(d)
        assert list(manifest) == ["keep.txt"]

    def test_stable_zip_is_deterministic(self, tmp_path):
        """同内容两次打包字节完全一致（否则 zip 哈希永远判「变了」）"""
        d = tmp_path / "d"
        d.mkdir()
        (d / "a.txt").write_text("hello", encoding="utf-8")
        (d / "sub").mkdir()
        (d / "sub" / "b.txt").write_text("world", encoding="utf-8")
        z1 = fp.build_stable_zip(d)
        time.sleep(0.01)
        (d / "a.txt").write_text("hello", encoding="utf-8")
        z2 = fp.build_stable_zip(d)
        assert z1 == z2
        assert fp.git_blob_sha(z1) == fp.git_blob_sha(z2)

    def test_git_blob_sha_matches_git(self, tmp_path):
        """git_blob_sha 必须与 git hash-object 一致（远端 sha 才能直接比对）"""
        data = b"drifox sync probe\n"
        expected = (
            subprocess.run(
                ["git", "hash-object", "--stdin"],
                input=data,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        assert fp.git_blob_sha(data) == expected


# =============================================================================
# 4. ConfigSyncService 上传链路
# =============================================================================


def _resp(status=200, json_data=None):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data if json_data is not None else {}
    m.text = json.dumps(json_data or {})
    return m


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """隔离到 tmp_path 的服务实例（真实文件系统，不用 MagicMock 路径）"""
    from app.core.sync import config_sync as cs

    monkeypatch.setattr(cs, "get_app_data_dir", lambda: tmp_path)
    service = cs.ConfigSyncService.get_instance()
    service._state = "idle"
    service._token = "token"
    service._owner = "owner"
    service._initial_sync_completed = True
    service._config_remote_sha = None
    service._custom_remote_sha = None
    service._records_remote_sha = None
    service._ext_remote_sha = {}
    service._ext_dirty = {}
    service._content_hashes = {}
    service._upload_lock = threading.Lock()
    service._sync_base_dir = tmp_path / "cache" / "sync_base"
    service._sha_cache_path = tmp_path / ".sync_shas.json"
    service._device_tag = "testbox-abcd"
    service._suppress_until = 0.0
    service._config_path = tmp_path / "app.config"
    service._records_path = tmp_path / "share" / "records.json"
    service._user_custom_path = tmp_path / "plugins" / "user-custom"
    service._user_custom_path.mkdir(parents=True, exist_ok=True)
    yield service
    service._state = "disabled"


def _zip_b64(root: Path, prefix: str = "") -> str:
    return base64.b64encode(fp.build_stable_zip(root, arc_prefix=prefix)).decode("utf-8")


class TestUploadUnit:
    def test_unchanged_content_makes_no_request(self, svc):
        """内容指纹未变 → 不打包、不发请求（空上传根治）

        先落一份条目表模拟「已完成升级迁移」；没有条目表时会被强制上传一次
        （见 test_dir_without_meta_forces_migration_upload）。
        """
        (svc._user_custom_path / "x.txt").write_text("v1", encoding="utf-8")
        svc._dir_sync_meta("user-custom", svc._user_custom_path)
        svc._content_hashes["user-custom"] = fp.dir_fingerprint(svc._user_custom_path)
        client = MagicMock()

        ok = svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client)

        assert ok is True
        assert client.put.call_count == 0
        assert client.post.call_count == 0
        assert client.get.call_count == 0

    def test_remote_already_has_content_skips_write(self, svc):
        """待上传字节的 blob sha 与 base 相同 → 远端已是这份内容，不写"""
        (svc._user_custom_path / "x.txt").write_text("v1", encoding="utf-8")
        payload = fp.build_stable_zip(
            svc._user_custom_path,
            sync_meta=svc._dir_sync_meta("user-custom", svc._user_custom_path),
        )
        svc._custom_remote_sha = fp.git_blob_sha(payload)
        client = MagicMock()

        ok = svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client)

        assert ok is True
        assert client.put.call_count == 0
        assert client.post.call_count == 0

    def test_dir_without_meta_forces_migration_upload(self, svc):
        """目录还没有条目表（旧版本升级上来）→ 即使内容指纹未变也要传一次带元数据的包

        否则云端 zip 永远缺 .__drifox_sync__.json，目录删除只能退化成 union。
        """
        (svc._user_custom_path / "x.txt").write_text("v1", encoding="utf-8")
        svc._content_hashes["user-custom"] = fp.dir_fingerprint(svc._user_custom_path)
        svc._custom_remote_sha = "old_sha"
        assert not svc._dir_meta_path("user-custom").exists()

        client = MagicMock()
        client.get.side_effect = [
            _resp(200, {"sha": "old_sha"}),  # _get_remote_file_sha
        ]
        client.put.return_value = _resp(200, {"content": {"sha": "new_sha"}})

        assert svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client) is True
        assert client.put.call_count == 1
        assert svc._dir_meta_path("user-custom").exists()

    def test_diverged_remote_is_merged_not_overwritten(self, svc, tmp_path):
        """远端被别的设备改过 → 先合并，冲突保留本地并生成远端副本"""
        svc._custom_remote_sha = "base_sha"

        # 本地：a.txt 改成 local（未上传）
        (svc._user_custom_path / "a.txt").write_text("local", encoding="utf-8")

        # 远端：a.txt 改成 remote，另有 b.txt
        remote_dir = tmp_path / "remote_src"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("remote", encoding="utf-8")
        (remote_dir / "b.txt").write_text("from-other-device", encoding="utf-8")
        remote_b64 = _zip_b64(remote_dir)

        client = MagicMock()
        client.get.side_effect = [
            _resp(200, {"sha": "live_sha"}),  # _get_remote_file_sha
            _resp(200, {"content": remote_b64, "sha": "live_sha"}),  # _read_remote_bytes
        ]
        client.put.return_value = _resp(200, {"content": {"sha": "new_sha"}})

        ok = svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client)

        assert ok is True
        # 本地内容不丢
        assert (svc._user_custom_path / "a.txt").read_text(encoding="utf-8") == "local"
        # 远端独有文件被补入
        assert (svc._user_custom_path / "b.txt").read_text(encoding="utf-8") == "from-other-device"
        # 冲突：对方版本留档到业务目录之外（插件不会扫描到）
        assert not [p for p in svc._user_custom_path.iterdir() if "remote-" in p.name]
        archived = list((tmp_path / "conflicts").rglob("a.txt"))
        assert len(archived) == 1
        assert archived[0].read_text(encoding="utf-8") == "remote"
        # 合并后确实发起了写入（把合并结果推回云端）
        assert client.put.call_count == 1

    def test_conflict_archived_outside_business_dir(self, svc, tmp_path):
        """冲突副本不落业务目录（插件会误读），改落 conflicts/ 并推到远端独立目录"""
        svc._custom_remote_sha = "base_sha"
        (svc._user_custom_path / "a.txt").write_text("local", encoding="utf-8")

        remote_dir = tmp_path / "remote2"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("remote", encoding="utf-8")
        client = MagicMock()
        client.get.side_effect = [
            _resp(200, {"sha": "live_sha"}),
            _resp(200, {"content": _zip_b64(remote_dir), "sha": "live_sha"}),
        ]
        client.put.return_value = _resp(200, {"content": {"sha": "new_sha"}})
        client.post.return_value = _resp(201, {"content": {"sha": "conflict_sha"}})

        assert svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client) is True

        # 业务目录只有主文件，没有任何副本
        assert sorted(p.name for p in svc._user_custom_path.iterdir()) == ["a.txt"]
        # 本地留档在 conflicts/
        assert list((tmp_path / "conflicts").rglob("a.txt"))
        # 远端也收到一份归档，路径在业务 zip 之外（新路径走 POST 新建）
        urls = [c.args[0] for c in client.post.call_args_list if c.args]
        assert any("/contents/drifox/conflicts/" in u for u in urls)

    def test_no_base_still_merges_instead_of_overwrite(self, svc, tmp_path):
        """没有 base（新机器 / sha 缓存被清）且远端已有内容 → 必须合并，不能直接覆盖"""
        (svc._user_custom_path / "mine.txt").write_text("local-only", encoding="utf-8")
        remote_dir = tmp_path / "r3"
        remote_dir.mkdir()
        (remote_dir / "theirs.txt").write_text("cloud-only", encoding="utf-8")
        client = MagicMock()
        client.get.side_effect = [
            _resp(200, {"sha": "live_sha"}),
            _resp(200, {"content": _zip_b64(remote_dir), "sha": "live_sha"}),
        ]
        client.put.return_value = _resp(200, {"content": {"sha": "new_sha"}})

        ok = svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client)

        assert ok is True
        assert (svc._user_custom_path / "mine.txt").read_text(encoding="utf-8") == "local-only"
        assert (svc._user_custom_path / "theirs.txt").read_text(encoding="utf-8") == "cloud-only"

    def test_upload_without_base_is_remote_authoritative(self, svc):
        """无合并基准（解绑重绑）上传前合并以云端为权威，本地旧配置不覆盖远端"""
        local = {
            "UI": {"FontSize": "12"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 99, "entries": {"UI.FontSize": {"h": "a", "t": 99}}}
                )
            },
        }
        remote = {
            "UI": {"FontSize": "18"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 7, "entries": {"UI.FontSize": {"h": "b", "t": 7}}}
                )
            },
        }
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")
        remote_b64 = base64.b64encode(json.dumps(remote).encode("utf-8")).decode("utf-8")

        client = MagicMock()
        client.get.side_effect = [
            _resp(200, {"sha": "live_sha"}),  # _get_remote_file_sha
            _resp(200, {"content": remote_b64, "sha": "live_sha"}),  # _read_remote_bytes
        ]
        client.put.return_value = _resp(200, {"content": {"sha": "new_sha"}})

        assert svc._upload_unit("app.config", "drifox/app.config", svc._config_path, client) is True

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "18"
        assert client.put.call_count == 1

    def test_put_conflict_does_not_retry_overwrite(self, svc):
        """PUT 报 sha 冲突时不重试覆盖：必须放弃本次写入，等下一轮合并"""
        (svc._records_path.parent).mkdir(parents=True, exist_ok=True)
        svc._records_path.write_text("[]", encoding="utf-8")
        svc._records_remote_sha = "base_sha"
        client = MagicMock()
        client.get.return_value = _resp(200, {"sha": "base_sha"})
        client.put.return_value = _resp(409, {"message": "sha does not match"})

        ok = svc._upload_unit("share_records", "drifox/share_records.json", svc._records_path, client)

        assert ok is False
        # 只发了一次 PUT，没有「拉最新 sha 再覆盖」的第二次写
        assert client.put.call_count == 1

    def test_provider_not_pushed_with_config_change(self, svc):
        """app.config 变脏不再搭车推送 provider（1.6MB 空转的根因）"""
        from app.core.sync import config_sync as cs

        provider_dir = svc._user_custom_path.parent / "assistant_hub"
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / "m.md").write_text("note", encoding="utf-8")
        provider = cs.SyncContentProvider(
            provider_id="assistant_hub",
            label="助手",
            local_dir=str(provider_dir),
            remote_path="drifox/ext/assistant_hub.zip",
        )
        cs._sync_content_providers["assistant_hub"] = provider
        try:
            svc._config_path.write_text(json.dumps({"UI": {}}), encoding="utf-8")
            svc._config_dirty = True
            svc._ext_dirty = {}
            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(svc, "_upload_unit", return_value=True) as mock_unit:
                    svc._do_upload()
            called_units = [c[0][0] for c in mock_unit.call_args_list]
            assert "app.config" in called_units
            assert "ext:assistant_hub" not in called_units
        finally:
            cs._sync_content_providers.pop("assistant_hub", None)


class TestConfigMergeLww:
    """app.config 的 LWW 合并（用户报障：删掉的服务商同步后复活）"""

    def test_remote_provider_delete_propagates(self, svc):
        local_meta = {
            "version": 1,
            "clock": 3,
            "entries": {
                "LLM.SavedProviders.p1": {"h": "h1", "t": 2},
                "LLM.SavedProviders.p2": {"h": "h2", "t": 2},
            },
        }
        remote_meta = {
            "version": 1,
            "clock": 9,
            "entries": {
                "LLM.SavedProviders.p1": {"h": "h1", "t": 2},
                "LLM.SavedProviders.p2": {"h": "h2", "t": 9, "d": True},
            },
        }
        local = {
            "LLM": {"SavedProviders": {"p1": {"name": "a"}, "p2": {"name": "b"}}},
            "System": {"SyncMeta": json.dumps(local_meta)},
        }
        remote = {
            "LLM": {"SavedProviders": {"p1": {"name": "a"}}},
            "System": {"SyncMeta": json.dumps(remote_meta)},
        }
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")

        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert set(data["LLM"]["SavedProviders"]) == {"p1"}
        state = json.loads(data["System"]["SyncMeta"])
        assert state["entries"]["LLM.SavedProviders.p2"]["d"] is True
        assert int(state["clock"]) >= 9

    def test_remote_newer_value_taken(self, svc):
        local = {
            "UI": {"FontSize": "12"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 2, "entries": {"UI.FontSize": {"h": "a", "t": 2}}}
                )
            },
        }
        remote = {
            "UI": {"FontSize": "18"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 7, "entries": {"UI.FontSize": {"h": "b", "t": 7}}}
                )
            },
        }
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")

        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "18"

    def test_without_meta_keeps_local_and_archives_remote(self, svc):
        """两端都没有 SyncMeta（老版本抹掉）→ 本地优先，远端值进备份留档"""
        local = {"UI": {"FontSize": "12"}}
        remote = {"UI": {"FontSize": "18"}}
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")

        svc._apply_config_merge(json.dumps(remote).encode("utf-8"))

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "12"
        assert "SyncMeta" in data["System"]

    def test_remote_authority_ignores_local_higher_ts(self, svc):
        """无合并基准时云端权威：本地旧快照的 t 再大也不能覆盖云端（解绑重绑事故）"""
        local = {
            "UI": {"FontSize": "12"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 99, "entries": {"UI.FontSize": {"h": "a", "t": 99}}}
                )
            },
        }
        remote = {
            "UI": {"FontSize": "18"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 7, "entries": {"UI.FontSize": {"h": "b", "t": 7}}}
                )
            },
        }
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")

        svc._apply_config_merge(json.dumps(remote).encode("utf-8"), remote_authority=True)

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "18"
        state = json.loads(data["System"]["SyncMeta"])
        assert state["entries"]["UI.FontSize"] == {"h": "b", "t": 7}

    def test_remote_authority_lifts_zero_ts(self, svc):
        """云端权威合并后本地独有键必须拿到正时间戳，留 t=0 会让两端平局各自为政"""
        local = {"UI": {"FontSize": "12", "OnlyLocal": 1}}
        remote = {"UI": {"FontSize": "18"}}
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")

        svc._apply_config_merge(json.dumps(remote).encode("utf-8"), remote_authority=True)

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        entries = json.loads(data["System"]["SyncMeta"])["entries"]
        assert entries["UI.OnlyLocal"]["t"] > 0
        assert entries["UI.FontSize"]["t"] > 0

    def test_download_without_base_is_remote_authoritative(self, svc):
        """没有合并基准（解绑重绑）→ 下载合并以云端为权威，本地旧值不赢"""
        local = {
            "UI": {"FontSize": "12"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 99, "entries": {"UI.FontSize": {"h": "a", "t": 99}}}
                )
            },
        }
        remote = {
            "UI": {"FontSize": "18"},
            "System": {
                "SyncMeta": json.dumps(
                    {"version": 1, "clock": 7, "entries": {"UI.FontSize": {"h": "b", "t": 7}}}
                )
            },
        }
        svc._config_path.write_text(json.dumps(local), encoding="utf-8")
        svc._config_remote_sha = None

        with patch.object(
            svc, "_read_remote_bytes", return_value=(json.dumps(remote).encode("utf-8"), "cloud_sha")
        ):
            assert svc._download_config_merge() is True

        data = json.loads(svc._config_path.read_text(encoding="utf-8"))
        assert data["UI"]["FontSize"] == "18"


class TestDownloadMerge:
    def test_download_merge_keeps_local_only_files(self, svc, tmp_path):
        """下载不整包覆盖：本地独有文件与本地改动都保留"""
        # 本地：a.txt 改过 + 独有 mine.txt
        (svc._user_custom_path / "a.txt").write_text("local-edit", encoding="utf-8")
        (svc._user_custom_path / "mine.txt").write_text("only-here", encoding="utf-8")

        # 远端：a.txt 未改，新增 remote.txt
        remote_dir = tmp_path / "r"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("old", encoding="utf-8")
        (remote_dir / "remote.txt").write_text("from-cloud", encoding="utf-8")

        with patch.object(
            svc,
            "_read_remote_bytes",
            return_value=(fp.build_stable_zip(remote_dir), "cloud_sha"),
        ):
            assert svc._download_user_custom_merge() is True

        assert (svc._user_custom_path / "a.txt").read_text(encoding="utf-8") == "local-edit"
        assert (svc._user_custom_path / "mine.txt").read_text(encoding="utf-8") == "only-here"
        assert (svc._user_custom_path / "remote.txt").read_text(encoding="utf-8") == "from-cloud"

    def test_download_merge_records_base_content_hash(self, svc, tmp_path):
        """合并后 content_hash 记录「远端那份内容」的指纹，本地独有改动仍会被上传"""
        remote_dir = tmp_path / "r2"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("cloud", encoding="utf-8")
        zip_bytes = fp.build_stable_zip(remote_dir)
        with patch.object(svc, "_read_remote_bytes", return_value=(zip_bytes, "cloud_sha")):
            svc._download_user_custom_merge()
        assert svc._content_hashes["user-custom"] == fp.manifest_digest(fp.dir_manifest(remote_dir))
        # 本地独有文件存在 → 指纹与基准不同 → 会被判定为有未同步改动
        (svc._user_custom_path / "local.txt").write_text("x", encoding="utf-8")
        assert svc._has_local_pending_changes() is True

    def test_dir_remote_tombstone_deletes_local_file(self, svc, tmp_path):
        """远端墓碑 t 更大 → 本地文件被删（换机后删除也能传播）"""
        from app.core.sync.lww import dir_meta_payload

        local_a = svc._user_custom_path / "a.txt"
        local_b = svc._user_custom_path / "b.txt"
        local_a.write_text("a", encoding="utf-8")
        local_b.write_text("b", encoding="utf-8")
        h_a = fp.file_fingerprint(local_a)
        h_b = fp.file_fingerprint(local_b)
        svc._save_dir_meta(
            "user-custom",
            {"a.txt": {"h": h_a, "t": 1}, "b.txt": {"h": h_b, "t": 1}},
            1,
        )

        remote_dir = tmp_path / "rdel"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("a", encoding="utf-8")
        meta = dir_meta_payload(
            {"a.txt": {"h": h_a, "t": 1}, "b.txt": {"h": h_b, "t": 9, "d": True}},
            9,
        )
        zip_bytes = fp.build_stable_zip(remote_dir, sync_meta=meta)
        with patch.object(svc, "_read_remote_bytes", return_value=(zip_bytes, "sha2")):
            assert svc._download_user_custom_merge() is True

        assert local_a.exists()
        assert not local_b.exists()

    def test_dir_meta_entry_not_extracted(self, svc, tmp_path):
        """包内元数据条目只用于合并，绝不落到用户目录（会被插件扫描）"""
        remote_dir = tmp_path / "rmeta"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("a", encoding="utf-8")
        zip_bytes = fp.build_stable_zip(remote_dir, sync_meta='{"version":1,"clock":1,"entries":{}}')
        with patch.object(svc, "_read_remote_bytes", return_value=(zip_bytes, "sha3")):
            svc._download_user_custom_merge()
        assert not (svc._user_custom_path / ".__drifox_sync__.json").exists()
        assert (svc._user_custom_path / "a.txt").exists()

    def test_dir_local_delete_not_resurrected(self, svc, tmp_path):
        """本地删了文件并写墓碑，远端仍是旧版 → 不复活"""
        from app.core.sync.lww import dir_meta_payload

        h_a = "0" * 64
        svc._save_dir_meta(
            "user-custom",
            {"a.txt": {"h": h_a, "t": 5, "d": True}},
            5,
        )

        remote_dir = tmp_path / "rres"
        remote_dir.mkdir()
        (remote_dir / "a.txt").write_text("old", encoding="utf-8")
        meta = dir_meta_payload({"a.txt": {"h": fp.file_fingerprint(remote_dir / "a.txt"), "t": 1}}, 1)
        zip_bytes = fp.build_stable_zip(remote_dir, sync_meta=meta)
        with patch.object(svc, "_read_remote_bytes", return_value=(zip_bytes, "sha4")):
            assert svc._download_user_custom_merge() is True

        assert not (svc._user_custom_path / "a.txt").exists()

    def test_dir_empty_meta_follows_remote_tombstone(self, svc, tmp_path):
        """本地无条目表（重绑/清缓存/首次升级）+ 远端已删 → 跟随删除

        回归：空表时 reconcile 曾把本地现存文件打成高于墓碑的 live t，
        墓碑在 LWW 中必输，对端删除永远不传播（2026-10-31 user-custom
        commands 删除未同步）。本地独有且远端无记录的文件必须保留。
        """
        from app.core.sync.lww import dir_meta_payload

        cmd_dir = svc._user_custom_path / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "team.md").write_text("cmd", encoding="utf-8")
        (svc._user_custom_path / "mine.txt").write_text("local only", encoding="utf-8")

        # 远端：commands/team.md 已被删除端删掉（墓碑），mine.txt 仍在
        remote_dir = tmp_path / "rtomb"
        remote_dir.mkdir()
        (remote_dir / "mine.txt").write_text("local only", encoding="utf-8")
        meta = dir_meta_payload(
            {
                "mine.txt": {"h": fp.file_fingerprint(remote_dir / "mine.txt"), "t": 1},
                "commands/team.md": {"h": "a" * 64, "t": 3, "d": True},
            },
            3,
        )
        zip_bytes = fp.build_stable_zip(remote_dir, sync_meta=meta)
        with patch.object(svc, "_read_remote_bytes", return_value=(zip_bytes, "sha5")):
            assert svc._download_user_custom_merge() is True

        assert not (svc._user_custom_path / "commands" / "team.md").exists()
        assert (svc._user_custom_path / "mine.txt").exists()
        # 墓碑必须进本地条目表持久化，否则下次上传会把它反杀回云端
        saved = svc._load_dir_meta("user-custom").get("entries") or {}
        assert saved.get("commands/team.md", {}).get("d") is True
