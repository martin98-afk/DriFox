# floating-card-example — 浮动卡片 UI 插件示例

## 这是什么

**浮动卡片（floating card）**的最小可运行结构：注册一张可停靠、可开关的卡片，注册后自动获得同名命令（`/floating-card-example`）。参照自系统插件 `file-tree`（其卡片本体较复杂，本示例只保留骨架与惯用法）。

```
floating-card/
├── plugin.json            # components: { "ui": true }
└── ui/
    ├── __init__.py        # register_ui(registry)：热重载清理 + register_floating_card
    └── example_card.py    # 卡片类：QWidget 子类 + parent=None 约定 + destroyed 清理
```

## 何时参照

- 从零写一个**新的浮动卡**（侧边停靠面板类 UI）
- 忘记 `register_floating_card` 参数（plugin_name / card_id / widget_class / container）
- 卡片改代码后不生效 / 报 NameError → 看 `__init__.py` 里的 sys.modules 清理惯用法
- 卡片关闭后程序崩溃 → 看 `destroyed.connect(self._cleanup)` 线程清理惯例

其他挂载点：欢迎页 tab → [`welcome-tab/`](../welcome-tab/)。

## 对应 SKILL.md 章节

- §1 组件类型决策树 — 确认浮动卡是你要的挂载点
- §3.1 第一次做 UI 插件 — 学习路径
- §5.5 异步 worker 生命周期 — 卡片里开线程必读
- §5.6 生成代码后的自检清单

## 如何使用

1. 复制本目录到 `~/.drifox/plugins/floating-card-example/`
2. 搜索 `[改名]` / `[惯例]` 标记：插件名、card_id、objectName、sys.modules 前缀四处对齐
3. 重启后输入 `/floating-card-example` 打开卡片
4. 在 `ExampleCard` 里填你自己的界面

> Qt 绑定：本示例按仓库现状使用 **PyQt5**（2026-09 实测）。新插件动手前建议 `grep "from PyQt5" plugins/` 复核一次。
