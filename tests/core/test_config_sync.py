# -*- coding: utf-8 -*-
"""
ConfigSyncService 虚拟测试

覆盖所有绑定/解绑/同步/抑制窗口场景，无需真实 Gitee API。
所有 HTTP 调用、文件 I/O、定时器均被 mock。

Run: pytest tests/core/test_config_sync.py -v
"""

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, call, patch

import httpx
import pytest
from PyQt5.QtCore import QTimer
from qfluentwidgets.common.config import OptionsConfigItem, OptionsValidator


# =============================================================================
# Helpers
# =============================================================================


def make_httpx_response(status_code=200, json_data=None, text=""):
    """构造模拟的 httpx.Response"""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    if json_data is not None:
        mock.json.return_value = json_data
    mock.text = text
    return mock


def make_gitee_file_response(content_str: str, sha: str = "abc123"):
    """构造 Gitee API 返回的文件内容响应（base64 编码）"""
    import base64

    return make_httpx_response(
        status_code=200,
        json_data={
            "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
            "sha": sha,
        },
    )


@pytest.fixture(autouse=True)
def reset_sync_service():
    """每个测试前重置 ConfigSyncService 状态"""
    from app.core.config_sync import ConfigSyncService

    svc = ConfigSyncService.get_instance()
    # 重置所有实例状态
    svc._state = "disabled"
    svc._token = ""
    svc._owner = ""
    svc._config_dirty = False
    svc._custom_dirty = False
    svc._records_dirty = False
    svc._config_remote_sha = None
    svc._custom_remote_sha = None
    svc._records_remote_sha = None
    svc._initial_sync_completed = False
    svc._suppress_until = 0.0
    svc._suppress_retry_scheduled = False
    svc._debounce_timer = None
    # 停掉 watch 线程
    svc._stop_watching()
    yield svc


# =============================================================================
# 1. _check_remote_file — 远端存在性检查
# =============================================================================


class TestCheckRemoteFile:
    """_check_remote_file 远端文件存在性检查（核心安全守卫）"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        reset_sync_service._token = "valid_token"
        reset_sync_service._owner = "test_user"
        return reset_sync_service

    def test_returns_true_when_200_with_content(self, svc):
        """HTTP 200 + 有 content → True（文件存在）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(
                200, {"content": "abc", "sha": "s1"}
            )
            assert svc._check_remote_file("drifox/app.config") is True

    def test_returns_false_when_404(self, svc):
        """HTTP 404 → False（文件确定不存在）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(404)
            assert svc._check_remote_file("drifox/app.config") is False

    def test_returns_none_when_401(self, svc):
        """HTTP 401 → None（token 无效，不确定远端状态）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(401, text="Unauthorized")
            assert svc._check_remote_file("drifox/app.config") is None

    def test_returns_none_when_403(self, svc):
        """HTTP 403 → None（权限不足，不确定远端状态）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(403, text="Forbidden")
            assert svc._check_remote_file("drifox/app.config") is None

    def test_returns_none_when_500(self, svc):
        """HTTP 500 → None（服务端错误，不确定远端状态）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(500)
            assert svc._check_remote_file("drifox/app.config") is None

    def test_returns_none_when_token_empty(self, svc):
        """token 为空 → None"""
        svc._token = ""
        # 实现演进：_check_remote_file 改用 _prepare_read_token 载入 token（不刷新）
        with patch.object(svc, "_prepare_read_token", return_value=False):
            assert svc._check_remote_file("drifox/app.config") is None

    def test_returns_none_when_network_error(self, svc):
        """网络异常 → None"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.ConnectError("connection refused")
            assert svc._check_remote_file("drifox/app.config") is None


# =============================================================================
# 2. _sync_token — token 同步安全
# =============================================================================


class TestSyncToken:
    """_sync_token token 同步"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        return reset_sync_service

    def test_returns_true_on_valid_token(self, svc):
        """有效 token → 更新 self._token 并返回 True"""
        with patch("app.gateway.auth.gitee.GiteeOAuthBackend") as mock_backend_cls:
            mock_backend = MagicMock()
            mock_backend_cls.return_value = mock_backend
            mock_backend.is_bound.return_value = True
            mock_backend._ensure_valid_token.return_value = ("new_token", "")

            result = svc._sync_token()
            assert result is True
            assert svc._token == "new_token"

    def test_clears_token_on_refresh_failure(self, svc):
        """刷新失败 → 清空 self._token 并返回 False"""
        svc._token = "old_expired_token"
        with patch("app.gateway.auth.gitee.GiteeOAuthBackend") as mock_backend_cls:
            mock_backend = MagicMock()
            mock_backend_cls.return_value = mock_backend
            mock_backend.is_bound.return_value = True
            mock_backend._ensure_valid_token.return_value = (None, "refresh failed")

            result = svc._sync_token()
            assert result is False
            assert svc._token == ""  # 不再回退到过期 token

    def test_returns_false_when_not_bound(self, svc):
        """未绑定 → 返回 False"""
        with patch("app.gateway.auth.gitee.GiteeOAuthBackend") as mock_backend_cls:
            mock_backend = MagicMock()
            mock_backend_cls.return_value = mock_backend
            mock_backend.is_bound.return_value = False

            result = svc._sync_token()
            assert result is False
            assert svc._token == ""

    def test_clears_token_on_exception(self, svc):
        """异常时清空 token（不保留过期 token）"""
        svc._token = "old_token"
        with patch("app.gateway.auth.gitee.GiteeOAuthBackend") as mock_backend_cls:
            mock_backend = MagicMock()
            mock_backend_cls.return_value = mock_backend
            mock_backend.is_bound.side_effect = RuntimeError("unexpected error")

            result = svc._sync_token()
            assert result is False
            assert svc._token == ""  # 异常时 token 被清空


