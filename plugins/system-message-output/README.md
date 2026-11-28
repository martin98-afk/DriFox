# 流式输出方式

在「设置 → 插件 → 流式输出方式」选择简洁或原始。首次使用继承旧简洁开关（新安装默认简洁），选择保存到插件配置 `mode_id`。新卡片捕获当前方式；进行中的卡片和它重新绑定的池化视图不会改变。选中的插件不可用时展示原始模式，不删除保存的选择。

## 为列表添加一种方式

在 UI 插件的 `ui/__init__.py` 中注册，manifest 声明 `components.ui: true`。方式 ID 须唯一，`original` 是宿主回退方式的保留 ID。

```python
def tool_groups(kinds, *, streaming):
    for i, kind in enumerate(kinds):
        if kind == 'tool':
            yield i, i + 1, False


def register_ui(registry):
    registry.register_output_mode('tools-only', '只折叠工具', '工具单项折叠，思考保持可见。',
                                  groups=tool_groups, plugin_name='tools-only')
```

输入 `kinds` 按消息顺序排列（`reasoning`、`tool`、`body`、`blank`）。策略只返回 `(起点, 终点, 默认展开)`，区间左闭右开，升序且不能重叠。注册参数 `css` 可选，用于定义摘要按钮的外观；宿主负责键盘交互和稳定节点。注册不会自动选中方式。插件卸载后，注册表按 `plugin_name` 清理方式，新消息自动回退到原始展示。
