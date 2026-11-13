# -*- coding: utf-8 -*-
"""
EdgeOne 匿名部署工具

把单页 HTML 部署到腾讯 EdgeOne Makers（Pages 后继产品）并返回公开链接，
用于解决 Gitee raw 直链对 HTML 返回 text/plain 导致浏览器显示源码的问题。

匿名部署无需账号或 API Token，站点链接 30 分钟内有效（EdgeOne 产品设计）。
流程：内存打包 zip → 申请临时凭证 → COS 上传 → 创建部署 → 轮询结果。

参考实现: edgeone CLI（npm 包 edgeone）的匿名部署分支
"""

import hashlib
import hmac
import io
import random
import string
import time
import urllib.parse
import zipfile
from typing import Optional, Tuple

import requests
from loguru import logger


class EdgeOneDeployError(Exception):
    """部署过程中的可读错误，消息直接展示给用户"""


class EdgeOneDeployer:
    """
    EdgeOne 匿名部署器（单例）

    用法:
        deployer = EdgeOneDeployer.get_instance()
        url, err = deployer.deploy_html("<html>...</html>")
    """

    _instance: Optional["EdgeOneDeployer"] = None

    # 匿名部署接口（EdgeOne CLI 使用的端点，无 /v1 前缀）
    API_BASE = "https://pages-api.edgeone.ai"

    # 轮询部署状态的间隔与总超时（实测 16s 左右完成）
    POLL_INTERVAL_S = 3.0
    POLL_TIMEOUT_S = 90.0
    # 单次 HTTP 请求超时
    HTTP_TIMEOUT_S = 30.0
    # COS 签名有效期（秒）
    SIGN_TTL_S = 3600

    def __init__(self):
        self._session = requests.Session()

    @classmethod
    def get_instance(cls) -> "EdgeOneDeployer":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 对外主入口 ──────────────────────────────────────────────

    def deploy_html(self, html_text: str, title: str = "") -> Tuple[Optional[str], Optional[str]]:
        """
        部署单页 HTML 到 EdgeOne，返回公开访问链接

        Args:
            html_text: 完整 HTML 文本
            title: 站点名提示（仅用于生成项目名，可空）

        Returns:
            (site_url, error):
                site_url - 成功时返回站点链接（含 eo_token，30 分钟内有效）
                error - 失败时返回错误描述
        """
        if not html_text or not html_text.strip():
            return None, "HTML 内容为空"

        try:
            zip_bytes = self._pack_single_page(html_text)
            cred = self._request_temp_token(title)
            cos_key = self._upload_to_cos(cred, zip_bytes)
            project_id, deployment_id = self._create_deployment(cred, cos_key)
            site_url = self._poll_deployment(cred, project_id, deployment_id)
            logger.info(f"[EdgeOneDeployer] 部署成功: {site_url}")
            return site_url, None
        except EdgeOneDeployError as e:
            logger.warning(f"[EdgeOneDeployer] 部署失败: {e}")
            return None, str(e)
        except requests.exceptions.Timeout:
            return None, "部署超时（网络请求无响应）"
        except requests.exceptions.ConnectionError:
            return None, "网络连接失败，无法访问 EdgeOne"
        except Exception as e:
            logger.error(f"[EdgeOneDeployer] 部署异常: {e}", exc_info=True)
            return None, f"部署异常: {e}"

    # ── 步骤 1：打包 ───────────────────────────────────────────

    @staticmethod
    def _pack_single_page(html_text: str) -> bytes:
        """把 HTML 打包成 zip（内存内完成，不落临时文件）"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", html_text.encode("utf-8"))
        return buf.getvalue()

    @staticmethod
    def _make_project_name(title: str) -> str:
        """生成 EdgeOne 项目名：仅小写字母数字与中划线，末尾加随机后缀避免撞名"""
        base = "".join(c if c.isalnum() and c.isascii() else "-" for c in (title or "").lower())
        base = "-".join(filter(None, base.split("-")))[:24] or "drifox-share"
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{base}-{suffix}"

    # ── 步骤 2：临时凭证 ───────────────────────────────────────

    def _request_temp_token(self, title: str) -> dict:
        """申请匿名部署的临时凭证（含 COS 上传所需的临时密钥）"""
        project_name = self._make_project_name(title)
        url = f"{self.API_BASE}/pages-public/describe_temp_token"
        payload = {"Language": "zh-CN", "ProjectName": project_name}
        try:
            resp = self._session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.HTTP_TIMEOUT_S,
            )
        except requests.RequestException:
            # 网络类异常冒泡到 deploy_html，由外层统一映射为友好提示
            raise
        except Exception as e:
            raise EdgeOneDeployError(f"申请临时凭证失败: {e}") from e

        if resp.status_code != 200:
            raise EdgeOneDeployError(f"申请临时凭证失败: HTTP {resp.status_code} {resp.text[:200]}")

        try:
            body = resp.json()
        except Exception as e:
            raise EdgeOneDeployError(f"临时凭证响应解析失败: {e}") from e

        if body.get("code") != 0:
            raise EdgeOneDeployError(f"申请临时凭证失败: {body.get('message') or body.get('code')}")

        data = body.get("data") or {}
        token = data.get("Token")
        response = data.get("Response") or {}
        credentials = response.get("Credentials") or {}
        if not token or not response.get("Bucket") or not credentials.get("TmpSecretId"):
            raise EdgeOneDeployError("临时凭证响应缺少必要字段")

        return {
            "token": token,
            "project_name": response.get("ProjectName") or project_name,
            "bucket": response["Bucket"],
            "region": response.get("Region") or "ap-singapore",
            "target_path": response.get("TargetPath") or "",
            "credentials": credentials,
            "expired_at": response.get("ExpiredTime") or data.get("Expired"),
        }

    # ── 步骤 3：COS 上传 ──────────────────────────────────────

    def _upload_to_cos(self, cred: dict, zip_bytes: bytes) -> str:
        """上传 zip 到 COS，返回对象 key"""
        target = cred["target_path"].strip("/")
        key = f"{target}/site.zip" if target else "site.zip"
        bucket = cred["bucket"]
        region = cred["region"]
        host = f"{bucket}.cos.{region}.myqcloud.com"
        url = f"https://{host}/{key}"
        auth = self._build_cos_authorization(cred["credentials"], host, key)

        try:
            resp = self._session.put(
                url,
                data=zip_bytes,
                headers={
                    "x-cos-security-token": cred["credentials"]["Token"],
                    "Authorization": auth,
                },
                timeout=self.HTTP_TIMEOUT_S,
            )
        except requests.RequestException:
            raise
        except Exception as e:
            raise EdgeOneDeployError(f"上传产物失败: {e}") from e

        if resp.status_code != 200:
            raise EdgeOneDeployError(f"上传产物失败: HTTP {resp.status_code} {resp.text[:200]}")
        return key

    def _build_cos_authorization(self, credentials: dict, host: str, key: str) -> str:
        """构造 COS v5 签名（sha1），临时密钥需附带 x-cos-security-token 参与签名"""
        secret_id = credentials["TmpSecretId"]
        secret_key = credentials["TmpSecretKey"]
        now = int(time.time())
        sign_time = f"{now};{now + self.SIGN_TTL_S}"

        headers = {
            "host": host,
            "x-cos-security-token": credentials["Token"],
        }
        header_list = ";".join(sorted(k.lower() for k in headers))
        header_str = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted((k.lower(), v) for k, v in headers.items())
        )
        http_string = f"put\n/{key}\n\n{header_str}\n"
        string_to_sign = f"sha1\n{sign_time}\n{hashlib.sha1(http_string.encode('utf-8')).hexdigest()}\n"
        sign_key = self._hmac_sha1(secret_key.encode("utf-8"), sign_time)
        signature = self._hmac_sha1(sign_key.encode("utf-8"), string_to_sign)

        return (
            "q-sign-algorithm=sha1"
            f"&q-ak={secret_id}"
            f"&q-sign-time={sign_time}"
            f"&q-key-time={sign_time}"
            f"&q-header-list={header_list}"
            "&q-url-param-list="
            f"&q-signature={signature}"
        )

    @staticmethod
    def _hmac_sha1(key: bytes, msg: str) -> str:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha1).hexdigest()

    # ── 步骤 4：创建部署 ──────────────────────────────────────

    def _create_deployment(self, cred: dict, cos_key: str) -> Tuple[str, str]:
        """用已上传的 COS 对象创建部署，返回 (ProjectId, DeploymentId)"""
        response = self._call_public_api(
            "create_deployment",
            {
                "Token": cred["token"],
                "ProjectName": cred["project_name"],
                "TempBucketPath": cos_key,
                "DistType": "Zip",
            },
            "创建部署失败",
        )
        project_id = response.get("ProjectId")
        deployment_id = response.get("DeploymentId")
        if not project_id or not deployment_id:
            raise EdgeOneDeployError("创建部署失败: 响应缺少 ProjectId/DeploymentId")
        return project_id, deployment_id

    # ── 步骤 5：轮询结果 ──────────────────────────────────────

    def _poll_deployment(self, cred: dict, project_id: str, deployment_id: str) -> str:
        """轮询部署状态至 Success，返回站点链接"""
        deadline = time.time() + self.POLL_TIMEOUT_S
        last_status = ""
        while time.time() < deadline:
            response = self._call_public_api(
                "describe_deployment",
                {
                    "Token": cred["token"],
                    "ProjectId": project_id,
                    "DeploymentIds": [deployment_id],
                    "NeedVisit": True,
                },
                "查询部署状态失败",
            )
            deployment = response.get("Deployment")
            if not deployment:
                raise EdgeOneDeployError("查询部署状态失败: 响应缺少 Deployment 字段")

            status = deployment.get("Status") or ""
            if status != last_status:
                logger.debug(f"[EdgeOneDeployer] 部署状态: {status}")
                last_status = status

            if status == "Success":
                site_url = deployment.get("ProjectUrl") or ""
                if not site_url:
                    raise EdgeOneDeployError("部署成功但未返回站点链接")
                return site_url
            if status in ("Failed", "Timeout", "Cancelled"):
                detail = deployment.get("Message") or deployment.get("Code") or status
                raise EdgeOneDeployError(f"部署失败: {detail}")

            time.sleep(self.POLL_INTERVAL_S)

        raise EdgeOneDeployError(f"部署超时（等待 {int(self.POLL_TIMEOUT_S)} 秒，最后状态: {last_status or '未知'}）")

    def _call_public_api(self, action: str, payload: dict, err_prefix: str) -> dict:
        """调用 pages-public 接口并解开 code/data/Response 三层结构"""
        url = f"{self.API_BASE}/pages-public/{action}"
        try:
            resp = self._session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.HTTP_TIMEOUT_S,
            )
        except requests.RequestException:
            raise
        except Exception as e:
            raise EdgeOneDeployError(f"{err_prefix}: {e}") from e

        if resp.status_code != 200:
            raise EdgeOneDeployError(f"{err_prefix}: HTTP {resp.status_code} {resp.text[:200]}")

        try:
            body = resp.json()
        except Exception as e:
            raise EdgeOneDeployError(f"{err_prefix}: 响应解析失败 {e}") from e

        if body.get("code") != 0:
            raise EdgeOneDeployError(f"{err_prefix}: {body.get('message') or body.get('code')}")

        response = (body.get("data") or {}).get("Response") or {}
        error = response.get("Error")
        if error:
            raise EdgeOneDeployError(f"{err_prefix}: {error.get('Message') or error.get('Code')}")
        return response


# 便捷函数
def get_edgeone_deployer() -> EdgeOneDeployer:
    """获取 EdgeOneDeployer 单例"""
    return EdgeOneDeployer.get_instance()


def deploy_html_to_edgeone(html_text: str, title: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    部署 HTML 到 EdgeOne（便捷函数）

    Args:
        html_text: 完整 HTML 文本
        title: 站点名提示

    Returns:
        (site_url, error)
    """
    return EdgeOneDeployer.get_instance().deploy_html(html_text, title)
