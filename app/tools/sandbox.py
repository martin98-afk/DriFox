# -*- coding: utf-8 -*-
"""
沙箱拦截核心（L1）

职责：
- SandboxConfig：sandbox_config.json 的加载/保存/单例（点路径 get/set）
- check_path / check_command / check_network：三分法判定，返回 "allow"|"confirm"|"deny"
- sandbox_check_tool：工具名 + 参数 → 判定（worker 层唯一入口）
- stash_delete_targets_to_trash：垃圾桶开启时，删除类命令执行前把目标移入垃圾箱
- OS 级沙箱（macOS Seatbelt）：build/wrap 把命令包进 sandbox-exec，内核层兑底

本模块不弹 UI；审批呈现由 chat_worker 既有弹窗链负责。
L1 三分法负责「要不要拦截/审批」，OS 级沙箱负责「放行进程实际能碰什么」，两层正交。
设计文档：docs/superpowers/specs/2026-09-18-sandbox-l1-security-center-design.md
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Union

from loguru import logger

# 三分法判定值
ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

Verdict = str

DEFAULT_CONFIG = {
    # ★ 默认关闭：L1 沙箱与删除保护仍处完善期，机制成熟后再改默认开启；
    # 已有配置文件中显式写入的值不受此处影响（_deep_merge 磁盘值优先）
    "sandbox_enabled": False,
    "path": {"whitelist": [], "blacklist": []},
    "command": {"allow_prefixes": [], "confirm_prefixes": []},
    "network": {"enabled": True, "blacklist_domains": []},
    "sys_tools_bypass": False,
    # OS 级沙箱（macOS Seatbelt/sandbox-exec；非 darwin 平台自动直通）。
    # enabled 默认 False：新机制完善期不默认开启（与 sandbox_enabled 同理，跨平台默认行为一致）；
    # level: "write"=工作区+/tmp 外禁写（放行网络）| "write+net"=禁写且禁网
    "os_sandbox": {"enabled": False, "level": "write"},
    "delete_protection": False,
    "delete": {"exempt_paths": [], "trash_enabled": False},
    "job_limits": {"memory_mb": 2048, "active_process": 64, "cpu_time_ms": 0},
    # 0 = 不限（不自动清理），>0 = 启动期与手动清理的上限
    "backup_limit_mb": 3000,
    # gateway 平台侧权限请求是否自动放行（True=保持现状；False=遵循沙箱判定，
    # 无人值守场景可能因等不到审批而失败）
    "gateway_auto_approve": True,
    # 审核处理：审批方式/超时/超时处理/AI 审核模型（安全中心「审核处理」组）
    "approval": {
        "mode": "manual",  # manual 人工审批 | ai AI审批（静默决策，失败回落人工）
        "timeout_sec": 120,  # 人工审批等待超时秒数，0=不超时
        "timeout_action": "deny",  # deny 拒绝 | allow 允许 | ai AI审核
        "ai_model": "",  # 审核模型 "服务商display_name:模型名"，空=跟随主模型
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，缺键由 base 补齐（递归）"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class SandboxConfig:
    """sandbox_config.json 配置（点路径读写，进程级单例）"""

    _instance: Optional["SandboxConfig"] = None

    def __init__(self, config_path: Union[str, Path, None] = None):
        if config_path == ":memory:":
            self._path: Optional[Path] = None
        else:
            from app.utils.utils import get_app_data_dir

            self._path = Path(config_path) if config_path else get_app_data_dir() / "sandbox_config.json"
        self._data: dict = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    @classmethod
    def get_instance(cls) -> "SandboxConfig":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """测试用：强制下次 get_instance 重建"""
        cls._instance = None

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            disk = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = _deep_merge(DEFAULT_CONFIG, disk if isinstance(disk, dict) else {})
        except Exception as e:  # noqa: BLE001 - 配置损坏回落默认，不阻断启动
            logger.warning(f"[Sandbox] 配置加载失败，使用默认值: {e}")

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, dotted: str, default=None):
        """点路径读取：get("path.whitelist")"""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        """点路径写入：set("path.blacklist", [...])，不自动落盘（显式 save）"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


# ============================================================
# 路径边界（写窄读宽）
# ============================================================
# 读/写工具的归属由 ToolRegistry 分组驱动（sandbox_check_tool），
# 分组名与插件侧 GROUP_WRITE/GROUP_READ（plugins/system-tools/tools/file_tools.py:109）
# 对齐；不在此写死工具名清单——插件增删热生效，写死必漏。


def _norm(p: Union[str, Path]) -> str:
    """展开环境变量/~ 后 resolve，统一大小写与分隔符（Windows 比较用）"""
    raw = str(p)
    expanded = os.path.expandvars(raw)
    return os.path.normcase(str(Path(expanded).expanduser().resolve()))


def _under(child: str, parent: str) -> bool:
    """child 是否位于 parent 目录内（含相等）"""
    if parent == child:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


def _match_blacklist(norm_path: str, blacklist: list) -> bool:
    """黑名单条目：绝对目录前缀匹配，或裸文件名匹配（如 ".env" 命中任意层级 .env）

    ⚠ 绝对条目的前缀匹配必须走 `_under`（含 `rstrip("\\/") + os.sep` 边界），
    否则 `D:/work/proj/secret` 会误命中 `secret2/b.txt` 与 `secret_backup/c.txt`
    —— 属误报（过度拦截），用户会困惑"为什么没配这个目录也被拦"。
    """
    for item in blacklist:
        item_str = str(item)
        if os.path.isabs(os.path.expandvars(item_str)):
            if _under(norm_path, _norm(item_str)):
                return True
        else:
            if Path(norm_path).name.lower() == item_str.lower():
                return True
    return False


def check_path(
    workdir: Union[str, Path],
    path: str,
    mode: str,
    cfg: Optional[SandboxConfig] = None,
) -> Verdict:
    """路径边界判定。mode: "read"|"write"。优先级：黑名单 > 白名单 > workdir 边界"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    if not path or not str(path).strip():
        return ALLOW

    norm_path = _norm(path)
    blacklist: list = list(cfg.get("path.blacklist") or [])
    whitelist: list = list(cfg.get("path.whitelist") or [])

    # 黑名单最高优先（读写都拦）
    if blacklist and _match_blacklist(norm_path, blacklist):
        return CONFIRM
    # 白名单放行出界写
    # 绝对条目直接用；相对条目按 **workdir** 解析（不是进程 cwd），
    # 否则用户在 UI 填 `other_proj` 会静默失效。
    # ⚠ 必须先 expandvars 再判绝对：`%USERPROFILE%\docs` 这类含环境变量的
    # 条目 os.path.isabs() 返回 False，不展开会被误当相对路径拼到 workdir 下。
    for item in whitelist:
        item_str = str(item)
        if os.path.isabs(os.path.expandvars(item_str)):
            if _under(norm_path, _norm(item_str)):
                return ALLOW
        else:
            if _under(norm_path, _norm(Path(workdir) / item_str)):
                return ALLOW
    if mode == "write":
        workdir_norm = _norm(workdir)
        return ALLOW if _under(norm_path, workdir_norm) else CONFIRM
    return ALLOW


# ============================================================
# 命令边界（递归解壳 + 用户名单叠加）
# ============================================================
# 系统级工具：沙箱管不住其内部行为（如 wsl 内的 Linux 侧操作）
SYS_LEVEL_TOOLS = frozenset({"wsl", "wmic", "sc", "reg", "schtasks", "diskpart", "bcdedit"})


def _first_token(command: str) -> str:
    """首 token 归一化：去路径前缀、去 .exe、小写"""
    token = command.strip().split()[0].lower().replace("\\", "/").split("/")[-1]
    return token[:-4] if token.endswith(".exe") else token


def _prefix_match(command: str, prefix: str) -> bool:
    """命令是否命中白名单前缀（**带词边界**，EU-G24）

    判据：前缀与命令在**词边界**处对齐 —— 即命令等于前缀，或前缀后紧跟
    空白/行尾。避免 `git` 误命中 `gitsomething`，也避免 `npm run` 误命中
    `npm runner`。

    条目本身可含参数（如 `git status`），此时按整串前缀 + 边界匹配。
    """
    if not command or not prefix:
        return False
    cmd = command.strip().lower()
    pre = str(prefix).strip().lower()
    if not pre:
        return False
    if not cmd.startswith(pre):
        return False
    if len(cmd) == len(pre):
        return True
    return cmd[len(pre)] in (" ", "\t")


def _matches_any_prefix(command: str, prefixes) -> bool:
    """命令是否命中任一用户前缀名单（带词边界，大小写不敏感）

    用于 EU-G23：用户显式写进 `command.confirm_prefixes` 的命令，不应因
    `delete_protection` 关闭而被放行 —— 显式加严优先于开关默认值。
    EU-G24：与白名单共用 `_prefix_match` 的边界语义，两条路径口径一致。
    """
    if not command:
        return False
    return any(_prefix_match(command, str(p)) for p in (prefixes or []))


def check_command(command: str, cfg: Optional[SandboxConfig] = None) -> Verdict:
    """命令判定：递归解壳 + 用户名单叠加 + 系统级工具豁免"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    if not command or not command.strip():
        return ALLOW

    from app.tools.command_safety import classify_command_deep

    stripped = command.strip()

    # 用户 confirm 名单优先于 allow 名单（显式加严；边界匹配见 _prefix_match）
    for prefix in cfg.get("command.confirm_prefixes") or []:
        if _prefix_match(stripped, str(prefix)):
            return CONFIRM
    # 系统级工具豁免
    if cfg.get("sys_tools_bypass") and _first_token(stripped) in SYS_LEVEL_TOOLS:
        return ALLOW
    # 用户 allow 名单：**有条件放行**
    # ⚠ EU-G24：此前是纯 `startswith` 前缀匹配 → 白名单填 `git` 会放行
    # `git rm -rf .` / `git push --force` 等全部子命令（设置页 placeholder
    # 「如 git、npm run」正引导用户如此配置），等于引导用户制造漏洞。
    # 现改为：命中白名单后仍检查 deep 判定的 **block 与 confirm 两层** ——
    #   · block → DENY（不变）
    #   · confirm → CONFIRM（新：不再被白名单无条件放行）
    #   · safe → ALLOW（白名单意图生效）
    # 效果：填 `git` 可省掉 `git status`/`git commit` 的审批，但 `git rm -rf .`
    # 仍要确认；若用户确实想全放行 git，需显式在 confirm 名单之外承担风险。
    for prefix in cfg.get("command.allow_prefixes") or []:
        if _prefix_match(stripped, str(prefix)):
            deep = classify_command_deep(stripped)
            if deep == "block":
                return DENY
            if deep == "confirm":
                return CONFIRM
            return ALLOW

    verdict = classify_command_deep(stripped)
    if verdict == "block":
        return DENY
    if verdict == "confirm":
        return CONFIRM
    return ALLOW


