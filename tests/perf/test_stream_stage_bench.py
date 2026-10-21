# -*- coding: utf-8 -*-
"""流式 token → 卡片刷新：viewer 层阶段耗时分解基准（T2 基线，T5 优化对照）。

定位与既有基准的关系：
    - `test_stream_render_bench.py` 测 **MessageCard.append_text 全链路**
      （含 _content_data 合并 / think·tag·fence 守卫 / viewer 增量 /
      _schedule_render / reconcile / finish）。它此前因 MessageCard 无 parent
      时 `_update_anim` 读 `self._parent.chat_scroll_area` 抛 AttributeError，
      被 Qt 事件循环捕获判 CALL ERROR；T5 已在该处补 AttributeError 防御，
      该文件现已恢复正常。
    - `test_stream_render_bench_web.py` 测 WebEngine 真机通道，需桌面会话。

本文件取中间层：**MarkdownBlockViewer 单卡的流式刷新路径**，把一次
"token 推送 → 卡片刷新" 拆成可归因的阶段：

    append_chunk      累积 md + 自适应档位 + 派发决策（_schedule_render）
    boundary_dispatch 派发决策命中分支（clean/soft/adaptive）→ 真正触发渲染的时机
    render            软/硬边界或兑底时的 _perform_update → _reconcile(force=True)
    reconcile_inner   _reconcile 内部：parse_blocks 解析 vs reconcile_widgets 差量对齐
    finish            finish_streaming 收尾全量 reconcile

渲染通道：DRIFOX_QT_RENDERER=1 纯 Qt 块级渲染器（offscreen 下 WebEngine
    原生层 0xC0000409 必崩，同 test_stream_render_bench.py 取舍）。

场景参数与 test_stream_render_bench.py 完全一致（8ms × 40 字符 × 800 chunks
    = 32000 字符混合 markdown），两份数据可直接上下游对照。

插桩方式：只环绕原实现计时 + patch 模块级 parse_blocks / reconcile_widgets，
    **不修改任何 app/ 源码**。⚠️ 计时切勿复刻被测逻辑——那会绕过被测的缓存
    路径，导致优化效果无法体现（口径失真）。

运行：
    cd <仓库根> && uv run pytest tests/perf/test_stream_stage_bench.py -v -s
    （tests/perf 被 addopts 默认排除，必须显式指定本文件路径）

基线（T2 · 修复前 / T5 · 优化后对照）
    采集环境：Windows / Python 3.14 / dev 分支 / offscreen + DRIFOX_QT_RENDERER=1
    采集命令：uv run pytest tests/perf/test_stream_stage_bench.py -v -s

    | 阶段 | 优化前 3 轮累计 | 优化后 3 轮累计 | 降幅 | 优化后 p50 / p95 |
    |---|---|---|---|---|
    | `_reconcile`（渲染） | 6283.9 ms | 2972.8 ms | **−52.7%** | 10.7 / 18.6 ms |
    | └ `parse_blocks` | 3248.4 ms | 424.2 ms | **−86.9%** | 1.63 / 3.47 ms |
    | └ 差量对齐 | 2989.1 ms | 2525.3 ms | −15.5% | 3.4 / 15.9 ms |
    | `append_chunk`（累积+派发） | 957.0 ms | 554.1 ms | −42.1% | 0.014 / 0.035 ms |
    | `finish_streaming`（收尾） | 101.6 ms | 51.1 ms | −49.7% | — |

    派发分支命中（单轮）：clean=14（自然边界立即渲染）、soft=8（句号 100ms 合并窗）、
    adaptive=778（无边界走 150-300-500ms 自适应拍）。

    T5 优化结论：
    1. **瓶颈在渲染侧 `_reconcile`**，优化前占已测量耗时 85.5%；其 87% 又是
       `parse_blocks`，而 `parse_blocks` 的 94% 是 `_md_to_html`
       （python-markdown 全量转换）——整链路 ~70% 集中在一次纯函数调用上。
    2. `append_chunk` 入口从来不是瓶颈（p50 13µs）。
    3. 生效的优化是 `_md_to_html_incremental`（见 markdown_block_viewer）：
       按最后一个空行边界切成「已闭合前缀（按文本寻址缓存）+ 变动尾部
       （单独转换）」，单次转换量从全量降到尾部几十字符。
    4. 被否决的方向（实测不可行，勿重复尝试）：
       - 逐块转换 + 块间补 `\n`：markdown 序列化在顶层块间恒插 `\n`，且会把
         空行分隔的相邻列表 / 相邻 blockquote 合并，600 组组合下 164 处不一致。
       - 同 `_md` 去重跳过 reconcile：相邻重复仅 1/800，收益 ~1%。
       - 文本级 LRU 缓存（key=输入文本）：流式输入每 chunk 必变，命中率 0%。
    5. 等价性由 `tests/widgets/test_markdown_incremental_html_equivalence.py`
       固化（含 600 组组合 + 缓存透明性 + 流式中间态）。

    ⚠️ 基线随机器与负载漂移 ±20%，跨机对比无意义；只在同一台机器做前后对比。
"""

