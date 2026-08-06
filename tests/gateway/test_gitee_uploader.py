# -*- coding: utf-8 -*-
"""
GiteeUploader 单元测试

覆盖:
  - 单例模式
  - _ensure_config (绑定账号/共享仓库)
  - reset_config
  - is_configured
  - upload_file (文件不存在/文件上传成功)
  - upload_bytes (核心上传逻辑)
  - _parse_error
  - 便捷函数 get_gitee_uploader / upload_to_gitee

Run: pytest tests/gateway/test_gitee_uploader.py -v
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from unittest import mock
import time

import pytest

pytest.importorskip("app.gateway.utils.gitee_uploader")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试前重置 GiteeUploader 单例"""
    from app.gateway.utils.gitee_uploader import GiteeUploader

    old = GiteeUploader._instance
    GiteeUploader._instance = None
    yield
    GiteeUploader._instance = old


@pytest.fixture
def uploader():
    """获取干净的 GiteeUploader 实例"""
    from app.gateway.utils.gitee_uploader import GiteeUploader

    return GiteeUploader.get_instance()


@pytest.fixture
def mock_settings_bound():
    """模拟已绑定 OAuth 账号的配置（Settings 在方法内 import，需 patch 在 app.utils.config 层级）"""
    with patch("app.utils.config.Settings.get_instance") as mock_get:
        cfg = MagicMock()
        cfg.gitee_bound.value = True
        cfg.gitee_user_token.value = "user_token_bound"
        cfg.gitee_user_refresh_token.value = "refresh_token_bound"
        cfg.gitee_token_expires_at.value = time.time() + 86400 * 30  # 30天后过期，不用刷新
        cfg.gitee_user_owner.value = "bound_user"
        cfg.gitee_user_repo.value = "DriFox_uploads"
        cfg.gitee_oauth_client_id.value = "test_client_id"
        cfg.gitee_oauth_client_secret.value = "test_client_secret"
        cfg.gitee_token.value = "share_token"
        cfg.gitee_owner.value = "share_owner"
        cfg.gitee_repo.value = "DriFox_share"
        cfg.gitee_path.value = "drifox"
        cfg.gitee_branch.value = "master"
        cfg.file = Path("dummy_config.json")
        mock_get.return_value = cfg
        yield cfg


@pytest.fixture
def mock_settings_unbound():
    """模拟未绑定时回退到共享仓库的配置"""
    with patch("app.utils.config.Settings.get_instance") as mock_get:
        cfg = MagicMock()
        cfg.gitee_bound.value = False
        cfg.gitee_user_token.value = ""
        cfg.gitee_user_owner.value = ""
        cfg.gitee_user_repo.value = "DriFox_uploads"
        cfg.gitee_token.value = "share_token"
        cfg.gitee_owner.value = "share_owner"
        cfg.gitee_repo.value = "DriFox_share"
        cfg.gitee_path.value = "drifox"
        cfg.gitee_branch.value = "master"
        mock_get.return_value = cfg
        yield cfg


@pytest.fixture
def temp_file(tmp_path):
    """临时测试文件"""
    f = tmp_path / "test_image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return str(f)


# =============================================================================
# 1. 单例测试
# =============================================================================


class TestSingleton:
    """单例模式"""

    def test_get_instance_returns_same(self):
        from app.gateway.utils.gitee_uploader import GiteeUploader

        u1 = GiteeUploader.get_instance()
        u2 = GiteeUploader.get_instance()
        assert u1 is u2

    def test_can_instantiate_directly(self):
        """GiteeUploader 允许直接 __init__（无单例保护）"""
        from app.gateway.utils.gitee_uploader import GiteeUploader

        u = GiteeUploader()
        assert u is not None


# =============================================================================
# 2. _ensure_config 测试
# =============================================================================


class TestEnsureConfig:
    """配置加载"""

    def test_uses_bound_account_first(self, uploader, mock_settings_bound):
        """绑定时使用用户绑定账号"""
        ok = uploader._ensure_config()
        assert ok is True
        assert uploader._token == "user_token_bound"
        assert uploader._owner == "bound_user"
        assert uploader._repo == "DriFox_uploads"

    def test_falls_back_to_shared(self, uploader, mock_settings_unbound):
        """未绑定时回退共享仓库"""
        ok = uploader._ensure_config()
        assert ok is True
        assert uploader._token == "share_token"
        assert uploader._owner == "share_owner"
        assert uploader._repo == "DriFox_share"

    def test_config_loaded_flag(self, uploader, mock_settings_bound):
        """加载后 _config_loaded 应为 True"""
        uploader._ensure_config()
        assert uploader._config_loaded is True

    def test_skip_reload_when_already_loaded(self, uploader, mock_settings_bound):
        """OAuth 模式下每次重新获取最新 token（支持自动刷新），不缓存旧值"""
        uploader._ensure_config()
        # OAuth 模式每次都调 get_bound_info() 拿最新 token
        # 只要 get_bound_info() 返回新值，_token 就会更新
        mock_settings_bound.gitee_user_token.value = "changed_token"
        uploader._ensure_config()
        # OAuth 模式下 token 会更新（因为 get_bound_info 每次都读 Settings）
        # 共享仓库模式才会缓存
        assert uploader._token == "changed_token"

    def test_config_load_failure(self, uploader):
        """配置加载异常时应返回 False"""
        with patch("app.utils.config.Settings.get_instance") as mock_get:
            mock_get.side_effect = Exception("config error")
            ok = uploader._ensure_config()
            assert ok is False


