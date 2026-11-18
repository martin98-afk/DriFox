/**
 * PluginBlock：custom 块占位（T9：插件 renderer 产出 HTML 直用；plugin-render/iframe 槽后接）。
 */

import { memo } from "react";
import { fingerprint } from "../fingerprint";

interface Props {
  customType: string;
  data: unknown;
}

export const PluginBlock = memo(function PluginBlock({ customType, data }: Props) {
  const key = fingerprint("custom", customType, data);
  return (
    <div className="plugin-block" data-key={key} data-custom-type={customType}>
      <div className="plugin-block-head">
        <span className="plugin-block-badge">插件</span>
        <span className="plugin-block-type">{customType}</span>
      </div>
      <pre className="plugin-block-body">{typeof data === "string" ? data : JSON.stringify(data, null, 2)}</pre>
    </div>
  );
});
