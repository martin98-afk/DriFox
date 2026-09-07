# -*- coding: utf-8 -*-
"""
PreToolUse Hook 函数 — 危险操作拦截（命令/写文件/读敏感文件）

参照 .drifox/plugins/security-guidance 的"pre-execution gate"模式：
- HARD BLOCK：灾难性、零合理场景的工具调用 → 每次都 BLOCK，不去重
- SOFT BLOCK：可教育改进的工具调用 → 首次 BLOCK（同 session 内），后续放行
- 拦截消息用 `{"decision": "block", "output": "..."}` 阻断工具执行，
  工具结果回填给 LLM，强制让 AI 重新设计命令 / 改用安全替代。

`add_output_to_context: true` 配合 BLOCK 决策：tool_executor 拿不到正常结果，
LLM 看到的是 hook 拦截消息，AI 必须调整行为。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

# ── 受关注的工具名（PascalCase，匹配 tool_executor 传入的 tool_name）───
_GUARDED_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "Read"})

# ════════════════════════════════════════════════════════════════════════
# HARD BLOCK 规则（灾难性，无合理使用场景）— 永远拦截
# ════════════════════════════════════════════════════════════════════════

# 1) Bash: rm -rf / 或 rm -rf ~ 或 rm -rf $HOME
#    必须避免误伤 `rm -rf build/` `rm -rf .git/old` 等，所以严格锚定目标
_RE_RM_RF_ROOT = re.compile(
    r"""\brm\s+(-[rRfF]*\s+)*\s*     # rm + flags
        (                             # 目标组
            /                         #   根
          | ~                         #   home
          | \$HOME                    #   $HOME
          | \$\{HOME\}                #   ${HOME}
          | /\*                       #   /* (everything)
        )
        \s*($|[;&|])                  # 收尾
    """,
    re.VERBOSE,
)

# 2) Bash: fork 炸弹
_RE_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")

# 3) Bash: mkfs/dd 写到物理磁盘
_RE_DISK_DESTROY = re.compile(
    r"""\b(mkfs(\.\w+)?\s+/dev/(sd|nvme|hd|xvd|vd)|     # mkfs.ext4 /dev/sda
        dd\s+.*\bof=/dev/(sd|nvme|hd|xvd|vd))           # dd of=/dev/nvme0n1
    """,
    re.VERBOSE,
)

# 4) Bash: 覆盖 /etc /usr /boot /var /sbin /bin（系统目录）
_RE_WRITE_TO_SYSTEM = re.compile(
    r""">(>?)\s*(/{1,2}etc/{1,2}|/{1,2}usr/{1,2}|/{1,2}boot/{1,2}|/{1,2}sbin/{1,2}|/{1,2}bin/{1,2})"""
)

# 5) Bash: 强制推送到 main/master（注意 --force-with-lease 也算）
_RE_FORCE_PUSH_MAIN = re.compile(
    r"""\bgit\s+push\b[^;&|]*                  # git push
        (?:--force(?:-with-lease)?|-f\b)        # 强制标志
        [^;&|]*\b(main|master)\b               # 目标 main/master
    """,
    re.VERBOSE,
)

# 6) 写文件: 目标是 .git/ 内部（hooks.json 自身除外，我们只能判断 .git/ 前缀）
_RE_GIT_INTERNAL_PATH = re.compile(r"(?:^|/)(\.git/)")

# ════════════════════════════════════════════════════════════════════════
# SOFT BLOCK 规则（可教育改进）— 首次拦截，同 session 内去重
# ════════════════════════════════════════════════════════════════════════

# Bash: curl/wget ... | sh  管道到 shell
_RE_PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^;&|]*\|\s*(ba)?sh\b")

# Bash: chmod -R 777
_RE_CHMOD_777 = re.compile(r"\bchmod\s+(-R\s+)?777\b")

# Bash: git reset --hard（HEAD 不可逆回滚）
_RE_GIT_RESET_HARD = re.compile(r"\bgit\s+reset\s+--hard\b")

# 内容: 硬编码密钥（精确到前缀，避免误报 base64 长串）
_SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws_key", "AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", "OpenAI/Anthropic API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", "GitHub PAT", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_oauth", "GitHub OAuth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b")),
    ("slack_token", "Slack token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b")),
    ("pem_key", "PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt_token", "JWT token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
]

# 路径: 读取敏感文件
_SENSITIVE_FILE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("env_file", ".env", re.compile(r"(?:^|/)\.env(?:\.[\w.-]+)?$", re.IGNORECASE)),
    ("ssh_key", "SSH 私钥", re.compile(r"(?:^|/)id_(rsa|dsa|ecdsa|ed25519)$", re.IGNORECASE)),
    ("pem_file", "PEM/KEY", re.compile(r"\.(pem|key|p12|pfx)$", re.IGNORECASE)),
    ("credentials", "凭证文件", re.compile(r"(?:^|/)(credentials|secrets)\.(json|ya?ml|toml)$", re.IGNORECASE)),
]

# ── session 级去重状态（key=session_id, value=已警告过的 rule 名集合）───
# ⚠️ 必须存到 sys.modules 固定 key，而不能放在模块级 dict。
# hook_manager 的 _import_relative_function 每次调用都会
# spec_from_file_location + module_from_spec + exec_module 创建新 module 对象，
# 模块级 dict（如 _WARNED = {}）每次都被重新初始化为空，状态全部丢失。
# 把 _WARNED 挂到 sys.modules['_safety_guard_warned'] 即可跨 reload 保留。
import sys as _sys

_WARNED_KEY = "_safety_guard_warned"
if _WARNED_KEY not in _sys.modules:
    _sys.modules[_WARNED_KEY] = {}  # type: ignore[assignment]


def _get_warned() -> dict[str, set[str]]:
    """获取跨 reload 共享的去重状态"""
    return _sys.modules[_WARNED_KEY]  # type: ignore[return-value]


def _mark_warned(session_id: str, rule: str) -> bool:
    """检查并标记：本 session 内此规则是否已警告过

    Returns:
        True  - 首次匹配，应该 BLOCK
        False - 已警告过，放行
    """
    sid = session_id or "_default"
    warned = _get_warned()
    seen = warned.setdefault(sid, set())
    if rule in seen:
        return False
    seen.add(rule)
    return True


# ── helpers ─────────────────────────────────────────────────────────────


def _normalize_path(p: str) -> str:
    """统一路径为 POSIX 风格，方便正则匹配"""
    if not p:
        return ""
    return PurePosixPath(p.replace("\\", "/")).as_posix()


def _block(reason: str) -> dict:
    """构造标准的 BLOCK 决策（hook_manager 会按 {"decision":"block","output":...} 解析）"""
    return {"decision": "block", "output": reason}


def _check_bash(args: dict, session_id: str) -> str | dict | None:
    """检查 bash 命令。返回 None=无问题；dict=BLOCK（无论硬/软）"""
    cmd = args.get("command") or ""
    if not cmd:
        return None

    # ── HARD BLOCK（不去重）────────────────────────────────────────
    if _RE_RM_RF_ROOT.search(cmd):
        return _block(
            "❌ 操作被拦截：`rm -rf /` 或 `rm -rf ~` 会删除根目录或用户主目录，系统级不可逆操作，请改用更具体的路径。"
        )
    if _RE_FORK_BOMB.search(cmd):
        return _block("❌ 操作被拦截：检测到 fork 炸弹 `:(){:|:&};:`,请立即停止并清理。")
    if _RE_DISK_DESTROY.search(cmd):
        return _block(
            "❌ 操作被拦截：`mkfs` / `dd` 写到物理磁盘 (`/dev/sd*` `/dev/nvme*` 等) "
            "会格式化或覆写整个磁盘，确认目标无误且数据已备份再操作。"
        )
    if _RE_FORCE_PUSH_MAIN.search(cmd):
        return _block(
            "❌ 操作被拦截：检测到对 main/master 的强制推送 (`git push --force` 或 `-f`),"
            "会重写远程历史，请改用 `--force-with-lease` 或 PR 流程。"
        )
    if _RE_WRITE_TO_SYSTEM.search(cmd):
        return _block("❌ 操作被拦截：检测到向系统目录 (`/etc/` `/usr/` `/boot/` 等) 重定向写入。")

    # ── SOFT BLOCK（session 内首次拦截，后续放行）─────────────────
    if _RE_PIPE_TO_SHELL.search(cmd) and _mark_warned(session_id, "curl_pipe_sh"):
        return _block(
            "🚫 操作被拦截：`curl ... | bash` 会直接执行远程脚本，"
            "请先下载到本地 `curl -O xxx.sh` 检查内容后再 `bash xxx.sh`。\n"
            "（提示：本会话内同类规则仅拦截一次）"
        )
    if _RE_CHMOD_777.search(cmd) and _mark_warned(session_id, "chmod_777"):
        return _block(
            "🚫 操作被拦截：`chmod 777` 权限过宽，会带来安全风险，"
            "建议 `chmod 755`（可执行）或 `chmod 644`（只读）。\n"
            "（提示：本会话内同类规则仅拦截一次）"
        )
    if _RE_GIT_RESET_HARD.search(cmd) and _mark_warned(session_id, "git_reset_hard"):
        return _block(
            "🚫 操作被拦截：`git reset --hard` 会丢弃未提交修改，"
            "请先 `git stash` 备份（或 `git commit` 保存）再操作。\n"
            "（提示：本会话内同类规则仅拦截一次）"
        )
    return None


def _extract_write_content(args: dict) -> str:
    """从 edit/write/multi_edit 中抽取要写入的内容"""
    if "content" in args:
        return str(args.get("content") or "")
    if "new_string" in args:
        return str(args.get("new_string") or "")
    edits = args.get("edits") or []
    chunks: list[str] = []
    for e in edits:
        if isinstance(e, dict):
            chunks.append(str(e.get("new_string") or ""))
    return "\n".join(chunks)


def _check_file_op(tool_name: str, args: dict, session_id: str) -> str | dict | None:
    """检查 write/edit/multi_edit/read 操作"""
    path = _normalize_path(args.get("file_path") or args.get("path") or args.get("file") or "")

    # ── read: 敏感文件 → 首次拦截（强制 AI 不在回复中回显）────────
    if tool_name == "Read":
        if path:
            for rule, label, pat in _SENSITIVE_FILE_PATTERNS:
                if pat.search(path) and _mark_warned(session_id, f"read_{rule}"):
                    return _block(
                        f"🚫 操作被拦截：正在读取敏感文件 `{path}` ({label})。\n"
                        "若必须读取，请在回复中仅引用关键字段，**不要完整回显文件内容**。\n"
                        "（提示：本会话内同类规则仅拦截一次）"
                    )
        return None

    # ── write/edit/multi_edit: 写路径 HARD BLOCK ──────────────────
    if path and _RE_GIT_INTERNAL_PATH.search(path):
        return _block(
            f"❌ 操作被拦截：写入 `.git/` 内部文件 ({path}) 会破坏 git 仓库完整性，"
            "请改用 git 命令操作（`git add` / `git commit` / `git config` 等）。"
        )

    # ── 写内容检查: 硬编码密钥 → 首次拦截 ─────────────────────────
    if tool_name in ("Write", "Edit", "MultiEdit"):
        content = _extract_write_content(args)
        if content:
            for rule, label, pat in _SECRET_PATTERNS:
                if pat.search(content) and _mark_warned(session_id, f"write_{rule}"):
                    return _block(
                        f"🚫 操作被拦截：检测到疑似硬编码密钥（{label}），"
                        "请改用环境变量 / `.env` (并加入 .gitignore) / 密钥管理服务。\n"
                        "（提示：本会话内同类规则仅拦截一次）"
                    )
    return None


# ── main hook ──────────────────────────────────────────────────────────


def hook(event: str, context: dict) -> str:
    """危险操作拦截

    Args:
        event: 事件名（PreToolUse）
        context: 由 tool_executor 构建的上下文，含：
            - tool_name: str PascalCase 工具名
            - tool_input / args: dict 工具参数
            - file: str（可选）文件路径
            - session_id: str（用于 SOFT BLOCK 去重）

    Returns:
        空字符串（无问题或 SOFT BLOCK 已去重放行）；
        {"decision": "block", "output": "..."} 字典（BLOCK，把 output 作为工具结果回填）
    """
    tool_name = context.get("tool_name", "")

    if tool_name not in _GUARDED_TOOLS:
        return ""

    args = context.get("args") or context.get("tool_input") or {}
    if not isinstance(args, dict):
        return ""

    session_id = context.get("session_id", "")

    try:
        if tool_name == "Bash":
            result: Any = _check_bash(args, session_id)
        else:
            result = _check_file_op(tool_name, args, session_id)
    except Exception as e:
        # hook 自身异常时静默放行（fail-open），避免阻塞正常工具调用
        from loguru import logger

        logger.warning(f"[SafetyGuardHook] 检查失败，已放行: {e}")
        return ""

    if result is None:
        return ""
    return result