# =============================================================================
# 3. reset_config / is_configured 测试
# =============================================================================


class TestConfigManagement:
    """配置管理"""

    def test_reset_config_clears_cache(self, uploader, mock_settings_bound):
        """reset_config 应清除 _config_loaded 标记"""
        uploader._ensure_config()
        assert uploader._config_loaded is True

        uploader.reset_config()
        assert uploader._config_loaded is False

    def test_is_configured_when_ready(self, uploader, mock_settings_bound):
        """配置完整时 is_configured 返回 True"""
        assert uploader.is_configured() is True

    def test_is_configured_when_missing(self, uploader):
        """配置不完整时 is_configured 返回 False"""
        with patch("app.utils.config.Settings.get_instance") as mock_get:
            cfg = MagicMock()
            cfg.gitee_bound.value = False
            cfg.gitee_token.value = ""
            cfg.gitee_owner.value = ""
            mock_get.return_value = cfg
            assert uploader.is_configured() is False


# =============================================================================
# 4. upload_file 测试
# =============================================================================


class TestUploadFile:
    """文件上传"""

    def test_file_not_found(self, uploader, mock_settings_bound):
        """文件不存在应返回错误"""
        url, err = uploader.upload_file("/nonexistent/path/file.png")
        assert url is None
        assert err is not None
        assert "不存在" in err

    def test_path_is_directory(self, uploader, mock_settings_bound, tmp_path):
        """路径是目录应返回错误"""
        url, err = uploader.upload_file(str(tmp_path))
        assert url is None
        assert err is not None
        assert "不是文件" in err

    def test_upload_file_success(self, uploader, mock_settings_bound, temp_file):
        """文件上传成功应返回下载链接"""
        with patch.object(uploader, "upload_bytes") as mock_upload:
            mock_upload.return_value = ("https://gitee.com/download", None)

            url, err = uploader.upload_file(temp_file)
            assert url == "https://gitee.com/download"
            assert err is None
            # 验证 upload_bytes 被调用时传入了正确的数据
            mock_upload.assert_called_once()
            args, _ = mock_upload.call_args
            assert args[0]  # data
            assert args[1] == "test_image.png"  # filename

    def test_upload_file_read_error(self, uploader, mock_settings_bound):
        """无法读取文件应返回错误"""
        url, err = uploader.upload_file(r"C:\Windows\System32\config\SAM")
        assert url is None
        assert err is not None


# =============================================================================
# 5. upload_bytes 测试
# =============================================================================


