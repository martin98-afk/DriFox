# -*- coding: utf-8 -*-
"""
文件操作记录器 - 支持撤销功能

用于记录文件操作并在需要时回滚
"""

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

# 预编译文件名清理正则
_SANITIZE_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*]')

from app.core.store import SessionStore


@dataclass
class RollbackResult:
    """回滚结果"""
    success_count: int = 0
    failed_count: int = 0
    failed_files: List[str] = None

    def __post_init__(self):
        if self.failed_files is None:
            self.failed_files = []


class FileOperationRecorder:
    """
    文件操作记录器

    在文件操作前备份，记录操作信息，支持撤销回滚
    """

    # 支持记录的文件操作类型：注册表"文件写入"分组（write/edit/multi_edit，插件声明）
    # 主程序不写死工具名——新写工具注册到该 group 即自动纳入备份跟踪。
    TRACKED_OPERATION_GROUP = "文件写入"

    def __init__(self, session_store: Optional[SessionStore] = None):
        self._session_store = session_store or SessionStore.get_instance()
        from app.utils.utils import get_app_data_dir
        self._backup_base_dir = get_app_data_dir() / "backups"

    def is_tracked_operation(self, tool_name: str) -> bool:
        """判断是否为需要记录的操作（registry 分组驱动）"""
        try:
            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance().get(tool_name)
            if reg is not None:
                return reg.group == self.TRACKED_OPERATION_GROUP
        except Exception:
            pass
        return False

    # 编辑后备份文件的后缀
    AFTER_BACKUP_SUFFIX = ".after.bak"

    def record_operation(self, session_id: str, call_id: str,
                        tool_name: str, file_path: str) -> Optional[str]:
        """
        记录文件操作前先备份

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID
            tool_name: 工具名称
            file_path: 目标文件路径

        Returns:
            str: 备份文件路径，失败返回 None
        """


        if not self.is_tracked_operation(tool_name):
            logger.debug(f"[FileRecorder] 工具不在追踪列表: {tool_name}")
            return None

        try:
            resolved_path = Path(file_path).resolve()


            # 如果文件不存在，可能是新建的文件，创建一个空备份文件用于差异比较
            file_existed = resolved_path.exists()
            if not file_existed:
                # 创建备份目录
                backup_dir = self._backup_base_dir / session_id
                backup_dir.mkdir(parents=True, exist_ok=True)

                # 生成备份文件名
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = self._sanitize_filename(resolved_path.name)
                call_prefix = call_id[:8] if len(call_id) >= 8 else call_id
                backup_name = f"{safe_name}_{timestamp}_{call_prefix}.bak"
                backup_path = backup_dir / backup_name

                # 创建一个空的备份文件
                backup_path.touch()
                logger.info(f"[FileRecorder] 新文件已创建空备份: {file_path} -> {backup_path}")

                # 记录到数据库
                self._session_store.record_file_operation(
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    file_path=str(resolved_path),
                    backup_path=str(backup_path)
                )

                return str(backup_path)

            # 创建备份目录
            backup_dir = self._backup_base_dir / session_id
            backup_dir.mkdir(parents=True, exist_ok=True)

            # 生成备份文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = self._sanitize_filename(resolved_path.name)
            call_prefix = call_id[:8] if len(call_id) >= 8 else call_id
            backup_name = f"{safe_name}_{timestamp}_{call_prefix}.bak"
            backup_path = backup_dir / backup_name

            # 复制文件到备份
            shutil.copy2(resolved_path, backup_path)
            logger.info(f"[FileRecorder] 已备份: {file_path} -> {backup_path}")

            # 记录到数据库

            self._session_store.record_file_operation(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                file_path=str(resolved_path),
                backup_path=str(backup_path)
            )


            return str(backup_path)

        except Exception as e:
            logger.error(f"[FileRecorder] 备份失败: {e}")

            return None

    def record_after_operation(self, session_id: str, call_id: str,
                               tool_name: str, file_path: str) -> Optional[str]:
        """
        记录文件操作后的备份（用于差异对比）

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID
            tool_name: 工具名称
            file_path: 目标文件路径

        Returns:
            str: 编辑后备份文件路径，失败返回 None
        """
        if not self.is_tracked_operation(tool_name):
            return None

        try:
            resolved_path = Path(file_path).resolve()

            # 获取对应的备份路径
            backup_path = self._get_backup_path(session_id, call_id)
            if not backup_path:
                logger.warning(f"[FileRecorder] 未找到对应的备份路径: session={session_id}, call={call_id}")
                return None

            backup_file = Path(backup_path)
            # 生成编辑后备份路径：xxx.bak -> xxx.after.bak
            after_backup_path = backup_file.with_suffix('.after.bak')

            # 如果文件不存在（被删除），创建空文件
            if not resolved_path.exists():
                after_backup_path.touch()
            else:
                shutil.copy2(resolved_path, after_backup_path)

            logger.info(f"[FileRecorder] 已备份编辑后: {file_path} -> {after_backup_path}")
            return str(after_backup_path)

        except Exception as e:
            logger.error(f"[FileRecorder] 编辑后备份失败: {e}")
            return None

    def _get_backup_path(self, session_id: str, call_id: str) -> Optional[str]:
        """根据 session_id 和 call_id 获取备份路径"""
        operations = self._session_store.get_file_operations_by_call_id(session_id, call_id)
        if operations:
            return operations[0].get("backup_path")
        return None

    def _get_after_backup_path(self, backup_path: str) -> str:
        """根据备份路径获取编辑后备份路径"""
        return str(Path(backup_path).with_suffix(self.AFTER_BACKUP_SUFFIX))

    def cleanup_on_failure(self, session_id: str, call_id: str):
        """
        编辑失败时清理备份文件

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID
        """
        backup_path = self._get_backup_path(session_id, call_id)
        if backup_path:
            self._cleanup_backup_files(backup_path)
            # 同时清理数据库中的记录
            self._session_store.remove_file_operation(session_id, call_id)
            logger.info(f"[FileRecorder] 已清理失败操作的备份: {backup_path}")

    def _cleanup_backup_files(self, backup_path: str):
        """清理备份文件及其对应的编辑后备份"""
        try:
            backup_file = Path(backup_path)
            # 删除主备份文件
            if backup_file.exists():
                backup_file.unlink()
                logger.debug(f"[FileRecorder] 已删除备份: {backup_file}")

            # 删除编辑后备份文件
            after_backup = self._get_after_backup_path(backup_path)
            after_file = Path(after_backup)
            if after_file.exists():
                after_file.unlink()
                logger.debug(f"[FileRecorder] 已删除编辑后备份: {after_file}")
        except Exception as e:
            logger.warning(f"[FileRecorder] 清理备份失败: {e}")

    def get_operations_for_preview(self, session_id: str, call_id: str) -> List[Dict]:
        """
        获取指定 call_id 的操作记录

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID

        Returns:
            List[Dict]: 操作列表
        """

        operations = self._session_store.get_file_operations_by_call_id(session_id, call_id)

        return operations

    def get_all_operations_for_session(self, session_id: str) -> List[Dict]:
        """获取指定会话的所有文件操作"""

        return self._session_store.get_all_file_operations(session_id)

    def rollback_operations(self, operations: List[Dict]) -> RollbackResult:
        """
        回滚一组操作

        Args:
            operations: 操作列表（应按时间正序传入）

        Returns:
            RollbackResult: 回滚结果
        """
        result = RollbackResult()

        # 逆序回滚（后执行的操作先回滚）
        for op in reversed(operations):
            try:
                file_path = op.get("file_path")
                backup_path = op.get("backup_path")

                if not file_path:
                    continue

                # 检查备份文件是否存在
                if not backup_path or not Path(backup_path).exists():
                    logger.warning(f"[FileRecorder] 备份文件不存在: {backup_path}")
                    result.failed_count += 1
                    result.failed_files.append(file_path)
                    continue

                backup_file = Path(backup_path)

                # 检查是否是新建文件（备份文件为空）
                is_new_file = backup_file.stat().st_size == 0

                if is_new_file:
                    # 新建文件的回滚：删除创建的文件
                    resolved_path = Path(file_path)
                    if resolved_path.exists():
                        resolved_path.unlink()
                    backup_file.unlink()
                    result.success_count += 1
                    logger.info(f"[FileRecorder] 已删除新建的文件: {file_path}")
                else:
                    # 恢复备份文件
                    resolved_path = Path(file_path)
                    shutil.copy2(backup_file, resolved_path)

                    # 删除备份文件（包括编辑后备份）
                    self._cleanup_backup_files(backup_path)

                    result.success_count += 1
                    logger.info(f"[FileRecorder] 已回滚: {file_path}")

            except FileNotFoundError:
                # 文件已被外部删除，跳过
                result.failed_count += 1
                result.failed_files.append(op.get("file_path", "unknown"))
                logger.warning(f"[FileRecorder] 文件已被删除，无法回滚: {op.get('file_path')}")
            except PermissionError as e:
                result.failed_count += 1
                result.failed_files.append(op.get("file_path", "unknown"))
                logger.error(f"[FileRecorder] 权限错误: {e}")
            except Exception as e:
                result.failed_count += 1
                result.failed_files.append(op.get("file_path", "unknown"))
                logger.error(f"[FileRecorder] 回滚失败: {e}")

        return result

    def clear_session(self, session_id: str) -> Tuple[int, List[str]]:
        """
        清空会话的所有文件操作和备份

        Args:
            session_id: 会话 ID

        Returns:
            Tuple[int, List[str]]: (删除的记录数, 备份文件路径列表)
        """
        deleted_count, backup_paths = self._session_store.clear_session_file_operations(session_id)

        # 删除备份文件（包括编辑后备份）
        for backup_path in backup_paths:
            self._cleanup_backup_files(backup_path)

        # 删除备份目录（如果为空）
        backup_dir = self._backup_base_dir / session_id
        if backup_dir.exists() and not any(backup_dir.iterdir()):
            try:
                backup_dir.rmdir()
            except Exception:
                pass

        return deleted_count, backup_paths

    def _sanitize_filename(self, filename: str) -> str:
        """移除文件名中不合法的字符"""
        return _SANITIZE_FILENAME_PATTERN.sub("_", filename)


