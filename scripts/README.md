# scripts/ — 性能观测工具链

供 perf-tester / perf-analyzer 复用的性能观测脚本。全部为独立新建文件，**不修改业务代码与测试**。

## 脚本一览

| 脚本 | 用途 | 关键依赖 |
|---|---|---|
| `mem_track.py` | 内存跟踪：tracemalloc + 进程 RSS 周期采样 → 内存曲线 JSON | psutil（项目已有）+ stdlib tracemalloc |
| `tab_cycle_bench.py` | 开/关 Tab 循环压测：参数化 N 与轮数，输出每轮内存增量 → 泄露检测 | psutil + PyQt5（可选，headless offscreen） |
| `action_timer.py` | 通用耗时测量：装饰器 / 上下文管理器，插桩测量 tab 打开各阶段耗时 | 纯 stdlib |
| `perf_regression.py` | **性能回归对比（T9-pre 固化）**：真实 GUI 一键测量开 tab 耗时 / 内存曲线 / 用量请求计数，输出「优化前/后」对比表 | psutil + PyQt5（真实桌面会话，非 headless） |

## 快速开始

```bash
# 内存曲线（自测演示，无需 GUI）
uv run python scripts/mem_track.py --self-test --interval 0.05 --duration 3

# 监控外部进程
uv run python scripts/mem_track.py --pid 1234 --interval 0.5 --duration 30 -o mem.json

# Tab 循环压测（内置 QTabWidget 仿真）
uv run python scripts/tab_cycle_bench.py --n-tabs 10 --rounds 5 -o bench.json

# 耗时统计自测
uv run python scripts/action_timer.py

# 性能回归对比（真实 GUI，2 轮 × 8 tab，自动隔离 .drifox 数据目录）
uv run python scripts/perf_regression.py

# 指定轮数/tab 数/输出路径
uv run python scripts/perf_regression.py --tabs 8 --rounds 3 -o perf_result.json

# 与优化前基线对比（--baseline 指向基线 JSON，输出对比表）
uv run python scripts/perf_regression.py --baseline perf_baseline.json
```

## 典型组合用法（插桩项目代码）

在项目内写驱动脚本，将三个工具组合观测 tab 打开/关闭：

```python
from scripts.mem_track import MemoryTracker
from scripts.action_timer import timer, dump_timings
from scripts.tab_cycle_bench import TabCycleBench

tracker = MemoryTracker(interval=0.05)
tracker.start()
with timer("tab.open.total"):
    panel.add_tab("新会话")
tracker.stop()
tracker.save("mem.json")
dump_timings("timings.json")

# 循环压测：注入项目真实 TabPanel 回调
bench = TabCycleBench(
    open_tab=lambda i: panel.add_tab(f"tab-{i}"),
    close_tab=lambda i: panel.remove_tab(panel.count() - 1),
    n_tabs=10,
    rounds=5,
)
report = bench.run()
```

## 输出格式说明

- `mem_track.py` → `{"meta": {...}, "samples": [{"t", "rss_mb", "traced_mb", "peak_mb"}]}`
- `tab_cycle_bench.py` → `{"meta", "rounds": [{"round", "open_s", "close_s", "delta_close_mb", ...}], "leak_check": {"verdict": "OK|LEAK", "growth_mb"}}`
- `action_timer.py` → `{"阶段名": {"calls", "total_s", "avg_s", "min_s", "max_s"}}`
- `perf_regression.py` → `{"meta", "rounds": [每轮完整数据], "summary": {startup_s, open_tab{avg/median/min/max}, memory{base/open/close/delta/leak_verdict}, ui, usage_fetch_total}}`

## perf_regression.py 说明（真实 GUI 回归）

- **测量语义**：每轮 = 独立子进程 + 干净 `.drifox`（自动备份/恢复），避免泄漏对象跨轮污染；流程为 启动 → 基线 RSS → 开 N tab（计时）→ 峰值 → 逐个关闭 → 强制 GC → 关闭后 RSS → UI 响应 → 用量请求计数。
- **LEAK 判定**：关闭后 RSS - 打开峰值 > 5MB 判定泄漏不回落（阈值可在 `LEAK_THRESHOLD_MB` 调整）。
- **注意事项**：
  - 需真实桌面会话（`QT_QPA_PLATFORM=windows`）；RDP/无 GPU 环境多窗口 WebEngine 可能受限。
  - 驱动自动 `init_shared_web_profile()`（缺省会 qFatal 崩溃）；默认 patch `_compact_process_heap_after_cleanup` 规避 HeapCompact access violation（`--keep-heapcompact` 关闭）。
  - `--no-isolate` 会污染用户数据目录，仅自检演示用。
  - 用量请求计数为运行时 patch `fetch_async`（不落盘不改代码）。

## 环境说明

- psutil 为 pyproject 核心依赖（无需新增）；pympler / py-spy **未安装**，脚本不依赖它们（`mem_track.py --objects` 需 pympler，未装则跳过）。
- Windows 无 `resource` 模块，RSS 统一走 psutil。
- 如需新增依赖（如 pympler、py-spy），须先汇报 Leader 决策，不得擅改 pyproject.toml。

## 检查

```bash
ruff check scripts/
```
