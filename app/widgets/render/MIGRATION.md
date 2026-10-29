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

环境变量 `DRIFOX_RENDER_V2=1` 时：

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
  落地后清除 JS 注入节点）、工具区显隐 + header、工具区内部滚底；
  patchBlocks 接 `restoreCollapsibleStates(made)`。
- DOM 重建点基线失效：`_load_skeleton` / `reset_for_reuse` 清 `_v2_last`。
- 测试：`tests/widgets/test_block_render_v2_wiring.py`（11 项）。

危险区（真机必须逐项跑通）：滚动锚定、高度上报、图表 vault、折叠 body 懒注入、
切 tab 回来、窗口 resize。`_SKELETON_CACHE_VERSION` 已递增至 52（patchBlocks
删除段 dispose + 折叠恢复 + _v2AfterPatch），首帧会全量重载骨架。

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
