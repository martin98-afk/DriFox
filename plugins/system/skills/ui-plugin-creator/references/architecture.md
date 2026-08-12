# UI 插件系统架构

> 核心架构概览，帮助理解 UI 插件的工作原理和集成点。

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│  plugins/<plugin-name>/ui/                              │
│  ├── __init__.py     → register_ui(registry) 入口       │
│  ├── cards.py        → 浮动卡片 QWidget 子类             │
│  └── renderers.py    → 内容块 HTML 渲染器函数             │
└──────────────────────┬──────────────────────────────────┘
                       │ register_ui(registry)
                       ▼
┌─────────────────────────────────────────────────────────┐
│  app/core/ui_plugin_registry.py                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │  UIPluginRegistry (单例)                           │  │
│  │  ├─ _content_renderers: Dict[str, RendererInfo]   │  │
│  │  ├─ _message_factories: List[FactoryInfo]         │  │
│  │  ├─ _floating_cards: Dict[str, CardInfo]          │  │
│  │  ├─ _welcome_tabs: Dict[str, WelcomeTabInfo]      │  │
│  │  ├─ load_plugin() / unload_plugin()               │  │
│  │  └─ _show_floating_card() → 创建/切换卡片         │  │
│  └───────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │ 依赖注入
                       ▼
┌─────────────────────────────────────────────────────────┐
│  app/main_widget.py                                      │
│  ├─ _card_manager (CardManager)                         │
│  ├─ 浮动卡片容器（BottomCardContainer / TopCardContainer）│
│  └─ _load_all_ui_plugins() — 启动时批量加载              │
└─────────────────────────────────────────────────────────┘
```

---

## 二、三种组件类型

### 2.1 浮动卡片（FloatingCard）

**用途**：像系统设置卡片一样，在聊天界面下方/上方弹出的独立面板。
**注册**：`registry.register_floating_card(...)` → 自动注册命令 `/card-id`
**容器**：
- `container="bottom"`：显示在聊天下方，隐藏输入区（与系统配置卡片一致）
- `container="top"`：显示在聊天上方（较少使用）
- `container="left"` / `container="right"`：停靠在 Tab 窗口左右侧停靠区（类似 IDE 侧边栏）
- `container="full"`：完整覆盖对话区（与系统配置卡片一致，走覆盖层）

**生命周期**：
1. 用户输入 `/card-id` → CommandCard 显示 → 回车执行
2. `_show_floating_card(card_id)` → 创建 widget 实例（per-window 缓存） → 注入上下文 → 加入容器布局
3. 调用 `card_manager.toggle_card()` → 显示/隐藏
4. 关闭时发射 `closed` 信号 → CardManager 同步状态

### 2.2 内容块渲染器（ContentRenderer）

**用途**：在消息卡片内以 HTML 渲染自定义内容。
**注册**：`registry.register_content_renderer(...)`
**调用链**：
```
AI 工具返回 {"type": "custom", "custom_type": "xxx", "data": {...}}
  → ensure_content_blocks() 识别 custom 块
  → content_to_markdown() 调用注册渲染器 → 返回 HTML
  → MessageCard 注入到 WebView
```

### 2.3 消息元素工厂（MessageFactory）

**用途**：替换整个消息气泡 widget（高级用法）。
**注册**：`registry.register_message_factory(...)`
**调用链**：
```
消息到达 → 遍历工厂列表（按 priority 降序）
  → condition_func(message) 判断是否匹配
  → factory_func(message, parent) 创建 widget
  → 失败则继续尝试下一个工厂，都不匹配走默认 MessageCard
```

### 2.4 欢迎卡片插件 tab（WelcomeTab）

**用途**：在欢迎卡片（会话初始卡片）新增一个 tab，内容为纯 HTML 片段。
**注册**：`registry.register_welcome_tab(plugin_name, mode_key, label, render_func)`
**调用链**：
```
欢迎卡片构建 → _build_welcome_mode_tabs() 把插件 tab 追加到 SegmentedWidget
  → 点击 tab → set_welcome_mode(mode) → _render_welcome_body(mode)
  → 命中插件 tab → render_func(ctx) 返回 HTML 片段
  → 拼进 "### 👋 greeting\n\n{body}" → markdown 管线 → CodeWebViewer innerHTML 注入
```

**关键约束**：innerHTML 注入的 `<script>` 不执行 → 内容 Python 预渲染、
交互用 onclick 内联 JS；`<style>` 注入生效 → 样式内联；明暗用
`prefers-color-scheme` 媒体查询（拿不到 Qt 主题）。完整模板见 `templates.md §八`。

---

## 三、关键文件速查

| 文件 | 职责 |
|------|------|
| `app/core/ui_plugin_registry.py` | 注册表单例 + 生命周期管理 |
| `app/core/plugin_manager.py` | 插件扫描 + `ui` 组件自动检测 |
| `app/widgets/cards/card_container.py` | CardContainer + 展开/折叠动画 |
| `app/widgets/cards/card_manager.py` | CardManager 卡片状态管理 |
| `app/main_widget.py` | `_load_all_ui_plugins()` 批量加载 + 依赖注入 |
| `app/core/message_content.py` | `ensure_content_blocks()` + `content_to_markdown()` |

---

## 四、上下文注入（拉模型）

```
UIPluginRegistry 为每个插件卡片创建 context_provider 闭包
  ↓ set_context_provider(provider)
卡片保存 provider 引用
  ↓ 显示时自行调用 provider()
卡片获取最新上下文 dict
  ↓ 刷新样式
```

上下文 dict 包含：
- `colors`: `{text_primary, text_secondary, border, accent, accent_warm, card_bg, scrollbar_handle, scrollbar_handle_hover, input_placeholder, ...}`
- `is_dark`: bool
- `font_family`: str
- `font_size`: int
- `project_root`: str
- `project_name`: str
- `session_id`: str
- `plugin_name`: str
- `card_id`: str

---

## 五、插件生命周期

```
PluginManager.initialize()
  → 扫描所有插件 → 识别 components.ui = true
  → 收集所有启用插件的 (name, path)
  → UIPluginRegistry.load_all_enabled_plugins()
    → 遍历每个插件
      → load_plugin(name, path)
        → import ui_plugin_{name}.ui.__init__
        → 调用 register_ui(registry)
        → 插件注册三种组件
        → 浮动卡片 → 自动注册 FUNCTION 命令

插件禁用/卸载 → unload_plugin(name)
  → 清理注册表
  → 移除浮动卡片 widget 实例
  → 注销命令
```

---

## 六、现有插件参考

| 插件 | 组件 | 学习要点 |
|------|------|---------|
| `plugin-marketplace` | FloatingCard + ContentRenderer | 远程 HTTP 数据、HTML 渲染器、安装/启用/禁用/卸载管理 |
| `context-usage-stats` | FloatingCard | SQLite 数据、自定义图表 QPainter、多图表组合 |
| `calendar` | WelcomeTab | HTML 注入会话初始卡片、onclick 内联 JS、prefers-color-scheme 明暗适配 |
