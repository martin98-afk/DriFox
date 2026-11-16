# 性能基线对比报告

- before: `tools\perf\results\baseline_before.json`
- after: `tools\perf\results\baseline_before.json`

| 指标 | before | after | Δ | Δ% | 判定 | 备注 |
|---|---|---|---|---|---|---|
| 主进程 rss_visible_mb | 228.84MB | 228.84MB | 0.0MB | 0.0% | ✅ OK |  |
| 主进程 rss_peak_mb | 645.8MB | 645.8MB | 0.0MB | 0.0% | ✅ OK |  |
| WebEngine web_rss_mb | 0.0MB | 0.0MB | 0.0MB | —% | ✅ OK |  |
| WebEngine 进程数 | 0个 | 0个 | 0个 | —% | ✅ OK |  |
| 单 renderer max_mb | — | — | — | —% | ✅ OK |  |
| 单 renderer median_mb | — | — | — | —% | ✅ OK |  |
| global_rendered_pages | 0页 | 0页 | 0页 | —% | ✅ OK |  |
| cpu_mean(单核%) | 111.61% | 111.61% | 0.0% | 0.0% | ✅ OK |  |
| cpu_p95(单核%) | 139.03% | 139.03% | 0.0% | 0.0% | ✅ OK |  |
| fps_mean | 1.97fps | 1.97fps | 0.0fps | 0.0% | ❌ FAIL | 低于 30fps 硬线 |
| frame_p99_ms | 1934.406ms | 1934.406ms | 0.0ms | 0.0% | ⚠️ WARN | 交互下限 30fps（p99≤33ms） |
| 启动 import_s(p95) | 0.42s | 0.42s | 0.0s | 0.0% | ✅ OK |  |
| 启动 to_visible_s(p95) | 2.7556s | 2.7556s | 0.0s | 0.0% | ✅ OK |  |
| 启动 app_init_s(p95) | 2.3356s | 2.3356s | 0.0s | 0.0% | ✅ OK |  |
| longevity rss_delta_mb | -62.86MB | -62.86MB | 0.0MB | 0.0% | ✅ OK |  |
| longevity leak_suspected | False | False | 0 | —% | ✅ OK |  |
| 回收动作计数(kill/idle/gate) | 0次 | 0次 | 0次 | —% | ✅ OK |  |

## 结论: ❌ FAIL（1 FAIL / 1 WARN）
