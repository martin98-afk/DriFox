#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Shell 输出压缩 - 减少 LLM token 消耗

基于 lean-ctx #56 pattern 压缩策略，针对高频命令优化

架构:
1. classify() - 判断命令是否需要压缩，区分 protected/passthrough/compress
2. compress_output() - 主入口，调用对应 pattern 压缩
3. compress() - 公开 API，带 token 节省统计
4. track() - Track Mode，仅返回元数据
5. 各个 pattern 模块 - git/npm/cargo/docker 等各自实现

关键词速查:
- git status   -> "main | staged: +a ~b | unstaged: ~c | untracked: d"
- npm install  -> "+pkg@ver (N deps, Xms)"
- cargo build  -> "Compiling X | Finished `dev` Ys"
- pytest       -> "N passed, M failed (Xs)"
- token 统计   -> "[saved 523 tok, 87%]"
"""

import re
from typing import Optional, Tuple

# =============================================================================
# 基础工具
# =============================================================================

def _strip_ansi(text: str) -> str:
    """去除 ANSI 转义序列"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def _normalize_whitespace(text: str) -> str:
    """折叠连续空白为空格"""
    return ' '.join(text.split())


def _count_tokens(text: str) -> int:
    """
    粗略估算 token 数量（按空格分词，简单粗暴但够用）
    实际可用 tiktoken，这里避免额外依赖
    """
    return len(text.split())


def _shorter_only(compressed: str, original: str) -> Optional[str]:
    """只有压缩后更短才返回压缩结果"""
    ct_compressed = _count_tokens(compressed)
    ct_original = _count_tokens(original)
    if ct_compressed < ct_original:
        return compressed
    if ct_compressed == ct_original and len(compressed) < len(original):
        return compressed
    return None


def _compact_lines(lines: list, max_keep: int = 20) -> list:
    """保留前 N 行，折叠剩余部分"""
    if len(lines) <= max_keep:
        return lines
    return lines[:max_keep] + [f"... ({len(lines) - max_keep} more lines)"]


# =============================================================================
# 命令分类策略
# =============================================================================

_PROTECTED_PATTERNS = [
    # 写入文件的操作，压缩可能丢失重要信息
    r'^git\s+commit',  # 需要看 hook 输出
    r'^\s*(>|>>)\s',   # 输出重定向
    r'\btee\b',        # tee 命令
    r'\bcat\s+<<',     # heredoc 写入
    # 结构化输出，必须完整保留
    r'^gh\s+api\b',    # GitHub API 原始 JSON
    r'^gh\s+run\s+view.*--log',  # log 输出
    r'^gh\s+run\s+view.*--log-failed',
    # 管道后跟 base64/jq 等结构化解析
    r'\|\s*(jq|yq|rg\s+-J)',  # 结构化输出再压缩没意义
]


def classify(command: str) -> str:
    """
    将命令分类为 protected / passthrough / compress
    
    - protected: 不压缩，保留原始输出（可能含敏感信息或结构化数据）
    - passthrough: 清理 ANSI 后直接返回（列表型输出需保持格式）
    - compress: 应用 pattern 压缩
    """
    cmd = command.strip().lower()
    
    # protected 检查
    for pat in _PROTECTED_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return "protected"
    
    # passthrough：容器列表、tree 等需要保持格式的命令
    passthrough = [
        r'^docker\s+ps',
        r'^docker\s+compose\s+ps',
        r'^kubectl\s+get\s+pods',     # 表格格式
        r'^kubectl\s+get\s+svc',
        r'^kubectl\s+get\s+deploy',
        r'^tree\s',
        r'^ls\s+--long',
        r'^ls\s+-l',
    ]
    for pat in passthrough:
        if re.search(pat, command, re.IGNORECASE):
            return "passthrough"
    
    return "compress"


# =============================================================================
# Pattern 路由
# =============================================================================

