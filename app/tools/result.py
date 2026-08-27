from typing import Any, Optional


class ToolResult:
    def __init__(self, success: bool, content: Any = None, error: Optional[str] = None,
                 diff: Optional[str] = None, anchors: Optional[str] = None,
                 echarts: Optional[str] = None, image_data: Optional[dict] = None,
                 todos: Optional[list] = None, data: Optional[dict] = None):
        self.success = success
        self.content = content
        self.error = error
        self.diff = diff      # diff 字符串，供 UI inline diff 展示
        self.anchors = anchors  # 新锚点块，供 LLM 链式编辑
        self.echarts = echarts  # ECharts 图表 JSON，供 UI 渲染 DAG 图
        self.image_data = image_data  # 图片数据: {"mime": str, "data": str(base64)}
        self.todos = todos  # 待办列表（工具插件 todowrite/todoread 回传，UI 卡片联动读取）
        self.data = data    # 结构化回传通道（通用）：设置卡动作按钮等 UI 编排消费

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
        if self.image_data:
            d["image_data"] = self.image_data
        if self.todos:
            d["todos"] = self.todos
        if self.data:
            d["data"] = self.data
        return d

    def __str__(self):
        if self.success:
            return str(self.content)
        return f"[Error] {self.error}"

    def is_success(self) -> bool:
        return self.success
