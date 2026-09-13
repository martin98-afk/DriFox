# -*- coding: utf-8 -*-
"""worktree-manager 插件工具 — 大模型工作树管理（单一工具 manage_worktree，action 分发）

对齐 manage_skill 的单工具多 action 风格：
- list   → 列出全部工作树（路径/分支/领先落后，标注主仓库与当前会话）+ merge-tree 冲突预判
- create → 创建工作树（默认基于 HEAD 新建分支，分支已存在则挂载；目录固定主仓库 .worktrees/ 下）
- remove → 删除工作树（串行 remove → prune → branch 删除；有未提交改动默认拒绝，force=true 强制；
           分支含未合并提交时默认保留分支，keep_branch=true 直接保留）
- merge  → 把工作树分支合并回主仓库当前分支（target_branch 校验 + 双向 dirty 预检 + 可自定义信息）；
           冲突时返回冲突文件与冲突块内容，不 abort，保留冲突现场由模型用文件工具解决后提交

写操作成功/冲突后：
1. 清 GitWorktreeDetector 相关缓存（否则 UI 刷新仍读旧数据）
2. 经 WorktreeChangeBridge 发跨线程信号 → SystemWorktreePage 主线程 refresh_data()

安全约束：remove 拒绝删除主仓库与当前会话工作目录所在的工作树；merge 拒绝合并主仓库自身。
"""

import os
import subprocess
import sys
from typing import Optional, Tuple

from loguru import logger

from app.tools.result import ToolResult
from app.utils.git_worktree import GitWorktreeDetector, WorktreeInfo

# Windows 下隐藏 cmd 窗口（对齐 worktree_section._CREATION_FLAGS）
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW

_GROUP = "Git 工作树"
_bridge_getter_cache = None  # change_bridge.get_bridge 缓存（fallback 副本复用）


def _run_git(args: list, cwd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATION_FLAGS,
        cwd=cwd,
    )


def _norm(p: str) -> str:
    """路径归一：abspath + normcase（Windows 大小写不敏感，混用会使前缀/相等比较漏判）"""
    return os.path.normcase(os.path.abspath(p))


def _dirty_files(cwd: str) -> list:
    """git status --porcelain 列未提交/未跟踪项；命令失败视为不脏（不阻断主流程）"""
    r = _run_git(["status", "--porcelain"], cwd, timeout=10)
    if r.returncode != 0:
        return []
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()]


def _resolve_main_repo(tool_ctx: dict, path: str = "") -> Tuple[Optional[str], str]:
    """定位主仓库根目录。

    优先 path，缺省用 tool_ctx["workdir"]；若目标在 worktree 中则回溯主仓库。
    返回 (主仓库根目录 | None, 错误消息)。
    """
    base = (path or (tool_ctx or {}).get("workdir") or "").strip()
    if not base or not os.path.isdir(base):
        return None, f"工作目录无效: {base or '(空)'}，请先在会话中设置项目工作目录"
    git_root = GitWorktreeDetector.detect_git(base)
    if not git_root:
        return None, f"{base} 不在 git 仓库中"
    if GitWorktreeDetector.is_worktree(git_root):
        main = GitWorktreeDetector.get_main_repo_path(git_root)
        if not main or not os.path.isdir(main):
            return None, f"无法从 worktree 回溯主仓库: {git_root}"
        return main, ""
    return git_root, ""


def _invalidate_info_cache(repo_root: str) -> None:
    """清掉指向该仓库的 get_repo_info/detect_git 缓存（value.root 匹配，跨 path key）"""
    for key, entry in list(GitWorktreeDetector._info_cache.items()):
        info = entry[0] if entry else None
        if info is not None and getattr(info, "root", None) == repo_root:
            GitWorktreeDetector._info_cache.pop(key, None)
    for key in list(GitWorktreeDetector._detect_cache):
        if os.path.abspath(key) == os.path.abspath(repo_root):
            GitWorktreeDetector._detect_cache.pop(key, None)


