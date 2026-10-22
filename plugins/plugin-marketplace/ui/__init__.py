# -*- coding: utf-8 -*-
"""plugin-marketplace UI 组件入口"""

import threading

from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal


def register_ui(registry):
    """注册 plugin-marketplace 的 UI 组件"""
    from .cards import MarketplaceCard

    # 注册浮动卡片（自动注册对应命令 /plugin-marketplace）
    # container="full"：完整覆盖对话区（与系统配置卡片一致，走覆盖层）
    registry.register_floating_card(
        plugin_name="plugin-marketplace",
        card_id="plugin-marketplace",
        widget_class=MarketplaceCard,
        container="full",
        title="插件市场",
        default_visible=False,
    )
    _ensure_auto_update_timer()
    logger.info("[plugin-marketplace] UI components registered")


# ── 后台更新检查（应用启动后低频执行；侧栏计数依赖本检查） ──
# 语义拆分：检查始终执行（侧栏「插件市场 (N)」计数不依赖打开过市场）；
# 是否自动安装由插件设置卡 auto_update_enabled 开关决定。

_timer = None
_bridge = None
_auto_busy = False


class _AutoUpdateBridge(QObject):
    """worker 线程 → 主线程完成通知（跨线程只能走 signal）"""

    finished = pyqtSignal(int, int, str, int, int)  # (updated, failed, summary, pending, checked)


def _ensure_auto_update_timer():
    """模块级单例定时器：启动后 90s 首检，此后每 30 分钟一次（热重载幂等）"""
    global _timer, _bridge
    if _timer is not None:
        return
    _bridge = _AutoUpdateBridge()
    _bridge.finished.connect(_on_auto_update_done)
    _timer = QTimer()
    _timer.setInterval(30 * 60 * 1000)
    _timer.timeout.connect(_tick_auto_update)
    _timer.start()
    QTimer.singleShot(90 * 1000, _tick_auto_update)
    logger.debug("[plugin-marketplace] update-check timer armed (first check in 90s)")


def _tick_auto_update():
    """定时 tick：后台检查待更新数（供侧栏计数），开关开启时顺带自动安装"""
    global _auto_busy
    if _auto_busy:
        return
    from .marketplace_manager import get_auto_update_enabled

    execute = get_auto_update_enabled()
    _auto_busy = True
    logger.debug(f"[plugin-marketplace] update check start (execute={execute})")

    def _work():
        updated, failed, pending, checked, summary = 0, 0, 0, 0, "无待更新插件"
        try:
            from .marketplace_manager import run_auto_update_check

            result = run_auto_update_check(execute=execute)
            updated = len(result.get("updated", []))
            failed = len(result.get("failed", []))
            pending = result.get("pending", 0)
            checked = result.get("checked", 0)
            if pending:
                summary = (
                    f"发现 {pending} 个待更新插件"
                    + (f"，已自动更新 {updated} 个" if execute else "，可在插件市场中更新")
                    + (f"，失败 {failed} 个" if failed else "")
                )
            else:
                summary = "无待更新插件"
        except Exception as e:
            summary = f"更新检查失败: {e}"
            logger.warning(f"[plugin-marketplace] update check failed: {e}")
        finally:
            _bridge.finished.emit(updated, failed, summary, pending, checked)

    threading.Thread(target=_work, daemon=True, name="marketplace-update-check").start()


def _on_auto_update_done(updated: int, failed: int, summary: str, pending: int, checked: int):
    """主线程槽：释放忙标志 + 同步侧栏计数 + 卡片可见则刷新并弹 InfoBar

    checked=0 说明拉取失败（全空数据不可信）：不覆盖侧栏计数，保持上次结果。
    """
    global _auto_busy
    _auto_busy = False
    try:
        from .cards import _sync_sidebar_title_count, notify_auto_update_done

        if checked > 0:
            _sync_sidebar_title_count(pending)
        notify_auto_update_done(updated, failed, summary)
    except Exception as e:
        logger.debug(f"[plugin-marketplace] update check notify failed: {e}")
