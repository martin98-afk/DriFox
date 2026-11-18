/**
 * BlockRenderer（T9）：块渲染分发 + 流式命令式通道。
 *
 * 两条通道（T10 数据流走线）：
 * - stable/定型块：props（memo 浅比较，settled 块 content 稳定即跳过）+ 管线 LRU
 * - live 块：streamStore 订阅回调直接改 ref.textContent，绕过 React 重渲
 *
 * 修正③：tool_streaming 走 tool.* 事件独立通道（streamStore.tools），不进 markdown 重算路径。
 */

import { memo, useEffect, useRef } from "react";
import { useStreamStore } from "../../stores/streamStore";
import type { MessageBlock, StreamingToolSnapshot, StreamingView } from "./blocks";
import { TextBlock } from "./render/TextBlock";
import { ReasoningCard } from "./render/ReasoningCard";
import { ToolCard } from "./render/ToolCard";
import { ImageBlock } from "./render/ImageBlock";
import { PluginBlock } from "./render/PluginBlock";

// ---------- 定型块分发 ----------

function BlockItem({ block, home }: { block: MessageBlock; home: string }) {
  switch (block.type) {
    case "text":
      return <TextBlock content={block.content} home={home} />;
    case "reasoning":
      return <ReasoningCard content={block.content} />;
    case "tool_streaming":
      return (
        <ToolCard
          tool={{ toolCallId: block.tool_call_id, toolName: block.tool_name, arguments: null, argsText: "", result: null, phase: "running" }}
        />
      );
    case "tool_result":
      return (
        <ToolCard
          tool={{ toolCallId: block.tool_call_id, toolName: block.tool_name, arguments: null, argsText: "", result: block.result, phase: "done" }}
        />
      );
    case "image_url":
    case "input_image":
    case "image":
      return <ImageBlock url={block.url} alt={block.alt} />;
    case "custom":
      return <PluginBlock customType={block.custom_type} data={block.data} />;
  }
}

export const BlockRenderer = memo(function BlockRenderer({ blocks, home }: { blocks: MessageBlock[]; home: string }) {
  return (
    <div className="block-renderer">
      {blocks.map((b, i) => (
        <BlockItem key={`${home}:${i}`} block={b} home={home} />
      ))}
    </div>
  );
});

// ---------- live 通道组件 ----------

/**
 * 正文活动段：订阅 streamStore 直改 textContent（rAF flush 后每帧一次，绕过 React）。
 * settledLenRef 由 useStreamingMessage 维护（250ms 静默升级边界）。
 * 修复（R4 🔴1）：settledLen 进 props——升级时数值变化打破 memo 并重跑 effect 立即重写，
 * 否则 memo 跳过 + store subscribe 不触发 → LiveTail 停留旧起点与 settled 块重复。
 * 修复（R4 🔴2）：write 尾部回调 onGrow，ChatArea 据此贴底（纯 delta 帧不走 React 重渲）。
 */
const LiveTail = memo(function LiveTail({
  sid,
  settledLenRef,
  settledLen,
  onGrow,
}: {
  sid: string;
  settledLenRef: { current: number };
  settledLen: number;
  onGrow?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const write = () => {
      const s = useStreamStore.getState().streams[sid];
      if (!s || !ref.current) return;
      ref.current.textContent = s.content.slice(settledLenRef.current) + s.pendingContent;
      onGrow?.();
    };
    write();
    return useStreamStore.subscribe(write);
    // settledLen 变化（升级定型）时重跑 effect 立即重写 DOM，消除重复段
  }, [sid, settledLenRef, settledLen, onGrow]);

  return <div className="live-tail md-body" ref={ref} />;
});

/** reasoning 活动段：同样命令式（reasoning+pendingReasoning 全量） */
const ReasoningLive = memo(function ReasoningLive({ sid }: { sid: string }) {
  const ref = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const write = () => {
      const s = useStreamStore.getState().streams[sid];
      if (!s || !ref.current) return;
      ref.current.textContent = s.reasoning + s.pendingReasoning;
    };
    write();
    return useStreamStore.subscribe(write);
  }, [sid]);

  return (
    <details className="reasoning-card is-streaming" open>
      <summary>
        <span className="reasoning-card-label">思考过程</span>
        <span className="reasoning-card-dot" aria-label="思考中" />
      </summary>
      <pre className="reasoning-card-body" ref={ref} />
    </details>
  );
});

// ---------- 流式视图组装 ----------

interface StreamingMessageProps {
  sid: string;
  view: StreamingView;
  tools: StreamingToolSnapshot[];
  settledLenRef: { current: number };
  /** 活动段 DOM 增高后回调（贴底跟随，🔴2） */
  onGrow?: () => void;
}

export function StreamingMessage({ sid, view, tools, settledLenRef, onGrow }: StreamingMessageProps) {
  const hasPre = view.reasoning.length > 0 || tools.length > 0;
  return (
    <div className="streaming-message">
      {hasPre && <ReasoningLive sid={sid} />}
      {tools.map((t) => (
        <ToolCard key={t.toolCallId} tool={t} />
      ))}
      {view.settledText && <TextBlock content={view.settledText} home={`${sid}:settled`} />}
      <LiveTail sid={sid} settledLenRef={settledLenRef} settledLen={view.settledText.length} onGrow={onGrow} />
    </div>
  );
}

/** 定型消息视图（MessageCard 内容列调用） */
export const MessageBlocks = BlockRenderer;
