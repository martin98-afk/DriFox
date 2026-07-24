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
REMOTE_RECORDS_PATH = "drifox/share_records.json"
SHA_CACHE_FILE = ".sync_shas.json"
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
    """获取 user-custom 绑定前备份路径（放 .drifox/ 根目录，避免触发插件 watch）"""
    return get_app_data_dir() / "user-custom.pre_bind"


def _get_records_path() -> Path:
    """获取分享记录文件路径"""
    return get_app_data_dir() / "share" / "records.json"


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
    stateChanged = pyqtSignal(str)  # idle / syncing / error / disabled
    syncDone = pyqtSignal(bool, str)  # success, message
    _configChanged = pyqtSignal()  # 文件变更通知（watch 线程 → 主线程）

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
        self._records_path: Path = _get_records_path().resolve()
        self._debounce_timer: Optional[QTimer] = None
        self._upload_lock = threading.Lock()
        self._watch_stop: Optional[threading.Event] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._suppress_until: float = 0.0  # 下载后抑制上传的时间窗口（时间戳）
        self._upload_thread: Optional[threading.Thread] = None  # 后台上传线程引用
        self._config_dirty: bool = False  # app.config 有待上传的变更
        self._custom_dirty: bool = False  # user-custom 有待上传的变更
        self._records_dirty: bool = False  # share_records.json 有待上传的变更
        self._config_remote_sha: Optional[str] = None  # 远端 app.config SHA 缓存
        self._custom_remote_sha: Optional[str] = None  # 远端 user-custom.zip SHA 缓存
        self._records_remote_sha: Optional[str] = None  # 远端 share_records.json SHA 缓存
        self._sha_cache_path: Path = get_app_data_dir().resolve() / SHA_CACHE_FILE

        # 从磁盘恢复持久化的 SHA 缓存（避免每次重启全量下载）
        self._load_sha_cache()

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

        # 残留清理由 _start_watching 内部的 _stop_watching 处理
        self._token = token
        self._owner = owner
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)

        self._set_state("idle")
        # 先设抑制窗口再启动 watch，防止 initial_sync 过程中的文件事件触发防抖上传
        self._suppress_until = time.time() + 30.0
        self._start_watching()

        # 异步检查远端并恢复/上传
        t = threading.Thread(target=self._initial_sync, daemon=True)
        t.start()

    def disable(self):
        """解绑前调用：停止文件监听和防抖计时器，清除 SHA 缓存"""
        self._stop_watching()
        if self._debounce_timer:
            self._debounce_timer.stop()
            self._debounce_timer = None
        self._token = ""
        self._owner = ""
        # 清除 SHA 缓存（磁盘 + 内存），防止解绑后重新绑定时误判跳过下载
        self._config_remote_sha = None
        self._custom_remote_sha = None
        self._records_remote_sha = None
        try:
            if self._sha_cache_path.exists():
                self._sha_cache_path.unlink()
        except Exception:
            pass
        self._set_state("disabled")

    def upload(self) -> bool:
        """强制立即上传当前配置、user-custom 和分享记录到远端（公开方法，供手动重试）"""
        self._config_dirty = True
        self._custom_dirty = True
        self._records_dirty = True
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
        # 确保旧线程已完全退出后再启动新线程，避免新旧线程同时监听导致重复触发
        self._stop_watching()
        self._watch_stop = threading.Event()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()
        logger.info("[ConfigSync] 文件监听已启动")

    def _stop_watching(self):
        if self._watch_stop:
            self._watch_stop.set()
        # 等待旧线程真正退出（watchfiles 的 stop_event 机制使其快速响应）
        old_thread = self._watch_thread
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=3.0)
        self._watch_stop = None
        self._watch_thread = None
        logger.info("[ConfigSync] 文件监听已停止")

    def _watch_loop(self):
        from watchfiles import watch

        cfg_dir = str(self._config_path.parent)
        cfg_name = self._config_path.name
        records_name = self._records_path.name

        # 构建监听路径列表（app.config 目录 + user-custom 目录）
        watch_dirs = [cfg_dir]
        uc_path_str = str(self._user_custom_path)
        if self._user_custom_path.exists():
            watch_dirs.append(uc_path_str)

        # 如果 share 目录存在，监听 records.json 所在的 share 目录
        records_dir = str(self._records_path.parent)
        if self._records_path.parent.exists():
            watch_dirs.append(records_dir)

        logger.info(f"[ConfigSync] watch 线程已启动，监控目录={watch_dirs}")
        try:
            for changes in watch(*watch_dirs, stop_event=self._watch_stop):
                if self._watch_stop and self._watch_stop.is_set():
                    break
                for change_type, changed_path in changes:
                    changed_p = Path(changed_path)
                    # app.config 变更
                    if changed_p.name == cfg_name:
                        logger.info(f"[ConfigSync] 检测到配置变更: {changed_path}")
                        self._config_dirty = True
                        self._on_config_changed()
                        continue
                    # share_records.json 变更
                    if changed_p.name == records_name:
                        logger.info(f"[ConfigSync] 检测到分享记录变更: {changed_path}")
                        self._records_dirty = True
                        self._on_config_changed()
                        continue
                    # user-custom 目录下文件变更（排除目录本身 mtime 变化）
                    if (
                        self._user_custom_path.exists()
                        and changed_p != self._user_custom_path
                        and changed_p.is_relative_to(self._user_custom_path)
                    ):
                        logger.info(f"[ConfigSync] 检测到用户插件变更: {changed_path}")
                        self._custom_dirty = True
                        self._on_config_changed()
                        continue
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
        """防抖到期 → 检查抑制窗口 → 启动后台上传线程"""
        if time.time() < self._suppress_until:
            logger.debug("[ConfigSync] 下载后抑制窗口内，取消本次上传")
            return
        logger.info("[ConfigSync] 防抖到期，启动后台上传线程")
        t = threading.Thread(target=self._do_upload, daemon=True)
        t.start()
        self._upload_thread = t

    # ── 远端同步 ──────────────────────────────────────────

    def _initial_sync(self):
        """绑定后首次同步：检查远端 → 下载/上传（含 app.config + user-custom 插件）"""
        self._set_state("syncing")
        try:
            remote_status = self._check_remote()
            if remote_status is True:
                # 远端有配置 → 下载恢复
                logger.info("[ConfigSync] 远端有配置，开始恢复…")
                ok = self._do_download()
                if ok:
                    self._set_state("idle")
                    self.syncDone.emit(True, "配置与用户插件已从云端恢复")
                else:
                    self._set_state("error")
                    self.syncDone.emit(False, "配置与用户插件恢复失败")
            elif remote_status is False:
                # 远端确定无配置 → 上传当前（全量）
                logger.info("[ConfigSync] 远端无配置，开始上传…")
                self._config_dirty = True
                self._custom_dirty = True
                self._records_dirty = True
                ok = self._do_upload()
                if ok:
                    self._set_state("idle")
                    self.syncDone.emit(True, "配置与用户插件已备份到云端")
                else:
                    self._set_state("error")
                    self.syncDone.emit(False, "配置与用户插件备份失败")
            else:
                # 网络异常无法确定远端状态 → 保守处理，不上传避免覆盖云端好配置
                logger.warning("[ConfigSync] 无法确认远端配置状态（网络异常），跳过本次同步")
                self._set_state("error")
                self.syncDone.emit(False, "网络异常，无法同步")
        finally:
            # 同步完成（无论成败），释放 enable() 设的长抑制窗口
            # 保留 5s 冷静期，让下载/解压的级联事件自然平息
            self._suppress_until = time.time() + 5.0

    def _check_remote(self) -> bool | None:
        """检查远端是否存在 app.config。

        Returns:
            True: 文件存在
            False: 文件不存在（404）
            None: 无法确定（网络异常），调用方应保守处理不上传
        """
        return self._check_remote_file(REMOTE_PATH)

    def _check_remote_file(self, remote_path: str) -> bool | None:
        """检查远端特定文件是否存在。

        Returns:
            True: 文件存在
            False: 文件不存在（404）
            None: 无法确定（网络异常），调用方应保守处理
        """
        if not self._token or not self._owner:
            return None
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                resp = client.get(url, params={"access_token": self._token})
            if resp.status_code != 200:
                return False
            body = resp.json()
            return isinstance(body, dict) and "content" in body
        except Exception:
            return None

    def _do_upload(self) -> bool:
        """上传有变更的项到远端（基于 _config_dirty / _custom_dirty 脏标记）。

        防抖触发时只上传被标记的项；
        手动 upload() / 首次同步强制全量（两项都标记）。
        """
        if not self._token or not self._owner:
            logger.warning("[ConfigSync] 无法上传：缺少 token/owner")
            return False

        # 抑制窗口检查已在 _on_config_changed_main / _on_debounce_timeout 中执行，
        # 不在 _do_upload 中重复检查——否则会误拦截 _initial_sync 的显式上传。
        if not self._config_dirty and not self._custom_dirty and not self._records_dirty:
            logger.debug("[ConfigSync] 无可上传的变更，跳过")
            return True

        # 读取脏标记后立刻清空，避免重复触发
        upload_config = self._config_dirty
        upload_custom = self._custom_dirty
        upload_records = self._records_dirty
        self._config_dirty = False
        self._custom_dirty = False
        self._records_dirty = False

        acquired = self._upload_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("[ConfigSync] 上传已在进行中，跳过")
            return False

        try:
            config_ok = True
            custom_ok = True
            records_ok = True

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

            # ── 3. 上传分享记录（仅脏时） ──
            if upload_records:
                if self._records_path.exists():
                    records_ok = self._upload_file(
                        local_path=self._records_path,
                        remote_path=REMOTE_RECORDS_PATH,
                        label="share_records",
                    )
                else:
                    # 本地无分享记录，跳过（不清除远端）
                    records_ok = True

            if config_ok and custom_ok and records_ok:
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
                    # 缓存远端 SHA，避免下次下载时重复拉取
                    try:
                        resp_data = resp.json()
                        content_data = resp_data.get("content")
                        if isinstance(content_data, dict):
                            sha = content_data.get("sha", "")
                            if sha:
                                if label == "app.config":
                                    self._config_remote_sha = sha
                                elif label == "user-custom":
                                    self._custom_remote_sha = sha
                                elif label == "share_records":
                                    self._records_remote_sha = sha
                                self._save_sha_cache()
                    except Exception:
                        pass
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
                            # 缓存重试后的 SHA
                            try:
                                resp2_data = resp2.json()
                                content2 = resp2_data.get("content")
                                if isinstance(content2, dict):
                                    sha2 = content2.get("sha", "")
                                    if sha2:
                                        if label == "app.config":
                                            self._config_remote_sha = sha2
                                        elif label == "user-custom":
                                            self._custom_remote_sha = sha2
                                        elif label == "share_records":
                                            self._records_remote_sha = sha2
                                        self._save_sha_cache()
                            except Exception:
                                pass
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
        """从远端下载配置和 user-custom 插件，覆盖本地（SHA 未变则跳过）"""
        if not self._token or not self._owner:
            return False

        results: list[bool] = []
        try:
            # ★ 进入下载立即取消防抖计时，防止之前 watch 事件的残留防抖与下载流程并发
            if self._debounce_timer:
                self._debounce_timer.stop()
            # ★ 设 30s 抑制窗口，覆盖下载写盘 + Settings.load 级联效应的全过程
            self._suppress_until = time.time() + 30.0

            # ── 1. app.config：独立下载并立即应用，不等其他项 ──
            try:
                remote_config_sha = self._get_remote_file_sha(REMOTE_PATH)
                if remote_config_sha and remote_config_sha == self._config_remote_sha:
                    logger.debug("[ConfigSync] 远端 app.config 未变化，跳过下载")
                    results.append(True)
                else:
                    if self._download_file(REMOTE_PATH, self._config_path, label="app.config"):
                        logger.info("[ConfigSync] app.config 已下载并立即应用")
                        try:
                            Settings.get_instance().load()
                            logger.info("[ConfigSync] Settings 已重新加载")
                        except Exception as e:
                            logger.warning(f"[ConfigSync] Settings 重载失败: {e}")
                        results.append(True)
                    else:
                        results.append(False)
            except Exception as e:
                logger.error(f"[ConfigSync] app.config 下载异常: {e}")
                results.append(False)

            # ── 2. user-custom：独立下载（可能很大，失败不影响 app.config） ──
            try:
                rc = self._check_remote_file(USER_CUSTOM_REMOTE_PATH)
                if rc is True:
                    remote_custom_sha = self._get_remote_file_sha(USER_CUSTOM_REMOTE_PATH)
                    if remote_custom_sha and remote_custom_sha == self._custom_remote_sha:
                        logger.debug("[ConfigSync] 远端 user-custom 未变化，跳过下载")
                        results.append(True)
                    else:
                        ok = self._download_user_custom_zip()
                        if ok:
                            logger.debug("[ConfigSync] user-custom 已解压恢复")
                        else:
                            logger.warning("[ConfigSync] user-custom 恢复失败")
                        results.append(ok)
                elif rc is None:
                    logger.warning("[ConfigSync] 无法确认远端 user-custom 状态，跳过")
                    results.append(False)
                else:
                    # rc is False: 远端没有 user-custom
                    results.append(True)
            except Exception as e:
                logger.error(f"[ConfigSync] user-custom 下载异常: {e}")
                results.append(False)

            # ── 3. share_records.json：独立下载 ──
            try:
                rc = self._check_remote_file(REMOTE_RECORDS_PATH)
                if rc is True:
                    remote_records_sha = self._get_remote_file_sha(REMOTE_RECORDS_PATH)
                    if remote_records_sha and remote_records_sha == self._records_remote_sha:
                        logger.debug("[ConfigSync] 远端 share_records 未变化，跳过下载")
                        results.append(True)
                    else:
                        ok = self._download_file(
                            REMOTE_RECORDS_PATH,
                            self._records_path,
                            label="share_records",
                        )
                        results.append(ok)
                elif rc is None:
                    logger.warning("[ConfigSync] 无法确认远端 share_records 状态，跳过")
                    results.append(False)
                else:
                    # rc is False: 远端没有 share_records
                    results.append(True)
            except Exception as e:
                logger.error(f"[ConfigSync] share_records 下载异常: {e}")
                results.append(False)

            # 清除下载本身触发的文件变更脏标记
            self._config_dirty = False
            self._custom_dirty = False
            self._records_dirty = False
            logger.debug("[ConfigSync] 已清除下载触发的脏标记")

            # 任何一项成功就算部分成功，不因 user-custom 失败而否定 app.config 恢复
            return any(results)
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

            # 缓存远端 SHA，避免下次重复下载
            remote_sha = body.get("sha", "")
            if remote_sha:
                if label == "app.config":
                    self._config_remote_sha = remote_sha
                elif label == "user-custom":
                    self._custom_remote_sha = remote_sha
                elif label == "share_records":
                    self._records_remote_sha = remote_sha
                self._save_sha_cache()

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

            # 解压前备份当前 user-custom（异常时回滚，备份放 .drifox/ 根目录避免触发插件 watch）
            rollback_path = get_app_data_dir() / "user-custom.backup"
            if self._user_custom_path.exists():
                if rollback_path.exists():
                    shutil.rmtree(str(rollback_path))
                shutil.move(str(self._user_custom_path), str(rollback_path))

            try:
                # 先解压到临时目录（不在插件 watch 路径内），再原子重命名到目标路径。
                # 避免 watchfiles 逐文件检测到 184+ 个写入事件 → PluginManager 反复
                # 在 manifest 未就绪时 rescan → 插件被移除/重载多次。
                _extract_ok = False
                _tmp_extract = get_app_data_dir() / "user-custom.extract"
                for _attempt in range(3):
                    try:
                        if _tmp_extract.exists():
                            shutil.rmtree(str(_tmp_extract))
                        _tmp_extract.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(str(zip_path), "r") as zf:
                            zf.extractall(str(_tmp_extract))
                        _extract_ok = True
                        break
                    except (PermissionError, OSError) as _e:
                        logger.warning(f"[ConfigSync] user-custom 解压文件被占用，重试中 ({_attempt + 1}/3)... {_e}")
                        time.sleep(1.0)
                if not _extract_ok:
                    raise RuntimeError("user-custom 解压失败：文件持续被占用")

                # 原子重命名到目标路径（单次目录操作，watch 只看到一个事件）
                if self._user_custom_path.exists():
                    shutil.rmtree(str(self._user_custom_path))
                shutil.move(str(_tmp_extract), str(self._user_custom_path))
            except RuntimeError, zipfile.BadZipFile:
                # 解压失败 → 回滚
                if self._user_custom_path.exists():
                    shutil.rmtree(str(self._user_custom_path))
                if rollback_path.exists():
                    shutil.move(str(rollback_path), str(self._user_custom_path))
                raise
            finally:
                # 确保临时目录被清理
                try:
                    _tmp_extract = get_app_data_dir() / "user-custom.extract"
                    if _tmp_extract.exists():
                        shutil.rmtree(str(_tmp_extract))
                except Exception:
                    pass

            # 解压成功后清理临时文件（失败不阻塞，不触发回滚）
            logger.info("[ConfigSync] user-custom 插件已从云端恢复")
            try:
                zip_path.unlink()
            except Exception:
                pass
            try:
                if rollback_path.exists():
                    shutil.rmtree(str(rollback_path))
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"[ConfigSync] user-custom 下载解压失败: {e}")
            # 清理残留
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except Exception:
                pass
            return False

    # ── SHA 缓存持久化 ───────────────────────────────────

    def _load_sha_cache(self):
        """从磁盘加载持久化的远端 SHA 缓存"""
        try:
            if self._sha_cache_path.exists():
                import json

                data = json.loads(self._sha_cache_path.read_text(encoding="utf-8"))
                self._config_remote_sha = data.get("config_sha") or None
                self._custom_remote_sha = data.get("custom_sha") or None
                self._records_remote_sha = data.get("records_sha") or None
                logger.debug(
                    f"[ConfigSync] SHA 缓存已加载: config={self._config_remote_sha[:12] if self._config_remote_sha else None}..."
                )
        except Exception as e:
            logger.warning(f"[ConfigSync] SHA 缓存加载失败: {e}")

    def _save_sha_cache(self):
        """持久化当前远端 SHA 到磁盘"""
        try:
            import json

            data = {}
            if self._config_remote_sha:
                data["config_sha"] = self._config_remote_sha
            if self._custom_remote_sha:
                data["custom_sha"] = self._custom_remote_sha
            if self._records_remote_sha:
                data["records_sha"] = self._records_remote_sha
            self._sha_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._sha_cache_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[ConfigSync] SHA 缓存持久化失败: {e}")

    # ── 辅助 ──────────────────────────────────────────────

    @staticmethod
    def _parse_error(resp) -> str:
        """解析 API 错误响应（兼容 httpx.Response）"""
        try:
            body = resp.json()
            return body.get("message", resp.text[:200])
        except Exception:
            return resp.text[:200]
