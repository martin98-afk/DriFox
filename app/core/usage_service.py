# -*- coding: utf-8 -*-
"""
用量聚合服务（进程级单例）— 套餐用量 + 余额的全局缓存 / 轮询 / 信号广播。

痛点②：每个 tab 独立 60s 轮询套餐用量、每次切换独立查余额 →
N tab × 同一 provider 会发起 N 路重复请求（N 路线程 + N 路 HTTP）。
本服务把请求按 (provider_name, config_id) 聚合为**全局 1 路**：
- 缓存命中直接广播（所有窗口共享同一结果）
- 未命中由单例后台线程抓取，写缓存后广播
- active key 注册后由单例 QTimer 统一轮询（TTL 过期重拉）

关键约定：
- 缓存不缓存 None（失败不污染缓存，下次请求自动重试）
- in_flight 去重：同一 key 并发请求只发 1 路，防并发风暴
- get_instance() 首次调用必须在主线程（QTimer 归属主线程）
- 服务只存 config 快照（dict copy），不持窗口引用；窗口关闭用
  unregister(config_id) 清理轮询注册，不影响其它窗口

与 coding_plan_fetcher 的关系：fetch（同步）/ fetch_async（每请求一线程）为
旧的多窗口独立调用入口；本服务在其上叠加缓存 + 聚合 + 单例轮询。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from loguru import logger
from PySide6.QtCore import QObject, QTimer, Signal

from app.plugins.registries.provider_registry import ProviderRegistry


class UsageService(QObject):
    """用量聚合服务（进程级单例，风格对齐 PluginManager.get_instance）"""

    # (provider_name, config_id, result) — result 为套餐用量 dict 或 None
    coding_plan_ready = Signal(str, str, object)
    # (provider_name, config_id, result) — result 为余额 dict / {"hide":...} / None
    balance_ready = Signal(str, str, object)

    _instance: Optional["UsageService"] = None

    # 轮询周期与缓存 TTL（秒）
    POLL_INTERVAL_MS = 60000
    PLAN_TTL_S = 60.0
    BALANCE_TTL_S = 60.0

    @classmethod
    def get_instance(cls) -> "UsageService":
        """获取全局唯一实例（首次调用应在主线程，QTimer 归属主线程）"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, parent: QObject = None):
        super().__init__(parent)
        # (provider_name, config_id) → (fetch_ts, result)
        self._plan_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        self._balance_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        # config_id → config 快照（dict copy，不持窗口引用）
        self._configs: Dict[str, dict] = {}
        # 正在轮询的 plan key（有数据后注册，供 _on_poll_tick 重发）
        self._active_plan_keys: set = set()
        # 正在轮询的 balance key（与 plan 同源 timer 统一驱动，60s 抓一次）
        self._active_balance_keys: set = set()
        # 并发去重：正在抓取的 key
        self._in_flight: set = set()
        self._poll_timer: Optional[QTimer] = None

    # ========== 套餐用量 ==========

    def has_coding_plan_fetcher(self, provider_name: str) -> bool:
        """该服务商（或其 family）是否注册了套餐用量获取器"""
        return self._resolve_fetcher(provider_name) is not None

    @staticmethod
    def _resolve_fetcher(provider_name: str) -> Optional[Callable]:
        """套餐用量 fetcher：优先精确匹配，回退按 family 前缀匹配（注册表替身。

        服务商插件通过 ProviderDef.coding_plan_fetcher 注册；
        无插件声明时返回 None。
        """
        registry = ProviderRegistry.get_instance()
        return registry._resolve_coding_plan_fetcher(provider_name)

    def request_coding_plan(self, provider_name: str, config_id: str, config: dict, force: bool = False) -> None:
        """请求套餐用量：缓存命中→广播缓存；in_flight→跳过；否则后台抓取后广播。

        无 fetcher 时同步 emit None（通知窗口隐藏圆环）。
        结果写缓存 + 注册 active key（供单例轮询）。

        force=True（轮询 tick 使用）：跳过缓存命中，每轮强制重拉。否则缓存
        TTL(60s) 与 tick 周期(60s) 相同，抓取耗时导致 tick 时缓存年龄恒 < TTL，
        永远命中旧值 → 实际每 2 轮（120s）才真刷新。
        """
        key = (provider_name, config_id)
        if config_id:
            self._configs[config_id] = dict(config or {})

        fetcher = self._resolve_fetcher(provider_name)
        if fetcher is None:
            self.coding_plan_ready.emit(provider_name, config_id, None)
            return

        hit = self._plan_cache.get(key)
        if not force and hit and time.monotonic() - hit[0] < self.PLAN_TTL_S:
            # 缓存命中：key 有抓取资格，恢复/保持 active 注册，确保 singleShot
            # 轮询 timer 存活（unregister 后新窗口命中缓存也要继续轮询）
            self._active_plan_keys.add(key)
            self.coding_plan_ready.emit(provider_name, config_id, hit[1])
            self._ensure_poll_timer()
            return

        if key in self._in_flight:
            # 并发去重：同一 key 已有一路在抓取，保持轮询 timer 存活等下一轮
            self._ensure_poll_timer()
            return  # 抓取完成会自行广播

        self._in_flight.add(key)

        def _run():
            try:
                result = fetcher(dict(config or {}))
            except Exception as e:
                logger.warning(f"[UsageService] coding plan fetch error: {e}")
                result = None
            if result is not None:
                self._plan_cache[key] = (time.monotonic(), result)
            # 无论成败都注册 active key：失败不写缓存（不污染），但必须进入
            # 轮询集合让 60s tick 持续重试——否则首次抓取失败后 key 永远不在
            # 集合里，轮询 timer 因集合为空而死亡，圆环停止更新直到新建标签页。
            self._active_plan_keys.add(key)
            self._in_flight.discard(key)
            # 跨线程信号：接收者（窗口槽）在主线程 → Qt 自动 queued 投递
            self.coding_plan_ready.emit(provider_name, config_id, result)

        threading.Thread(target=_run, daemon=True, name="usage-plan-fetch").start()
        self._ensure_poll_timer()

    # ========== 余额 ==========

    def request_balance(self, provider_name: str, config_id: str, config: dict, force: bool = False) -> None:
        """请求余额：缓存命中→广播缓存；in_flight→跳过；否则后台抓取后广播。

        非白名单服务商同步 emit None（窗口隐藏余额组件）。
        抓到有效结果（含 hide）即注册 active key，由单例 _poll_timer 统一
        60s 轮询，与套餐用量共用同一 timer，UI 自动刷新。

        force=True（轮询 tick 使用）：跳过缓存命中，每轮强制重拉（同
        request_coding_plan 的 TTL/tick 同长竞态说明）。
        """
        key = (provider_name, config_id)
        if config_id:
            self._configs[config_id] = dict(config or {})

        if not ProviderRegistry.get_instance().has_balance_support(provider_name):
            self.balance_ready.emit(provider_name, config_id, None)
            return

        hit = self._balance_cache.get(key)
        if not force and hit and time.monotonic() - hit[0] < self.BALANCE_TTL_S:
            # 缓存命中：注册 active key 保证轮询 timer 存活（与 plan 同步）
            self._active_balance_keys.add(key)
            self.balance_ready.emit(provider_name, config_id, hit[1])
            self._ensure_poll_timer()
            return

        if key in self._in_flight:
            # 并发去重：保持 timer 存活等抓取完成
            self._ensure_poll_timer()
            return

        self._in_flight.add(key)
        self._ensure_poll_timer()

        def _run():
            try:
                result = self._fetch_balance_sync(provider_name, dict(config or {}))
            except Exception as e:
                logger.warning(f"[UsageService] balance fetch error: {e}")
                result = None
            if result is not None:
                # 后台线程只读写内存态（GIL 原子）；QTimer 保活统一由主线程
                # request_balance 路径 / tick 兜底负责，禁止跨线程操作 QTimer。
                self._balance_cache[key] = (time.monotonic(), result)
            # 同 plan：失败也注册 active key，由 60s 轮询持续重试（防首次
            # 失败后 key 脱离轮询集合、timer 死亡、余额显示永久停止更新）
            self._active_balance_keys.add(key)
            self._in_flight.discard(key)
            # 跨线程信号：接收者（窗口槽）在主线程 → Qt 自动 queued 投递
            self.balance_ready.emit(provider_name, config_id, result)

        threading.Thread(target=_run, daemon=True, name="usage-balance-fetch").start()

    def _fetch_balance_sync(self, provider_name: str, config: dict) -> Optional[dict]:
        """同步查询余额（worker 线程执行，fetcher 由 providers 插件提供）。

        返回：
        - {"balance": float, "currency": str, "provider": str}  成功
        - {"hide": True, "provider": str, "tooltip": str|省略}  失败/无余额
        - None                                                 非白名单/无 API key
        """
        result = ProviderRegistry.get_instance().balance_fetch(provider_name, config)
        if result is None:
            return None
        if isinstance(result, dict) and "balance" in result:
            result = dict(result)
            result.setdefault("provider", provider_name)
        elif isinstance(result, dict):
            result = dict(result)
            result.setdefault("provider", provider_name)
        return result

    # ========== 生命周期 ==========

    def invalidate(self, config_id: str) -> None:
        """配置变更时失效该 config 的用量/余额缓存（下次请求强制重拉）。

        由 main_widget 在服务商配置保存后调用（invalidate 幂等，可重复调用）。
        只删缓存条目与 config 快照，不影响 in_flight（进行中的请求结果
        下次轮询自然覆盖）。
        """
        if not config_id:
            return
        for cache in (self._plan_cache, self._balance_cache):
            for key in [k for k in cache if k[1] == config_id]:
                cache.pop(key, None)
        for active in (self._active_plan_keys, self._active_balance_keys):
            for key in [k for k in active if k[1] == config_id]:
                active.discard(key)
        self._configs.pop(config_id, None)

    def unregister(self, config_id: str) -> None:
        """窗口关闭时清理该 config 的轮询注册与 config 快照。

        缓存条目保留（供其它窗口复用）；仅停掉该 config 的 active key 轮询
        与快照引用，避免已关闭窗口的配置持续触发后台请求。
        """
        if not config_id:
            return
        for active in (self._active_plan_keys, self._active_balance_keys):
            for key in [k for k in active if k[1] == config_id]:
                active.discard(key)
        self._configs.pop(config_id, None)

    # ========== 轮询 ==========

    def _ensure_poll_timer(self) -> None:
        """确保轮询定时器存在（QTimer 单例 singleShot，须主线程创建）"""
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.setSingleShot(True)
            self._poll_timer.timeout.connect(self._on_poll_tick)
        if not self._poll_timer.isActive():
            self._poll_timer.start(self.POLL_INTERVAL_MS)

    def _on_poll_tick(self) -> None:
        """轮询 tick：遍历 active keys 重发请求（缓存 TTL 过期则后台重拉）。

        同源 timer 驱动 plan + balance 两类 active keys，N tab × 同 provider
        只发 1 路请求。balance 与 plan 各自的 request_* 内部会在命中 / 抓取
        完成后调用 _ensure_poll_timer() 重启 timer。
        """
        keys = list(self._active_plan_keys)
        for key in keys:
            provider_name, config_id = key
            config = self._configs.get(config_id)
            if config is None:
                # 窗口已 unregister，清理残留 key
                self._active_plan_keys.discard(key)
                continue
            # force=True：tick 语义是「到期重拉」，跳过缓存命中，每轮必重拉
            self.request_coding_plan(provider_name, config_id, config, force=True)

        balance_keys = list(self._active_balance_keys)
        for key in balance_keys:
            provider_name, config_id = key
            config = self._configs.get(config_id)
            if config is None:
                # 窗口已 unregister，清理残留 key
                self._active_balance_keys.discard(key)
                continue
            # force=True：同 plan，每轮必重拉
            self.request_balance(provider_name, config_id, config, force=True)

        # 兜底：tick 内所有路径（命中/in_flight/无 fetcher）都会 return 不重启
        # timer，这里保证只要还有 active key，轮询就继续下一轮
        if self._active_plan_keys or self._active_balance_keys:
            self._ensure_poll_timer()