class TestUploadBytes:
    """字节数据上传"""

    def test_not_configured(self, uploader):
        """未配置时应返回错误"""
        with patch.object(uploader, "_ensure_config", return_value=False):
            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is None
            assert err is not None
            assert "未配置" in err

    def test_successful_upload(self, uploader, mock_settings_bound):
        """上传成功应返回 download_url"""
        with (
            patch.object(uploader, "_ensure_config", return_value=True),
            patch.object(uploader, "_backend") as mock_backend,
        ):
            mock_backend.upload.return_value = True

            url, err = uploader.upload_bytes(b"test_data", "test.png")
            assert url is not None
            assert "https://gitee.com/" in url
            assert err is None

    def test_upload_fails_return_error(self, uploader, mock_settings_bound):
        """后台上传失败应返回错误"""
        with (
            patch.object(uploader, "_ensure_config", return_value=True),
            patch.object(uploader, "_backend") as mock_backend,
        ):
            mock_backend.upload.return_value = False

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is None
            assert err is not None

    def test_upload_http_error(self, uploader, mock_settings_bound):
        """后端异常应返回错误"""
        with (
            patch.object(uploader, "_ensure_config", return_value=True),
            patch.object(uploader, "_backend") as mock_backend,
        ):
            mock_backend.upload.side_effect = Exception("API error")

            url, err = uploader.upload_bytes(b"data", "test.bad")
            assert url is None
            assert err is not None

    def test_filename_inference(self, uploader, mock_settings_bound):
        """不传扩展名时从文件名推断"""
        with (
            patch.object(uploader, "_ensure_config", return_value=True),
            patch.object(uploader, "_backend") as mock_backend,
        ):
            mock_backend.upload.return_value = True

            url, err = uploader.upload_bytes(b"data", "photo.jpg")
            assert url is not None
            # 文件名应包含 .jpg
            assert ".jpg" in url

    def test_oauth_401_retry_fail_emits_token_invalid(self, uploader, mock_settings_bound):
        """OAuth 模式 401 且重试仍失败 → tokenInvalid 信号发出"""
        uploader._oauth_mode = True
        with (
            patch.object(uploader, "_ensure_config", side_effect=[True, True]),
            patch.object(uploader, "_backend") as mock_backend,
            patch.object(uploader, "tokenInvalid") as mock_signal,
        ):
            # 首次 401，重试仍 401
            mock_backend.upload.side_effect = [False, False]
            mock_backend.last_error = "[401] Access token is expired"

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is None
            assert err is not None
            mock_signal.emit.assert_called_once()

    def test_oauth_401_retry_success_no_token_invalid(self, uploader, mock_settings_bound):
        """OAuth 模式 401 但刷新重试成功 → 不发出 tokenInvalid"""
        uploader._oauth_mode = True
        with (
            patch.object(uploader, "_ensure_config", side_effect=[True, True]),
            patch.object(uploader, "_backend") as mock_backend,
            patch.object(uploader, "tokenInvalid") as mock_signal,
        ):
            # 首次 401，重试成功
            mock_backend.upload.side_effect = [False, True]
            mock_backend.last_error = "[401] Access token is expired"

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is not None
            mock_signal.emit.assert_not_called()

    def test_oauth_401_config_fail_recovers_from_cloud(self, uploader, mock_settings_bound):
        """401 后本地刷新失败（RT 被其他设备轮换）→ 云端恢复成功 → 重试上传成功"""
        uploader._oauth_mode = True
        fake_svc = mock.Mock()
        fake_svc.recover_token_from_cloud.return_value = True
        with (
            patch.object(uploader, "_ensure_config", side_effect=[True, False, True]),
            patch.object(uploader, "_backend") as mock_backend,
            patch.object(uploader, "tokenInvalid") as mock_signal,
            patch("app.core.config_sync.ConfigSyncService.get_instance", return_value=fake_svc),
        ):
            # 首次 401；云端恢复后重试上传成功
            mock_backend.upload.side_effect = [False, True]
            mock_backend.last_error = "[401] Access token is expired"

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is not None, f"云端恢复后应重试成功, err={err}"
            assert err is None
            fake_svc.recover_token_from_cloud.assert_called_once()
            mock_signal.emit.assert_not_called()

    def test_shared_mode_401_no_token_invalid(self, uploader, mock_settings_bound):
        """共享仓库模式（非 OAuth）401 → 不发出 tokenInvalid（勿误报）"""
        uploader._oauth_mode = False
        with (
            patch.object(uploader, "_ensure_config", side_effect=[True, True]),
            patch.object(uploader, "_backend") as mock_backend,
            patch.object(uploader, "tokenInvalid") as mock_signal,
        ):
            mock_backend.upload.side_effect = [False, False]
            mock_backend.last_error = "[401] Access token is expired"

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is None
            mock_signal.emit.assert_not_called()

    def test_network_error_no_token_invalid(self, uploader, mock_settings_bound):
        """网络类错误（非 401）→ 不发出 tokenInvalid（勿误报）"""
        uploader._oauth_mode = True
        with (
            patch.object(uploader, "_ensure_config", return_value=True),
            patch.object(uploader, "_backend") as mock_backend,
            patch.object(uploader, "tokenInvalid") as mock_signal,
        ):
            mock_backend.upload.return_value = False
            mock_backend.last_error = "[502] Bad Gateway"

            url, err = uploader.upload_bytes(b"data", "test.png")
            assert url is None
            mock_signal.emit.assert_not_called()


# =============================================================================
# 6. _parse_error 测试
# =============================================================================


class TestParseError:
    """错误响应解析（迁移自 GiteeUploader，现由 GiteeContentBackend 承载）"""

    def test_parse_error_json(self, uploader):
        """JSON 响应应提取 message"""
        from app.gateway.utils.gitee_uploader import GiteeContentBackend

        resp = MagicMock()
        resp.json.return_value = {"message": "Not Found"}
        assert GiteeContentBackend._parse_error(resp) == "Not Found"

    def test_parse_error_fallback(self, uploader):
        """非 JSON 响应回退到 text"""
        from app.gateway.utils.gitee_uploader import GiteeContentBackend

        resp = MagicMock()
        resp.json.side_effect = Exception("bad json")
        resp.text = "raw error text"
        assert GiteeContentBackend._parse_error(resp) == "raw error text"


# =============================================================================
# 7. 便捷函数测试
# =============================================================================


class TestConvenienceFunctions:
    """get_gitee_uploader / upload_to_gitee"""

    def test_get_gitee_uploader_returns_singleton(self):
        from app.gateway.utils.gitee_uploader import GiteeUploader, get_gitee_uploader

        u = get_gitee_uploader()
        assert isinstance(u, GiteeUploader)
        assert u is GiteeUploader.get_instance()

    def test_upload_to_gitee_delegates(self, mock_settings_bound, temp_file):
        from app.gateway.utils.gitee_uploader import upload_to_gitee

        # mock 后台上传
        with patch("app.gateway.utils.gitee_uploader.GiteeUploader.upload_file") as mock_uf:
            mock_uf.return_value = ("https://dl", None)
            url, err = upload_to_gitee(temp_file)
            assert url == "https://dl"
            mock_uf.assert_called_once_with(temp_file)