# =============================================================================
# 3. _do_upload — 上传安全守卫
# =============================================================================


class TestDoUpload:
    """_do_upload 上传逻辑"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._token = "valid_token"
        svc._owner = "test_user"
        svc._initial_sync_completed = True  # 默认已完成初始同步
        svc._config_dirty = True
        svc._config_path = MagicMock()
        svc._config_path.exists.return_value = True
        svc._config_path.read_bytes.return_value = b"config_data"
        svc._user_custom_path = MagicMock()
        svc._user_custom_path.exists.return_value = False  # 跳过 user-custom
        svc._records_path = MagicMock()
        svc._records_path.exists.return_value = False  # 跳过 share_records
        svc._upload_lock = threading.Lock()
        return svc

    def test_blocks_before_initial_sync_complete(self, svc):
        """初始同步未完成 → 禁止上传（不需要 initial_sync 参数时）"""
        svc._initial_sync_completed = False
        assert svc._do_upload() is False

    def test_allows_initial_sync_param(self, svc):
        """initial_sync=True 时绕过初始同步检查"""
        svc._initial_sync_completed = False
        with patch.object(svc, "_upload_file", return_value=True):
            with patch.object(svc, "_sync_token", return_value=True):
                assert svc._do_upload(initial_sync=True) is True

    def test_allows_upload_after_initial_sync(self, svc):
        """初始同步完成后可正常上传"""
        with patch.object(svc, "_sync_token", return_value=True):
            with patch.object(svc, "_upload_file", return_value=True):
                assert svc._do_upload() is True

    def test_skips_when_nothing_dirty(self, svc):
        """没有脏标记 → 跳过上传"""
        svc._config_dirty = False
        svc._custom_dirty = False
        svc._records_dirty = False
        with patch.object(svc, "_sync_token", return_value=True):
            assert svc._do_upload() is True  # 跳过但返回 True

    def test_returns_false_on_token_invalid(self, svc):
        """token 无效 → 返回 False"""
        with patch.object(svc, "_sync_token", return_value=False):
            assert svc._do_upload() is False

    def test_clears_dirty_flags_before_upload(self, svc):
        """上传前清空脏标记，避免重复触发"""
        with patch.object(svc, "_sync_token", return_value=True):
            with patch.object(svc, "_upload_file", return_value=True):
                svc._do_upload()
                assert svc._config_dirty is False
                assert svc._custom_dirty is False
                assert svc._records_dirty is False

    def test_acquires_lock(self, svc):
        """同时只有一个上传线程能获取锁"""
        svc._upload_lock.acquire()  # 锁已被占用
        with patch.object(svc, "_sync_token", return_value=True):
            assert svc._do_upload() is False  # 获取锁失败 → 跳过
        svc._upload_lock.release()

    def test_uploads_app_config_only(self, svc):
        """只上传有脏标记的项"""
        svc._custom_dirty = False
        svc._records_dirty = False
        with patch.object(svc, "_sync_token", return_value=True):
            with patch.object(svc, "_upload_file", return_value=True) as mock_upload:
                svc._do_upload()
                # 只上传了 app.config
                assert mock_upload.call_count == 1
                _args, kwargs = mock_upload.call_args
                assert kwargs.get("label") == "app.config"


# =============================================================================
# 4. _do_download — 下载安全守卫
# =============================================================================


class TestDoDownload:
    """_do_download 下载逻辑"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._token = "valid_token"
        svc._owner = "test_user"
        svc._config_path = MagicMock()
        svc._records_path = MagicMock()
        svc._user_custom_path = MagicMock()
        svc._user_custom_path.exists.return_value = False
        svc._records_path.parent.exists.return_value = False
        svc._sha_cache_path = MagicMock()
        # 默认 httpx mock
        svc._debounce_timer = MagicMock(spec=QTimer)
        return svc

    def test_downloads_app_config_successfully(self, svc):
        """正常下载 app.config"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client

            # 第一步：检查 SHA（返回 SHA）
            # 第二步：下载文件（返回 base64 编码的内容）
            mock_client.get.side_effect = [
                make_httpx_response(200, {"sha": "remote_sha_123"}),  # SHA 检查
                make_gitee_file_response("new_config_data", sha="remote_sha_123"),  # 下载
            ]

            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(svc._config_path, "write_bytes") as mock_write:
                    result = svc._do_download()
                    assert result is True
                    mock_write.assert_called_once_with(b"new_config_data")

    def test_aborts_when_app_config_fails(self, svc):
        """app.config 下载失败 → 整体失败（不继续下载其他项）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client

            # SHA 检查成功，但下载失败
            mock_client.get.side_effect = [
                make_httpx_response(200, {"sha": "remote_sha"}),  # SHA 检查
                make_httpx_response(500),  # 下载失败
            ]

            with patch.object(svc, "_sync_token", return_value=True):
                # 调用后不应该执行到 user-custom/share_records 的 HTTP 调用
                result = svc._do_download()
                assert result is False
                # 只调用了 2 次 get（SHA 检查 + 下载），没有后续的 user-custom 检查
                assert mock_client.get.call_count == 2

    def test_aborts_on_sha_check_exception(self, svc):
        """SHA 检查异常 → 整体失败"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = httpx.TimeoutException("timeout")

            with patch.object(svc, "_sync_token", return_value=True):
                result = svc._do_download()
                assert result is False

    def test_skips_when_sha_matches(self, svc):
        """远端 SHA 与缓存一致 → 跳过下载"""
        svc._config_remote_sha = "cached_sha"
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # SHA 检查返回与缓存相同的 SHA
            mock_client.get.return_value = make_httpx_response(
                200, {"sha": "cached_sha"}
            )

            with patch.object(svc, "_sync_token", return_value=True):
                with patch("pathlib.Path.write_bytes") as mock_write:
                    result = svc._do_download()
                    assert result is True  # 跳过下载但仍算成功
                    mock_write.assert_not_called()  # 没有写入文件

    def test_returns_false_on_token_invalid(self, svc):
        """token 无效（本地无 token）→ 返回 False"""
        # 实现演进：_do_download 改用 _prepare_read_token 载入 token（不刷新）
        with patch.object(svc, "_prepare_read_token", return_value=False):
            assert svc._do_download() is False

    def test_sets_suppress_window_on_download(self, svc):
        """下载成功后设置 30s 抑制窗口"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                make_httpx_response(200, {"sha": "s1"}),
                make_gitee_file_response("data", sha="s1"),
            ]

            before = time.time()
            with patch.object(svc, "_sync_token", return_value=True):
                with patch("pathlib.Path.write_bytes"):
                    svc._do_download()
                    assert svc._suppress_until >= before + 29.0  # 大约 30s


