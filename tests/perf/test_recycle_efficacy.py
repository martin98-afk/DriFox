# -*- coding: utf-8 -*-
"""虚拟滚动回收效能验证（判定「回收生效 / 回收未生效 / 无法判定」，供 #5 修复决策）。

背景：#4 实测每会话 UI 层增量 146.6 MB，批增量 26-40 MB/10 张无平台期无回落，
推断「虚拟滚动回收未生效」。#4 用的是裸 MessageCard（不含容器），属推断。
本文件在**真实容器**（OpenAIChatToolWindow + `_batch_cards` 批次管理 +
`_recycle_lru_batches` 淘汰链）里复现，把推断变成实测。

核心指标：**存活 MessageCard 对象数**。选它而非 RSS 的理由（leader #5 变更指令）：
Qt 原生内存与 Python arena 都可能「已释放但 RSS 不回落」，RSS 单独不能作为
「回收未生效」的证据，只作辅助；存活对象数才是判定依据。

═══ 防自污染措施（leader #5 变更指令 1，本文件采用 WeakSet 方案）═══
#1 root-cause-analyst 曾报「cleanup() 后 RSS 回收 0.0 MB、widget 树存活 1120
子控件」，后自查推翻：那是**探针自己的 `cards` 列表持有强引用**造成的假象
（`gc.get_referrers(card)` 返回的 list 就是探针自身）。
本文件的防污染手段：**全程用 `weakref.WeakSet` 注册卡片并统计 `len()`，
测试代码不持有任何卡片强引用**（建卡后立刻 `del` 局部变量，卡片只由 Qt
布局持有）。WeakSet 不阻止 GC，故「存活数」= 真被别处引用着的对象数。
另在每次采样前 `gc.collect()`，避免年轻代待回收对象虚高计数。

═══ 降级层级（本文件落在第 3 层，如实标注）═══
第 1 层（未达成）`_load_message_batch` / `_switch_to_session` 全链路 ——
    offscreen 下懒渲染队列（`_process_next_lazy_batch`）泵事件时进程崩溃
    （0xC0000409 家族，与 QWebEngineView offscreen 崩溃同源）。
第 2 层（未达成）`_append_user_message` / `_append_assistant_message` 建卡 ——
    内部仍走 CodeWebViewer 路径，卡片 showEvent 崩溃（card_viewers.py:1717）。
第 3 层（本文件）**直接构造 MessageCard 挂进真实 `chat_layout`，手工标记
    `_lazy_rendered` 并计入配额，再手工调 `_recycle_lru_batches` /
    `_unload_batch` 触发真实淘汰链**。
    容器、布局、批次结构、配额判定、`_unload_batch` 全部是真实代码路径；
    绕过的是「滚动事件驱动」「懒渲染定时器」「_append_* 的 WebEngine 分支」
    这三个 offscreen 下必崩的触发源。**未改任何 `app/` 源码来迁就测试。**
第 3 层能回答的问题：给定真实容器与真实淘汰链，超配额时淘汰是否真的把
卡片卸下来、对象是否真的被释放。

⚠️ 第 3 层的已知盲区（影响结论强度，务必连带阅读）：
    a. 卡片未真正 `ensure_rendered`（offscreen 下会崩），故 `_lazy_rendered`
       是手工置位。`_unload_batch` 内部有 `_batch_fully_rendered` 守卫，
       本文件建的空卡满足该守卫（无未渲染卡），故淘汰链能真实跑到。
    b. **不调用 `deleteLater()`**：Qt 在 offscreen 下析构 QWebEngineView 有
       竞态崩溃（与 tests/ui 记录的「崩族①/②」同源）。因此本文件测的是
       「淘汰链是否把卡片从布局/批次中摘除并清空引用」，**不能**证明
       对象最终被 C++ 析构。存活数不回落一定说明有泄漏，回落也不等于
       原生资源已归还 OS。
    c. 渲染通道沿用 #4：`DRIFOX_QT_RENDERER=1` 纯 Qt 渲染器。
    d. 刻意不调 `init_shared_web_profile`：用例内不泵事件，但 pytest teardown
       会泵，届时窗口内延迟渲染定时器可能走到 `CodeWebViewer` →
       `get_shared_web_profile()` 抛 RuntimeError（被 Qt 事件循环捕获，不
       影响本文件采集到的数字，但会在输出末尾留一条 teardown 噪声）。
       若初始化它反而会真的建 WebEngine → offscreen 崩溃（更糟），故不初始化。

配额：`_MAX_RENDERED_CARDS=12`（main_widget 实测 `_effective_max_rendered_cards()`=12）。

场景
  1. 真实容器累积曲线：灌入 FEED_CARDS 张（默认 120，配额的 10 倍），
     每 SAMPLE_EVERY 张采样 RSS 与存活卡片数 → 曲线是线性上升还是走平。
  2. 稳态存活数 vs 配额：稳态后实际存活卡片数 vs 配额 12。
  3. `_recycle_lru_batches` 前后对比：手动触发淘汰，测 RSS 与存活对象数的
     变化，并统计 `_stream_text_acc` 残留（leader 提供的已确认真实洞）。
  4. 跨会话复用度：两批各 60 张，看总存活数是 ≈2×配额（复用）还是全量常驻。

运行：
    cd <仓库根> && uv run pytest tests/perf/test_recycle_efficacy.py -v -s
    （tests/perf 被 addopts 默认排除，必须显式指定路径；-s 才能看到 [RECY] 打印）

基线（修复前）
    采集环境：Windows / Python 3.14 / dev 分支 caa2137d / offscreen + DRIFOX_QT_RENDERER=1
    采集命令：uv run pytest tests/perf/test_recycle_efficacy.py -v -s

    | 指标 | 修复前基线 | 修复后 | 说明 |
    |---|---|---|---|
    | **灌入 120 张后存活卡片数** | **120 张（配额 12）** | — | 核心数字：超出配额 108 张，零回收 |
    | 累积曲线形态 | 严格线性（20→120 全程 1:1） | — | 场景 1，无任何走平迹象 |
    | **淘汰前后存活卡片数** | **120 → 120（Δ=0）** | — | 场景 3，_recycle_lru_batches 一次都没卸下 |
    | 淘汰前后 RSS | 241.9 → 241.9 MB（Δ=+0.0） | — | 场景 3，仅辅助，不作判定依据 |
    | 淘汰前后 _batch_cards 空槽 | 0 → 0 | — | 场景 3，_unload_batch 从未执行到 |
    | `_stream_text_acc` 残留 | 0 B | — | 本文件未走流式路径，未复现该残留 |
    | 两批各 60 张后总存活数 | 120 张（理想 ≈24） | — | 场景 4，全量常驻，无跨会话复用 |

    **结论：回收未生效**（三选一取第一种）。依据不是 RSS 而是存活对象数：
    灌入 120 张、配额 12，稳态存活仍是 120 张；手动触发 `_recycle_lru_batches()`
    后存活数、记账渲染数、`_batch_cards` 空槽三项**全部纹丝不动**，说明淘汰链
    压根没跑到 `_unload_batch`。修复方向应盯「为什么 `_recycle_lru_batches` 早退」，
    而不是「`_unload_batch` 卸得不干净」。

    ⚠️ 结论强度提示（务必连带阅读）：本文件落在降级第 3 层（见上），卡片未真正
    ensure_rendered、`_lazy_rendered` 由测试手工置位。因此本结论证明的是「给定
    真实容器 + 真实配额判定 + 真实淘汰函数，超配额 10 倍时淘汰不发生」，
    **不能**证明真机滚动场景下的行为完全一致（真机多了滚动事件与懒渲染驱动）。
    但配合 #4 的真实实例 2.75 GB 与「批增量无平台期」，三者的指向一致。

    ⚠️ 同 #3/#4：基线随机器漂移，只在同一台机器上做修复前/后对比。
"""