def compress_output(command: str, output: str) -> Optional[str]:
    """
    Shell 输出压缩主入口
    
    Args:
        command: 原始命令
        output: 原始输出
    
    Returns:
        压缩后的字符串，如果无法压缩则返回 None
    """
    policy = classify(command)
    
    if policy == "protected":
        return None
    
    # 先清理 ANSI
    cleaned = _strip_ansi(output)
    if len(cleaned) < len(output):
        output = cleaned
    
    if policy == "passthrough":
        # passthrough: 去除空行、截断超长输出
        lines = [l.rstrip() for l in output.split('\n') if l.strip()]
        if len(lines) > 200:
            lines = _compact_lines(lines, 100)
        return '\n'.join(lines)
    
    # compress: 尝试各 pattern
    # 按使用频率排序
    if (cmd_starts_with(command, "git ") or cmd_starts_with(command, "git.exe")):
        result = _compress_git(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "npm ") or cmd_starts_with(command, "yarn "):
        result = _compress_npm(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "pnpm "):
        result = _compress_pnpm(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "cargo "):
        result = _compress_cargo(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "pytest") or cmd_starts_with(command, "python -m pytest"):
        result = _compress_pytest(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "ruff "):
        result = _compress_ruff(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "mypy") or cmd_starts_with(command, "python -m mypy"):
        result = _compress_mypy(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "pip "):
        result = _compress_pip(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "docker ") or cmd_starts_with(command, "docker-compose "):
        result = _compress_docker(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "kubectl "):
        result = _compress_kubectl(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "go "):
        result = _compress_go(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "node "):
        result = _compress_node(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "npx "):
        result = _compress_npx(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "tsc") or "typescript" in command.lower():
        result = _compress_typescript(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "eslint") or cmd_starts_with(command, "biome "):
        result = _compress_eslint(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "curl "):
        result = _compress_curl(command, output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "grep ") or cmd_starts_with(command, "rg "):
        result = _compress_grep(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "find "):
        result = _compress_find(output)
        if result:
            return _shorter_only(result, output) or result
    
    if cmd_starts_with(command, "ls ") or command.strip() == "ls":
        result = _compress_ls(output)
        if result:
            return _shorter_only(result, output) or result
    
    # 回退：通用压缩（去除空行，截断超长输出）
    result = _compress_generic(output)
    if result:
        return _shorter_only(result, output) or result
    
    return None


def cmd_starts_with(command: str, prefix: str) -> bool:
    """大小写不敏感的命令前缀匹配"""
    return command.strip().lower().startswith(prefix.lower())


# =============================================================================
# Git 压缩
# =============================================================================

def _compress_git(command: str, output: str) -> Optional[str]:
    """Git 各子命令的压缩"""
    cmd_lower = command.strip().lower()
    
    # git status
    if re.search(r'git\s+status', cmd_lower):
        return _compress_git_status(output)
    
    # git log
    if re.search(r'git\s+log', cmd_lower):
        return _compress_git_log(command, output)
    
    # git diff (不压缩 git diff 的实际代码内容，只压缩统计行)
    if re.search(r'git\s+diff', cmd_lower):
        return _compress_git_diff(output)
    
    # git add
    if re.search(r'git\s+add', cmd_lower):
        return _compress_git_add(output)
    
    # git commit
    if re.search(r'git\s+commit', cmd_lower):
        return _compress_git_commit(output)
    
    # git push
    if re.search(r'git\s+push', cmd_lower):
        return _compress_git_push(output)
    
    # git pull
    if re.search(r'git\s+pull', cmd_lower):
        return _compress_git_pull(output)
    
    # git branch
    if re.search(r'git\s+branch', cmd_lower):
        return _compress_git_branch(output)
    
    # git stash
    if re.search(r'git\s+stash', cmd_lower):
        return _compress_git_stash(command, output)
    
    # git clone
    if re.search(r'git\s+clone', cmd_lower):
        return _compress_git_clone(output)
    
    # git fetch
    if re.search(r'git\s+fetch', cmd_lower):
        return _compress_git_fetch(output)
    
    # git merge
    if re.search(r'git\s+merge', cmd_lower):
        return _compress_git_merge(output)
    
    return None


