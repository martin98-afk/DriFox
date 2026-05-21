# 多窗口分支选择隔离修复

**日期**: 2026-05-21  
**状态**: 已实施  
**影响范围**: `app/main_widget.py`, `app/widgets/memory_card.py`, `app/core/memory_manager.py`, `app/core/backend.py`, `app/core/tool_executor.py`

## 1. 问题

多窗口打开同一项目时，选择不同 worktree/分支后互相覆盖：

1. 窗口 A 选择 worktree-A → 写入 SQLite `is_working_dir = 1`
2. 窗口 B 选择 worktree-B → 覆盖 SQLite `is_working_dir = 1`
3. 任何一个窗口刷新/重建 UI 时，从 SQLite 读取，都读到 worktree-B（最后写入的）
4. 结果：两个窗口最终都显示同一路径，而非各自的选择

项目选择（project selector）存在类似潜在问题，但分析后发现 `self._current_project` 已是实例变量，核心问题在于 workdir。

## 2. 目标

- **实例优先**：每个窗口使用内存中的分支选择，不受其他窗口写入影响
- **持久化仅用于恢复**：DB/配置写入只用于"新窗口打开时的默认值"
- **与模型隔离修复模式一致**：沿用 `_load_model_configs` 的实例优先模式

## 3. 方案

### 方案 A：实例缓存优先 + 全局写入延迟（已选）

每个窗口实例持有 `_current_workdir: Dict[str, str]`（按 project 缓存），读取时实例缓存优先，写入时实例缓存与 DB 同步写入。

## 4. 修改详情

### 4.1 `app/main_widget.py`

#### 4.1.1 新增实例级缓存

在 `__init__` 中初始化：
```python
self._current_workdir: Dict[str, str] = {}  # {project: workdir_path} 实例级缓存
```

#### 4.1.2 修改 `_sync_working_directory`

**修改前**：纯从 DB 读取
```python
def _sync_working_directory(self):
    ...
    workdir = None
    if self.backend.memory_manager:
        workdir = self.backend.memory_manager.get_working_directory(project)
    self.backend.tool_executor.set_workdir(workdir)
```

**修改后**：实例缓存优先，首次回退 DB
```python
def _sync_working_directory(self):
    ...
    # 实例缓存优先（多窗口隔离的关键：保持自身选择）
    workdir = self._current_workdir.get(project)
    if workdir is None:
        # 首次启动，从 DB 读取默认值（新窗口恢复用）
        if self.backend.memory_manager:
            workdir = self.backend.memory_manager.get_working_directory(project)
    if workdir:
        self._current_workdir[project] = workdir
    self.backend.tool_executor.set_workdir(workdir or None)
```

#### 4.1.3 修改 `_on_working_dir_changed`

**修改前**：仅同步到 tool_executor
```python
def _on_working_dir_changed(self, file_path: str):
    if self.backend and self.backend.tool_executor:
        self.backend.tool_executor.set_workdir(file_path or None)
    self._update_branch()
```

**修改后**：更新实例缓存 + 同步到 tool_executor
```python
def _on_working_dir_changed(self, file_path: str):
    # 更新实例缓存（多窗口隔离：每个窗口独立持有自己的 workdir）
    if file_path:
        self._current_workdir[self._current_project] = file_path
    else:
        self._current_workdir.pop(self._current_project, None)
    # 同步到 tool_executor
    if self.backend and self.backend.tool_executor:
        self.backend.tool_executor.set_workdir(file_path or None)
    self._update_branch()
```

### 4.2 `app/widgets/memory_card.py`

#### 4.2.1 `_on_worktree_changed` — 写入 DB 的注释更新

**修改前**：
```python
def _on_worktree_changed(self, original_folder: str, worktree_path: str):
    """Worktree 切换：写入 DB + 切换 workdir（UI 层过滤不显示 git_worktree 条目）"""
    memory_mgr = self._get_memory_manager()
    if not memory_mgr:
        return
    # 必须写入 DB，否则 set_working_directory 找不到路径
    # added_by="git_worktree" 标记，UI 显示时过滤掉
    memory_mgr.add_key_document(self._current_project, worktree_path, "git_worktree")
    # 设为工作目录
    memory_mgr.set_working_directory(self._current_project, worktree_path)
    self.workingDirChanged.emit(worktree_path)
    self._load_key_documents()
```

**修改后**：注释说明 DB 写入的目的（新窗口默认值）
```python
def _on_worktree_changed(self, original_folder: str, worktree_path: str):
    """Worktree 切换：写入 DB（新窗口默认值）+ 切换 workdir（UI 层过滤不显示 git_worktree 条目）
    
    多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
    当前窗口通过 workingDirChanged 信号通知 main_widget 更新实例缓存。
    """
    memory_mgr = self._get_memory_manager()
    if not memory_mgr:
        return
    # 写入 DB（新窗口默认值 + worktree 路径记录）
    # added_by="git_worktree" 标记，UI 显示时过滤掉
    memory_mgr.add_key_document(self._current_project, worktree_path, "git_worktree")
    # 设为工作目录（DB 写入：新窗口恢复用）
    memory_mgr.set_working_directory(self._current_project, worktree_path)
    # 通知 main_widget 更新实例缓存 + tool_executor
    self.workingDirChanged.emit(worktree_path)
    self._load_key_documents()
```

