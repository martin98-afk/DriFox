# -*- coding: utf-8 -*-
"""云同步三方合并与内容指纹测试。

覆盖三条核心约束：
1. 内容没变 → 零网络请求，不产生空 commit；
2. 远端被别的设备改过 → 先合并，绝不覆盖；
3. 冲突 → 本地优先 + 远端副本留档，双方内容都不丢。

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
from app.core.sync.merge import (
    LOCAL_ONLY_CONFIG_KEYS,
    conflict_name,
    is_conflict_copy,
    merge_config_dicts,
    merge_dir_manifests,
)


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
# 2. 目录三方合并
# =============================================================================


class TestDirMerge:
    def test_remote_change_taken_when_local_untouched(self):
        plan = merge_dir_manifests(
            base={"a": "h1"},
            local={"a": "h1"},
            remote={"a": "h2"},
        )
        assert plan.take_remote == ["a"]

    def test_local_change_kept_when_remote_untouched(self):
        plan = merge_dir_manifests(
            base={"a": "h1"},
            local={"a": "h2"},
            remote={"a": "h1"},
        )
        assert plan.take_remote == []
        assert plan.conflicts == []

    def test_both_changed_is_conflict(self):
        plan = merge_dir_manifests(
            base={"a": "h1"},
            local={"a": "h2"},
            remote={"a": "h3"},
        )
        assert plan.conflicts == ["a"]

    def test_remote_new_file_added(self):
        plan = merge_dir_manifests(base={}, local={}, remote={"new": "h1"})
        assert plan.take_remote == ["new"]

    def test_local_new_file_preserved(self):
        plan = merge_dir_manifests(base={}, local={"mine": "h1"}, remote={})
        assert plan.local_added == ["mine"]

    def test_remote_delete_propagates_when_local_untouched(self):
        plan = merge_dir_manifests(base={"a": "h1"}, local={"a": "h1"}, remote={})
        assert plan.delete_local == ["a"]

    def test_remote_delete_keeps_locally_modified(self):
        """远端删了但本地改过 → 保留本地，不静默删掉用户内容"""
        plan = merge_dir_manifests(base={"a": "h1"}, local={"a": "h2"}, remote={})
        assert plan.keep_deleted_local == ["a"]
        assert plan.delete_local == []

    def test_local_delete_not_resurrected(self):
        """本地删除、远端未改 → 不要把文件补回来"""
        plan = merge_dir_manifests(base={"a": "h1"}, local={}, remote={"a": "h1"})
        assert plan.take_remote == []

    def test_local_delete_but_remote_changed_takes_remote(self):
        plan = merge_dir_manifests(base={"a": "h1"}, local={}, remote={"a": "h2"})
        assert plan.take_remote == ["a"]

    def test_conflict_copy_naming(self):
        assert conflict_name("sub/a.json", "mach-1a2b", "20261028-171500") == (
            "sub/a.remote-mach-1a2b-20261028-171500.json"
        )
        assert is_conflict_copy("sub/a.remote-mach-1a2b-20261028-171500.json")
        assert not is_conflict_copy("sub/a.json")


# =============================================================================
# 3. app.config 三方合并
# =============================================================================


def _cfg(**values):
    """构造 qfluentwidgets 形态的分组配置"""
    return {k.split("__")[0]: {k.split("__")[1]: {"value": v}} for k, v in values.items()}


class TestConfigMerge:
    def test_scalar_merge_takes_changed_side(self):
        base = {"UI": {"FontSize": {"value": "12"}, "ChatWidth": {"value": "800"}}}
        local = {"UI": {"FontSize": {"value": "14"}, "ChatWidth": {"value": "800"}}}
        remote = {"UI": {"FontSize": {"value": "12"}, "ChatWidth": {"value": "900"}}}
        merged, conflicts, keys = merge_config_dicts(base, local, remote)
        assert merged["UI"]["FontSize"]["value"] == "14"
        assert merged["UI"]["ChatWidth"]["value"] == "900"
        assert conflicts == {}
        assert keys == []

    def test_saved_providers_merge_at_entry_level(self):
        """双机各自新增 provider → 两侧都保留（整块合并会丢一边）"""
        base = {"LLM": {"SavedProviders": {"value": {"p1": {"API_KEY": "", "model": "x"}}}}}
        local = {
            "LLM": {
                "SavedProviders": {
                    "value": {
                        "p1": {"API_KEY": "KEY_LOCAL", "model": "x"},
                        "p2": {"API_KEY": "K2", "model": "y"},
                    }
                }
            }
        }
        remote = {
            "LLM": {
                "SavedProviders": {
                    "value": {
                        "p1": {"API_KEY": "KEY_REMOTE", "model": "x"},
                        "p3": {"API_KEY": "K3", "model": "z"},
                    }
                }
            }
        }
        merged, conflicts, keys = merge_config_dicts(base, local, remote)
        providers = merged["LLM"]["SavedProviders"]["value"]
        assert set(providers) == {"p1", "p2", "p3"}
        # 密钥本机权威：本地已有的 API_KEY 永不被云端覆盖
        assert providers["p1"]["API_KEY"] == "KEY_LOCAL"
        assert any("API_KEY" in k for k in keys)

    def test_api_key_taken_from_remote_when_local_empty(self):
        """新机器本地密钥为空 → 接收云端密文（保留可解密的机会）"""
        base = {"LLM": {"SavedProviders": {"value": {"p1": {"API_KEY": ""}}}}}
        local = {"LLM": {"SavedProviders": {"value": {"p1": {"API_KEY": ""}}}}}
        remote = {"LLM": {"SavedProviders": {"value": {"p1": {"API_KEY": "enc:v2:abc"}}}}}
        merged, _, keys = merge_config_dicts(base, local, remote)
        assert merged["LLM"]["SavedProviders"]["value"]["p1"]["API_KEY"] == "enc:v2:abc"
        assert keys == []

    def test_gitee_tokens_always_local(self):
        base = {"Gitee": {"UserToken": {"value": "old"}}}
        local = {"Gitee": {"UserToken": {"value": "local_token"}}}
        remote = {"Gitee": {"UserToken": {"value": "remote_token"}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        assert merged["Gitee"]["UserToken"]["value"] == "local_token"
        assert "UserToken" in LOCAL_ONLY_CONFIG_KEYS

    def test_mcp_servers_merge_by_element(self):
        base = {"MCP": {"Servers": {"value": [{"name": "a", "cmd": "1"}]}}}
        local = {"MCP": {"Servers": {"value": [{"name": "a", "cmd": "1"}, {"name": "b", "cmd": "2"}]}}}
        remote = {"MCP": {"Servers": {"value": [{"name": "a", "cmd": "1"}, {"name": "c", "cmd": "3"}]}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        names = [s["name"] for s in merged["MCP"]["Servers"]["value"]]
        assert names == ["a", "b", "c"]

    def test_config_item_not_deleted_when_remote_lacks_it(self):
        """老版本客户端重写 app.config 丢掉新 ConfigItem → 同步不得把它删掉

        回归：删除传播若作用在「配置项」层，一台旧版本的机器保存一次就会让
        新加的配置项在所有设备上一起消失。
        """
        base = {"UI": {"FontSize": {"value": "12"}, "NewThing": {"value": True}}}
        local = {"UI": {"FontSize": {"value": "12"}, "NewThing": {"value": True}}}
        remote = {"UI": {"FontSize": {"value": "12"}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        assert "NewThing" in merged["UI"]

    def test_nested_new_item_survives_group_level_shortcut(self):
        """整组只在远端变化时也不能短路：组内新增项必须保留"""
        base = {"MCP": {"Enabled": {"value": True}}}
        local = {"MCP": {"Enabled": {"value": True}, "Servers": {"value": [{"name": "a"}]}}}
        remote = {"MCP": {"Enabled": {"value": True}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        assert merged["MCP"]["Servers"]["value"] == [{"name": "a"}]

    def test_collection_element_delete_still_propagates(self):
        """集合元素（MCP 服务 / provider）的删除要传播（depth 3 起）"""
        base = {"MCP": {"Servers": {"value": [{"name": "a"}, {"name": "b"}]}}}
        local = {"MCP": {"Servers": {"value": [{"name": "a"}]}}}
        remote = {"MCP": {"Servers": {"value": [{"name": "a"}, {"name": "b"}]}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        assert [e["name"] for e in merged["MCP"]["Servers"]["value"]] == ["a"]

    def test_list_local_order_preserved(self):
        base = {"P": {"L": {"value": []}}}
        local = {"P": {"L": {"value": [{"name": "z"}, {"name": "y"}]}}}
        remote = {"P": {"L": {"value": [{"name": "z"}, {"name": "y"}, {"name": "x"}]}}}
        merged, _, _ = merge_config_dicts(base, local, remote)
        assert [e["name"] for e in merged["P"]["L"]["value"]] == ["z", "y", "x"]


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
        """内容指纹未变 → 不打包、不发请求（空上传根治）"""
        (svc._user_custom_path / "x.txt").write_text("v1", encoding="utf-8")
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
        payload = fp.build_stable_zip(svc._user_custom_path)
        svc._custom_remote_sha = fp.git_blob_sha(payload)
        client = MagicMock()

        ok = svc._upload_unit("user-custom", "drifox/user-custom.zip", svc._user_custom_path, client)

        assert ok is True
        assert client.put.call_count == 0
        assert client.post.call_count == 0

    def test_diverged_remote_is_merged_not_overwritten(self, svc, tmp_path):
        """远端被别的设备改过 → 先三方合并，冲突保留本地并生成远端副本"""
        # base：a.txt = old
        base_dir = tmp_path / "base_snapshot"
        base_dir.mkdir()
        (base_dir / "a.txt").write_text("old", encoding="utf-8")
        svc._save_base_manifest("user-custom", fp.dir_manifest(base_dir))
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
        assert not [p for p in svc._user_custom_path.iterdir() if is_conflict_copy(p.name)]
        archived = list((tmp_path / "conflicts").rglob("a.txt"))
        assert len(archived) == 1
        assert archived[0].read_text(encoding="utf-8") == "remote"
        # 合并后确实发起了写入（把合并结果推回云端）
        assert client.put.call_count == 1

    def test_conflict_archived_outside_business_dir(self, svc, tmp_path):
        """冲突副本不落业务目录（插件会误读），改落 conflicts/ 并推到远端独立目录"""
        base_dir = tmp_path / "base2"
        base_dir.mkdir()
        (base_dir / "a.txt").write_text("old", encoding="utf-8")
        svc._save_base_manifest("user-custom", fp.dir_manifest(base_dir))
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


class TestDownloadMerge:
    def test_download_merge_keeps_local_only_files(self, svc, tmp_path):
        """下载不整包覆盖：本地独有文件与本地改动都保留"""
        # base：a.txt=old
        base_dir = tmp_path / "b"
        base_dir.mkdir()
        (base_dir / "a.txt").write_text("old", encoding="utf-8")
        svc._save_base_manifest("user-custom", fp.dir_manifest(base_dir))

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
