# 子任务 #5 — splitter 拖拽 / 折叠动画 卡顿优化 回归验收

> 验收者：perf-tester@win_803 ｜ 日期：2026-08-22
> 验收对象：#4（tab_manager_window.py 冻结 `_content_area` + `_splitter_idle_timer` 防抖恢复）、
>  #10（解冻点 `_on_sidebar_anim_finished` / `_on_splitter_idle` 显式 `_set_cards_resize_preview_mode(False)`）、
>  #12（message_card.py 欢迎卡补 `resize_placeholder` + 删 welcome 早返回）
> 改动文件：`app/widgets/tab_manager_window.py`(+109/-25)、`app/widgets/message_card.py`(+19/-5)
> 核查脚本：`tools/perf/splitter_tabpanel_baseline.py`（--mode live 需显示器）

## 0. 环境限制（重要）

本沙箱**无显示器**：`show()/exec_()` 事件循环会挂起；`--mode live`（强制 `platform=windows`）
不可行。offscreen 下 `QApplication` 可跑，但 `message_card` 拉入 `QWebEngineView` 等组件后
导入/构造会挂起或 abort，**无法在此跑真实 FPS/卡顿帧测量**。

→ 故 #5 采用：**静态审查 + 编译核查 + 冻结/隐藏逻辑路径审查**，并附**人工 live 验收清单**。
（与任务指引「沙箱无显示器时给替代核查结论 + 人工验收清单」一致。）

可自动化验证项均已通过：
- `py_compile` 两改动文件：**COMPILE_OK**
- `git diff --name-only`：**仅** `app/widgets/message_card.py` + `app/widgets/tab_manager_window.py`
  （`tab_panel.py` 无未提交改动，其修复已提交保留）

## 1. 静态/路径审查结论

### #4 冻结 `_content_area` + 防抖恢复（防黑屏）
- 动画冻结：`_start_sidebar_anim` 内 `if hasattr(self,"_content_area"): self._content_area.setUpdatesEnabled(False)`。
- 拖拽冻结：`_on_splitter_manually_moved` 首帧 `setUpdatesEnabled(False)` + 每帧 `self._splitter_idle_timer.start()`（120ms 防抖）。
- 恢复（三处，均 `hasattr` 守卫，无 AttributeError 风险）：
  1. `_on_sidebar_anim_finished`：**try/finally** → 无论正常结束/嵌套展开动画/异常，均 `setUpdatesEnabled(True)`；
     仅当「本回调又同步启动新动画（`_sidebar_anim.state()==Running`）」时跳过，交新动画收尾恢复 → **不会黑屏**。
  2. `_on_splitter_idle`（timer 超时）：`setUpdatesEnabled(True)` + `_tab_panel.set_resizing(False)` + 复位拖拽标记。
  3. `_on_resize_finished`：新增 `if not (self._splitter_idle_timer.isActive())` 守卫，避免与拖拽防抖 timer 双重释放竞态/闪烁。
- 结论：`_content_area` 定义于 `tab_manager_window.py:536`（`QStackedWidget`），所有落点用 `hasattr` 守卫，
  未定义时安全跳过，不抛异常。**黑屏风险已通过 try/finally 消除。**

### #10 解冻点显式恢复 WebView 预览（对齐空窗）
- `_on_sidebar_anim_finished` finally 与 `_on_splitter_idle` 均显式
  `mw = self._content_area.currentWidget(); if hasattr(mw,"_set_cards_resize_preview_mode"): mw._set_cards_resize_preview_mode(False)`。
- `_set_cards_resize_preview_mode`（`main_widget.py:9163`）遍历 `chat_layout` 全部 `MessageCard` 调用
  `set_resize_preview_mode(False)` → 解冻时统一恢复 webview 显示。**幂等、覆盖当前 tab 全部卡片。**

### #12 欢迎卡补 placeholder + 删 welcome 早返回
- `message_card.py` 欢迎分支（`role=="welcome"`）现已创建 `self.resize_placeholder = QFrame(self)`
  （与 assistant 分支一致）；`set_resize_preview_mode` 删除 `if self.role=="welcome": return` 早返回。
- 审查 `set_resize_preview_mode(9896)`：先 `if self.viewer is None: return`（welcome 未懒渲染时安全早退），
  其后才访问 `self.resize_placeholder` → **welcome 卡不会 AttributeError**；viewer 存在时走占位逻辑
  （托高占位、隐藏 webview、不重建）。**#12 两处（placeholder 创建 + 删早返回）均已落地。**

## 2. 验收指标对照（✅=静态/路径可证；⏳=需 live）

