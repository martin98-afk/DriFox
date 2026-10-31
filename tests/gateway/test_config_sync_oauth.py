# -*- coding: utf-8 -*-
"""
ConfigSync 与 Gitee OAuth 的闭环集成测试（云端 single source of truth）

核心验证点：
  - 下载/读取路径**严禁刷新** refresh_token（否则旋转云端 RT → 误清绑）
  - 旋转 RT 只发生在「下载云端 RT 之后刷新并上传」或「access_token 过期本地刷新并上传」
  - 刷新失败区分 invalid_grant（真失效，清绑）与网络异常（保留绑定）
  - Gitee invalid_grant 被标记为 TOKEN_REVOKED::

Run: pytest tests/gateway/test_config_sync_oauth.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("app.core.sync.config_sync")

from app.core.sync.config_sync import ConfigSyncService


@pytest.fixture
def svc_and_mocks(monkeypatch):
    """绕过 __init__ 构造实例，注入可控制的 mock，避免真实网络/文件 IO"""
    svc = ConfigSyncService.__new__(ConfigSyncService)
    svc._config_dirty = False
    svc._suppress_until = 999.0  # 模拟下载后设定的上传抑制窗口
    svc.syncDone = MagicMock()
    svc.stateChanged = MagicMock()

    mock_backend = MagicMock()
    mock_cfg = MagicMock()

    monkeypatch.setattr(
        "app.gateway.auth.gitee.GiteeOAuthBackend",
        lambda: mock_backend,
    )
    monkeypatch.setattr(
        "app.utils.config.Settings.get_instance",
        lambda: mock_cfg,
    )

    # svc 上的依赖方法全部 mock，专注验证闭环决策逻辑
    svc._do_download = MagicMock(return_value=True)
    svc._do_upload = MagicMock(return_value=True)
    svc._load_gitee_token_from_disk = MagicMock()

    return svc, mock_backend, mock_cfg


class TestRefreshAfterDownload:
    def test_reloads_local_token_only(self, svc_and_mocks):
        """下载后只从磁盘恢复本地 token：不刷新、不上传

        token 是设备本地独立凭证，云端 app.config 不含 token 段。历史版本
        「用云端 RT 刷新并回传」会让多设备共享同一份 refresh_token 互相旋转作废，
        已废止。
        """
        svc, backend, cfg = svc_and_mocks
        cfg.gitee_bound = MagicMock(value=True)

        svc._refresh_and_upload_after_download()

        svc._load_gitee_token_from_disk.assert_called_once_with(cfg)
        backend._ensure_valid_token.assert_not_called()
        svc._do_upload.assert_not_called()
        svc._do_download.assert_not_called()
        assert svc._suppress_until == 999.0  # 抑制窗口原样保留，不解除

    def test_unbound_skips_reload(self, svc_and_mocks):
        """未绑定：直接返回，不去读磁盘 token"""
        svc, backend, cfg = svc_and_mocks
        cfg.gitee_bound = MagicMock(value=False)

        svc._refresh_and_upload_after_download()

        svc._load_gitee_token_from_disk.assert_not_called()
        svc._do_upload.assert_not_called()


class TestInitialSyncOrdering:
    """验证 _initial_sync 的关键顺序：下载前绝不刷新 refresh_token"""

    @pytest.fixture
    def svc_initial(self, monkeypatch):
        svc = ConfigSyncService.__new__(ConfigSyncService)
        svc._config_dirty = False
        svc._suppress_until = 999.0
        svc._initial_sync_completed = False
        svc._pending_sync_message = None
        svc._state = "idle"
        svc.syncDone = MagicMock()
        svc.stateChanged = MagicMock()

        mock_backend = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.gitee_bound = MagicMock(value=True)
        mock_cfg.gitee_user_token = MagicMock(value="at_local")
        mock_cfg.gitee_user_owner = MagicMock(value="owner")
        mock_cfg.gitee_token_expires_at = MagicMock(value=9999999999.0)  # 有效

        monkeypatch.setattr("app.gateway.auth.gitee.GiteeOAuthBackend", lambda: mock_backend)
        monkeypatch.setattr("app.utils.config.Settings.get_instance", lambda: mock_cfg)

        # 记录 _sync_token 是否被调用（下载前刷新是本次要根治的误清绑根因）
        svc._sync_token = MagicMock(return_value=True)
        svc._prepare_read_token = MagicMock(return_value=True)
        svc._check_remote = MagicMock(return_value=True)
        svc._do_download = MagicMock(return_value=True)
        svc._refresh_and_upload_after_download = MagicMock()
        svc._refresh_local_and_upload = MagicMock()
        svc._do_upload = MagicMock(return_value=True)
        svc._mark_dir_meta_migration = MagicMock(return_value=False)

        return svc, mock_backend, mock_cfg

    def test_valid_token_downloads_without_refresh(self, svc_initial):
        """token 有效 + 远端有配置：直接下载，下载前绝不刷新 refresh_token"""
        svc, backend, cfg = svc_initial

        svc._initial_sync()

        # 关键修复点：下载前绝不调用 _sync_token（避免旋转云端 RT）
        assert svc._sync_token.call_count == 0
        svc._prepare_read_token.assert_called()
        svc._do_download.assert_called_once()
        svc._refresh_and_upload_after_download.assert_called_once()
        svc._refresh_local_and_upload.assert_not_called()

    def test_expired_token_local_refresh_upload(self, svc_initial):
        """access_token 过期 + 远端有配置：无法读取 → 本地刷新使本地权威并上传（不下载覆盖）"""
        svc, backend, cfg = svc_initial
        cfg.gitee_token_expires_at.value = 0.0  # 过期

        svc._initial_sync()

        svc._do_download.assert_not_called()  # 不下载（读不了）
        svc._refresh_and_upload_after_download.assert_not_called()
        svc._refresh_local_and_upload.assert_called_once()  # 本地刷新 + 上传

    def test_remote_empty_uploads_current(self, svc_initial):
        """远端无配置：直接上传当前配置（token 有效时不刷新）"""
        svc, backend, cfg = svc_initial
        svc._check_remote.return_value = False

        svc._initial_sync()

        assert svc._sync_token.call_count == 0  # token 有效无需刷新
        svc._do_download.assert_not_called()
        svc._do_upload.assert_called_once_with(initial_sync=True)

    def test_unbound_skips_sync(self, svc_initial):
        """未绑定：直接跳过，不下载不上传"""
        svc, backend, cfg = svc_initial
        cfg.gitee_bound.value = False

        svc._initial_sync()

        svc._do_download.assert_not_called()
        svc._do_upload.assert_not_called()
        svc._refresh_local_and_upload.assert_not_called()

    def test_network_unknown_skips_upload(self, svc_initial):
        """远端状态无法确认（网络异常）：保守跳过，不上传不下载"""
        svc, backend, cfg = svc_initial
        svc._check_remote.return_value = None

        svc._initial_sync()

        svc._do_download.assert_not_called()
        svc._do_upload.assert_not_called()


class TestTokenRevokedDetection:
    def test_classification(self, svc_and_mocks):
        svc, backend, cfg = svc_and_mocks
        assert svc._token_revoked("TOKEN_REVOKED::invalid_grant") is True
        assert svc._token_revoked("刷新 Token 网络异常：超时") is False
        assert svc._token_revoked("") is False
        assert svc._token_revoked(None) is False


class TestRefreshAccessTokenErrors:
    def test_invalid_grant_marked_revoked(self, monkeypatch):
        import app.gateway.auth.gitee as g

        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.json.return_value = {"error": "invalid_grant", "error_description": "bad"}

        with patch("app.gateway.auth.gitee.requests.post", return_value=fake_resp):
            data, err = g.refresh_access_token("rt", "cid", "sec")

        assert data is None
        assert "TOKEN_REVOKED::" in err

    def test_network_error_not_marked_revoked(self, monkeypatch):
        import app.gateway.auth.gitee as g

        with patch("app.gateway.auth.gitee.requests.post", side_effect=Exception("network down")):
            data, err = g.refresh_access_token("rt", "cid", "sec")

        assert data is None
        assert "TOKEN_REVOKED::" not in err
        assert "网络异常" in err