def _sys_tool_bypassed(command: str, cfg: SandboxConfig) -> bool:
    """命令是否落在系统级工具豁免内（开关开 + 首 token 属 SYS_LEVEL_TOOLS）

    独立成函数供 `sandbox_check_tool` 的删除升档判定复用：豁免语义是
    「该命令族由系统自身管理，沙箱不再二次升级判定」，升档必须让位于它。
    """
    if not cfg.get("sys_tools_bypass") or not command:
        return False
    return _first_token(command.strip()) in SYS_LEVEL_TOOLS


# ============================================================
# 网络外传（弱防护）
# ============================================================
# L1 是特征检测不是 egress 隔离，目标是拦误用（如 curl -d @secret.txt），
# 不追求绕过完备性；宁多问一次。
_NET_TOOL_TOKENS = (
    "curl",
    "wget",
    "invoke-webrequest",
    "invoke-restmethod",
    "iwr",
    "irm",
    "nc",
    "ncat",
    "netcat",
    "ftp",
    "scp",
    "bitsadmin",
    "certutil",
)
_NET_UPLOAD_FLAGS = (
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "-f",
    "--form",
    "--upload-file",
    "-t",
    "--post-file",
    "--body",
)
# 网络能力的判据（2026-10-25 重写）
# ⚠ 旧实现用 `token in command.lower()` 判断「是不是网络命令」，导致
#   `nc` 命中 `encode`/`function`、`irm` 命中任意含该子串的词；且「带 URL 就
#   confirm」不看命令本身是否有网络能力 —— `git commit -m 'fix https://x'`
#   与 `echo 'see https://docs.python.org'` 一律弹审批，误报率不可接受。
# 现改为三条判据（任一成立才算网络命令）：
#   ① 段首 token（按 | ; && 切段）恰为已知网络工具
#   ② git 的远程子命令（clone/fetch/pull/push/ls-remote...）
#   ③ 命令文本出现网络 API 调用（requests./urllib/httpx/Invoke-WebRequest...）
# 非网络命令里的 URL 只当普通文本，不再触发审批。
_GIT_NET_SUBCOMMANDS = frozenset(
    {"clone", "fetch", "pull", "push", "ls-remote", "remote", "submodule", "archive"}
)
_NET_API_PATTERN = re.compile(
    r"(?i)(?:"
    r"\brequests\.(?:get|post|put|patch|delete|request|Session)\b"
    r"|\burllib\b|\bhttpx\b|\baiohttp\b|\bhttp\.client\b"
    r"|invoke-webrequest|invoke-restmethod|\biwr\b|\birm\b"
    r"|downloadstring|uploadstring|downloadfile|webclient|httpclient"
    r")"
)
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
# 裸域名（无 scheme）：仅在网络工具的**位置参数**上匹配，避免全局正则误报
# （`curl evil.com/x` 会被 curl 自动补 http://，旧实现完全绕过域名黑名单）
_BARE_DOMAIN_RE = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:[/?#].*)?$")
# 位置参数取值的候选前缀（这些 flag 后面跟的值不是"目标"）
_VALUE_FLAGS = frozenset(
    {
        "-o",
        "--output",
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "-H",
        "--header",
        "-u",
        "--user",
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-x",
        "--proxy",
        "-F",
        "--form",
        "--upload-file",
        "-T",
        "--body",
        "--max-time",
        "-m",
        "--connect-timeout",
    }
)


def _extract_bare_domains(command: str) -> list:
    """从网络命令的位置参数中抽取裸域名（保守：只认"像域名"的 token）

    判据（全部满足才算）：
    - 不是 flag（不以 - 开头）
    - 不紧跟在"取值型 flag"之后（如 `-o out.txt`、`-H X: Y`）
    - 形如 `host.tld[...]`（至少一个点 + TLD 字母后缀）
    - 不含 Windows 盘符/路径分隔特征（`C:\\`、`\\server\\share`）

    只用于域名黑名单比对，不单独触发"带 URL 就审批"（避免把普通参数误判为外传）。
    """
    if not command:
        return []
    tokens = _split_tokens(command)
    domains = []
    expect_value = False
    for raw in tokens:
        token = _strip_quotes(raw)
        if not token:
            continue
        if expect_value:
            expect_value = False
            continue
        if token.startswith("-"):
            if token in _VALUE_FLAGS:
                expect_value = True
            continue
        # 排除 Windows 盘符与 UNC 路径（C:\x、\\srv\share）
        if re.match(r"^[a-zA-Z]:[\\/]", token) or token.startswith("\\\\"):
            continue
        # 已有 scheme 的交给 _URL_PATTERN，这里只看裸形态
        if "://" in token:
            continue
        # scp/rsync 形态 user@host:/path：域名在 @ 与 : 之间
        # （旧实现整 token 不匹配 _BARE_DOMAIN_RE，导致黑名单对 scp 完全失效）
        if "@" in token:
            host = token.split("@", 1)[1].split(":", 1)[0]
            if _BARE_DOMAIN_RE.match(host):
                domains.append(host)
                continue
        if _BARE_DOMAIN_RE.match(token):
            domains.append(token)
    return domains


def _is_net_capable(command: str) -> bool:
    """命令是否具备网络能力（决定「带 URL 是否过审批」与域名黑名单是否生效）

    段首 token 精确匹配（不做子串包含，避免 `nc` 命中 `encode`），
    外加 git 远程子命令与网络 API 调用特征。
    """
    if not command:
        return False
    for seg in _split_segments(command):
        token = _first_token(seg)
        if token in _NET_TOOL_TOKENS:
            return True
        if token == "git":
            parts = seg.strip().split()
            if len(parts) > 1 and parts[1].lower() in _GIT_NET_SUBCOMMANDS:
                return True
    return bool(_NET_API_PATTERN.search(command))


def check_network(command: str, cfg: Optional[SandboxConfig] = None) -> Optional[Verdict]:
    """网络外传判定：命中返回 CONFIRM，无风险返回 None"""
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled") or not cfg.get("network.enabled"):
        return None
    if not command:
        return None

    # 非网络命令：URL 只是文本（如 commit message 里的链接），不做任何判定
    if not _is_net_capable(command):
        return None

    lowered = " " + command.lower()
    urls = _URL_PATTERN.findall(command)
    bare_domains = _extract_bare_domains(command)

    # 域名黑名单（后缀匹配）：带 scheme 的 URL + 裸域名 + scp 形态统一比对
    hosts = [re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower() for url in urls]
    hosts += [d.split("/")[0].split(":")[0].lower() for d in bare_domains]
    for host in hosts:
        for domain in cfg.get("network.blacklist_domains") or []:
            d = str(domain).lower().lstrip(".")
            if d and (host == d or host.endswith("." + d)):
                return CONFIRM

    # 上传/POST 体形态
    if any(f" {flag}" in lowered for flag in _NET_UPLOAD_FLAGS):
        return CONFIRM
    # 带 URL 的网络命令一律过一道审批（弱防护定位）
    if urls:
        return CONFIRM
    return None


# ============================================================
# 命令写目标提取（写边界收缩：重定向 / tee / mkdir / touch）
# ============================================================
# 背景：命令分支原只做「黑名单读取检查 + workdir 边界不判」，导致 bash 类工具
# 的写入（`echo x > D:/out.txt` / `mkdir D:/new` / `tee` / `touch`）零拦截 ——
# workdir 写边界只对 write/edit/multi_edit 三个文件工具生效。AI 可经 bash 自由
# 写工作目录外的任意位置，绕开用户对「写只在工作目录内」的预期。
#
# 设计（写窄读宽在命令层的落地）：
# - 只提取**明确写语义**的形态：重定向符（> / >> / N> / &>）、`tee`、
#   `mkdir`/`md`、`touch`。不做泛化「所有位置参数都当写」——那会把 `git clone url dest`
#   / `npm install` 等全打成 confirm，误报率不可接受。
# - 提取结果只走 `check_path(mode="write")`：黑名单（读写都拦）+ 白名单 + workdir
#   边界。命中返回 CONFIRM（审批），与文件工具口径一致。
# - 判定失败/提取不出目标 → 不改变原判定（fail-open，与全模块一致）。
# - 幂等性：目标相对路径按 workdir 解析（不是进程 cwd），与白名单条目同款。

# 写入类命令（首 token）：值 = 该命令是否取「全部非 flag 位置参数」为写目标
_WRITE_CMD_TOKENS = frozenset({"mkdir", "md", "touch"})
# 目标取单个（后续参数）的写入命令：tee（可接多个文件，取全部非 flag 段）
_TEE_TOKEN = "tee"
# 重定向目标终止字符（`> f` 的 f 到空白/控制符为止）
_TARGET_STOP = frozenset(" \t<>|&;")


def _split_segments(command: str) -> list:
    """按顶层分隔符（| ; && || & 换行）切段，引号内不切

    用途：管道/命令链里**段首才是真正的命令**（`a | tee f` 的写命令是 tee）。
    `2>&1` 的 `&` 后跟数字 → fd 复制，不视为分隔（留在段内）。
    """
    segments: list = []
    buf: list = []
    quote = ""
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "&":
            nxt = command[i + 1] if i + 1 < n else ""
            if nxt == "&":
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            if nxt.isdigit() or nxt == "-":
                buf.append(ch)  # 2>&1 / >&- 的 fd 复制，留在段内
                i += 1
                continue
            segments.append("".join(buf))  # 命令分隔（含 &>file 的 &）
            buf = []
            i += 1
            continue
        if ch == "|":
            segments.append("".join(buf))
            buf = []
            i += 2 if (i + 1 < n and command[i + 1] == "|") else 1
            continue
        if ch == ";" or ch in "\r\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s for s in (seg.strip() for seg in segments) if s]


def _scan_redirects(segment: str) -> list:
    """段内提取重定向写目标（`> f` / `>> f` / `2> f` / `&> f` / 无空格 `x>f`）

    引号感知（引号内 `>` 不参与）；排除 fd 复制（`2>&1` / `>&2`，目标以 `&` 开头）。
    不用正则：需处理"无空格重定向 + 引号目标"组合，正则易漏易误。
    """
    targets: list = []
    quote = ""
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == ">":
            j = i + 1
            if j < n and segment[j] == ">":
                j += 1
            while j < n and segment[j] in " \t":
                j += 1
            if j >= n:
                break
            if segment[j] in "\"'":
                q = segment[j]
                k = segment.find(q, j + 1)
                if k < 0:
                    break
                raw = segment[j + 1 : k]
                i = k + 1
            else:
                k = j
                while k < n and segment[k] not in _TARGET_STOP:
                    k += 1
                raw = segment[j:k]
                i = k
            raw = raw.strip()
            if raw and not raw.startswith("&"):
                targets.append(raw)
            continue
        i += 1
    return targets


def _positional_targets(segment: str, head: str) -> list:
    """段内位置参数提取（mkdir/md/touch/tee 的写目标）

    `-p` / `-c` 等 flag 与 `--` 分隔符跳过；引号包裹整段保留为一个目标。
    """
    if head not in _WRITE_CMD_TOKENS and head != _TEE_TOKEN:
        return []
    targets = []
    for tok in _split_tokens(segment)[1:]:
        raw = _strip_quotes(tok)
        if not raw or raw == "--" or raw.startswith("-"):
            continue
        targets.append(raw)
    return targets


def _segment_write_targets(segment: str) -> list:
    """单段的写目标 = 重定向目标 + 段首写命令的位置参数

    段首判定用 `_first_token`（去路径前缀/.exe），`cmd /c mkdir X` 这类壳形态
    在 write_targets 里经解壳递归处理，此处不重复。
    """
    targets = _scan_redirects(segment)
    head = _first_token(segment)
    targets.extend(_positional_targets(segment, head))
    return targets


def _shell_payload_write_targets(command: str) -> list:
    """壳命令载荷内的写目标（cmd /c mkdir X、bash -c 'echo x > f' 等）

    复用 command_safety 的壳表（SHELL_WRAPPER_FLAGS）。
    ⚠ 不用 shlex.split 定位载荷：shlex 会吞 Windows 反斜杠（C:\\tmp → C:tmp），
    载荷用引号剥离 + 首 token 直接切的方式还原，保路径原样。
    """
    try:
        from app.tools.command_safety import SHELL_WRAPPER_FLAGS
    except Exception:  # noqa: BLE001 - 依赖不可用不改判定
        return []
    stripped = command.strip()
    if not stripped:
        return []
    head = _first_token(stripped)
    flags = SHELL_WRAPPER_FLAGS.get(head)
    if flags is None:
        return []
    # 跳过首 token（命令名）本身
    rest = stripped.split(None, 1)
    payload = rest[1] if len(rest) > 1 else ""
    if flags and payload:
        # 有定位 flag：取 flag 之后的整段（flag 可能带引号）
        lowered = payload.lower().lstrip()
        for flag in flags:
            if lowered.startswith(flag) and (len(lowered) == len(flag) or lowered[len(flag)] in " \t\"'"):
                payload = payload.lstrip()[len(flag) :]
                break
    payload = payload.strip().strip("\"'").strip()
    if not payload:
        return []
    return [_t for seg in _split_segments(payload) for _t in _segment_write_targets(seg)]


def write_targets(command: str) -> list:
    """命令的写入目标路径列表（重定向 + tee/mkdir/touch，含壳命令载荷）

    供 sandbox_check_tool 的写边界判定使用；提取失败返回 []（不改判定）。
    去重保序。
    """
    if not command or not command.strip():
        return []
    seen: set = set()
    result: list = []
    for seg in _split_segments(command):
        candidates = _segment_write_targets(seg)
        if not candidates:
            candidates = _shell_payload_write_targets(seg)
        for raw in candidates:
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(raw)
    return result


# ============================================================
# 工具路由（worker 层唯一入口）
# ============================================================
# 分组名与插件侧 GROUP_WRITE/GROUP_READ（plugins/system-tools/tools/file_tools.py:109）
# 对齐；"文件写入" 同时是 FileRecorder 追踪组的同源字符串。
# 不写死工具名清单——插件增删热生效，写死必漏。
GROUP_WRITE_NAME = "文件写入"
GROUP_READ_NAME = "文件读取"


def _tools_in(group_name: str) -> frozenset:
    """registry 分组成员查询；registry 不可用时返回空集（fail-open）"""
    try:
        from app.tools.registry import ToolRegistry

        return ToolRegistry.get_instance().tools_in_group(group_name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Sandbox] registry 分组查询失败({group_name}): {e}")
        return frozenset()


def _current_workdir() -> Path:
    """当前有效工作目录：BackgroundTaskManager 单例通道，回退 cwd"""
    try:
        from app.tools.bg_manager import BackgroundTaskManager

        inst = BackgroundTaskManager._instance
        if inst is not None:
            wd = inst._effective_workdir()
            if wd is not None:
                return Path(wd)
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def sandbox_check_tool(
    tool_name: str,
    arguments: dict,
    cfg: Optional[SandboxConfig] = None,
    workdir: Optional[Path] = None,
) -> Verdict:
    """worker 层唯一入口：工具名 + 参数 → 三分判定（deny/confirm/allow）

    路由规则（元数据驱动，不写死工具名）：
    - 参数带 command 键 → 命令工具 → check_command + check_network
    - tool_name ∈ registry "文件写入" 分组 → check_path(write)
    - tool_name ∈ registry "文件读取" 分组 → check_path(read)
    - 其余 → allow

    workdir 显式传入时以此为准；未传时回退 `_current_workdir()`。
    ⚠ 回退来源是 BackgroundTaskManager 单例里的全局 workdir（由每次工具调用刷新），
    多窗口 / 主对话与子智能体并发时会串味（拿到"最后一次调用者"的 workdir）。
    需要确定性基准的调用方应显式传 workdir。
    """
    cfg = cfg or SandboxConfig.get_instance()
    if not cfg.get("sandbox_enabled"):
        return ALLOW
    args = dict(arguments or {})

    command = args.get("command")
    if isinstance(command, str) and command.strip():
        verdict = check_command(command, cfg)
        # ── 删除识别兜底升档：deep 分类器不认识的删除变体（如 .NET
        # [IO.Directory]::Delete）在保护开启时同样强制审批；deny 优先，
        # 豁免由下方豁免块统一判定 ──
        # ⚠ 系统级工具豁免（sys_tools_bypass）命中的命令不得在此升档：
        # `check_command` 已按豁免放行，若此处无条件升回 CONFIRM，豁免对
        # `wsl rm -rf /mnt/c/x` 这类「豁免工具 + 删除参数」组合完全失效，
        # 与开关文案「这些命令绕过沙箱审批」矛盾（2026-10-25 实测复现）。
        if (
            verdict != DENY
            and cfg.get("delete_protection")
            and is_delete_command(command)
            and not _sys_tool_bypassed(command, cfg)
        ):
            verdict = CONFIRM
        # ── 盘符根/系统目录硬拒（2026-10-25 事故）──
        # 删除目标为盘符根（C:\）或盘根一级系统目录（C:\Windows 等）时无合法
        # 删除场景，垃圾桶也不得收（跨盘 move = 先整盘拷贝再整盘删除）；
        # deny 优先于后续豁免与黑名单判定。
        if (
            cfg.get("delete_protection")
            and verdict == CONFIRM
            and is_delete_command(command)
            and any(_is_protected_root(t) for t in delete_targets(command, workdir)["existing"])
        ):
            verdict = DENY
        # ── 删除保护门控（EU-G23，正解 A）──
        # `delete_protection` 关闭时，**仅取消「因删除命令而 confirm」这一层**。
        # 依据：删除类命令的 confirm 来自 CONFIRM_COMMANDS 命令名单（command_safety），
        # 与路径边界无关 —— 所以门控必须落在 command 分支，而非 check_path。
        # ⚠ 以下三层**不受影响**，必须继续拦截：
        #   ① deny（cacls 等 block 命令）② check_network（外传）③ G31 黑名单
        #   ④ 用户 confirm_prefixes 显式加严（如手动加了 `rm`）—— 用户显式意图优先
        #   ⑤ **其他判据给出的 confirm**（如 command_safety 的 git clean -fdx /
        #      git push --force）—— 门控只针对「删除动词」这一层，不得把别人
        #      独立判定出的 confirm 一并降级（2026-10-25 实测：宽松判据接入后
        #      `git clean -fdx` 被误降为 allow，破坏性操作在开关关闭时静默执行）。
        # 降级为 ALLOW 后仍会走下方 check_network，故外传层不会因此漏判。
        if (
            verdict == CONFIRM
            and not cfg.get("delete_protection")
            and bool(_find_delete_command_spans(command))
            and not _matches_any_prefix(command, cfg.get("command.confirm_prefixes"))
        ):
            verdict = ALLOW
        if verdict == CONFIRM and is_delete_command(command) and delete_exempt(command, cfg, workdir):
            return ALLOW
        # ── 黑名单定向检查（命令类工具的路径边界）──
        # 背景：command 分支原不查路径，导致 path.blacklist 对命令类工具完全失效
        # （AI 用 `type D:/secrets/key.pem` 可静默读取敏感目录）。而 CONFIRM_COMMANDS
        # 的条目全是写类/系统类命令，一个读取类都没有 → 黑名单形同虚设。
        # 设计：只过黑名单，**不判 workdir 边界** —— check_path 的 read 模式天然
        # 只检查黑名单（其余无条件 allow），故统一传 "read" 可避开 mode 推断
        # （rm=write / type=read / git、npm、python -m 无法静态判定）。
        # read 模式不判 workdir 边界 → 零误报（plan 用 16 个真实 AI 命令实测 16/16）。
        # 顺序：放在 delete_exempt 之后（用户显式豁免优先于黑名单）。
        # 幂等：黑名单为空（默认 []）时整块短路，零性能开销、零行为变化。
        if cfg.get("path.blacklist"):
            wd = workdir if workdir is not None else _current_workdir()
            for token in _split_tokens(command):
                raw = _strip_quotes(token)
                if not raw:
                    continue
                # 引号内可能是完整子命令（powershell -c "Get-Content X"），
                # 需二次拆分才拿得到其中的路径 token（与 delete_targets 同款处理）
                candidates = _split_tokens(raw) if " " in raw.strip() else [raw]
                for item in candidates:
                    item = _strip_quotes(item)
                    if not item or item.startswith(("-", "/")):
                        continue
                    if check_path(wd, item, "read", cfg) == CONFIRM:
                        return CONFIRM
        # ── 写边界（命令层）：重定向 / tee / mkdir / touch 的目标过 check_path(write) ──
        # 背景：本分支原只做黑名单读取检查，workdir 写边界对命令类写入零约束，
        # `echo x > D:/out.txt` / `mkdir D:/new` / `tee` / `touch` 全部静默放行。
        # 设计：提取明确写语义的目标后过 `check_path(mode="write")`（黑名单 + 白名单
        # + workdir 边界），命中 CONFIRM。与文件工具口径一致：越界写 = 弹审批。
        # ⚠ 两个坑（已踩）：
        #   ① verdict == DENY 时必须跳过本块 —— 否则 `cacls ... > D:/out.txt` 会被
        #      本块降级为 CONFIRM（block 命令的硬拒不能因附带重定向而放宽）。
        #   ② 相对目标必须按 **workdir** 解析（bash 进程 cwd = workdir，
        #      terminal_tools chdir 到 workdir；check_path 内部 _norm 按进程 cwd
        #      解析相对路径，直接传会全误报）。
        # 幂等：非写形态命令 write_targets 返回空 → 零行为变化。
        if verdict != DENY:
            _wt = write_targets(command)
            if _wt:
                wd_w = workdir if workdir is not None else _current_workdir()
                for item in _wt:
                    if check_path(wd_w, str(_resolve_cmd_path(item, wd_w)), "write", cfg) == CONFIRM:
                        return CONFIRM
        if verdict == ALLOW:
            net = check_network(command, cfg)
            return net if net else ALLOW
        return verdict

    if tool_name in _tools_in(GROUP_WRITE_NAME):
        mode = "write"
    elif tool_name in _tools_in(GROUP_READ_NAME):
        mode = "read"
    else:
        return ALLOW

    # 路径字段提取：单值（path/file_path）优先，其次数组形态（files/paths）。
    # 数组逐个元素判定，任一命中 CONFIRM 则整体 CONFIRM。
    # 说明：`multi_edit` 的 schema 已有 `path` 字段故不属漏判；`stage_files`
    # 是 danger="safe" + GROUP_READ（走 read 分支恒定 ALLOW），**本项对
    # stage_files 不产生实际拦截效果** —— 它是防御未来可能出现的数组写工具。
    raw_paths = args.get("path") or args.get("file_path")
    if raw_paths is None:
        for key in ("files", "paths"):
            vals = args.get(key)
            if isinstance(vals, list) and vals:
                raw_paths = vals
                break
    if not raw_paths:
        return ALLOW
    items = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    wd = workdir if workdir is not None else _current_workdir()
    verdicts = [check_path(wd, str(p), mode, cfg) for p in items if p]
    if any(v == CONFIRM for v in verdicts):
        return CONFIRM
    return ALLOW


# ============================================================
# 删除保护（审批放行后、执行前快照）
# ============================================================
DELETE_COMMANDS = frozenset({"rm", "del", "erase", "rd", "rmdir", "deltree", "unlink", "remove-item", "ri"})

# .NET / PowerShell 侧删除 API 调用形态（词表外变体，2026-10-18 实测漏拦）：
# [System.IO.Directory]::Delete / [IO.File]::Delete 等；方法名限定 Delete，
# 避免误报 CreateDirectory 等同名前缀方法
_DOTNET_DELETE_PATTERN = re.compile(
    r"(?i)\[\s*(?:system\.)?io\.(?:directory|file)\s*\]\s*::\s*delete\b"
)

# rm/del 等常见 flag（快照时剔除）
_DELETE_FLAGS = {"-r", "-rf", "-f", "-fr", "-d", "-rd", "-recursive", "/s", "/q", "/f"}

# shell 连接符：token 命中即视为「上一条命令结束」，删除命令的参数到此为止。
# 2026-10-24 事故：原实现不切边界，把 `cd <工作目录> && find ... -exec rm ...`
# 里的工作目录当成删除目标整个 move 进垃圾箱。
_SHELL_SEPARATORS = frozenset({"&&", ";", "|", "||", "&", "|&", ";;"})

# ── 命令位判定（2026-10-25 误搬事故）──
# 背景：删除目标提取原先「逐 token 全扫」，只要任一 token 是 rm/del/ri 就当成
# 删除命令，其后所有 token 全当删除目标。实测误搬（目标真实存在即被 move）：
#   git rm -r --cached app          → 只脱离索引，整个 app 被搬进垃圾桶
#   git commit -m "fix: rm cache"   → commit message 里的 rm，app 被搬
#   grep -rn "rm" src/ tests/       → 引号里的 rm，tests/ 被搬
#   ls rm build / pip install del   → 参数位的动词，build 被搬
# 结论：**只有命令位（段首 / 包装器载荷 / find -exec 之后）的删除动词才算数**，
# 引号内容只有作为壳命令载荷时才当代码看。
#
# 命令位包装器：段首命中时真实命令在其后（跳过 flag 与取值）
_COMMAND_PREFIX_WRAPPERS = frozenset(
    {
        "sudo", "doas", "env", "command", "builtin", "exec", "nohup",
        "xargs", "busybox", "wsl", "start", "runas",
        "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "dash", "fish",
        # 字符串求值器：载荷本身就是命令（eval "rm -rf x" / iex "Remove-Item x"）
        "eval", "iex", "invoke-expression",
    }
)
# for/while/until 循环头：其后的 do 之后是命令位
_LOOP_KEYWORDS = frozenset({"for", "while", "until"})
_LOOP_BODY_KEYWORDS = frozenset({"do", "then"})
# 包装器的「取值型」flag（其后紧跟的值不是命令）。
# ⚠ 不得加入 -c/-e/-o/-p 等载荷 flag：那些 flag 之后紧邻的就是载荷本身。
_WRAPPER_VALUE_FLAGS = frozenset(
    {"-u", "--user", "-g", "--group", "-n", "--max-args", "-i", "--replace", "-p", "--max-procs", "-s", "--signal"}
)
# find 的内嵌命令动作 flag：其后第一个非 flag token 是命令
_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# find -exec 的命令终止符（既不是参数也不该被搬）
_FIND_EXEC_TERMINATORS = frozenset({"{}", "+", ";", "\\;"})
# 不落盘的删除 flag：命令只改索引/只预演，磁盘文件不动，垃圾桶绝不该动它
_NO_DISK_DELETE_FLAGS = frozenset({"--cached", "--dry-run"})
# 解壳递归深度上限（sudo bash -c ... 嵌套）
_MAX_UNWRAP_DEPTH = 4


# 命令分词：shlex.split(posix=True) 会吞掉 Windows 反斜杠
# （C:\\tmp\\a.txt → C:tmpa.txt）导致快照路径失真，故用正则分词：
# 成对引号整段保留，其余按空白切；去壳引号在取值时处理。
_TOKEN_PATTERN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _split_tokens(command: str) -> list:
    """命令分词（保留 Windows 路径反斜杠，成对引号作单 token）"""
    if not command:
        return []
    return _TOKEN_PATTERN.findall(command)


# shell 连接符（按长度降序，保证 && 先于 & 匹配）
_EXPLODE_SEPS = (";;", "&&", "||", "|&", ";", "|", "&")


def _explode_separators(tokens: list) -> list:
    """把紧贴路径的连接符拆成独立 token（``a.txt&&rm`` → ``a.txt`` / ``&&`` / ``rm``）

    分词器按空白切，``rm a.txt&&rm b.txt`` 里 ``a.txt&&rm`` 是单个 token，
    导致两个后果（2026-10-25 实测）：
    - 前一个 ``rm`` 的参数解析成 ``a.txt&&rm`` → ``a.txt`` 既不进 existing 也不进
      missing，**目标静默丢失**；
    - 后一个 ``rm`` 根本不在命令位，``b.txt`` 也丢。

    引号内整段保留（引号里的 ``;``/``&&`` 是数据不是分隔符）。
    ``&`` 后紧跟数字 / ``-`` / ``>`` 时是 fd 复制（``2>&1`` / ``>&-`` / ``&>f``），
    不拆。
    """
    out: list = []
    for tok in tokens:
        stripped = tok.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            out.append(tok)
            continue
        buf = ""
        i = 0
        n = len(stripped)
        while i < n:
            matched = ""
            for sep in _EXPLODE_SEPS:
                if stripped.startswith(sep, i):
                    matched = sep
                    break
            if matched:
                nxt = stripped[i + len(matched)] if i + len(matched) < n else ""
                # & 的 fd 复制/重定向形态不拆
                if matched == "&" and (nxt.isdigit() or nxt in ("-", ">")):
                    buf += matched
                    i += 1
                    continue
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(matched)
                i += len(matched)
                continue
            buf += stripped[i]
            i += 1
        if buf:
            out.append(buf)
    return out


def _strip_quotes(token: str) -> str:
    stripped = token.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    return stripped


def _extract_quoted_paths(token: str) -> list:
    """从 $var='path' / --arg="path" 形态 token 提取引号内路径候选

    PowerShell 变量赋值后再传入删除 API 的形态（如 $p='D:/x'; ...::Delete($p)）
    无法靠命令词定位目标，靠引号内容补候选；误报方向是多拍快照，无害。
    """
    if "'" not in token and '"' not in token:
        return []
    return [p for p in re.findall(r"[\"']([^\"']{2,})[\"']", token) if not p.startswith("-")]


def _resolve_cmd_path(item: str, workdir: Optional[Path] = None) -> Path:
    """命令内路径解析：expandvars/expanduser 后，相对路径按 workdir 补齐

    ⚠ 相对路径的基准是 **workdir**（bash 进程 cwd = workdir，terminal_tools
    chdir 到该目录），不是进程 cwd。实测踩坑：`rm -rf subdir`（workdir 下确有
    subdir）在快照时按进程 cwd 解析 → 目标不存在 → **静默漏拍快照**，
    删除保护失效而不报错。delete_targets / stash_delete_targets_to_trash /
    delete_exempt 及写目标判定全部走本函数，不得各自展开。
    """
    p = Path(os.path.expandvars(item)).expanduser()
    if not p.is_absolute():
        base = workdir if workdir is not None else _current_workdir()
        p = Path(base) / p
    return p


def _head_of(token: str) -> str:
    """token 归一化成命令名：去引号、去路径前缀、去 .exe、小写"""
    name = _strip_quotes(token).lower().replace("\\", "/").split("/")[-1]
    return name[:-4] if name.endswith(".exe") else name


def _skip_wrapper_flags(tokens: list, start: int) -> Optional[int]:
    """从 start 起跳过 flag（含取值）与环境变量赋值，返回其后第一个 token 索引

    只跳「包装器自身的选项」，跳到第一个非 flag token 即停 —— 那个 token
    才是真实命令（``sudo -u root rm -rf x`` 里的 ``rm``）。

    flag 判定含 Windows 形态 ``/c`` / ``/k``（cmd 用斜杠），但 ``/tmp/x`` 这类
    形似路径的 token 不算 flag（``_looks_like_path`` 判据）。
    """
    n = len(tokens)
    i = start
    while i < n:
        inner = _strip_quotes(tokens[i])
        low = inner.lower()
        if not low:
            i += 1
            continue
        if low in _SHELL_SEPARATORS:
            return None
        # env 形态的前置赋值（env FOO=bar rm x）：不是命令
        if "=" in low and not low.startswith(("-", "/")) and "\\" not in low and "/" not in low:
            i += 1
            continue
        is_flag = low.startswith("-") or (low.startswith("/") and not _looks_like_path(inner))
        if is_flag:
            i += 2 if low in _WRAPPER_VALUE_FLAGS else 1
            continue
        return i
    return None


# git 全局选项（其后跟一个值），用于定位子命令位置
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_GIT_HEADS = frozenset({"git", "git.exe"})


def _command_positions(tokens: list) -> set:
    """返回段内处于「命令位」的 token 索引集合

    命令位的定义：段首、shell 连接符之后、命令位包装器之后（sudo/xargs/
    cmd -c/powershell -c/... 的第一个非 flag token）、find 的 -exec 之后、
    ``git`` 的子命令位（``git rm`` 的 ``rm``）。

    这条判定是 2026-10-25 误搬事故的修法核心：**参数位出现的 rm/del 不算
    删除命令**。若不做区分，下面这些只读命令都会把后面的路径搬进垃圾桶：
    ``grep -rn "rm" src/ tests/``、``ls rm build``、``uv pip install del build``、
    ``git diff rm app``、``git commit -m "fix: rm cache"``。
    """
    positions: set = set()
    expect = True  # 下一个非 flag token 应处于命令位
    i = 0
    n = len(tokens)
    while i < n:
        raw = tokens[i]
        inner = _strip_quotes(raw)
        low = inner.lower()
        if not low:
            i += 1
            continue
        if low in _SHELL_SEPARATORS:
            expect = True
            i += 1
            continue
        # find -exec/-execdir/-ok：其后第一个非 flag、非终止符 token 是命令
        if low in _FIND_EXEC_FLAGS:
            j = i + 1
            while j < n:
                cand = _strip_separators(_strip_quotes(tokens[j]))
                if cand and cand.lower() not in _FIND_EXEC_TERMINATORS and not cand.startswith("-"):
                    i = j
                    expect = True
                    break
                j += 1
            else:
                i += 1
                expect = False
            continue
        if expect:
            head = _head_of(low)
            if head in _LOOP_KEYWORDS or low in _LOOP_BODY_KEYWORDS:
                # for f in *.txt; do rm "$f"; done —— do/then 后是命令位
                i += 1
                expect = True
                continue
            if head in _COMMAND_PREFIX_WRAPPERS:
                j = _skip_wrapper_flags(tokens, i + 1)
                if j is not None:
                    # j 本身可能是包装器（sudo bash -c ...），回到循环再判一次
                    i = j
                    expect = True
                    continue
                i += 1
                expect = False
                continue
            if head in _GIT_HEADS:
                # git 的子命令位也是命令位（`git rm` 的 rm 真删工作区文件）；
                # 跳过全局选项（-C <path> / -c k=v / --git-dir=...）后定位子命令
                j = i + 1
                while j < n:
                    cand = _strip_quotes(tokens[j])
                    if cand in _GIT_GLOBAL_VALUE_FLAGS:
                        j += 2
                        continue
                    if cand.startswith("-"):
                        j += 1
                        continue
                    break
                if j < n and _strip_quotes(tokens[j]).lower() not in _SHELL_SEPARATORS:
                    positions.add(i)  # git 自身也是命令位
                    positions.add(j)
                    i = j + 1
                    expect = False
                    continue
                i += 1
                expect = False
                continue
            positions.add(i)
            expect = False
        # 紧贴路径的连接符（`a.txt;` / `b.txt&&`）→ 本 token 是最后一段参数
        if _has_trailing_separator(raw):
            expect = True
        i += 1
    return positions


def _payload_inner_has_delete(token: str) -> bool:
    """引号包裹的壳载荷内部是否存在命令位删除动词

    仅用于「已经是命令位的壳载荷 token」这一种形态 —— 引号出现在普通参数位
    （``grep "rm -rf" app/``）时整段根本不是命令位，不会走到这里。
    """
    inner = _strip_quotes(token)
    inner_tokens = _split_tokens(inner)
    if len(inner_tokens) < 2:
        return False
    for idx in _command_positions(inner_tokens):
        if _head_of(inner_tokens[idx]) in DELETE_COMMANDS:
            return True
    return False


def _find_delete_command_spans(command: str, _depth: int = 0) -> list:
    """定位命令中每个**命令位**删除动词，返回 [(动词 token 索引, 参数 token 索引列表)]

    ``arg_idxs`` 为 ``-1`` 表示「参数在动词 token 自身内部」——壳命令包裹形态
    （``powershell -c "Remove-Item -Recurse x"``）里整条子命令是一个 token，
    参数不在后续 token 中，由 ``_delete_arg_tokens`` 递归拆解。

    参数边界按 shell 连接符切分：``&&``、``;``、``|``、``||``、``&`` 之后属于
    下一条命令，不属于本删除命令；遇到即停。这条边界是 2026-10-24 事故的直接教训：
    原实现把命令里**全部** token 都当删除目标，导致
    ``cd D:/work/proj && find . -exec rm ...`` 里的 ``D:/work/proj`` 被当作删除目标
    整个 move 进垃圾箱（工作目录消失）。

    ⚠ 分词器不切分紧贴路径的连接符（``rm a.txt; cd x`` 里 ``a.txt;`` 是单个
    token），故解析前先跑 `_explode_separators` 把连接符拆成独立 token。
    不拆的后果是双重的（2026-10-25 实测）：``rm a.txt&&rm b.txt`` 里前一个
    参数解析成 ``a.txt&&rm``（目标丢失）、后一个 ``rm`` 不在命令位（``b.txt``
    也丢），两个待搬目标全部漏掉。

    ⚠ 只认命令位（见 ``_command_positions``）。2026-10-25 实测的误搬集合全部
    来自「参数位也认」：``git rm --cached app``（只脱离索引）、
    ``git commit -m "fix: rm cache"``（提交信息）、``grep -rn "rm" src/ tests/``
    （搜索词）、``ls rm build``（参数）。删除动词出现在参数位时不是删除命令。

    ⚠ ``--cached`` / ``--dry-run`` 的 span 整条丢弃：命令只改索引或只预演，
    磁盘文件不动，垃圾桶绝不能搬。
    """
    tokens = _explode_separators(_split_tokens(command or ""))
    spans: list = []
    for idx in sorted(_command_positions(tokens)):
        head = _head_of(tokens[idx])
        if head in DELETE_COMMANDS:
            arg_idxs: list = []
            for j in range(idx + 1, len(tokens)):
                raw_next = tokens[j]
                if _strip_quotes(raw_next) in _SHELL_SEPARATORS:
                    break
                # 紧贴路径的连接符（a.txt; / b.txt&&）→ 本 token 是最后一个参数，到此为止
                if _has_trailing_separator(raw_next):
                    arg_idxs.append(j)
                    break
                arg_idxs.append(j)
            # 不落盘的删除（git rm --cached / --dry-run）：候选保留供展示与豁免
            # 判定，但垃圾桶层（stash_delete_targets_to_trash）绝不搬它们
            spans.append((idx, arg_idxs))
            continue
        # 命令位的壳载荷（powershell -c "Remove-Item x"）：内部再按命令位判一次
        if _depth < _MAX_UNWRAP_DEPTH and _payload_inner_has_delete(tokens[idx]):
            spans.append((idx, [-1]))
    return spans


def _has_trailing_separator(token: str) -> bool:
    """token 是否以 shell 连接符结尾（如 ``a.txt;``、``b.txt&&``）"""
    for sep in (";;", "&&", "||", "|&", ";", "|", "&"):
        if token.endswith(sep):
            return True
    return False


def _strip_separators(token: str) -> str:
    """剥掉 token 首尾的 shell 连接符（保留引号内内容原样）"""
    out = token
    changed = True
    while changed:
        changed = False
        for sep in (";;", "&&", "||", "|&", ";", "|", "&"):
            if out.startswith(sep):
                out = out[len(sep) :]
                changed = True
            if out.endswith(sep):
                out = out[: -len(sep)]
                changed = True
    return out


def _dotnet_delete_segments(command: str) -> list:
    """含 .NET IO Delete 调用的语句段，以及它**之前**的变量赋值段

    目标常以 ``$p='D:/x'`` 先赋值、再传给 ``::Delete($p)``，赋值语句在 Delete
    语句之前，所以必须向前纳入 —— 但只纳入形如「变量赋值」的段（段内无空格前
    的 token 含 ``=``），否则 ``cd "D:/work" && …::Delete($p)`` 里的工作目录会
    被当目标。Delete 之后的语句一律不取：``…::Delete($p); Get-ChildItem D:/work``
    的 ``D:/work`` 与删除无关。
    """
    segments = _split_segments(command or "")
    last = -1
    for idx, seg in enumerate(segments):
        if _DOTNET_DELETE_PATTERN.search(seg):
            last = idx
    if last < 0:
        return []
    picked: list = []
    for seg in segments[: last + 1]:
        if _DOTNET_DELETE_PATTERN.search(seg):
            picked.append(seg)
            continue
        head = seg.strip().split(maxsplit=1)[0] if seg.strip() else ""
        if "=" in head and not head.startswith(("-", "/")):
            picked.append(seg)
    return picked


def _delete_arg_tokens(command: str) -> list:
    """取出所有删除命令的参数 token（不含动词本身、不含后续命令）

    壳命令包裹形态（``powershell -c "Remove-Item -Recurse x"``）里参数与动词
    同在一个 token，此处就地按空白拆开、丢掉动词本身，其余作为参数返回。

    每个 token 先剥掉首尾连接符（``a.txt;`` → ``a.txt``），否则带分号的路径
    解析出来会是个不存在的怪路径。

    ⚠ 无命令位 span 时**只在 .NET Delete 形态下退化为引号路径候选**，且限定在
    含 Delete 调用的那条语句内。旧实现在任何「找不到动词」的情况都返回全命令
    的 token，等于把无关路径也当删除目标 —— ``powershell -c "[IO.File]::Delete($p);
    Get-ChildItem D:/work"`` 会连 ``D:/work`` 一起搬。
    """
    tokens = _explode_separators(_split_tokens(command or ""))
    spans = _find_delete_command_spans(command)
    if spans:
        picked: list = []
        seen_idx: set = set()
        for verb_idx, arg_idxs in spans:
            if arg_idxs == [-1]:
                # 参数在动词 token 内部：拆出子词，剔除删除动词名
                inner = _strip_quotes(tokens[verb_idx])
                for sub in inner.split():
                    head = sub.lower().replace("\\", "/").split("/")[-1]
                    if head in DELETE_COMMANDS:
                        continue
                    picked.append(sub)
                continue
            for j in arg_idxs:
                if j not in seen_idx:
                    seen_idx.add(j)
                    picked.append(tokens[j])
        return [_strip_separators(t) for t in picked if _strip_separators(t)]
    # .NET 形态：目标藏在 $var / 括号里，只能取该语句内的引号路径
    picked = []
    for seg in _dotnet_delete_segments(command):
        for tok in _split_tokens(seg):
            picked.extend(_extract_quoted_paths(tok))
    return picked


def delete_targets(command: str, workdir: Optional[Path] = None) -> dict:
    """解析删除命令的目标路径（供审批展示与垃圾桶共用）

    只取**删除命令自身的参数**作为候选（见 ``_delete_arg_tokens``）：命令里的
    ``cd <目录>`` 前缀、管道前一端的路径、``&&`` 之后的其他命令参数都不再被当目标。

    ⚠ 前置判定用 `is_trash_delete_command`（严格）：目标解析不出来时返回空，
    垃圾桶就不会搬任何东西。这里若用宽松判据，``find -delete`` 这类无法静态
    定位目标的命令会走到「找不到目标」分支，而旧实现的兜底会把命令里所有
    存在的 token 当目标 —— 正是误搬的来源。

    Returns:
        {"existing": [Path, ...], "missing": [str, ...]}——
        existing 为真实存在的目标（去重），missing 为命令里写了但当前不存在的候选
        （两者均为**按 workdir 解析后**的绝对路径；相对目标不再按进程 cwd 失真）
    """
    result: dict = {"existing": [], "missing": []}
    # 严格判据（命令位动词）→ 走常规解析；
    # .NET 形态（目标藏在变量里）单独解析：它的目标**不供垃圾桶搬**（stash 用
    # is_trash_delete_command 拒绝），但审批展示与盘根硬拒仍需知道目标是谁。
    if not is_trash_delete_command(command) and not _DOTNET_DELETE_PATTERN.search(command):
        return result
    seen: set = set()
    for token in _delete_arg_tokens(command):
        raw = _strip_quotes(token)
        lowered = raw.lower()
        if lowered in _DELETE_FLAGS or lowered in DELETE_COMMANDS:
            continue
        if lowered.startswith(("-", "/")) and not _looks_like_path(raw):
            continue
        # 引号内多词（如 powershell -c "Remove-Item x"）逐个试；
        # 另提取 $var='path' 赋值形态的引号内路径（.NET Delete 变体目标）
        candidates = _split_tokens(raw) if " " in raw.strip() else [raw]
        candidates = candidates + _extract_quoted_paths(raw)
        # glob 展开：`rm *.pyc` 要搬的是匹配到的那几个文件，不是字面量 `*.pyc`
        expanded: list = []
        for cand in candidates:
            expanded.extend(_expand_glob_target(_strip_quotes(cand), workdir))
        for item in expanded:
            item = _strip_quotes(item)
            if not item or item.lower() in DELETE_COMMANDS or item.lower() in _DELETE_FLAGS:
                continue
            if item.startswith(("-", "/")) and not _looks_like_path(item):
                continue
            candidate = _resolve_cmd_path(item, workdir)
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                result["existing"].append(candidate)
            else:
                result["missing"].append(str(candidate))
    return result


def _looks_like_path(token: str) -> bool:
    """-/开头但形似路径的特例（/s /q 是 flag，/tmp/x 是 Unix 绝对路径）

    含路径分隔符或扩展名点号才视为路径，否则按 flag 处理。
    """
    return ("/" in token[1:] or "\\" in token or "." in token[1:]) and len(token) > 2


def delete_exempt(command: str, cfg: Optional[SandboxConfig] = None, workdir: Optional[Path] = None) -> bool:
    """删除豁免判定：命令的删除目标全部落在豁免路径内时，confirm 降级为 allow

    豁免条目支持绝对路径（workdir 外）或相对 workdir 的路径（如 tests/）。
    任一目标不在豁免内即不豁免（宁多问一次）；无目标可解析则不豁免。

    ⚠ 双分支写法与 `check_path` 白名单（G5）**逐字同构**，不要退回 `any(A or B)`：
    那会让相对条目在"目标是绝对路径且 workdir 不同"时全部失效
    （如豁免 `tests/` + `rm /abs/work/tests/x` 曾被判不豁免）。
    另：相对条目必须按 **workdir** 解析（不是进程 cwd），`%VAR%` 需先 expandvars 再判绝对。

    ⚠ 黑名单优先于豁免（2026-10-25）：目标同时落在 `path.blacklist` 内时不豁免。
    此前豁免无条件先于黑名单判定，同一路径既在黑名单又在豁免时黑名单被静默吞掉
    （`rm D:/secrets/x` → allow），用户在黑名单里划的禁区可从豁免绕开。
    现在语义分层：豁免只能豁免「workdir 边界」这一层，不能豁免「用户显式拉黑的路径」。
    """
    cfg = cfg or SandboxConfig.get_instance()
    exempt = [str(p) for p in (cfg.get("delete.exempt_paths") or [])]
    if not exempt:
        return False
    targets = delete_targets(command, workdir)
    candidates = targets["existing"] + [Path(m) for m in targets["missing"]]
    if not candidates:
        return False
    workdir = workdir if workdir is not None else _current_workdir()
    blacklist: list = list(cfg.get("path.blacklist") or [])
    for target in candidates:
        norm_target = _norm(target)
        # 黑名单优先：落在用户显式拉黑路径内的目标不允许被豁免放行
        if blacklist and _match_blacklist(norm_target, blacklist):
            return False
        matched = False
        for item in exempt:
            if os.path.isabs(os.path.expandvars(item)):
                if _under(norm_target, _norm(item)):
                    matched = True
                    break
            else:
                if _under(norm_target, _norm(Path(workdir) / item)):
                    matched = True
                    break
        if not matched:
            return False
    return True


def is_delete_command(command: str) -> bool:
    """是否删除类命令 —— **审批层判据（宽松）**

    用途：审批升档（`sandbox_check_tool` 的删除保护门控）、审批卡来源与风险
    档位、UI 展示。这里**宁可多认**：多认一次只是多弹一次审批，漏认则是真删除
    静默执行。

    覆盖：
    - 命令位删除动词（``rm`` / ``del`` / ``Remove-Item`` / ``git rm`` / 壳载荷）
    - .NET IO Delete 调用形态
    - ``find -delete`` / ``find -exec rm``
    - 解释器内删除（``python -c "shutil.rmtree(...)"`` / ``node -e "fs.rmSync"``）
    - 其他真删工具（``shred`` / ``srm`` / ``truncate -s 0`` / ``git clean -fdx``）

    ⚠ 与之对应的是 `is_trash_delete_command`（严格判据，供垃圾桶用）。两者
    不可合并：垃圾桶误判 = 把无关文件搬走（2026-10-25 实测多起事故）；审批
    误判 = 多问一次。方向相反，共用一份判定必有一端出错。
    """
    if not command:
        return False
    if _find_delete_command_spans(command):
        return True
    if _DOTNET_DELETE_PATTERN.search(command):
        return True
    return _has_soft_delete_signal(command)


# find 的 -delete 动作（无删除动词，但真删文件）
_FIND_DELETE_FLAG = "-delete"
# 解释器载荷内删除特征：命中即视为删除命令（无法静态解析变量，只能按特征）
# ⚠ 全部按**小写**比较（调用方已 lowered）。node 侧不写 "fs.rm"：
#   `require('fs').rmSync(...)` 里模块名与方法名被 `).` 隔开，只有
#   `.rmsync(` / `.rmdirsync(` 这种**方法调用形态**才命得中（实测漏判两次）。
_INTERPRETER_DELETE_TOKENS = (
    "shutil.rmtree",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "path.unlink",
    ".unlink(",
    "fs.rmsync",
    "fs.rm(",
    "fs.rmdir",
    "fs.unlinksync",
    ").rmsync(",
    ").rmdirsync(",
    ").unlinksync(",
    "rimraf",
    "remove-item",
    "ri ",
)
# 其他真删磁盘文件的命令（词表外，但破坏不可逆）
_EXTRA_DELETE_HEADS = frozenset({"shred", "srm", "wipe", "erase"})
# 条件删除型 git 子命令（需配合破坏性 flag，见 _has_soft_delete_signal）
_GIT_CLEAN_SUBCOMMAND = "clean"


def _has_soft_delete_signal(command: str) -> bool:
    """宽松删除信号：处理「没有删除动词但确实删文件」的形态

    只供审批层使用（`is_delete_command`）。垃圾桶不采样本函数 —— 这些形态
    目标藏在 glob / 变量 / 管道里，无法安全定位要搬什么，硬搬只会误伤。
    """
    lowered = command.lower()
    tokens = _split_tokens(command)
    # find -delete
    if any(_strip_quotes(t).lower() == _FIND_DELETE_FLAG for t in tokens):
        return True
    # 解释器/脚本载荷内的删除调用
    if _interpreter_payload_like(command) and any(tok in lowered for tok in _INTERPRETER_DELETE_TOKENS):
        return True
    # 其他真删工具（段首才认，避免 `grep shred` 误报）
    for idx in _command_positions(tokens):
        head = _head_of(tokens[idx])
        if head in _EXTRA_DELETE_HEADS:
            return True
    # git clean -f/-x/-d 会真删未跟踪文件
    if _git_clean_forced(tokens):
        return True
    # truncate 截断为 0 字节等价删除内容
    if _truncate_zeroed(tokens):
        return True
    return False


def _interpreter_payload_like(command: str) -> bool:
    """命令是否在调用解释器（python -c / node -e / powershell -c …）"""
    tokens = _split_tokens(command)
    if not tokens:
        return False
    head = _head_of(tokens[0])
    return head in _COMMAND_PREFIX_WRAPPERS or head in ("python", "python3", "node", "ruby", "perl")


def _git_clean_forced(tokens: list) -> bool:
    """``git clean`` 是否带强制/实际删除 flag（-f/-x/-d 任一，-n 只预演）"""
    for idx in _command_positions(tokens):
        if _head_of(tokens[idx]) != _GIT_CLEAN_SUBCOMMAND:
            continue
        rest = [_strip_quotes(t).lower() for t in tokens[idx + 1 :]]
        if "-n" in rest or "--dry-run" in rest:
            return False
        if any(t in ("-f", "--force", "-x", "-d") or (t.startswith("-") and not t.startswith("--") and set("fxd") & set(t)) for t in rest):
            return True
    return False


def _truncate_zeroed(tokens: list) -> bool:
    """``truncate -s 0 f`` 把文件截成空 = 内容销毁"""
    for idx in _command_positions(tokens):
        if _head_of(tokens[idx]) != "truncate":
            continue
        rest = [_strip_quotes(t).lower() for t in tokens[idx + 1 :]]
        for pos, tok in enumerate(rest):
            if tok == "-s" and pos + 1 < len(rest) and rest[pos + 1] in ("0", "0b", "0k"):
                return True
            if tok.startswith("-s") and len(tok) > 2 and tok[2:] in ("0", "0b", "0k"):
                return True
    return False


def is_trash_delete_command(command: str) -> bool:
    """是否**垃圾桶可安全处理**的删除命令 —— 严格判据

    与 `is_delete_command`（审批宽松判据）的分工：

    | | 误判为删除 | 漏判 |
    |---|---|---|
    | 审批层（宽松） | 多弹一次审批，无害 | 真删除静默执行，危险 |
    | 垃圾桶（严格） | **把无关文件搬走**，危险 | 少一层保护，无害 |

    所以垃圾桶只认「命令位删除动词 + 目标**可静态解析**」的形态：
    - 不采样 `find -delete` / `git clean -fdx` / 解释器删除 —— 目标藏在 glob、
      变量、管道里，`delete_targets` 解析出来的是错误路径，硬搬就是误伤
    - 不采样 .NET Delete —— 目标是 `$p` 变量
    - 带变量的目标（`rm "$f"`）同样不搬：`$f` 展开后是什么这里算不出来

    注意：`eval "rm -rf build"` 这类**目标仍是字面量**的形态可以搬 —— 判据落在
    目标是否可解析，而不是命令是否用了求值器。
    """
    if not command:
        return False
    if not _find_delete_command_spans(command):
        return False
    return not _has_unresolvable_target(command)


# 目标 token 里的变量/占位特征：出现即无法静态定位（展开结果依赖运行时）
_UNRESOLVABLE_TARGET_CHARS = ("$", "%")
# glob 通配特征：可展开成实际路径，垃圾桶能安全处理
_GLOB_TARGET_CHARS = ("*", "?", "[", "]")
_UNRESOLVABLE_TARGET_TOKENS = frozenset({"{}", "+", ";", "\\;"})


def _has_unresolvable_target(command: str) -> bool:
    """删除命令是否存在**无法静态解析**的目标（变量 / 占位符）

    有则垃圾桶不敢搬：搬 ``$f`` 会去搬一个字面名为 ``$f`` 的路径（不存在），
    展开结果与这里算的不同，搬错比不搬更糟。

    注意 glob（``rm *.pyc``）**不算**不可解析：可以用 `glob` 展开成真实路径，
    垃圾桶照常能搬（见 `delete_targets` 的 glob 分支）。若把 glob 也排除，
    ``rm -rf *.log`` 这类高频清理就完全失去保护。
    """
    tokens = _explode_separators(_split_tokens(command or ""))
    for verb_idx, arg_idxs in _find_delete_command_spans(command):
        if arg_idxs == [-1]:
            # 壳载荷：内部再判一次
            if _has_unresolvable_target(_strip_quotes(tokens[verb_idx])):
                return True
            continue
        for j in arg_idxs:
            raw = _strip_quotes(tokens[j])
            if not raw:
                continue
            if raw in _UNRESOLVABLE_TARGET_TOKENS:
                return True
            if raw.lower() in _DELETE_FLAGS or raw.startswith(("-", "/")):
                continue
            if any(ch in raw for ch in _UNRESOLVABLE_TARGET_CHARS):
                return True
    return False


def _expand_glob_target(item: str, workdir: Optional[Path]) -> list:
    """展开 glob 目标为实际路径（`*.pyc` → 逐个匹配文件）

    非 glob 目标原样返回（交给 `_resolve_cmd_path` 解析）。glob 用 `Path.glob`
    在 workdir 下展开，展开失败/无匹配返回空列表（不搬任何东西，fail-safe）。
    """
    if not any(ch in item for ch in _GLOB_TARGET_CHARS):
        return [item]
    base = workdir if workdir is not None else _current_workdir()
    if not base:
        return []
    try:
        pattern = item.replace("\\", "/")
        # 绝对 glob（D:/x/*.log）与相对 glob 分别处理
        if Path(pattern).is_absolute():
            root = Path(Path(pattern).anchor)
            rel = pattern[len(str(root)) :].lstrip("/")
            return [str(p) for p in root.glob(rel)]
        return [str(p) for p in Path(base).glob(pattern)]
    except (OSError, ValueError):
        return []


def _is_workdir_or_ancestor(target: Path, workdir: Optional[Path] = None) -> bool:
    """target 是否就是工作目录本身，或工作目录的祖先目录

    只拦「向上」的方向：``rm -rf subdir``（subdir 在 workdir 内）必须继续正常工作。
    工作目录取显式实参，缺省回退 ``_current_workdir()``。
    """
    try:
        base = workdir if workdir is not None else _current_workdir()
        if not base:
            return False
        norm_target = _norm(target)
        norm_base = _norm(base)
        return _under(norm_base, norm_target)
    except (OSError, ValueError):
        return False


# 盘根一级的系统关键目录名（normcase 后小写比较）。这些目录的删除没有
# 合法场景：无论垃圾桶还是原命令，对它们的任何写操作都是灾难。
_PROTECTED_ROOT_NAMES = frozenset(
    {"windows", "system32", "program files", "program files (x86)", "programdata", "users"}
)


def _is_protected_root(target: Path) -> bool:
    """target 是否为盘符根（C:\\）或盘根一级系统目录（C:\\Windows 等）

    2026-10-25 事故：验证脚本把 C:\\ 喷给 stash，垃圾桶无防护直接
    shutil.move('C:\\', ...)，跨盘 move = copytree + rmtree —— 先花了
    33 分钟把 C 盘可读文件全部拷进垃圾桶，再反过来删 C 盘能删的一切。
    这类目标永不进垃圾桶，判定层也应直接 deny。
    """
    try:
        p = Path(target)
        if p.anchor and Path(p.anchor) == p:
            return True
        parent = p.parent
        if parent.anchor and Path(parent.anchor) == parent and p.name.lower() in _PROTECTED_ROOT_NAMES:
            return True
    except (OSError, ValueError):
        return False
    return False


def _contains_trash(target: Path, trash_root: Path) -> bool:
    """target 是否包含垃圾桶目录（trash_root 位于 target 之内或就是它）

    2026-10-25 补：move 一个包含垃圾桶的目录 = 把它搬进它自己内部，
    shutil 会走 copytree + rmtree 的退化路径（跨盘或目标已在源内），
    轻则报错留半份副本，重则递归膨胀。垃圾桶永不该收自己的容器。
    """
    try:
        norm_target = _norm(target)
        norm_trash = _norm(trash_root)
        return _under(norm_trash, norm_target)
    except (OSError, ValueError):
        return False


def _command_skips_disk(command: str) -> bool:
    """命令是否只改索引/只预演（git rm --cached、各种 --dry-run）

    这类命令不删除磁盘文件，垃圾桶搬走目标反而让用户的文件凭空消失
    （2026-10-25 实测：`git rm -r --cached app` 把整个 app 目录搬进垃圾桶）。
    """
    for token in _split_tokens(command or ""):
        if _strip_quotes(token).lower() in _NO_DISK_DELETE_FLAGS:
            return True
    return False


def stash_delete_targets_to_trash(command: str, trash_root: Union[str, Path], workdir: Optional[Path] = None) -> list:
    """垃圾桶模式：把删除命令的目标**移动**到垃圾箱目录（软删除，可找回）

    move 让原路径消失，原命令随后对目标执行时目标已不在
    （rm -f 静默成功）。单个目标移动失败不阻断（该目标由原命令正常删除）。
    """
    if not is_trash_delete_command(command):
        return []
    # 只改索引/只预演的命令不碰磁盘（--cached / --dry-run）
    if _command_skips_disk(command):
        return []
    targets = delete_targets(command, workdir)
    if not targets["existing"]:
        return []
    trash_root = Path(trash_root)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    moved: list = []
    for target in targets["existing"]:
        # 硬保险：工作目录本身（或其祖先）绝不 move。误判代价不对等——
        # 该进垃圾箱的没进，只是少一层保护；工作目录被 move 走，整个仓库
        # 从磁盘消失、所有后续命令报错（2026-10-24 实测连丢三次）。
        if _is_workdir_or_ancestor(target, workdir):
            logger.warning(f"[Sandbox] 拒绝把工作目录移入垃圾桶: {target}")
            continue
        # 盘符根/系统目录同样绝不 move（跨盘 move = copytree + rmtree，
        # 2026-10-25 实测把 C:\ 搬残）；保护与 workdir 无关，按目标本身判定。
        if _is_protected_root(target):
            logger.warning(f"[Sandbox] 拒绝把盘符根/系统目录移入垃圾桶: {target}")
            continue
        # 含垃圾桶自身的目录同样拒绝（否则等于把它搬进它自己内部）
        if _contains_trash(target, trash_root):
            logger.warning(f"[Sandbox] 拒绝把包含垃圾桶的目录移入垃圾桶: {target}")
            continue
        try:
            trash_root.mkdir(parents=True, exist_ok=True)
            dest = trash_root / f"{stamp}_{target.name}"
            n = 1
            while dest.exists():
                dest = trash_root / f"{stamp}_{target.stem}_{n}{target.suffix}"
                n += 1
            shutil.move(str(target), str(dest))
            moved.append(dest)
            logger.info(f"[Sandbox] 删除目标已移入垃圾桶: {target} -> {dest}")
        except Exception as e:  # noqa: BLE001 - 移动失败不阻断用户已批准的删除
            logger.warning(f"[Sandbox] 移入垃圾桶失败({e}): {target}")
    return moved


# ============================================================
# 审核处理配置（approval 节点归一化读取）
# ============================================================

APPROVAL_MODES = ("manual", "ai")
APPROVAL_TIMEOUT_ACTIONS = ("deny", "allow", "ai")
APPROVAL_TIMEOUT_MAX_SEC = 86400


def read_approval_policy(cfg: Optional[SandboxConfig] = None) -> dict:
    """读取并归一化审核处理配置（UI 与决策链共用；取值白名单 + 范围收窄）"""
    node = (cfg or SandboxConfig.get_instance()).get("approval") or {}
    if not isinstance(node, dict):
        node = {}
    mode = node.get("mode")
    if mode not in APPROVAL_MODES:
        mode = "manual"
    action = node.get("timeout_action")
    if action not in APPROVAL_TIMEOUT_ACTIONS:
        action = "deny"
    try:
        timeout = int(node.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        timeout = 0
    timeout = max(0, min(timeout, APPROVAL_TIMEOUT_MAX_SEC))
    model = node.get("ai_model")
    return {
        "mode": mode,
        "timeout_sec": timeout,
        "timeout_action": action,
        "ai_model": model if isinstance(model, str) else "",
    }


# ============================================================
# OS 级沙箱（macOS Seatbelt / sandbox-exec）
# ============================================================
# L1 三分法（本文件上半部）是执行前软判定；进程放行后无内核围栏。
# 本节把 agent 命令包进 sandbox-exec：白名单外的写入与（可选）网络在内核层被拒。
# 仅 darwin 生效；sandbox-exec 缺失 / 未启用 / 包装异常一律原样直通，绝不阻断执行。

_SEATBELT_BIN = "/usr/bin/sandbox-exec"
# SBPL 里 deny 过滤器的 require-not 排除区：系统临时/缓存与设备文件。
# /private/var/folders 是 macOS per-user 临时区（Python 缓存、剪贴板服务等依赖它）。
_OS_SB_BASE_WRITABLE = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/dev")
_os_sb_profile_cache: dict = {}  # (workdir_real, level) -> profile 文件路径


def os_sandbox_active() -> bool:
    """OS 级沙箱是否生效：darwin + 已启用 + sandbox-exec 存在"""
    if sys.platform != "darwin":
        return False
    try:
        if not bool(SandboxConfig.get_instance().get("os_sandbox.enabled")):
            return False
    except Exception:  # noqa: BLE001 - 配置异常视为关闭
        return False
    return Path(_SEATBELT_BIN).exists()


def _seatbelt_profile_text(workdir: Path, allow_network: bool) -> str:
    """生成 SBPL profile（deny default + 白名单 allow 模式）。

    ⚠ 实测（macOS 26.6）两个坑，改 profile 结构前先看：
    1. allow default + (deny file-write* (require-not ...)) 结构：并列多个
       require-not 或内嵌 or 时行为不定；且该结构下写 /dev/null 一律被拒
       （devfs vnode 在 deny 过滤器的 require-not/or 里不按 /dev 路径匹配）。
       deny default + allow 白名单模式则全部正常。
    2. workdir 同时放行原始与 resolve() 形态（/tmp → /private/tmp symlink）。

    白名单：workdir、系统临时区（/tmp、/private/tmp、/var/folders，
    覆盖 Python tempfile/pip 缓存）、/dev（/dev/null、urandom 必需）。
    mach-lookup/ipc 全放：本层目标是挡误操作（乱写/乱网），非硬隔离。
    """
    workdir_raw = str(workdir)
    workdir_real = str(workdir.resolve())
    write_paths = [workdir_raw, workdir_real, "/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/dev"]
    write_rule = " ".join(f'(subpath "{p}")' for p in dict.fromkeys(write_paths))
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow file-read*)",
        f"(allow file-write* {write_rule})",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow sysctl-write)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow iokit-get-properties)",
    ]
    if allow_network:
        lines.append("(allow network*)")
    # write+net 档：deny default 下网络默认已禁，无需额外 deny
    return "\n".join(lines) + "\n"


