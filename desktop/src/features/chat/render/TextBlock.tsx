/**
 * TextBlock：markdown 全管线产物 HTML（T9 text 块）。
 * memo：content 不变（settled 块）→ 跳过重渲；renderMarkdown 内部 LRU 防重算。
 */

import { memo, useMemo } from "react";
import { renderMarkdown } from "../pipeline";

interface Props {
  content: string;
  home: string;
}

export const TextBlock = memo(function TextBlock({ content }: Props) {
  const html = useMemo(() => renderMarkdown(content, true), [content]);
  return <div className="text-block md-body" dangerouslySetInnerHTML={{ __html: html }} />;
});