def _load_get_bridge():
    """加载 change_bridge.get_bridge（跨模块单例）。

    本文件由 tool loader 以裸模块（无 __package__）加载，相对导入不可用；
    插件目录名带连字符也无法作为包 import。加载顺序：
    1. UI loader 已把 change_bridge 以 ui_plugin_worktree_manager.change_bridge
       注册进 sys.modules → 直接复用（避免两份同名 PyQt 类共存导致信号解析冲突）
    2. 未命中（UI 未加载）→ 按文件路径 exec 副本（bridge 实例挂在
       QCoreApplication 上，模块副本不同不影响单例同一性）

    注意：不得向 sys.modules 写入任何键——tool loader 的 AST 安全网会因
    「动态键无法静态确认落点」拒载整个工具模块（不区分作用域）。
    """
    global _bridge_getter_cache
    if _bridge_getter_cache is not None:
        return _bridge_getter_cache

    ui_mod = sys.modules.get("ui_plugin_worktree_manager.change_bridge")
    if ui_mod is not None and hasattr(ui_mod, "get_bridge"):
        _bridge_getter_cache = ui_mod.get_bridge
        return _bridge_getter_cache

    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "ui" / "change_bridge.py"
    spec = importlib.util.spec_from_file_location("_worktree_change_bridge_loader", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 change_bridge: {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _bridge_getter_cache = mod.get_bridge
    return _bridge_getter_cache


def _notify_changed(summary: str) -> None:
    """跨线程通知 UI 刷新（emit → 主线程 QueuedConnection）"""
    try:
        _load_get_bridge()().changed.emit(summary)
    except Exception as e:  # UI 未加载（纯后端调用等）：刷新通知可缺省
        logger.debug(f"[worktree_tools] UI 刷新通知未送达: {e}")


def _find_registered_worktree(tool_ctx: dict, path: str) -> Tuple[Optional[str], Optional[WorktreeInfo], str]:
    """校验 path 是当前项目主仓库登记的工作树。

    返回 (主仓库根, WorktreeInfo, 错误消息)；target 主仓库时也返回（is_main=True 由调用方裁决）。
    """
    raw = (path or "").strip()
    wt_path = os.path.abspath(raw)
    if not wt_path or not os.path.isdir(wt_path):
        return None, None, f"工作树路径无效或不存在: {raw or '(空)'}"
    repo_root, err = _resolve_main_repo(tool_ctx or {}, path=wt_path)
    if err or repo_root is None:
        return None, None, err or "无法定位主仓库"
    info = GitWorktreeDetector.get_repo_info(repo_root)
    if info is None:
        return None, None, f"获取仓库信息失败: {repo_root}"
    target = next((wt for wt in info.worktrees if _norm(wt.path) == _norm(wt_path)), None)
    if target is None:
        return repo_root, None, f"{wt_path} 不是仓库 {repo_root} 登记的工作树（可用 action=list 查询）"
    return repo_root, target, ""


def _merge_preview(repo_root: str, branch: str) -> str:
    """merge-tree 预判 branch 合入主仓库 HEAD 的冲突情况（git ≥2.38，失败静默降级）"""
    r = _run_git(["merge-tree", "--write-tree", "--name-only", "HEAD", branch], repo_root, timeout=15)
    if r.returncode == 0:
        return "合并预判: 无冲突，可直接合并"
    if r.returncode == 1:
        files = [ln for ln in (r.stdout or "").splitlines()[1:] if ln.strip()]
        if not files:
            return "合并预判: 存在冲突（文件列表不可用）"
        shown = ", ".join(files[:5]) + ("…" if len(files) > 5 else "")
        return f"合并预判: 将冲突 {len(files)} 个文件 ({shown})"
    return "合并预判: 不可用（git 版本过低或仓库状态异常）"


# ── action: list ──


def _action_list(tool_ctx: dict, kwargs: dict) -> ToolResult:
    repo_root, err = _resolve_main_repo(tool_ctx or {})
    if err or repo_root is None:
        return ToolResult(False, error=err or "无法定位主仓库")
    info = GitWorktreeDetector.get_repo_info(repo_root)
    if info is None or not info.worktrees:
        return ToolResult(False, error=f"获取仓库信息失败: {repo_root}")

    current_workdir = _norm((tool_ctx or {}).get("workdir") or "")
    lines = []
    for wt in info.worktrees:
        tags = []
        if wt.is_main:
            tags.append("主仓库")
        if current_workdir and current_workdir.startswith(_norm(wt.path)):
            tags.append("当前会话")
        tag = f" [{','.join(tags)}]" if tags else ""
        delta = ""
        if not wt.is_main and (wt.ahead_main or wt.behind_main):
            delta = f" (领先主仓库 {wt.ahead_main} / 落后 {wt.behind_main} 提交)"
        preview = ""
        if not wt.is_main and wt.branch and wt.branch != "(detached)":
            preview = f"\n  {_merge_preview(repo_root, wt.branch)}"
        lines.append(f"- {wt.path}{tag}\n  分支: {wt.branch}{delta}{preview}")
    return ToolResult(True, content=f"仓库 {repo_root} 共 {len(info.worktrees)} 个工作树:\n" + "\n".join(lines))


# ── action: create ──


def _action_create(tool_ctx: dict, kwargs: dict) -> ToolResult:
    branch_name = str(kwargs.get("branch_name") or "").strip()
    if not branch_name:
        return ToolResult(False, error="create 需要 branch_name（分支名）")
    base_branch = str(kwargs.get("base_branch") or "").strip()
    dir_name = str(kwargs.get("dir_name") or "").strip() or branch_name.replace("/", "-")

    repo_root, err = _resolve_main_repo(tool_ctx or {})
    if err or repo_root is None:
        return ToolResult(False, error=err or "无法定位主仓库")
    worktree_dir = os.path.join(repo_root, ".worktrees", dir_name.replace("/", "-"))
    if os.path.exists(worktree_dir):
        return ToolResult(False, error=f"目标目录已存在: {worktree_dir}")

    # 分支已存在 → 挂载已有分支；否则 -b 新建（对齐 UI 语义并支持已有分支）
    exists = _run_git(["rev-parse", "--verify", f"refs/heads/{branch_name}"], repo_root).returncode == 0
    if exists:
        cmd = ["worktree", "add", worktree_dir, branch_name]
    else:
        cmd = ["worktree", "add", "-b", branch_name, worktree_dir, base_branch or "HEAD"]

    try:
        r = _run_git(cmd, repo_root, timeout=30)
    except Exception as e:
        return ToolResult(False, error=f"git worktree add 执行失败: {e}")
    if r.returncode != 0:
        return ToolResult(False, error=r.stderr.strip() or f"git worktree add 失败 (rc={r.returncode})")

    _invalidate_info_cache(repo_root)
    _notify_changed(f"created {worktree_dir}")
    logger.info(f"[worktree_tools] 已创建工作树: {worktree_dir} (分支 {branch_name})")
    return ToolResult(
        True, content=f"工作树已创建: {worktree_dir}\n分支: {branch_name}（{'已有分支挂载' if exists else '新建分支'}）"
    )


# ── action: remove ──


def _action_remove(tool_ctx: dict, kwargs: dict) -> ToolResult:
    keep_branch = bool(kwargs.get("keep_branch") or False)
    force = bool(kwargs.get("force") or False)
    repo_root, target, err = _find_registered_worktree(tool_ctx, kwargs.get("path", ""))
    if err:
        return ToolResult(False, error=err)
    assert target is not None and repo_root is not None
    wt_path = os.path.abspath(target.path)
    if target.is_main:
        return ToolResult(False, error=f"{wt_path} 是主仓库，拒绝删除")
    current_workdir = _norm((tool_ctx or {}).get("workdir") or "")
    if current_workdir and current_workdir.startswith(_norm(wt_path)):
        return ToolResult(False, error=f"{wt_path} 是当前会话的工作目录，拒绝删除；请先切换工作目录或改用 UI 操作")

    # 防数据丢失：有未提交改动默认拒绝删除
    dirty = _dirty_files(wt_path)
    if dirty and not force:
        shown = "\n".join(dirty[:10])
        more = f"\n…共 {len(dirty)} 项" if len(dirty) > 10 else ""
        return ToolResult(
            False,
            error=f"工作树有未提交改动，删除将丢失（{len(dirty)} 项）:\n{shown}{more}\n确认丢弃请传 force=true",
        )

    # 串行 remove → prune → branch 删除（顺序依赖硬约束，对齐 UI _delete_worktree_job）
    try:
        cmd = ["worktree", "remove"] + (["--force"] if force else []) + [wt_path]
        r = _run_git(cmd, repo_root, timeout=30)
        if r.returncode != 0 and force:
            # 双重 --force：处理锁定等残留场景
            r = _run_git(["worktree", "remove", "--force", "--force", wt_path], repo_root, timeout=30)
        if r.returncode != 0:
            return ToolResult(False, error=r.stderr.strip() or f"git worktree remove 失败 (rc={r.returncode})")
        _run_git(["worktree", "prune"], repo_root, timeout=10)
    except Exception as e:
        return ToolResult(False, error=f"git worktree remove 执行失败: {e}")

    # 分支删除：先 -d 探测未合并提交，被拒且无 force 时保留分支
    branch_note = f"分支 {target.branch} 已保留" if keep_branch else ""
    if not keep_branch and target.branch and target.branch != "(detached)":
        br = _run_git(["branch", "-d", target.branch], repo_root, timeout=15)
        if br.returncode == 0:
            branch_note = f"分支 {target.branch} 已删除"
        elif force:
            br2 = _run_git(["branch", "-D", target.branch], repo_root, timeout=15)
            branch_note = (
                f"分支 {target.branch} 已强制删除"
                if br2.returncode == 0
                else f"分支删除失败: {br2.stderr.strip()}"
            )
        else:
            branch_note = f"分支 {target.branch} 含未合并提交，已保留（确认丢弃可传 force=true 重删或手动 git branch -D）"

    _invalidate_info_cache(repo_root)
    _notify_changed(f"removed {wt_path}")
    logger.info(f"[worktree_tools] 已删除工作树: {wt_path}（{branch_note or '无分支关联'}）")
    return ToolResult(True, content=f"工作树已删除: {wt_path}\n{branch_note or '无分支关联'}")


# ── action: merge ──


def _extract_conflicts(file_path: str, max_chunks: int = 5, max_chars: int = 2500) -> str:
    """读冲突文件，抽取 <<<<<<< ... >>>>>>> 冲突块文本（限额防刷屏）"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return "（无法读取文件内容，可能为二进制或已被删除）"

    chunks, buf, in_chunk = [], [], False
    for line in lines:
        if line.startswith("<<<<<<<"):
            in_chunk, buf = True, [line]
        elif in_chunk:
            buf.append(line)
            if line.startswith(">>>>>>>"):
                if len(chunks) < max_chunks and sum(len(b) for b in chunks) + len(buf) < max_chars:
                    chunks.append("\n".join(buf))
                in_chunk = False
                if len(chunks) >= max_chunks:
                    break
    if not chunks:
        return "（未见冲突标记，可能已被部分解决）"
    out = "\n".join(chunks)
    if len(chunks) >= max_chunks:
        out += f"\n…（冲突块超过 {max_chunks} 个，已截断；完整内容用 read 工具查看该文件）"
    return out


def _action_merge(tool_ctx: dict, kwargs: dict) -> ToolResult:
    repo_root, target, err = _find_registered_worktree(tool_ctx, kwargs.get("path", ""))
    if err:
        return ToolResult(False, error=err)
    assert target is not None and repo_root is not None
    if target.is_main:
        return ToolResult(False, error=f"{target.path} 是主仓库，无可合并的工作树分支")
    branch = target.branch
    if not branch or branch == "(detached)":
        return ToolResult(False, error=f"工作树 {target.path} 处于 detached HEAD 状态，无分支可合并")

    # 目标分支校验：git merge 只能合并进主仓库当前 checkout 的分支
    target_branch = str(kwargs.get("target_branch") or "").strip()
    head_ref_r = _run_git(["symbolic-ref", "--short", "HEAD"], repo_root, timeout=10)
    head_branch = (head_ref_r.stdout or "").strip()
    if target_branch and target_branch != head_branch:
        return ToolResult(
            False,
            error=(
                f"目标分支 {target_branch} 不是主仓库当前 checkout 的分支（当前 {head_branch or 'detached HEAD'}）；"
                "git merge 只能合并进当前分支，请先在主仓库切换"
            ),
        )

    # 双向 dirty 预检：主仓库脏易撞车；工作树脏则未提交改动不进入合并
    main_dirty = _dirty_files(repo_root)
    if main_dirty:
        return ToolResult(False, error=f"主仓库有 {len(main_dirty)} 项未提交改动，先提交或 stash 再合并")
    wt_dirty = _dirty_files(os.path.abspath(target.path))
    if wt_dirty:
        return ToolResult(False, error=f"工作树有 {len(wt_dirty)} 项未提交改动，先提交再合并，否则这些改动不进入合并结果")

    message = str(kwargs.get("message") or "").strip()
    try:
        cmd = ["merge", branch]
        if message:
            cmd += ["-m", message]
        r = _run_git(cmd, repo_root, timeout=60)
    except Exception as e:
        return ToolResult(False, error=f"git merge 执行失败: {e}")

    if r.returncode == 0:
        _invalidate_info_cache(repo_root)
        _notify_changed(f"merged {branch}")
        logger.info(f"[worktree_tools] 已合并 {branch} → 主仓库 {repo_root}")
        msg = (r.stdout or "").strip().splitlines()
        tail = "\n".join(msg[-3:]) if msg else ""
        return ToolResult(True, content=f"分支 {branch} 已合并到主仓库当前分支。\n{tail}".strip())

    # 冲突判定：合并中断且存在未合并路径（区分冲突与普通失败如本地更改挡路）
    status = _run_git(["diff", "--name-only", "--diff-filter=U"], repo_root, timeout=10)
    conflict_files = [ln for ln in (status.stdout or "").splitlines() if ln.strip()]
    if not conflict_files:
        return ToolResult(False, error=r.stderr.strip() or r.stdout.strip() or f"git merge 失败 (rc={r.returncode})")

    # 冲突：保留现场（不 abort），返回冲突文件与冲突块内容，由模型继续解决
    parts = [f"合并 {branch} 产生冲突，共 {len(conflict_files)} 个文件（冲突现场已保留）："]
    budget = 6000
    for fp in conflict_files:
        full = os.path.join(repo_root, fp)
        chunk = _extract_conflicts(full) if budget > 0 else "（输出预算已用尽，用 read 工具查看）"
        budget -= len(chunk)
        parts.append(f"\n### {fp}\n{chunk}")
    parts.append(
        "\n处理指引：用 read/edit 工具解决上述文件中的冲突标记（<<<<<<< / ======= / >>>>>>>），"
        "然后 bash 执行 git add <文件> && git commit 完成合并；"
        "放弃合并则 bash 执行 git merge --abort。"
    )
    _invalidate_info_cache(repo_root)
    _notify_changed(f"merge-conflict {branch}")
    logger.warning(f"[worktree_tools] 合并 {branch} 冲突: {len(conflict_files)} 个文件待解决")
    return ToolResult(False, error="\n".join(parts))


# ── action 分发 ──

_ACTIONS = {
    "list": _action_list,
    "create": _action_create,
    "remove": _action_remove,
    "merge": _action_merge,
}


def _worktree_manage_impl(tool_ctx, **kwargs) -> ToolResult:
    action = (kwargs.get("action") or "").lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return ToolResult(False, error=f"未知 action: {kwargs.get('action')!r}（支持 list/create/remove/merge）")
    return handler(tool_ctx or {}, kwargs)


# ── schema ──

_WORKTREE_MANAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "manage_worktree",
        "description": (
            "项目 git 工作树管理（单一入口，action 分发）。"
            "list=列出全部工作树（路径/分支/与主仓库领先落后 + merge-tree 冲突预判，标注主仓库与当前会话；无其他参数）。"
            "create=创建新工作树：默认基于 HEAD 新建分支 branch_name，该分支已存在则直接挂载（base_branch 被忽略）；"
            "目录固定在主仓库 .worktrees/ 下（dir_name 缺省用分支名，/ 转 -）。"
            "remove=删除工作树：串行 remove → prune → branch 删除（keep_branch=true 保留分支）；"
            "有未提交改动默认拒绝（force=true 强制）；不能删除主仓库，也不能删除当前会话工作目录所在的工作树。"
            "merge=把工作树分支合并回主仓库当前分支（双向 dirty 预检；target_branch 须为主仓库当前分支）："
            "产生冲突时返回冲突文件与冲突块内容并保留冲突现场"
            "（不 abort），按返回中的指引用文件工具解决后提交。"
            "create/remove/merge 操作后工作树面板自动刷新。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "remove", "merge"],
                    "description": "list=列出；create=创建；remove=删除；merge=合并回主仓库",
                },
                "path": {
                    "type": "string",
                    "description": "remove/merge 必填：工作树路径（可用 action=list 查询）",
                },
                "branch_name": {"type": "string", "description": "create 必填：分支名（已存在则挂载，否则新建）"},
                "base_branch": {
                    "type": "string",
                    "description": "create 可选：新分支的起点（分支名/提交/标签），缺省 HEAD",
                },
                "dir_name": {"type": "string", "description": "create 可选：工作树目录名，缺省用分支名（/ 转 -）"},
                "keep_branch": {
                    "type": "boolean",
                    "description": "remove 可选：true=保留分支只删目录，默认 false（删分支；含未合并提交时仍保留除非 force=true）",
                },
                "force": {
                    "type": "boolean",
                    "description": "remove 可选：true=工作树有未提交改动时仍强制删除并强删未合并分支（默认拒绝/保留分支）",
                },
                "target_branch": {
                    "type": "string",
                    "description": "merge 可选：合并目标分支，须为主仓库当前 checkout 的分支，缺省即当前分支",
                },
                "message": {
                    "type": "string",
                    "description": "merge 可选：合并提交信息（git merge -m），缺省用 git 默认信息",
                },
            },
            "required": ["action"],
        },
    },
}


def _preview_manage(tool_args: dict) -> str:
    action = (tool_args.get("action") or "").lower()
    if action == "create":
        return f"创建工作树 {tool_args.get('branch_name', '')}".strip()
    if action == "remove":
        return f"删除工作树 {tool_args.get('path', '')}".strip()
    if action == "merge":
        return f"合并工作树 {tool_args.get('path', '')}".strip()
    return "查看工作树列表"


def register(registry) -> None:
    registry.register(
        "manage_worktree",
        _WORKTREE_MANAGE_SCHEMA,
        impl=_worktree_manage_impl,
        danger="dangerous",
        icon="manage_worktree",
        cn_name="管理工作树",
        group=_GROUP,
        description="工作树增删查与合并（list/create/remove/merge）",
        aliases=["ManageWorktree"],
        preview=_preview_manage,
    )
