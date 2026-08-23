# 子任务 #2 — splitter 拖拽 & tab_panel 折叠/展开 baseline 性能报告

> 测试者：perf-tester@win_803 ｜ 测量日期：2026-08-22
> 被测代码：D:/work/DriFox `app/widgets/tab_panel.py` + `app/widgets/tab_manager_window.py`（含并行子任务 #1 的待定改动）
> 脚本：`tools/perf/splitter_tabpanel_baseline.py`
> 原始数据：`tools/perf/baseline_result.json`

## 1. 运行环境与复现命令

```bash
# 无显示器（CI/沙箱，默认 offscreen）——可复现、确定性
cd D:/work/DriFox
.venv\Scripts\python.exe tools/perf/splitter_tabpanel_baseline.py --tabs 30 --runs 5

# 带显示器（真实 FPS/卡顿，需在开发机运行）
.venv\Scripts\python.exe tools/perf/splitter_tabpanel_baseline.py --mode live --tabs 30 --runs 5
```

环境：Windows / Python 3.14.2 / PyQt5 5.15.2 / Qt 5.15.2。
测量方法：构造**真实 `TabPanel` + `QSplitter`**（约束与 `TabManagerWindow` 一致：
`panel.setMinimumWidth(_collapsed_min_width=46)`、`stretch(0,1)`、`handleWidth=4`），复刻 manager
的宽度动画逻辑（`setSizes` + `sync_collapsed_ui`，200ms / OutCubic），用 `QApplication.notify`
插桩记录每个事件派发耗时(ms) → 主线程阻塞与 >16.7ms 卡顿帧占比。offscreen 下 `update()` 不自动
flush，故**每帧显式 `repaint()`** 强制绘制以测得真实 paint 主线程成本。

## 2. baseline 量化数据（offscreen，30 tabs，5 次均值）

| 操作 | 纯计算墙钟 | 帧数(paint) | paint 均值 | paint 最大 | >16.7ms 卡顿帧 | jank 占比 | 最大单事件 |
|---|---|---|---|---|---|---|---|
| collapse（展开→折叠） | 115.8 ms | 5303 | 0.014 ms | 0.44 ms | 0 | 0.0% | 2.10 ms |
| expand（折叠→展开） | 122.4 ms | 5273 | 0.014 ms | 0.41 ms | 0 | 0.0% | 2.14 ms |
| splitter 拖拽（展开↔折叠 往返，2px/步） | 387.8 ms | 16842 | 0.015 ms | 0.50 ms | 0 | 0.0% | 11.0 ms* |

> *11.0ms 为单次 LayoutRequest/MetaCall 离群事件（非 paint），仍 < 16.7ms 预算。

辅助指标：
- `sync_collapsed_ui()` 单次成本：**0.70 ms**（跨阈值时调用一次，遍历所有 TabItem 切紧凑态）
- 单次 panel `repaint()` 最大耗时随 tab 数（1/10/30/50）：**0.15 / 0.16 / 0.17 / 0.15 ms**（CPU 侧几乎不随条目数增长，因 offscreen 仅绘制可见区）
- TabPanel 一次性构造成本：**~4.6–7.0 ms**（仅首建，非每帧）

## 3. 卡顿判定

对照 60FPS 单帧预算 **16.7ms**：

- **tab_panel 侧：流畅，非瓶颈。** 折叠/展开每帧 paint 仅 ~0.4–0.5ms（占预算 3%），
  0 个卡顿帧；动画 200ms 设计时长内若按 60fps  pacing，每帧 compute ~2.4ms，富余 ~14ms，
  不掉帧。**splitter 拖拽每步 ~2.7ms compute，同样远低于 16.7ms。**
- 结论：用户报告的「卡卡」**不是由 tab_panel 自身 paint/resize 引起**（offscreen CPU 侧已证明极低）。

## 4. 重要限制与下一步（给 #3 分析 / #4 落地）

1. **offscreen 仅为 CPU 侧下界**：不含真实栅格化 / GPU 合成 / vsync / 高 DPR。真实显示器下
   paint 收尾（buffer→屏幕）、窗口合成、WebEngine 视图重绘会显著放大成本。
2. **右侧 content 区未覆盖**：本 harness 的 content 是空 `QWidget`。真实 app 中 splitter 拖拽
   同时重绘**右侧聊天区（`_chat_frame`，含 `QWebEngineView`）**——这才是拖拽「卡卡」的高概率来源。
   必须在 `--mode live` 或真实环境下复测整窗。
3. 建议 `#3 perf-analyzer` 在开发机以 `--mode live` 跑本脚本，获取真实 FPS 与卡顿帧分布；
   重点排查 `_chat_frame`/WebEngine 在 resize 时的重绘开销与布局抖动。

## 5. 复现要点

- 确定性：offscreen 模式无随机性，可 CI 复跑；改 `--tabs`/`--runs` 调规模。
- 工作树含并行子任务 #1 的 `tab_panel.py` 修复性改动（自定义插件 compact 状态恢复），
  baseline 基于该工作树；若 #4 前这些改动被回退/修改，需重测。
- 本文件与 `splitter_tabpanel_baseline.py` 为 #2 交付；`tools/perf/` 下 `baseline.py` /
  `baseline_extra.py` / `results/` / `README.md` 非本任务产物，未改动。