import os
import statistics
import time

# 必须早于 QApplication 与任何 app.* 导入（pytest 收集期即执行）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRIFOX_QT_RENDERER"] = "1"
os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", ".drifox-test"))

import pytest  # noqa: E402

# Qt 属性必须在 QApplication 创建前设置（与 main.py / 既有 perf 基准对齐）
from PyQt5.QtCore import QCoreApplication, Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

pytestmark = pytest.mark.perf

ROUNDS = 3  # 基线重复轮数
CHUNK_CHARS = 40  # 与 test_stream_render_bench.py 一致
FEED_INTERVAL_MS = 8  # 与 test_stream_render_bench.py 一致
TOTAL_CHARS = 32000  # 与 test_stream_render_bench.py 一致

# 与 test_stream_render_bench.py 同款混合 markdown 模板（段落软边界 + 列表 +
# 成对代码块 fence），保证跨文件可比。
_MD_TEMPLATE = (
    "这是一段用于模拟大语言模型流式输出的中文正文，句子以句号分隔，"
    "段落之间留有空行以触发自然边界全量渲染。"
    "流式渲染管线会在软边界处合并派发渲染任务，在自然边界处立即刷新。\n\n"
    "- 列表项一：块级 reconcile 只重建尾部变化块，key 指纹命中直接复用\n"
    "- 列表项二：增量注入经 80ms 节流，避免每 chunk 都打穿渲染层\n"
    "- 列表项三：finish 收尾再做一次全量渲染保证最终态一致\n\n"
    "```python\n"
    "def bench_point(cards: list[str]) -> int:\n"
    "    return sum(len(c) for c in cards)\n"
    "```\n\n"
)


def _build_payload() -> str:
    """循环填充模板到 TOTAL_CHARS（截断到 chunk 对齐边界）。"""
    unit = _MD_TEMPLATE
    n, text = divmod(TOTAL_CHARS, len(unit))
    return unit * n + unit[: text - text % CHUNK_CHARS]


