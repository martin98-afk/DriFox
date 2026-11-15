# -*- coding: utf-8 -*-
"""UI 事件总线 — 插件订阅主程序 UI 事件（主题切换/Tab 切换/卡片显隐等）

设计：
- 单例（与 UIPluginRegistry 生命周期对齐，reset_instance 供测试）
- 弱约定：事件名为字符串常量，payload 为 kwargs dict
- 回调异常隔离：单个订阅者抛异常不影响其他订阅者（记日志）
- 插件级退订：UIPluginRegistry.unload_plugin 时调用 unsubscribe_plugin，
  插件无需在 unload_ui 里手动退订（防止悬挂回调引用旧模块）
"""

import inspect
import weakref
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# ── 事件常量（payload 字段在注释中约定）──
EV_THEME_CHANGED = "theme_changed"  # payload: theme_id, theme_name, is_dark
EV_TAB_SWITCHED = "tab_switched"  # payload: tab_index, window_id
EV_CARD_VISIBILITY_CHANGED = "card_visibility_changed"  # payload: card_id, window_id, visible
EV_WELCOME_TAB_REFRESHED = "welcome_tab_refreshed"
# 欢迎卡片插件 tab 数据已更新（异步 fetcher 完成 / 数据源刷新等），
# 通知主程序对指定 mode_key 触发重渲染（不重建 QWebEngineView）。
# payload: mode_key, plugin_name, window_id（可选；缺省 = 全窗口）
EV_PROJECT_CHANGED = "project_changed"  # payload: project, workdir, window_id
# 对话报错（backend conversation_error 同步广播；能力池 ui.events 订阅）。
# payload: window_id, session_id, provider, model, message, error_type, timestamp。
# 全局监听插件（如自动换模型）按 window_id 定向操作目标窗口。
EV_CONVERSATION_ERROR = "conversation_error"
# 工作台页签徽标条目已变化（插件 register_slot_entry 到 "workbench:tab" region 后
# 由注册表发布，WorkbenchPanel 订阅并重挂徽标）。payload: 无（消费方全量重读 region）
EV_WORKBENCH_BADGES_CHANGED = "workbench.badges_changed"
# 工作台页签集合已变化（register_workbench_tab 后发布，WorkbenchPanel 订阅并
# 重跑 sync_plugin_pages reconcile——热加载新插件页时无需重启即可见）。payload: 无
EV_WORKBENCH_TABS_CHANGED = "workbench.tabs_changed"
# 宿主判定「该生成标题」后发布，插件据此调 model.complete。
# payload: window_id, session_id, messages, fallback
EV_TITLE_REQUEST = "title.request"
# 项目 / 工作目录已变更（MainWidget._sync_working_directory 收尾处发布，按
# (project, workdir) 去重）。UI 插件实现可选协议 on_project_changed 即接收：
# 只对可见的工作台页与浮动卡派发，不可见的由切页 refresh_data() / 再次显示
# show_card() 自行补刷（详见 app/core/project_changed.py 与
# docs/superpowers/specs/2026-09-12-ui-plugin-project-changed-design.md）

_Callback = Callable[[Dict[str, Any]], None]


def _resolve_callable(cb_ref):
    """统一取当前可调用：WeakMethod 已死（宿主对象已销毁）返回 None。

    输入为普通可调用（强持有形态）时原样返回（weakref 调用不可弱引用
    对象会抛 TypeError，按原样返回兜底）。
    """
    try:
        return cb_ref()
    except TypeError:
        return cb_ref


def _is_dead(cb_ref) -> bool:
    """弱引用是否已死；非弱引用恒为存活。"""
    try:
        return cb_ref() is None
    except TypeError:
        return False


def _same_callable(pair_cb, callback) -> bool:
    """订阅表里的当前可调用与待退订 callback 是否同一目标。

    绑定方法每次属性访问产生新对象（h.on_theme != h.on_theme），身份比较
    须按 (__self__, __func__) 对齐；普通函数/lambda 的 __self__ 为 None，
    仍走 `is` 短路，语义不变。
    """
    cur = _resolve_callable(pair_cb)
    if cur is callback:
        return True
    cb_self = getattr(callback, "__self__", None)
    if cb_self is None:
        return False
    return (
        getattr(cur, "__self__", None) is cb_self
        and getattr(cur, "__func__", None) is getattr(callback, "__func__", None)
    )


