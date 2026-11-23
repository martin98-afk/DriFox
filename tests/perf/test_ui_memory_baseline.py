# -*- coding: utf-8 -*-
"""UI 层内存回归基准（修复前基线采集，验证「GB 级大头在 Qt/Chromium 侧」假设）。

背景：#3 数据层基准（tests/perf/test_memory_baseline.py）实测单会话 900 条消息
仅 7.9 MB、每会话 5.1 MB，判「1GB/会话」不成立。故 GB 级必然在数据层之外。
本文件量 UI 层（MessageCard / 渲染器 viewer / QPixmap）的内存开销，给出可比基线。

分工边界：本文件只做**定量采集**（造尺子），「谁持有引用没释放」的定性定位由
#1 root-cause-analyst 负责，两者不重叠。

运行：
    cd <仓库根> && uv run pytest tests/perf/test_ui_memory_baseline.py -v -s
    （tests/perf 被 addopts 默认排除，必须显式指定路径；-s 才能看到 [UI-MEM] 打印）

⚠️ WebEngine 未启用（offscreen 下 QWebEngineView 崩溃 0xC0000409，见下）：
    本文件统一设 DRIFOX_QT_RENDERER=1 走纯 Qt 块级渲染器（MarkdownBlockViewer），
    与 tests/perf/test_stream_render_bench.py 的取舍一致。**每 QWebEngineView 的
    Chromium 成本本文件未覆盖**，改由「真实进程快照」场景（场景 5）侧面取证。

场景
  1. 卡片实例成本：构造 N 个 MessageCard（不启事件循环），记录每实例 RSS /
     tracemalloc 增量 → 「每条消息的 UI 成本」。
  2. 已渲染卡片成本：构造 N 个卡片并 ensure_rendered（纯 Qt 渲染器），记录
     每实例增量 + 线性度（每 10 张采样一次，检是否有回收/平台期）。
  3. 多会话 UI 层线性度：K 个会话 × M 条消息，统计卡片总数与总 RSS →
     「每会话 UI 层增量」——直接对照用户「几个对话 2-3GB」的关键指标。
  4. QPixmap / 图标重复解码：统计 QPixmap 实例数与 QIcon 缓存命中情况
     （估算口径，见场景内说明）。
  5. 真实进程快照（只读，不构造任何 Qt 对象）：若本机正跑着 Drifox 实例，
     打印其主进程 RSS + QtWebEngineProcess 子进程数与 RSS 之和。这是唯一能
     触达真实 Chromium 成本的通道，无实例时自动 skip。

基线（修复前）
    采集环境：Windows / Python 3.14 / dev 分支 caa2137d / offscreen + DRIFOX_QT_RENDERER=1
    采集命令：uv run pytest tests/perf/test_ui_memory_baseline.py -v -s

    | 指标 | 修复前基线 | 修复后 | 说明 |
    |---|---|---|---|
    | 空卡片实例成本 | 0.88 MB/卡 | — | 场景 1，60 张未渲染 MessageCard，总增 52.6 MB |
    | 已渲染卡片成本 | 2.96 MB/卡 | — | 场景 2，100 张含 MarkdownBlockViewer，总增 296 MB |
    | 每会话 UI 层增量 | 146.6 MB/会话 | — | 场景 3，5 会话 × 50 条 = 250 卡，总增 733 MB |
    | 每卡片 UI 成本 | 2.93 MB/卡 | — | 场景 3 口径，与场景 2 一致 |
    | 真实实例总内存 | 2770 MB | — | 场景 5，主进程 1772 + 子进程 997（含 3 个 QtWebEngineProcess 665 MB） |

    初判（供 #1 / #4 参考）：
    1. **GB 级大头确在 UI 层，且是「卡片实例数线性累积」而非单卡暴涨**。每卡片约
       2.9 MB（纯 Qt 渲染器口径，未含 Chromium），250 张卡即 733 MB。真实实例上
       3 个 QtWebEngineProcess 又叠加 665 MB —— 与用户「几个对话 2-3GB」吻合。
    2. 场景 2 的批增量长期稳定在 26-31 MB/10 张（无平台期、无回落），说明
       **虚拟滚动回收未生效或回收后对象未释放**——若回收正常，累计曲线应趋于平缓。
       这一点是 #1 做引用链定位时最该优先验证的假设。
    3. 场景 4 口径下 QPixmap/QIcon 实例数不随卡片数增长（增量 0），图标层未见
       重复解码，可排除。

    ⚠️ 同 #3：基线随机器漂移，只在同一台机器上做修复前/后对比。
"""

