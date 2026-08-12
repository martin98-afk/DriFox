# -*- coding: utf-8 -*-
"""插件下载/更新记录 — 持久化到 .drifox/cache/marketplaces/records.json

记录安装（install）与更新（update）的成功/失败事件，供「代理」页
「下载 / 更新记录」区块展示；失败记录保存完整 plugin_meta，支持一键重试
（重试原动作：安装失败→重新安装，更新失败→重新更新）。

设计：
- 事件流式追加，最新在前；超过 MAX（100）条丢弃最旧
- 每条保存 action / name / success / error / time / meta（重试用）
- meta 可能含 _marketplace_source 等嵌套结构，与 market 数据同源可 JSON 序列化
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）"""
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
                app_support = Path(app_support_path) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


class MarketplaceRecords:
    """下载/更新记录存储：事件流式追加 + 上限裁剪 + 持久化"""

    # 保留上限（条）：超出丢弃最旧
    MAX = 100

    def __init__(self, file: Optional[Path] = None):
        self._file = file or (_drifox_dir() / "cache" / "marketplaces" / "records.json")
        self._records: List[Dict[str, Any]] = self._load()

    # ── 持久化 ──

    def _load(self) -> List[Dict[str, Any]]:
        """加载记录（损坏/缺失 → 空列表）"""
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data[: self.MAX]
        except Exception:
            pass
        return []

    def _save(self):
        """持久化（失败仅告警，不影响主流程）"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Marketplace] 下载/更新记录保存失败: {e}")

    # ── 读写 ──

    def add(
        self,
        action: str,
        name: str,
        success: bool,
        *,
        error: str = "",
        meta: Optional[dict] = None,
    ):
        """追加一条记录（最新在前），并裁剪到上限

        Args:
            action: "install" | "update"
            name: 插件名
            success: 是否成功
            error: 失败原因摘要（成功时可为空）
            meta: 插件元数据（失败记录保留供一键重试原动作）
        """
        record = {
            "action": action,
            "name": name,
            "success": bool(success),
            "error": error[:500],  # 截断防单条撑爆文件
            "time": time.time(),
            "meta": meta,
        }
        self._records.insert(0, record)
        if len(self._records) > self.MAX:
            self._records = self._records[: self.MAX]
        self._save()

    def get(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取记录（最新在前）；limit 截取前 N 条"""
        records = list(self._records)
        if limit is not None:
            records = records[:limit]
        return records

    def clear(self):
        """清空全部记录"""
        self._records = []
        self._save()


# ── 单例 ──

_instance: Optional[MarketplaceRecords] = None


def get_records() -> MarketplaceRecords:
    global _instance
    if _instance is None:
        _instance = MarketplaceRecords()
    return _instance