import gc
import os
import weakref
from pathlib import Path

import psutil
import pytest

# 必须早于 QApplication 与任何 app.* 导入（pytest 收集期即执行）
os.environ.setdefault("DRIFOX_DATA_DIR", str(Path(__file__).resolve().parents[2] / ".drifox-test"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 纯 Qt 块级渲染器：offscreen 下 QWebEngineView 崩溃（0xC0000409），同 #4 取舍
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402

# Qt 属性必须在 QApplication 创建前设置
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

# perf_long：本目录为显式运行的基准。
# qt_no_exception_capture：pytest-qt 会在 teardown 泵一次 Qt 事件循环并把其中
# 抛出的异常转 ERROR。本文件刻意不初始化 WebEngine Profile（初始化会更糟，见
# 文件头「已知盲区 d」），teardown 期窗口内延迟渲染定时器必然抛 RuntimeError。
# 该异常与被测行为无关（数字早已采集完），故禁止 pytest-qt 捕获以免污染结果。
pytestmark = [pytest.mark.perf_long, pytest.mark.qt_no_exception_capture]

# ── 场景参数（保持默认才能与基线表对比）────────────────────────────────
FEED_CARDS = 120  # 场景 1/2/3：灌入卡片数（配额 12 的 10 倍）
SAMPLE_EVERY = 20  # 场景 1：每多少张采样一次
GROUP_CARDS = 60  # 场景 4：每批卡片数
QUOTA_EXPECTED = 12  # _MAX_RENDERED_CARDS（dpr=1 时 effective 值）

_PROC = psutil.Process()


# ── 工具函数 ──────────────────────────────────────────────────────────
def _rss_mb() -> float:
    return _PROC.memory_info().rss / 1048576


def _build_window(qapp):
    """构造真实容器：TabManagerWindow → OpenAIChatToolWindow，出屏放置。"""
    from PySide6.QtWidgets import QWidget

    from app.utils.config import Settings
    from qfluentwidgets import Theme, setTheme

    setTheme(Theme.DARK)
    # ⚠️ 刻意**不**调 init_shared_web_profile：一旦初始化，延迟渲染定时器就会
    # 真的去建 CodeWebViewer → offscreen 下进程崩溃（比抛错更糟）。不初始化时
    # 该路径只是抛 RuntimeError（被 Qt 事件循环捕获，不影响采集）。
    # teardown 期若仍抛错属已知噪声，见文件头「已知盲区 d」。

    class _FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = Settings.get_instance()

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "recycle_bench"

    from app.widgets.tab_manager_window import TabManagerWindow

    # 单例：后续用例直接构造会抛「请使用 get_instance() 获取」，忽略即可
    try:
        TabManagerWindow(_FakePage())
    except RuntimeError:
        pass

    from app.main_widget import OpenAIChatToolWindow

    win = OpenAIChatToolWindow(_FakePage())
    win.resize(1200, 900)
    from tools.ui_driver import place_offscreen

    place_offscreen(win)
    return win


def _feed_cards(win, registry: weakref.WeakSet, n: int, start_index: int = 0) -> int:
    """往真实容器灌 n 张卡片，登记进 _batch_cards；返回新建张数。

    防污染：卡片只经 WeakSet 登记，局部变量用完即 del，测试代码零强引用。
    """
    from app.widgets.message_card import MessageCard

    made = 0
    for i in range(n):
        card = MessageCard("assistant")
        card.resize(900, 300)
        win.chat_layout.addWidget(card)
        card._message_index = start_index + i
        # 手工置位：offscreen 下 ensure_rendered 会崩（见文件头降级说明）。
        # 置位后满足 _batch_fully_rendered 守卫，淘汰链能真实跑到。
        card._lazy_rendered = True
        win._rendered_card_count += 1
        registry.add(card)
        del card  # 不持有强引用
        made += 1
    # 队列排空：已手工置位 _lazy_rendered，无需懒渲染（不改业务代码，只清本实例队列）
    win._pending_lazy_cards.clear()
    win._batch_cards = [[win.chat_layout.itemAt(i).widget()] for i in range(win.chat_layout.count())]
    win._visible_batch_end = len(win._batch_cards)
    win._visible_batch_start = max(0, win._visible_batch_end - win._initial_visible_batch_count)
    return made


def _stream_acc_bytes(registry: weakref.WeakSet) -> int:
    """统计存活卡片上 _stream_text_acc 的残留字节数（leader 提供的真实洞）。"""
    total = 0
    for card in list(registry):  # list() 只在统计瞬间持引用，统计完即释放
        total += len(getattr(card, "_stream_text_acc", "") or "")
    return total


def _settle(times: int = 5) -> None:
    """沉降：只做 gc，不泵 Qt 事件循环。

    ⚠️ 关键：offscreen 下**绝不能泵事件**。窗口内任一延迟渲染定时器
    （_process_next_lazy_batch / _flush_batch_immediate_renders / 视口回收器）
    都会走到 CodeWebViewer → get_shared_web_profile() → 抛
    「共享 WebEngine Profile 尚未初始化」并让进程崩溃（0xC0000409 家族）。
    本文件全程不泵事件，_recycle_lru_batches 是同步调用，不依赖事件循环。
    """
    gc.collect()


# ── 场景 1 + 2：真实容器累积曲线 + 稳态存活数 vs 配额 ─────────────────
def test_perf_recycle_accumulation_curve(qapp):
    """灌入远超配额的卡片，每 SAMPLE_EVERY 张采样 RSS 与存活卡片数。"""
    win = _build_window(qapp)
    quota = win._effective_max_rendered_cards()
    registry: weakref.WeakSet = weakref.WeakSet()
    print(f"[RECY] 配额 _effective_max_rendered_cards()={quota}", flush=True)

    gc.collect()
    rss0 = _rss_mb()
    samples: list[tuple[int, float, int]] = []
    for upto in range(SAMPLE_EVERY, FEED_CARDS + 1, SAMPLE_EVERY):
        _feed_cards(win, registry, SAMPLE_EVERY, start_index=len(registry))
        _settle()
        samples.append((len(registry), _rss_mb() - rss0, len(registry)))
        print(
            f"[RECY] 灌入={samples[-1][0]} 张, RSS 增量={samples[-1][1]:.1f} MB, "
            f"存活卡片={samples[-1][2]} 张（配额 {quota}）",
            flush=True,
        )

    final_alive = len(registry)
    print(
        f"[RECY] 累积曲线汇总 灌入={FEED_CARDS} 张, 存活卡片={final_alive} 张, 配额={quota}, "
        f"超出={final_alive - quota} 张, RSS 增量={samples[-1][1]:.1f} MB",
        flush=True,
    )
    assert final_alive > 0, "存活卡片数为 0，采集链路失效"
    assert final_alive <= FEED_CARDS, "存活数超过灌入数，计数异常"


# ── 场景 3：淘汰链前后对比 ────────────────────────────────────────────
def test_perf_recycle_unload_before_after(qapp):
    """手动触发 _recycle_lru_batches，对比淘汰前后 RSS 与存活卡片数。"""
    win = _build_window(qapp)
    quota = win._effective_max_rendered_cards()
    registry: weakref.WeakSet = weakref.WeakSet()

    _feed_cards(win, registry, FEED_CARDS)
    _settle()

    before_rss = _rss_mb()
    before_alive = len(registry)
    before_rendered = win._rendered_card_count
    before_none = sum(1 for c in win._batch_cards if c is None)
    before_acc = _stream_acc_bytes(registry)
    print(
        f"[RECY] 淘汰前 存活卡片={before_alive} 张, 记账渲染数={before_rendered}, "
        f"_batch_cards 空槽={before_none}, RSS={before_rss:.1f} MB, "
        f"_stream_text_acc={before_acc} B",
        flush=True,
    )

    # 真实淘汰链：温和层 LRU → 内部调 _unload_batch
    win._recycle_lru_batches()
    _settle(times=10)

    after_rss = _rss_mb()
    after_alive = len(registry)
    after_rendered = win._rendered_card_count
    after_none = sum(1 for c in win._batch_cards if c is None)
    after_acc = _stream_acc_bytes(registry)
    print(
        f"[RECY] 淘汰后 存活卡片={after_alive} 张, 记账渲染数={after_rendered}, "
        f"_batch_cards 空槽={after_none}, RSS={after_rss:.1f} MB, "
        f"_stream_text_acc={after_acc} B",
        flush=True,
    )
    print(
        f"[RECY] 淘汰效果 卡片 {before_alive} → {after_alive}（Δ={after_alive - before_alive}）, "
        f"RSS {before_rss:.1f} → {after_rss:.1f} MB（Δ={after_rss - before_rss:+.1f}）, "
        f"空槽 {before_none} → {after_none}, 配额={quota}",
        flush=True,
    )
    assert before_alive > 0, "淘汰前存活卡片数为 0，采集链路失效"


# ── 场景 4：跨会话复用度 ──────────────────────────────────────────────
def test_perf_recycle_cross_group_reuse(qapp):
    """两批各灌 GROUP_CARDS 张，看总存活数是 ≈2×配额（复用）还是全量常驻。"""
    win = _build_window(qapp)
    quota = win._effective_max_rendered_cards()
    registry: weakref.WeakSet = weakref.WeakSet()

    gc.collect()
    rss0 = _rss_mb()
    _feed_cards(win, registry, GROUP_CARDS)
    _settle()
    after_g1 = len(registry)
    print(f"[RECY] 批次1 灌入={GROUP_CARDS} 张, 存活卡片={after_g1} 张", flush=True)

    # 第二批：模拟切会话后新会话的卡片进同一容器
    _feed_cards(win, registry, GROUP_CARDS, start_index=len(registry))
    win._recycle_lru_batches()
    _settle()

    total = len(registry)
    print(
        f"[RECY] 两批合计 灌入={2 * GROUP_CARDS} 张, 存活卡片={total} 张, 配额={quota}, "
        f"理想复用≈{2 * quota} 张, 全量常驻={2 * GROUP_CARDS} 张, "
        f"RSS 增量={_rss_mb() - rss0:.1f} MB",
        flush=True,
    )
    assert total > 0, "跨批存活卡片数为 0，采集链路失效"
