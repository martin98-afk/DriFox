# -*- coding: utf-8 -*-
"""
Gitee OAuth 平台后端

分层说明：
  - 本模块承载 Gitee 平台的 **网络/API 层**（token 换取、用户信息、仓库管理），
    所有 requests 调用集中于此，便于测试 mock 与平台横向扩展；
  - 授权码流程编排复用 base.run_authorization_code_flow（通用实现），
    本模块只负责 Gitee 特有的初始化动作（拉用户信息、建仓库、存配置）。
"""

from typing import Optional, Tuple

import requests
from loguru import logger

from app.gateway.auth.base import OAuthAppConfig, OAuthBackend, parse_error, run_authorization_code_flow

# ── Gitee 端点常量 ────────────────────────────────────────
GITEE_AUTHORIZE_URL = "https://gitee.com/oauth/authorize"
GITEE_TOKEN_URL = "https://gitee.com/oauth/token"
GITEE_USER_URL = "https://gitee.com/api/v5/user"
GITEE_REPO_URL = "https://gitee.com/api/v5/repos/{owner}/{repo}"
GITEE_CREATE_REPO_URL = "https://gitee.com/api/v5/user/repos"

FIXED_REPO_NAME = "DriFox_uploads"
SETTINGS_REPO_NAME = "DriFox_settings"  # 配置备份仓库（强制私有）


# ── Gitee API 层（网络调用集中在这里） ────────────────────


