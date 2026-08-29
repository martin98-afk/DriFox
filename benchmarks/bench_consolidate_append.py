"""consolidate_messages 长会话追加基准（自动与 git 基线对比）。

模拟真实流式：会话不断增长，每追加一条消息就调用一次 consolidate_messages
（渲染 / round 计算 / 差异统计都会走它），并额外调用一次「长度未变」的查询
（渲染期同一帧内多处调用的典型模式）。

对比修复前后：
- 修复前：缓存 key 含 len(list) → 每次追加必然 miss → O(n²) 全表 normalize
- 修复后：增量复用 → 只 normalize 新增的 Δ 条

基线模块通过 ``git show`` 取源码后在内存中加载（不落盘、不建 worktree），
因此需要能执行 git 命令。

用法::

    python benchmarks/bench_consolidate_append.py            # 默认 1200 轮
    python benchmarks/bench_consolidate_append.py 2000       # 指定轮数
    python benchmarks/bench_consolidate_append.py --baseline v1.2.3
"""

from __future__ import annotations

import argparse
import subprocess
import time
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_module_from_source(src: str, name: str) -> types.ModuleType:
    """把源码字符串加载为独立模块（用于并排对比不同 commit 的实现）。"""
    mod = types.ModuleType(name)
    mod.__file__ = f"<benchmark:{name}>"
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)  # noqa: S102
    return mod


def _measure(consolidate, rounds: int) -> float:
    """返回累计耗时（毫秒）。"""
    messages = [{"role": "system", "content": "你是一个助手"}]
    body = "x" * 200  # 模拟真实消息体积
    started = time.perf_counter()
    for i in range(rounds):
        messages.append({"role": "user", "content": f"问题 {i} " + body})
        messages.append({"role": "assistant", "content": f"回答 {i} " + body})
        # 追加后的查询（长度变化 → 修复前必然 miss）
        consolidate(messages)
        # 同帧内长度未变的重复查询（渲染路径常见）
        consolidate(messages)
    return (time.perf_counter() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rounds", nargs="?", type=int, default=1200, help="追加轮数（每轮 2 条消息）")
    parser.add_argument("--baseline", default="HEAD", help="对比基线 commit（默认 HEAD）")
    parser.add_argument("--no-baseline", action="store_true", help="只测当前实现")
    args = parser.parse_args()

    try:
        from app.core.message_content import consolidate_messages as current
    except Exception as exc:  # pragma: no cover
        print(f"导入当前实现失败：{exc}")
        return 1

    cur_ms = _measure(current, args.rounds)
    final_n = args.rounds * 2 + 1
    print(f"轮数={args.rounds}  最终消息数={final_n}")
    print(f"当前实现   : {cur_ms:8.1f} ms   （每轮 {cur_ms / args.rounds:.3f} ms）")

    if args.no_baseline:
        return 0

    proc = subprocess.run(
        ["git", "show", f"{args.baseline}:app/core/message_content.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        print(f"读取基线失败（{args.baseline}）：{proc.stderr.strip()}")
        return 1
    try:
        base_mod = _load_module_from_source(proc.stdout, "message_content_baseline")
        base_ms = _measure(base_mod.consolidate_messages, args.rounds)
    except Exception as exc:
        print(f"基线运行失败：{exc}")
        return 1

    print(f"基线({args.baseline[:8]}): {base_ms:8.1f} ms   （每轮 {base_ms / args.rounds:.3f} ms）")
    if cur_ms > 0:
        print(f"提速       : {base_ms / cur_ms:.1f}x   （省 {base_ms - cur_ms:.1f} ms）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
