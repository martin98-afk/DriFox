# -*- coding: utf-8 -*-
"""消息流式渲染性能基准（WebEngine 通道 / 真机桌面会话口径）。

⚠️ 运行前提：
    1. 须真机桌面会话运行（Windows 桌面），**不得**在 offscreen / 无图形
       会话下运行——CodeWebViewer(QWebEngineView) 在 offscreen 平台下原生层
       0xC0000409 必崩（tests/perf/test_new_tab_baseline.py 头注、tests/ui
       B/A 档结论均记录）。本文件带环境守卫自动 skip。
    2. 运行前关闭正在运行的 DriFox 实例（WebEngine 共享 profile 与
       Chromium user-data-dir 锁冲突会污染采样）。
    3. 不设 QT_QPA_PLATFORM（真机 windows 口径）；不设 DRIFOX_QT_RENDERER
       （走生产默认 CodeWebViewer 通道，与 tests/perf/test_stream_render_bench.py
       的 Qt 灰度通道互补，两份数据才可对照）。

测量对象：MessageCard.append_text 全链路（assistant 路径，WebEngine 版）
    _content_data 块合并 → think/tag/fence 守卫 → viewer._append_text_incremental
    （JS 增量 DOM 注入）→ _schedule_render（90ms 合并派发 + 打字机揭示）→
    finish_streaming 收尾全量渲染。

与 Qt 通道差异：js_ready 等待上限放宽（WebEngine 首次骨架注入 ~2-5s，
Qt 通道恒就绪）；无 reconcile 口径（渲染在 Chromium 进程，Python 侧只有
调度与 JS 投递耗时），页面侧真实渲染以主线程帧节奏间接反映。

指标（每轮 print 输出）：
    feed_elapsed_ms   首 chunk → 末 chunk 投递完成（墙钟）
    total_elapsed_ms  首 chunk → finish_streaming 完成 + 定时器冲刷（墙钟）
    append_p50/p90/p99/max_ms  单次 card.append_text 耗时分布
    append_total_ms   append_text 累计占用
    finish_ms         finish_streaming 单次耗时
    frame_p50/p99_ms  主线程 1ms tick 帧间隔分布
    jank_count        帧间隔 >33ms 次数（掉帧）
    big_jank_count    帧间隔 >100ms 次数（卡顿）

场景：与 Qt 通道同款——8ms 间隔 × 40 字符/chunk × 800 chunks = 32000 字符
    混合 markdown（段落 / 列表 / 成对代码块）。

运行：
    cd <仓库根> && uv run pytest tests/perf/test_stream_render_bench_web.py -v -s
    （tests/perf 被 addopts 默认排除，必须显式指定本文件路径）

断言策略：只断言「链路可用 + 数据完整」（viewer 就绪、字符全量到达、指标
非空），不断言性能数值——基线以 print 输出供采集，阈值随机器漂移。
"""

import os
import time

# 环境守卫必须最早生效：offscreen / 无图形会话直接 skip（不 import Qt 重链路，
# 不给 QApplication 任何创建机会——WebEngine 在该环境下创建即崩）
if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
    import pytest

    pytest.skip("WebEngine channel requires a real desktop session", allow_module_level=True)

import statistics

# 显式确保走真机平台口径（setdefault：外部已显式设置时尊重）
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
# ⚠️ 不设 DRIFOX_QT_RENDERER：本文件测生产默认 WebEngine 通道

import pytest  # noqa: E402

# Qt 属性必须在 QApplication 创建前设置（与 main.py / test_new_tab_baseline 对齐）
from PySide6.QtCore import QCoreApplication, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

# QtWebEngine 类必须在 QCoreApplication 实例化前导入（main.py 同款约束）
from PySide6.QtWebEngineCore import QWebEngineSettings  # noqa: E402,F401

ROUNDS = 3  # 基线重复轮数
CHUNK_CHARS = 40  # 每 chunk 字符数（贴近真实 LLM 吞吐粒度）
FEED_INTERVAL_MS = 8  # 投递节奏
TOTAL_CHARS = 32000  # 总量 ≥3 万字符
JS_READY_TIMEOUT_MS = 30000  # WebEngine 首次骨架注入 ~2-5s（冷启动更久），放宽到 30s

# 混合 markdown 模板：普通段落（高频软边界）+ 列表 + 成对代码块（fence 守卫）。
# 无 <think> / 渲染型 fence（```echarts），避免测到「静默累积」特殊分支。
_MD_TEMPLATE = (
    "这是一段用于模拟大语言模型流式输出的中文正文，句子以句号分隔，"
    "段落之间留有空行以触发自然边界全量渲染。"
    "流式渲染管线会在软边界处合并派发渲染任务，在自然边界处立即刷新。\n\n"
    "- 列表项一：块级 reconcile 只重建尾部变化块，key 指纹命中直接复用\n"
    "- 列表项二：增量注入经节流合并，避免每 chunk 都打穿渲染层\n"
    "- 列表项三：finish 收尾再做一次全量渲染保证最终态一致\n\n"
    "```python\n"
    "def bench_point(cards: list[str]) -> int:\n"
    "    return sum(len(c) for c in cards)\n"
    "```\n\n"
)


def _build_payload() -> str:
    """循环填充模板到 TOTAL_CHARS（截断到 chunk 对齐边界）"""
    unit = _MD_TEMPLATE
    n, rem = divmod(TOTAL_CHARS, len(unit))
    payload = unit * n + unit[: rem - rem % CHUNK_CHARS]
    return payload


