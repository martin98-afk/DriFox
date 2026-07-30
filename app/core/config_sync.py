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
    _reloadSettings = pyqtSignal()  # 请求在主线程重新加载 Settings（后台线程 → 主线程）

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
        self._pending_sync_message: Optional[str] = None  # 延迟 syncDone 消息（Settings 在主线程重载完成后发射）
        self._initial_sync_completed: bool = False  # 初始同步是否已完成（禁止初始同步前上传）
        self._suppress_retry_scheduled: bool = False  # 抑制窗口结束后的重试已调度

        # 从磁盘恢复持久化的 SHA 缓存（避免每次重启全量下载）
        self._load_sha_cache()

        # 跨线程桥：watchfiles 线程 → 主线程
        self._configChanged.connect(self._on_config_changed_main)
        # 跨线程桥：后台线程请求重新加载 Settings → 主线程执行
        self._reloadSettings.connect(self._reload_settings_on_main_thread)

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
                # [PERF] 原子恢复：先复制到临时目录（不在插件 watch 路径内），
                # 再 rename 到目标路径（单次目录操作），避免 shutil.copytree
                # 逐个文件触发 ChatBackend 插件重载（如 49+ 次文件事件）。
                _tmp_restore = get_app_data_dir() / "user-custom.restore"
                if _tmp_restore.exists():
                    shutil.rmtree(str(_tmp_restore))
                shutil.copytree(str(uc_bak), str(_tmp_restore))
                if uc.exists():
                    shutil.rmtree(str(uc))
                shutil.move(str(_tmp_restore), str(uc))
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
        self._initial_sync_completed = False
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
        self._initial_sync_completed = False
        try:
            if self._sha_cache_path.exists():
                self._sha_cache_path.unlink()
        except Exception:
            pass
        self._set_state("disabled")

    def upload(self) -> bool:
        """强制立即上传当前配置、user-custom 和分享记录到远端（公开方法，供手动重试）

        初始同步未完成时（_initial_sync_completed=False），说明首次检查远端因网络异常中断，
        此时不盲目上传本地配置，而是重新触发 _initial_sync 走完整检查流程：
          - 远端有配置 → 下载覆盖本地
          - 远端无配置 → 上传本地到远端
        """
        if not self._initial_sync_completed:
            logger.info("[ConfigSync] 初始同步未完成，手动重试触发完整同步流程")
            t = threading.Thread(target=self._initial_sync, daemon=True)
            t.start()
            return True

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

                # [PERF] 批处理：watchfiles 每次迭代可能包含多个文件变更（如解压 user-custom 插件时
                # 37+ 个文件同时出现）。只在整批处理完成后发射一次 _configChanged 信号，避免
                # 大量跨线程信号拥塞主线程事件队列导致 UI 卡顿。
                needs_notify = False
                custom_logged = False

                for change_type, changed_path in changes:
                    changed_p = Path(changed_path)
                    # app.config 变更
                    if changed_p.name == cfg_name:
                        if not self._config_dirty:
                            logger.info(f"[ConfigSync] 检测到配置变更: {changed_path}")
                        self._config_dirty = True
                        needs_notify = True
                        continue
                    # share_records.json 变更
                    if changed_p.name == records_name:
                        if not self._records_dirty:
                            logger.info(f"[ConfigSync] 检测到分享记录变更: {changed_path}")
                        self._records_dirty = True
                        needs_notify = True
                        continue
                    # user-custom 目录下文件变更（排除目录本身 mtime 变化）
                    if (
                        self._user_custom_path.exists()
                        and changed_p != self._user_custom_path
                        and changed_p.is_relative_to(self._user_custom_path)
                    ):
                        self._custom_dirty = True
                        needs_notify = True
                        if not custom_logged:
                            logger.info("[ConfigSync] 检测到用户插件变更（批量）")
                            custom_logged = True
                        continue

                if needs_notify:
                    self._on_config_changed()
        except Exception as e:
            logger.error(f"[ConfigSync] 文件监听异常: {e}")

    def _on_config_changed(self):
        """文件变更回调（watchfiles 线程）→ 通过信号桥接到主线程"""
        self._configChanged.emit()

    def _on_config_changed_main(self):
        """主线程：重置防抖计时器。timeout 后由后台线程执行上传，不阻塞 UI。"""
        if self._state == "disabled":
            return
        if time.time() < self._suppress_until:
            # 抑制窗口内不启动防抖，但调度一次窗口结束后的检查
            # 防止窗口内仅有的一次变更被永久丢失
            if not self._suppress_retry_scheduled:
                self._suppress_retry_scheduled = True
                remaining = self._suppress_until - time.time() + 0.5
                if 0 < remaining < 3600:
                    logger.debug(f"[ConfigSync] 抑制窗口内，{remaining:.0f}s 后检查待上传变更")
                    QTimer.singleShot(int(remaining * 1000), self._flush_suppressed_changes)
            return
        self._start_debounce_timer()

    def _start_debounce_timer(self):
        """启动防抖计时器（仅在有脏标记时）"""
        if not self._debounce_timer:
            return
        if not (self._config_dirty or self._custom_dirty or self._records_dirty):
            return
        logger.info(f"[ConfigSync] 防抖计时器启动，{DEBOUNCE_MS}ms 后上传")
        self._debounce_timer.start(DEBOUNCE_MS)

    def _flush_suppressed_changes(self):
        """抑制窗口结束：处理被延迟的变更（由 QTimer.singleShot 调用）"""
        self._suppress_retry_scheduled = False
        if self._state == "disabled":
            return
        # 抑制窗口可能已被新的下载刷新，再次检查
        if time.time() < self._suppress_until:
            return
        if not self._initial_sync_completed:
            return
        self._start_debounce_timer()

    def _on_debounce_timeout(self):
        """防抖到期 → 检查抑制窗口 → 启动后台上传线程"""
        if time.time() < self._suppress_until:
            logger.debug("[ConfigSync] 下载后抑制窗口内，取消本次上传")
            return
        if not self._initial_sync_completed:
            logger.debug("[ConfigSync] 初始同步未完成，跳过防抖上传")
            return
        logger.info("[ConfigSync] 防抖到期，启动后台上传线程")
        t = threading.Thread(target=self._do_upload, daemon=True)
        t.start()
        self._upload_thread = t

    # ── 主线程 Settings 重载 ──────────────────────────────

    def _reload_settings_on_main_thread(self):
        """在主线程重新加载 Settings（响应 _reloadSettings 信号）

        由 _reloadSettings（后台线程发射）桥接到主线程执行，
        避免大量 valueChanged 信号从非主线程发射导致 UI 卡顿。

        ⚠️ 重载前保存 token 相关值，防止远端旧配置覆盖本地刷新后的 token。

        本方法不使用 cfg.load() —— QConfig.load() 被 @exceptionHandler() 装饰，
        任何一个 ConfigItem 反序列化异常都会导致整个 load 静默失败、所有值保持原样。
        改为全量手动从文件同步，确保 100% 生效。
        """
        try:
            cfg = Settings.get_instance()

            # 保存当前内存中的 token 值（可能是自动刷新后的新 token）
            _saved = {
                "bound": cfg.gitee_bound.value,
                "token": cfg.gitee_user_token.value,
                "refresh_token": cfg.gitee_user_refresh_token.value,
                "expires_at": cfg.gitee_token_expires_at.value,
                "owner": cfg.gitee_user_owner.value,
                "repo": cfg.gitee_user_repo.value,
            }

            # ── 全量手动从文件同步（绕过 @exceptionHandler 的静默吞异常问题） ──
            try:
                import json as _json
                from qfluentwidgets.common.config import ConfigItem as _CI

                with open(cfg.file, encoding="utf-8") as _f:
                    _file_data = _json.load(_f)

                # 遍历所有 section，Gitee 段稍后由 token 保护逻辑处理
                for _section_name, _section_data in _file_data.items():
                    if _section_name == "Gitee":
                        continue
                    for _key, _value in _section_data.items():
                        _matched = None
                        for _name in dir(cfg.__class__):
                            _item = getattr(cfg.__class__, _name)
                            if isinstance(_item, _CI) and _item.group == _section_name and _item.name == _key:
                                _matched = _name
                                break
                        if _matched:
                            getattr(cfg, _matched).value = _value

                logger.debug("[ConfigSync] 全量配置已从文件同步到内存")
            except Exception as _e:
                logger.warning(f"[ConfigSync] 从文件同步配置项失败: {_e}")

            # 恢复 token 相关值：远端 app.config 中的 token 可能是过期的，
            # 本地刚刷新好的 token 不应被覆盖
            if _saved["bound"]:
                cfg.gitee_bound.value = _saved["bound"]
                cfg.gitee_user_token.value = _saved["token"]
                cfg.gitee_user_refresh_token.value = _saved["refresh_token"]
                cfg.gitee_token_expires_at.value = _saved["expires_at"]
                cfg.gitee_user_owner.value = _saved["owner"]
                cfg.gitee_user_repo.value = _saved["repo"]

            logger.info("[ConfigSync] Settings 已重新加载（主线程，全量手动同步，token 已保护）")
        except Exception as e:
            logger.warning(f"[ConfigSync] Settings 重载失败: {e}")

        # 发射被延迟的 syncDone（_do_download 中设置的 _pending_sync_message）
        if self._pending_sync_message:
            msg = self._pending_sync_message
            self._pending_sync_message = None
            self._set_state("idle")
            self.syncDone.emit(True, msg)

    # ── Token 同步 ──────────────────────────────────────────

    def _sync_token(self) -> bool:
        """
        从中央 token 管理器同步有效 access_token（自动检测过期并刷新）。

        ConfigSyncService 在 enable() 时收到的 token 是一次性快照，
        token 过期后不会自动更新。本方法每次 API 调用前调用，确保
        self._token 持有最新有效值。

        Returns:
            True: token 有效
            False: token 无效（未绑定/刷新失败）

        注意：刷新失败时不会回退到旧 token，而是清空 self._token 并返回 False，
        防止下游流程使用过期 token 导致误判远端状态。
        """
        try:
            from app.gateway.auth.gitee import GiteeOAuthBackend, refresh_access_token

            backend = GiteeOAuthBackend()
            if not backend.is_bound():
                self._token = ""
                return False

            cfg = Settings.get_instance()
            access_token = cfg.gitee_user_token.value
            refresh_token = cfg.gitee_user_refresh_token.value
            expires_at = cfg.gitee_token_expires_at.value

            # ── 判断是否需要刷新 ──
            needs_refresh = False
            if refresh_token:
                if not expires_at or time.time() >= expires_at - 60:
                    needs_refresh = True
            elif not access_token:
                self._token = ""
                return False
            # 无 refresh_token 但有 access_token → 直接使用

            if not needs_refresh:
                self._token = access_token
                return True

            # ── 自行刷新（不调 _ensure_valid_token → 不触发 _persist_tokens 写 app.config） ──
            client_id = cfg.gitee_oauth_client_id.value
            client_secret = cfg.gitee_oauth_client_secret.value
            new_tokens, err = refresh_access_token(refresh_token, client_id, client_secret)
            if new_tokens is None:
                self._token = ""
                logger.warning(f"[ConfigSync] token 刷新失败: {err}")
                return False

            # 仅更新内存，不写 app.config（避免触发文件 watcher）
            cfg.gitee_user_token.value = new_tokens["access_token"]
            if "refresh_token" in new_tokens and new_tokens["refresh_token"]:
                cfg.gitee_user_refresh_token.value = new_tokens["refresh_token"]
            if "expires_in" in new_tokens:
                cfg.gitee_token_expires_at.value = time.time() + new_tokens["expires_in"]

            self._token = new_tokens["access_token"]
            logger.info("[ConfigSync] access_token 已续期（内存，未写盘）")
            return True
        except Exception as e:
            logger.warning(f"[ConfigSync] token 同步异常: {e}")
            self._token = ""
            return False

    # ── 远端同步 ──────────────────────────────────────────

    def _initial_sync(self):
        """绑定后首次同步：检查远端 → 下载/上传（含 app.config + user-custom 插件）"""
        # 先同步有效 token
        self._sync_token()

        self._set_state("syncing")
        self._pending_sync_message = None  # 确保无残留
        remote_status: bool | None = None
        try:
            remote_status = self._check_remote()
            if remote_status is True:
                # 远端有配置 → 下载恢复
                logger.info("[ConfigSync] 远端有配置，开始恢复…")
                ok = self._do_download()
                if ok:
                    self._initial_sync_completed = True
                    if self._pending_sync_message:
                        # Settings 重载已延迟到主线程执行，syncDone 会在
                        # _reload_settings_on_main_thread 中自动发射
                        logger.debug("[ConfigSync] 等待 Settings 在主线程重载后发射 syncDone")
                    else:
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
                ok = self._do_upload(initial_sync=True)
                if ok:
                    self._initial_sync_completed = True
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
            # 上传路径（远端无配置/初次上传）：短抑制 5s
            # 下载路径（远端有配置）：_do_download 已设 30s 长抑制，保留不动
            if remote_status is False:
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
        # 先同步有效 token（自动刷新）
        self._sync_token()

        if not self._token or not self._owner:
            return None
        try:
            url = GITEE_FILE_API.format(owner=self._owner, repo=SETTINGS_REPO_NAME, path=remote_path)
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                resp = client.get(url, params={"access_token": self._token})
            if resp.status_code == 404:
                return False  # 文件确定不存在
            if resp.status_code != 200:
                logger.warning(f"[ConfigSync] 检查远端文件失败: HTTP {resp.status_code}")
                return None  # 其他错误 → 不确定状态，保守处理不上传
            body = resp.json()
            return isinstance(body, dict) and "content" in body
        except Exception:
            return None

    def _do_upload(self, initial_sync: bool = False) -> bool:
        """上传有变更的项到远端（基于 _config_dirty / _custom_dirty 脏标记）。

        防抖触发时只上传被标记的项；
        手动 upload() / 首次同步强制全量（两项都标记）。

        Args:
            initial_sync: 是否由 _initial_sync 调用。为 True 时跳过
                         _initial_sync_completed 检查（初始同步本身就是建立首次同步状态）。
        """
        # 先同步有效 token（自动刷新），防止使用过期 token 导致 401
        if not self._sync_token():
            logger.warning("[ConfigSync] 无法上传：token 无效")
            return False

        if not self._token or not self._owner:
            logger.warning("[ConfigSync] 无法上传：缺少 token/owner")
            return False

        # 初始同步未完成时禁止上传，防止未确认远端状态就覆盖云端配置
        if not initial_sync and not self._initial_sync_completed:
            logger.warning("[ConfigSync] 初始同步未完成，禁止上传（防止覆盖远端现有配置）")
            return False

        # 抑制窗口检查已在 _on_config_changed_main / _on_debounce_timeout 中执行，
        # 不在 _do_upload 中重复检查——否则会误拦截 _initial_sync 的显式上传。
        if not self._config_dirty and not self._custom_dirty and not self._records_dirty:
            logger.debug("[ConfigSync] 无可上传的变更，跳过")
            return True

        # ★ 先获取锁，再清脏标记：防止锁被占用时脏标记永久丢失
        acquired = self._upload_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("[ConfigSync] 上传已在进行中，跳过")
            return False

        upload_config = self._config_dirty
        upload_custom = self._custom_dirty
        upload_records = self._records_dirty
        self._config_dirty = False
        self._custom_dirty = False
        self._records_dirty = False

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
        # 先同步有效 token（自动刷新）
        if not self._sync_token():
            logger.warning("[ConfigSync] 无法下载：token 无效")
            return False

        if not self._token or not self._owner:
            return False

        results: list[bool] = []
        try:
            # ★ 进入下载立即取消防抖计时，防止之前 watch 事件的残留防抖与下载流程并发
            if self._debounce_timer:
                self._debounce_timer.stop()
            # ★ 设 30s 抑制窗口，覆盖下载写盘 + Settings.load 级联效应的全过程
            self._suppress_until = time.time() + 30.0

            # ── 1. app.config：必须成功，否则整个下载失败 ──
            # app.config 是核心配置，下载失败意味着本地配置未同步，后续不允许上传覆盖远端。
            # user-custom / share_records 依赖 app.config 的上下文，单独成功无意义。
            try:
                remote_config_sha = self._get_remote_file_sha(REMOTE_PATH)
                if remote_config_sha and remote_config_sha == self._config_remote_sha:
                    logger.debug("[ConfigSync] 远端 app.config 未变化，跳过下载")
                    results.append(True)
                else:
                    if self._download_file(REMOTE_PATH, self._config_path, label="app.config"):
                        logger.info("[ConfigSync] app.config 已下载，安排在主线重载 Settings")
                        # [PERF] 不在后台线程直接调用 Settings.load()，避免大量
                        # valueChanged 信号从非主线程发射到主线程队列引发 UI 卡顿。
                        # 改用信号桥接到主线程执行，load 完成后自动发射 syncDone。
                        self._pending_sync_message = "配置与用户插件已从云端恢复"
                        self._reloadSettings.emit()
                        results.append(True)
                    else:
                        logger.error("[ConfigSync] app.config 下载失败，中止后续下载")
                        return False
            except Exception as e:
                logger.error(f"[ConfigSync] app.config 下载异常: {e}")
                return False

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
            except (RuntimeError, zipfile.BadZipFile):
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
