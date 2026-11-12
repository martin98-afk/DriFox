# -*- coding: utf-8 -*-
"""插件依赖统一加载器（deps_loader）

设计文档：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md

职责（宿主侧单一入口）：
1. 平台声明检查：解析 plugin.json 的 platforms 字段，判断当前系统是否兼容
2. deps 路径注入：把插件自包含依赖目录幂等注入 sys.path
3. pip 依赖解析：合并 dependencies.pip 的 default 与当前平台列表

deps 目录规范：
    <plugin>/
      deps/            # 平台无关（纯 Python 包）
      deps/win32/      # Windows 二进制（.pyd/.dll），按 sys.platform 值命名
      deps/linux/
      deps/darwin/

注入顺序：deps/<platform>/ 先于 deps/（平台特定优先，同名包取平台版本）。
兼容存量：仅 deps/ 的老插件行为不变；两者皆无则不注入。

平台名映射（单一事实源）：
    plugin.json platforms 声明用友好名：windows / linux / darwin
    目录名与 dependencies.pip key 用 sys.platform 原值：win32 / linux / darwin
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

# P1-4：pip spec 白名单——首字符字母数字（拒 -flag 与 . 开头）、包名 + extras 可选 +
# 版本约束可选（裸包名合法）；另拒常见文件扩展名（requirements.txt 等间接注入链）
_PIP_SPEC_RE = re.compile(
    r"^(?!-)[A-Za-z0-9_][\w.\[\]-]*"
    r"((==|>=|<=|~=|!=|>|<)\d[\d.*]*)?"
    r"(,(==|>=|<=|~=|!=|>|<)\d[\d.*]*)*$"
)
_PIP_FILE_EXT_RE = re.compile(r"\.(txt|whl|tar|gz|zip|git|json|cfg|ini|yml|yaml)$", re.IGNORECASE)

# 友好名 → sys.platform 值（目录名 / pip key 用）
PLATFORM_DIR_MAP = {
    "windows": "win32",
    "win32": "win32",  # 容忍直接写 sys.platform 值
    "linux": "linux",
    "darwin": "darwin",
    "macos": "darwin",
    "mac": "darwin",
}

# 当前平台的 sys.platform 值（win32 / linux / darwin / 其它原值）
CURRENT_PLATFORM = sys.platform


def current_platform_key() -> str:
    """返回当前平台的 deps 目录名 / pip key（sys.platform 原值，win 归一为 win32）。"""
    return PLATFORM_DIR_MAP.get(CURRENT_PLATFORM, CURRENT_PLATFORM)


def normalize_platforms(manifest: dict) -> Optional[List[str]]:
    """解析 manifest 的 platforms 字段为 sys.platform 值列表。

    返回 None 表示未声明（= 全平台兼容，存量插件零改动）。
    非法值（未知平台名）记 warning 并忽略该项。
    """
    raw = manifest.get("platforms")
    if not raw:
        return None
    if not isinstance(raw, list):
        logger.warning(f"[deps_loader] platforms 字段须为数组，忽略: {raw!r}")
        return None
    keys: List[str] = []
    for name in raw:
        if not isinstance(name, str):
            continue
        key = PLATFORM_DIR_MAP.get(name.strip().lower())
        if key is None:
            logger.warning(f"[deps_loader] 未知平台名 {name!r}（支持 windows/linux/darwin），忽略")
            continue
        if key not in keys:
            keys.append(key)
    return keys or None


def check_platform(manifest: dict) -> Tuple[bool, str]:
    """检查插件是否兼容当前平台。

    Returns:
        (兼容?, 原因文案)。未声明 platforms → 恒兼容（向后兼容）。
    """
    keys = normalize_platforms(manifest)
    if keys is None:
        return True, ""
    if current_platform_key() in keys:
        return True, ""
    declared = ", ".join(keys)
    return False, f"插件仅支持 [{declared}]，当前平台 {current_platform_key()} 不兼容"


def check_host_compat(manifest: dict) -> Tuple[bool, str]:
    """检查插件的 min_host_version 是否满足当前宿主（市场端可读版本判定）。

    与 ``check_platform`` 并列、同一返回形状，供插件市场据此把「安装 / 更新」
    按钮置灰。版本比较复用 ``app.plugins.version_gate.parse_semver``，与宿主
    加载门禁用的是同一套解析规则（避免「市场说能装、装完被门禁拦掉」）。

    与 ``check_host_version`` 的区别是**不写日志**：市场一次渲染要判定全部
    插件，走带 warning 的门禁函数会刷出上百行 [VersionGate] 噪音。

    Returns:
        (兼容?, 原因文案)。未声明 min_host_version → 恒兼容（向后兼容）。
    """
    from app.plugins.version_gate import host_version, parse_semver

    required = (manifest or {}).get("min_host_version")
    if not required:
        return True, ""
    need = parse_semver(str(required))
    if need is None:
        return False, f"插件的版本要求格式非法（{required}），无法判断兼容性"
    cur = parse_semver(host_version())
    if cur is None:
        return True, ""  # 宿主版本读不到：放行，不误伤
    if cur >= need:
        return True, ""
    return False, (
        f"需要 DriFox v{need[0]}.{need[1]}.{need[2]} 或更高版本，"
        f"当前 v{cur[0]}.{cur[1]}.{cur[2]}"
    )


def host_version_string() -> str:
    """当前宿主版本（去 v 前缀）；读不到返回 "0.0.0"。"""
    from app.plugins.version_gate import host_version

    return host_version()


def resolve_pip_deps(manifest: dict, platform_key: Optional[str] = None) -> List[str]:
    """合并 dependencies.pip 的 default 与指定平台列表（去重、保序）。

    P1-4：PEP 508 白名单校验——包名+extras 可选+版本约束必需；
    git+/file:///-r/--flag 等 注入形态拒收（移除 + warning）。

    Args:
        manifest: 插件清单
        platform_key: 平台 key，缺省用当前平台

    Returns:
        合并后的依赖规格列表（如 ["httpx>=0.27", "pywin32"]）；无声明返回空。
    """
    deps = manifest.get("dependencies") or {}
    pip = deps.get("pip")
    if not isinstance(pip, dict):
        return []
    key = platform_key or current_platform_key()

    merged: List[str] = []

    def _add(specs) -> None:
        if isinstance(specs, str):
            specs = [specs]
        if not isinstance(specs, list):
            return
        for s in specs:
            if isinstance(s, str) and s and s not in merged:
                # P1-4：spec 白名单 + 文件扩展名拒绝（-r 间接注入链）
                if not _PIP_SPEC_RE.match(s) or _PIP_FILE_EXT_RE.search(s):
                    logger.warning(
                        f"[deps_loader] 拒收非法 pip spec（白名单外，疑似注入/非固定版本）: {s!r}"
                    )
                    continue
                merged.append(s)

    _add(pip.get("default"))
    _add(pip.get(key))
    return merged


# ── pip spec 解析与版本比较（L1 版本校验 / L2 区间冲突判定）────────────
# 不依赖 packaging：宿主打包版不保证带该库。只处理 _PIP_SPEC_RE 白名单允许的
# 约束形态（==/>=/<=/~/!=/>/< 与 ==1.* 通配），语义求“不错杀”，不追求 PEP 440 完备。

_SPEC_OP_RE = re.compile(r"^(==|>=|<=|~=|!=|>|<)(.+)$")


def _split_spec(spec: str) -> Tuple[str, str]:
    """spec → (包名原文, 约束段原文)。"""
    body = spec.strip()
    for i, ch in enumerate(body):
        if ch in "=<>!~":
            return body[:i].strip(), body[i:].strip()
    return body, ""


def canonical_pkg_name(spec: str) -> str:
    """spec / 分发名 → 规范化名（PEP 503：小写，`-` `.` 归一为 `_`）。"""
    raw, _ = _split_spec(spec)
    raw = raw.split("[")[0].strip()
    return raw.replace("-", "_").replace(".", "_").lower()


def parse_spec_ops(spec: str) -> List[Tuple[str, str]]:
    """spec → [(op, version)]；裸包名返回空列表。"""
    _, cons = _split_spec(spec)
    if not cons:
        return []
    ops: List[Tuple[str, str]] = []
    for part in cons.split(","):
        m = _SPEC_OP_RE.match(part.strip())
        if m:
            ops.append((m.group(1), m.group(2).strip()))
    return ops


def version_key(v: str) -> Tuple:
    """宽松版本键：数字段转 int、字母段转小写字符串，补齐到 4 段。

    只为「装的是不是声明的版本」与区间冲突服务。预发布段（rc/beta）按字符串
    比较会得出 rc > 正式版，与 PEP 440 相反，但方向是宽松（不误杀），可接受。
    """
    parts = re.findall(r"\d+|[A-Za-z]+", str(v))
    key: List[Tuple[int, object]] = []
    for p in parts:
        key.append((0, int(p)) if p.isdigit() else (1, p.lower()))
    while len(key) < 4:
        key.append((0, 0))
    return tuple(key)


def spec_satisfied(installed: str, spec: str) -> bool:
    """已安装版本是否满足 spec（裸包名恒 True）。"""
    ops = parse_spec_ops(spec)
    if not ops:
        return True
    have = version_key(installed)
    for op, want in ops:
        if op == "==":
            if want.endswith(".*"):
                if not str(installed).startswith(want[:-2]):
                    return False
            elif have != version_key(want):
                return False
        elif op == "!=":
            if have == version_key(want):
                return False
        elif op == ">=":
            if have < version_key(want):
                return False
        elif op == "<=":
            if have > version_key(want):
                return False
        elif op == ">":
            if have <= version_key(want):
                return False
        elif op == "<":
            if have >= version_key(want):
                return False
        elif op == "~=":
            # ~=1.4.2 = >=1.4.2 且 <1.5；只校下界，上界宁可不判（不误杀）
            if have < version_key(want):
                return False
    return True


def _bounds_verdict(ops: List[Tuple[str, str]]) -> str:
    """合并约束判断是否有解："ok" / "conflict" / "unknown"。"""
    eq: Optional[Tuple] = None
    lo: Optional[Tuple] = None
    lo_inc = True
    hi: Optional[Tuple] = None
    hi_inc = True
    for op, want in ops:
        if want.endswith(".*"):
            return "unknown"  # 通配不参与区间判定
        k = version_key(want)
        if op == "==":
            if eq is not None and eq != k:
                return "conflict"
            eq = k
        elif op == ">=":
            if lo is None or k > lo:
                lo, lo_inc = k, True
        elif op == ">":
            if lo is None or k > lo:
                lo, lo_inc = k, False
        elif op == "<=":
            if hi is None or k < hi:
                hi, hi_inc = k, True
        elif op == "<":
            if hi is None or k < hi:
                hi, hi_inc = k, False
    if eq is not None:
        if lo is not None and (eq < lo or (eq == lo and not lo_inc)):
            return "conflict"
        if hi is not None and (eq > hi or (eq == hi and not hi_inc)):
            return "conflict"
        return "ok"
    if lo is None or hi is None:
        return "ok"  # 单边/无边：一定存在解
    if lo > hi:
        return "conflict"
    if lo == hi and not (lo_inc and hi_inc):
        return "conflict"
    return "ok"


def specs_conflict(spec_a: str, spec_b: str) -> bool:
    """两个 spec 是否不可能同时满足（同包前提下）。

    保守：读不懂的组合（通配、上界缺失的 ~=）一律判不冲突，宁可漏报不误杀——
    误杀会让本来能用的插件被门禁拦掉。
    """
    if canonical_pkg_name(spec_a) != canonical_pkg_name(spec_b):
        return False
    return _bounds_verdict(parse_spec_ops(spec_a) + parse_spec_ops(spec_b)) == "conflict"


def find_deps_conflicts(declarations: Dict[str, List[str]]) -> List[Tuple[str, str, str]]:
    """探测同一包被不同插件要求了不可调和的版本。

    Args:
        declarations: {插件名: [spec, ...]}（只传门禁未拦的启用插件）

    Returns:
        [(包名, 插件A, 插件B)]，同一对只报一次；同插件自相矛盾不在此处报。
    """
    by_pkg: Dict[str, List[Tuple[str, str]]] = {}
    for plugin, specs in declarations.items():
        for spec in specs:
            name = canonical_pkg_name(spec)
            if name:
                by_pkg.setdefault(name, []).append((plugin, spec))
    out: List[Tuple[str, str, str]] = []
    for pkg, items in sorted(by_pkg.items()):
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                pa, sa = items[i]
                pb, sb = items[j]
                if pa == pb:
                    continue
                if specs_conflict(sa, sb):
                    out.append((pkg, pa, pb))
    return out


def deps_paths(plugin_dir: Path) -> List[Path]:
    """返回插件应注入的 deps 路径列表（仅实际存在的，按优先级排序）。

    顺序即 sys.path 优先级：平台特定目录在前，公共 deps/ 在后。
    """
    if not plugin_dir.is_dir():
        return []
    paths: List[Path] = []
    plat = plugin_dir / "deps" / current_platform_key()
    if plat.is_dir():
        paths.append(plat)
    common = plugin_dir / "deps"
    # 公共 deps/ 必须是目录且非空才有意义；空目录不注入
    if common.is_dir() and any(common.iterdir()):
        # 平台目录是 deps/ 的子目录，已单独注入；公共注入仍保留（纯 Python 包在顶层）
        paths.append(common)

    # —— 安全：剔除会劫持 stdlib / 宿主包的 deps 目录 ——
    # deps 位于 sys.path[0]（优先于 stdlib），deps/json.py 即可替换全进程
    # 的 json 模块。此处按目录粒度拒注入并告警，不影响其余 deps 生效。
    safe: List[Path] = []
    for d in paths:
        shadowed = _shadowing_entries(d)
        if shadowed:
            logger.warning(
                f"[deps_loader] {plugin_dir.name} 的 {d.name} 含与 stdlib/宿主同名的顶层模块 "
                f"{shadowed}，已拒绝注入 sys.path（疑似依赖劫持）"
            )
            continue
        safe.append(d)
    return safe


_STDLIB_TOPLEVEL: Optional[frozenset] = None


def _stdlib_toplevel_names() -> frozenset:
    """stdlib 顶层模块名集合（sys.stdlib_module_names，3.10+）。

    用于识别 deps/ 里的"同名劫持"：deps 被 insert 到 sys.path[0]，
    若其中存在 json.py / os.py / typing.py 等，会整体替换全进程对 stdlib 的导入。

    P4：结果模块级缓存（stdlib 集合进程内不变，免每次调用重建 frozenset）。
    """
    global _STDLIB_TOPLEVEL
    if _STDLIB_TOPLEVEL is None:
        names = getattr(sys, "stdlib_module_names", None)
        _STDLIB_TOPLEVEL = frozenset(names) if names else frozenset()
    return _STDLIB_TOPLEVEL


def _host_toplevel_names() -> frozenset:
    """宿主自身顶层包名（app / plugins）— 同样禁止被 deps 覆盖。"""
    return frozenset({"app", "plugins"})


def _shadowing_entries(dep_dir: Path) -> List[str]:
    """返回 dep_dir 下会劫持 stdlib / 宿主包的顶层模块名列表。"""
    banned = _stdlib_toplevel_names() | _host_toplevel_names()
    if not banned:
        return []
    hits: List[str] = []
    try:
        for entry in dep_dir.iterdir():
            stem = entry.name
            is_pkg = entry.is_dir() and (entry / "__init__.py").exists()
            if entry.is_file() and entry.suffix == ".py":
                stem = entry.stem
            elif not is_pkg:
                continue
            if stem in banned:
                hits.append(stem)
    except OSError as e:
        logger.debug(f"[deps_loader] 扫描 deps 目录失败 {dep_dir}: {e}")
    return sorted(set(hits))


# ── 注入登记（卸载/禁用时摘除 sys.path；冲突诊断时回溯归属）─────────────
# (sys.path 里的原始路径字符串, 插件名)。sys.path 项可能由插件自己在模块顶层
# insert（gateway 老写法），本表只记录宿主注入的部分，摘除时按路径归属判定。
_INJECTED: List[Tuple[str, str]] = []


def remove_deps_from_path(plugin_dir: Path) -> List[str]:
    """摘除插件注入过的 deps 路径（卸载/禁用后调用，幂等）。

    不摘除的后果：僵尸路径长期占据 sys.path，同名目录一旦被复用就"复活"旧依赖；
    插件 A 的包被插件 B 隐式 import 的能力也不会随卸载消失。

    Returns:
        本次从 sys.path 摘除的路径列表。
    """
    try:
        prefix = str(Path(plugin_dir).resolve()).lower()
    except (OSError, ValueError):
        return []
    removed: List[str] = []
    for s, _name in list(_INJECTED):
        try:
            p = Path(s)
        except (OSError, ValueError):
            continue
        # 只认 deps 目录本身或其子目录：插件根目录也可能被工具/测试插进
        # sys.path（为了 import 插件模块），按前缀无脑删会把它一起摘掉
        if "deps" not in p.parts:
            continue
        try:
            key = str(p.resolve()).lower()
        except (OSError, ValueError):
            key = s.lower()
        # 前缀命中且落在插件目录内（用分隔符断开，避免 foo 命中 foobar）
        if not (key == prefix or key.startswith(prefix + "/") or key.startswith(prefix + "\\")):
            continue
        _INJECTED.remove((s, _name))
        hit = False
        while s in sys.path:
            sys.path.remove(s)
            hit = True
        if hit:
            removed.append(s)
    if removed:
        logger.debug(f"[deps_loader] 摘除 {plugin_dir.name} deps: {removed}")
    return removed


def ensure_deps_on_path(plugin_dir: Path) -> List[str]:
    """幂等注入插件的 deps 路径到 sys.path（平台目录优先）。

    Returns:
        本次实际注入的路径列表（已在 sys.path 中的不重复注入）。
    """
    injected: List[str] = []
    for p in reversed(deps_paths(plugin_dir)):
        s = str(p)
        if s not in sys.path:
            # 倒序插入（先 deps/ 后 deps/<platform>/）：后插者排 sys.path 更前，
            # 保证平台目录优先于公共目录
            sys.path.insert(0, s)
            injected.append(s)
        if all(s != item[0] for item in _INJECTED):
            _INJECTED.append((s, plugin_dir.name))
    injected.reverse()  # 返回按优先级排序（平台在前）
    if injected:
        logger.debug(f"[deps_loader] 注入 {plugin_dir.name} deps: {injected}")
    return injected


def _scan_deps_state(plugin_dir: Path) -> Tuple[Dict[str, str], set]:
    """扫描 deps/<platform>/ 与 deps/：返回 (规范化分发名→版本, 无版本顶层名集合)。

    两个原始目录都扫（不走 deps_paths）：被安全过滤拒注入的目录同样参与判定，
    否则会误报缺失 → 触发无意义的重复安装。平台目录先扫，同名包以平台版本为准
    （实际 import 时平台目录在 sys.path 更前）。
    """
    scan_dirs: List[Path] = []
    plat = plugin_dir / "deps" / current_platform_key()
    if plat.is_dir():
        scan_dirs.append(plat)
    common = plugin_dir / "deps"
    if common.is_dir():
        scan_dirs.append(common)

    versions: Dict[str, str] = {}  # 规范化分发名 → 已装版本（dist-info 权威）
    loose_names: set = set()  # 无版本信息的顶层目录 / .py stem
    for d in scan_dirs:
        try:
            for entry in d.iterdir():
                if entry.is_dir():
                    if not entry.name.endswith(".dist-info"):
                        loose_names.add(canonical_pkg_name(entry.name))
                        continue
                    # 目录名是 {分发名}-{版本}.dist-info（分发名里的 - 已换成 _）；
                    # 版本优先读 METADATA 的 Version 行，读不到才回退目录名解析
                    ver = _read_dist_version(entry)
                    if not ver:
                        rest = entry.name[: -len(".dist-info")]
                        _head, _, tail = rest.partition("-")
                        if tail:
                            ver = tail
                    if ver:
                        versions.setdefault(canonical_pkg_name(entry.name.split("-")[0]), ver)
                elif entry.is_file() and entry.suffix == ".py":
                    loose_names.add(canonical_pkg_name(entry.stem))
        except OSError:
            continue
    return versions, loose_names


def _importable(pkg: str) -> bool:
    """宿主环境能否 import 该顶层名（插件 deps 之外的兜底来源）。"""
    import importlib.util

    try:
        return importlib.util.find_spec(pkg) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def missing_pip_deps(plugin_dir: Path, manifest: dict) -> List[str]:
    """检查声明的 pip 依赖哪些未就绪（未落地、版本不满足、或不可导入）。

    判定顺序（任一成立即就绪）：
    1. deps/<platform>/ 或 deps/ 下的 dist-info 版本满足声明约束
    2. deps 目录里有同名顶层目录/模块但读不到版本（源码/vendor 形态）→ 就绪
    3. importlib.util.find_spec 可导入（宿主环境提供）→ 就绪

    ★ L1：此前只按名字判定，声明 httpx>=0.28 而 deps 里装着 0.20 也算就绪，
    插件静默跑在错误版本上。现在带 dist-info 的一律按版本判定：不满足即判缺失，
    由 DepsInstaller 重装成声明版本。

    Returns:
        缺失的依赖规格列表（供 UI 提示安装）。
    """
    specs = resolve_pip_deps(manifest)
    if not specs:
        return []
    versions, loose_names = _scan_deps_state(plugin_dir)

    missing: List[str] = []
    for spec in specs:
        pkg = canonical_pkg_name(spec)
        if not pkg:
            continue
        ver = versions.get(pkg)
        if ver is not None:
            if spec_satisfied(ver, spec):
                continue
            logger.warning(
                f"[deps_loader] {plugin_dir.name} 依赖 {pkg} 已装 {ver} 不满足声明 {spec}，判缺失待重装"
            )
            missing.append(spec)
            continue
        # 已有同名顶层包但无版本信息（源码/vendor 形态）：沿用旧的按名判定
        if pkg in loose_names:
            continue
        if _importable(pkg):
            continue
        missing.append(spec)
    return missing


def describe_dep_issues(plugin_dir: Path, manifest: dict) -> List[str]:
    """依赖问题的可读描述（诊断报告 / 设置卡展示用）。

    判定口径与 ``missing_pip_deps`` 完全一致，只是把「哪一条不满足、装的是
    什么版本」写出来——诊断报告里只说「依赖缺失」会让用户和 AI 都难以定位。

    Returns:
        每项形如 "httpx>=0.28（已装 0.20.0，不满足）" / "pywin32（未安装）"；
        全部就绪返回空列表。
    """
    specs = resolve_pip_deps(manifest)
    if not specs:
        return []
    versions, loose_names = _scan_deps_state(plugin_dir)

    out: List[str] = []
    for spec in specs:
        pkg = canonical_pkg_name(spec)
        if not pkg:
            continue
        ver = versions.get(pkg)
        if ver is not None:
            if not spec_satisfied(ver, spec):
                out.append(f"{spec}（已装 {ver}，不满足）")
            continue
        if pkg in loose_names:
            continue
        if _importable(pkg):
            continue
        out.append(f"{spec}（未安装）")
    return out


def _read_dist_version(dist_info_dir: Path) -> str:
    """读 dist-info/METADATA 的 Version 行；读不到返回空串。"""
    meta = dist_info_dir / "METADATA"
    try:
        with meta.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
                if not line.strip():
                    break  # 头部结束
    except OSError:
        pass
    return ""
