# 消息渲染管线（流式 / 滚动 / WebView）

> 涉及 `message_card`、流式输出、滚动跳变、空白卡片、骨架屏、图表渲染时**必读**。
> 这是全项目最活跃的链路，也是坑密度最高的地方（state 坑点库 Top1 模块）。
> 改这里之前先读 `known-pitfalls.md` 的「渲染与滚动」一节。

---

## 一、管线全景

```
流/chunk 到达
  → MessageCard._perform_update()
      ├─ 流式分支：增量 diff → _inject_tool_blocks / _inject_think_cards → JS 注入
      └─ 非流式分支（历史加载 / 流式结束终渲染）：整页重建 → _cached_streaming_html 复用判定
  → CodeWebViewer（QWebEngineView，来自 WebViewPool）
       骨架 HTML（_SKELETON_CACHE_VERSION）
       + typewriter 队列（window._tw）
       + FLIP 动画队列（window._animEnqueue）
  → 高度上报 → HeightCommitBatch（批量提交 + 锚点保持）
  → main_widget 滚动锚定补偿（_on_message_card_height_changed）
```

## 二、CodeWebViewer 与 WebViewPool

| 位置 | 说明 |
|------|------|
| `app/widgets/webview_pool.py` | `WebViewPool`（进程级单例）：`acquire() / release() / clear() / pids() / set_enabled()` |
| `MAX_IDLE_PER_BUCKET = 4` | 每桶（light/full）保留空闲 viewer 上限 |
| `MAX_CONSECUTIVE_FAILURES = 3` | 连续失败达阈值整池停用（自动降级为新建实例） |
| `app/widgets/message_card.py` → `CodeWebViewer` | `MAX_WIDTH=1800` / `MAX_HEIGHT=10000` |
| `CodeWebViewer.reset_for_reuse()` | 复用前复位；失败时整池停用 |
| `MessageCard.detach_viewer()` / `ensure_rendered()` | 归还 / 取用 viewer |
| `main_widget._try_detach_card_viewer()` | 回收前先尝试 detach |
| `main_widget._pid_still_in_use()` | 强回收护栏：把池中 renderer PID 视为在用 |

**硬约束**
1. 复用 viewer 必须 `reset_for_reuse()`：清内容但**保留骨架**（`_RESET_CONTENT_FOR_REUSE_JS`）。
2. **任何骨架 JS / DOM 结构改动都要递增 `_SKELETON_CACHE_VERSION`**（当前 27）。不递增 → 拿到旧缓存 HTML → 空白卡 / JS 未就绪。
3. 池化实例被外部 kill 后再复用会白屏；强回收前必须问 `pids()`。
4. 不要为每张卡片建 transient profile；共享 profile 见 `app/core/webengine_profile.py`。

## 三、流式渲染的三条分支

| 分支 | 触发 | 要点 |
|------|------|------|
| 流式增量 | 生成中 | 增量 diff + JS 注入，禁止整页重建 |
| 非流式（历史加载） | `viewer._is_history = True` | 完成态渲染，无流式字数统计 / 坞态 |
| 非流式（流式结束终渲染） | `finish_streaming()` → `_final_render_pending = True` | **必须同步完成**，且不能走线程池异步，否则异步 JS 未执行被下一次渲染覆盖 |

**守卫**
- `_final_render_pending`：终结渲染标记。为 True 时禁止复用 `_cached_streaming_html`（否则形态跳变 / 缺工具运行框）。
- 历史实例也要正确设置 `_streaming` 标志（viewer 初始为 True 是流式设计，历史渲染须切非流式）。
- 长内容渲染：最终渲染同步化，避免异步渲染结果晚到覆盖。

## 四、高度与滚动锚定

