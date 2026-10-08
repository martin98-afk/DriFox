# -*- coding: utf-8 -*-
"""内存/生命周期治理 — 自 main_widget.py 尾部迁出（2026-09-29 拆分步骤 1）

职责：
- ``_is_sip_deleted``：PyQt C++ 对象销毁防御守卫
- GC 钩子（T9）：防抖触发全局渲染缓存清理 + Python 堆回收
- B4 温和层：WebEngine 并发渲染页上限（per-window + 跨窗口全局闸门）
- B4 强回收层：内存超阈值 kill 离屏 renderer 进程（双判据 + 活跃窗口保守策略）

所有权说明：可变全局 ``_global_rendered_pages`` / ``_gc_hook_pending`` 归本模块。
调用方必须以 ``memory_governor._global_rendered_pages`` 模块属性方式读写；
禁止 from-import 这两个名字（int 值拷贝会与真实计数脱钩，闸门/防抖失效）。
"""

import gc

import sip


def _is_sip_deleted(obj) -> bool:
    """判断 PyQt 对象是否已被 C++ 侧销毁（防御 wrapped C/C++ object has been deleted）。

    destroyed 信号在 C++ 对象真正销毁时触发，此时 Python 侧的 self 仍存在但
    C++ 包装已失效，访问 self 的任何 Qt 属性都会抛 RuntimeError。
    用于 destroyed 回调 / 清理路径的入口守卫，静默返回 False 兜底。
    """
    try:
        return sip.isdeleted(obj)
    except Exception:
        return False


# GC 钩子（T9）：模块级防抖标志，150ms 内多次触发只执行一次。
_gc_hook_pending = False


# ── B4 温和层：WebEngine 并发页上限（T12 蓝图） ──
# 每渲染卡 ~64MB renderer 进程，长对话必须锁峰值：
# 温和层：并发页 ≤ _MAX_RENDERED_CARDS（18 = 可视 12 批 + 上下 6 批缓冲）。
# 强回收层（kill 离屏 renderer）依赖 message_card 的 renderer_pid 记录，暂缓。
# 2026-09-09 内存治理：18 → 12（可视 ~8 批 + 上下 4 批缓冲）。真机日志显示
# 大会话（60+ 批次）内存主体是**并发 WebEngine 页数**而非单卡 HTML 体积
# （单卡 DOM 数百 KB vs 每页数十 MB 常驻），砍 6 页 ≈ 直接省下数百 MB，
# 且历史渲染已异步化，回滚重建不再阻塞主线程。
_MAX_RENDERED_CARDS = 12
_global_rendered_pages: int = 0  # 跨窗口观测计数（日志用，非硬约束）

# ── B4 温和层：跨窗口全局渲染页闸门 ──
# [PERF] _MAX_RENDERED_CARDS 原本是 **per-window** 常量，多窗口场景下
# N 窗口 = N×18 张已渲染卡片常驻。每张卡片的 DOM/JS heap 都要挤在
# --renderer-process-limit 封顶的那几个 renderer 进程里，内存随窗口数线性增长
# （4 窗口 ≈ 72 页）。改为全局配额：新窗口只能分到「全局剩余配额」，
# 但每窗口至少保留 _MIN_RENDERED_CARDS_PER_WINDOW 张 —— 宁可全局超限，
# 也不能让某个窗口白屏（可用性优先于内存）。
_MAX_GLOBAL_RENDERED_PAGES = 32  # 跨窗口并发渲染页硬闸门
_MIN_RENDERED_CARDS_PER_WINDOW = 6  # 每窗口保底页数（闸门的下限保护）
# [MEM] 保底页数的收缩下限。原实现保底是常量 6 —— N 个窗口必然 N×6 页
# （8 窗口 = 48 页，实测每页 27-39MB → 1.3GB+），_MAX_GLOBAL_RENDERED_PAGES=32
# 被完全架空。现在保底随存活窗口数收缩，但降到本值即停，避免窗口被饿死到白屏。
_MIN_RENDERED_CARDS_PER_WINDOW_FLOOR = 3

