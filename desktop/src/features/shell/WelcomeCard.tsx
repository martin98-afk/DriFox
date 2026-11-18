/**
 * WelcomeCard：无会话/空会话时的主区欢迎占位卡（review 仲裁语义：非页签、无关闭钮）。
 * 居中 logo + 提示文案 + 插件模式 tab 条占位；发送首条消息后被对话流替换。
 */

export function WelcomeCard() {
  return (
    <div className="WelcomeCard">
      <div className="welcome-logo" aria-hidden>DF</div>
      <h2 className="welcome-title">开始一场对话</h2>
      <p className="welcome-hint">选择左侧会话，或点击「新建对话」开始。发送首条消息后此处将被对话流替换。</p>
      <div className="welcome-tabs" aria-hidden>
        <span className="welcome-tab is-active">默认模式</span>
        <span className="welcome-tab">插件模式一</span>
        <span className="welcome-tab">插件模式二</span>
      </div>
    </div>
  );
}
