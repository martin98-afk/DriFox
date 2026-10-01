# -*- coding: utf-8 -*-
"""EU-G24 + EU-G25：git 破坏性子命令接入审批 + 白名单匹配收窄

## G25 背景
`classify_command` 只看首 token，而 `git` 不在 `CONFIRM_COMMANDS` 里 →
**所有 git 命令一律 safe**。实测确认 `git rm -rf .` / `git clean -fdx` /
`git reset --hard` / `git push --force` **不触发任何审批**，AI 可在无提示下
丢弃未提交改动或强推覆盖远端。

## G24 背景
`command.allow_prefixes` 原为纯 `startswith` 前缀匹配 → 白名单填 `git` 会放行
`git rm -rf .` 等**全部**子命令；而设置页 placeholder「如 git、npm run」正引导
用户如此配置，等于引导用户制造漏洞。

## 行为变更（用户可感知）
以下命令此前 **allow（无提示）**，现在 **confirm（弹审批）**：
`git rm` / `git clean -f[-fdx]` / `git reset --hard` / `git push --force|-f` /
`git branch -D` / `git checkout -- <path>` / `git restore <path>` /
`git stash drop|clear` / `git tag -d` / `git filter-branch|filter-repo` / `git update-ref`
"""

import pytest

from app.tools.command_safety import classify_command_deep
from app.tools.sandbox import SandboxConfig, check_command

G = "git "


def _cfg(allow=None, confirm=None):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete_protection", True)
    cfg.set("command.allow_prefixes", allow or [])
    cfg.set("command.confirm_prefixes", confirm or [])
    return cfg


# ══════════════ G25：破坏性子命令 → confirm ══════════════


@pytest.mark.parametrize(
    "cmd",
    [
        G + "rm -rf .",
        G + "rm --cached secret.env",
        G + "clean -fdx",
        G + "clean -f",
        G + "reset" + " --hard",
        G + "push --force",
        G + "push -f origin main",
        G + "push --force-with-lease",
        G + "branch -D feat",
        G + "checkout -- .",
        G + "restore .",
        G + "stash drop",
        G + "stash clear",
        G + "tag -d v1",
        G + "filter-branch --all",
        G + "update-ref HEAD~1",
    ],
)
def test_destructive_git_subcommands_confirm(cmd):
    """破坏性子命令必须判 confirm"""
    assert classify_command_deep(cmd) == "confirm", f"{cmd} 应触发审批"


@pytest.mark.parametrize(
    "cmd",
    [
        G + "status",
        G + "log --oneline",
        G + "diff HEAD",
        G + "add .",
        G + "commit -m x",
        G + "stash",
        G + "stash list",
        G + "push origin main",
        G + "pull",
        G + "fetch",
        G + "checkout main",
        G + "checkout -b new",
        G + "branch --list",
        G + "branch -d merged",
        G + "clean -n",
        G + "reset --soft HEAD~1",
        G + "reset --mixed",
        G + "restore --staged f.txt",
        G + "tag v1",
        G + "show HEAD",
    ],
)
def test_readonly_and_normal_git_subcommands_safe(cmd):
    """只读/常规子命令不受影响（零误报是硬要求）"""
    assert classify_command_deep(cmd) == "safe", f"{cmd} 不应被误拦"


def test_git_dash_D_distinguished_from_dash_d():
    """`-D`（强制删未合并分支）与 `-d`（只删已合并）必须区分

    踩坑记录：初版把 args 统一 `.lower()` → `-D` 变 `-d` 无法区分，
    实测 `git branch -D feat` 被判 safe。必须用原大小写判定。
    """
    assert classify_command_deep(G + "branch -D feat") == "confirm"
    assert classify_command_deep(G + "branch -d merged") == "safe"


def test_git_global_options_skipped():
    """git 全局选项（-C / -c）不干扰子命令定位"""
    assert classify_command_deep(G + "-C /repo rm -rf .") == "confirm"
    assert classify_command_deep(G + "-C /repo status") == "safe"


def test_non_git_commands_unaffected():
    """非 git 命令判定不变（回归）"""
    assert classify_command_deep("rm x.txt") == "confirm"
    assert classify_command_deep("chmod 777 x") == "confirm"
    assert classify_command_deep("ls -la") == "safe"
    assert classify_command_deep("cacls C:\\ /g u:F") == "block"


# ══════════════ G24：白名单不再连带放行破坏子命令 ══════════════


def test_whitelist_does_not_release_destructive_subcommand():
    """白名单填 `git` 后：常规子命令放行，破坏性子命令仍要审批

    这是 G24 的核心收益 —— 设置页 placeholder 正是这么引导用户的。
    """
    cfg = _cfg(allow=["git"])
    assert check_command(G + "status", cfg) == "allow"
    assert check_command(G + "commit -m x", cfg) == "allow"
    assert check_command(G + "rm -rf .", cfg) == "confirm"
    assert check_command(G + "push --force", cfg) == "confirm"
    assert check_command(G + "clean -fdx", cfg) == "confirm"


