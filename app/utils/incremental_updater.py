# -*- coding: utf-8 -*-
"""
增量更新引擎模块

实现文件级增量更新的核心逻辑：
- 下载远程 manifest.json，与本地对比找出差异文件
- 下载 files.zip，提取差异文件到临时目录
- 非锁定文件直接替换，锁定文件通过 updater 脚本处理
- 支持失败时回退到全量更新

依赖:
    - httpx: 异步 HTTP 下载
    - PyQt5.QtCore: QThread + pyqtSignal
"""

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from loguru import logger
from PyQt5.QtCore import QThread, pyqtSignal

BUF_SIZE = 64 * 1024  # SHA256 流式计算缓冲区


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class DiffReport:
    """manifest 对比差异报告。"""

    added: list[str] = field(default_factory=list)  # 远程有、本地无
    modified: list[str] = field(default_factory=list)  # SHA256 不同
    deleted: list[str] = field(default_factory=list)  # 本地有、远程无
    total_download_size: int = 0  # 需下载总字节数

    @property
    def total_files(self) -> int:
        return len(self.added) + len(self.modified)

    @property
    def is_empty(self) -> bool:
        return self.total_files == 0


# ============================================================================
# 增量更新引擎
# ============================================================================


class IncrementalUpdater(QThread):
    """文件级增量更新引擎，在后台线程执行。

    信号:
        stage_changed(str): 阶段变化
            "comparing"  — 正在对比 manifest
            "downloading" — 正在下载 files.zip
            "extracting"  — 正在解压差异文件
            "installing"  — 正在替换文件
            "done"        — 完成

        progress(int, int, int, int):
            current_files, total_files, downloaded_bytes, total_bytes

        error(str): 错误消息

        finished(bool, list[str]):
            success=True/False, locked_files=需要 stub 处理的文件列表
    """

    stage_changed = pyqtSignal(str)
    progress = pyqtSignal(int, int, int, int)  # cur_files, total_files, dl_bytes, total_bytes
    error = pyqtSignal(str)
    finished = pyqtSignal(bool, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_canceled = False

        # 由调用方在 start() 前设置
        self.manifest_url: str = ""
        self.files_zip_url: str = ""
        self.install_dir: str = ""
        self.token: str = ""
        self.remote_version: str = ""

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """取消当前更新（下次启动时可恢复）。"""
        self._is_canceled = True

    # ------------------------------------------------------------------
    # 线程主入口
    # ------------------------------------------------------------------

    def run(self) -> None:
        """后台线程主逻辑。"""
        try:
            self._do_update()
        except Exception as e:
            logger.exception(f"[IncrementalUpdater] 更新异常: {e}")
            self.error.emit(str(e))
            self.finished.emit(False, [])

    def _do_update(self) -> None:
        """执行完整的增量更新流程。"""
        # ① 下载远程 manifest
        self.stage_changed.emit("comparing")
        remote_manifest = self._download_json(self.manifest_url)
        if remote_manifest is None:
            self._fallback_full_update("无法下载远程文件清单")
            return

        # ② 加载/生成本地 manifest
        local_manifest = self._load_local_manifest()

        # ③ 版本跨度保护：跳 3+ minor 或跨大版本 → 强制全量
        if not self._check_version_span(local_manifest, remote_manifest):
            self._fallback_full_update("版本跨度太大，建议全量更新")
            return

        # ④ 对比差异
        diff = self._compare_manifests(local_manifest, remote_manifest)
        if diff.is_empty:
            logger.info("[IncrementalUpdater] 本地文件已是最新，无需下载")
            self.stage_changed.emit("done")
            self.finished.emit(True, [])
            return

        logger.info(
            f"[IncrementalUpdater] 差异: +{len(diff.added)} 新增, "
            f"~{len(diff.modified)} 修改, "
            f"-{len(diff.deleted)} 删除, "
            f"共 {diff.total_download_size / 1024 / 1024:.1f} MB"
        )

        # ④ 下载 files.zip
        self.stage_changed.emit("downloading")
        temp_dir = self._create_temp_dir()
        zip_path = os.path.join(temp_dir, "files.zip")

        if not self._download_zip(self.files_zip_url, zip_path, diff.total_download_size):
            self._fallback_full_update("增量包下载失败")
            return

        # ⑤ 解压差异文件
        self.stage_changed.emit("extracting")
        extract_dir = os.path.join(temp_dir, "files")
        os.makedirs(extract_dir, exist_ok=True)
        self._extract_diff_files(zip_path, extract_dir, diff)

        # ⑥ 安装到目标目录
        self.stage_changed.emit("installing")
        locked_files = self._install_files(extract_dir, self.install_dir, diff)
        # 清理已解压的 zip（不再需要）
        os.remove(zip_path)

        # ⑦ 处理锁定文件
        if locked_files:
            # 保存 remote manifest 供 updater 脚本使用
            manifest_src = os.path.join(extract_dir, "_remote_manifest.json")
            with open(manifest_src, "w", encoding="utf-8") as f:
                json.dump(remote_manifest, f, ensure_ascii=False, indent=2)
            self._handle_locked_files(locked_files, extract_dir, self.install_dir)
            # 有锁定文件 → 需要重启才能完成
            self.stage_changed.emit("done")
            self.finished.emit(True, locked_files)
        else:
            # 无锁定文件 → 完全原地完成
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._save_local_manifest(remote_manifest)
            self.stage_changed.emit("done")
            self.finished.emit(True, [])

    # ------------------------------------------------------------------
    # Manifest 相关
    # ------------------------------------------------------------------

    def _download_json(self, url: str) -> dict | None:
        """下载 JSON 资源（manifest.json）。"""
        headers = {"Authorization": self.token} if self.token else {}
        try:
            # 使用同步 httpx，因为已经在 QThread 中
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"[IncrementalUpdater] 下载 manifest 失败: HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.warning(f"[IncrementalUpdater] 下载 manifest 异常: {e}")
            return None

    def _load_local_manifest(self) -> dict:
        """加载本地 manifest.json，如不存在则扫描安装目录生成。"""
        manifest_path = os.path.join(self.install_dir, "manifest.json")

        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.warning("[IncrementalUpdater] 本地 manifest 损坏，重新扫描")

        # 回退：扫描安装目录
        logger.info("[IncrementalUpdater] 本地无 manifest，全量扫描安装目录")
        return self._scan_local_files()

    def _save_local_manifest(self, manifest: dict) -> None:
        """保存 manifest 到安装目录，供下次增量对比使用。"""
        manifest_path = os.path.join(self.install_dir, "manifest.json")
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[IncrementalUpdater] 保存本地 manifest 失败: {e}")

    @staticmethod
    def _parse_version(version_str: str) -> tuple[int, int, int]:
        """解析版本号 v0.3.8 → (0, 3, 8)。"""
        import re

        v = version_str.lstrip("v").strip()
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", v)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        return 0, 0, 0

    def _check_version_span(self, local: dict, remote: dict) -> bool:
        """检查版本跨度：跨大版本或跳 3+ minor → 返回 False（建议全量）。

        阈值: major 不同 或 minor 差距 >= 3 → 强制全量。
        首次安装（无本地 manifest）→ 允许增量。
        """
        local_ver = local.get("version", "")
        remote_ver = remote.get("version", self.remote_version)

        # 首次安装，本地无版本号 → 允许增量
        if local_ver == "local" or not local_ver:
            return True

        l_major, l_minor, _ = self._parse_version(local_ver)
        r_major, r_minor, _ = self._parse_version(remote_ver)

        if l_major != r_major:
            logger.info(
                f"[IncrementalUpdater] 跨大版本 {local_ver} → {remote_ver}，强制全量"
            )
            return False

        span = abs(r_minor - l_minor)
        if span >= 3:
            logger.info(
                f"[IncrementalUpdater] minor 跨度 {span} ({local_ver} → {remote_ver})，强制全量"
            )
            return False

        return True

    def _scan_local_files(self) -> dict:
        """扫描安装目录，生成本地文件清单。跳过 __pycache__ 等。"""
        files = {}
        total_size = 0
        skip_dirs = {"__pycache__", ".mypy_cache"}

        for dirpath, dirnames, filenames in os.walk(self.install_dir):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]

            for filename in filenames:
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, self.install_dir).replace("\\", "/")
                try:
                    size = os.path.getsize(full)
                    sha = _sha256_file(full)
                    files[rel] = {"sha256": sha, "size": size}
                    total_size += size
                except OSError:
                    continue

        return {
            "version": "local",
            "file_count": len(files),
            "total_size": total_size,
            "files": dict(sorted(files.items())),
        }

    # ------------------------------------------------------------------
    # Diff 对比
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_path(path: str) -> str | None:
        """确保路径安全：拒绝绝对路径和 .. 遍历。"""
        # 绝对路径：os.path.isabs + Unix/Windows 前缀兜底
        if os.path.isabs(path) or path.startswith("/") or path.startswith("\\"):
            return None

        # 检查路径组件中是否包含 ..
        parts = path.replace("\\", "/").split("/")
        for part in parts:
            if part == "..":
                return None

        # 规范化确认
        normalized = os.path.normpath(path).replace("\\", "/")
        if os.path.isabs(normalized):
            return None

        return normalized

    def _compare_manifests(self, local: dict, remote: dict) -> DiffReport:
        """对比本地和远程 manifest，生成差异报告。"""
        local_files: dict = local.get("files", {})
        remote_files: dict = remote.get("files", {})
        diff = DiffReport()

        for path, info in remote_files.items():
            safe = self._sanitize_path(path)
            if safe is None:
                logger.warning(f"[IncrementalUpdater] 跳过不安全路径: {path}")
                continue
            if safe not in local_files:
                diff.added.append(safe)
                diff.total_download_size += info.get("size", 0)
            elif local_files[safe].get("sha256") != info.get("sha256"):
                diff.modified.append(safe)
                diff.total_download_size += info.get("size", 0)

        for path in local_files:
            if path not in remote_files:
                diff.deleted.append(path)

        return diff

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _download_zip(self, url: str, dest: str, expected_size: int) -> bool:
        """下载 files.zip，带进度报告。"""
        headers = {"Authorization": self.token} if self.token else {}

        try:
            with httpx.Client(timeout=600.0, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.warning(
                            f"[IncrementalUpdater] 下载 zip 失败: HTTP {resp.status_code}"
                        )
                        return False

                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0

                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=BUF_SIZE):
                            if self._is_canceled:
                                return False
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(0, 0, downloaded, total)

            return True

        except Exception as e:
            logger.warning(f"[IncrementalUpdater] 下载 zip 异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 解压
    # ------------------------------------------------------------------

    def _extract_diff_files(self, zip_path: str, dest: str, diff: DiffReport) -> None:
        """从 files.zip 中只解压差异文件到目标目录。

        zip 内路径示例: Drifox/_internal/app/... （以 onedir 根名开头）
        需要去除第一级前缀后对应到安装目录。
        """
        needed = set(diff.added + diff.modified)
        if not needed:
            return

        with zipfile.ZipFile(zip_path, "r") as zf:
            # 检测 zip 内是否有顶层目录前缀
            prefix = self._detect_zip_prefix(zf)

            total = len(needed)
            done = 0
            for member in zf.infolist():
                if member.is_dir():
                    continue

                # 去除前缀得到相对路径
                rel = member.filename
                if prefix:
                    if not rel.startswith(prefix):
                        continue
                    rel = rel[len(prefix):]

                if rel not in needed:
                    continue

                # 解压
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                done += 1
                self.progress.emit(done, total, 0, 0)

    @staticmethod
    def _detect_zip_prefix(zf: zipfile.ZipFile) -> str:
        """检测 zip 包内顶层目录前缀。

        zip 结构通常是 Drifox/Drifox.exe, Drifox/_internal/...
        返回前缀如 "Drifox/" 或 ""。
        """
        names = [m.filename for m in zf.infolist() if not m.is_dir()]
        if not names:
            return ""

        # 取第一个文件的路径，提取其第一级目录
        first = names[0]
        parts = first.replace("\\", "/").split("/")
        if len(parts) > 1:
            prefix = parts[0] + "/"
            # 验证：至少 80% 的文件都以该前缀开头
            count = sum(1 for n in names if n.startswith(prefix))
            if count / len(names) > 0.8:
                return prefix

        return ""

    # ------------------------------------------------------------------
    # 文件安装
    # ------------------------------------------------------------------

    def _install_files(
        self, src_dir: str, dst_dir: str, diff: DiffReport
    ) -> list[str]:
        """将差异文件从临时目录安装到目标目录。

        策略:
            - 先用临时文件名写入 → rename（同分区原子操作）
            - 无法 rename 的文件（跨分区或锁定）→ 记录到 locked_files
        """
        locked_files: list[str] = []
        all_files = diff.added + diff.modified

        for i, rel_path in enumerate(all_files):
            if self._is_canceled:
                break

            src = os.path.join(src_dir, rel_path)
            dst = os.path.join(dst_dir, rel_path)
            dst_tmp = dst + ".new"

            if not os.path.isfile(src):
                logger.warning(f"[IncrementalUpdater] 源文件缺失: {rel_path}")
                continue

            # 确保目标目录存在
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            # 先复制到临时文件
            try:
                shutil.copy2(src, dst_tmp)
            except OSError as e:
                logger.warning(f"[IncrementalUpdater] 复制失败: {rel_path} ({e})")
                locked_files.append(rel_path)
                continue

            # 原子替换
            try:
                os.replace(dst_tmp, dst)
            except OSError:
                # 文件被锁定（如 Drifox.exe 正在运行）
                locked_files.append(rel_path)
                # 保留 .new 文件供 updater 使用
                # 注意：dst_tmp 就是 .new，保留即可

            self.progress.emit(i + 1, len(all_files), 0, 0)

        # 清理旧文件（被删除的文件）
        for rel_path in diff.deleted:
            target = os.path.join(dst_dir, rel_path)
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

        return locked_files

    # ------------------------------------------------------------------
    # 锁定文件处理
    # ------------------------------------------------------------------

    def _handle_locked_files(
        self, locked_files: list[str], src_dir: str, dst_dir: str
    ) -> None:
        """为锁定文件生成 updater 脚本并启动。

        Windows: 生成 updater.bat
        macOS:   生成 updater.sh
        """
        system = platform.system().lower()

        if system == "windows":
            self._generate_windows_updater(locked_files, src_dir, dst_dir)
        elif system == "darwin":
            self._generate_macos_updater(locked_files, src_dir, dst_dir)
        else:
            # Linux: 同样用 shell 脚本
            self._generate_linux_updater(locked_files, src_dir, dst_dir)

    def _generate_windows_updater(
        self, locked_files: list[str], src_dir: str, dst_dir: str
    ) -> None:
        """生成 Windows batch 脚本。"""
        script_path = os.path.join(dst_dir, "_updater.bat")

        lines = [
            "@echo off",
            "chcp 65001 >nul",
            f'cd /d "{dst_dir}"',
            "",
            ":: 等待主进程退出（最多 30 秒）",
            "set /a count=0",
            ":wait",
            "timeout /t 1 /nobreak >nul",
            "set /a count+=1",
            "if %count% gtr 30 goto force",
            "tasklist /fi \"IMAGENAME eq Drifox.exe\" 2>nul | find /i \"Drifox.exe\" >nul",
            "if not errorlevel 1 goto wait",
            "goto replace",
            "",
            ":force",
            "echo 等待超时，强制替换...",
            "",
            ":replace",
        ]

        # 逐个替换锁定文件
        for rel_path in locked_files:
            src = os.path.join(src_dir, rel_path)
            dst = os.path.join(dst_dir, rel_path)
            dst_new = dst + ".new"

            if os.path.isfile(dst_new):
                lines.append(f'echo 替换: {shlex.quote(rel_path)}')
                lines.append(f'copy /Y "{dst_new}" "{dst}" >nul')
                lines.append(f'del "{dst_new}" 2>nul')
            elif os.path.isfile(src):
                lines.append(f'echo 替换: {shlex.quote(rel_path)}')
                lines.append(f'copy /Y "{src}" "{dst}" >nul')

        # 清理临时目录
        lines.append(f'if exist "{src_dir}" rmdir /s /q "{src_dir}"')

        # 保存本地 manifest
        manifest_dst = os.path.join(dst_dir, "manifest.json")
        manifest_src = os.path.join(src_dir, "_remote_manifest.json")
        lines.append(f'if exist "{manifest_src}" move /Y "{manifest_src}" "{manifest_dst}" >nul')

        # 重启主程序
        main_exe = os.path.join(dst_dir, "Drifox.exe")
        lines.append('echo 重启 Drifox...')
        lines.append(f'start "" "{main_exe}"')

        # 自毁
        lines.append("del \"%~f0\" >nul 2>&1")

        with open(script_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        # 启动脚本（detached，不等待）
        subprocess.Popen(
            ["cmd.exe", "/c", script_path],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS,
        )

        logger.info("[IncrementalUpdater] updater.bat 已生成并启动")

    def _generate_macos_updater(
        self, locked_files: list[str], src_dir: str, dst_dir: str
    ) -> None:
        """生成 macOS shell 脚本。"""
        script_path = os.path.join(dst_dir, "_updater.sh")

        lines = [
            "#!/bin/bash",
            "set -e",
            "",
            f'APP_PID=$(pgrep -f "{dst_dir}")',
            'if [ -n "$APP_PID" ]; then',
            '    echo "等待进程退出..."',
            "    for i in $(seq 1 30); do",
            '        if ! kill -0 "$APP_PID" 2>/dev/null; then',
            "            break",
            "        fi",
            "        sleep 1",
            "    done",
            "fi",
            "",
        ]

        for rel_path in locked_files:
            src = os.path.join(src_dir, rel_path)
            dst = os.path.join(dst_dir, rel_path)
            dst_new = dst + ".new"

            if os.path.isfile(dst_new):
                lines.append(f'cp -f {shlex.quote(dst_new)} {shlex.quote(dst)} && rm -f {shlex.quote(dst_new)}')
            elif os.path.isfile(src):
                lines.append(f'cp -f {shlex.quote(src)} {shlex.quote(dst)}')

        # 清理
        lines.append(f'rm -rf {shlex.quote(src_dir)}')

        # 重启：使用 .app bundle 路径
        lines.append(f'open {shlex.quote(dst_dir)}')

        # 自毁
        lines.append(f'rm -f {shlex.quote(script_path)}')

        with open(script_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        os.chmod(script_path, 0o755)
        subprocess.Popen(["bash", script_path])

        logger.info("[IncrementalUpdater] updater.sh 已生成并启动")

    def _generate_linux_updater(
        self, locked_files: list[str], src_dir: str, dst_dir: str
    ) -> None:
        """Linux: 复用 macOS 逻辑（都是 shell）。"""
        self._generate_macos_updater(locked_files, src_dir, dst_dir)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _create_temp_dir(self) -> str:
        """在安装目录旁创建安全的临时目录。"""
        base = os.path.dirname(self.install_dir)
        return tempfile.mkdtemp(prefix="_update_", dir=base)

    def _fallback_full_update(self, reason: str) -> None:
        """增量更新失败，通知调用方走全量更新。"""
        logger.warning(f"[IncrementalUpdater] 增量更新失败: {reason}，回退全量更新")
        self.error.emit(f"增量更新失败({reason})，将使用全量更新")
        self.finished.emit(False, [])


# ============================================================================
# 工具函数
# ============================================================================


def _sha256_file(filepath: str) -> str:
    """流式计算文件 SHA256。"""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(BUF_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def generate_local_manifest(install_dir: str) -> dict:
    """公开工具：为当前安装目录生成 manifest（用于首次安装后初始化）。"""
    updater = IncrementalUpdater()
    updater.install_dir = install_dir
    manifest = updater._scan_local_files()
    updater._save_local_manifest(manifest)
    return manifest
