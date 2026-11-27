# 流式高度提交合并实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 滚动活跃期间跳过重复的 WebEngine 高度提交，降低流式与滚动同时发生时的事件循环长尾，同时保证滚动停止后高度继续收敛。

**Architecture:** 保留 `MessageCard._stream_height_tick_step()` 的目标高度、逼近、收敛和失效处理，仅在读取当前 viewer 高度并计算差值后增加 `is_scroll_active()` 门控。门控期间不调用 `_apply_viewer_height()`，也不停止追踪定时器；下一个非活跃 tick 继续原逻辑。通过现有 Qt viewer 桩补行为回归，不改滚动模块和 WebEngine 回收链。

**Tech Stack:** Python 3.14、PyQt5、pytest、pytest-qt/QApplication 测试桩、现有 `app.utils.scroll_tuning.is_scroll_active()`。

---

### Task 1: 为滚动期高度提交增加失败回归测试

**Files:**
- Modify: `tests/widgets/test_stream_height_noise_guard.py`
- Reference: `app/widgets/message_card.py:3521-3558`

- [ ] **Step 1: 在现有追踪测试类后新增滚动期门控测试**

使用现有 `_make_streaming_card()` 和 `_StubViewer`，通过 monkeypatch 替换 `app.widgets.message_card.is_scroll_active`，避免依赖真实时间窗口。测试必须验证滚动期间不调用 viewer 的 `setFixedHeight`，且追踪状态仍保持；随后把滚动状态改为非活跃，调用同一个 tick，验证高度提交恢复。

```python
def test_height_tick_defers_during_scroll_and_resumes(self, qapp, monkeypatch):
    """滚动期不重复提交高度，滚动停止后仍从最新目标继续收敛。"""
    container, card = _make_streaming_card(qapp, viewer_height=500)
    try:
        card._target_viewer_height = 700
        card._stream_height_anim_active = True
        scroll_active = True
        monkeypatch.setattr(
            "app.widgets.message_card.is_scroll_active",
            lambda: scroll_active,
        )

        card._stream_height_tick_step()

        assert card.viewer.set_calls == []
        assert card._stream_height_anim_active is True
        assert card._target_viewer_height == 700

        scroll_active = False
        card._stream_height_tick_step()

        assert card.viewer.set_calls
        assert card.viewer.set_calls[-1] > 500
    finally:
        container.deleteLater()
        qapp.processEvents()
```

- [ ] **Step 2: 单独运行新测试，确认当前实现失败**

Run:

```bash
.venv/bin/python -m pytest tests/widgets/test_stream_height_noise_guard.py::TestTrackDirectionGuard::test_height_tick_defers_during_scroll_and_resumes -q
```

Expected: FAIL，因为当前 `_stream_height_tick_step()` 不检查 `is_scroll_active()`，第一次调用会直接执行 `_apply_viewer_height()`，`set_calls` 非空。

- [ ] **Step 3: 提交测试变更**

```bash
git add tests/widgets/test_stream_height_noise_guard.py
git commit -m "test(perf): 覆盖滚动期高度提交门控"
```

### Task 2: 实现滚动期高度提交门控

**Files:**
- Modify: `app/widgets/message_card.py:3521-3558`
- Test: `tests/widgets/test_stream_height_noise_guard.py`

- [ ] **Step 1: 在差值计算后增加最小门控**

保留 `target <= 0` 的停拍逻辑和 `try/except`，在 `current_height`、`diff` 计算完成后、收敛判断之前加入：

```python
            if is_scroll_active():
                # 滚动补偿期避免与 QWebEngine 高度调整争抢主线程；目标值保留，
                # 滚动活跃窗口结束后由下一拍继续收敛。
                return
```

完整目标结构应保持如下顺序：

