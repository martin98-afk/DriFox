# -*- coding: utf-8 -*-
"""
并行运行时诊断 — 验证项目里的 ThreadPoolExecutor 并行模式是否真并行

跑两轮：
  Round 1: 裸 ThreadPoolExecutor，3 个 2s 阻塞任务
  Round 2: 复刻 _execute_tools_parallel 的 as_completed 模式
  Round 3: 反例（串行）作为对照

判定：
  - 串行 3×2s ≈ 6s
  - 真并行 3×2s ≈ 2s
"""
import concurrent.futures
import sys
import time
import threading


def heavy_io(name: str, seconds: float = 2.0):
    """模拟一个耗时的 IO 工具（类似 bash / webfetch / read_file）"""
    start = time.perf_counter()
    tid = threading.get_ident()
    time.sleep(seconds)
    elapsed = time.perf_counter() - start
    return {
        "name": name,
        "thread_id": tid,
        "started_at": round(start, 4),
        "duration_s": round(elapsed, 4),
    }


def round1_basic_pool():
    print("\n" + "=" * 60)
    print("Round 1: 裸 ThreadPoolExecutor — 3 个 sleep(2)")
    print("=" * 60)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(heavy_io, f"task_{i}", 2.0) for i in range(3)]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]
    total = time.perf_counter() - t0
    print(f"  总耗时: {total:.2f}s   (串行预期 6s, 并行预期 2s)")
    for r in results:
        print(f"  - {r['name']}  thread_id={r['thread_id']}  dur={r['duration_s']}s")
    tids = {r["thread_id"] for r in results}
    print(f"  唯一线程数: {len(tids)}   "
          f"{'✅ 真并行' if total < 3.5 and len(tids) >= 2 else '❌ 串行'}")
    return total, len(tids)


def round2_mimic_chat_worker(tool_count: int = 3, sleep_s: float = 2.0):
    """复刻 chat_worker._execute_tools_parallel 的两阶段结构"""
    print("\n" + "=" * 60)
    print(f"Round 2: 复刻 _execute_tools_parallel — {tool_count} 工具 × {sleep_s}s")
    print("=" * 60)

    # === Phase 1: 预处理（串行） ===
    t0 = time.perf_counter()
    tool_calls = [{"name": f"bash_{i}", "args": {"command": f"echo {i}"}} for i in range(tool_count)]
    pre_elapsed = time.perf_counter() - t0
    print(f"  Phase1 预处理（参数校验）: {pre_elapsed*1000:.2f}ms")

    # === Phase 2: 并行执行 ===
    t1 = time.perf_counter()
    parallel_results = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(tool_calls), 8)
    ) as executor:
        future_map = {}
        for idx, tc in enumerate(tool_calls):
            # 模拟 _execute_one_tool_parallel
            future = executor.submit(
                heavy_io, tc["name"], sleep_s
            )
            future_map[future] = idx

        for future in concurrent.futures.as_completed(future_map):
            original_idx = future_map[future]
            try:
                result = future.result()
                if result is not None:
                    parallel_results.append((original_idx, result))
            except Exception as e:
                print(f"  [ERROR] {e}")

    total = time.perf_counter() - t0
    parallel_elapsed = time.perf_counter() - t1
    print(f"  Phase2 并行执行: {parallel_elapsed:.2f}s")
    print(f"  总耗时: {total:.2f}s   (串行预期 6s, 并行预期 2s)")

    parallel_results.sort(key=lambda x: x[0])
    for idx, r in parallel_results:
        print(f"  - [{idx}] {r['name']}  thread_id={r['thread_id']}  dur={r['duration_s']}s")
    tids = {r["thread_id"] for _, r in parallel_results}
    print(f"  唯一线程数: {len(tids)}   "
          f"{'✅ 真并行' if parallel_elapsed < sleep_s * tool_count * 0.7 and len(tids) >= 2 else '❌ 串行'}")
    return parallel_elapsed, len(tids)


def round3_serial_baseline():
    """串行反例，用于和上面两轮对比"""
    print("\n" + "=" * 60)
    print("Round 3: 串行反例（基线）— 3 个 sleep(2) 顺序执行")
    print("=" * 60)
    t0 = time.perf_counter()
    for i in range(3):
        heavy_io(f"serial_{i}", 2.0)
    total = time.perf_counter() - t0
    print(f"  总耗时: {total:.2f}s   （这就是串行该有的样子）")
    return total


def main():
    print(f"Python: {sys.version}")
    print(f"GIL 状态: {'关闭' if hasattr(sys, '_is_gil_enabled') and not sys._is_gil_enabled() else '开启（GIL 默认）'}")
    print(f"建议: GIL 开启下 IO 密集型任务仍可并行（线程释放 GIL）")
    print(f"      但纯 CPU 密集型会被 GIL 串行化\n")

    t1, tid1 = round1_basic_pool()
    t2, tid2 = round2_mimic_chat_worker()
    t3 = round3_serial_baseline()

    print("\n" + "=" * 60)
    print("综合判定")
    print("=" * 60)
    speedup = t3 / t2 if t2 > 0 else 0
    print(f"  Round1 加速比: {t3 / t1:.2f}x")
    print(f"  Round2 加速比: {speedup:.2f}x")
    if speedup >= 2.5:
        print(f"  ✅ 结论: 项目里的 ThreadPoolExecutor 模式在本环境能真并行（加速 ~{speedup:.1f}x）")
    elif speedup >= 1.5:
        print(f"  ⚠️ 结论: 部分并行（加速 ~{speedup:.1f}x），可能受 GIL 或 IO 模型限制")
    else:
        print(f"  ❌ 结论: 几乎串行（加速 ~{speedup:.1f}x），并行未生效")


if __name__ == "__main__":
    main()