# =============================================================================
# 5. _initial_sync — 初始同步策略
# =============================================================================


class TestInitialSync:
    """_initial_sync 初始同步逻辑"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._token = "valid_token"
        svc._owner = "test_user"
        svc._config_path = MagicMock()
        svc._config_path.exists.return_value = True
        svc._config_path.read_bytes.return_value = b"local_config"
        svc._user_custom_path = MagicMock()
        svc._user_custom_path.exists.return_value = False
        svc._records_path = MagicMock()
        svc._records_path.exists.return_value = False
        svc._sha_cache_path = MagicMock()
        svc._debounce_timer = MagicMock(spec=QTimer)
        svc._upload_lock = threading.Lock()
        return svc

    def test_downloads_when_remote_exists(self, svc):
        """远端有配置 → 下载覆盖本地 → 标记初始同步完成"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # _check_remote → 文件存在 → True
            # _do_download 内部 SHA 检查 + 下载
            mock_client.get.side_effect = [
                make_httpx_response(200, {"content": "abc", "sha": "s1"}),  # check_remote
                make_httpx_response(200, {"sha": "s1"}),  # SHA check
                make_gitee_file_response("remote_data", sha="s1"),  # download
            ]

            with patch("pathlib.Path.write_bytes"):
                svc._initial_sync()
                assert svc._initial_sync_completed is True

    def test_uploads_when_remote_empty(self, svc):
        """远端无配置(404) → 上传本地 → 标记初始同步完成"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # _check_remote → 404
            mock_client.get.return_value = make_httpx_response(404)

            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(
                    svc, "_upload_file", return_value=True
                ) as mock_upload:
                    svc._initial_sync()
                    assert svc._initial_sync_completed is True
                    assert mock_upload.called

    def test_skips_when_remote_unknown(self, svc):
        """远端状态不确定(网络错误) → 跳过 → 初始同步未完成"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # _check_remote → 非 404 错误 → None
            mock_client.get.return_value = make_httpx_response(401)

            svc._initial_sync()
            assert svc._initial_sync_completed is False

    def test_skips_when_token_invalid(self, svc):
        """token 无效（本地无 token）→ skip → 初始同步未完成"""
        svc._token = ""  # token 为空
        # 实现演进：_initial_sync 改用 _prepare_read_token 载入 token（不刷新）
        with patch.object(svc, "_prepare_read_token", return_value=False):
            svc._initial_sync()
        assert svc._initial_sync_completed is False

    def test_does_not_set_completed_on_download_failure(self, svc):
        """远端有配置但下载失败 → 降级本地刷新上传；刷新也失败 → 不标记完成"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                make_httpx_response(200, {"content": "abc", "sha": "s1"}),  # check_remote
                make_httpx_response(200, {"sha": "s1"}),  # SHA check
                make_httpx_response(500),  # download fails
            ]

            # 下载失败 → 降级 _refresh_local_and_upload（路径 B），本地刷新也失败 → 不标记完成
            with patch(
                "app.gateway.auth.gitee.GiteeOAuthBackend._ensure_valid_token",
                return_value=(None, "network timeout"),
            ):
                svc._initial_sync()
            assert svc._initial_sync_completed is False

    def test_does_not_set_completed_on_upload_failure(self, svc):
        """远端无配置但上传失败 → 不标记完成"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(404)

            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(svc, "_upload_file", return_value=False):
                    svc._initial_sync()
                    assert svc._initial_sync_completed is False

    def test_suppress_window_upload_path(self, svc):
        """上传路径 ⇒ 抑制窗口 5s（短抑制）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(404)

            before = time.time()
            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(svc, "_upload_file", return_value=True):
                    svc._initial_sync()
                    remaining = svc._suppress_until - time.time()
                    assert 0 <= remaining <= 6.0  # 大约 5s 抑制

    def test_suppress_window_download_path(self, svc):
        """下载路径 ⇒ 抑制窗口 30s（长抑制，来自 _do_download）"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                make_httpx_response(200, {"content": "abc", "sha": "s1"}),  # check_remote
                make_httpx_response(200, {"sha": "s1"}),  # SHA check
                make_gitee_file_response("data", sha="s1"),  # download
            ]

            before = time.time()
            with patch("pathlib.Path.write_bytes"):
                # 下载成功后闭环刷新（_refresh_and_upload_after_download）会把抑制窗口
                # 覆盖为上传路径的 5s 短抑制；此处 mock 掉闭环，隔离验证 _do_download
                # 本身设置的 30s 长抑制，且 finally 不缩短下载路径的抑制窗口
                with patch.object(svc, "_refresh_and_upload_after_download"):
                    svc._initial_sync()
                remaining = svc._suppress_until - time.time()
                assert remaining >= 20.0  # 保留 ~30s 抑制


