# -*- coding: utf-8 -*-
"""模型列表定时刷新服务（进程级单例）。

背景：服务商列表此前无任何自动刷新机制——「模型列表」只在用户手动点
「获取模型列表」时更新，插件换了模型池、厂商下线了模型，用户配置里仍是旧的，
表现为「模型选择器里全是过期模型」。

两个开关（per-provider，用户在编辑卡勾选，随配置落盘）：

- **自动刷新模型**：到期拉取 → 默认模型仍在返回列表里 → **静默合并**新列表落盘
- **健康检查**：到期拉取 → 只写状态（不碰模型列表），供列表行状态点显示

设计要点：

- **tick 粒度 5 分钟**而非单个 24h 长定时器：长定时器在系统睡眠 / 挂起后会
  漂移（醒来时已过期很久才触发，或干脆不触发）。短 tick 内查「距上次刷新
  ≥ 24h」既能抗漂移，又能让用户在睡眠唤醒后较快看到刷新。
- **线程红线**：`QTimer` 与 `cfg.set/save` **只在主线程**。后台线程只做
  HTTP + 读写内存 + `emit` 信号；落盘在主线程槽 `_on_refresh_result` 内做。
  qfluentwidgets / QConfig 均非线程安全，越界即随机崩溃（本模块最大翻车源）。
- **落盘不走 `provider_save_plan` / `apply_provider_save`**：那套是表单语义
  （用户显式编辑、冲突弹窗、config_id 重算）。这里是服务端静默更新，按
  config_id 原地更新即可，不重算 hash（避免无谓的条目搬迁）。
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal

# 刷新周期（秒）与 tick 粒度（毫秒）
REFRESH_INTERVAL_S = 24 * 3600
TICK_MS = 5 * 60 * 1000

# 配置键（与服务商配置同键名，随 saved_providers 落盘）
KEY_AUTO_REFRESH = "自动刷新模型"
KEY_HEALTH_CHECK = "健康检查"
KEY_MODELS = "模型列表"
KEY_LAST_REFRESH = "上次模型刷新"
KEY_REFRESH_STATUS = "模型刷新状态"

# 状态取值（供 UI 状态点映射）
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_UNREACHABLE = "unreachable"


def is_default_model_safe(current_model: str, models: List[str]) -> bool:
    """默认模型是否仍在拉取到的列表里（静默合并的安全闸）。

    模型不在新列表里 → 说明厂商下线了该模型，此时合并会让用户配置指向一个
    不存在的模型（选择器空、发送直接 400）。故只在「仍在」时才合并。

    共用点：编辑卡 `_apply_fetched` 与服务侧 `_should_merge` 都调本函数，
    避免两处判定逻辑各写一份后漂移。
    """
    model = str(current_model or "").strip()
    if not model:
        return False
    wanted = model.lower()
    return any(str(m or "").strip().lower() == wanted for m in (models or []))


def parse_refresh_ts(value: Any) -> float:
    """把「上次模型刷新」的值解析成 epoch 秒；无法解析返回 0.0（视为从未刷新）。"""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return 0.0


def now_ts_str() -> str:
    """当前时间字符串（与 format_relative_time 约定一致的格式）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ModelRefreshService(QObject):
    """模型列表定时刷新（进程级单例，风格对齐 UsageService）。"""

    # (config_id, ok, status, models) — 刷新完成（主线程发射；models 供落盘合并）
    refresh_finished = pyqtSignal(str, bool, str, list)

    _instance: Optional["ModelRefreshService"] = None

    @classmethod
    def get_instance(cls) -> "ModelRefreshService":
        """获取全局唯一实例（首次调用须在主线程，QTimer 归属主线程）"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, parent: QObject = None):
        super().__init__(parent)
        # 参与刷新的 config_id 集合（双开关任一为 True）
        self._active: Set[str] = set()
        # config_id → config 快照（dict copy，不持窗口引用）
        self._configs: Dict[str, dict] = {}
        # 上次触发时间（epoch 秒）——进程内兜底，配置里的时间戳为准
        self._last_trigger: Dict[str, float] = {}
        # 并发去重：正在抓取的 config_id
        self._in_flight: Set[str] = set()
        self._timer: Optional[QTimer] = None
        self._stopped = False
        # ⚠ 跨线程信号：后台线程 emit → Qt 队列投递 → 主线程槽执行落盘。
        # 默认 AutoConnection 在同线程时是直连（会变成后台线程里落盘），故显式声明
        # QueuedConnection 保证落盘恒在主线程（QConfig/qfluentwidgets 非线程安全）。
        self.refresh_finished.connect(self._on_refresh_result, Qt.QueuedConnection)

    # ══════════════════════════════════════════════════════════
    # 注册 / 生命周期（主线程）
    # ══════════════════════════════════════════════════════════

    def sync_from_config(self, saved_providers: Optional[Dict[str, dict]] = None) -> None:
        """全量重建 active 集（双开关任一为 True 的条目）。

        由主程序在启动与「服务商保存」后调用（保存回调里先 invalidate 再
        调本方法，让新配置的快照生效）。
        """
        if saved_providers is None:
            try:
                from app.utils.config import Settings

                saved_providers = Settings.get_instance().llm_saved_providers.value or {}
            except Exception as e:
                logger.warning(f"[ModelRefresh] 读取服务商配置失败，跳过本次 sync: {e}")
                return

        active: Set[str] = set()
        configs: Dict[str, dict] = {}
        for config_id, info in (saved_providers or {}).items():
            if not isinstance(info, dict):
                continue
            auto = bool(info.get(KEY_AUTO_REFRESH))
            health = bool(info.get(KEY_HEALTH_CHECK))
            if not (auto or health):
                continue
            active.add(config_id)
            configs[config_id] = dict(info)

        # 丢掉已不在活跃集的条目（开关被关掉 / 条目被删）
        for gone in set(self._configs) - set(configs):
            self._configs.pop(gone, None)
        self._active = active
        self._configs = configs

        if active:
            self._ensure_timer()
        logger.debug(f"[ModelRefresh] sync 完成，{len(active)} 个服务商参与定时刷新")

    def invalidate(self, config_id: str) -> None:
        """条目被编辑/删除：清快照，避免用旧配置继续抓。"""
        if not config_id:
            return
        self._configs.pop(config_id, None)
        self._active.discard(config_id)
        self._last_trigger.pop(config_id, None)

    def unregister(self, config_id: str) -> None:
        """同 invalidate（语义化别名，供窗口侧调用）"""
        self.invalidate(config_id)

    def stop(self) -> None:
        """退出前停表（aboutToQuit 调用；线程为 daemon，不阻塞退出）"""
        self._stopped = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except RuntimeError:
                pass

    # ══════════════════════════════════════════════════════════
    # 到期判定
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def is_due(info: Dict[str, Any], now: Optional[float] = None, interval_s: int = REFRESH_INTERVAL_S) -> bool:
        """该配置是否到了刷新时间（距「上次模型刷新」≥ interval）。

        时间戳缺失 / 无法解析 → 视为到期（首次启用即抓一次）。
        """
        now = time.time() if now is None else now
        last = parse_refresh_ts((info or {}).get(KEY_LAST_REFRESH))
        if last <= 0:
            return True
        return (now - last) >= interval_s

    # ══════════════════════════════════════════════════════════
    # tick 链（主线程）
    # ══════════════════════════════════════════════════════════

    def _ensure_timer(self) -> None:
        if self._stopped:
            return
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._on_tick)
        if not self._timer.isActive():
            self._timer.start(TICK_MS)

    def _on_tick(self) -> None:
        """tick：把到期条目派给后台线程，然后续下一次 tick。"""
        if self._stopped:
            return
        now = time.time()
        for config_id in list(self._active):
            info = self._configs.get(config_id)
            if info is None:
                self._active.discard(config_id)
                continue
            if not self.is_due(info, now):
                continue
            if config_id in self._in_flight:
                continue  # 并发去重：上一轮还没回来
            self._in_flight.add(config_id)
            threading.Thread(
                target=self._fetch_one,
                args=(config_id, dict(info)),
                daemon=True,
                name=f"model-refresh-{config_id}",
            ).start()
        self._ensure_timer()

    # ══════════════════════════════════════════════════════════
    # 后台线程（⚠ 只做 HTTP + 内存 + emit，禁碰 cfg / QTimer / QWidget）
    # ══════════════════════════════════════════════════════════

    def _fetch_one(self, config_id: str, info: Dict[str, Any]) -> None:
        """后台抓取单条：hook 优先 → REST 兜底；结果经信号回主线程。"""
        models: List[str] = []
        status = STATUS_UNREACHABLE
        try:
            provider_name = str(info.get("provider_name", "") or "")
            hook = self._resolve_models_hook(provider_name)
            if hook is not None:
                try:
                    result = hook(dict(info)) or []
                    models = [str(m) for m in result if m]
                    status = STATUS_OK if models else STATUS_EMPTY
                except Exception as e:
                    logger.info(f"[ModelRefresh] models_hook 失败 config_id={config_id}: {e}")
                    status = STATUS_UNREACHABLE
            else:
                from app.widgets.cards.settings.provider_edit_card import fetch_provider_models

                api_url = str(info.get("API_URL", "") or "")
                api_key = str(info.get("API_KEY", "") or "")
                if not api_url:
                    status = STATUS_UNREACHABLE
                else:
                    filtered, _removed, status = fetch_provider_models(
                        api_url,
                        api_key,
                        provider_name,
                        str(info.get("认证方式", "") or "bearer"),
                    )
                    models = list(filtered)
        except Exception as e:
            logger.warning(f"[ModelRefresh] 刷新异常 config_id={config_id}: {e}")
            status = STATUS_UNREACHABLE
        finally:
            self._in_flight.discard(config_id)

        ok = status in (STATUS_OK, STATUS_EMPTY)
        self.refresh_finished.emit(config_id, ok, status, list(models))

    @staticmethod
    def _resolve_models_hook(provider_name: str):
        """取插件的 capabilities["models_hook"]（无则 None）"""
        if not provider_name:
            return None
        try:
            from app.plugins.registries.provider_registry import ProviderRegistry

            p = ProviderRegistry.get_instance().get(provider_name)
            if p is None:
                return None
            hook = p.capabilities.get("models_hook")
            return hook if callable(hook) else None
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════
    # 主线程槽：落盘
    # ══════════════════════════════════════════════════════════

    def _should_merge(self, info: Dict[str, Any], models: List[str]) -> bool:
        """是否允许把新列表静默合并进配置。

        仅「自动刷新模型」开启 + 默认模型仍在列表里 才合并；健康检查单独开启时
        只写状态，绝不碰模型列表（用户可能手动维护过列表）。
        """
        if not info.get(KEY_AUTO_REFRESH):
            return False
        return is_default_model_safe(str(info.get("模型名称", "") or ""), models)

    def _on_refresh_result(self, config_id: str, ok: bool, status: str, models: List[str]) -> None:
        """主线程槽：写状态 / 静默合并 / 落盘 / 通知窗口重载。

        本方法必须在主线程（`refresh_finished` 用 QueuedConnection 投递，
        见 `__init__`）。
        """
        info = self._configs.get(config_id)
        if info is None:
            return
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            saved = cfg.llm_saved_providers.value
            if not isinstance(saved, dict) or config_id not in saved:
                return
            current = saved[config_id]
            if not isinstance(current, dict):
                return

            updated = deepcopy(saved)
            entry = updated[config_id]
            entry[KEY_LAST_REFRESH] = now_ts_str()
            entry[KEY_REFRESH_STATUS] = status if not ok else (STATUS_OK if models else STATUS_EMPTY)
            if ok and models and self._should_merge(current, models):
                entry[KEY_MODELS] = list(models)
                logger.info(f"[ModelRefresh] 静默合并模型列表 config_id={config_id}（{len(models)} 个）")

            cfg.set(cfg.llm_saved_providers, updated, save=True)
            # 同步进程内快照，下一轮到期判定读到新时间戳
            self._configs[config_id] = dict(entry)
            self._last_trigger[config_id] = time.time()

            self._notify_windows()
        except Exception as e:
            logger.warning(f"[ModelRefresh] 落盘失败 config_id={config_id}: {e}")

    def _notify_windows(self) -> None:
        """通知各窗口重载模型配置（刷新列表行副标题 / 选择器）。

        先例：`global_card_controller.py:383` 的服务商保存回调亦走此链。
        （服务商列表自身经 cfg valueChanged → `_refresh_items` 自动刷新，
        本处补的是「模型选择器 / 当前模型配置」这类不监听该键的消费者。）
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is None:
                return
            count = tm.windowCount()
            for idx in range(count):
                win = tm.windowAt(idx)
                loader = getattr(win, "_load_model_configs", None)
                if callable(loader):
                    try:
                        loader()
                    except Exception:
                        pass
        except Exception:
            pass


def get_model_refresh_service() -> ModelRefreshService:
    """便捷入口（延迟建单例）"""
    return ModelRefreshService.get_instance()
