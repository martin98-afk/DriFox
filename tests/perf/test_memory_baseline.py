# -*- coding: utf-8 -*-
"""内存 + 卡顿回归基准（修复前基线采集，供 #1 内存 / #2 卡顿修复对照）。

目的：给缺陷狩猎团提供一把「可重复、可比」的尺子。修复前后各跑一次，
对比 docstring 底部「基线表」里的四个数字即可判断修复是否有效。

设计原则（与 tests/perf 既有基准一致）：
  - **数据层优先**：全程不创建 QApplication、不实例化任何 QWidget。
    ChatSession 是纯数据对象、SessionManager 是 QObject 子类但构造不需要
    QApplication（已实测），故场景 1-4 全部可无头运行。
  - **不修改任何业务代码**：只读调用 app/ 的公开路径，不 patch、不 monkeypatch。
  - **不新增三方依赖**：RSS 用已有的 psutil 依赖，Python 侧峰值用标准库
    tracemalloc。
  - **不断言性能阈值**：机器不同绝对值会漂移，只断言「链路可用 + 数字采集到」，
    阈值判断留给人工对照基线表。

场景
  1. 单会话稳态：1 个会话灌入 MSG_ROUNDS×3 条消息（含长文本 + 大工具结果），
     记录 RSS 增量与 tracemalloc 峰值。
  2. 多会话线性度：逐个创建 MULTI_SESSION_K 个会话并同样灌满，记录每会话
     RSS 增量曲线，输出「每会话平均增量」——这是验证「1GB/会话」类问题的
     关键指标（单位：MB/会话）。
  3. 对话轮次增量：同一会话内分 TURN_ROUNDS 步追加，记录每步 RSS 增量，
     检测泄漏式线性增长（单位：MB/步，每步 TURN_ROUNDS_PER_STEP 轮对话）。
  4. 上下文投影耗时：ContextPipeline.project_for_send / project_for_ui 的
     单次耗时分布（p50/max，ms）——为 #2 卡顿修复留口子。
     前置：需先扫描加载 system-context 插件的 tier 链；链为空时该场景自动跳过
     （xfail 不报错），打印 `[MEM][WARN]` 提示。

运行：
    cd <仓库根> && uv run pytest tests/perf/test_memory_baseline.py -v -s
    （tests/perf 被 addopts 默认排除，必须显式指定本文件路径；-s 才能看到
      [MEM] 打印，否则 print 被 pytest 捕获）

基线（修复前）
    采集环境：Windows / Python 3.14 / dev 分支 caa2137d
    采集命令：uv run pytest tests/perf/test_memory_baseline.py -v -s

    | 指标 | 修复前基线 | 修复后 | 说明 |
    |---|---|---|---|
    | 单会话稳态 RSS 增量 | 7.9 MB | — | 900 条消息（300 轮 × 3 条），含 300 条 20KB 工具结果 |
    | 单会话 tracemalloc 峰值 | 6.9 MB | — | 同上，Python 堆口径（持有 6.5 MB） |
    | 每会话平均增量 | 5.1 MB/会话 | — | 5 会话线性度，判「1GB/会话」 |
    | 每轮增量（稳态段） | 0.46 MB/步 | — | 每步 20 轮对话，1800 条消息总增 14.0 MB |
    | 上下文投影 send p50 / max | 0.49 / 39.3 ms | — | 180 条消息；max 为首次冷启动（编码器/链加载） |
    | 上下文投影 ui p50 / max | 0.33 / 0.37 ms | — | 同上 |

    初判（供 #1 参考）：数据层口径下「1GB/会话」不成立 —— 单会话 900 条消息
    仅占 7.9 MB，每会话增量 5.1 MB，且无泄漏式线性增长（稳态 0.46 MB/步与
    消息体积线性对应）。若用户实测确为 GB 级，增长点大概率不在 ChatSession
    消息体，而在 UI 层（MessageCard / WebEngineView 实例）或持久化层缓存。

    ⚠️ 基线数字随机器与负载漂移 ±20%，跨机对比无意义；只在同一台机器上做
    修复前/后对比。
"""

import gc
import statistics
import time
import tracemalloc

import psutil
import pytest

# 模块级：早于任何 app.* 导入，把测试数据目录隔离到仓库内 .drifox-test/
# （tests/conftest.py 已 setdefault，这里再兜一次，单跑本文件时也生效）
import os
from pathlib import Path