# =============================================================================
# 6. 抑制窗口 — 不丢失变更
# =============================================================================


class TestSuppressionWindow:
    """抑制窗口期间不丢失配置修改"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._state = "idle"
        svc._initial_sync_completed = True
        svc._debounce_timer = MagicMock(spec=QTimer)
        svc._config_dirty = True
        return svc

    def test_queues_retry_when_suppressed(self, svc):
        """抑制窗口内收到变更 → 调度延迟检查（不直接跳过）"""
        svc._suppress_until = time.time() + 10.0  # 10s 抑制

        with patch("PyQt5.QtCore.QTimer.singleShot") as mock_singleshot:
            svc._on_config_changed_main()
            # 应该调度了延迟检查
            assert svc._suppress_retry_scheduled is True
            assert mock_singleshot.called

    def test_does_not_duplicate_retry(self, svc):
        """多次变更不重复调度延迟检查"""
        svc._suppress_until = time.time() + 10.0
        svc._suppress_retry_scheduled = True  # 已调度

        with patch("PyQt5.QtCore.QTimer.singleShot") as mock_singleshot:
            svc._on_config_changed_main()
            # 不会重复调度
            assert mock_singleshot.called is False

    def test_starts_debounce_when_not_suppressed(self, svc):
        """抑制窗口外 → 正常启动防抖"""
        svc._suppress_until = time.time() - 1.0  # 已过期

        svc._on_config_changed_main()
        assert svc._debounce_timer.start.called

    def test_does_not_start_debounce_when_nothing_dirty(self, svc):
        """没有脏标记 → 不启动防抖（_start_debounce_timer 优化）"""
        svc._suppress_until = time.time() - 1.0
        svc._config_dirty = False
        svc._custom_dirty = False
        svc._records_dirty = False

        svc._on_config_changed_main()
        assert svc._debounce_timer.start.called is False

    def test_flush_after_suppress_ends(self, svc):
        """_flush_suppressed_changes → 有脏标记时启动防抖"""
        svc._suppress_until = time.time() - 1.0  # 已过期
        svc._initial_sync_completed = True
        svc._config_dirty = True

        svc._flush_suppressed_changes()
        assert svc._debounce_timer.start.called

    def test_flush_noop_when_still_suppressed(self, svc):
        """_flush_suppressed_changes → 窗口被刷新 → 不启动防抖"""
        svc._suppress_until = time.time() + 30.0  # 又被新窗口覆盖
        svc._config_dirty = True

        svc._flush_suppressed_changes()
        assert svc._debounce_timer.start.called is False

    def test_flush_noop_when_initial_sync_not_done(self, svc):
        """_flush_suppressed_changes → 初始同步未完成 → 不启动防抖"""
        svc._suppress_until = time.time() - 1.0
        svc._initial_sync_completed = False

        svc._flush_suppressed_changes()
        assert svc._debounce_timer.start.called is False


# =============================================================================
# 7. 重新绑定安全
# =============================================================================


class TestReBinding:
    """重新绑定场景安全"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._config_path = MagicMock()
        svc._config_path.exists.return_value = True
        svc._config_path.read_bytes.return_value = b"local_config"
        svc._user_custom_path = MagicMock()
        svc._user_custom_path.exists.return_value = False
        svc._records_path = MagicMock()
        svc._records_path.exists.return_value = False
        svc._sha_cache_path = MagicMock()
        svc._debounce_timer = MagicMock(spec=QTimer)
        svc._upload_lock = threading.Lock()
        return svc

    def test_enable_clears_initial_sync_flag(self, svc):
        """enable() 重置 _initial_sync_completed"""
        svc._initial_sync_completed = True  # 假设之前已完成
        svc.enable("new_token", "new_owner")
        assert svc._initial_sync_completed is False

    def test_disable_clears_sha_cache(self, svc):
        """disable() 清除 SHA 缓存"""
        svc._config_remote_sha = "cached_sha"
        svc._custom_remote_sha = "custom_sha"
        svc._records_remote_sha = "records_sha"
        svc._sha_cache_path.exists.return_value = True

        svc.disable()
        assert svc._config_remote_sha is None
        assert svc._custom_remote_sha is None
        assert svc._records_remote_sha is None

    def test_disable_clears_initial_sync_flag(self, svc):
        """disable() 重置 _initial_sync_completed"""
        svc._initial_sync_completed = True
        svc.disable()
        assert svc._initial_sync_completed is False

    def test_rebind_downloads_remote(self, svc):
        """重新绑定后远端有配置 → 下载（不盲目上传）"""
        svc._state = "disabled"
        svc._token = "new_token"
        svc._owner = "new_owner"
        svc._initial_sync_completed = False

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = [
                make_httpx_response(200, {"content": "abc", "sha": "s1"}),  # check_remote
                make_httpx_response(200, {"sha": "s1"}),  # SHA check
                make_gitee_file_response("remote_data", sha="s1"),  # download
            ]

            with patch.object(svc, "_sync_token", return_value=True):
                with patch("pathlib.Path.write_bytes"):
                    svc._initial_sync()
                    assert svc._initial_sync_completed is True

    def test_rebind_uploads_when_remote_gone(self, svc):
        """重新绑定后远端无配置(404) → 上传本地"""
        svc._state = "disabled"
        svc._token = "new_token"
        svc._owner = "new_owner"
        svc._initial_sync_completed = False

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(404)  # check_remote

            with patch.object(svc, "_sync_token", return_value=True):
                with patch.object(svc, "_upload_file", return_value=True):
                    svc._initial_sync()
                    assert svc._initial_sync_completed is True

    def test_upload_method_redirects_when_not_synced(self, svc):
        """upload() 在初始同步未完成时触发 _initial_sync（非盲目上传）"""
        svc._initial_sync_completed = False
        svc._state = "idle"
        svc._token = "t"
        svc._owner = "o"

        with patch.object(svc, "_initial_sync") as mock_init_sync:
            result = svc.upload()
            assert result is True  # 返回 True（调度了同步）
            assert mock_init_sync.called