def _ensure_profile_file(workdir: Path, level: str) -> Path:
    """按 (工作区真实路径, 档位) 进程级缓存 profile 文件，temp 下落盘"""
    import hashlib

    workdir_real = workdir.resolve()
    key = (str(workdir_real), level)
    cached = _os_sb_profile_cache.get(key)
    if cached and Path(cached).exists():
        return Path(cached)
    text = _seatbelt_profile_text(workdir, allow_network=(level != "write+net"))
    digest = hashlib.md5(key[0].encode() + level.encode()).hexdigest()[:12]
    profile_path = Path(tempfile.gettempdir()) / f"drifox-os-sbx-{digest}.sb"
    profile_path.write_text(text, encoding="utf-8")
    _os_sb_profile_cache[key] = str(profile_path)
    return profile_path


def wrap_os_sandbox(command: str, cwd: Optional[Union[str, Path]] = None, use_shell: bool = False) -> str:
    """把命令包装进 sandbox-exec；未启用/非 darwin/异常时原样返回。

    use_shell 与调用方分流一致：run_safe 传 False（argv 直执行），run_with_shell 传 True。
    Path A：sandbox-exec -f profile <argv...>（shlex.quote 重组，不引入 shell 解释）
    Path B：sandbox-exec -f profile /bin/bash -c <command>（quote 防二次解释）
    """
    if not os_sandbox_active() or not command.strip():
        return command
    try:
        cfg = SandboxConfig.get_instance()
        level = str(cfg.get("os_sandbox.level") or "write")
        if level not in ("write", "write+net"):
            level = "write"
        workdir = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
        profile = _ensure_profile_file(workdir, level)
        prefix = [_SEATBELT_BIN, "-f", str(profile)]
        if use_shell:
            return " ".join(shlex.quote(p) for p in prefix) + f" /bin/bash -c {shlex.quote(command)}"
        argv = shlex.split(command)
        return " ".join(shlex.quote(p) for p in [*prefix, *argv])
    except Exception as e:  # noqa: BLE001 - 包装失败绝不阻断命令执行
        logger.warning(f"[OS sandbox] 包装失败，直通执行: {e}")
        return command
