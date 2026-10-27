# -*- coding: utf-8 -*-
"""net_sync 能力 provider 单测（net.avatar / net.download / sync.register）
+ upload_file 工具改走能力池（ctx.capabilities["net.upload"]）的用例。

Run: pytest tests/plugins/test_capability_net_sync.py -v
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]

pytest.importorskip("app.plugins.capabilities.providers.net_sync")

from app.plugins.capabilities.providers import net_sync


# ── 加载 interaction_tools（与 test_system_tools_mcp_render 同款按路径加载）──
_TOOLS_MODULE = _ROOT / "plugins" / "system-tools" / "tools" / "interaction_tools.py"
_spec = importlib.util.spec_from_file_location("test_net_sync_interaction_mod", str(_TOOLS_MODULE))
_tools = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_net_sync_interaction_mod", _tools)
_spec.loader.exec_module(_tools)


@pytest.fixture
def _avatar_cache_root(tmp_path, monkeypatch):
    """把头像缓存根目录指到 tmp（get_app_data_dir 在 net_sync 内按名导入）"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
    return tmp_path


# ══════════════════════════════════════════════════
# net.avatar
# ══════════════════════════════════════════════════


class TestNetAvatar:
    def test_shape(self):
        avatar = net_sync.net_avatar(None)
        assert set(avatar.keys()) == {"fetch", "cached_path"}
        assert all(callable(v) for v in avatar.values())

    def test_fetch_unbound_returns_none(self, _avatar_cache_root):
        with patch("app.gateway.auth.get_oauth_backend", return_value=MagicMock(get_bound_info=lambda: None)):
            assert net_sync.net_avatar(None)["fetch"]() is None

    def test_fetch_cache_hit_no_network(self, _avatar_cache_root):
        cache = _avatar_cache_root / "cache" / "gitee_avatar_me.png"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"png")
        backend = MagicMock(get_bound_info=lambda: {"owner": "me", "token": "t"})
        with patch("app.gateway.auth.get_oauth_backend", return_value=backend), patch(
            "app.gateway.auth.gitee.fetch_user_avatar_url"
        ) as mock_fetch, patch("requests.get") as mock_get:
            got = net_sync.net_avatar(None)["fetch"]()
        assert got == str(cache)
        mock_fetch.assert_not_called()
        mock_get.assert_not_called()

    def test_fetch_miss_downloads_and_caches(self, _avatar_cache_root):
        resp = MagicMock(status_code=200, content=b"avatar-bytes")
        backend = MagicMock(get_bound_info=lambda: {"owner": "me", "token": "t"})
        with patch("app.gateway.auth.get_oauth_backend", return_value=backend), patch(
            "app.gateway.auth.gitee.fetch_user_avatar_url", return_value=("https://av/me.png", "")
        ), patch("requests.get", return_value=resp):
            got = net_sync.net_avatar(None)["fetch"]()
        cache = _avatar_cache_root / "cache" / "gitee_avatar_me.png"
        assert got == str(cache)
        assert cache.read_bytes() == b"avatar-bytes"

    def test_fetch_download_failure_returns_none(self, _avatar_cache_root):
        resp = MagicMock(status_code=500, content=b"")
        backend = MagicMock(get_bound_info=lambda: {"owner": "me", "token": "t"})
        with patch("app.gateway.auth.get_oauth_backend", return_value=backend), patch(
            "app.gateway.auth.gitee.fetch_user_avatar_url", return_value=("https://av/me.png", "")
        ), patch("requests.get", return_value=resp):
            assert net_sync.net_avatar(None)["fetch"]() is None

    def test_cached_path_unbound(self, _avatar_cache_root):
        cfg = MagicMock()
        cfg.gitee_bound.value = False
        with patch("app.utils.config.Settings.get_instance", return_value=cfg):
            assert net_sync.net_avatar(None)["cached_path"]() is None

    def test_cached_path_hit(self, _avatar_cache_root):
        cache = _avatar_cache_root / "cache" / "gitee_avatar_me.png"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"png")
        cfg = MagicMock()
        cfg.gitee_bound.value = True
        cfg.gitee_user_owner.value = "me"
        with patch("app.utils.config.Settings.get_instance", return_value=cfg):
            assert net_sync.net_avatar(None)["cached_path"]() == str(cache)


# ══════════════════════════════════════════════════
# net.download / sync.register
# ══════════════════════════════════════════════════


class TestNetDownloadAndSyncRegister:
    def test_download_delegates_to_uploader(self):
        with patch("app.gateway.utils.gitee_uploader.get_gitee_uploader") as mock_get:
            mock_get.return_value.download_file.return_value = (r"C:\out\x.png", None)
            local, err = net_sync.net_download(None)("drifox/x.png", r"C:\out\x.png")
        assert local == r"C:\out\x.png" and err is None
        mock_get.return_value.download_file.assert_called_once_with("drifox/x.png", r"C:\out\x.png")

    def test_sync_register_delegates(self):
        captured = {}

        def _fake_register(**kwargs):
            captured.update(kwargs)
            return True

        with patch("app.core.sync.config_sync.register_sync_content_provider", side_effect=_fake_register):
            ok = net_sync.sync_register(None)(
                provider_id="p1", label="L", local_dir=r"D:\dir", remote_path="drifox/ext/p1.zip"
            )
        assert ok is True
        assert captured == {
            "provider_id": "p1",
            "label": "L",
            "local_dir": r"D:\dir",
            "remote_path": "drifox/ext/p1.zip",
            "enabled": True,
        }

    def test_sync_register_swallows_exception(self):
        with patch(
            "app.core.sync.config_sync.register_sync_content_provider", side_effect=RuntimeError("boom")
        ):
            assert net_sync.sync_register(None)(provider_id="p", local_dir="d") is False


# ══════════════════════════════════════════════════
# upload_file 工具：走能力池，不走 services 旧键
# ══════════════════════════════════════════════════


class TestUploadFileImpl:
    def test_uses_capability_pool(self):
        upload = MagicMock(return_value="OK")
        out = _tools._upload_file_impl(
            {"capabilities": {"net.upload": upload}, "services": {}}, local_path="a.png"
        )
        assert out == "OK"
        upload.assert_called_once_with(local_path="a.png")

    def test_unauthorized_returns_error(self):
        out = _tools._upload_file_impl({"capabilities": {}, "services": {}}, local_path="a.png")
        assert out.success is False
        assert "net.upload" in out.error

    def test_missing_ctx_returns_error(self):
        out = _tools._upload_file_impl({}, local_path="a.png")
        assert out.success is False