# ── B4 强回收层：内存超阈值时 kill 离屏 renderer 进程（T13 蓝图 / T30 双判据） ──
# 双判据：主进程 RSS 超总阈值，且 WebEngine 子进程 RSS 超子阈值才触发强回收——
# 避免仅主进程内存高（如 Python 堆）时误杀 renderer。
# [MEM] 强回收的主判据已改为 WebEngine 子进程 RSS（见 _over_memory_threshold）。
# 实测（tools/diag_webengine_mem_probe.py，dpr=2.25）：并发 8 个 QWebEngineView
# 让子进程涨 216MB、主进程只涨 4MB —— 并发对话的内存主体全在 renderer 子进程，
# 拿主进程 RSS 当门槛等于永远够不着，强回收永不触发 → 子进程一路涨到 4GB。
_WEB_MEM_THRESHOLD_MB = 600  # 非活跃窗口：WebEngine 子进程 RSS 触发阈值
_WEB_MEM_THRESHOLD_MB_ACTIVE = 1000  # 活跃窗口：阈值更高，避免滚动回看时重建抖动
# 兜底：子进程采样不可用时（无 psutil / 尚未创建 view）退回主进程 RSS 判据
_MEM_THRESHOLD_TOTAL_MB = 900
_LRU_RENDERER_KEEP = 8  # 强回收后保留最近活跃 renderer 数
_KILL_COOLDOWN_S = 60  # kill 冷却（防抖动）
_KILL_BATCH_MAX = 12  # 每轮最多 kill
_OFFSCREEN_BATCHES_FOR_KILL = 8  # 距可视区 ≥8 批才可 kill（严格离屏护栏）

# ── 活跃窗口强回收（修复「活跃窗口永不回收」导致的内存单调增长）──
# 活跃窗口用户正在交互，回收需要更保守，但绝不能像旧实现那样直接跳过
# （跳过 = 单窗口场景永不回收 = 内存溢出）。
# - 阈值更高：避免刚过阈值就频繁 kill 造成重建抖动
# - 保留更多：距可视区近的 renderer 留着，回滚时无需重建
# - 离屏更远才 kill：只回收用户短期内不会滚回的批次
# - 队列上限做最终兜底：护栏再严也保证队列与 renderer 进程数有界
_MEM_THRESHOLD_TOTAL_MB_ACTIVE = 1400  # 活跃窗口阈值（高于非活跃的 900MB）
_LRU_RENDERER_KEEP_ACTIVE = 14  # 活跃窗口保留更多最近 renderer（对比非活跃 8）
_OFFSCREEN_BATCHES_FOR_KILL_ACTIVE = 12  # 活跃窗口要求离屏更远（对比非活跃 8）
_UNLOADED_PIDS_MAX = 32  # _unloaded_pids 队列硬上限：超限强制 kill 最老的（背压兜底）


def _run_gc_hook():
    """GC 钩子执行体：清理全局渲染缓存 + 回收进程堆。

    由 _schedule_gc_hook 防抖合并后调用（150ms singleShot），
    全 try/except 吞异常，不影响主流程。
    """
    global _gc_hook_pending
    _gc_hook_pending = False
    try:
        from app.widgets.card_render_core import clear_global_render_cache

        clear_global_render_cache()
    except Exception:
        pass
    try:
        _compact_process_heap_after_cleanup()
    except Exception:
        pass


def _cleanup_global_lru_caches():
    """清理全局 LRU 缓存，释放旧会话渲染/估算占用的内存。

    在新建会话、切换会话时调用，避免缓存的 HTML 渲染结果和 token 估算值累积。
    """
    try:
        from app.widgets.card_render_core import clear_global_render_cache

        clear_global_render_cache()
    except Exception:
        pass
    try:
        from app.core.infra.token_estimator import estimate_tokens

        estimate_tokens.cache_clear()
    except Exception:
        pass
    try:
        from app.widgets.message_card import _render_tool_block_content

        _render_tool_block_content.cache_clear()
    except Exception:
        pass
    try:
        from app.widgets.render_helpers import invalidate_render_caches

        invalidate_render_caches()
    except Exception:
        pass
    try:
        from app.utils.utils import invalidate_icon_cache

        invalidate_icon_cache()
    except Exception:
        pass
    try:
        from app.utils.provider_icons import invalidate_provider_icon_cache

        invalidate_provider_icon_cache()
    except Exception:
        pass


def _compact_process_heap_after_cleanup():
    """卡片清理后触发 gc，回收 Python 对象图（T11：移除失效的 HeapCompact/malloc_trim）。

    T10 实测：Python 3.14 下 ctypes.WinDLL("kernel32").HeapCompact 100% 抛
    access violation（被 except 吞掉），主线程高频路径（新建/切换/恢复会话、
    切换项目、undo）上制造无效异常开销；Linux malloc_trim 收益同样有限。
    故移除两者，保留 gc.collect() —— pymalloc arena 的归还由 CPython
    内存管理自行处理，gc 收集足够。
    """
    gc.collect()