# =============================================================================
# 8. 全链路集成 — enable/disable 生命周期
# =============================================================================


class TestLifecycle:
    """enable / disable 生命周期"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        return reset_sync_service

    def test_enable_sets_state_idle(self, svc):
        """enable() 设置状态为 idle（在 _initial_sync 线程启动前）"""
        with patch.object(svc, "_start_watching"):
            with patch.object(svc, "_initial_sync"):  # 阻止后台线程修改状态
                svc.enable("token", "owner")
                assert svc._state == "idle"

    def test_enable_ignores_duplicate(self, svc):
        """重复 enable() 被忽略"""
        svc._state = "idle"
        with patch.object(svc, "_start_watching") as mock_watch:
            svc.enable("token", "owner")
            # 已在运行中，不会再次启动 watch
            assert mock_watch.called is False

    def test_disable_sets_state_disabled(self, svc):
        """disable() 设置状态为 disabled"""
        svc._state = "idle"
        with patch.object(svc, "_stop_watching"):
            svc.disable()
            assert svc._state == "disabled"

    def test_disable_stops_debounce_timer(self, svc):
        """disable() 停止防抖计时器"""
        mock_timer = MagicMock(spec=QTimer)
        svc._debounce_timer = mock_timer
        svc.disable()
        assert mock_timer.stop.called

    def test_disable_disconnects_signals(self, svc):
        """disable() 断开信号连接（通过停止 watch 实现）"""
        with patch.object(svc, "_stop_watching") as mock_stop:
            svc.disable()
            assert mock_stop.called


# =============================================================================
# 9. 上传/下载文件 — _upload_file / _download_file
# =============================================================================


class TestFileTransfer:
    """文件传输基础操作"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        svc = reset_sync_service
        svc._token = "valid_token"
        svc._owner = "test_user"
        return svc

    def test_upload_file_post_new(self, svc):
        """新文件（无 SHA）→ POST"""
        local_path = MagicMock()
        local_path.read_bytes.return_value = b"hello"

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # GET SHA → None (文件不存在)
            # POST → 201
            mock_client.get.return_value = make_httpx_response(404)
            mock_client.post.return_value = make_httpx_response(
                201, {"content": {"sha": "new_sha"}}
            )

            result = svc._upload_file(local_path, "drifox/app.config", "app.config")
            assert result is True
            assert svc._config_remote_sha == "new_sha"

    def test_upload_file_put_existing(self, svc):
        """已有文件（有 SHA）→ PUT"""
        local_path = MagicMock()
        local_path.read_bytes.return_value = b"hello"

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # GET SHA → "existing_sha"
            # PUT → 200
            mock_client.get.return_value = make_httpx_response(
                200, {"sha": "existing_sha", "content": "abc"}
            )
            mock_client.put.return_value = make_httpx_response(
                200, {"content": {"sha": "updated_sha"}}
            )

            result = svc._upload_file(local_path, "drifox/app.config", "app.config")
            assert result is True
            assert svc._config_remote_sha == "updated_sha"

    def test_download_file_success(self, svc):
        """下载文件成功"""
        import base64

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(
                200,
                {
                    "content": base64.b64encode(b"hello").decode("utf-8"),
                    "sha": "file_sha",
                },
            )

            local_path = MagicMock()
            result = svc._download_file(
                "drifox/app.config", local_path, "app.config"
            )
            assert result is True
            local_path.write_bytes.assert_called_once_with(b"hello")
            assert svc._config_remote_sha == "file_sha"

    def test_download_file_404(self, svc):
        """下载不存在的文件 → False"""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.return_value = make_httpx_response(404)

            result = svc._download_file(
                "drifox/app.config", MagicMock(), "app.config"
            )
            assert result is False


