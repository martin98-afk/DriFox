# -*- coding: utf-8 -*-
"""
AutoLoop 配置数据类
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AutoLoopConfig:
    """AutoLoop 循环配置"""
    max_iterations: int = 50
    max_tokens: int = 5000000
    max_duration_minutes: int = 120
    completion_signal: str = "MISSION_COMPLETE"
    completion_threshold: int = 3
    project_path: str = ""
    notes_file: str = "SHARED_TASK_NOTES.md"
    logs_dir: str = ".autoloop/logs"
    task_prompt: str = ""

    def to_dict(self) -> dict:
        return {
            "max_iterations": self.max_iterations,
            "max_tokens": self.max_tokens,
            "max_duration_minutes": self.max_duration_minutes,
            "completion_signal": self.completion_signal,
            "completion_threshold": self.completion_threshold,
            "project_path": self.project_path,
            "notes_file": self.notes_file,
            "logs_dir": self.logs_dir,
            "task_prompt": self.task_prompt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutoLoopConfig":
        return cls(
            max_iterations=d.get("max_iterations", 50),
            max_tokens=d.get("max_tokens", 5000000),
            max_duration_minutes=d.get("max_duration_minutes", 120),
            completion_signal=d.get("completion_signal", "MISSION_COMPLETE"),
            completion_threshold=d.get("completion_threshold", 3),
            project_path=d.get("project_path", ""),
            notes_file=d.get("notes_file", "SHARED_TASK_NOTES.md"),
            logs_dir=d.get("logs_dir", ".autoloop/logs"),
            task_prompt=d.get("task_prompt", ""),
        )
