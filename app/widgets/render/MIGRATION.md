# block 级渲染管线 · 接线施工说明

本包当前是**未接线的纯函数层**：新代码在仓库里，但没有任何生产路径调用它。
迁移完成后可删除本文件（不会留下指向已不存在代码的文档）。

设计定稿：`docs/superpowers/specs/2026-11-02-block-render-pipeline-design.md`

## 接线四步

### 步骤 2：骨架加 patchBlocks JS 通道

`card_viewers._load_skeleton` 里新增 `window.patchBlocks(ops)`：对
`#content-placeholder` 的子节点按 `ops.base` 起做 splice。每个子节点必须带
`data-block-index` 与 `data-block-key`，splice 时按 index 递增重排。

保留 `updateContent`（旧路径不动）。**必须递增 `_SKELETON_CACHE_VERSION`**，
否则拿到旧骨架缓存 → patchBlocks 未定义 → 整卡不更新。

### 步骤 3：CodeWebViewer 接新通道（特性开关）✅ 已落地（2026-10-29）

**默认开启**（2026-10-29 起进入真机灰度）；环境变量 `DRIFOX_RENDER_V2=0/false/no`
显式关闭（逃生门，退回旧全量路径）。v2 开启时：

- `_perform_update` 入口分派 `_perform_update_v2`：`render_message` →
  `diff_message` → `patchBlocks('content-placeholder' / 'tool-content')`，
  同步渲染（块级缓存下每批只重渲活动块，线程池往返反而更贵）。
- 数据源：`_v2_blocks()` 从父级 MessageCard 读 `_content_data`（块列表）。
  `_markdown_text` 仍照常维护（get_plain_text / 工具 body 反查 / 导出的书账）。
- 无块源（用户 / 欢迎消息的 str 内容）→ 回退旧路径并永久关闭该 viewer 的 v2。
- 每 viewer 独享 `BlockCache`；ctx 签名（compact / lazy_body / heavy_caps /
  主题 / 图标 / 字号 / suppress 数）变化即清缓存 + 全量重落地。
- 全量落地（首帧 / set_content / 主题 / finish / 哨兵补渲）前清两容器并
  dispose 全部 echarts 实例；patchBlocks 删除段也逐节点 dispose。
- `_append_text_incremental` 在 v2 下短路（纯文本注入会破坏 DOM↔block 对齐）；
  `_render_tail_inline` / typewriter / save-restore 在 v2 路径不执行。
- JS 侧新增 `window._v2AfterPatch`：流式运行框按 tool_call_id 去重（真实块
  落地后清除 JS 注入节点）、简洁模式下把嵌在 text 块内的协议思考/工具卡搬到
  工具区（reorganizeContent 最小子集，非块节点不参与 splice）、工具区显隐 +
  header、工具区内部滚底；patchBlocks 接 `restoreCollapsibleStates(made)`。
- DOM 重建点基线失效：`_load_skeleton` / `reset_for_reuse` 清 `_v2_last`。
- 测试：`tests/widgets/test_block_render_v2_wiring.py`（11 项）。

危险区（真机必须逐项跑通）：滚动锚定、高度上报、图表 vault、折叠 body 懒注入、
切 tab 回来、窗口 resize。`_SKELETON_CACHE_VERSION` 已递增至 52（patchBlocks
删除段 dispose + 折叠恢复 + _v2AfterPatch），首帧会全量重载骨架。

### 步骤 3.5：流式纯文本尾态（B5.2）✅ 已落地（2026-10-29）

v2 原先每批对活动块跑全量 markdown 并整块重传（`patchBlocks` 的 ops 里是
活动块的完整 HTML）。真实语料实测（30KB 回答、40 字符/批、747 批）：累计
传输 7.9~14.6MB、Python 渲染累计 4.5~5.1s，随回答长度呈 O(n²)。

现在活动 text 块（≥512 字符）走「纯文本尾态」：

- `render_message(..., live_index=k, live_html=...)` → 该块产出恒定 key
  （`live:k`，见 `blocks.live_key`）、html 为纯文本节点。
- key 与内容无关 → `diff_message` 永远不产生该块的替换 op（html 不在
  `BlockRender.identity` 里）。
- 每批只把新增文本经 `window._v2LiveAppend(container, key, text)` 追加到
  `.v2-live-text`（`document.createTextNode` + `appendChild`，不触发整块
  `innerHTML` 重解析）。live 容器插在 `#char-count` 之前，统计占位保持末尾。
- 安全边界（段落/句号结尾 + 无未闭合 fence/think + 距上次 ≥1s + 新增
  ≥256 字符）→ 整块正式化一次（markdown 全量，`patchBlocks` 替换）；
  `_v2_live_stabilize` 把落库基线的该块换回恒定 key，避免下一批又判定替换。
- 文本回退/重写（重试、撤回、编辑）→ 整卡重落地并复位尾态。
- `_load_skeleton` / `reset_for_reuse` / force_full → 复位尾态。
- 流式结束走非 live 分支（`char_count` 正常拼、终渲染一致）。

实测收益（同语料，`tests/debug/_probe_stream_live.py`）：传输
6.1~8.7MB → 0.16~0.17MB（7~51 倍），Python 渲染 3.5~4.9s → 89~116ms
（18~42 倍）。

骨架新增 `window._v2LiveNode` / `_v2LiveAppend` 与 `.v2-live-text` 样式，
`_SKELETON_CACHE_VERSION` 已递增至 53（旧骨架缺追加函数 → 增量静默丢弃，
必须失效缓存）。测试：`tests/widgets/test_block_render_v2_live.py`（8 项，
含骨架 `<script>` 块 node 编译防线）。

**观感取舍**：流式期间活动块以纯文本显示（不解析 markdown），每到安全边界
且距上次 ≥1s（且新增 ≥256 字符）才正式格式化成 markdown。长代码块在围栏
闭合前保持纯文本（半截围栏不能正式化）。调 `_LIVE_FORMALIZE_MIN_INTERVAL_S`
可换「格式化及时度 ↔ 成本」。

### 步骤 4：拆旧路

开关稳定后删除：`_perform_update` 的增量分支、`_render_tail_inline`、
`_extract_closed_segments`、`_render_stable_segment`、`_render_inline_tail`、
`updateContentAppend`、`updateTailHtml`、`reorganizeContent`、
`_build_save_and_restore_js`、typewriter JS、`_stable_md_len` / `_stable_html`。

## 验收基线（改完必须复测不退化）

| 维度 | 脚本 | 2026-10-29 基线 |
|---|---|---|
| 等价性 | `tests/debug/_probe_block_render_real.py` | 16053 条真实消息，可见文本 0 差异 |
| 渲染耗时 | `tests/debug/_bench_real_render_cost.py` | p50 0.5ms / p90 2.7ms / p99 9.5ms（7550 条） |
| 单元契约 | `tests/widgets/test_block_render.py` | 17 项 |

## 已知存量缺陷（本层不修，迁移时确认是否顺带解决）

`message_content.py` 的 `result_escaped = _sanitize_result(result_raw)[:300]`
把工具结果截断到 300 字符后才写进渲染源，而折叠框展开是从该渲染源反查的
（`find_tool_segment_by_call_id`）→ 展开看到的工具结果本身就是截断版。
block 级渲染直接拿 `block["result"]` 完整值，这条反查链可整条删除。