import gc
import os
import tracemalloc
from pathlib import Path

import psutil
import pytest

# 必须早于 QApplication 与任何 app.* 导入（pytest 收集期即执行）
os.environ.setdefault("DRIFOX_DATA_DIR", str(Path(__file__).resolve().parents[2] / ".drifox-test"))
# [E11-#15] 平台跟随（不再强制 offscreen）：稳态对照组需构造真实容器
# （OpenAIChatToolWindow），offscreen 下窗口构造实测 0xC0000409（2026-11-18，
# 与 test_recycle_efficacy.py 方案 B 同款取舍）。真窗 + DRIFOX_QT_RENDERER=1
# 纯 Qt 渲染器已无 WebEngine 崩溃源；如需 offscreen 请显式 export QT_QPA_PLATFORM。
# 纯 Qt 块级渲染器：offscreen 下 QWebEngineView 崩溃（0xC0000409），
# 与 test_stream_render_bench.py 保持一致取舍。
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

# Qt 属性必须在 QApplication 创建前设置（否则 message_card 导入
# QtWebEngineWidgets 直接 ImportError）
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

pytestmark = pytest.mark.perf_long

# ── 场景参数（保持默认才能与基线表对比）────────────────────────────────
RAW_CARD_N = 60  # 场景 1：未渲染卡片数
RENDERED_CARD_N = 100  # 场景 2：已渲染卡片总数
RENDERED_SAMPLE_EVERY = 10  # 场景 2：每多少张采样一次
SESSION_K = 5  # 场景 3：会话数
SESSION_M = 50  # 场景 3：每会话消息数（= 卡片数）
QT_SETTLE_MS = 100  # 每批构造后留给 Qt 的沉降时间

_PROC = psutil.Process()


# ── 工具函数 ──────────────────────────────────────────────────────────
def _rss_mb() -> float:
    return _PROC.memory_info().rss / 1048576


def _mb(n: float) -> float:
    return n / 1048576


def _children_mb() -> tuple[int, float]:
    """本进程直接子进程的数量与 RSS 之和（MB）。"""
    kids = _PROC.children()
    return len(kids), sum(k.memory_info().rss / 1048576 for k in kids)


def _settle(ms: int = QT_SETTLE_MS) -> None:
    """让 Qt 事件循环跑一会儿（懒渲染定时器 / 挂起的 deleteLater）。"""
    QTest.qWait(ms)
    QCoreApplication.instance().processEvents()


# ── 场景 1：未渲染卡片实例成本 ────────────────────────────────────────
def test_perf_ui_raw_card_cost(qapp):
    """构造 N 个 MessageCard（不渲染），测每实例的 RSS 与 Python 堆成本。"""
    from app.widgets.message_card import MessageCard

    gc.collect()
    rss0 = _rss_mb()
    tracemalloc.start()
    cards = [MessageCard("assistant") for _ in range(RAW_CARD_N)]
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    rss1 = _rss_mb()

    n = len(cards)
    per_rss = (rss1 - rss0) / n
    per_py = _mb(cur) / n
    print(
        f"[UI-MEM] 空卡片 实例数={n}, 总 RSS 增量={rss1 - rss0:.1f} MB, "
        f"每实例 RSS={per_rss:.2f} MB | Python 堆持有={_mb(cur):.1f} MB, 每实例={per_py:.2f} MB, "
        f"tracemalloc 峰值={_mb(peak):.1f} MB",
        flush=True,
    )
    assert n == RAW_CARD_N
    assert rss1 - rss0 > 0, "卡片构造无 RSS 增长，采集链路疑似失效"
    del cards


