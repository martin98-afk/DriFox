/**
 * ReasoningCard：思考卡（折叠卡，直出文本不跑 markdown）。
 * 流式 live 态由 BlockRenderer 的 ReasoningLive 命令式通道承载（本组件只管定型态）。
 */

import { memo } from "react";

interface Props {
  content: string;
  streaming?: boolean;
  defaultOpen?: boolean;
}

export const ReasoningCard = memo(function ReasoningCard({ content, streaming = false, defaultOpen = true }: Props) {
  return (
    <details className={`reasoning-card${streaming ? " is-streaming" : ""}`} open={defaultOpen}>
      <summary>
        <span className="reasoning-card-label">思考过程</span>
        {streaming && <span className="reasoning-card-dot" aria-label="思考中" />}
      </summary>
      <pre className="reasoning-card-body">{content}</pre>
    </details>
  );
});
