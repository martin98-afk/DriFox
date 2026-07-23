# -*- coding: utf-8 -*-
"""
Gitee 图床上传工具

将本地文件上传至 Gitee 仓库，返回公开下载链接。
Gateway 适配器在发送文件/图片时自动调用，
AI 也可通过 gitee_upload 工具手动上传。

参考实现: WorkFlowGUI/app/components/消息推送 中的 Gitee 上传逻辑
"""

import base64
import uuid
from pathlib import Path
from typing import Optional, Tuple

import requests
from loguru import logger


class GiteeUploader:
    """
    Gitee 图床上传器（单例）

    用法:
        uploader = GiteeUploader.get_instance()
        url = uploader.upload_file("/path/to/image.png")
        url = uploader.upload_bytes(raw_data, "chart.png")
    """

    _instance: Optional["GiteeUploader"] = None

    API_URL = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"

    def __init__(self):
        self._token: str = ""
        self._owner: str = ""
        self._repo: str = ""
        self._path: str = "drifox"
        self._branch: str = "master"
        self._config_loaded: bool = False

    @classmethod
    def get_instance(cls) -> "GiteeUploader":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_config(self) -> bool:
        """加载配置。
        优先使用用户 OAuth 绑定的 token/repo，
        未绑定时回退到共享仓库配置。
        """
        if self._config_loaded:
            return bool(self._token and self._owner and self._repo)

        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()

            # 优先使用用户绑定的 Gitee 账号
            if cfg.gitee_bound.value and cfg.gitee_user_token.value:
                self._token = cfg.gitee_user_token.value
                self._owner = cfg.gitee_user_owner.value
                self._repo = cfg.gitee_user_repo.value or "DriFox_uploads"
                self._path = "drifox"
                self._branch = "master"
                logger.info("[GiteeUploader] 使用用户绑定账号上传")
            else:
                # 回退共享仓库
                self._token = cfg.gitee_token.value or ""
                self._owner = cfg.gitee_owner.value or ""
                self._repo = cfg.gitee_repo.value or ""
                self._path = cfg.gitee_path.value or "drifox"
                self._branch = cfg.gitee_branch.value or "master"
                logger.info("[GiteeUploader] 使用共享仓库上传")

            self._config_loaded = True
        except Exception as e:
            logger.warning(f"[GiteeUploader] 加载配置失败: {e}")

        return bool(self._token and self._owner and self._repo)

    def reset_config(self):
        """重置配置缓存，使下次访问时重新加载（用于绑定/解绑后刷新）"""
        self._config_loaded = False

    def is_configured(self) -> bool:
        """检查 Gitee 是否已配置"""
        return self._ensure_config()

    def upload_file(self, local_path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        上传本地文件到 Gitee 仓库

        Args:
            local_path: 本地文件路径

        Returns:
            (download_url, error):
                download_url - 成功时返回公开下载链接
                error - 失败时返回错误描述
        """
        fp = Path(local_path)
        if not fp.exists():
            return None, f"文件不存在: {local_path}"
        if not fp.is_file():
            return None, f"路径不是文件: {local_path}"

        try:
            data = fp.read_bytes()
            ext = fp.suffix.lower()
            return self.upload_bytes(data, fp.name, ext)
        except Exception as e:
            return None, f"读取文件失败: {e}"

    def upload_bytes(self, data: bytes, filename: str = "", ext: str = "") -> Tuple[Optional[str], Optional[str]]:
        """
        上传字节数据到 Gitee 仓库

        Args:
            data: 文件字节数据
            filename: 文件名（用于确定扩展名）
            ext: 自定义扩展名（覆盖 filename 中的扩展名）

        Returns:
            (download_url, error):
                download_url - 成功时返回公开下载链接
                error - 失败时返回错误描述
        """
        if not self._ensure_config():
            return None, "Gitee 未配置 (缺少 token/owner/repo)"

        try:
            # 确定文件名：优先用原始文件名，仅无文件名时才用 UUID
            if not ext and filename:
                ext = Path(filename).suffix
            if not ext:
                ext = ".png"

            if filename:
                # 使用原始文件名（分享文件已带时间戳，不存在冲突）
                upload_name = filename
            else:
                upload_name = f"{uuid.uuid4().hex}{ext}"

            storage_path = self._path.strip("/")
            full_path = f"{storage_path}/{upload_name}" if storage_path else upload_name

            # Base64 编码
            content_b64 = base64.b64encode(data).decode("utf-8")

            url = self.API_URL.format(owner=self._owner, repo=self._repo, path=full_path)

            payload = {
                "access_token": self._token,
                "content": content_b64,
                "message": f"DriFox Upload: {upload_name}",
                "branch": self._branch,
            }

            resp = requests.post(url, data=payload, timeout=30)
            if resp.status_code == 201:
                download_url = resp.json().get("content", {}).get("download_url")
                if download_url:
                    logger.info(f"[GiteeUploader] 上传成功: {upload_name} → {download_url}")
                    return download_url, None
                return None, "API 返回了 201 但缺少 download_url"

            # 常见错误码处理
            err_msg = self._parse_error(resp)
            logger.warning(f"[GiteeUploader] 上传失败 [{resp.status_code}]: {err_msg}")
            return None, f"[{resp.status_code}] {err_msg}"

        except requests.exceptions.Timeout:
            return None, "上传超时 (30s)"
        except requests.exceptions.ConnectionError:
            return None, "网络连接失败"
        except Exception as e:
            logger.error(f"[GiteeUploader] 上传异常: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def _parse_error(resp) -> str:
        """解析 Gitee API 错误响应"""
        try:
            body = resp.json()
            return body.get("message", resp.text[:200])
        except Exception:
            return resp.text[:200]


# 便捷函数
def get_gitee_uploader() -> GiteeUploader:
    """获取 GiteeUploader 单例"""
    return GiteeUploader.get_instance()


def upload_to_gitee(local_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    上传本地文件到 Gitee（便捷函数）

    Args:
        local_path: 本地文件路径

    Returns:
        (download_url, error)
    """
    return GiteeUploader.get_instance().upload_file(local_path)