# ── 场景 2：已渲染卡片成本 + 线性度 ───────────────────────────────────
def test_perf_ui_rendered_card_cost(qapp):
    """构造并渲染 N 个卡片，每 RENDERED_SAMPLE_EVERY 张采样一次，检线性度。"""
    from app.widgets.message_card import MessageCard

    gc.collect()
    rss0 = _rss_mb()
    prev = rss0
    cards: list = []
    steps: list[float] = []
    batch = 0
    while len(cards) < RENDERED_CARD_N:
        for _ in range(RENDERED_SAMPLE_EVERY):
            card = MessageCard("assistant")
            card.resize(900, 600)
            card.show()
            card.ensure_rendered()
            cards.append(card)
        _settle()
        gc.collect()
        cur = _rss_mb()
        steps.append(cur - prev)
        prev = cur
        batch += 1
        print(
            f"[UI-MEM] 已渲染卡片 n={len(cards)}, 累计 RSS={cur - rss0:.1f} MB, "
            f"本批增量={steps[-1]:.1f} MB, 每实例={(cur - rss0) / len(cards):.2f} MB",
            flush=True,
        )
        if batch > RENDERED_CARD_N // RENDERED_SAMPLE_EVERY + 2:  # 防御死循环
            break

    n = len(cards)
    total = prev - rss0
    viewers = sum(1 for c in cards if getattr(c, "viewer", None) is not None)
    n_kids, kids_mb = _children_mb()
    print(
        f"[UI-MEM] 已渲染卡片汇总 实例数={n}, viewer 数={viewers}, 总 RSS={total:.1f} MB, "
        f"每实例={total / n:.2f} MB | 批增量 min={min(steps):.1f} max={max(steps):.1f} MB, "
        f"子进程={n_kids} 个 / {kids_mb:.1f} MB",
        flush=True,
    )
    assert n >= RENDERED_CARD_N
    assert total > 0, "渲染后无 RSS 增长，采集链路疑似失效"
    del cards


# ── 场景 3：多会话 UI 层线性度（对照「几个对话 2-3GB」的关键指标）──────
def test_perf_ui_session_linearity(qapp):
    """K 个会话 × M 条消息，全部建卡片并渲染，输出每会话 UI 层增量。

    [E11-#15] 口径标注：本用例卡片全部强引用在裸列表、无容器无回收器，
    量得的是**零回收上界**（修复前基线 127.9 MB/会话 即此口径）。
    「容器内 + 配额回收后稳态」对照见
    ``test_perf_ui_session_linearity_recycled_steady_state``（本文件下方）。
    """
    from app.widgets.message_card import MessageCard

    gc.collect()
    rss0 = _rss_mb()
    prev = rss0
    per_session: list[float] = []
    cards: list = []
    for k in range(SESSION_K):
        for _ in range(SESSION_M):
            card = MessageCard("assistant")
            card.resize(900, 600)
            card.show()
            card.ensure_rendered()
            cards.append(card)
        _settle()
        gc.collect()
        cur = _rss_mb()
        per_session.append(cur - prev)
        prev = cur
        print(
            f"[UI-MEM] 会话 k={k + 1}/{SESSION_K}, 累计卡片={len(cards)}, "
            f"累计 RSS={cur - rss0:.1f} MB, 本会话增量={per_session[-1]:.1f} MB",
            flush=True,
        )

    total = prev - rss0
    avg_session = total / SESSION_K
    n_kids, kids_mb = _children_mb()
    print(
        f"[UI-MEM] 多会话 UI 层 会话数={SESSION_K}, 每会话消息={SESSION_M}, 卡片总数={len(cards)}, "
        f"总 RSS={total:.1f} MB, 每会话增量={avg_session:.1f} MB/会话, 每卡片={total / len(cards):.2f} MB "
        f"| 子进程={n_kids} 个 / {kids_mb:.1f} MB",
        flush=True,
    )
    assert len(cards) == SESSION_K * SESSION_M
    assert total > 0, "多会话 UI 层无 RSS 增长，采集链路疑似失效"
    del cards


# ── 场景 3b：容器内 + 配额回收后稳态（[E11-#15] 对照组）───────────────
_CACHED_WIN = None  # 模块级复用（同 test_recycle_efficacy：第二实例构造会崩）


class _FakeSettledViewer:
    """高度落定守卫的最小 viewer 桩（同 test_recycle_efficacy：纯 Qt 渲染器语义）。"""

    def __init__(self, h: int = 300):
        self._h = h

    def height(self) -> int:
        return self._h


