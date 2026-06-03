from typing import Any, Optional


class ToolResult:
    def __init__(self, success: bool, content: Any = None, error: Optional[str] = None,
                 diff: Optional[str] = None, anchors: Optional[str] = None,
                 echarts: Optional[str] = None):
        self.success = success
        self.content = content
        self.error = error
        self.diff = diff      # diff 字符串，供 UI inline diff 展示
        self.anchors = anchors  # 新锚点块，供 LLM 链式编辑
        self.echarts = echarts  # ECharts 图表 JSON，供 UI 渲染 DAG 图

    def to_dict(self) -> dict:
        d = {"success": self.success}
        if self.success:
            d["content"] = self.content
        else:
            d["error"] = self.error
        if self.diff:
            d["diff"] = self.diff
        if self.anchors:
            d["anchors"] = self.anchors
        if self.echarts:
            d["echarts"] = self.echarts
        return d

    def __str__(self):
        if self.success:
            return str(self.content)
        return f"[Error] {self.error}"

    def is_success(self) -> bool:
        return self.success
