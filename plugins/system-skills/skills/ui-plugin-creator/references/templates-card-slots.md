# UI 插件代码模板 — 消息卡片槽位（footer_action / footer_stat）

> 何时读：要在**消息卡片上加按钮**（助手页脚 or 用户气泡底部操作行），
> 或在页脚**注入一行信息**（字数/耗时/状态统计等）时。
> 前置依赖：architecture.md（UI 架构总览）。
> 产出：`register_footer_action` / `register_footer_stat` 注册。

## 十六、消息卡片槽位模板（footer_action / footer_stat）

> 参考实现：`plugins/ui-slots-demo/ui/__init__.py`（role=user/both 按钮 + 字数信息项）、
> `plugins/agent_trace/ui/__init__.py`（footer_stat 注入会话平均吞吐量）。
> 适配场景：给单条消息加操作（转发/引用/收藏/润色）或补充统计信息。

### 16.1 两个扩展点分工

| 扩展点 | 位置 | 形态 |
|--------|------|------|
| `register_footer_action` | 助手卡片页脚按钮组 与/或 用户气泡底部操作行 | 20px（助手）/ 26px（用户）hover 按钮，与内置按钮同排同风格 |
| `register_footer_stat` | 助手卡片页脚左区（耗时右侧，`·` 分隔） | 一行纯文本信息项（可带颜色与 tooltip） |

> ⚠️ 只作用于 **assistant**（stat）或按 role 分流（action）；user 卡片页脚无 stat 槽位。

### 16.2 register_footer_action（按钮）

```python
registry.register_footer_action(
    plugin_name=PLUGIN_NAME,
    action_id=f"{PLUGIN_NAME}:quote",   # 唯一 ID
    icon_path=_ICON_DARK,                # 深色主题图标
    icon_light_path=_ICON_LIGHT,         # 浅色主题图标（主题切换主程序自动刷新）
    tooltip="引用到工具窗",
    role="user",                         # "assistant"（默认）| "user" | "both"
    on_click=_on_click,                  # callback(ctx)
    priority=0,
)
```

**role 分流**：

- `"assistant"`（默认）→ 助手卡片页脚按钮组（与内置分支/复制同排，20px）
- `"user"` → 用户消息气泡底部操作行，**位于内置复制/撤销/删除左侧**（26px，hover 随容器浮现）
- `"both"` → 两端都渲染

**on_click(ctx) 的 ctx 含**：`card`（MessageCard 实例）/ `role` / `message_index` /
`round_index` / `window_id` / `main_widget` / `model_name` 等。

```python
def _on_click(ctx):
    card = ctx.get("card")
    text = card.get_plain_text() if card is not None else ""   # 消息纯文本
    ...
```

### 16.3 register_footer_stat（信息项）

```python
registry.register_footer_stat(
    plugin_name=PLUGIN_NAME,
    stat_id=f"{PLUGIN_NAME}:char-count",
    provider=_stat_provider,   # callback(ctx) -> Optional[{"text", "color", "tooltip"}]
    priority=0,
)

def _stat_provider(ctx):
    try:
        n = len(ctx["card"].get_plain_text() or "")
    except Exception:
        return None
    if n <= 0:
        return None            # None = 本条消息不显示（分隔点自动收敛）
    return {"text": f"{n} 字", "color": None, "tooltip": "本条消息字数"}
```

**约束与时机**：

- provider 在**主线程**被调用，要求**纯内存快速计算**（不得阻塞、不得弹 UI）
- 刷新时机：卡片构建 / 回合落定 / 流式每秒节拍（`streaming=True` 时 ctx 附带
  `live_text`（本流累计原文）与 `live_gen_s`（首字至今秒数），适合做实时速率类信息；
  token 估算口径由 provider 自定，主程序不做 chars÷4 粗估）
- 返回 `color=None` 时用默认 muted 色；文本走纯文本渲染（`<` `&` 不会被当标记）

### 16.4 联动示例（按钮 → 独立弹窗）

```python
def _on_user_button(ctx):
    """用户气泡按钮：把消息内容送到独立弹窗展示"""
    text = ""
    card = ctx.get("card")
    if card is not None:
        text = card.get_plain_text() or ""
    win = UIPluginRegistry.get_instance().open_window(f"{PLUGIN_NAME}:tool")
    page = getattr(win, "_content", None) if win is not None else None
    if page is not None and hasattr(page, "set_last_message"):
        page.set_last_message(text)
```

（弹窗侧模板见 `templates-window.md`。）

### 16.5 卸载与兼容

- 卸载幂等：`unload_plugin` 按 `plugin_name` 自动清理两类注册，无需插件侧额外处理
- 已构建的卡片**不感知注册变化**（构建时快照）：热重载后需新消息/重渲染才看到新按钮
- 旧主程序兼容：`role` 参数是 2026-09-17 新增，旧版本不接受该 kwarg——
  用 `inspect.signature(registry.register_footer_action).parameters` 检测降级
  （与 `register_input_button` 的 on_right_click 兼容模式同款）

### 16.6 验证清单

见 `checklist.md §15`（独立弹窗 / 卡片槽位验证）。
