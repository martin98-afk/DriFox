# tests/perf/long_run — 长时间运行性能基线测试

本目录是 **`tests/perf/` 的运行时子目录**，专做"长时压测 + 内存曲线量化"。

## 与 `tests/perf/*.py`（静态分析）的区别

| 维度 | `tests/perf/test_*.py` | `tests/perf/long_run/` |
|---|---|---|
| 性质 | 静态源码分析（pathlib + re 匹配） | **运行时压测**（真实 import + 操作循环） |
| 耗时 | <1s/用例 | demo 30s/场景，full 30min/场景 |
| 输出 | pytest assert 通过/失败 | CSV + JSON + HTML 图表 + Markdown 报告 |
| 触发 | `pytest tests/perf/` 默认包含 | `pytest -m perf_long` |

## 三个压测场景

| 场景 | 文件 | 压测目标 | 循环量级 |
|---|---|---|---|
| A | `scenario_a_message_stream.py` | SessionManager + mock stream_chunk 信号链 | ≥3k 次（demo）/ ≥50k 次（full） |
| B | `scenario_b_session_switch.py` | SessionManager.create/switch/delete + _evict_if_needed | ≥3k 次（demo）/ ≥50k 次（full） |
| C | `scenario_c_plugin_hot_reload.py` | ChatBackend._on_hot_reload_requested → reload_plugin_subsystems | ≥1k 次（demo）/ ≥50k 次（full） |

## 运行方式

```bash
# Demo（每场景 30s，适合本地/CI）
cd D:/work/DriFox && python -m tests.perf.long_run.runner

# Full（每场景 30min，总 90min；需 LONGRUN_FULL=1）
LONGRUN_FULL=1 python -m tests.perf.long_run.runner

# 自定义时长
LONGRUN_DURATION=120 python -m tests.perf.long_run.runner

# 单场景
python -m tests.perf.long_run.runner --scenario a

# pytest 入口（marker: perf_long）
pytest tests/perf/long_run/test_long_run_scenarios.py -v -m perf_long

# 自定义采样间隔（默认 60s）
PERF_SAMPLE_INTERVAL=10 python -m tests.perf.long_run.runner
```

## 输出物

| 文件 | 路径 | 用途 |
|---|---|---|
| CSV | `tests/perf/long_run/baselines/long_run_<scenario>_<ts>.csv` | 时序数据（RSS/QObject/tracemalloc） |
| JSON | `tests/perf/long_run/baselines/long_run_<scenario>_<ts>.json` | 完整 summary（含每个 sample 详情） |
| HTML | `tests/perf/long_run/baselines/long_run_chart_<ts>.html` | ECharts 内存曲线（浏览器可看） |
| Markdown | `docs/perf/long_run_baseline.md` | 报告（基线数字 + 增长率 + 泄漏判定） |

## 内存增长判定

| RSS 速率（MB/h） | 等级 | 含义 |
|---|---|---|
| < 5 | `stable` | 稳定（demo 模式常见 GC 抖动） |
| 5-50 | `watch` | 弱增长；建议长时（≥8h）二次采样 |
| ≥50 | `suspect_leak` | ⚠️ 可疑泄漏；需结合 perf-analyzer 静态分析定位 |

## 依赖

- `psutil`（RSS 读取；无则降级到 ctypes GetProcessMemoryInfo）
- `tracemalloc`（标准库）
- PyQt5（必需；conftest.py 自动建 QApplication）
- 无 matplotlib/plotly 依赖（HTML 图表走 ECharts CDN）

## 注意事项

1. **不动生产代码**：本目录只读取 `app/`，不修改。
2. **场景 C 路径**：monkeypatch `PluginManager.get_instance` 返回 mock，避免真实磁盘/插件加载。
3. **PyQt5 单例**：QApplication 只能建一次；qapp fixture scope=module，多场景共享。
4. **CI 友好**：pytest 默认跑 demo（每场景 ≤10s）；`LONGRUN_FULL=1` 切换全量。