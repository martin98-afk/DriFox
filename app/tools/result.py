from typing import Any, Optional


class ToolResult:
    def __init__(self, success: bool, content: Any = None, error: Optional[str] = None,
                 diff: Optional[str] = None, anchors: Optional[str] = None,
                 echarts: Optional[str] = None, image_data: Optional[dict] = None,
                 todos: Optional[list] = None, data: Optional[dict] = None,
                 artifacts: Optional[list] = None, end_turn: bool = False):
        self.success = success
        self.content = content
        self.error = error
        self.diff = diff      # diff 字符串，供 UI inline diff 展示
        self.anchors = anchors  # 新锚点块，供 LLM 链式编辑
        self.echarts = echarts  # ECharts 图表 JSON，供 UI 渲染 DAG 图
        self.image_data = image_data  # 图片数据: {"mime": str, "data": str(base64)}
        self.todos = todos  # 待办列表（工具插件 todowrite/todoread 回传，UI 卡片联动读取）
        self.data = data    # 结构化回传通道（通用）：设置卡动作按钮等 UI 编排消费
        self.artifacts = artifacts  # 成果条目列表（工具产出的文件/媒体，卡片成果栏数据源）
        # 本轮收尾请求（插件通用契约）：True = 本次工具返回后不再发起下一轮 API 调用。
        # 适用场景：交接/移交类工具写完就必须换会话、批处理工具已跑完整批、
        # 脚手架工具建完项目后没必要再让模型续话。主 worker 与子智能体 worker 同一语义。
        self.end_turn = end_turn

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
        if self.artifacts:
            d["artifacts"] = self.artifacts
        if self.end_turn:
            d["end_turn"] = self.end_turn
        return d

    def __str__(self):
        if self.success:
            return str(self.content)
        return f"[Error] {self.error}"

    def is_success(self) -> bool:
        return self.success