def _compress_git_status(output: str) -> str:
    """git status 压缩为核心信息"""
    lines = output.strip().split('\n')
    branch = "?"
    staged = []
    unstaged = []
    untracked = []
    section = ""
    ahead = 0
    
    for line in lines:
        t = line.strip()
        
        # 分支
        m = re.match(r'On branch (.+)', t)
        if m:
            branch = m.group(1)
            continue
        
        m = re.match(r'Your branch is ahead .+ by (\d+) commit', t)
        if m:
            ahead = int(m.group(1))
            continue
        
        # 分段
        if 'Changes to be committed' in t:
            section = "staged"
            continue
        if 'Changes not staged' in t:
            section = "unstaged"
            continue
        if 'Untracked files' in t:
            section = "untracked"
            continue
        
        # 文件
        if t.startswith('new file:'):
            f = t[9:].strip()
            if section == "staged":
                staged.append(f"+{f}")
        elif t.startswith('modified:'):
            f = t[8:].strip()
            if section == "staged":
                staged.append(f"~{f}")
            elif section == "unstaged":
                unstaged.append(f"~{f}")
        elif t.startswith('deleted:'):
            f = t[8:].strip()
            if section == "staged":
                staged.append(f"-{f}")
        elif t.startswith('renamed:'):
            f = t[8:].strip()
            if section == "staged":
                staged.append(f"->{f}")
        elif section == "untracked" and t and not t.startswith('(') and not t.startswith('Untracked'):
            untracked.append(t)
    
    # 构造输出
    parts = []
    if ahead > 0:
        parts.append(f"{branch} ↑{ahead}")
    else:
        parts.append(branch)
    
    if staged:
        parts.append(f"staged: {' '.join(staged)}")
    if unstaged:
        parts.append(f"unstaged: {' '.join(unstaged)}")
    if untracked:
        parts.append(f"untracked: {' '.join(untracked)}")
    if "nothing to commit" in output:
        parts.append("clean")
    
    return '\n'.join(parts)


def _compress_git_log(command: str, output: str) -> str:
    """git log 压缩"""
    # --oneline 格式
    if '--oneline' in command:
        lines = [l.strip() for l in output.strip().split('\n') if l.strip()]
        if len(lines) <= 10:
            return output.strip()
        # 截断到前 8 行，加摘要
        return '\n'.join(lines[:8]) + f"\n... ({len(lines) - 8} more commits)"
    
    # 标准 log：保留前 3 条完整信息，剩余截断
    commits = re.split(r'\n(?=commit\s)', output)
    if len(commits) <= 3:
        # -p 或 --stat 的完整 diff，保留
        if any('-p' in command or '--patch' in command for _ in [1]):
            if 'diff --git' in output:
                return output  # 完整保留
        return output.strip()
    
    # 截断
    kept = commits[:3]
    if 'diff --git' in output:
        # 保留最后一个完整 diff，其余截断
        kept = commits[:1]
    
    return '\n'.join(kept) + f"\n... ({len(commits) - len(kept)} more commits)"


def _compress_git_diff(output: str) -> str:
    """git diff 压缩（保留实际内容，只压缩统计行）"""
    # --stat 只保留汇总
    if re.match(r'\s*\d+\s+file', output) and 'diff --git' not in output:
        return output.strip()
    
    # 有实际 diff 内容，保留 hunks + 统计
    stat_match = re.search(r'(\d+\s+files?\s+changed.*)', output)
    stats = stat_match.group(1) if stat_match else ""
    
    # 提取关键文件变更
    files = re.findall(r'(diff --git.*?)(?=\n(?:diff --git|---|\+\+\+|\Z))', output, re.DOTALL)
    if not files:
        return output.strip()
    
    # 保留前 3 个文件的完整 diff
    compressed = []
    for f in files[:3]:
        compressed.append(f.strip())
    
    if stats:
        compressed.append(stats)
    if len(files) > 3:
        compressed.append(f"... ({len(files) - 3} more files)")
    
    return '\n'.join(compressed)


def _compress_git_add(output: str) -> str:
    """git add 压缩"""
    if not output.strip():
        return "ok"
    return output.strip()


def _compress_git_commit(output: str) -> str:
    """git commit 压缩"""
    parts = []
    
    # hook 输出
    hook_lines = []
    for line in output.split('\n'):
        if re.match(r'[a-z][a-z0-9_-]+\s+\.+', line):
            hook_lines.append(line.strip())
        elif line.strip().startswith('['):
            break
    
    if hook_lines:
        if len(hook_lines) > 5:
            parts.append(f"{len(hook_lines)} hooks passed")
        else:
            parts.extend(hook_lines)
    
    # commit hash + message
    m = re.search(r'\[([^\s]+)\s+([a-f0-9]+)\]\s*(.+)', output)
    if m:
        branch = m.group(1)
        hash_ = m.group(2)[:7]
        msg = m.group(3).strip()
        parts.append(f"{branch} {hash_} {msg}")
    
    # 变更统计
    m = re.search(r'(\d+)\s+files?\s+changed.*', output)
    if m:
        parts.append(output[output.rfind(m.group(0)):].strip())
    
    return '\n'.join(parts) if parts else output.strip()


