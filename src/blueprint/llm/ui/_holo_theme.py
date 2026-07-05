# -*- coding: utf-8 -*-
"""赫萝主题配色 + QSS 生成器（供 4 个 UI 壳子统一调用）。

主题：固定奶茶白日（赫萝金棕发色 + 苹果红强调 + 奶茶背景）。

对外 API：
  get_theme(name: str = "day") -> dict[str, str]   取色板字典（参数兼容历史写法，实际固定返回 day）
  build_qss(name: str = "day") -> str              把主题色板填进 QSS 模板，返回完整 QSS
  font_stack() -> str                               返回 QSS 可用的 font-family 字符串
"""
from __future__ import annotations

from typing import Dict, List

# =====================================================================
# 色板：key 命名规范 = 组件_部位_状态
# =====================================================================
_THEME_DAY: Dict[str, str] = {
    # ---- 背景 ----
    "bg_root":         "#FFF5EC",   # 对话框主背景（奶茶浅棕）
    "bg_card":         "#FFFFFF",   # 卡片/气泡外框（女仆围裙白）
    "bg_sidebar":      "#FFEACC",   # 左侧栏背景（深一点的奶茶）
    "bg_hover":        "#FFE1C2",   # 悬停高亮
    "bg_checked":      "#FFD89A",   # 选中/按下（发色渐变浅段）
    "bg_input":        "#FFFFFF",   # 输入框
    "bg_input_dis":    "#F7EAD4",   # 禁用输入框

    # ---- 主色/强调色（赫萝提取）----
    "primary":         "#E8A95C",   # 赫萝发色 金棕 主色
    "primary_light":   "#FFD89A",   # 主色浅色背景（蓝图状态胶囊 / 浅色高亮）
    "primary_dark":    "#C97F35",   # hover / pressed
    "accent":          "#E55D5D",   # 赫萝苹果红（强调 / 发送按钮）
    "accent_dark":     "#C94040",
    "wolf":            "#6B5B73",   # 狼尾灰紫（辅助文字）
    "wolf_light":      "#B9A8D4",   # 狼尾亮紫（强调辅助色）
    "maid_black":      "#2D2419",   # 女仆装黑（标题色）
    "maid_white":      "#FAF6F0",   # 女仆装内衬（面板高光）
    "maid_green":      "#7FAA5C",   # 女仆装抹茶绿（胶囊 pill / tag）

    # ---- 文字 ----
    "text_main":       "#3A2D1E",   # 正文文字（深咖）
    "text_sub":        "#8A7460",   # 辅助文字 / placeholder
    "text_on_primary": "#FFFFFF",   # 主色按钮上的文字
    "text_on_accent":  "#FFFFFF",   # 苹果红按钮上的文字

    # ---- 边框/阴影 ----
    "border":          "#F2D4B0",   # 正常边框（奶茶描边）
    "border_focus":    "#E8A95C",   # focus 状态（发色）
    "shadow":          "0 4px 18px rgba(201, 127, 53, 0.15)",  # 卡片投影（金棕柔光）

    # ---- 气泡 ----
    "bubble_user_0":   "#FFD89A",   # 用户气泡 发色渐变（起）
    "bubble_user_1":   "#E8A95C",   # 用户气泡 发色渐变（止）
    "bubble_holo_0":   "#FFFFFF",   # 赫萝气泡 奶白（起）
    "bubble_holo_1":   "#FFF5EC",   # 赫萝气泡 奶茶（止）
    "bubble_holo_border": "#F2D4B0",
    # -- 粉红可爱版赫萝气泡（day：樱粉→蔷薇粉） --
    "bubble_holo_pink_0":   "#FFF7FB",   # 粉樱白
    "bubble_holo_pink_1":   "#FFE4F0",   # 浅粉
    "bubble_holo_pink_border": "#F5B7C9",

    # ---- 状态 ----
    "success":         "#7FAA5C",   # 成功绿（温柔，不是 GitHub 那种深绿）
    "warn":            "#E8A95C",   # 警告 = 发色
    "danger":          "#E55D5D",   # 危险 = 苹果红

    # ---- 主题名 ----
    "name":            "day",
}

_THEMES: Dict[str, Dict[str, str]] = {
    "day": _THEME_DAY,
}


def get_theme(name: str = "day") -> Dict[str, str]:
    """取主题色板字典（实际固定返回奶茶白日 day）；name 参数为历史兼容保留。"""
    return dict(_THEMES["day"])


