# -*- coding: utf-8 -*-
"""插件 pip 依赖安装器（DepsInstaller）

设计文档：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md

职责：把 plugin.json 的 dependencies.pip 声明落盘到插件 deps/<platform>/ 目录。

安装策略（优先级）：
1. 系统 uv：`uv pip install --python <PY> --target <plugin>/deps/<platform>/ pkg...`
   uv 自管解释器下载与解析，不依赖宿主 exe 内嵌的 PyInstaller 打散 Python。
2. 回退（无 uv 或 uv 失败）：PyPI simple API 查当前平台兼容 wheel
   （win_amd64 / manylinux / macosx）→ 下载 → zipfile 解压到 deps/<platform>/。

约束：
- 宿主是 PyInstaller onedir 打包，exe 内无 pip、无完整标准库结构，
  不能向宿主环境 pip install —— 一律装进插件目录（--target 语义）。
- 失败不回滚插件本体安装（设计决策 C：自动+手动兜底），
  由调用方依据 InstallResult.missing 在 UI 显示手动重试按钮。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from loguru import logger

from app.plugins.deps_loader import current_platform_key, resolve_pip_deps  # noqa: F401 (resolve 供复用)

# uv 安装使用的 Python 版本（与 DriFox 发版一致；uv 会按需自管下载解释器）
TARGET_PYTHON = "3.14"

PYPI_SIMPLE = "https://pypi.org/simple"
# 镜像 simple 端点约定：index_url 以 /simple 结尾直接用，否则补 /simple
DEFAULT_TIMEOUT = 120


@dataclass
class InstallResult:
    """依赖安装结果"""

    success: bool = True
    missing: List[str] = field(default_factory=list)  # 未装上的依赖规格
    log: List[str] = field(default_factory=list)  # 关键日志（UI 可展示）


def get_index_url() -> str:
    """读取 Settings 的 pip.index_url（空串回退官方源）。"""
    try:
        from app.utils.config import Settings

        url = (Settings.get_instance().pip_index_url.value or "").strip()
        if url:
            if not url.rstrip("/").endswith("/simple"):
                url = url.rstrip("/") + "/simple"
            return url
    except Exception:
        pass
    return PYPI_SIMPLE


class DepsInstaller:
    """插件 pip 依赖安装器（uv 优先 / wheel 回退）"""

    def install(self, plugin_dir: Path, manifest: dict) -> InstallResult:
        """安装插件声明的 pip 依赖到 deps/<platform>/

        - 无声明 → 直接成功（零依赖插件零开销）
        - 依赖已就绪（deps 落地或可导入）→ 跳过
        """
        specs = resolve_pip_deps(manifest)
        if not specs:
            return InstallResult()

        missing = self._missing(plugin_dir, manifest)
        if not missing:
            return InstallResult()

        target = plugin_dir / "deps" / current_platform_key()
        target.mkdir(parents=True, exist_ok=True)

        res = self._install_with_uv(missing, target)
        if res.success:
            return res
        logger.info(f"[DepsInstaller] uv 路径失败，回退 wheel 解压: {res.log[-1] if res.log else ''}")
        return self._install_with_wheels(missing, target, res)

    # ── 缺失检测 ──────────────────────────────────────────

    def _missing(self, plugin_dir: Path, manifest: dict) -> List[str]:
        from app.plugins.deps_loader import missing_pip_deps

        try:
            return missing_pip_deps(plugin_dir, manifest)
        except Exception as e:  # 检测失败按全部缺失处理（保守：宁可重装）
            logger.warning(f"[DepsInstaller] 缺失检测异常，按全量处理: {e}")
            return resolve_pip_deps(manifest)

    def _install_with_uv(self, specs: List[str], target: Path) -> InstallResult:
        uv = shutil.which("uv")
        if not uv:
            return InstallResult(success=False, missing=specs, log=["未检测到系统 uv"])

        cmd = [
            uv,
            "pip",
            "install",
            "--python",
            TARGET_PYTHON,
            "--index-url",
            get_index_url(),
            "--target",
            str(target),
            "--quiet",
            *specs,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired:
            return InstallResult(success=False, missing=specs, log=[f"uv 安装超时(>{DEFAULT_TIMEOUT}s)"])
        except OSError as e:
            return InstallResult(success=False, missing=specs, log=[f"uv 启动失败: {e}"])

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            return InstallResult(
                success=False,
                missing=specs,
                log=[f"uv 退出码 {proc.returncode}: {err[-1] if err else ''}"],
            )
        logger.info(f"[DepsInstaller] uv 安装成功: {specs} -> {target}")
        return InstallResult(success=True, log=[f"uv 安装成功: {', '.join(specs)}"])

    # ── 路径 2：PyPI simple API + wheel 解压 ────────────────

    def _install_with_wheels(
        self, specs: List[str], target: Path, prev: InstallResult
    ) -> InstallResult:
        """无 uv / uv 失败时：逐包下载平台兼容 wheel 并解压（纯 zipfile，无 pip）。"""
        import urllib.request

        res = InstallResult(log=list(prev.log))
        tag_prefs = _platform_wheel_tags()
        index = get_index_url()

        for spec in specs:
            pkg, want_ver = _split_spec(spec)
            try:
                url, filename = _pick_wheel(index, pkg, want_ver, tag_prefs)
            except Exception as e:
                res.success = False
                res.missing.append(spec)
                res.log.append(f"{spec}: 解析 wheel 失败: {e}")
                continue
            if url is None:
                res.success = False
                res.missing.append(spec)
                res.log.append(f"{spec}: {index} 未找到兼容 wheel（平台 {current_platform_key()}）")
                continue
            try:
                logger.info(f"[DepsInstaller] 下载 {filename}")
                req = urllib.request.Request(url, headers={"User-Agent": "DriFox-deps-installer"})
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp, tempfile_path(
                    filename
                ) as tmp:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        tmp.write(chunk)
                    tmp.flush()
                    with zipfile.ZipFile(tmp.name) as zf:
                        # 防路径穿越：拒绝绝对路径/.. 成员
                        for member in zf.namelist():
                            _m = Path(member)
                            if _m.is_absolute() or ".." in _m.parts:
                                raise ValueError(f"wheel 内非法路径: {member}")
                        zf.extractall(target)
                res.log.append(f"{spec}: 安装成功 ({filename})")
            except Exception as e:
                res.success = False
                res.missing.append(spec)
                res.log.append(f"{spec}: 下载/解压失败: {e}")

        if res.success:
            logger.info(f"[DepsInstaller] wheel 回退安装成功: {specs} -> {target}")
        return res


# ============================================================
# 辅助函数
# ============================================================


def _platform_wheel_tags() -> List[str]:
    """当前平台的 wheel 兼容 tag 优先级列表（前缀匹配，通用在前）。"""
    import platform as _pf

    arch = _pf.machine().lower()  # amd64 / x86_64 / arm64 ...
    if arch in ("x86_64", "amd64"):
        arch64 = "amd64"
        glibc = "manylinux_2_17_x86_64.manylinux2014_x86_64"
    elif arch in ("arm64", "aarch64"):
        arch64 = "arm64"
        glibc = "manylinux_2_17_aarch64.manylinux2014_aarch64"
    else:
        arch64 = arch
        glibc = f"manylinux_{arch}"

    ver = "".join(str(v) for v in sys.version_info[:2])  # 3.14 -> "314"
    return [
        f"py3-none-any",  # 纯 Python 最优先
        f"cp{ver}-cp{ver}",
        f"win_{arch64}",
        f"{glibc}",
        f"macosx_11_0_{arch64}",
        f"manylinux2014_{arch64.replace('amd64', 'x86_64')}",
        f"linux_{arch64}",
    ]


def _split_spec(spec: str):
    """'pkg>=1.2' -> ('pkg', '1.2')；'pkg' -> ('pkg', None)。"""
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        if sep in spec:
            pkg, ver = spec.split(sep, 1)
            return pkg.strip(), ver.strip()
    return spec.strip(), None


def _pick_wheel(
    index: str, pkg: str, want_ver: Optional[str], tag_prefs: List[str]
) -> tuple[Optional[str], str]:
    """从 simple API 选版本最新且平台兼容的 wheel。

    Returns:
        (下载 URL, 文件名)；无兼容 wheel → (None, "")
    """
    import re
    import urllib.request

    ver_cls = _version_cls()

    url = f"{index}/{pkg}/"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    files = data.get("files", [])
    best = None  # (Version, tag_rank, url, filename)
    for f in files:
        fn = f.get("filename", "")
        if not fn.endswith(".whl"):
            continue
        m = re.match(r"^[^-]+-[^-]+-[^-]+\.whl$", fn)
        if not m:
            continue
        parts = fn[:-4].split("-")
        if len(parts) < 3:
            continue
        ver_str, tag = parts[1], parts[-1]
        try:
            ver = ver_cls(ver_str)
        except Exception:
            continue
        if want_ver and not _ver_satisfies(ver_cls, ver_str, want_ver):
            continue
        rank = next((i for i, pref in enumerate(tag_prefs) if tag.startswith(pref)), None)
        if rank is None:
            continue
        if best is None or (ver, -rank) > (best[0], -best[1]):
            best = (ver, rank, f["url"], fn)
    if best is None:
        return None, ""
    return best[2], best[3]


def _version_cls():
    """packaging.version.Version（缺 packaging 时退化为字符串比较类）。"""
    try:
        from packaging.version import Version as _V

        return _V
    except ImportError:

        class _StrVer(str):
            # 字符串序近似版本序（仅排序用途，不精确不拦截）

            def __lt__(self, other):
                return str.__lt__(self, other)

            def __gt__(self, other):
                return str.__gt__(self, other)

            def __ge__(self, other):
                return not self.__lt__(other)

            def __eq__(self, other):
                return str.__eq__(self, other)

        return _StrVer


def _ver_satisfies(ver_cls, ver_str: str, want: str) -> bool:
    """宽松版本满足判断（wheel 回退路径专用，仅支持 == / >= 语义）。"""
    try:
        if want.startswith("=="):
            return ver_cls(ver_str) == ver_cls(want[2:])
        if want.startswith(">="):
            return ver_cls(ver_str) >= ver_cls(want[2:])
    except Exception:
        pass
    return True  # 无法解析的约束不拦（宁可多试）


def tempfile_path(filename: str):
    """wheel 临时文件上下文（退出自动删除）。"""
    import tempfile

    class _Ctx:
        def __init__(self):
            self._fd = tempfile.NamedTemporaryFile(
                suffix=".whl", prefix="drifox-dep-", delete=False
            )
            self.name = self._fd.name

        def __enter__(self):
            return self._fd

        def __exit__(self, *exc):
            try:
                self._fd.close()
                Path(self.name).unlink(missing_ok=True)
            except Exception:
                pass

    return _Ctx()
