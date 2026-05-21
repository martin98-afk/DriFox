# 多窗口分支选择隔离修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复多窗口同项目选择不同分支时的冲突，使实例内存选择优先于全局 DB 配置。

**Architecture:** 在 `main_widget.py` 中新增 `_current_workdir: Dict[str, str]` 实例级缓存，读取时实例优先/首次回退 DB，写入时同步更新实例缓存与 DB（DB 仅作为新窗口默认值）。与已修复的模型选择隔离模式完全一致。

**Tech Stack:** PyQt5, SQLite, Python 3.10+

**Spec:** `docs/superpowers/specs/2026-05-21-multi-window-isolation-fix.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/main_widget.py` | Modify | 新增 `_current_workdir` 实例缓存，修改 `_sync_working_directory` 和 `_on_working_dir_changed` |
| `app/widgets/memory_card.py` | Modify | 更新 `_on_worktree_changed`、`_set_as_working_directory`、`_on_worktree_deleted` 的注释 |

---

### Task 1: main_widget.py — 新增实例级工作目录缓存

**Files:**
- Modify: `app/main_widget.py:178` (在 `_current_project` 初始化后)
- Modify: `app/main_widget.py:5870-5893` (`_on_working_dir_changed` 和 `_sync_working_directory`)

- [ ] **Step 1: 在 `__init__` 中初始化 `_current_workdir`**

在 `app/main_widget.py` 第 178 行 `self._current_project = ...` 之后，添加：

```python
        self._current_project = self.cfg.current_project.value or "默认项目"  # 当前项目
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        # 优先级：实例缓存 > DB；DB 写入仅作为新窗口的默认恢复值
        self._current_workdir: Dict[str, str] = {}
```

- [ ] **Step 2: 修改 `_on_working_dir_changed` — 更新实例缓存**

将 `_on_working_dir_changed`（约第 5870 行）从：

```python
    def _on_working_dir_changed(self, file_path: str):
        """工作目录变更 → 同步到工具执行器 + 刷新分支标签"""
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(file_path or None)
            from loguru import logger
            logger.info(f"[MainWidget] Working directory synced to tool executor: {file_path or 'default'}")
        # 工作目录变更后刷新分支标签
        self._update_branch()
```

改为：

```python
    def _on_working_dir_changed(self, file_path: str):
        """工作目录变更 → 更新实例缓存 + 同步到工具执行器 + 刷新分支标签
        
        多窗口隔离：更新实例缓存（关键！），DB 写入在 memory_card 层已完成。
        """
        # 更新实例缓存（多窗口隔离：每个窗口独立持有自己的 workdir）
        if file_path:
            self._current_workdir[self._current_project] = file_path
        else:
            self._current_workdir.pop(self._current_project, None)
        # 同步到工具执行器
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(file_path or None)
            from loguru import logger
            logger.info(f"[MainWidget] Working directory synced to tool executor: {file_path or 'default'}")
        # 工作目录变更后刷新分支标签
        self._update_branch()
```

- [ ] **Step 3: 修改 `_sync_working_directory` — 实例缓存优先**

将 `_sync_working_directory`（约第 5879 行）从：

```python
    def _sync_working_directory(self):
        """切换项目时自动加载并同步工作目录"""
        if getattr(self, '_is_destroyed', False):
            return
        if not self.backend or not self.backend.tool_executor:
            return
        project = self._current_project
        workdir = None
        if self.backend.memory_manager:
            workdir = self.backend.memory_manager.get_working_directory(project)
        self.backend.tool_executor.set_workdir(workdir)
        from loguru import logger
        logger.info(f"[MainWidget] Synced working directory for project '{project}': {workdir or 'default'}")
```

改为：

