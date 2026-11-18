/**
 * 消息块模型（T9 六类闭集）：定型消息 blocks 数组的类型与流式视图状态。
 * tool_streaming 走 tool.* 事件独立通道（streamStore.tools），不进本列表的 markdown 重算路径。
 */

export interface TextBlockData {
  type: "text";
  content: string;
}

export interface ReasoningBlockData {
  type: "reasoning";
  content: string;
}

export interface ToolStreamingBlockData {
  type: "tool_streaming";
  tool_call_id: string;
  tool_name: string;
}

export interface ToolResultBlockData {
  type: "tool_result";
  tool_call_id: string;
  tool_name: string;
  result: unknown;
}

export interface ImageBlockData {
  type: "image_url" | "input_image" | "image";
  url: string;
  alt?: string;
}

export interface CustomBlockData {
  type: "custom";
  custom_type: string;
  data: unknown;
}

export type MessageBlock =
  | TextBlockData
  | ReasoningBlockData
  | ToolStreamingBlockData
  | ToolResultBlockData
  | ImageBlockData
  | CustomBlockData;

/** chatLog 内的消息模型（定型 + 流式占位统一） */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  /** 用户消息正文（气泡文本；不跑 markdown 管线） */
  text?: string;
  /** 助手定型消息的标准 blocks */
  blocks?: MessageBlock[];
  /** meta 数据 */
  model?: string;
  elapsedS?: number;
  tokPerS?: number;
  ts: number;
}

/** 流式视图状态（useActiveView 输出，ChatArea 据此组 items） */
export interface StreamingView {
  sid: string;
  /** reasoning 已提交全量（live） */
  reasoning: string;
  /** 正文已固化段（250ms 静默升级后的 stable 文本，渲染走管线缓存） */
  settledText: string;
  /** 正文活动段（live 纯文本尾态） */
  activeText: string;
  /** 是否尚无任何 delta（骨架态） */
  awaitingFirstDelta: boolean;
}

/** 流式工具卡快照（与 streamStore.ToolCardData 同形，事件驱动低频走 props） */
export interface StreamingToolSnapshot {
  toolCallId: string;
  toolName: string;
  arguments: unknown;
  argsText: string;
  result: unknown;
  phase: "running" | "done";
}