def _pct(vals: list, q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def _run_one_round(qapp, round_idx: int, payload: str) -> dict:
    """单轮压测：新 MessageCard + 模拟定时投递，返回指标 dict"""
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()

    card = MessageCard("assistant")
    card.resize(900, 800)
    card.show()
    card.ensure_rendered()
    for _ in range(JS_READY_TIMEOUT_MS // 100):  # WebEngine 骨架就绪等待（放宽）
        QTest.qWait(100)
        if card.viewer is not None and getattr(card.viewer, "_is_js_ready", False):
            break
    viewer = card.viewer
    assert viewer is not None and getattr(viewer, "_is_js_ready", False), (
        f"round{round_idx}: viewer 未就绪（WebEngine 骨架注入超时 {JS_READY_TIMEOUT_MS}ms）"
    )

    card.start_streaming_anim()
    QTest.qWait(200)

    append_cost: list[float] = []
    _orig_append = card.append_text

    def timed_append(text, immediate_render=True):
        t0 = time.perf_counter()
        _orig_append(text, immediate_render)
        append_cost.append((time.perf_counter() - t0) * 1000)

    card.append_text = timed_append

    # 主线程帧节奏采样：1ms PreciseTimer，间隔突刺即阻塞证据
    frames: list[float] = []
    _last = time.perf_counter()

    def on_tick():
        nonlocal _last
        now = time.perf_counter()
        frames.append((now - _last) * 1000)
        _last = now

    tick = QTimer()
    tick.setTimerType(Qt.PreciseTimer)
    tick.timeout.connect(on_tick)

    total = len(payload)
    sent = 0
    feed_timer = QTimer()
    feed_timer.setInterval(FEED_INTERVAL_MS)

    def feed():
        nonlocal sent
        if sent >= total:
            feed_timer.stop()
            return
        piece = payload[sent : sent + CHUNK_CHARS]
        sent += len(piece)
        card.append_text(piece)

    feed_timer.timeout.connect(feed)

    t_start = time.perf_counter()
    tick.start(1)
    feed_timer.start()

    while sent < total:
        QTest.qWait(20)
    feed_elapsed = (time.perf_counter() - t_start) * 1000

    QTest.qWait(800)  # 冲刷 80ms 增量节流 + 90ms 渲染合并定时器 + 打字机揭示尾

    t_finish = time.perf_counter()
    card.finish_streaming()
    finish_ms = (time.perf_counter() - t_finish) * 1000
    QTest.qWait(300)
    total_elapsed = (time.perf_counter() - t_start) * 1000

    tick.stop()
    card.append_text = _orig_append
    card.deleteLater()
    QTest.qWait(200)

    assert sent == total, f"round{round_idx}: 字符未投递完 {sent}/{total}"
    assert append_cost, f"round{round_idx}: append_text 未被调用"

    metrics = {
        "round": round_idx,
        "total_chars": total,
        "chunks": (total + CHUNK_CHARS - 1) // CHUNK_CHARS,
        "feed_elapsed_ms": round(feed_elapsed, 1),
        "total_elapsed_ms": round(total_elapsed, 1),
        "append_p50_ms": round(_pct(append_cost, 0.50), 3),
        "append_p90_ms": round(_pct(append_cost, 0.90), 3),
        "append_p99_ms": round(_pct(append_cost, 0.99), 3),
        "append_max_ms": round(max(append_cost), 3),
        "append_total_ms": round(sum(append_cost), 1),
        "finish_ms": round(finish_ms, 1),
        "frame_p50_ms": round(_pct(frames, 0.50), 3),
        "frame_p99_ms": round(_pct(frames, 0.99), 3),
        "jank_count": sum(1 for v in frames if v > 33),
        "big_jank_count": sum(1 for v in frames if v > 100),
    }
    return metrics


def _print_metrics(m: dict) -> None:
    print(
        f"\n--- round {m['round']} | {m['total_chars']} chars / {m['chunks']} chunks ---",
        flush=True,
    )
    print(
        f"  feed_elapsed={m['feed_elapsed_ms']}ms  total_elapsed={m['total_elapsed_ms']}ms  finish={m['finish_ms']}ms",
        flush=True,
    )
    print(
        f"  append p50={m['append_p50_ms']} p90={m['append_p90_ms']} p99={m['append_p99_ms']} max={m['append_max_ms']}ms"
        f"  累计={m['append_total_ms']}ms",
        flush=True,
    )
    print(
        f"  frame p50={m['frame_p50_ms']}ms p99={m['frame_p99_ms']}ms"
        f"  jank(>33ms)={m['jank_count']}  big-jank(>100ms)={m['big_jank_count']}",
        flush=True,
    )


def test_bench_stream_render_webengine(qapp):
    """WebEngine 通道流式基线：1 轮预热 + 3 轮正式采样，打印指标供采集。"""
    payload = _build_payload()
    assert len(payload) >= 30000, f"压测文本不足 3 万字符：{len(payload)}"

    # 预热轮：骨架注入 / Chromium 上下文初始化 / 字体加载等一次性成本，不计入基线
    _run_one_round(qapp, 0, payload[: 5 * CHUNK_CHARS])
    QTest.qWait(300)

    results = []
    for i in range(1, ROUNDS + 1):
        m = _run_one_round(qapp, i, payload)
        _print_metrics(m)
        results.append(m)

    # 稳定性：3 轮 total_elapsed 离散度（max/min）应可控（<2x），异常尖刺说明环境噪声大
    totals = [m["total_elapsed_ms"] for m in results]
    spread = max(totals) / max(1.0, min(totals))
    print(
        f"\n=== 基线汇总：total_elapsed 3 轮 = {totals} ms，max/min 离散度 = {spread:.2f}x ===",
        flush=True,
    )
    assert spread < 2.0, f"3 轮基线离散度过大（{spread:.2f}x），环境噪声影响采样可信度"

    med = {k: statistics.median(m[k] for m in results) for k in results[0]}
    _print_metrics(med)