| 组件 | 位置 | 要点 |
|------|------|------|
| `HeightCommitBatch` | `app/widgets/height_commit_batch.py` | `begin/submit/flush/end`；`_IDLE_CLOSE_MS=150`；同卡多次上报取最后一次；锚点 + 相对视口偏移（与到达顺序无关）；期间关闭 per-card 增量补偿（`card._last_height_delta = 0`）；flush 用 `setUpdatesEnabled(False/True)` 包裹批量 `setFixedHeight` |
| `ResizeOrchestrator` | `app/widgets/resize_orchestrator.py` | resize 期统一协调，避免抖动与重复布局 |
| 滚动常量 | `app/main_widget.py` | `AT_BOTTOM_TOLERANCE = 24`；`SCROLL_JUMP_SHOW_THRESHOLD = 120` |
| 贴底跟随 | `main_widget` | `_bottom_anchor_deadline` + `_maintain_bottom_anchor()`；`_user_intentionally_away_from_bottom` 与 `_should_follow_bottom()` 单点裁决 |
| 兜底 | `_ensure_at_bottom(retries=8)` | 8×300ms ≈ 2.4s，覆盖 WebEngine 异步高度 |
| 高度补偿 | `_on_message_card_height_changed()` | 仅当卡片底 `<= value` 或 `_should_follow_bottom()` 才 `value += delta` |

**滚动锚定的既有约定**（JS 侧，改之前先对齐）
- `wheel` 监听要**同步**置位上滚意图；`scroll` 监听必须检查 `_suppressScrollEvent`。
- save/restore 前后要保存并恢复 `scrollTop`，用 `_progScroll` 标记程序性滚动。
- 工具区 / 思考区（`#tool-content`）与正文（`#content-placeholder`）是两套滚动，改一处要同步另一处的模式。

## 五、动画与揭示队列

| 机制 | 要点 |
|------|------|
| typewriter | `_TYPEWRITER_JS` → `window._tw` / `_twPush/_twStep/_twFlush/_twReset`；`CATCHUP_MS=110`、`BURST_LEN=400`；高度上报节流 ≥80ms 防回环 |
| FLIP | `_FLIP_JS` → `_animEnqueue(fn,dur)` 串行队列 + `_flipArm/_flipCapture/_flipFind/_flipPlay`；切坞态 arm 1200ms、流式结束前 `_flipArm(2000)`；渲染用 `data-flip-key="think-{flip_idx}"` |

多个动画同时开跑 = 跳变。新增动画一律进串行队列。

## 六、Markdown / 代码块 / 图表

- **fence 哨兵**：`_extract_fenced_code / _restore_fenced_code` 把完整 fence 抽成 `\x00FN\x00` 占位，注入类操作前提取、convert 前放回。5 处管线（impl / stable / inline / worker 两分支）都要接。
- **fence 跨空行**：闭合时回溯 `fence_start`，把整个 fence 区间作为**单段**产出，否则末尾闪现孤立空代码块。
- **图表 vault**：全量渲染前按内容 key 把已渲染 echarts / mermaid / katex 节点暂存到 detached Map，`innerHTML` 替换后回插（回插后 delete，防同图挪位）；echarts init 用 rAF 每帧 1 个，mermaid 并发限 2。
- **renderProcessTerminated 自愈**：`_context_lost_count > 2` 走 `needRecreate`。
- **内联 SVG**：工具栏扫描器要穿透 `div/p` 里唯一子节点为 svg 的容器。

## 七、卡片配额与内存

- `_MAX_RENDERED_CARDS = 12`（`main_widget.py`）；`_MIN_RENDERED_CARDS_PER_WINDOW` 兜底。
- `_effective_max_rendered_cards()` 按可用内存动态收缩；回收走 `_try_detach_card_viewer` + 池归还。
- 超过配额才回收，受保护批次跳过（`_rendered_card_count` 记账）。

## 八、改这条链路的自检清单

- [ ] 动过骨架 JS / DOM → 递增 `_SKELETON_CACHE_VERSION`
- [ ] 复用 viewer → `reset_for_reuse()`，且 `_pid_still_in_use()` 护栏没绕过
- [ ] 新增注入类处理 → 走 fence 哨兵，5 处管线全覆盖
- [ ] 高度上报 → 走 `HeightCommitBatch`，不要单卡 `setFixedHeight`
- [ ] 滚动改动 → wheel 同步置位 + `_suppressScrollEvent` 检查 + scrollTop 保存恢复
- [ ] 终结态渲染 → `_final_render_pending` 路径同步执行，不复用流式缓存
- [ ] 长消息 / 多卡片跑一遍：无白屏、无空白卡、滚到底不跳、切 tab 回来位置正确
