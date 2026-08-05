# -*- coding: utf-8 -*-
"""
存储后端抽象层测试

覆盖:
  - get_storage_backend 工厂函数
  - StorageBackend 接口约束
  - GiteeStorageBackend 的基本操作

Run: pytest tests/gateway/test_storage_abstraction.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("app.gateway.storage")
pytest.importorskip("app.gateway.storage.base")
pytest.importorskip("app.gateway.storage.gitee")


# =============================================================================
# 1. 工厂函数
# =============================================================================


class TestGetStorageBackend:
    """get_storage_backend 工厂"""

    def test_returns_gitee_by_name(self):
        """指定 name='gitee' 应返回 GiteeStorageBackend"""
        from app.gateway.storage import get_storage_backend
        from app.gateway.storage.gitee import GiteeStorageBackend

        backend = get_storage_backend("gitee", token="t", owner="o", repo="r")
        assert isinstance(backend, GiteeStorageBackend)

    def test_raises_for_unknown(self):
        """不存在的后端名应抛出 ValueError"""
        from app.gateway.storage import get_storage_backend

        with pytest.raises(ValueError, match="unknown"):
            get_storage_backend("unknown", token="t", owner="o", repo="r")


# =============================================================================
# 2. GiteeStorageBackend 操作
# =============================================================================


class TestGiteeStorageBackend:
    """GiteeStorageBackend — 核心文件操作"""

    @pytest.fixture
    def backend(self):
        """构造一个 GiteeStorageBackend 实例"""
        from app.gateway.storage.gitee import GiteeStorageBackend

        return GiteeStorageBackend(token="test_token", owner="test_user", repo="test_repo")

    def test_exists_returns_true_when_200(self, backend):
        """远端文件存在时 exists() 返回 True"""
        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"content": "abc", "sha": "sha1"}
            assert backend.exists("drifox/app.config") is True

    def test_exists_returns_false_when_404(self, backend):
        """远端文件不存在时 exists() 返回 False"""
        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 404
            assert backend.exists("drifox/app.config") is False

    def test_get_sha_returns_sha_when_200(self, backend):
        """远端文件存在时 get_sha() 返回 SHA"""
        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"sha": "abc123"}
            sha = backend.get_sha("drifox/app.config")
            assert sha == "abc123"

    def test_get_sha_returns_none_on_404(self, backend):
        """远端文件不存在时 get_sha() 返回 None"""
        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 404
            assert backend.get_sha("drifox/app.config") is None

    def test_upload_creates_new_file(self, backend):
        """上传新文件（无已有 SHA）应 POST"""
        with (
            patch.object(backend, "get_sha") as mock_get_sha,
            patch.object(backend, "_post") as mock_post,
        ):
            mock_get_sha.return_value = None
            mock_post.return_value.status_code = 201
            mock_post.return_value.json.return_value = {
                "content": {"sha": "newsha1"},
            }
            result = backend.upload("drifox/app.config", b"hello")
            assert result is True

    def test_upload_updates_existing_file(self, backend):
        """更新已有文件（有 SHA）应 PUT"""
        with (
            patch.object(backend, "get_sha") as mock_get_sha,
            patch.object(backend, "_put") as mock_put,
        ):
            mock_get_sha.return_value = "oldsha"
            mock_put.return_value.status_code = 200
            mock_put.return_value.json.return_value = {"content": {"sha": "newsha"}}
            result = backend.upload("drifox/app.config", b"hello")
            assert result is True

    def test_upload_returns_false_on_error(self, backend):
        """上传失败时返回 False"""
        with (
            patch.object(backend, "get_sha") as mock_get_sha,
            patch.object(backend, "_post") as mock_post,
        ):
            mock_get_sha.return_value = None
            mock_post.return_value.status_code = 401
            result = backend.upload("drifox/app.config", b"hello")
            assert result is False

    def test_download_returns_bytes_when_200(self, backend):
        """下载成功时返回 bytes"""
        import base64

        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "content": base64.b64encode(b"hello world").decode("utf-8"),
                "sha": "abc",
            }
            data = backend.download("drifox/app.config")
            assert data == b"hello world"

    def test_download_returns_none_on_404(self, backend):
        """下载不存在的文件返回 None"""
        with patch.object(backend, "_get") as mock_get:
            mock_get.return_value.status_code = 404
            assert backend.download("drifox/app.config") is None