os.environ.setdefault("DRIFOX_DATA_DIR", str(Path(__file__).resolve().parents[2] / ".drifox-test"))

pytestmark = pytest.mark.perf

# ── 场景参数（调大即加重负载；保持默认才能与基线表对比）────────────────
MSG_ROUNDS = 300  # 单会话灌入轮数（每轮 3 条：user + tool + assistant）
TOOL_RESULT_CHARS = 20000  # 单条工具结果字符数（模拟 read_file 读大文件）
USER_CHARS = 200  # 单条用户消息字符数
ASSISTANT_CHARS = 400  # 单条助手消息字符数
MULTI_SESSION_K = 5  # 多会话线性度的会话数
TURN_ROUNDS = 30  # 轮次增量场景的采样步数
TURN_ROUNDS_PER_STEP = 20  # 每步追加的对话轮数
CTX_SAMPLES = 5  # 上下文投影耗时采样次数

_PROC = psutil.Process()


# ── 工具函数 ──────────────────────────────────────────────────────────
def _rss_mb() -> float:
    """当前进程 RSS（MB）。"""
    return _PROC.memory_info().rss / 1048576


def _mb(n_bytes: float) -> float:
    return n_bytes / 1048576


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def _make_tool_block(round_idx: int) -> dict:
    """构造一条大工具结果消息（走真实 make_tool_result_block 链路）。"""
    from app.core.conversation.message_content import make_tool_result_block

    return {
        "role": "tool",
        "content": [
            make_tool_result_block(
                "read_file",
                {"path": f"src/module_{round_idx % 20}.py"},
                "Y" * TOOL_RESULT_CHARS,
            )
        ],
        "ts_ms": round_idx,
    }


def _fill_session(session, rounds: int) -> int:
    """往会话里灌 rounds 轮（user + tool + assistant），返回消息总条数。"""
    for i in range(rounds):
        session.add_user_message("用户提问 " + "问" * USER_CHARS)
        session.messages.append(_make_tool_block(i))
        session.add_assistant_message("助手回答 " + "答" * ASSISTANT_CHARS)
    return len(session.messages)


def _ensure_context_chain() -> bool:
    """扫描加载 system-context 的 tier 链（幂等）。返回链是否非空。"""
    try:
        from app.plugins.loaders.runtime_component_loader import (
            _make_budget_resolver_loader,
            _make_context_tier_loader,
        )

        _make_context_tier_loader().scan_roots()
        _make_budget_resolver_loader().scan_roots()
        from app.core.context.pipeline import ContextPipeline

        return len(ContextPipeline()._chain("send")) > 0
    except Exception as e:  # pragma: no cover - 环境缺插件时的防御分支
        print(f"[MEM][WARN] tier 链加载失败，场景 4 跳过: {e}", flush=True)
        return False


# ── 场景 1：单会话稳态 RSS ────────────────────────────────────────────
def test_perf_single_session_steady_rss():
    """单会话灌满后的稳态内存占用（RSS 增量 + tracemalloc 峰值）。"""
    from app.core.conversation.chat_session import ChatSession

    gc.collect()
    rss0 = _rss_mb()
    tracemalloc.start()
    session = ChatSession(name="bench-single")
    n = _fill_session(session, MSG_ROUNDS)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    rss1 = _rss_mb()

    assert n == MSG_ROUNDS * 3, f"消息条数不符：{n} != {MSG_ROUNDS * 3}"
    print(
        f"[MEM] 单会话稳态 RSS={rss1 - rss0:.1f} MB, 峰值={_mb(peak):.1f} MB"
        f" | 持有={_mb(cur):.1f} MB, 消息={n} 条, 进程 RSS={rss1:.1f} MB",
        flush=True,
    )
    # 只断言链路可用与数字可采集：灌入 900 条消息后 Python 堆必然有实质增长
    assert _mb(peak) > 1.0, f"tracemalloc 峰值过小（{_mb(peak):.2f} MB），采集链路疑似失效"
    assert rss1 >= rss0, "RSS 采集异常（负值）"


