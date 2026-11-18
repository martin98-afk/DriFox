/**
 * ToolCard：工具卡 streaming/done 两态（T9：tool.* 事件驱动独立通道，不进 markdown 重算路径）。
 * 数据源：streamStore.tools（流式期）/ 定型 blocks 的 tool_result（定型后）。
 */

import { memo } from "react";
import type { ToolCard as ToolCardData } from "../../../stores/streamStore";

interface Props {
  tool: ToolCardData;
}

function summarize(value: unknown, max = 600): string {
  let text: string;
  try {
    text = typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
  } catch {
    text = String(value);
  }
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export const ToolCard = memo(function ToolCard({ tool }: Props) {
  const running = tool.phase === "running";
  return (
    <div className={`tool-card${running ? " is-running" : " is-done"}`}>
      <div className="tool-card-head">
        <span className={`tool-card-state-dot${running ? " is-spin" : ""}`} aria-hidden />
        <span className="tool-card-name">{tool.toolName}</span>
        <span className="tool-card-phase">{running ? "运行中" : "完成"}</span>
      </div>
      {(tool.argsText || tool.arguments != null) && (
        <details className="tool-card-args" open={running}>
          <summary>参数</summary>
          <pre>{running ? summarize(tool.argsText || tool.arguments) : summarize(tool.arguments)}</pre>
        </details>
      )}
      {!running && (
        <details className="tool-card-result" open>
          <summary>结果</summary>
          <pre>{summarize(tool.result)}</pre>
        </details>
      )}
    </div>
  );
});