```python
    def _sync_working_directory(self):
        """切换项目时自动加载并同步工作目录
        
        多窗口隔离：实例缓存优先；首次启动时从 DB 读取（新窗口默认值回退）。
        """
        if getattr(self, '_is_destroyed', False):
            return
        if not self.backend or not self.backend.tool_executor:
            return
        project = self._current_project
        # 实例缓存优先（多窗口隔离的关键：保持自身选择，不受其他窗口 DB 写入影响）
        workdir = self._current_workdir.get(project)
        if workdir is None:
            # 首次启动或项目首次切换，从 DB 读取默认值（新窗口恢复用）
            if self.backend.memory_manager:
                workdir = self.backend.memory_manager.get_working_directory(project)
            if workdir:
                self._current_workdir[project] = workdir
        self.backend.tool_executor.set_workdir(workdir or None)
        from loguru import logger
        logger.info(f"[MainWidget] Synced working directory for project '{project}': {workdir or 'default'}")
```

- [ ] **Step 4: Commit**

```bash
git add app/main_widget.py
git commit -m "fix: main_widget — 新增 _current_workdir 实例缓存，工作目录读取实例优先"
```

---

### Task 2: memory_card.py — 更新注释说明 DB 写入目的

**Files:**
- Modify: `app/widgets/memory_card.py:1122` (`_set_as_working_directory`)
- Modify: `app/widgets/memory_card.py:1138` (`_on_worktree_changed`)
- Modify: `app/widgets/memory_card.py:1153` (`_on_worktree_deleted`)

- [ ] **Step 1: 更新 `_set_as_working_directory` 注释**

将 `_set_as_working_directory`（约第 1122 行）的 docstring 从：

```python
    def _set_as_working_directory(self, file_path: str):
        """设置为工作目录（再次点击取消）"""
```

改为：

```python
    def _set_as_working_directory(self, file_path: str):
        """设置为工作目录（再次点击取消）
        
        多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
        当前窗口通过 workingDirChanged 信号通知 main_widget 更新实例缓存。
        """
```

- [ ] **Step 2: 更新 `_on_worktree_changed` 注释**

将 `_on_worktree_changed`（约第 1138 行）的 docstring 从：

```python
    def _on_worktree_changed(self, original_folder: str, worktree_path: str):
        """Worktree 切换：写入 DB + 切换 workdir（UI 层过滤不显示 git_worktree 条目）"""
```

改为：

```python
    def _on_worktree_changed(self, original_folder: str, worktree_path: str):
        """Worktree 切换：写入 DB（新窗口默认值）+ 切换 workdir（UI 层过滤不显示 git_worktree 条目）
        
        多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
        当前窗口通过 workingDirChanged 信号通知 main_widget 更新实例缓存。
        """
```

- [ ] **Step 3: 更新 `_on_worktree_deleted` 注释**

将 `_on_worktree_deleted`（约第 1153 行）的 docstring 从：

```python
    def _on_worktree_deleted(self, worktree_path: str):
        """Worktree 被删除后：移除 DB 记录 + 恢复到主仓库"""
```

改为：

```python
    def _on_worktree_deleted(self, worktree_path: str):
        """Worktree 被删除后：移除 DB 记录 + 恢复到主仓库 + 清除实例缓存
        
        多窗口隔离：通过 workingDirChanged 信号通知 main_widget 清除对应实例缓存。
        """
```

- [ ] **Step 4: Commit**

```bash
git add app/widgets/memory_card.py
git commit -m "docs: memory_card — 更新工作目录相关方法注释，说明多窗口隔离策略"
```

---

## Self-Review

**1. Spec 覆盖率：**
- ✅ `_current_workdir` 实例缓存 — Task 1 Step 1
- ✅ `_on_working_dir_changed` 更新缓存 — Task 1 Step 2
- ✅ `_sync_working_directory` 实例优先 — Task 1 Step 3
- ✅ memory_card 注释更新 — Task 2
- ✅ `_on_worktree_deleted` 通过信号自动清除缓存 — 无需额外代码，`workingDirChanged` 已连接到 `_on_working_dir_changed`

**2. Placeholder 扫描：** 无 TBD/TODO。

**3. 类型一致性：**
- `_current_workdir: Dict[str, str]` — 与 `project: str` key 和 `workdir: str` value 一致
- `file_path` 参数类型为 `str`，与 `Dict[str, str]` 值类型一致
- `workdir or None` / `workdir or "default"` 与 `set_workdir` 参数类型一致