def _compress_git_push(output: str) -> str:
    """git push 压缩"""
    parts = []
    
    # 远程信息
    if 'To ' in output:
        m = re.search(r'To\s+(\S+)', output)
        if m:
            parts.append(f"-> {m.group(1)}")
    
    # ref 更新
    m = re.search(r'([a-f0-9]+)\.\.([a-f0-9]+)\s+(\S+)\s*->\s*(\S+)', output)
    if m:
        parts.append(f"{m.group(3)}: {m.group(2)[:7]}..{m.group(4)[:7]}")
    
    # PR/MR URL
    if 'pull/' in output or 'merge_requests' in output:
        m = re.search(r'(https://\S+(?:pull/\S+|merge_requests/\S+))', output)
        if m:
            parts.append(m.group(1))
    
    # pipeline URL
    if 'pipeline' in output.lower():
        m = re.search(r'(https://\S+pipelines/\S+)', output)
        if m:
            parts.append(m.group(1))
    
    return '\n'.join(parts) if parts else output.strip()


def _compress_git_pull(output: str) -> str:
    """git pull 压缩"""
    if 'Already up to date' in output:
        return "Already up to date"
    if 'Updating' in output:
        m = re.search(r'Updating\s+([a-f0-9]+)\.\.([a-f0-9]+)', output)
        if m:
            return f"Updated {m.group(1)[:7]}..{m.group(2)[:7]}"
    return output.strip()


def _compress_git_branch(output: str) -> str:
    """git branch 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip() and not l.startswith(' ')]
    # 当前分支有 *，压缩为列表
    branches = [l.replace('* ', '') for l in lines if l != '* (HEAD detached']
    if len(branches) <= 5:
        return '\n'.join(branches)
    return '\n'.join(branches[:5]) + f"\n... ({len(branches) - 5} more)"


def _compress_git_stash(command: str, output: str) -> str:
    """git stash 压缩"""
    if 'stash list' in command:
        lines = [l.strip() for l in output.split('\n') if l.strip() and 'stash@' in l]
        if len(lines) <= 5:
            return '\n'.join(lines)
        return '\n'.join(lines[:3]) + f"\n... ({len(lines) - 3} more)"
    
    if 'stash pop' in command or 'stash drop' in command or 'stash apply' in command:
        if output.strip():
            return output.strip()
        return "ok"
    
    if 'stash show' in command:
        return output.strip()
    
    # git stash (push)
    if output.strip():
        return output.strip()
    return "ok"


def _compress_git_clone(output: str) -> str:
    """git clone 压缩"""
    if 'Cloning into' in output:
        m = re.search(r"Cloning into\s+'([^']+)'", output)
        dest = m.group(1) if m else "?"
        parts = [f"Cloning {dest}"]
        # 进度
        m = re.search(r'Receiving objects:\s*(\d+)%', output)
        if m:
            parts.append(f"{m.group(1)}%")
        return ' | '.join(parts)
    return output.strip()


def _compress_git_fetch(output: str) -> str:
    """git fetch 压缩"""
    if not output.strip():
        return "ok"
    # 简单返回
    if 'Fetching' in output or 'receiving' in output.lower():
        return "fetching..."
    return output.strip()


def _compress_git_merge(output: str) -> str:
    """git merge 压缩"""
    if 'Already up to date' in output:
        return "Already up to date"
    if 'Merge made' in output or 'Fast-forward' in output:
        m = re.search(r'(\d+)\s+files?\s+changed.*', output)
        stats = m.group(0) if m else ""
        return f"merge ok {stats}".strip()
    return output.strip()


# =============================================================================
# npm/yarn/pnpm 压缩
# =============================================================================

def _compress_npm(command: str, output: str) -> str:
    """npm 压缩"""
    cmd = command.lower()
    
    if 'npm install' in cmd or 'npm i' in cmd:
        return _compress_npm_install(output)
    
    if 'npm test' in cmd:
        return _compress_npm_test(output)
    
    if 'npm run' in cmd:
        return _compress_npm_run(output, command)
    
    if 'npm list' in cmd:
        return _compress_npm_list(output)
    
    if 'npm outdated' in cmd:
        return _compress_npm_outdated(output)
    
    if 'npm audit' in cmd:
        return _compress_npm_audit(output)
    
    return output.strip()


def _compress_npm_install(output: str) -> str:
    """npm install 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    added = 0
    for line in lines:
        m = re.search(r'added\s+(\d+)\s+packages?', line)
        if m:
            added = int(m.group(1))
        if 'audited' in line:
            m = re.search(r'audited\s+(\d+)\s+packages?', line)
            if m:
                return f"+{added} (audited {m.group(1)} pkgs)"
    return output.strip()