# =============================================================================
# 10. SHA 缓存持久化
# =============================================================================


class TestShaCache:
    """SHA 缓存读写"""

    @pytest.fixture
    def svc(self, reset_sync_service):
        return reset_sync_service

    def test_save_and_load_sha_cache(self, svc, tmp_path):
        """保存 SHA 到磁盘后能正确加载"""
        svc._sha_cache_path = tmp_path / ".sync_shas.json"
        svc._config_remote_sha = "abc123"
        svc._custom_remote_sha = "def456"
        svc._records_remote_sha = "ghi789"

        svc._save_sha_cache()
        assert svc._sha_cache_path.exists()

        # 新建实例加载（模拟重启）
        svc._config_remote_sha = None
        svc._custom_remote_sha = None
        svc._records_remote_sha = None
        svc._load_sha_cache()

        assert svc._config_remote_sha == "abc123"
        assert svc._custom_remote_sha == "def456"
        assert svc._records_remote_sha == "ghi789"

    def test_sha_cache_skip_when_all_none(self, svc, tmp_path):
        """所有 SHA 为 None 时写入空对象"""
        svc._sha_cache_path = tmp_path / ".sync_shas.json"
        svc._config_remote_sha = None
        svc._custom_remote_sha = None
        svc._records_remote_sha = None

        svc._save_sha_cache()
        data = json.loads(svc._sha_cache_path.read_text(encoding="utf-8"))
        assert data == {}