# =====================================================================
# 字体 fallback 栈（Windows 环境下的卡通二次元友好字体）
# =====================================================================
_FONT_STACK = [
    "\"站酷庆科黄油体\"",        # 标题首选（免费商用，卡通圆滚滚）
    "\"Source Han Sans CN Heavy\"",  # 思源黑体 CN Heavy（次选）
    "\"Microsoft YaHei UI\"",    # Windows 默认兜底
    "\"微软雅黑\"",
    "sans-serif",
]


def font_stack() -> str:
    """返回一段 QSS 里能直接用的 font-family 字符串。"""
    return ", ".join(_FONT_STACK)


# =====================================================================
# QSS 生成：把色板 + 字体填进一个「通用基础样式」模板
# =====================================================================
_QSS_TEMPLATE = """
/* ================ 赫萝风格通用基础样式（主题：{name}） ================ */

/* ---------- 全局透明兜底：拒绝系统深色/浅色主题渗透进来 ----------
   只有下面显式声明了 background 的容器 / 控件才负责填色。
   未声明背景的控件（QLabel / Tab 内封装 QWidget / 布局空白区…）一律透明，
   让父级容器的配色（固定奶茶白日）正确透出。 */
QWidget {{
    background: transparent;
}}

/* ---------- 真正需要填实色的容器 ---------- */
QDialog, QWidget#holoRoot {{
    background: {bg_root};
}}
QWidget#holoTabContent {{
    background: {bg_card};
}}

/* ---------- 通用 QPushButton ---------- */
QPushButton {{
    background: {bg_card};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 14px;
    padding: 6px 14px;
    font-family: {fonts};
    font-size: 13px;
}}
QPushButton:hover {{
    background: {bg_hover};
    border-color: {primary};
    color: {primary_dark};
}}
QPushButton:pressed {{
    background: {bg_checked};
    border-color: {primary_dark};
}}
QPushButton:disabled {{
    color: {text_sub};
    background: {bg_input_dis};
    border-color: {border};
}}

/* ---------- QPushButton 赫萝侧栏 Tab（可 check，左图标+文字左对齐） ---------- */
QPushButton#holoSideTab {{
    text-align: left;
    padding: 10px 12px;
    border-radius: 14px;
    background: transparent;
    color: {text_main};
    border: 1px solid transparent;
}}
QPushButton#holoSideTab:hover {{
    background: {bg_hover};
    border: 1px solid {border};
}}
QPushButton#holoSideTab:checked {{
    background: {bg_checked};
    border: 1px solid {primary};
    color: {primary_dark};
    font-weight: 700;
}}

/* ---------- QPushButton 苹果红 Primary（发送 / 主操作） ---------- */
QPushButton#holoPrimary {{
    background: {accent};
    color: {text_on_accent};
    border: 2px solid {accent_dark};
    border-radius: 14px;
    font-weight: 700;
}}
QPushButton#holoPrimary:hover   {{ background: #F56D6D; }}
QPushButton#holoPrimary:pressed {{ background: {accent_dark}; }}

/* ---------- QPushButton 成功绿（导入/保存等） ---------- */
QPushButton#holoSuccess {{
    background: {success};
    color: #FFFFFF;
    border: 1px solid {success};
    border-radius: 14px;
    font-weight: 600;
}}
QPushButton#holoSuccess:hover   {{ background: #92C572; }}

/* ---------- 苹果红圆形图标按钮（发送按钮：icon_send.png） ---------- */
QPushButton#holoAppleSend {{
    background: {accent};
    border: 2px solid {accent_dark};
    border-radius: 22px;
}}
QPushButton#holoAppleSend:hover   {{ background: #F56D6D; }}
QPushButton#holoAppleSend:pressed {{ background: {accent_dark}; }}

/* ---------- 输入框 ---------- */
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QTextEdit {{
    background: {bg_input};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 16px;
    padding: 8px 12px;
    selection-background-color: {primary};
    selection-color: #FFFFFF;
    font-family: {fonts};
    font-size: 13px;
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus, QTextEdit:focus {{
    border-color: {border_focus};
}}
QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled {{
    background: {bg_input_dis};
    color: {text_sub};
}}
QPlainTextEdit {{ border-radius: 16px; padding-top: 10px; }}

/* ---------- 组合框 drop-down ---------- */
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {bg_card};
    color: {text_main};
    border: 1px solid {border};
    selection-background-color: {primary};
    selection-color: #FFFFFF;
    outline: 0;
}}

/* ---------- QFrame 卡片 ---------- */
QFrame#holoCard {{
    background: {bg_card};
    border: 1px solid {border};
    border-radius: 20px;
}}

/* ---------- QListWidget（聊天消息列表 / 会话历史） ---------- */
QListWidget {{
    background: {bg_root};
    border: 1px solid {border};
    border-radius: 16px;
    outline: none;
    padding: 4px;
}}
QListWidget::item {{
    padding: 2px;
    margin: 0;
    border-radius: 16px;
}}

/* ---------- Section 标题 ---------- */
QLabel#holoSection {{
    color: {text_sub};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 10px 12px 2px 12px;
}}

/* ---------- 状态胶囊 ---------- */
QLabel#holoStatus {{
    padding: 4px 12px;
    border-radius: 14px;
    font-weight: 600;
}}

/* ---------- QTabWidget（设置面板） ---------- */
QTabWidget::pane {{
    border: 1px solid {border};
    border-radius: 16px;
    background: {bg_card};
    margin-top: 8px;
    padding: 6px;
}}
QTabBar::tab {{
    background: {bg_sidebar};
    color: {text_sub};
    padding: 8px 12px 6px 12px;
    border: 1px solid {border};
    border-bottom: none;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
    margin-right: 2px;
    font-family: {fonts};
    font-weight: 700;
    font-size: 13px;
    min-width: 96px;
}}
QTabBar::tab:selected {{
    background: {bg_card};
    color: {primary_dark};
    border-color: {primary};
    border-top: 2px solid {primary};
}}

/* ---------- QGroupBox（设置分组） ---------- */
QGroupBox {{
    color: {text_main};
    border: 1px solid {border};
    border-radius: 16px;
    margin-top: 12px;
    padding-top: 12px;
    background: {bg_card};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 10px;
    color: {primary_dark};
    font-weight: 800;
    font-size: 13.5px;
}}

/* ---------- QCheckBox ---------- */
QCheckBox {{
    color: {text_main};
    spacing: 8px;
    font-family: {fonts};
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 6px;
    border: 1px solid {border};
    background: {bg_input};
}}
QCheckBox::indicator:hover   {{ border-color: {primary}; }}
QCheckBox::indicator:checked {{
    background: {success};
    border-color: {success};
}}

/* ---------- QSlider ---------- */
QSlider::groove:horizontal {{
    height: 6px;
    background: {border};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {primary};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px;
    height: 16px;
    margin: -6px 0;
    background: {primary};
    border: 2px solid {primary_dark};
    border-radius: 8px;
}}

/* ---------- 滚动条（细 + 奶茶色） ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px;
}}
QScrollBar::handle:vertical {{
    background: {border};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {primary}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {primary}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---------- QMessageBox ---------- */
QMessageBox {{
    background: {bg_root};
}}
QMessageBox QLabel {{
    color: {text_main};
    font-family: {fonts};
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 90px;
}}

/* ---------- 补漏：Popup 类控件 / 系统原生部件（防止透明兜底后它们看不见） ---------- */
QMenu {{
    background: {bg_card};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 12px;
    padding: 6px 4px;
    font-family: {fonts};
    font-size: 13px;
}}
QMenu::item {{
    padding: 6px 22px;
    border-radius: 8px;
}}
QMenu::item:selected {{
    background: {primary};
    color: {text_on_primary};
}}
QMenu::separator {{
    height: 1px;
    background: {border};
    margin: 4px 10px;
}}

QToolTip {{
    background: {bg_card};
    color: {text_main};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 4px 8px;
    font-family: {fonts};
    font-size: 12px;
}}

QLabel {{
    color: {text_main};
    font-family: {fonts};
}}

QRadioButton {{
    color: {text_main};
    spacing: 8px;
    font-family: {fonts};
}}
QRadioButton::indicator {{
    width: 20px;
    height: 20px;
    border-radius: 10px;
    border: 2px solid {border};
    background: {bg_input};
}}
QRadioButton::indicator:hover   {{ border-color: {primary}; }}
QRadioButton::indicator:checked {{
    background: {bg_card};
    border: 2px solid {primary_dark};
}}

QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 16px;
    background: transparent;
    border: none;
}}
QSpinBox::up-arrow, QSpinBox::down-arrow {{
    width: 8px;
    height: 8px;
    background: {primary};
}}
"""


def build_qss(name: str = "day") -> str:
    """把主题色板填进 _QSS_TEMPLATE，返回完整 QSS 字符串。"""
    t = get_theme(name)
    t["fonts"] = font_stack()
    return _QSS_TEMPLATE.format(**t)


# =====================================================================
# 便捷取色（用于 paintEvent 里的 QColor）
# =====================================================================
def c(theme: Dict[str, str], key: str, fallback: str = "#000000") -> str:
    return theme.get(key, fallback)