# ── 场景 2：多会话线性度（判「1GB/会话」的关键指标）───────────────────
def test_perf_multi_session_linearity():
    """逐个创建 K 个会话并灌满，输出每会话平均 RSS 增量与线性度。"""
    from app.core.conversation.chat_session import ChatSession

    gc.collect()
    rss0 = _rss_mb()
    steps: list[float] = []
    prev = rss0
    alive: list = []
    for k in range(MULTI_SESSION_K):
        session = ChatSession(name=f"bench-multi-{k}")
        _fill_session(session, MSG_ROUNDS)
        alive.append(session)  # 持有引用，防止被 GC 回收导致测不到增量
        gc.collect()
        cur = _rss_mb()
        steps.append(cur - prev)
        prev = cur
        print(
            f"[MEM] 多会话 k={k + 1}/{MULTI_SESSION_K} 累计 RSS={cur - rss0:.1f} MB, 本步增量={steps[-1]:.1f} MB",
            flush=True,
        )

    total = prev - rss0
    avg = total / MULTI_SESSION_K
    print(
        f"[MEM] 多会话线性度 总增量={total:.1f} MB, 每会话平均增量={avg:.1f} MB/会话"
        f" | 单步 min={min(steps):.1f} max={max(steps):.1f} MB, 会话数={MULTI_SESSION_K}",
        flush=True,
    )
    assert len(steps) == MULTI_SESSION_K
    assert total > 0, "多会话 RSS 无增长，采集链路疑似失效"
    assert len(alive) == MULTI_SESSION_K


# ── 场景 3：对话轮次增量（泄漏式线性增长检测）────────────────────────
def test_perf_turn_incremental_rss():
    """同一会话内逐步追加，输出每步 RSS 增量（稳态段均值 + 首步冷启动）。"""
    from app.core.conversation.chat_session import ChatSession

    gc.collect()
    rss0 = _rss_mb()
    session = ChatSession(name="bench-turns")
    prev = rss0
    deltas: list[float] = []
    for step in range(TURN_ROUNDS):
        _fill_session(session, TURN_ROUNDS_PER_STEP)
        gc.collect()
        cur = _rss_mb()
        deltas.append(cur - prev)
        prev = cur

    total = prev - rss0
    # 稳态段：跳过前 1/3（解释器预热、 arena 首次扩张会放大首几步）
    warm = deltas[len(deltas) // 3 :]
    steady = statistics.mean(warm) if warm else 0.0
    print(
        f"[MEM] 轮次增量 总增量={total:.1f} MB, 每轮增量(稳态)={steady:.2f} MB/步"
        f" | 首步={deltas[0]:.2f} MB, 稳态 p50={_pct(warm, 0.5):.2f} max={max(warm):.2f} MB"
        f" | 步数={TURN_ROUNDS}, 每步={TURN_ROUNDS_PER_STEP} 轮, 消息={len(session.messages)} 条",
        flush=True,
    )
    assert len(deltas) == TURN_ROUNDS
    assert total > 0, "轮次追加无 RSS 增长，采集链路疑似失效"


# ── 场景 4：上下文 tier cascade 单次耗时（为 #2 卡顿留口子）───────────
def test_perf_context_projection_cost():
    """project_for_send / project_for_ui 单次耗时分布（p50/max，ms）。"""
    if not _ensure_context_chain():
        pytest.skip("system-context tier 链为空，跳过上下文投影耗时采集（需先加载插件）")

    from app.core.context.pipeline import ContextPipeline

    pipe = ContextPipeline()
    llm_config = {"model": "gpt-4o"}
    # 构造一份会触发 tier 的负载：长文本 + 大工具结果
    from app.core.conversation.chat_session import ChatSession

    session = ChatSession(name="bench-ctx")
    _fill_session(session, 60)
    messages = session.messages

    send_ms: list[float] = []
    ui_ms: list[float] = []
    for _ in range(CTX_SAMPLES):
        t0 = time.perf_counter()
        view_send = pipe.project_for_send(messages, llm_config)
        send_ms.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        view_ui = pipe.project_for_ui(messages, llm_config)
        ui_ms.append((time.perf_counter() - t0) * 1000)

    print(
        f"[MEM] 上下文投影 send p50={_pct(send_ms, 0.5):.2f} ms max={max(send_ms):.2f} ms"
        f" | ui p50={_pct(ui_ms, 0.5):.2f} ms max={max(ui_ms):.2f} ms"
        f" | 消息={len(messages)} 条, tier 命中={len(view_send.stats)} 层, 采样={CTX_SAMPLES} 次",
        flush=True,
    )
    assert len(send_ms) == CTX_SAMPLES
    assert view_send.messages is not None and view_ui.messages is not None
    assert max(send_ms) > 0 and max(ui_ms) > 0