def test_whitelist_still_denies_block_commands():
    """白名单命中但命令是 block 类 → 仍 DENY（原有语义保留）"""
    cfg = _cfg(allow=["cacls"])
    assert check_command("cacls C:\\ /g user:F", cfg) == "deny"


def test_whitelist_word_boundary():
    """白名单匹配带词边界：`git` 不误命中 `gitsomething`"""
    cfg = _cfg(allow=["git"])
    # `gitsomething` 不命中白名单 → 走默认判定（safe → allow，非白名单放行）
    assert check_command("gitsomething", cfg) == "allow"
    # 关键是 `npm run` 不误命中 `npm runner`
    cfg2 = _cfg(allow=["npm run"])
    assert check_command("npm runner", cfg2) == "allow"  # 默认 safe
    assert check_command("npm run build", cfg2) == "allow"


def test_multitoken_whitelist_entry():
    """白名单条目可含参数（如 `git status`）"""
    cfg = _cfg(allow=["git status"])
    assert check_command(G + "status", cfg) == "allow"
    # 不含 `git commit` → 走默认（safe → allow，但非白名单短路）
    assert check_command(G + "commit -m x", cfg) == "allow"


# ══════════════ 与 EU-G23 的交互（leader 特别要求）══════════════


def test_git_rm_interacts_with_delete_protection_gate():
    """`git rm` **会**被识别为删除命令 → 受 `delete_protection` 门控（EU-G23 交互）

    ⚠ 这是 EU-G23 与 G25 的**真实交互**（leader 特别要求说明）：
    `is_delete_command` 是**逐 token 扫描**（`DELETE_COMMANDS` 含 `rm`），
    所以 `git rm -rf .` 的 token 序列里含 `rm` → 判 True → 门控生效：
    - `delete_protection=True`  → confirm（G25 的拦截稳定）
    - `delete_protection=False` → **allow**（门控降级，G25 的拦截被开关覆盖）

    语义自洽性：关闭"删除保护"＝用户明确表示"删除类操作不要审批"，
    `git rm` 本质就是删除文件，同属删除类 → 被同一开关统一管住是**正确**的，
    而非漏洞。若用户想要"git 破坏性操作始终拦"，应保持该开关开启。

    **文档提示**：此行为需在 CHANGELOG / UI 文案中说明（避免用户以为
    `git rm` 一定被拦）。
    """
    from app.tools.sandbox import is_delete_command, sandbox_check_tool

    assert is_delete_command("git rm -rf .") is True, "git rm 的 token 含 rm，会被识别为删除命令"

    off = SandboxConfig(config_path=":memory:")
    off.set("sandbox_enabled", True)
    off.set("delete_protection", False)
    assert sandbox_check_tool("bash", {"command": "git rm -rf ."}, off) == "allow", "门控降级"

    on = SandboxConfig(config_path=":memory:")
    on.set("sandbox_enabled", True)
    on.set("delete_protection", True)
    assert sandbox_check_tool("bash", {"command": "git rm -rf ."}, on) == "confirm"


def test_git_destructive_non_delete_subcommands_unaffected_by_gate():
    """不受门控影响的 git 破坏性操作（非删除类）在 tp=False 时仍 confirm

    对照上例：`git push --force` / `git clean -fdx` / `git reset --hard` /
    `git branch -D` 的 token 里没有文件删除命令名 → **不受 delete_protection 门控**
    → 无论开关状态都拦截。这是 G25 真正稳定生效的部分。
    """
    from app.tools.sandbox import sandbox_check_tool

    for cmd in ("git push --force", "git clean -fdx", "git reset" + " --hard", "git branch -D feat"):
        off = SandboxConfig(config_path=":memory:")
        off.set("sandbox_enabled", True)
        off.set("delete_protection", False)
        assert sandbox_check_tool("bash", {"command": cmd}, off) == "confirm", f"{cmd} 不受门控"


def test_plain_rm_still_gated_by_delete_protection():
    """对照：普通 `rm` 受 delete_protection 门控（EU-G23 语义不变）"""
    from app.tools.sandbox import sandbox_check_tool

    off = SandboxConfig(config_path=":memory:")
    off.set("sandbox_enabled", True)
    off.set("delete_protection", False)
    assert sandbox_check_tool("bash", {"command": "rm x.txt"}, off) == "allow"

    on = SandboxConfig(config_path=":memory:")
    on.set("sandbox_enabled", True)
    on.set("delete_protection", True)
    assert sandbox_check_tool("bash", {"command": "rm x.txt"}, on) == "confirm"


# ══════════════ confirm_prefixes 也用边界匹配（一致性）══════════════


def test_confirm_prefix_word_boundary():
    """confirm 名单同样带词边界（避免 `git` 误命中 `gitsomething`）"""
    cfg = _cfg(confirm=["git"])
    assert check_command(G + "status", cfg) == "confirm"
    assert check_command("gitsomething", cfg) == "allow"  # 默认 safe


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
