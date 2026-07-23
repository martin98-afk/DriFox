# -*- coding: utf-8 -*-
"""
云端配置同步服务 (ConfigSyncService)

单例模式。基于已有 Gitee OAuth 绑定机制，实现 app.config 的云端自动备份与恢复：
- 绑定前备份本地配置 → 解绑时恢复
- 运行时监听配置变更，10s 防抖自动上传
- 绑定后自动检查远端，有则下载覆盖
"""

import base64
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.utils.config import Settings
from app.utils.utils import get_app_data_dir

# ── 常量 ──────────────────────────────────────────────────
SETTINGS_REPO_NAME = "DriFox_settings"
REMOTE_PATH = "drifox/app.config"
DEBOUNCE_MS = 10000

GITEE_FILE_API = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"
GITEE_REPO_API = "https://gitee.com/api/v5/repos/{owner}/{repo}"


def _get_config_path() -> Path:
    """获取 app.config 文件路径"""
    return get_app_data_dir() / "app.config"


def _get_backup_path() -> Path:
    """获取绑定前备份路径"""
    return get_app_data_dir() / "app.config.pre_bind"


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
        self._config_path: Path = _get_config_path()
        self._backup_path: Path = _get_backup_path()
        self._debounce_timer: Optional[QTimer] = None
        self._upload_lock = threading.Lock()
        self._watch_stop: Optional[threading.Event] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._suppress_upload: bool = False  # 下载恢复时抑制上传反馈

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
        """绑定前：备份本地 app.config → app.config.pre_bind"""
        cfg = self._config_path
        bak = self._backup_path
        if not cfg.exists():
            logger.warning("[ConfigSync] 本地配置文件不存在，跳过备份")
            return False
        try:
            shutil.copy2(str(cfg), str(bak))
            logger.info(f"[ConfigSync] 已备份本地配置 → {bak.name}")
            return True
        except Exception as e:
            logger.error(f"[ConfigSync] 备份失败: {e}")
            return False

    def restore_local(self) -> bool:
        """解绑后：恢复备份 app.config.pre_bind → app.config"""
        bak = self._backup_path
        cfg = self._config_path
        if not bak.exists():
            logger.info("[ConfigSync] 无备份文件，跳过恢复")
            return False
        try:
            shutil.copy2(str(bak), str(cfg))
            logger.info(f"[ConfigSync] 已恢复本地配置 ← {bak.name}")
            # 删除备份
            try:
                os.remove(str(bak))
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"[ConfigSync] 恢复失败: {e}")
            return False

    def enable(self, token: str, owner: str):
        """绑定成功后启动同步：启动文件监听 → 检查远端 → 自动恢复或上传"""
        logger.info(f"[ConfigSync] enable token={token[:8]}... owner={owner}")
        logger.info(f"[ConfigSync] config_path={self._config_path}")
        logger.info(f"[ConfigSync] cfg_dir={self._config_path.parent}")
        logger.info(f"[ConfigSync] cfg_name={self._config_path.name}")

        if self._state != "disabled":
            logger.warning("[ConfigSync] 已在运行中，忽略重复 enable")
            return

        # 确保任何残留的旧监听线程已停止
        self._stop_watching()

        self._token = token
        self._owner = owner
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._do_upload)

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
        """强制立即上传当前配置到远端（公开方法，供手动重试）"""
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
        logger.info(f"[ConfigSync] watch 线程已启动，监控目录={cfg_dir}，文件名={cfg_name}")
        try:
            for changes in watch(cfg_dir):
                if self._watch_stop and self._watch_stop.is_set():
                    break
                # 只关心 app.config 的变更
                for change_type, changed_path in changes:
                    if Path(changed_path).name == cfg_name:
                        logger.info(f"[ConfigSync] 检测到文件变更: {changed_path}")
                        self._on_config_changed()
                        break
        except Exception as e:
            logger.error(f"[ConfigSync] 文件监听异常: {e}")

    def _on_config_changed(self):
        """文件变更回调（watchfiles 线程）→ 通过信号桥接到主线程"""
        self._configChanged.emit()

    def _on_config_changed_main(self):
        """主线程：重置防抖计时器"""
        if self._state == "disabled":
            return
        # 下载恢复中，抑制上传（避免下载 → 触发变更 → 重新上传的循环）
        if self._suppress_upload:
            return
        if self._debounce_timer:
            logger.info(f"[ConfigSync] 防抖计时器启动，{DEBOUNCE_MS}ms 后上传")
            self._debounce_timer.start(DEBOUNCE_MS)

    # ── 远端同步 ──────────────────────────────────────────

    def _initial_sync(self):
        """绑定后首次同步：检查远端 → 下载/上传"""
        self._set_state("syncing")
        if self._check_remote():
            # 远端有配置 → 下载恢复
            logger.info("[ConfigSync] 远端有配置，开始恢复…")
            ok = self._do_download()
            if ok:
                self._set_state("idle")
                self.syncDone.emit(True, "配置已从云端恢复")
            else:
                self._set_state("error")
                self.syncDone.emit(False, "配置恢复失败")
        else:
            # 远端无配置 → 上传当前
            logger.info("[ConfigSync] 远端无配置，开始上传…")
            ok = self._do_upload()
            if ok:
                self._set_state("idle")
                self.syncDone.emit(True, "配置已备份到云端")
            else:
                self._set_state("error")
                self.syncDone.emit(False, "配置备份失败")

    def _check_remote(self) -> bool:
        """检查远端是否存在配置文件（200 + 返回 dict=文件存在，list=仅目录存在）"""
        if not self._token or not self._owner:
            return False
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=REMOTE_PATH)
            resp = requests.get(url, params={"access_token": self._token}, timeout=10)
            if resp.status_code != 200:
                return False
            body = resp.json()
            return isinstance(body, dict) and "content" in body
        except Exception:
            return False

    def _do_upload(self) -> bool:
        """上传 app.config 到远端。先 GET 查 sha，有则 PUT+sha，无则 POST 新建。"""
        if not self._token or not self._owner:
            logger.warning("[ConfigSync] 无法上传：缺少 token/owner")
            return False
        if not self._config_path.exists():
            logger.warning("[ConfigSync] 无法上传：配置文件不存在")
            return False

        acquired = self._upload_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("[ConfigSync] 上传已在进行中，跳过")
            return False

        try:
            data = self._config_path.read_bytes()
            content_b64 = base64.b64encode(data).decode("utf-8")
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=REMOTE_PATH)
            base_payload = {
                "access_token": self._token,
                "content": content_b64,
                "message": f"DriFox config sync ({time.strftime('%Y-%m-%d %H:%M:%S')})",
                "branch": "master",
            }

            # 先查询远端是否存在（获取 sha）
            existing_sha = self._get_remote_sha()

            if existing_sha:
                # 文件已存在 → PUT + sha 更新
                payload = {**base_payload, "sha": existing_sha}
                resp = requests.put(url, data=payload, timeout=30)
                method = "PUT"
            else:
                # 文件不存在 → POST 新建
                resp = requests.post(url, data=base_payload, timeout=30)
                method = "POST"

            if resp.status_code in (200, 201):
                logger.info(f"[ConfigSync] 配置上传成功 ({method})")
                self._set_state("idle")
                return True

            err_msg = self._parse_error(resp)
            if resp.status_code in (401, 403):
                logger.error(f"[ConfigSync] 上传失败 (token 无效) [{resp.status_code}]: {err_msg}")
                self._set_state("error")
                return False

            # 如果是 PUT 失败但原因是 sha 不匹配（并发冲突），重新 GET sha 后重试一次
            if method == "PUT" and "sha" in err_msg.lower():
                retry_sha = self._get_remote_sha()
                if retry_sha:
                    payload["sha"] = retry_sha
                    resp2 = requests.put(url, data=payload, timeout=30)
                    if resp2.status_code in (200, 201):
                        logger.info("[ConfigSync] 配置上传成功 (PUT retry)")
                        self._set_state("idle")
                        return True

            logger.error(f"[ConfigSync] 上传最终失败 [{resp.status_code}]: {err_msg}")
            self._set_state("error")
            return False
        except requests.exceptions.Timeout:
            logger.error("[ConfigSync] 上传超时")
            self._set_state("error")
            return False
        except requests.exceptions.ConnectionError:
            logger.error("[ConfigSync] 网络连接失败")
            self._set_state("error")
            return False
        except Exception as e:
            logger.error(f"[ConfigSync] 上传异常: {e}")
            self._set_state("error")
            return False
        finally:
            self._upload_lock.release()

    def _get_remote_sha(self) -> Optional[str]:
        """查询远端文件的 sha，不存在返回 None"""
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=REMOTE_PATH)
            resp = requests.get(url, params={"access_token": self._token}, timeout=10)
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict) and "sha" in body:
                    return body["sha"]
            return None
        except Exception:
            return None

    def _do_download(self) -> bool:
        """从远端下载配置，覆盖本地 app.config，然后重新加载 Settings"""
        if not self._token or not self._owner:
            return False
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=REMOTE_PATH)
            resp = requests.get(url, params={"access_token": self._token}, timeout=15)
            if resp.status_code != 200:
                err = self._parse_error(resp)
                logger.error(f"[ConfigSync] 下载失败 [{resp.status_code}]: {err}")
                return False

            body = resp.json()
            if not isinstance(body, dict):
                logger.error("[ConfigSync] 远端路径是目录而非文件")
                return False
            content_b64 = body.get("content", "")
            if not content_b64:
                logger.error("[ConfigSync] 下载的内容为空")
                return False

            decoded = base64.b64decode(content_b64)

            # 下载写文件前抑制上传反馈，避免下载→触发变更→上传的循环
            self._suppress_upload = True
            self._config_path.write_bytes(decoded)
            self._suppress_upload = False
            logger.info("[ConfigSync] 配置已从云端下载并覆盖本地")

            # 重新加载 Settings
            try:
                Settings.get_instance().load()
                logger.info("[ConfigSync] Settings 已重新加载")
            except Exception as e:
                logger.warning(f"[ConfigSync] Settings 重载失败: {e}")

            return True
        except Exception as e:
            logger.error(f"[ConfigSync] 下载异常: {e}")
            return False

    # ── 辅助 ──────────────────────────────────────────────

    @staticmethod
    def _parse_error(resp) -> str:
        try:
            body = resp.json()
            return body.get("message", resp.text[:200])
        except Exception:
            return resp.text[:200]