def _compress_npm_test(output: str) -> str:
    """npm test 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    for line in lines:
        if 'FAIL' in line or 'failed' in line.lower():
            return line
    # 全部通过
    for line in lines:
        m = re.search(r'(\d+)\s+pass', line, re.IGNORECASE)
        if m:
            return f"{m.group(1)} passed"
    return output.strip()


def _compress_npm_run(cmd: str, output: str) -> str:
    """npm run <script> 压缩 - 根据脚本名判断"""
    # 提取脚本名
    m = re.search(r'npm\s+run\s+(\w+)', cmd)
    if not m:
        return output.strip()
    script = m.group(1)
    
    # build/dev/lint 等常见脚本
    if script in ('build', 'dev', 'start', 'preview'):
        for line in output.split('\n'):
            if 'compiled with warnings' in line or 'compiled successfully' in line:
                return line.strip()
            if 'error' in line.lower() and 'warning' not in line.lower():
                return line.strip()
        return output.strip()
    
    return output.strip()


def _compress_npm_list(output: str) -> str:
    """npm list 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip() and '`' not in l and '|' not in l]
    if len(lines) > 20:
        lines = lines[:10] + [f"... ({len(lines) - 10} more)"]
    return '\n'.join(lines)


def _compress_npm_outdated(output: str) -> str:
    """npm outdated 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip() and '`' not in l and '|' not in l]
    return '\n'.join(lines)


def _compress_npm_audit(output: str) -> str:
    """npm audit 压缩"""
    for line in output.split('\n'):
        if 'found' in line.lower() and ('vulnerabilit' in line.lower() or 'issues' in line.lower()):
            return line.strip()
    return output.strip()


def _compress_pnpm(command: str, output: str) -> str:
    """pnpm 压缩"""
    cmd = command.lower()
    if 'pnpm install' in cmd or 'pnpm add' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        for line in lines:
            if 'Done in' in line or 'added' in line:
                return line.strip()
        return output.strip()
    return output.strip()


# =============================================================================
# Cargo 压缩
# =============================================================================

def _compress_cargo(command: str, output: str) -> str:
    """cargo 压缩"""
    cmd = command.lower()
    
    if 'cargo build' in cmd or 'cargo check' in cmd or 'cargo clippy' in cmd:
        return _compress_cargo_build(output)
    
    if 'cargo test' in cmd or 'cargo bench' in cmd:
        return _compress_cargo_test(output)
    
    if 'cargo run' in cmd:
        return _compress_cargo_run(output)
    
    return output.strip()


def _compress_cargo_build(output: str) -> str:
    """cargo build/check/clippy 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    parts = []
    
    for line in lines:
        if line.startswith('Compiling ') or line.startswith('   Compiling '):
            # 提取 crate 名
            m = re.match(r'^\s*Compiling\s+(\S+)', line)
            if m:
                parts.append(f"Compiling {m.group(1)}")
        elif 'Finished' in line:
            parts.append(line)
            return '\n'.join(parts)
        elif 'error[' in line or 'error:' in line:
            parts.append(line)
        elif 'warning[' in line and len(parts) < 3:
            parts.append(line)
    
    if parts:
        return '\n'.join(parts)
    return output.strip()


def _compress_cargo_test(output: str) -> str:
    """cargo test 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    for line in lines:
        if 'test result:' in line or re.match(r'\d+\s+passed', line):
            return line
        if 'FAILED' in line or 'test result: .*failures' in output:
            for l in lines:
                if 'FAILED' in l or 'failures:' in l:
                    return l
    return output.strip()


def _compress_cargo_run(output: str) -> str:
    """cargo run 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    parts = []
    for line in lines:
        if 'Compiling' in line:
            continue  # 跳过编译输出
        if 'Finished' in line:
            parts.append(line)
        else:
            parts.append(line)
    return '\n'.join(parts[:20])  # 限制行数


# =============================================================================
# pytest / 测试压缩
# =============================================================================