```python
        try:
            current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
            diff = target - current_height
            if is_scroll_active():
                # 滚动补偿期避免与 QWebEngine 高度调整争抢主线程；目标值保留，
                # 滚动活跃窗口结束后由下一拍继续收敛。
                return
            if abs(diff) <= STREAM_HEIGHT_TRACK_EPSILON:
                self._apply_viewer_height(target)
                self._stop_stream_height_track()
                return
```

不要修改 `_target_viewer_height`、`_stream_height_tick`、`_height_track_factor`、滚动补偿或 WebEngine 回收逻辑。门控不能放在 `target <= 0` 之前，也不能以 `_streaming` 作为条件，否则会破坏结束态收尾。

- [ ] **Step 2: 运行新测试并确认通过**

Run:

```bash
.venv/bin/python -m pytest tests/widgets/test_stream_height_noise_guard.py::TestTrackDirectionGuard::test_height_tick_defers_during_scroll_and_resumes -q
```

Expected: PASS。

- [ ] **Step 3: 运行相关高度与滚动回归测试**

Run:

```bash
.venv/bin/python -m pytest \
  tests/widgets/test_stream_height_noise_guard.py \
  tests/widgets/test_stream_height_track_throttle.py \
  tests/widgets/test_streaming_smoothness_wiring.py \
  tests/widgets/test_placeholder_height_settled.py \
  tests/utils/test_scroll_tuning.py -q
```

Expected: 新增测试及既有高度、节拍、滚动测试通过；若出现 macOS Qt 段错误，拆成单文件运行并记录为既有环境问题，不修改生产逻辑绕过。

- [ ] **Step 4: 运行静态检查**

Run:

```bash
.venv/bin/ruff check app/widgets/message_card.py tests/widgets/test_stream_height_noise_guard.py
.venv/bin/python tools/eol_guard.py check app/widgets/message_card.py tests/widgets/test_stream_height_noise_guard.py
```

Expected: 无 ruff 错误；行尾检查通过。

- [ ] **Step 5: 提交实现**

```bash
git add app/widgets/message_card.py tests/widgets/test_stream_height_noise_guard.py
git commit -m "fix(perf): 合并滚动期流式高度提交"
```

### Task 3: 完成性能验证与结果记录

**Files:**
- Reference: `benchmarks/results/stream_scroll_mac_20261117_154656.json`
- Allowed result output: `benchmarks/results/` 现有基准产物目录，不修改业务文件

- [ ] **Step 1: 运行可用的流式滚动性能基准**

使用项目已有的流式滚动 probe，保持与基线相同的 macOS 环境、消息规模和采样配置；若脚本入口不同，先从 `benchmarks/` 与 `tests/debug/` 中定位现有入口，不新建第二套基准。

验收重点：

- streaming/scroll event-loop lag p95、p99 相比历史 `约 51ms` 下降并向 baseline `约 16ms` 靠拢；
- `jank_gt33ms_count` 不增加；
- `update_content` 与单次滚动耗时不回归。

- [ ] **Step 2: 运行会话内存回归**

```bash
.venv/bin/python benchmarks/bench_session_leak.py --rounds 40 --msgs 20
```

Expected：会话数仍稳定归位；tracemalloc/RSS 斜率不明显高于当前 `+2.43KB/轮`、`+2.65KB/轮` 基线。

- [ ] **Step 3: 记录验证结论并检查差异范围**

```bash
git diff HEAD~2..HEAD --stat
git status --short
```

确认变更只涉及 `app/widgets/message_card.py`、`tests/widgets/test_stream_height_noise_guard.py` 及基准产物；不得修改启动线、历史读链、缓存或 WebEngine 回收模块。若性能未改善或出现内容裁剪、底部漂移、追踪不收敛，回退 Task 2 的门控提交，保留失败测量证据。

- [ ] **Step 4: 提交前完成代码审查准备**

```bash
git diff --check
.venv/bin/python tools/eol_guard.py check --cached
```

Expected：无空白错误、无混合行尾噪声；随后进入代码审查和合并决策。
