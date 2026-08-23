# UI 模块级扩展（Phase F 二期）

> DriFox UI 灵活性三层模型的中间层——模块级（UIModule）。本文件覆盖 UIModule 契约、五个系统模块、`UIComposition.compose` 装配器，以及插件 override 指南。

---

## 1. 三层模型总览

| 层级 | 计划 | 文档 |
|---|---|---|
| 条目级 | Phase E（一期） | [ui-slots.md](./ui-slots.md) |
| **模块级** | **Phase F（二期，本文件）** | — |
| 页面级 | Phase G（三期） | [ui-workspace.md](./ui-workspace.md) |

---

## 2. UIModule 契约

```python
# app/plugins/contracts/ui_module.py
class UIModule:
    module_id: str = ""  # 子类必填

    def build(self, host: Any) -> None:
        """构建模块 UI。所有产物 setattr(host, <原属性名>, <widget>)

        Args:
            host: 宿主窗口（实现 IWindowHost 协议或鸭子属性访问）。
                  根布局经 host.layout() 获取（首个模块 build 前需已创建）。
        """
        raise NotImplementedError

    def teardown(self, host: Any) -> None:
        """销毁模块产物（默认空：Qt 父子树随窗口销毁；有外部资源才需实现）"""
```

### 铁律

- **build 产物属性必须 setattr 挂回 host**（与原 setup_ui 同名）
- 宿主类其余代码靠属性访问，**属性名变更 = 破坏性重构，禁止**
- 局部 layout / 临时变量保持局部（不挂 host）

---

## 3. 五个系统模块

| module_id | 职责 | 产物属性（节选） |
|---|---|---|
| `title_bar` | 标题栏 + session_bar_layout | `_session_bar` `session_bar_layout` `project_btn` `branch_btn` `title_edit` `_session_right_buttons` `_model_btn_container` 等 9 属性 |
| `chat_area` | 对话滚动区 + 上下卡容器 | `_top_card_container` `_bottom_card_container` `chat_scroll_area` `chat_container` `chat_layout` |
| `system_cards` | 六张系统卡懒创建 + 项目选择卡 | `_tool_control_card` `_project_selector_card` `_project_selector_card_content` + 各卡引用 |
| `input_card` | 输入卡/附件区/命令三卡 | `_bottom_input_container` `_input_card` `_input_card_wrapper` `_attach_container` `_attach_layout` `input_area` `_command_card` `_file_mention_card` `_undo_delete_card` `_attachments` `_history_working_attachments` 等 15 属性 |
| `bottom_toolbar` | 底部工具栏（模型/记忆/历史/新会话/工具切换等） | `_bottom_toolbar_strip` `_model_btn_container` `current_model_btn` `settings_btn` `effort_btn` `_tool_toggle_btn` `_toolbar_capsule` `memory_btn` `history_btn` `new_session_btn` `_input_glow_underlay` 等 20 属性 |

属性契约表来源：各模块 Task 步骤中 `grep self.<attr>` 提取。

---

## 4. UIComposition.compose

```python
# app/widgets/ui_composition.py
def compose(
    host: Any,
    module_ids: List[str],
    root_layout_factory: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Optional[str]]:
    """按 module_ids 顺序装配 UIModule 到 host

    Returns:
        {module_id: 状态}——"system" / 插件名 / "failed" / 缺失时 None
    """
```

### 装配顺序

```python
_SYSTEM_MODULE_ORDER = ["title_bar", "chat_area", "system_cards", "input_card", "bottom_toolbar"]
```

### 失败隔离

单模块 `build()` 抛异常不影响其他模块——记 `logger.error` 并标记 `failed`。

### 根布局

主程序路径根布局已在 `setup_ui` 头部建好，传 `lambda h: None` 跳过根创建。

---

## 5. 插件 override 指南

### 5.1 注册

```python
def register_ui(registry):
    # priority >= 100 覆盖系统 priority=0
    registry.register_ui_module(
        "input_card",
        MyCustomInputCardModule,  # 类引用（factory 在 get_ui_module 时实例化）
        plugin_name="my-plugin",
        priority=100,
    )
```

### 5.2 完整示例（覆盖 input_card）

```python
# plugins/my-plugin/ui/__init__.py
from app.plugins.contracts.ui_module import UIModule


class MyCustomInputCardModule(UIModule):
    module_id = "input_card"

    def build(self, host) -> None:
        from PyQt5.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

        # 产物属性挂回 host（与系统默认同名：input_area 等）
        container = QWidget(host)
        lay = QVBoxLayout(container)
        host._bottom_input_container = container

        # 自定义输入框
        host.input_area = MyCustomTextEdit(container)
        lay.addWidget(host.input_area)
        # 关键：暴露与系统一致 signal 接口（sendMessageRequested/stopMessageRequested 等）
```

### 5.3 卸载自动回退

```python
# 插件卸载时
registry.unload_plugin("my-plugin")
# → register_ui_module("input_card", MyCustomInputCardModule, priority=100) 被清理
# → 重新 get_ui_module("input_card") 返回系统 InputCardModule（priority=0）
# → 下次窗口 setup_ui 自动恢复系统默认
```

### 5.4 优先级规则

- `SYSTEM_MODULE_PRIORITY = 0`（系统基线）
- 插件 `priority >= 100` 覆盖系统
- 同 priority 后注册胜（索引 tiebreaker）

---

## 6. 编写自定义模块检查清单

- [ ] 属性契约：grep 系统模块 `host.<attr>` 列表，确保子类 setattr 全部覆盖
- [ ] qapp 测试模板：

  ```python
  from PyQt5.QtWidgets import QVBoxLayout, QWidget
  from app.widgets.ui_composition import compose

  def test_module_contract(qapp, fresh_registry):
      host = QWidget()
      host.setLayout(QVBoxLayout(host))
      fresh_registry.register_ui_module("input_card", MyModule, plugin_name="test")
      compose(host, ["input_card"])
      for attr in ("_bottom_input_container", "input_area", ...):
          assert hasattr(host, attr)
  ```

- [ ] 启动冒烟点检表：
  - [ ] 对话发送
  - [ ] 主题切换
  - [ ] 设置弹窗
  - [ ] 浮动卡开关
  - [ ] 插件按钮
  - [ ] 热重载（改插件文件触发）

---

## 7. 一期 → 二期 → 三期 集成

```
setup_ui 入口
  ├─ 根 QVBoxLayout（主程序）
  ├─ _register_system_ui_modules()   # 注册 5 个系统模块
  └─ compose(host=self, module_ids=_SYSTEM_MODULE_ORDER)
       ├─ title_bar      → TitleBarModule.build
       ├─ chat_area      → ChatAreaModule.build
       ├─ system_cards   → SystemCardsModule.build
       ├─ input_card     → InputCardModule.build  ← 插件可 override
       └─ bottom_toolbar → BottomToolbarModule.build
```

页面级（Phase G）独立于本路径：`WorkspacePageHost.attach_to(tab_window)` 在 `TabManagerWindow._setup_ui` 末尾挂载，挂到 `_content_area`（QStackedWidget）。