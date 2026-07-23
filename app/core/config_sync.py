# -*- coding: utf-8 -*-
"""
云端配置同步服务 (ConfigSyncService)

单例模式。基于已有 Gitee OAuth 绑定机制，实现 app.config 与 user-custom 插件的云端自动备份与恢复：
- 绑定前备份本地配置和 user-custom 插件 → 解绑时恢复
- 运行时监听配置和 user-custom 变更，10s 防抖自动上传
- 绑定后自动检查远端，有则下载覆盖
"""

import base64
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.utils.config import Settings
from app.utils.utils import get_app_data_dir

# ── 常量 ──────────────────────────────────────────────────
SETTINGS_REPO_NAME = "DriFox_settings"
REMOTE_PATH = "drifox/app.config"
USER_CUSTOM_REMOTE_PATH = "drifox/user-custom.zip"
DEBOUNCE_MS = 10000

GITEE_FILE_API = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"
GITEE_REPO_API = "https://gitee.com/api/v5/repos/{owner}/{repo}"


def _get_config_path() -> Path:
    """获取 app.config 文件路径"""
    return get_app_data_dir() / "app.config"


def _get_backup_path() -> Path:
    """获取绑定前备份路径"""
    return get_app_data_dir() / "app.config.pre_bind"


def _get_user_custom_path() -> Path:
    """获取 user-custom 插件目录路径"""
    return get_app_data_dir() / "plugins" / "user-custom"


def _get_user_custom_backup_path() -> Path:
    """获取 user-custom 绑定前备份路径"""
    return get_app_data_dir() / "plugins" / "user-custom.pre_bind"


# ── ConfigSyncService ─────────────────────────────────────


