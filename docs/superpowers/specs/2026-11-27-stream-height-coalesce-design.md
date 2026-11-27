# 流式高度提交合并设计

日期：2026-11-27
状态：待用户审阅
范围：运行期流式交互性能；明确排除启动线与 WebEngine 回收核心逻辑

## 1. 背景与证据

现有基线显示：

- `benchmarks/results/stream_scroll_mac_20261117_154656.json`：事件循环延迟 baseline p95=`16.04ms`，streaming p95=`51.10ms`，scroll p95=`51.13ms`；单次滚动 p95=`3.03ms`，`jank_gt33ms_count=0`。
- `update_content` p95=`0.03ms`，说明 Python 内容更新本身不是主要耗时。
- `app/widgets/message_card.py:_stream_height_tick_step()` 每 `50ms` 执行一次高度追踪，并调用 `_apply_viewer_height()`；该调用最终会触发 QWebEngine 高度调整与跨进程重新布局。
- 滚动期已有装饰动画降载，但高度追踪 tick 仍会继续提交 `setFixedHeight`，与滚动补偿链竞争事件循环。

这些证据指向同一类问题：高度更新单次不慢，但固定 50ms 的跨进程提交在流式和滚动同时发生时形成事件循环长尾。

## 2. 目标与非目标

### 目标

1. 减少流式期间不必要的 QWebEngine 高度提交。
2. 滚动活跃期间避免高度追踪与滚动补偿同时争抢主线程。
3. 停止滚动后仍能及时提交最终高度，不改变内容显示完整性。
4. 以回归测试和性能基准证明事件循环长尾改善，且不引入内存增长。

### 非目标

- 不调整启动时序、启动预算或顶层 import。
- 不修改 WebEngine profile、renderer 配额、LRU 回收和卡片虚拟化策略。
- 不改变内容高度计算、流式文本语义、滚动补偿方向和卡片复用协议。
- 不把 `STREAM_HEIGHT_TICK_MS` 放宽到现有护栏禁止的频率之外。

## 3. 方案

在 `_stream_height_tick_step()` 的提交点增加滚动活跃门控：

1. 仍读取并保留最新 `_target_viewer_height`。
2. `is_scroll_active()` 为真时，不调用 `_apply_viewer_height()`，本次 tick 直接结束；追踪定时器保持运行。
3. 滚动状态结束后，下一次 tick 使用最新目标值继续逼近并提交。
4. 保留现有 `target<=0`、收敛、viewer 失效和结束态逻辑。

该方案只抑制滚动期间的重复提交，不改变高度目标与最终收敛路径。若测试证明滚动停止后的首个 tick 延迟过大，再增加一个绑定卡片生命周期的单次补提交，不在第一版预先引入。

## 4. 测试与验收

### 结构/行为测试

新增或修改 `tests/widgets` 中流式高度追踪测试，覆盖：

- 滚动活跃时不调用 `_apply_viewer_height()`。
- 滚动结束后仍会调用 `_apply_viewer_height()` 并收敛到目标高度。
- 目标已收敛时，原有停止逻辑不变。
- 结束态 `_streaming=False` 时，仍不被错误地提前停止。
- viewer 被回收或销毁时，定时器仍能安全停止。

### 性能验收

使用同一 macOS 环境运行流式/滚动基准，比较优化前后：

- streaming 与 scroll 的 event-loop lag p95/p99，目标从约 `51ms` 向 baseline `16ms` 靠拢。
- `jank_gt33ms_count` 不增加，优先下降。
- 对话管线与会话生命周期内存斜率不恶化。
- 既有流式高度、滚动、虚拟回收测试通过。

由于当前 `bench_chat_pipeline.py` 在 macOS WebEngine 初始化阶段出现 `SIGSEGV (-11)`，该基准暂不作为本改动的唯一验收依据；需记录为环境/测量设施阻塞，并使用不触发该段错误的测试和流式滚动基准交叉验证。

## 5. 风险与回滚

主要风险是滚动期间高度暂不更新导致短暂底部空白或最终高度收敛延迟。通过保留最新目标、滚动结束后继续 tick、补充收敛测试控制风险。

若性能指标未改善，或出现内容裁剪、底部漂移、回收卡死，立即回退本次门控，不修改已有滚动补偿算法和 WebEngine 回收链。

## 6. 实施边界

首版只允许修改：

- `app/widgets/message_card.py`
- 与该行为直接对应的 `tests/widgets` 测试文件
- 本设计文档及必要的性能结果说明

不得顺手修改启动链、历史读链、全局缓存和 WebEngine 回收模块。
