# scripts/measure_perf.py — 性能回归测量骨架

轻量回归测量工具（子任务 #4.3）。测量 `startup / animation / upload / memory` 四类场景，
输出 **JSON 结果文件**（便于 diff），并可与基线 `reports/perf-baseline-before.json` 对比，
超阈值项在控制台 **红色标记**。

## 铁律遵守
- **不修改任何产品代码**：仅测量（可 import 产品模块，不改动）。
- **不引入新三方依赖**：仅标准库 + 已在依赖中的 `psutil` / `PyQt5` / `requests`。
- **可在 dev 分支无产品代码改动下跑通**：全部测量 headless 安全，无需显示器 / 外网。

## 用法

```bash
# 内存场景，3 次迭代（默认），输出 reports/perf-measure-memory.json
uv run python scripts/measure_perf.py --scenario memory

# 启动场景，5 次迭代
uv run python scripts/measure_perf.py --scenario startup --iterations 5

# 与基线对比（超 10% 标红，默认阈值）
uv run python scripts/measure_perf.py --scenario memory --baseline reports/perf-baseline-before.json

# 自定义阈值（超 20% 标红）与输出路径
uv run python scripts/measure_perf.py --scenario upload --threshold 0.20 --output reports/mine.json
```

参数：
| 参数 | 默认 | 说明 |
|---|---|---|
| `--scenario` | 必填 | `startup` / `animation` / `upload` / `memory` |
| `--iterations` | `3` | 迭代次数 |
| `--output` | `reports/perf-measure-{scenario}.json` | 结果路径，支持 `{scenario}` 占位 |
| `--baseline` | `reports/perf-baseline-before.json` | 基线 JSON（缺失则跳过对比） |
| `--threshold` | `0.10` | 回归阈值：相对基线的百分比，默认 0.10（即超过 10 个百分点标红） |

## 输出 JSON 结构

```json
{
  "scenario": "memory",
  "iterations": 3,
  "metrics": {
    "tracemalloc_peak_mb": {"min": 1.2, "median": 1.3, "mean": 1.3, "max": 1.4},
    "rss_delta_mb":       {"min": 0.1, "median": 0.2, "mean": 0.2, "max": 0.3}
  },
  "method": "tracemalloc + psutil：...",
  "note":   "代理负载：...",
  "baseline_file": null,
  "threshold": 0.10,
  "regressions": [],          // 非空则为回归项（metric/baseline/current/ratio）
  "pass": true,
  "_env": { "python": "3.14.2", "platform": "...", "timestamp": "..." }
}
```

## 基线对比
- 基线文件支持两种格式：单场景结果（含 `metrics`）或多场景聚合（`{scenario: {metrics}}`）。
- 对比以 `median` 为准（缺则 `mean`）；`current > baseline * (1 + threshold)` 判定为回归。
- 回归项在控制台以 ANSI 红色输出，并写入 `regressions` 数组 + `pass=false`。

## ⚠️ 当前测量语义（重要）
本骨架的 scenario 测量器为 **代表性代理负载**，用于打通「测量 → JSON → 基线对比」链路，
**不代表真实产品端到端指标**：
- `memory`   ：tracemalloc + psutil 测 2000 条卡片级 dict 构造的净分配
- `startup`  ：子进程冷导入 `openai`（导入耗时 Top1 包，占 34%）
- `animation`：PyQt5.QColor 渐变插值（值类型，无需 QApplication）
- `upload`   ：本地 loopback HTTP POST 64KB 往返

待 #1/#2/#3 诊断报告产出后，由 build 在本骨架内将代理测量器替换为真实产品负载
（驱动真实 GUI / 真实上传线程 / 真实渲染管线），框架与 baseline 对比逻辑可直接复用。

## 与现有基础设施的关系
- 真实 GUI 全链路测量请用 `scripts/perf_regression.py`（需桌面会话）。
- 导入/启动/会话/管线深度基准请用 `benchmarks/bench_*.py`（`uv run python benchmarks/...`）。
- 本脚本定位为 **轻量、可 diff、可 CI 门禁化的回归探针对**，与上面的重型基准互补。
