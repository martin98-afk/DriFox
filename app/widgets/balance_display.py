# -*- coding: utf-8 -*-
"""
余额显示组件 - 支持 DeepSeek 和 SiliconFlow

用量聚合（T6）：余额查询逻辑迁移至 app/core/usage_service.py 进程级单例，
本组件只负责显示——set_provider 仅更新显示状态，请求/缓存/轮询由
UsageService 统一驱动，结果经 balance_ready 信号广播回来。
"""

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.utils.design_tokens import scale_font_size
from app.utils.utils import get_font_family_css

# 支持余额查询的服务商及其接口
BALANCE_APIS = {
    "DeepSeek": {
        "url": "https://api.deepseek.com/user/balance",
        "balance_key": "total_balance",  # 从 balance_infos[0] 中获取
        "currency": "¥",
    },
    "SiliconFlow (硅基流动)": {
        "url": "https://api.siliconflow.cn/v1/user/info",
        "balance_key": "totalBalance",  # 从 data 对象中获取
        "currency": "¥",
    },
}

# 余额显示的服务商列表
SUPPORTED_PROVIDERS = list(BALANCE_APIS.keys())


class BalanceDisplay(QWidget):
    """余额显示组件（显示层，请求委托 UsageService 全局单例）"""

    # 信号：当余额更新时发出
    balance_updated = pyqtSignal(float, str)  # balance, currency

    def __init__(self, parent=None):
        super().__init__(parent)
        self._balance = None
        self._currency = "¥"
        self._loading = False
        self._current_provider = ""
        self._current_config_id = ""  # 当前显示结果对应的 config_id（竞态校验）

        # 布局：图标 + 金额
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignVCenter)

        # 余额图标
        self._icon_label = QLabel("💰", self)
        self._icon_label.setFixedWidth(16)
        self._icon_label.setAlignment(Qt.AlignCenter)

        # 金额标签
        self._balance_label = QLabel("", self)
        self._balance_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._balance_label.setStyleSheet(f"""
            QLabel {{
                color: #5aa9ff;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                font-weight: 500;
                background: transparent;
                padding: 0px;
            }}
        """)

        layout.addWidget(self._icon_label)
        layout.addWidget(self._balance_label)

        # 初始隐藏
        self.setVisible(False)
        self.setFixedHeight(20)

        # ★ 用量聚合（T6）：余额结果由进程级单例 UsageService 广播
        # （缓存命中 / 后台抓取结果均经此信号），本组件只负责消费显示。
        # UsageService 只存 config 快照不持窗口引用；本连接随组件销毁自动断开。
        from app.core.usage_service import UsageService

        UsageService.get_instance().balance_ready.connect(self.show_balance_result)

        self.setToolTip("余额查询")

    def set_provider(self, provider_name: str, config_id: str = ""):
        """设置当前服务商（仅更新显示状态；请求由 UsageService 统一驱动）

        Args:
            provider_name: 真实服务商名（白名单判断）
            config_id: 当前配置 ID（UUID，用于结果竞态校验）
        """
        # 如果不是支持的服务商，隐藏组件
        if provider_name not in SUPPORTED_PROVIDERS:
            self.setVisible(False)
            self._balance = None
            self._current_provider = ""
            self._current_config_id = ""
            return

        # 立即显示加载状态（具体余额结果由 balance_ready 广播带回）
        self._current_provider = provider_name
        self._current_config_id = config_id
        self._balance_label.setText("...")
        self._icon_label.setText("⏳")
        self.setVisible(True)

    def refresh(self):
        """手动刷新余额（对话完成后调用）— 请求由 UsageService 单例统一驱动

        仅恢复加载态显示；实际重新拉取由 main_widget._refresh_balance →
        UsageService.request_balance 触发（TTL 内命中缓存，过期重拉）。
        """
        if self._current_provider:
            self._balance_label.setText("...")
            self._icon_label.setText("⏳")

    def show_balance_result(self, provider_name: str, config_id: str, result):
        """UsageService 广播的余额结果（主线程执行）。

        过期结果（用户已切换服务商/配置）通过 config_id 校验直接忽略。
        """
        # 过期结果（用户已切换服务商）直接忽略
        if config_id != self._current_config_id:
            return
        self._loading = False
        self._icon_label.setText("💰")

        if not result or result.get("hide"):
            self.setVisible(False)
            if result and result.get("tooltip"):
                self.setToolTip(result["tooltip"])
            return

        self._balance = result["balance"]
        self._currency = result["currency"]
        self._update_display()
        self.setToolTip(f"{result['provider']} 余额")
        self.balance_updated.emit(self._balance, self._currency)

    def _extract_balance(self, data: dict, config: dict) -> Optional[float]:
        """从响应数据中提取余额"""
        try:
            balance_key = config["balance_key"]

            if balance_key == "total_balance":
                # DeepSeek: {"balance_infos": [{"total_balance": "0.92", ...}]}
                balance_infos = data.get("balance_infos", [])
                if balance_infos and len(balance_infos) > 0:
                    balance_str = balance_infos[0].get("total_balance", "")
                    if balance_str:
                        return float(balance_str)
            else:
                # SiliconFlow: {"data": {"totalBalance": "-0.0079", ...}}
                data_obj = data.get("data", data)
                balance_str = data_obj.get(balance_key, "")
                if balance_str:
                    return float(balance_str)
            return None
        except (ValueError, TypeError, KeyError):
            return None

    def _update_display(self):
        """更新显示"""
        if self._balance is None:
            self.setVisible(False)
            return

        balance = self._balance
        color = "#5aa9ff"  # 默认蓝色

        # 根据余额大小设置颜色
        if balance < 0:
            # 欠费或余额为负 - 红色
            color = "#ff6b6b"
        elif balance < 1:
            # 小余额 - 黄色警告
            color = "#f6c453"
        elif balance < 10:
            # 低于10元 - 橙色
            color = "#ff9f43"
        else:
            # 正常余额 - 蓝色
            color = "#5aa9ff"
        # 统一保留 1 位小数
        display_text = f"{self._currency}{balance:.1f}"

        self._balance_label.setStyleSheet(f"""
            QLabel {{
                color: {color};
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                font-weight: 500;
                background: transparent;
                padding: 0px;
            }}
        """)
        self._balance_label.setText(display_text)
        self.setVisible(True)

    def clear(self):
        """清除显示"""
        self._balance = None
        self._balance_label.setText("")
        self._current_provider = ""
        self._current_config_id = ""
        self.setVisible(False)
        self.setToolTip("余额查询")