def _compress_pytest(output: str) -> str:
    """pytest 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    for line in lines:
        # 通过摘要
        m = re.search(r'(\d+)\s+passed', line)
        if m:
            failed_m = re.search(r'(\d+)\s+failed', line)
            skipped_m = re.search(r'(\d+)\s+skipped', line)
            time_m = re.search(r'in\s+([\d.]+)s', line)
            
            parts = [f"{m.group(1)} passed"]
            if failed_m:
                parts.append(f"{failed_m.group(1)} failed")
            if skipped_m:
                parts.append(f"{skipped_m.group(1)} skipped")
            if time_m:
                parts.append(f"({time_m.group(1)}s)")
            return ', '.join(parts)
    
    # 失败详情
    for line in lines:
        if 'FAILED' in line or 'ERROR' in line:
            return line
    
    # 收集错误
    errors = [l for l in lines if 'error' in l.lower() and ('Error' in l or 'error:' in l)]
    if errors:
        return '\n'.join(errors[:5])
    
    return output.strip()


# =============================================================================
# Ruff / Linter 压缩
# =============================================================================

def _compress_ruff(output: str) -> str:
    """ruff 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    errors = []
    summary = None
    
    for line in lines:
        # 错误列表
        if re.search(r'\.(py|rs|ts|js):\d+', line):
            errors.append(line)
        # 汇总
        m = re.search(r'Found\s+(\d+)\s+(error|warning)', line, re.IGNORECASE)
        if m:
            summary = f"{m.group(1)} {m.group(2)}s"
    
    if errors:
        result = errors[:10]
        if summary:
            result.append(summary)
        if len(errors) > 10:
            result.append(f"... ({len(errors) - 10} more)")
        return '\n'.join(result)
    
    return output.strip()


def _compress_mypy(output: str) -> str:
    """mypy 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    errors = []
    summary = None
    
    for line in lines:
        if re.search(r'\.py:\d+', line):
            errors.append(line)
        m = re.search(r'Found\s+(\d+)\s+error', line, re.IGNORECASE)
        if m:
            summary = f"{m.group(1)} errors"
        m = re.search(r'Success:', line)
        if m:
            return "Success"
    
    if errors:
        result = errors[:8]
        if summary:
            result.append(summary)
        return '\n'.join(result)
    
    return output.strip()


def _compress_eslint(command: str, output: str) -> str:
    """ESLint/biome 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    # 有错误列表
    errors = []
    for line in lines:
        if re.search(r'\.(ts|js|tsx|jsx):\d+', line):
            errors.append(line)
    
    if errors:
        result = errors[:10]
        # 统计
        total = len(errors)
        m = re.search(r'(\d+)\s+(problems?|errors?|warnings?)', output, re.IGNORECASE)
        if m:
            result.append(f"{m.group(1)} {m.group(2)}")
        if total > 10:
            result.append(f"... ({total - 10} more)")
        return '\n'.join(result)
    
    return output.strip()


# =============================================================================
# Python 包管理压缩
# =============================================================================

def _compress_pip(command: str, output: str) -> str:
    """pip 压缩"""
    cmd = command.lower()
    
    if 'pip install' in cmd or 'pip3 install' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        for line in lines:
            if 'Successfully installed' in line or 'Requirement already satisfied' in line:
                return line
            m = re.search(r'Successfully\s+downloaded\s+(\d+)\s+packages?', line)
            if m:
                return f"downloaded {m.group(1)} packages"
        return output.strip()
    
    if 'pip list' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        return '\n'.join(lines[:15])
    
    return output.strip()


# =============================================================================
# Docker 压缩
# =============================================================================

def _compress_docker(command: str, output: str) -> str:
    """docker 压缩"""
    cmd = command.lower()
    
    if 'docker build' in cmd:
        return _compress_docker_build(output)
    
    if 'docker images' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        if len(lines) > 10:
            return '\n'.join(lines[:5]) + f"\n... ({len(lines) - 5} more)"
        return output.strip()
    
    return output.strip()


