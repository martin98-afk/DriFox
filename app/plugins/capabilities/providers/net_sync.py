# -*- coding: utf-8 -*-
"""网络与云同步的 host 无关能力 provider（net.avatar / net.download / sync.register）。

三个实现都不依赖 host（数据源是 OAuth 后端 / GiteeUploader / ConfigSyncService
单例），ui 与 tool 两个 surface 共用同一实现（bootstrap 分别注册）。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from loguru import logger


def _avatar_cache_path(owner: str):
    """头像磁盘缓存路径：与宿主设置卡（gitee_card）共用同一份。"""
    from app.utils.utils import get_app_data_dir

    return get_app_data_dir() / "cache" / f"gitee_avatar_{owner}.png"


def _read_cached_avatar(owner: str) -> Optional[str]:
    """读缓存头像路径（只 stat，无网络，主线程安全）；未命中返回 None。"""
    if not owner:
        return None
    try:
        p = _avatar_cache_path(owner)
        return str(p) if p.exists() and p.stat().st_size > 0 else None
    except OSError:
        return None


def net_avatar(host: Any):
    """Gitee 账户头像能力。

    返回 dict：
    - ``fetch() -> Optional[str]``：确保头像已缓存并返回本地路径；缓存未命中时
      走网络拉取（内部可能触发 token 刷新与图片下载，**须在后台线程调用**）。
      未绑定 / 失败返回 None。
    - ``cached_path() -> Optional[str]``：纯本地查缓存路径（只读 Settings 与
      文件 stat，主线程安全）；未绑定 / 未命中返回 None。
    """

    def fetch() -> Optional[str]:
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.auth.gitee import fetch_user_avatar_url

            info = get_oauth_backend("gitee").get_bound_info()
            if not info:
                return None
            owner = str(info.get("owner") or "")
            cached = _read_cached_avatar(owner)
            if cached:
                return cached
            url, err = fetch_user_avatar_url(info["token"])
            if not url:
                logger.warning(f"[Capability:net.avatar] 拉取头像 URL 失败: {err}")
                return None
            import requests as _requests

            # 显式 UA：foruda CDN 对空 UA 偶发返回降级图（与 gitee_card 同口径）
            resp = _requests.get(url, timeout=10, headers={"User-Agent": "DriFox/1.0"})
            if resp.status_code != 200 or not resp.content:
                logger.warning(f"[Capability:net.avatar] 下载头像失败: HTTP {resp.status_code}")
                return None
            cache = _avatar_cache_path(owner)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(resp.content)
            return str(cache)
        except Exception as e:  # noqa: BLE001 - 能力边界内消化异常，返回 None
            logger.warning(f"[Capability:net.avatar] 获取头像异常: {e}")
            return None

    def cached_path() -> Optional[str]:
        # 刻意不走 get_bound_info()：那会触发 token 刷新网络调用，不能进主线程。
        # 直读 Settings 的绑定标记与 owner（纯本地）。
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            if not bool(cfg.gitee_bound.value):
                return None
            return _read_cached_avatar(str(cfg.gitee_user_owner.value or ""))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[Capability:net.avatar] cached_path 失败: {e}")
            return None

    return {"fetch": fetch, "cached_path": cached_path}


def net_download(host: Any):
    """从 Gitee 绑定仓库下载文件到本地路径。

    返回 ``download(remote_path, local_path) -> (local_path_str|None, err|None)``。
    """

    def download(remote_path: str, local_path: str) -> Tuple[Optional[str], Optional[str]]:
        from app.gateway.utils.gitee_uploader import get_gitee_uploader

        return get_gitee_uploader().download_file(remote_path, local_path)

    return download


def sync_register(host: Any):
    """注册本地目录进入云同步监控（目录内容将打包备份到云端）。

    返回 ``register(provider_id, label, local_dir, remote_path="", enabled=True) -> bool``。
    """

    def register(
        provider_id: str,
        label: str = "",
        local_dir: str = "",
        remote_path: str = "",
        enabled: bool = True,
    ) -> bool:
        try:
            from app.core.sync.config_sync import register_sync_content_provider

            return register_sync_content_provider(
                provider_id=provider_id,
                label=label,
                local_dir=local_dir,
                remote_path=remote_path,
                enabled=enabled,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Capability:sync.register] 注册同步内容失败 ({provider_id}): {e}")
            return False

    return register
