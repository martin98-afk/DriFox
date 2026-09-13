# welcome-tab-example — 欢迎页 tab UI 插件示例

## 这是什么

**欢迎卡片 tab**（welcome tab）的最小可运行结构：在主页欢迎卡片的标签栏加一个自己的 tab，`render_func` 返回一段 HTML 即完成渲染。参照自系统插件 `welcome_changelog`。

```
welcome-tab/
├── plugin.json            # components: { "ui": true }
└── ui/
    ├── __init__.py        # register_ui(registry)：register_welcome_tab
    └── _render.py         # render_example(ctx) -> 完整 HTML 片段
```

## 何时参照

- 想在**欢迎卡片**上展示自己的内容（公告 / 统计 / 快捷入口）
- 忘记 `register_welcome_tab` 参数（plugin_name / mode_key / label / render_func / priority）
- 不确定 `render_func` 的签名与返回值约定：`(ctx: dict) -> str(HTML)`
- 内容需要后台拉数据 → 照 welcome_changelog 的分层：`_fetcher.py` 缓存 + ui_event_bus 事件刷新，render_func 保持纯函数

侧边停靠卡片 → [`floating-card/`](../floating-card/)。

## 对应 SKILL.md 章节

- §1 组件类型决策树 — welcome tab vs 浮动卡的选择
- §2 references/ 文件结构 — ctx 字段、HTML 渲染管线细节
- §3.1 第一次做 UI 插件 — 学习路径
- §5 实战经验总结 — Python 3.14 语法、字体注入等踩坑记录

## 如何使用

1. 复制本目录到 `~/.drifox/plugins/welcome-tab-example/`
2. 搜索 `[改名]` 标记：plugin_name / mode_key / label 三处对齐
3. 重启后在欢迎卡片看到「⭐ 示例」tab；在 `_render.py` 里替换成你的内容
4. tab 的显隐跟随插件的启用/禁用，无需额外代码

> Qt 绑定：本示例渲染层纯 HTML，无 Qt 导入。若需 Qt（如 _fetcher 用 QThread），DriFox 当前使用 **PyQt5**（2026-09 实测仓库现状）。
