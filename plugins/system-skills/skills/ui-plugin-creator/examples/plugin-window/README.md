# plugin-window-example — 独立弹窗 UI 插件示例

## 这是什么

**独立弹窗（plugin window）**的最小可运行结构：注册一个可脱离主窗的顶级窗口，
自动获得左侧插件栏条目（点击开/关）+ 右键「弹出」+ 命令 `/plugin-window-example:tool`。
参照自仓库内置示例 `plugins/ui-slots-demo/`（功能更全：还演示了消息卡片按钮与信息注入）。

```
plugin-window/
├── plugin.json                  # 需自建（内容见下），components: { "ui": true }
└── ui/
    ├── __init__.py              # register_ui(registry)：热重载清理 + register_window
    └── example_window_page.py   # 内容页：三约定接口 set_context_provider/show_card/refresh_theme
```

> ⚠️ 技能包内不带 `plugin.json`（仓库 `.gitignore` 的 `*.json` 规则不放行非
> `.drifox-plugin/` 目录的 json）。复制后请自建 `plugin.json`：
>
> ```json
> {
>   "name": "plugin-window-example",
>   "description": "独立弹窗示例",
>   "version": "0.1.0",
>   "author": { "name": "you" },
>   "license": "MIT",
>   "components": { "ui": true }
> }
> ```

## 何时参照

- 从零写一个**独立弹窗**（工具窗：面板类 UI、需要拖出主窗/多屏摆放）
- 忘记 `register_window` 参数（window_id / widget_class / group / context_provider）
- **弹窗内容"黑块 + 无数据"** → 看内容页的 `show_card()` 约定（模板文档 §15.3、pitfalls §19）
- 窗口拖不动、点不到标题栏按钮 → 看 pitfalls §20（自建 FramelessWindow 的布局坑）

其他挂载点：浮动卡 → [`floating-card/`](../floating-card/)；欢迎页 tab → [`welcome-tab/`](../welcome-tab/)。

## 对应技能章节

- `templates-window.md` §15 — 独立弹窗完整模板（载体选择 / API / 三约定 / 取色 / 联动）
- `checklist.md` §15.1 — 独立弹窗验证清单
- `pitfalls.md` §19/§20 — 弹窗内容页与自建窗口的两个实战坑

## 如何使用

1. 复制本目录到 `~/.drifox/plugins/plugin-window-example/`
2. 搜索 `[改名]` 标记：插件名、window_id、sys.modules 前缀三处对齐
3. 重启后：左侧「自定义插件」栏出现条目（点击开/关），或输入 `/plugin-window-example:tool`
4. 在 `ExampleWindowPage` 里填你自己的界面；数据加载与主题统一放 `show_card()`

> Qt 绑定：本示例按仓库现状使用 **PyQt5**（2026-09 实测）。
