"""回归测试：subagent/title 默认模型保存的服务商标识歧义

Bug 背景：
    用户通过命令卡枚举选择「服务商名 + 模型名」配置 subagent / title 默认模型。
    枚举选项 value 使用 display_name（唯一，如 'opencode免费模型 #2'），但旧保存
    逻辑 _handle_subagents_command / _handle_title_gen_command 却取 config 的
    provider_name（如 'OpenCode Zen'）写入 SubagentDefaultModel / TitleGenDefaultModel。

    重新解析时 _resolve_service_provider 优先按 display_name 匹配，当存在两个同
    provider_name 的配置（df810bab 的 display_name='OpenCode Zen' 与 68ea6d92 的
    display_name='opencode免费模型 #2'）时，保存的 'OpenCode Zen' 会歧义匹配到
    df810bab，而非用户选择的 68ea6d92 —— 表现为「用的服务商记录和实际保存的不一致」。

修复：保存改用 display_name（与枚举 value 一致且唯一），消除解析歧义。
"""
import types

from app.main_widget import OpenAIChatToolWindow

# 模拟用户真实配置：两个 config 的 provider_name 同为 'OpenCode Zen'，
# 但 display_name 不同（靠 name / suffix 区分）。
VALID_CONFIGS = {
    "df810bab": {"provider_name": "OpenCode Zen", "display_name": "OpenCode Zen", "模型列表": ["hy3", "hy3-free"]},
    "68ea6d92": {"provider_name": "OpenCode Zen", "display_name": "opencode免费模型 #2", "模型列表": ["hy3-free", "hy3"]},
    "511f04aa": {"provider_name": "MiniMax", "display_name": "MiniMax", "模型列表": ["MiniMax-M2.7"]},
}


def _make_obj():
    obj = types.SimpleNamespace()
    obj._valid_configs = {k: dict(v) for k, v in VALID_CONFIGS.items()}
    obj._display_to_config_id = {c["display_name"]: cid for cid, c in VALID_CONFIGS.items()}
    return obj


def test_resolve_service_provider_display_name_precise_under_same_provider_name():
    obj = _make_obj()
    method = types.MethodType(OpenAIChatToolWindow._resolve_service_provider, obj)
    # 用户选 68ea6d92（display_name='opencode免费模型 #2'），枚举 value 用 display_name
    # -> 必须精确解析回原 config，不能歧义到 df810bab
    assert method("opencode免费模型 #2") == "68ea6d92"
    # 反向验证旧 bug：保存若误用 provider_name 'OpenCode Zen'，会歧义命中 df810bab
    assert method("OpenCode Zen") == "df810bab"


def test_saved_default_model_roundtrip_uses_display_name():
    obj = _make_obj()
    method = types.MethodType(OpenAIChatToolWindow._resolve_service_provider, obj)
    selected = "68ea6d92"
    # 修复后保存格式：display_name:model（与命令卡枚举 value 一致）
    saved = f"{VALID_CONFIGS[selected]['display_name']}:hy3-free"
    provider_part, _ = saved.split(":", 1)
    # 闭环：保存的标识能精确解析回用户所选 config
    assert method(provider_part) == selected


def test_model_options_flat_use_display_name():
    obj = _make_obj()
    obj._get_model_list_for_provider = lambda cid: VALID_CONFIGS[cid].get("模型列表", [])
    flat = OpenAIChatToolWindow._get_all_model_options_flat.__get__(obj)()
    values = [o["value"] for o in flat]
    # 两个同 provider_name 的配置必须用各自 display_name 区分
    assert "opencode免费模型 #2:hy3-free" in values
    assert "OpenCode Zen:hy3" in values
    # 68ea6d92（用户选中的 config）的全部选项都以它自己的 display_name 开头，
    # 绝不能退化成 provider_name 'OpenCode Zen'（否则保存后会歧义匹配到 df810bab）
    expected_68 = {f"opencode免费模型 #2:{m}" for m in VALID_CONFIGS["68ea6d92"]["模型列表"]}
    assert expected_68.issubset(set(values))