# =============================================================================
# 7. 主题同步时序 — 自定义主题不被静默回退为默认
# =============================================================================


class TestThemeSyncTiming:
    """修复回归：gitee 同步后自定义主题因未注册被 OptionsValidator.correct() 静默回退默认。

    背景：app.config 中 ui_theme_style 若引用由 user-custom 插件提供的自定义主题，
    而该插件是本次下载才解压落地（theme_manager 尚未重新扫描），那么直接执行
    配置项写入时，OptionsConfigItem.value setter 里的 OptionsValidator.correct()
    会因主题不在选项列表而把值静默改写为 options[0]（默认主题），自定义主题永久丢失。
    修复要求：_reload_settings_on_main_thread 必须先 reload 主题 + 刷新验证器，
    再写 ui_theme_style，确保自定义主题已注册后再按配置恢复。
    """

    class _FakeSettings:
        """最小化 Settings 替身：仅含 file 与 ui_theme_style 一个配置项"""

        file = Path("")

        # 构造一个真实 OptionsConfigItem：直接验证 OptionsValidator.correct() 行为
        ui_theme_style = OptionsConfigItem(
            "UI",
            "ThemeStyle",
            "lumia",
            OptionsValidator(["lumia"]),
        )

        @classmethod
        def get_instance(cls):
            return cls

    @pytest.fixture
    def svc(self, reset_sync_service):
        return reset_sync_service

    def _write_config(self, tmp_path: Path, theme: str) -> Path:
        cfg = tmp_path / "app.config"
        cfg.write_text(json.dumps({"UI": {"ThemeStyle": theme}}), encoding="utf-8")
        return cfg

    def test_options_validator_falls_back_to_default_when_unregistered(self):
        """复现旧 bug：自定义主题未注册时 value setter 静默回退默认主题"""
        cfg = self._FakeSettings
        cfg.ui_theme_style.value = "lumia"
        cfg.ui_theme_style.validator.__init__(["lumia"])

        cfg.ui_theme_style.value = "mytheme"  # 云端配置写自定义主题（theme 尚未注册）
        assert cfg.ui_theme_style.value == "lumia"  # 被 correct() 回退默认

        # 修复前置：主题注册后再写 → 保持自定义主题
        cfg.ui_theme_style.validator.__init__(["lumia", "mytheme"])
        cfg.ui_theme_style.value = "mytheme"
        assert cfg.ui_theme_style.value == "mytheme"

    def test_reload_settings_registers_themes_before_write(self, svc, tmp_path, monkeypatch):
        """_reload_settings_on_main_thread 先注册主题再写 ui_theme_style → 不回退"""
        from app.core import config_sync as cs

        cfg_path = self._write_config(tmp_path, "mytheme")

        fake = self._FakeSettings
        fake.file = cfg_path
        fake.ui_theme_style.validator.__init__(["lumia"])  # 模拟：本次同步前主题未注册

        # 把主题注册委托到 update_theme_options：模拟 reload 后验证器有了新主题
        def _fake_update_theme_options():
            themes = list(fake.ui_theme_style.validator.options)
            if "mytheme" not in themes:
                themes.append("mytheme")
            fake.ui_theme_style.validator.__init__(themes)

        monkeypatch.setattr("app.utils.config.update_theme_options", _fake_update_theme_options)
        monkeypatch.setattr(
            "app.utils.theme_manager.ThemeManager.reload",
            lambda self: None,  # reload 不真正扫盘，仅由 update_theme_options 补主题
        )
        monkeypatch.setattr(cs, "Settings", fake)

        svc._reload_settings_on_main_thread()

        assert fake.ui_theme_style.value == "mytheme", "自定义主题应被保留，而非回退默认"