def _build_window(qapp):
    """构造真实容器（与 test_recycle_efficacy._build_window 同款：真窗出屏 + 复用）。"""
    global _CACHED_WIN
    if _CACHED_WIN is not None:
        return _CACHED_WIN
    from PyQt5.QtWidgets import QWidget

    from qfluentwidgets import Theme, setTheme

    setTheme(Theme.DARK)

    class _FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = __import__("app.utils.config", fromlist=["Settings"]).Settings.get_instance()

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "memory_baseline"

    from app.widgets.tab_manager_window import TabManagerWindow

    try:
        TabManagerWindow(_FakePage())
    except RuntimeError:
        pass

    from app.main_widget import OpenAIChatToolWindow

    win = OpenAIChatToolWindow(_FakePage())
    win.resize(1200, 900)
    from tools.ui_driver import place_offscreen

    place_offscreen(win)
    _CACHED_WIN = win
    return win


def _feed_cards_steady(win, n: int, start_index: int) -> None:
    """往真实容器灌 n 张守卫合规卡（桩 viewer + 几何排开），增量并入批次结构。

    与 test_recycle_efficacy._feed_cards 同款守卫构造；批次结构**增量 extend**
    而非整批重建——重建会把已卸载批次的占位 widget 误作活卡批次，破坏回收链
    记账（实测记账 12→32 漂移的根因）。
    """
    from app.widgets.message_card import MessageCard

    new_batches = []
    for i in range(n):
        card = MessageCard("assistant")
        card.resize(900, 300)
        win.chat_layout.addWidget(card)
        card.setGeometry(0, (start_index + i) * 300, 900, 300)
        card._message_index = start_index + i
        card._lazy_rendered = True
        card.viewer = _FakeSettledViewer(300)
        card._viewer_async_height = False
        win._rendered_card_count += 1
        new_batches.append([card])
        del card
    win._pending_lazy_cards.clear()
    win._batch_cards.extend(new_batches)
    win._visible_batch_end = len(win._batch_cards)
    win._visible_batch_start = max(0, win._visible_batch_end - win._initial_visible_batch_count)


def _settle_placeholders(win) -> None:
    """[E11-#15] 把占位 widget 摆回被卸批次的几何位置（布局未激活的模拟补偿）。

    _install_batch_placeholder 的占位在未激活布局里 geometry 重叠 (0,0)，
    viewport 扫描会把全部占位判成「视口内」→ 视口 range 被撑满 → 保护范围
    扩大 → 每轮少卸。占位应躺在被卸批次的位置（本构造每批 1 卡 × 300px，
    y = 批 idx × 300）。
    """
    for idx, ph in (getattr(win, "_batch_placeholders", None) or {}).items():
        try:
            ph.setGeometry(0, idx * 300, 900, max(1, int(ph.height())))
        except RuntimeError:
            pass


def test_perf_ui_session_linearity_recycled_steady_state(qapp):
    """[E11-#15] 容器内 + 配额回收后稳态：每会话灌卡后触发回收链，量 RSS/卡数。

    线上语义对照：真实 UI 有 _batch_cards 容器与 _recycle_lru_batches 淘汰链，
    稳态卡片数应为配额级（12 卡）而非全量常驻——与场景 3 的零回收上界口径
    （146.6 MB/会话）相对照。
    """
    win = _build_window(qapp)
    quota = win._effective_max_rendered_cards()
    gc.collect()
    rss0 = _rss_mb()
    prev = rss0
    per_session: list[float] = []
    steady_cards: list[int] = []
    fed = len(win._batch_cards)
    for k in range(SESSION_K):
        _feed_cards_steady(win, SESSION_M, start_index=fed)
        fed += SESSION_M
        win._recycle_lru_batches()
        _settle_placeholders(win)  # 占位几何补偿：防视口 range 被重叠占位撑大
        gc.collect()
        cur = _rss_mb()
        per_session.append(cur - prev)
        prev = cur
        rendered = win._rendered_card_count
        steady_cards.append(rendered)
        print(
            f"[UI-MEM] 稳态 会话 k={k + 1}/{SESSION_K}, 灌入={SESSION_M} 张, "
            f"回收后记账卡数={rendered}（配额 {quota}）, 布局={win.chat_layout.count()}, "
            f"空槽={sum(1 for c in win._batch_cards if c is None)}, "
            f"本会话 RSS 增量={per_session[-1]:.1f} MB, 累计={cur - rss0:.1f} MB",
            flush=True,
        )

    total = prev - rss0
    avg_session = total / SESSION_K
    tail_avg = sum(per_session[2:]) / max(1, len(per_session[2:]))  # 平台期口径：后段会话均值
    print(
        f"[UI-MEM] 回收后稳态汇总 会话数={SESSION_K}, 每会话灌入={SESSION_M} 张, "
        f"稳态记账卡数={steady_cards[-1]}（配额 {quota}）, 总 RSS 增量={total:.1f} MB, "
        f"平均={avg_session:.1f} MB/会话, 后段均值={tail_avg:.1f} MB/会话 "
        f"| 对照上界口径 146.6 MB/会话",
        flush=True,
    )
    assert steady_cards[-1] <= quota, f"回收后稳态卡数应回到配额内：{steady_cards[-1]}/{quota}"
    assert all(c <= quota for c in steady_cards), f"每会话回收后都应回到配额内：{steady_cards}"