def _compress_docker_build(output: str) -> str:
    """docker build 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    parts = []
    
    for line in lines:
        # Step 信息
        m = re.match(r'Step \d+/(\d+)\s*:\s*(.+)', line)
        if m:
            parts.append(f"Step {m.group(1)}: {m.group(2)[:60]}")
        # 成功
        if 'Successfully built' in line or 'Successfully tagged' in line:
            m = re.search(r'Successfully\s+(built|tagged)\s+(\S+)', line)
            if m:
                parts.append(f"ok {m.group(2)[:40]}")
        # 失败
        if 'error' in line.lower() and 'Step' in line:
            parts.append(line[:120])
    
    return '\n'.join(parts) if parts else output.strip()


# =============================================================================
# kubectl 压缩
# =============================================================================

def _compress_kubectl(command: str, output: str) -> str:
    """kubectl 压缩"""
    cmd = command.lower()
    
    if 'kubectl get' in cmd and 'pod' in cmd:
        return _compress_kubectl_pods(output)
    
    if 'kubectl logs' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        if len(lines) > 50:
            return '\n'.join(lines[:30]) + f"\n... ({len(lines) - 30} more lines)"
        return output.strip()
    
    return output.strip()


def _compress_kubectl_pods(output: str) -> str:
    """kubectl get pods 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip() and not l.startswith('NAME')]
    
    if not lines:
        return output.strip()
    
    pods = []
    for line in lines:
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0]
            status = parts[2] if len(parts) > 2 else "?"
            pods.append(f"{name} {status}")
    
    # 统计
    running = sum(1 for p in pods if 'Running' in p)
    ready = sum(1 for p in pods if '1/1' in p or '2/2' in p)
    crash = sum(1 for p in pods if 'CrashLoop' in p or 'Error' in p or 'Evicted' in p)
    
    result = [f"{len(pods)} pods: {running} Running, {ready} Ready"]
    if crash:
        result.append(f"{crash} not ready")
    result.extend(pods[:5])
    if len(pods) > 5:
        result.append(f"... ({len(pods) - 5} more)")
    
    return '\n'.join(result)


# =============================================================================
# Go 压缩
# =============================================================================

def _compress_go(command: str, output: str) -> str:
    """go 压缩"""
    cmd = command.lower()
    
    if 'go test' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        for line in lines:
            if 'ok' in line or 'FAIL' in line:
                return line
        return output.strip()
    
    if 'go build' in cmd:
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        if not lines:
            return "ok"
        return lines[0] if len(lines) <= 3 else '\n'.join(lines[:3])
    
    return output.strip()


# =============================================================================
# Node/npx 压缩
# =============================================================================

def _compress_node(command: str, output: str) -> str:
    """node 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    # 通常是单行输出
    if len(lines) == 1:
        return lines[0]
    return output.strip()


def _compress_npx(command: str, output: str) -> str:
    """npx 压缩"""
    cmd = command.lower()
    
    # vite / next 等
    if 'vite' in cmd:
        return _compress_vite(output)
    
    if 'next' in cmd:
        return _compress_next(output)
    
    return output.strip()


def _compress_vite(output: str) -> str:
    """vite 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    for line in lines:
        if 'ready in' in line.lower() or 'built in' in line.lower():
            return line
        if 'error' in line.lower():
            return line
    return output.strip()


def _compress_next(output: str) -> str:
    """next 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    for line in lines:
        if 'compiled' in line.lower() or 'ready' in line.lower():
            return line
        if 'error' in line.lower():
            return line
    return output.strip()


# =============================================================================
# TypeScript (tsc) 压缩
# =============================================================================

def _compress_typescript(output: str) -> str:
    """tsc 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    errors = []
    summary = None
    
    for line in lines:
        if re.search(r'\.(ts|tsx):\d+', line):
            errors.append(line)
        m = re.search(r'Found\s+(\d+)\s+errors?', line, re.IGNORECASE)
        if m:
            summary = f"{m.group(1)} errors"
    
    if errors:
        result = errors[:8]
        if summary:
            result.append(summary)
        return '\n'.join(result)
    
    return output.strip()


# =============================================================================
# curl 压缩
# =============================================================================