def enforce_backup_limit(backup_base_dir, limit_mb: float, exclude_names: tuple = ()) -> list:
    """按 mtime 先进先出清理备份，直到总量 <= limit_mb

    - `limit_mb <= 0` 表示**不限制，不做任何清理**（语义变更说明见下）
    - `limit_mb > 0`：超出上限时按 mtime 从旧到新删到上限内
    - `exclude_names`：顶层子目录名命中该元组的项整体跳过（用于分治配额，
      例如 FileRecorder 备份清理时排除 `deleted/` 删除快照目录）

    返回被删文件路径列表。模块级独立函数：设置页手动触发 / 启动期触发均可调用。

    ⚠ 语义变更（2026-09-21）：旧实现 `limit_mb <= 0` 会**清空全部备份**
    （因 `limit_bytes > 0` 判断被跳过，全部文件进删除列表）。这对用户是陷阱：
    想"设为 0 表示不限制"，结果备份被全删。现统一为"0 = 不限"，与 Docker
    `--memory=0`、多数限流配置的 0 语义一致；且不再依赖调用方自觉加
    `if limit > 0` 包壳（任何新调用方漏判都会全删备份 —— 现在的实现自处理）。
    """
    base = Path(backup_base_dir)
    if not base.exists():
        return []
    if float(limit_mb) <= 0:  # 0 = 不限：不做任何清理
        return []
    try:
        files = [f for f in base.rglob("*") if f.is_file()]
    except OSError as e:
        logger.warning(f"[FileRecorder] 备份目录扫描失败: {e}")
        return []
    if exclude_names:
        excluded = tuple(str(n) for n in exclude_names)
        files = [f for f in files if not _has_excluded_ancestor(f, base, excluded)]
    if not files:
        return []

    limit_bytes = int(float(limit_mb) * 1024 * 1024)
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            continue
    if limit_bytes > 0 and total <= limit_bytes:
        return []

    removed: list = []
    for f in sorted(files, key=lambda x: x.stat().st_mtime):
        if limit_bytes > 0 and total <= limit_bytes:
            break
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            removed.append(f)
        except OSError as e:
            logger.warning(f"[FileRecorder] 备份清理失败: {f} ({e})")
    if removed:
        logger.info(f"[FileRecorder] 备份 FIFO 清理 {len(removed)} 个文件，剩余 {total / 1024 / 1024:.1f} MB")
    return removed