#### 4.2.2 `_set_as_working_directory` — 同样更新注释

**修改后**：注释说明 DB 写入目的
```python
def _set_as_working_directory(self, file_path: str):
    """设置为工作目录（再次点击取消）
    
    多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
    当前窗口通过 workingDirChanged 信号通知 main_widget 更新实例缓存。
    """
    memory_mgr = self._get_memory_manager()
    if not memory_mgr:
        return
    current_wd = memory_mgr.get_working_directory(self._current_project)
    if current_wd == file_path:
        memory_mgr.set_working_directory(self._current_project, "clear")
        self.workingDirChanged.emit("")
    else:
        memory_mgr.set_working_directory(self._current_project, file_path)
        self.workingDirChanged.emit(file_path)
    self._load_key_documents()
```

### 4.3 `_on_worktree_deleted` — 同样更新注释

当 worktree 被删除时，需要同步清除实例缓存：
```python
def _on_worktree_deleted(self, worktree_path: str):
    """Worktree 删除：清理 DB + 实例缓存"""
    memory_mgr = self._get_memory_manager()
    if not memory_mgr:
        return
    current_wd = memory_mgr.get_working_directory(self._current_project)
    memory_mgr._key_documents_repo.remove_by_path(self._current_project, worktree_path)
    if current_wd == worktree_path:
        # 切回原始仓库目录
        repo_root = self._original_folder_for_worktree
        if repo_root:
            memory_mgr.set_working_directory(self._current_project, repo_root)
            self.workingDirChanged.emit(repo_root)
        else:
            memory_mgr.set_working_directory(self._current_project, "clear")
            self.workingDirChanged.emit("")
    self._load_key_documents()
```

> 这里不需要额外改 `main_widget`，因为 `workingDirChanged` 信号已经连接到 `_on_working_dir_changed`，新增的逻辑会自动处理实例缓存清除。

## 5. 项目选择分析

项目选择（`_current_project`）天然是实例级变量，运行时切换不会受其他窗口影响。`cfg.current_project.value = project` 写入全局配置的行为是正确的——它只影响"下次新开窗口的默认项目"。但需确保：

- 所有 `self._current_project` 的读取都来自实例变量
- 不存在从 `cfg.current_project.value` 重新初始化 `_current_project` 的路径（除了 `__init__`）

**结论**：项目选择不需要额外修改。

## 6. 与模型隔离修复的对齐

| 隔离项 | 实例缓存 | DB/配置写入 | 优先级 | 状态 |
|--------|----------|-------------|--------|------|
| 模型选择 | `_current_provider_name/_current_model_name` | `cfg.llm_selected_model` | 实例 > DB | ✅ 已修复 |
| 工作目录 | `_current_workdir[project]` | SQLite `is_working_dir` | 实例 > DB | ✅ 已修复 |
| 工作目录(memory_card) | `_instance_workdir[project]` | SQLite `is_working_dir` | 实例 > DB | ✅ 已修复 |
| 长期记忆注入 | `tool_executor.get_workdir()` | SQLite `is_working_dir` | 实例 > DB | ✅ 已修复 |
| 项目选择 | `_current_project` | `cfg.current_project` | 实例 > cfg | ✅ 天然隔离 |

## 7. 测试要点

1. **多窗口独立切换**：开两个窗口选同一项目 → 窗口A切到 branch-A、窗口B切到 branch-B → 各自显示正确
2. **刷新不丢失**：窗口A在刷新关键文档后仍显示 branch-A（不受窗口B DB 写入影响）
3. **新窗口恢复**：关闭所有窗口后重开 → 恢复到最后操作的 workdir
4. **Worktree 删除**：删除当前 worktree → 回退到仓库根目录 → 实例缓存同步清除
5. **新建窗口继承**：窗口A选择 branch-A 后新建窗口 → 新窗口默认继承 branch-A

## 8. 长期记忆注入提示词隔离（补充修复）

### 8.1 问题

`format_memories_for_prompt` 内部通过 `self.get_working_directory(project)` 直接读 DB 获取工作目录，
用于标注关键文档中的"项目根目录"标记和 Worktree 上下文信息。

多窗口场景下，这个 DB 读取也会受到其他窗口写入的影响。

### 8.2 修改

| 文件 | 修改点 | 说明 |
|------|--------|------|
| `app/core/memory_manager.py` | `format_memories_for_prompt` 新增 `workdir_override` 参数 | 如果提供则替代 DB 读取 |
| `app/core/backend.py` | `get_memory_context_string` 传入 `tool_executor.get_workdir()` | 使用实例级 workdir |
| `app/core/backend.py` | `_build_memory_context` 传入 `tool_executor.get_workdir()` | 使用实例级 workdir |
| `app/core/tool_executor.py` | 新增 `get_workdir()` 方法 | 公开获取实例级 workdir |

### 8.3 数据流

```
ChatEngine 构建 system prompt:
  → context_builder.get_memory_context_string()
  → backend.get_memory_context_string()
  → tool_executor.get_workdir()  ← 实例级值（已被 _on_working_dir_changed 同步）
  → memory_manager.format_memories_for_prompt(workdir_override=workdir)
  → 如果 workdir_override 不为 None，使用它而非 DB 值
```