# ── 场景 4：QPixmap / 图标重复解码检测（估算口径）─────────────────────
def test_perf_ui_pixmap_instance_count(qapp):
    """统计 QPixmap / QIcon 实例数（估算口径，非精确 sizeof）。

    口径说明：`sys.getsizeof` 只给 Python 包装对象大小，拿不到 Qt 内部
    QPixmapCache 与底层位图，故本场景只报**实例数量级**，不折算 MB。
    用途：判断「同一图标是否被反复解码成多个 QPixmap」——数量随卡片数
    线性增长即说明图标未走缓存。
    """
    from PyQt5.QtGui import QIcon, QPixmap

    def _count(cls) -> int:
        return sum(1 for o in gc.get_objects() if isinstance(o, cls))

    gc.collect()
    pix0, icon0 = _count(QPixmap), _count(QIcon)
    cards = []
    from app.widgets.message_card import MessageCard

    for _ in range(30):
        card = MessageCard("assistant")
        card.resize(900, 600)
        card.show()
        card.ensure_rendered()
        cards.append(card)
    _settle()
    gc.collect()
    pix1, icon1 = _count(QPixmap), _count(QIcon)

    per_card = (pix1 - pix0) / 30
    print(
        f"[UI-MEM] QPixmap 实例 前={pix0} 后={pix1} 增={pix1 - pix0} 每卡={per_card:.2f} | "
        f"QIcon 实例 前={icon0} 后={icon1} 增={icon1 - icon0} "
        f"（估算口径：仅实例数，不含底层位图）",
        flush=True,
    )
    assert pix1 >= pix0
    del cards


# ── 场景 5：真实进程快照（唯一能触达 Chromium 成本的通道）─────────────
def test_perf_ui_real_process_snapshot():
    """只读快照：若本机正跑着 Drifox 实例，打印主进程 + QtWebEngineProcess 子进程内存。

    本文件其余场景因 offscreen 限制走纯 Qt 渲染器，测不到 Chromium；此场景
    从真实运行实例侧面取证。无实例时 skip（不是失败）。
    """
    targets = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info["name"] or ""
        if name.lower().startswith("drifox"):
            targets.append(proc)
    if not targets:
        pytest.skip("本机未运行 Drifox 实例，跳过真实进程快照（需先启动应用复现）")

    lines = []
    grand = 0.0
    for proc in targets:
        try:
            main_rss = proc.memory_info().rss / 1048576
            kids = proc.children(recursive=True)
            web_kids = [k for k in kids if "QtWebEngineProcess" in k.name()]
            web_mb = sum(k.memory_info().rss / 1048576 for k in web_kids)
            all_kids_mb = sum(k.memory_info().rss / 1048576 for k in kids)
            total = main_rss + all_kids_mb
            grand += total
            lines.append(
                f"[UI-MEM] 真实实例 pid={proc.pid} 主进程 RSS={main_rss:.0f} MB, "
                f"QtWebEngineProcess={len(web_kids)} 个 / {web_mb:.0f} MB, "
                f"全部子进程={len(kids)} 个 / {all_kids_mb:.0f} MB, 合计={total:.0f} MB, "
                f"线程数={proc.num_threads()}"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            lines.append(f"[UI-MEM] 真实实例 pid={proc.pid} 读取失败: {e}")
    print("\n".join(lines), flush=True)
    print(f"[UI-MEM] 真实实例总内存={grand:.0f} MB（主进程 + 全部子进程）", flush=True)
    assert grand > 0, "快照采集失败"