def cleanup_backups_partitioned(backups_dir, limit_mb: float) -> dict:
    """分治清理两类备份（启动期与 UI「立即清理」共用同一入口）

    两类备份**分治配额**，不可改回统算：
    - FileRecorder 备份（每次编辑一条，高频产物）：配额 = limit_mb，排除 deleted/
    - 删除保护快照（用户误删后唯一恢复手段）：配额 = max(100, limit_mb // 4)
      下限 100MB 是保底，避免总配额很小时快照被挤成 0

    共享一个 FIFO 配额会让日常编辑把快照挤干净 —— 安全功能静默失效，不可接受。

    Args:
        backups_dir: 备份根目录（其下 `deleted/` 为删除快照）
        limit_mb: 总配额；<= 0 表示不限（两个分支都不清理）

    Returns:
        {"file_backups_removed": [...], "snapshots_removed": [...], "snapshot_limit_mb": int}
    """
    base = Path(backups_dir)
    limit = int(float(limit_mb or 0))
    # 快照配额派生规则：总配额 1/4，下限 100MB
    snapshot_limit = max(100, limit // 4) if limit > 0 else 0
    removed_files = enforce_backup_limit(base, limit, exclude_names=("deleted",))
    removed_snaps = enforce_backup_limit(base / "deleted", snapshot_limit)
    return {
        "file_backups_removed": removed_files,
        "snapshots_removed": removed_snaps,
        "snapshot_limit_mb": snapshot_limit,
    }


def _has_excluded_ancestor(path: Path, base: Path, excluded: tuple) -> bool:
    """判断 path 是否位于 base 下某个被排除的顶层子目录内

    只看**顶层段**（base 的直接子目录名），不做任意层级匹配——否则
    `a/deleted/b.bak` 这类深层同名目录也会被误排除。
    """
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    parts = rel.parts
    return bool(parts) and parts[0] in excluded