class UIEventBus:
    """UI 事件总线（单例）"""

    _instance: Optional["UIEventBus"] = None

    def __init__(self):
        # {event: [(plugin_name or None, callback), ...]}
        self._subs: Dict[str, List[tuple]] = {}

    @classmethod
    def get_instance(cls) -> "UIEventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def subscribe(self, event: str, callback: _Callback, plugin_name: Optional[str] = None) -> None:
        """订阅事件；plugin_name 非空时随插件 unload 自动退订

        [LEAK-FIX P1-6] 绑定方法改 weakref.WeakMethod 弱持有：单例窗口等宿主
        以绑定方法订阅且无插件名，旧实现 _subs 强引用 callback，宿主销毁后
        旧窗口整树仍被总线常驻（真实泄漏，详见 ISSUE_LIST P1-6）。弱引用使
        回调随宿主对象销毁自然失效，publish 沿途惰性清理死引用；普通函数 /
        lambda / 闭包不可安全弱引用（会提前失效），维持强持有，生命周期仍由
        unsubscribe_plugin / unsubscribe 手动通道管理。
        """
        if inspect.ismethod(callback):
            callback = weakref.WeakMethod(callback)
        self._subs.setdefault(event, []).append((plugin_name, callback))

    def unsubscribe(self, event: str, callback: _Callback) -> None:
        """按 callback 退订（主程序对象生命周期管理：widget 销毁时释放悬挂回调）

        ``plugin_name=None`` 的订阅者不走 ``unsubscribe_plugin``，需调用方手动退订
        防止 callback 引用已销毁对象。
        """
        pairs = self._subs.get(event)
        if not pairs:
            return
        # 弱持有形态下 pair[1] 是 WeakMethod：按当前可调用/绑定目标身份比对
        kept = [pair for pair in pairs if not _same_callable(pair[1], callback)]
        if kept:
            self._subs[event] = kept
        else:
            del self._subs[event]

    def unsubscribe_plugin(self, plugin_name: str) -> None:
        """退订某插件的全部事件（unload_plugin 调用）"""
        for event in list(self._subs.keys()):
            self._subs[event] = [pair for pair in self._subs[event] if pair[0] != plugin_name]
            if not self._subs[event]:
                del self._subs[event]

    def publish(self, event: str, **payload: Any) -> None:
        """发布事件；订阅回调异常隔离（吞掉记 warning）

        [PERF] 慢订阅者打点（>=50ms 记 WARNING）：publish 是**同步直调**，
        任一订阅者慢都会阻塞整条调用链（主题切换实测出现过 ev_publish 段
        秒级）。日志给出事件名 + plugin_name + 回调限定名 + 耗时，可直接
        定位是哪个订阅者吃掉了时间，不用逐个插件猜。
        """
        from time import perf_counter

        pairs = self._subs.get(event, [])
        live: List[tuple] = []
        deliverable: List[tuple] = []
        for pair in pairs:
            cb = _resolve_callable(pair[1])
            if cb is None:
                continue  # 弱引用宿主已销毁：不投递，顺带惰性清理
            live.append(pair)
            deliverable.append((pair[0], cb))
        if len(live) != len(pairs):
            # 死引用清出订阅表（纯列表维护，不改变投递/异常语义）
            if live:
                self._subs[event] = live
            else:
                del self._subs[event]

        for plugin_name, callback in deliverable:
            _t0 = perf_counter()
            try:
                callback(payload)
            except Exception as e:
                logger.warning(f"[UIEventBus] {event} 订阅回调异常 ({plugin_name}): {e}")
            finally:
                _ms = (perf_counter() - _t0) * 1000
                if _ms >= 50:
                    owner = getattr(callback, "__qualname__", None) or getattr(callback, "__name__", "?")
                    logger.warning(f"[UIEventBus] 慢订阅者 {event}: {plugin_name or 'main'} {owner} = {_ms:.0f}ms")

    def subscriptions(self) -> Dict[str, int]:
        """当前各事件订阅数（调试/测试；已死弱引用不计入）"""
        return {
            event: sum(1 for pair in pairs if not _is_dead(pair[1]))
            for event, pairs in self._subs.items()
        }