class ConfigSyncService(QObject):
    """
    云端配置同步服务（单例）

    用法:
        svc = ConfigSyncService.get_instance()
        svc.enable(token, owner)   # 启动同步
        svc.disable()              # 停止同步
    """

    _instance: Optional["ConfigSyncService"] = None

    # 信号
    stateChanged = pyqtSignal(str)       # idle / syncing / error / disabled
    syncDone = pyqtSignal(bool, str)     # success, message
    _configChanged = pyqtSignal()        # 文件变更通知（watch 线程 → 主线程）

    def __init__(self):
        if ConfigSyncService._instance is not None:
            raise RuntimeError("ConfigSyncService 是单例，请使用 get_instance()")
        super().__init__()
        self._state: str = "disabled"
        self._token: str = ""
        self._owner: str = ""
        self._config_path: Path = _get_config_path().resolve()
        self._backup_path: Path = _get_backup_path().resolve()
        self._user_custom_path: Path = _get_user_custom_path().resolve()
        self._user_custom_backup_path: Path = _get_user_custom_backup_path().resolve()
        self._debounce_timer: Optional[QTimer] = None
        self._upload_lock = threading.Lock()
        self._watch_stop: Optional[threading.Event] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._suppress_until: float = 0.0  # 下载后抑制上传的时间窗口（时间戳）
        self._upload_thread: Optional[threading.Thread] = None  # 后台上传线程引用
        self._config_dirty: bool = False     # app.config 有待上传的变更
        self._custom_dirty: bool = False     # user-custom 有待上传的变更

        # 跨线程桥：watchfiles 线程 → 主线程
        self._configChanged.connect(self._on_config_changed_main)

    # ── 单例 ──────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "ConfigSyncService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 公开 API ──────────────────────────────────────────

    def backup_local(self) -> bool:
        """绑定前：备份本地 app.config → app.config.pre_bind，备份 user-custom → user-custom.pre_bind"""
        ok = True

        # 备份 app.config
        cfg = self._config_path
        bak = self._backup_path
        if cfg.exists():
            try:
                shutil.copy2(str(cfg), str(bak))
                logger.info(f"[ConfigSync] 已备份本地配置 → {bak.name}")
            except Exception as e:
                logger.error(f"[ConfigSync] app.config 备份失败: {e}")
                ok = False
        else:
            logger.warning("[ConfigSync] 本地配置文件不存在，跳过备份")

        # 备份 user-custom 插件
        uc = self._user_custom_path
        uc_bak = self._user_custom_backup_path
        if uc.exists():
            try:
                # 清除上一次备份（如有）
                if uc_bak.exists():
                    shutil.rmtree(str(uc_bak))
                shutil.copytree(str(uc), str(uc_bak))
                logger.info(f"[ConfigSync] 已备份 user-custom 插件 → {uc_bak.name}")
            except Exception as e:
                logger.error(f"[ConfigSync] user-custom 备份失败: {e}")
                ok = False
        else:
            logger.info("[ConfigSync] user-custom 插件目录不存在，跳过备份")

        return ok

    def restore_local(self) -> bool:
        """解绑后：恢复备份 app.config.pre_bind → app.config，恢复 user-custom.pre_bind → user-custom"""
        ok = True

        # 恢复 app.config
        bak = self._backup_path
        cfg = self._config_path
        if bak.exists():
            try:
                shutil.copy2(str(bak), str(cfg))
                logger.info(f"[ConfigSync] 已恢复本地配置 ← {bak.name}")
                os.remove(str(bak))
            except Exception as e:
                logger.error(f"[ConfigSync] app.config 恢复失败: {e}")
                ok = False
        else:
            logger.info("[ConfigSync] 无 app.config 备份，跳过恢复")

        # 恢复 user-custom 插件
        uc_bak = self._user_custom_backup_path
        uc = self._user_custom_path
        if uc_bak.exists():
            try:
                if uc.exists():
                    shutil.rmtree(str(uc))
                shutil.copytree(str(uc_bak), str(uc))
                logger.info(f"[ConfigSync] 已恢复 user-custom 插件 ← {uc_bak.name}")
                shutil.rmtree(str(uc_bak))
            except Exception as e:
                logger.error(f"[ConfigSync] user-custom 恢复失败: {e}")
                ok = False
        else:
            logger.info("[ConfigSync] 无 user-custom 备份，跳过恢复")

        return ok

    def enable(self, token: str, owner: str):
        """绑定成功后启动同步：启动文件监听 → 检查远端 → 自动恢复或上传"""
        logger.info(f"[ConfigSync] enable token={token[:8]}... owner={owner}")
        logger.info(f"[ConfigSync] config_path={self._config_path}")
        logger.info(f"[ConfigSync] cfg_dir={self._config_path.parent}")
        logger.info(f"[ConfigSync] cfg_name={self._config_path.name}")
        logger.info(f"[ConfigSync] user_custom_path={self._user_custom_path}")

        if self._state != "disabled":
            logger.warning("[ConfigSync] 已在运行中，忽略重复 enable")
            return

        # 确保任何残留的旧监听线程已停止
        self._stop_watching()

        self._token = token
        self._owner = owner
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)

        self._set_state("idle")
        self._start_watching()

        # 异步检查远端并恢复/上传
        t = threading.Thread(target=self._initial_sync, daemon=True)
        t.start()

    def disable(self):
        """解绑前调用：停止文件监听和防抖计时器"""
        self._stop_watching()
        if self._debounce_timer:
            self._debounce_timer.stop()
            self._debounce_timer = None
        self._token = ""
        self._owner = ""
        self._set_state("disabled")

    def upload(self) -> bool:
        """强制立即上传当前配置和 user-custom 到远端（公开方法，供手动重试）"""
        self._config_dirty = True
        self._custom_dirty = True
        return self._do_upload()

    def download(self) -> bool:
        """强制从远端下载配置并覆盖本地（公开方法）"""
        return self._do_download()

    # ── 状态管理 ──────────────────────────────────────────

    def _set_state(self, state: str):
        self._state = state
        self.stateChanged.emit(state)

    # ── 文件监听 ──────────────────────────────────────────

    def _start_watching(self):
        self._watch_stop = threading.Event()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()
        logger.info("[ConfigSync] 文件监听已启动")

    def _stop_watching(self):
        if self._watch_stop:
            self._watch_stop.set()
            self._watch_stop = None
        self._watch_thread = None
        logger.info("[ConfigSync] 文件监听已停止")

    def _watch_loop(self):
        from watchfiles import watch

        cfg_dir = str(self._config_path.parent)
        cfg_name = self._config_path.name

        # 构建监听路径列表（app.config 目录 + user-custom 目录）
        watch_dirs = [cfg_dir]
        uc_path_str = str(self._user_custom_path)
        if self._user_custom_path.exists():
            watch_dirs.append(uc_path_str)

        logger.info(f"[ConfigSync] watch 线程已启动，监控目录={watch_dirs}")
        try:
            for changes in watch(*watch_dirs):
                if self._watch_stop and self._watch_stop.is_set():
                    break
                for change_type, changed_path in changes:
                    changed_p = Path(changed_path)
                    # app.config 变更
                    if changed_p.name == cfg_name:
                        logger.info(f"[ConfigSync] 检测到配置变更: {changed_path}")
                        self._config_dirty = True
                        self._on_config_changed()
                        break
                    # user-custom 目录下文件变更（排除目录本身 mtime 变化）
                    if (self._user_custom_path.exists()
                            and changed_p != self._user_custom_path
                            and changed_p.is_relative_to(self._user_custom_path)):
                        logger.info(f"[ConfigSync] 检测到用户插件变更: {changed_path}")
                        self._custom_dirty = True
                        self._on_config_changed()
                        break
        except Exception as e:
            logger.error(f"[ConfigSync] 文件监听异常: {e}")

    def _on_config_changed(self):
        """文件变更回调（watchfiles 线程）→ 通过信号桥接到主线程"""
        self._configChanged.emit()

    def _on_config_changed_main(self):
        """主线程：重置防抖计时器。timeout 后由后台线程执行上传，不阻塞 UI。"""
        if self._state == "disabled":
            return
        # 下载后 5 秒内抑制上传，覆盖 下载→Settings重载→UI刷新→再写盘 的级联
        if time.time() < self._suppress_until:
            logger.debug("[ConfigSync] 处于下载后抑制窗口，跳过上传")
            return
        if self._debounce_timer:
            logger.info(f"[ConfigSync] 防抖计时器启动，{DEBOUNCE_MS}ms 后上传")
            self._debounce_timer.start(DEBOUNCE_MS)

    def _on_debounce_timeout(self):
        """防抖到期 → 启动后台上传线程，只上传有变更的项，不阻塞 UI"""
        logger.info("[ConfigSync] 防抖到期，启动后台上传线程")
        t = threading.Thread(target=self._do_upload, daemon=True)
        t.start()
        self._upload_thread = t

    # ── 远端同步 ──────────────────────────────────────────

    def _initial_sync(self):
        """绑定后首次同步：检查远端 → 下载/上传（含 app.config + user-custom 插件）"""
        self._set_state("syncing")
        if self._check_remote():
            # 远端有配置 → 下载恢复
            logger.info("[ConfigSync] 远端有配置，开始恢复…")
            ok = self._do_download()
            if ok:
                self._set_state("idle")
                self.syncDone.emit(True, "配置与用户插件已从云端恢复")
            else:
                self._set_state("error")
                self.syncDone.emit(False, "配置与用户插件恢复失败")
        else:
            # 远端无配置 → 上传当前（全量）
            logger.info("[ConfigSync] 远端无配置，开始上传…")
            self._config_dirty = True
            self._custom_dirty = True
            ok = self._do_upload()
            if ok:
                self._set_state("idle")
                self.syncDone.emit(True, "配置与用户插件已备份到云端")
            else:
                self._set_state("error")
                self.syncDone.emit(False, "配置与用户插件备份失败")

    def _check_remote(self) -> bool:
        """检查远端是否存在配置文件（200 + 返回 dict=文件存在，list=仅目录存在）"""
        return self._check_remote_file(REMOTE_PATH)

    def _check_remote_file(self, remote_path: str) -> bool:
        """检查远端特定文件是否存在"""
        if not self._token or not self._owner:
            return False
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                resp = client.get(url, params={"access_token": self._token})
            if resp.status_code != 200:
                return False
            body = resp.json()
            return isinstance(body, dict) and "content" in body
        except Exception:
            return False

    def _do_upload(self) -> bool:
        """上传有变更的项到远端（基于 _config_dirty / _custom_dirty 脏标记）。

        防抖触发时只上传被标记的项；
        手动 upload() / 首次同步强制全量（两项都标记）。
        """
        if not self._token or not self._owner:
            logger.warning("[ConfigSync] 无法上传：缺少 token/owner")
            return False

        if not self._config_dirty and not self._custom_dirty:
            logger.debug("[ConfigSync] 无可上传的变更，跳过")
            return True

        # 读取脏标记后立刻清空，避免重复触发
        upload_config = self._config_dirty
        upload_custom = self._custom_dirty
        self._config_dirty = False
        self._custom_dirty = False

        acquired = self._upload_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("[ConfigSync] 上传已在进行中，跳过")
            return False

        try:
            config_ok = True
            custom_ok = True

            # ── 1. 上传 app.config（仅脏时） ──
            if upload_config:
                if not self._config_path.exists():
                    logger.warning("[ConfigSync] 无法上传：配置文件不存在")
                    config_ok = False
                else:
                    config_ok = self._upload_file(
                        local_path=self._config_path,
                        remote_path=REMOTE_PATH,
                        label="app.config",
                    )

            # ── 2. 上传 user-custom 插件（仅脏时，压缩后上传） ──
            if upload_custom and self._user_custom_path.exists():
                custom_ok = self._upload_user_custom_zip()

            if config_ok and custom_ok:
                self._set_state("idle")
                return True

            self._set_state("error")
            return False
        except httpx.TimeoutException:
            logger.error("[ConfigSync] 上传超时")
            self._set_state("error")
            return False
        except httpx.ConnectError:
            logger.error("[ConfigSync] 网络连接失败")
            self._set_state("error")
            return False
        except Exception as e:
            logger.error(f"[ConfigSync] 上传异常: {e}")
            self._set_state("error")
            return False
        finally:
            self._upload_lock.release()

    def _upload_file(self, local_path: Path, remote_path: str, label: str = "") -> bool:
        """通用上传：将本地文件 base64 后 PUT/POST 到远端。

        Args:
            local_path: 本地文件路径
            remote_path: 远端路径（相对仓库根）
            label: 日志标识
        """
        tag = label or remote_path
        try:
            data = local_path.read_bytes()
            content_b64 = base64.b64encode(data).decode("utf-8")
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            payload = {
                "access_token": self._token,
                "content": content_b64,
                "message": f"DriFox sync {tag} ({time.strftime('%Y-%m-%d %H:%M:%S')})",
                "branch": "master",
            }

            with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
                # 查远端 sha
                existing_sha = self._get_remote_file_sha(remote_path, client)
                if existing_sha:
                    payload["sha"] = existing_sha
                    resp = client.put(url, data=payload)
                    method = "PUT"
                else:
                    resp = client.post(url, data=payload)
                    method = "POST"

                if resp.status_code in (200, 201):
                    logger.info(f"[ConfigSync] {tag} 上传成功 ({method})")
                    return True

                err_msg = self._parse_error(resp)
                if resp.status_code in (401, 403):
                    logger.error(f"[ConfigSync] {tag} 上传失败 (token 无效) [{resp.status_code}]: {err_msg}")
                    return False

                # PUT sha 冲突时重试一次
                if method == "PUT" and "sha" in err_msg.lower():
                    retry_sha = self._get_remote_file_sha(remote_path, client)
                    if retry_sha:
                        payload["sha"] = retry_sha
                        resp2 = client.put(url, data=payload)
                        if resp2.status_code in (200, 201):
                            logger.info(f"[ConfigSync] {tag} 上传成功 (PUT retry)")
                            return True

                logger.error(f"[ConfigSync] {tag} 上传最终失败 [{resp.status_code}]: {err_msg}")
                return False
        except Exception as e:
            logger.error(f"[ConfigSync] {tag} 上传异常: {e}")
            return False

    def _upload_user_custom_zip(self) -> bool:
        """压缩 user-custom 插件目录并上传到远端"""
        import zipfile

        zip_path = get_app_data_dir() / "user-custom.zip"
        try:
            # 压缩 user-custom 目录
            with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
                for f in self._user_custom_path.rglob("*"):
                    if f.is_file():
                        arcname = str(f.relative_to(self._user_custom_path))
                        zf.write(str(f), arcname)

            # 上传
            ok = self._upload_file(zip_path, USER_CUSTOM_REMOTE_PATH, label="user-custom")
            return ok
        except Exception as e:
            logger.error(f"[ConfigSync] user-custom 压缩上传失败: {e}")
            return False
        finally:
            # 清理临时 zip 文件
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except Exception:
                pass

    def _get_remote_file_sha(self, remote_path: str, client: Optional[httpx.Client] = None) -> Optional[str]:
        """查询远端特定文件的 sha，不存在返回 None。可复用已打开的 httpx.Client。"""
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            if client:
                resp = client.get(url, params={"access_token": self._token})
            else:
                with httpx.Client(timeout=httpx.Timeout(10.0)) as c:
                    resp = c.get(url, params={"access_token": self._token})
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict) and "sha" in body:
                    return body["sha"]
            return None
        except Exception:
            return None

    def _get_remote_sha(self, client: Optional[httpx.Client] = None) -> Optional[str]:
        """查询远端 app.config 文件的 sha（兼容旧调用方）"""
        return self._get_remote_file_sha(REMOTE_PATH, client)

    def _do_download(self) -> bool:
        """从远端下载配置和 user-custom 插件，覆盖本地"""
        if not self._token or not self._owner:
            return False

        config_ok = False
        try:
            # ── 1. 下载 app.config ──
            config_ok = self._download_file(REMOTE_PATH, self._config_path, label="app.config")

            if config_ok:
                # 写文件后抑制上传 5 秒
                self._suppress_until = time.time() + 5.0
                logger.info("[ConfigSync] 配置已从云端下载并覆盖本地")
                # 重新加载 Settings
                try:
                    Settings.get_instance().load()
                    logger.info("[ConfigSync] Settings 已重新加载")
                except Exception as e:
                    logger.warning(f"[ConfigSync] Settings 重载失败: {e}")

            # ── 2. 下载 user-custom 插件 ──
            custom_ok = True
            if self._check_remote_file(USER_CUSTOM_REMOTE_PATH):
                custom_ok = self._download_user_custom_zip()

            return config_ok and custom_ok
        except Exception as e:
            logger.error(f"[ConfigSync] 下载异常: {e}")
            return False

    def _download_file(self, remote_path: str, local_path: Path, label: str = "") -> bool:
        """通用下载：从远端下载文件到本地"""
        tag = label or remote_path
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
                resp = client.get(url, params={"access_token": self._token})
            if resp.status_code != 200:
                err = self._parse_error(resp)
                logger.error(f"[ConfigSync] {tag} 下载失败 [{resp.status_code}]: {err}")
                return False

            body = resp.json()
            if not isinstance(body, dict):
                logger.error(f"[ConfigSync] {tag} 远端路径是目录而非文件")
                return False
            content_b64 = body.get("content", "")
            if not content_b64:
                logger.error(f"[ConfigSync] {tag} 下载内容为空")
                return False

            decoded = base64.b64decode(content_b64)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(decoded)
            logger.info(f"[ConfigSync] {tag} 已下载到本地")
            return True
        except Exception as e:
            logger.error(f"[ConfigSync] {tag} 下载异常: {e}")
            return False

    def _download_user_custom_zip(self) -> bool:
        """从远端下载 user-custom.zip 并解压到 user-custom 目录"""
        import zipfile

        zip_path = get_app_data_dir() / "user-custom.zip"
        try:
            ok = self._download_file(USER_CUSTOM_REMOTE_PATH, zip_path, label="user-custom")
            if not ok:
                return False

            # 解压前备份当前 user-custom（异常时回滚）
            rollback_path = get_app_data_dir() / "plugins" / "user-custom.tmp"
            if self._user_custom_path.exists():
                if rollback_path.exists():
                    shutil.rmtree(str(rollback_path))
                shutil.move(str(self._user_custom_path), str(rollback_path))

            try:
                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    zf.extractall(str(self._user_custom_path))
                logger.info("[ConfigSync] user-custom 插件已从云端恢复")
                # 清理临时 zip 和回滚备份
                try:
                    zip_path.unlink()
                except Exception:
                    pass
                if rollback_path.exists():
                    shutil.rmtree(str(rollback_path))
                return True
            except Exception:
                # 解压失败 → 回滚
                if self._user_custom_path.exists():
                    shutil.rmtree(str(self._user_custom_path))
                if rollback_path.exists():
                    shutil.move(str(rollback_path), str(self._user_custom_path))
                raise
        except Exception as e:
            logger.error(f"[ConfigSync] user-custom 下载解压失败: {e}")
            # 清理残留
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except Exception:
                pass
            return False

    # ── 辅助 ──────────────────────────────────────────────

    @staticmethod
    def _parse_error(resp) -> str:
        """解析 API 错误响应（兼容 httpx.Response）"""
        try:
            body = resp.json()
            return body.get("message", resp.text[:200])
        except Exception:
            return resp.text[:200]