| 验收指标 | 结论 |
|---|---|
| splitter 连续拖拽卡顿帧(>16.7ms)=0 | ⏳ live 实测；静态层面冻结使内容区每帧不再全量重绘/reflow，机制成立 |
| FPS≥55 | ⏳ live 实测 |
| MainWidget 子树 Paint 事件较修复前↓≥90% | ⏳ live 实测；冻结 `setUpdatesEnabled(False)` + webview 隐藏直接削减 paint |
| 折叠动画逐帧 paint_max<16.7ms、0 卡顿 | ⏳ live；本 #2 基线已证 panel 侧 paint ~0.5ms/帧，叠加冻结后内容区更稳 |
| 松手仅 1 次批量重绘 | ✅ `_splitter_idle_timer` 合并单次恢复；`_on_resize_finished` 防抖守卫避免双释放 |
| 欢迎卡+assistant 卡 webview 三类冻结期均经 `set_resize_preview_mode` 隐藏 | ✅ 见 §1；动画/拖拽冻结经 MainWidget.resizeEvent 自动触发，窗口 resize 直接触发 |
| 无 AttributeError（#12 两处） | ✅ 审查通过（viewer-None 守卫 + welcome 已建 placeholder） |
| 无黑屏（#4 try/finally） | ✅ try/finally + 新动画跳过逻辑保证恢复 |

## 3. 残留风险（非阻塞，建议 #4 后续小修）
- `_on_splitter_idle` 未判断「折叠动画是否仍在运行」：若拖拽途中跨阈值触发 collapse 动画、
  且松手后 120ms timer 先到期，会**提前** `setUpdatesEnabled(True)` + `_set_cards_resize_preview_mode(False)`，
  在动画尾段恢复内容区绘制（约 80ms 窗口），可能引入少量额外重绘。
  建议：`_on_splitter_idle` 内加 `if self._sidebar_anim is not None and self._sidebar_anim.state()==Running: return`
  （交由动画收尾统一恢复），与 `_on_sidebar_anim_finished` 的跳过逻辑对齐。

## 4. git diff 范围
- 未提交工作树改动（vs HEAD）：**`app/widgets/message_card.py`、`app/widgets/tab_manager_window.py`** 共两文件。
- `app/widgets/tab_panel.py`：**无未提交改动**（其 compact 状态修复已提交，保留）。
- 其他文件（含 `tests/`）：本优化未触及；`tools/perf/` 下仅本任务新增 `splitter_tabpanel_baseline.py`/`BASELINE_REPORT.md`/`
  baseline_result.json`（测试资产，不进业务 PR）。
- ✅ 符合「diff 范围仅 message_card.py + tab_manager_window.py」。

## 5. 人工 live 验收清单（开发机 / 带 GPU + 高 DPR）
```bash
cd D:/work/DriFox
.venv\Scripts\python.exe tools/perf/splitter_tabpanel_baseline.py --mode live --tabs 30 --runs 10
```
逐项确认：
1. [ ] 连续拖拽 splitter 把手（展开↔折叠 往返多轮）：用 QTest/手感确认无掉帧；
       用脚本采集 MainWidget 子树 Paint 事件数，较修复前（旧版同脚本基线）↓≥90%；FPS≥55。
2. [ ] 折叠/展开动画逐帧：`paint_max<16.7ms`、0 卡顿帧（栏侧本 #2 基线已 ~0.5ms，需确认叠加 content 冻结后仍达标）。
3. [ ] 松手（停拖 120ms）：仅 1 次批量重绘（对比顶层 resize 行为），无闪烁/双释放。
4. [ ] 欢迎卡（CodeWebViewer）+ assistant 卡：三类冻结期（折叠动画 / splitter 拖拽 / 窗口 resize）
       均被 `set_resize_preview_mode(True)` 隐藏（占位托高、不重建）；松手后正确恢复。
5. [ ] 无 AttributeError（确认 #12 两处同落地）；无黑屏（确认 #4 try/finally 恢复）。
6. [ ] 高 DPR（150%/200%）下重测 1–5，确认缩放不放大卡顿。

## 6. 提交/合并建议
- ✅ **建议可提交/合并**：两项业务改动范围精确（仅 2 文件）、编译通过、静态路径审查无崩溃/黑屏/AttributeError 风险，
  冻结+防抖+显式恢复机制自洽，符合优化目标。
- ⚠️ 合并前建议：在开发机跑一次 §5 live 清单（尤其 FPS≥55 与 Paint↓≥90% 两条需实测）；
  并视情况采纳 §3 的 `_on_splitter_idle` 动画运行守卫小修（非阻塞）。
- 注意：本 PR 不应包含 `tools/perf/` 测试资产与 `tab_panel.py`（已提交）；仅 `message_card.py` + `tab_manager_window.py`。
