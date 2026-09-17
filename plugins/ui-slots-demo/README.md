# ui-slots-demo — UI 扩展点示例插件

演示 DriFox 消息卡片与独立弹窗相关的 UI 插件扩展点，可作为新插件开发的参考模板。

## 演示内容

| 扩展点 | 位置 | 行为 |
|--------|------|------|
| `register_window` | 左侧「自定义插件」栏「UI 扩展点演示」条目 | 点击开/关切换独立弹窗；右键菜单「弹出」重新打开；自动命令 `/ui-slots-demo:demo` |

> **左侧栏分组**：`register_window(..., group="system"|"custom")` 可覆盖条目所在分区——
> `"system"` 进常驻区，`"custom"` 进「自定义插件」折叠区，不传则跟随插件归属
> （仓库内置插件→常驻，用户插件→自定义）。
| `register_footer_action`（`role="user"`） | 用户消息气泡底部按钮行（内置复制/撤销/删除**左侧**） | 「发送到演示窗」：把该条消息内容送去弹窗展示 |
| `register_footer_action`（`role="both"`） | 助手页脚按钮组 + 用户气泡按钮行 | 「打开演示窗」：打开/前置独立弹窗 |
| `register_footer_stat` | 消息卡片页脚左区（耗时右侧） | 注入「N 字」信息项（本条消息字数） |

## 关键代码路径

- `ui/__init__.py`：`register_ui(registry)` 注册四类扩展点 + 联动回调
- `ui/demo_page.py`：弹窗内容页（`DemoWindowPage`），演示三个约定接口：
  - `set_context_provider(provider)` —— 上下文拉模型（每次调 provider() 拿最新 ctx）
  - `show_card()` —— 弹窗打开/激活时的数据加载入口
  - `refresh_theme()` —— 主题切换刷新

## 内容页编写要点

1. **窗口壳不用写**：无边框、自定义标题栏、拖动/缩放/最小化/关闭、主题跟随、生命周期（随应用退出 / 插件卸载销毁）全由主程序提供，插件只写客户区内容。
2. **context 取色**：优先读 `ctx["colors"]`（`text_primary` / `text_secondary` / `border` / `accent`），缺失时按明暗主题回退安全色。
3. **数据加载走 `show_card()`**：主程序在弹窗打开/激活时调用；不实现该方法内容页只建骨架。
4. **按钮回调 ctx**：`footer_action` 的 `on_click(ctx)` 收到 `card`（消息卡片实例）/ `role` / `message_index` 等，用 `card.get_plain_text()` 取消息文本。

## 安装与验证

随仓库分发时位于 `plugins/ui-slots-demo/`（系统插件目录）；作为用户插件使用时可整个目录复制到 `~/.drifox/plugins/`，watchfiles 会在 1-3 秒内热加载。