def _pct(vals: list, q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def _stats(vals: list) -> tuple:
    """返回 (n, total, p50, p95, max)，统一口径供报告复用。"""
    return (
        len(vals),
        round(sum(vals), 2),
        round(_pct(vals, 0.50), 3),
        round(_pct(vals, 0.95), 3),
        round(max(vals), 3) if vals else 0.0,
    )


def _run_one_round(round_idx: int, payload: str) -> dict:
    """单轮压测：新 MarkdownBlockViewer + 模拟定时投喂，返回阶段指标 dict。"""
    from app.widgets.markdown_block_viewer import MarkdownBlockViewer

    viewer = MarkdownBlockViewer()
    viewer.resize(900, 800)
    viewer.show()
    for _ in range(20):
        QTest.qWait(10)
        if viewer.isVisible():
            break

    # ── 插桩计时（环绕原实现 / patch 模块级函数，不改业务源码）───────────
    append_cost: list[float] = []
    render_cost: list[float] = []
    parse_cost: list[float] = []
    align_cost: list[float] = []
    dispatch_kinds: list[str] = []

    _orig_append_chunk = viewer.append_chunk
    _orig_schedule_render = viewer._schedule_render
    _orig_reconcile = viewer._reconcile

    def timed_append_chunk(text):
        t0 = time.perf_counter()
        _orig_append_chunk(text)
        append_cost.append((time.perf_counter() - t0) * 1000)

    def timed_schedule_render(immediate=False):
        # 只记录派发分支归属（不吞原逻辑）；真正渲染成本由 _reconcile 承担
        md = viewer._md or ""
        if immediate:
            kind = "immediate"
        elif not viewer._streaming:
            kind = "idle"
        elif viewer._has_reached_clean_boundary(md):
            kind = "clean"
        elif viewer._has_reached_soft_boundary(md):
            kind = "soft"
        else:
            kind = "adaptive"
        dispatch_kinds.append(kind)
        return _orig_schedule_render(immediate)

    def timed_reconcile(force=False):
        # 渲染阶段（一次全量/差量刷新）落地在 _reconcile。
        # ⚠️ 只环绕调用 _orig_reconcile：复刻其逻辑会绕过被测的缓存路径，
        # 使优化效果无法体现在数字上（口径失真）。
        t0 = time.perf_counter()
        _orig_reconcile(force)
        render_cost.append((time.perf_counter() - t0) * 1000)

    viewer.append_chunk = timed_append_chunk
    viewer._schedule_render = timed_schedule_render
    viewer._reconcile = timed_reconcile

    # parse / 差量对齐内部分解：patch 模块级函数，_reconcile 内部经全局名调用
    import app.widgets.markdown_block_viewer as mbv

    _orig_parse = mbv.parse_blocks
    _orig_align = mbv.reconcile_widgets

    def timed_parse(md_text):
        t0 = time.perf_counter()
        r = _orig_parse(md_text)
        parse_cost.append((time.perf_counter() - t0) * 1000)
        return r

    def timed_align(*args, **kwargs):
        t0 = time.perf_counter()
        r = _orig_align(*args, **kwargs)
        align_cost.append((time.perf_counter() - t0) * 1000)
        return r

    mbv.parse_blocks = timed_parse
    mbv.reconcile_widgets = timed_align

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
        viewer.append_chunk(piece)

    feed_timer.timeout.connect(feed)
    t_start = time.perf_counter()
    feed_timer.start()
    while sent < total:
        QTest.qWait(20)
    feed_elapsed = (time.perf_counter() - t_start) * 1000

    QTest.qWait(300)  # 冲刷 100ms 软边界合并窗 / 自适应派发拍

    t_finish = time.perf_counter()
    viewer.finish_streaming()
    finish_ms = (time.perf_counter() - t_finish) * 1000
    QTest.qWait(100)
    total_elapsed = (time.perf_counter() - t_start) * 1000

    feed_timer.stop()
    viewer.append_chunk = _orig_append_chunk
    viewer._schedule_render = _orig_schedule_render
    viewer._reconcile = _orig_reconcile
    mbv.parse_blocks = _orig_parse
    mbv.reconcile_widgets = _orig_align
    viewer.cleanup()
    viewer.deleteLater()
    QTest.qWait(100)

    assert sent == total, f"round{round_idx}: 字符未投递完 {sent}/{total}"
    assert append_cost, f"round{round_idx}: append_chunk 未被调用"
    assert render_cost, f"round{round_idx}: 渲染一次都没触发（派发链断了？）"
    assert parse_cost and align_cost, f"round{round_idx}: reconcile 内部分段未采样到"

    metrics = {
        "round": round_idx,
        "total_chars": total,
        "chunks": (total + CHUNK_CHARS - 1) // CHUNK_CHARS,
        "feed_elapsed_ms": round(feed_elapsed, 1),
        "total_elapsed_ms": round(total_elapsed, 1),
        "finish_ms": round(finish_ms, 1),
        "append": _stats(append_cost),
        "render": _stats(render_cost),
        "parse": _stats(parse_cost),
        "align": _stats(align_cost),
        "dispatch_clean": dispatch_kinds.count("clean"),
        "dispatch_soft": dispatch_kinds.count("soft"),
        "dispatch_adaptive": dispatch_kinds.count("adaptive"),
        "dispatch_immediate": dispatch_kinds.count("immediate"),
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
        f"  append_chunk   n={m['append'][0]:<4} total={m['append'][1]:<9}ms"
        f" p50={m['append'][2]} p95={m['append'][3]} max={m['append'][4]}ms",
        flush=True,
    )
    print(
        f"  _reconcile(渲染) n={m['render'][0]:<4} total={m['render'][1]:<9}ms"
        f" p50={m['render'][2]} p95={m['render'][3]} max={m['render'][4]}ms",
        flush=True,
    )
    print(
        f"    └ parse_blocks   n={m['parse'][0]:<4} total={m['parse'][1]:<9}ms"
        f" p50={m['parse'][2]} p95={m['parse'][3]} max={m['parse'][4]}ms",
        flush=True,
    )
    print(
        f"    └ reconcile_diff n={m['align'][0]:<4} total={m['align'][1]:<9}ms"
        f" p50={m['align'][2]} p95={m['align'][3]} max={m['align'][4]}ms",
        flush=True,
    )
    print(
        f"  dispatch 分支命中：clean={m['dispatch_clean']} soft={m['dispatch_soft']}"
        f" adaptive={m['dispatch_adaptive']} immediate={m['dispatch_immediate']}",
        flush=True,
    )


def test_bench_stream_stage_baseline(qapp):
    """流式 viewer 层阶段基线：1 轮预热 + 3 轮正式采样，打印指标供基线采集。"""
    payload = _build_payload()
    assert len(payload) >= 30000, f"压测文本不足 3 万字符：{len(payload)}"

    # 预热轮：加载 pygments/字体/类初始化等一次性成本，不计入基线
    _run_one_round(0, payload[: 5 * CHUNK_CHARS])
    QTest.qWait(200)

    results = []
    for i in range(1, ROUNDS + 1):
        m = _run_one_round(i, payload)
        _print_metrics(m)
        results.append(m)

    # 稳定性：3 轮 total_elapsed 离散度应可控（<2x），异常尖刺说明环境噪声大
    totals = [m["total_elapsed_ms"] for m in results]
    spread = max(totals) / max(1.0, min(totals))
    print(
        f"\n=== 阶段基线汇总：total_elapsed 3 轮 = {totals} ms，max/min 离散度 = {spread:.2f}x ===",
        flush=True,
    )

    # 阶段占比（3 轮累计口径）：回答「哪一阶段最贵」
    stage_total = {
        "_reconcile(渲染)": sum(m["render"][1] for m in results),
        "  └ parse_blocks": sum(m["parse"][1] for m in results),
        "  └ 差量对齐": sum(m["align"][1] for m in results),
        "append_chunk(累积+派发)": sum(m["append"][1] for m in results),
        "finish_streaming(收尾)": sum(m["finish_ms"] for m in results),
    }
    measured = stage_total["_reconcile(渲染)"] + stage_total["append_chunk(累积+派发)"] + stage_total["finish_streaming(收尾)"]
    print("=== 阶段耗时占比（3 轮累计 / 已测量耗时）===", flush=True)
    for k, v in sorted(stage_total.items(), key=lambda kv: -kv[1]):
        share = (v / measured * 100) if measured > 0 else 0.0
        print(f"  {k:<22} {v:>10.2f} ms   {share:>5.1f}%", flush=True)
    print(
        f"  已测量合计              {measured:>10.2f} ms"
        f"   （墙钟 total_elapsed 之和 {sum(totals):.1f} ms，差额为 chunk 间隔等待与定时器冲刷）",
        flush=True,
    )

    assert spread < 2.0, f"3 轮基线离散度过大（{spread:.2f}x），环境噪声影响采样可信度"

    # 稳定参考口径：取 3 轮中位数
    med = {k: statistics.median(m[k] for m in results) for k in results[0]}
    _print_metrics(med)
