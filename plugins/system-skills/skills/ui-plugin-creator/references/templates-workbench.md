# UI 插件代码模板 — 右侧工作台页（workbench_tab）

> 何时读：在右侧工作台加常驻内容页时；先读 §11.3 分清 workbench_tab vs workspace_page。
> 前置依赖：architecture.md（UI 架构总览）。
> 产出：register_workbench_tab 注册 + 页面 widget。

## 十一、右侧工作台页模板（workbench_tab）

> 参考实现：`plugins/system-ui/ui/__init__.py` + `_worktree_page.py`（工作树）、`_artifacts_page.py`（产物）。
> 适配场景：插件要一个**常驻内容页**挂在右侧工作台（Tab 侧栏切换的全页内容），
> 与"点按钮弹出的浮动卡"不同——工作台页由面板槽位管理。

### 11.1 注册

```python
def register_ui(registry) -> None:
    try:
        from ._my_page import MyWorkbenchPage

        registry.register_workbench_tab(
            plugin_name=PLUGIN_NAME,
            page_id="my_page",          # 同 page_id 高优先级覆盖；"worktree"/"artifacts" 是保留槽位 id
            label="我的页面",
            widget_class=MyWorkbenchPage,   # QWidget 子类，懒实例化
            priority=10,
            metadata={"source": "system"},
        )
        logger.info(f"[{PLUGIN_NAME}] 已注册工作台 tab")
    except Exception as e:  # noqa: BLE001
        # 降级：注册失败只影响本页（面板显示占位页），不拖垮其他组件
        logger.warning(f"[{PLUGIN_NAME}] 注册工作台 tab 失败: {e}")
```

### 11.2 页面 widget 约定（参考 _worktree_page.py）

- widget_class 是普通 QWidget 子类，**懒实例化**：构造期别做重 IO（文件扫描/网络），
  数据读取放首次 show 后或异步 worker（`patterns.md` §3）。
- 宿主上下文注入：`context["backend"]`（ChatBackend 门面）、
  `context["working_dir_changed_callback"]`（工作目录变更上报）等，构造参数收 `context`。
- 主题样式用 `app.utils.design_tokens.Colors` + `get_font_family_css()`（同卡片规范）。
- 卸载后槽位显示占位页（主程序行为），插件无需自己清理槽位。

### 11.3 workbench_tab vs workspace_page（别选错扩展点）

| | `register_workbench_tab` | `register_workspace_page` |
|---|---|---|
| 落位 | 右侧工作台 tab 面板（Tab 侧栏切换） | 工作区页面槽（WorkspacePage） |
| 保留 id | `worktree` / `artifacts` | 无保留 id，order_hint 升序排布 |
| 图标 | 无（label 文字） | icon_path + icon_light_path 深浅两套 |
| 覆盖规则 | 同 page_id 高优先级覆盖 | 同 page_id 后注册覆盖 |

### 11.4 验证清单

```
1. 页出现在右侧工作台，label 正确？            → register_workbench_tab
2. 切到页时无卡顿（数据未阻塞构造）？          → 懒加载/异步（patterns.md §3）
3. 插件卸载后槽位显示占位页，无崩溃？          → 主程序行为，注册失败降级
4. 保留槽位 id 冲突时优先级行为正确？          → worktree/artifacts 同 id 覆盖规则
```

> 完整验证清单见 `checklist.md §14.2`；其他低频扩展点速查表见 `checklist.md §14.3`。
