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

系统模块源码在**主程序** `app/widgets/modules/`（不是插件目录），由
`app/main_widget.py:_register_system_ui_modules()` 注册、`app/widgets/ui_composition.py`
装配。产物属性清单（setattr 挂回 host 的真实集合）：

| module_id | 源码文件 | 职责 | 产物属性（完整 setattr 清单） |
|---|---|---|---|
| `title_bar` | `app/widgets/modules/title_bar_module.py` | 标题栏（项目/分支/标题/余额/编码计划环/上下文用量/问答/分享） | `diff_btn`(兼容占位 None) `_project_branch_container` `_project_avatar` `_project_label` `_branch_widget` `title_edit` `balance_display` `coding_plan_ring` `_coding_plan_hidden` `context_usage_ring` `_history_questions_btn` `_history_questions_badge` `_share_btn` |
| `chat_area` | `app/widgets/modules/chat_area_module.py` | 对话滚动区 + 消息容器 + 装饰层 | `chat_scroll_area` `chat_container` `chat_layout` `_decoration_layer` `_apply_decorations` `_scroll_to_bottom_button`（`_top_card_container`/`_bottom_card_container` 由 setup_ui 头部预创建，**非**本模块产物） |
| `system_cards` | `app/widgets/modules/system_cards_module.py` | 六张系统卡懒创建 + 项目选择卡 | `_history_card` `_history_popup_card` `_share_card` `_share_card_content` `_history_questions_card` `_history_questions_card_content` `_memory_card` `_memory_card_popup` `_model_config_card` `_model_config_popup` `_model_selector_card` `_model_selector_card_content` `_tool_control_card` `_project_selector_card` `_project_selector_card_content` `_project_new_edit` `_project_new_btn` `_project_open_folder_btn` `_project_import_btn` `_question_floating_widget` |
| `input_card` | `app/widgets/modules/input_card_module.py` | 输入卡/附件区/命令三卡 | `_bottom_input_container` `_bottom_input_layout` `_input_card` `_input_card_wrapper` `_attach_container` `_attach_layout` `input_area` `_command_card` `_file_mention_card` `_undo_delete_card` `_undo_delete_cache` `_truncation_sentinel` `_pending_send_after_truncation` `_pending_send_user_text`（无 `_attachments`/`_history_working_attachments`——原文档有误） |
| `bottom_toolbar` | `app/widgets/modules/bottom_toolbar_module.py` | 底部工具栏（模型选择/工具切换/记忆/历史/新会话等） | `_bottom_toolbar_strip` `_model_btn_container` `_model_sep_name` `_model_sep_usage` `current_model_btn` `_model_btn_icon` `_model_btn_text` `settings_btn` `_settings_btn_icon` `effort_btn` `_settings_effort_label` `_current_provider_name` `_current_model_name` `_user_manually_selected_model` `_tool_toggle_btn` `_tool_danger_label` `_tool_safe_label` `_tool_restore_btn` `_toolbar_capsule` `memory_btn`(None) `history_btn`(None) `new_session_btn` `_plugin_input_buttons` `_input_glow_underlay` `_input_card_primary_shadow` `_input_card_ambient_shadow` `_bottom_toolbar_shadow` `_input_card_focused` `_input_area_collapsed` |

> ⚠️ 覆盖 `input_card` 插件时 `input_area` 必须保持 `SendableTextEdit` 兼容接口
> （`sendMessageRequested`/`stopMessageRequested` 信号等），宿主其余代码依赖它。

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
    # 真实签名: register_ui_module(module_id, factory, plugin_name="system", priority=0)
    # priority >= 100 覆盖系统 priority=0；同 priority 后注册胜
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
  ├─ _register_system_ui_modules()   # 注册 5 个系统模块（app/main_widget.py）
  └─ compose(host=self, module_ids=_SYSTEM_MODULE_ORDER)
       ├─ title_bar      → TitleBarModule.build（app/widgets/modules/title_bar_module.py）
       ├─ chat_area      → ChatAreaModule.build（app/widgets/modules/chat_area_module.py）
       ├─ system_cards   → SystemCardsModule.build（app/widgets/modules/system_cards_module.py）
       ├─ input_card     → InputCardModule.build（app/widgets/modules/input_card_module.py）← 插件可 override
       └─ bottom_toolbar → BottomToolbarModule.build（app/widgets/modules/bottom_toolbar_module.py）
```

页面级（Phase G）独立于本路径：`WorkspacePageHost.attach_to(tab_window)` 在 `TabManagerWindow._setup_ui` 末尾挂载，挂到 `_content_area`（QStackedWidget）。