def _compress_curl(command: str, output: str) -> str:
    """curl 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    # JSON 响应
    if output.strip().startswith('{') or output.strip().startswith('['):
        m = re.search(r'"(\w+)"\s*:\s*[^{[]+', output)
        if m and len(output) > 500:
            return f"[JSON] {output[:200]}... ({len(output)} bytes)"
        return output.strip()[:500]
    
    # HTTP 状态
    status_m = re.search(r'HTTP/[.\d]+\s+(\d+)', output)
    if status_m:
        status = status_m.group(1)
        size_m = re.search(r'Content-Length:\s*(\d+)', output)
        size = f" {size_m.group(1)}B" if size_m else ""
        return f"{status}{size}"
    
    return output.strip()[:500]


# =============================================================================
# grep / find 压缩
# =============================================================================

def _compress_grep(output: str) -> str:
    """grep/rg 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    # 统计匹配
    for line in lines:
        m = re.search(r'(\d+)\s+matches?', line, re.IGNORECASE)
        if m:
            return f"{m.group(1)} matches"
    
    if len(lines) > 20:
        return '\n'.join(lines[:10]) + f"\n... ({len(lines) - 10} more)"
    
    return '\n'.join(lines)


def _compress_find(output: str) -> str:
    """find 压缩"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    if len(lines) > 30:
        return '\n'.join(lines[:15]) + f"\n... ({len(lines) - 15} more)"
    return '\n'.join(lines)


# =============================================================================
# ls 压缩
# =============================================================================

def _compress_ls(output: str) -> str:
    """ls 压缩（仅文件名列表）"""
    lines = [l.strip() for l in output.split('\n') if l.strip()]
    
    # 去掉详情列（权限、大小、日期等）
    compressed = []
    for line in lines:
        if line.startswith('total'):
            continue
        # 提取文件名（最后一列或第一个非数字列）
        parts = line.split()
        if parts:
            compressed.append(parts[-1])
    
    if len(compressed) > 30:
        return '\n'.join(compressed[:20]) + f"\n... ({len(compressed) - 20} more)"
    return '\n'.join(compressed)


# =============================================================================
# 通用回退压缩
# =============================================================================

def _compress_generic(output: str) -> Optional[str]:
    """通用压缩：去除空行、截断超长输出"""
    lines = [l.rstrip() for l in output.split('\n') if l.strip()]
    
    if len(lines) <= 3:
        return output.strip()
    
    if len(lines) > 100:
        return '\n'.join(_compact_lines(lines, 50))
    
    return '\n'.join(lines)


# =============================================================================
# 公开 API
# =============================================================================

def compress(command: str, output: str, include_stats: bool = True) -> str:
    """
    Shell 输出进行智能压缩
    
    Args:
        command: 原始命令
        output: 原始输出
        include_stats: 是否包含 token 节省统计（默认 True）
    
    Returns:
        压缩后的字符串（永远不会返回 None，即使无法压缩也返回清理后的输出）
    """
    # 如果输出太短，不压缩
    if len(output.strip()) < 50:
        return output.strip()
    
    # 计算原始 token 数
    orig_tokens = _count_tokens(output)
    
    # 执行压缩
    result = compress_output(command, output)
    if result is None:
        # 无法压缩：清理 ANSI 后返回
        return _strip_ansi(output).strip()
    
    # 只有压缩后更短才使用压缩结果
    compressed = _shorter_only(result, output)
    if compressed is None:
        return output.strip()
    
    # 添加 token 节省统计
    if include_stats and _count_tokens(compressed) < orig_tokens:
        new_tokens = _count_tokens(compressed)
        saved = orig_tokens - new_tokens
        pct = int((saved / orig_tokens) * 100) if orig_tokens > 0 else 0
        compressed = f"{compressed}\n[saved {saved} tok, {pct}%]"
    
    return compressed


def track(command: str, output: str, exit_code: int = 0, elapsed: float = 0) -> str:
    """
    Track Mode - 仅返回元数据，不返回实际输出
    
    适用于 dev server 等长时间运行或输出不重要的命令
    
    Args:
        command: 原始命令
        output: 原始输出（会被丢弃）
        exit_code: 退出码
        elapsed: 执行耗时（秒）
    
    Returns:
        元数据字符串，如 "exit=0 (12.4s)"
    """
    # 判断是否应该 track（长输出或特定命令）
    should_track = (
        len(output) > 5000 or  # 输出超过 5KB
        'npm run dev' in command.lower() or
        'yarn dev' in command.lower() or
        'python -m http.server' in command.lower() or
        'docker compose up' in command.lower()
    )
    
    if not should_track:
        # 输出短或非 track 类型命令，走正常压缩
        return compress(command, output, include_stats=False)
    
    # Track mode：只返回元数据
    status = "ok" if exit_code == 0 else f"fail({exit_code})"
    if elapsed > 0:
        return f"{status} ({elapsed:.1f}s)"
    return status