def exchange_token(code: str, client_id: str, client_secret: str, redirect_uri: str) -> Tuple[Optional[str], str]:
    """用授权码换取 access_token。Returns: (access_token|None, err_msg)"""
    resp = requests.post(
        GITEE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return None, f"换取 Token 失败：{parse_error(resp)}"

    access_token = resp.json().get("access_token")
    if not access_token:
        return None, "换取 Token 失败：响应中缺少 access_token"

    logger.info("[GiteeOAuth] 获取 access_token 成功")
    return access_token, ""


def fetch_user_login(access_token: str) -> Tuple[Optional[str], str]:
    """获取当前用户的 login（账号名）。Returns: (login|None, err_msg)"""
    resp = requests.get(GITEE_USER_URL, params={"access_token": access_token}, timeout=10)
    if resp.status_code != 200:
        return None, f"获取用户信息失败：{parse_error(resp)}"

    owner = resp.json().get("login")
    if not isinstance(owner, str) or not owner:
        return None, "获取用户信息失败：login 字段无效"

    logger.info(f"[GiteeOAuth] 用户: {owner}")
    return owner, ""


def ensure_repo(token: str, owner: str, repo: str, private: bool) -> Tuple[bool, str]:
    """确保仓库存在，不存在则创建，并保证可见性与要求一致"""
    # 先检查仓库是否存在
    check_resp = requests.get(
        GITEE_REPO_URL.format(owner=owner, repo=repo),
        params={"access_token": token},
        timeout=10,
    )
    if check_resp.status_code == 200:
        # 仓库存在 → 检查可见性是否匹配，不匹配则更新
        existing = check_resp.json()
        current_private = existing.get("private", False)
        if current_private != private:
            logger.info(f"[GiteeOAuth] 仓库已存在，更新可见性: {owner}/{repo} private={private}")
            patch_resp = requests.patch(
                GITEE_REPO_URL.format(owner=owner, repo=repo),
                data={
                    "access_token": token,
                    "name": repo,
                    "private": "true" if private else "false",
                },
                timeout=10,
            )
            if patch_resp.status_code == 200:
                return True, f"仓库已存在，可见性已更新：{owner}/{repo}"
            else:
                err = parse_error(patch_resp)
                logger.warning(f"[GiteeOAuth] 更新可见性失败: {err}")
                return False, f"更新仓库可见性失败：{err}"
        logger.info(f"[GiteeOAuth] 仓库已存在: {owner}/{repo}，复用")
        return True, f"复用已有仓库：{owner}/{repo}"

    # 创建仓库
    logger.info(f"[GiteeOAuth] 创建仓库: {owner}/{repo}")
    create_resp = requests.post(
        GITEE_CREATE_REPO_URL,
        data={
            "access_token": token,
            "name": repo,
            "description": "DriFox 自动创建的上传仓库",
            "auto_init": "true",
        },
        timeout=15,
    )
    if create_resp.status_code not in (201, 200):
        err = parse_error(create_resp)
        return False, f"创建仓库失败：{err}"

    # 创建后显式设置可见性（创建 API 的 private 参数不可靠）
    logger.info(f"[GiteeOAuth] 设置可见性: {owner}/{repo} private={private}")
    visibility = "true" if private else "false"
    patch_resp = requests.patch(
        GITEE_REPO_URL.format(owner=owner, repo=repo),
        data={"access_token": token, "name": repo, "private": visibility},
        timeout=10,
    )
    if patch_resp.status_code != 200:
        err = parse_error(patch_resp)
        logger.warning(f"[GiteeOAuth] 设置可见性失败: {err}")
        return False, f"设置仓库可见性失败：{err}"

    logger.info(f"[GiteeOAuth] 仓库创建成功: {owner}/{repo} (private={private})")
    return True, f"仓库已创建：{owner}/{repo}"


# ── 平台后端实现 ──────────────────────────────────────────


class GiteeOAuthBackend(OAuthBackend):
    """Gitee 平台 OAuth 后端"""

    name = "gitee"
    display_name = "Gitee"

    def bind(self, **options) -> Tuple[bool, str]:
        """
        绑定 Gitee 账号（阻塞，约 120s 超时）。

        使用 base.run_authorization_code_flow 完成通用授权码流程，
        再执行 Gitee 特有的初始化：拉用户信息、确保仓库存在、写配置。

        Args:
            repo_private (bool): 上传仓库是否私有，默认 True
        """
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        client_id = cfg.gitee_oauth_client_id.value
        client_secret = cfg.gitee_oauth_client_secret.value

        if not client_id or not client_secret:
            return False, "OAuth 凭证未配置"

        # 1. 构建 OAuth 应用配置
        app_cfg = OAuthAppConfig(
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=GITEE_AUTHORIZE_URL,
            token_url=GITEE_TOKEN_URL,
            scope="user_info projects",
        )

        # 2. 执行通用授权码流程（阻塞，起回调服务器 → 开浏览器 → 等授权 → 换 token）
        token_data, err = run_authorization_code_flow(app_cfg)
        if token_data is None:
            return False, err

        access_token = token_data["access_token"]

        # 3. 获取用户信息（Gitee 特有）
        owner, err = fetch_user_login(access_token)
        if not owner:
            return False, err

        # 4. 检查/创建上传仓库
        repo_private = bool(options.get("repo_private", True))
        repo_ok, repo_msg = ensure_repo(access_token, owner, FIXED_REPO_NAME, repo_private)
        if not repo_ok:
            return False, repo_msg

        # 4b. 创建配置备份仓库（强制私有，失败不阻断绑定流程）
        settings_ok, settings_msg = ensure_repo(access_token, owner, SETTINGS_REPO_NAME, private=True)
        if settings_ok:
            logger.info(f"[GiteeOAuth] {SETTINGS_REPO_NAME} 已就绪")
        else:
            logger.warning(f"[GiteeOAuth] {SETTINGS_REPO_NAME} 创建失败: {settings_msg}（不影响绑定）")

        # 5. 存储到配置
        cfg.gitee_user_token.value = access_token
        cfg.gitee_user_owner.value = owner
        cfg.gitee_user_repo.value = FIXED_REPO_NAME
        cfg.gitee_bound.value = True
        try:
            cfg.save()
        except Exception as e:
            logger.error(f"[OAuth] 绑定后保存配置失败: {e}")
            return False, f"绑定失败（配置保存错误）：{e}"

        logger.info(f"[OAuth] 平台绑定成功: {self.name} — {owner}/{FIXED_REPO_NAME}")
        return True, f"绑定成功！仓库：{owner}/{FIXED_REPO_NAME}"

    def unbind(self) -> Tuple[bool, str]:
        """解绑 Gitee 账号，清除本地 token"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        cfg.gitee_bound.value = False
        cfg.gitee_user_token.value = ""
        cfg.gitee_user_owner.value = ""
        cfg.gitee_user_repo.value = ""
        try:
            cfg.save()
        except Exception as e:
            logger.error(f"[OAuth] 解绑时保存配置失败: {e}")
            return False, f"解绑失败（配置保存错误）：{e}"

        logger.info(f"[OAuth] 平台已解绑: {self.name}")
        return True, "已解绑 Gitee 账号"

    def is_bound(self) -> bool:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        return bool(cfg.gitee_bound.value)

    def get_bound_info(self) -> Optional[dict]:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        if not cfg.gitee_bound.value:
            return None
        return {
            "owner": cfg.gitee_user_owner.value,
            "repo": cfg.gitee_user_repo.value,
            "token": cfg.gitee_user_token.value,
        }
