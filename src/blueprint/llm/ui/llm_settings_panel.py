# -*- coding: utf-8 -*-
"""LLM 设置面板（赫萝风格 · 纯 UI Stub · 文档 §4.4 规范）。

改造点：
  · 顶部：总开关加大看板娘（圆形大头像 + 「赫萝女仆助理」标题 + 启用按钮）
  · Tab 结构改 4 个：模型服务商 / 通用参数 / 🎭 赫萝人设(新增) / 🧠 记忆库(新增)
  · 人设 Tab：第一人称、自称、性格、口癖、初始问候、身份卡（女仆/贤狼自定义）
  · 记忆库 Tab：全局人设、用户档案、常玩游戏配置、3 个快捷入口按钮
  · 模型/参数 原有结构保留，仅 QSS 换成赫萝奶茶白日主题
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractSlider,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ._holo_theme import build_qss, get_theme
from ._icon_loader import icon, pixmap

_THEME_NAME = os.environ.get("BA_HOLO_THEME", "day")
_THEME = get_theme(_THEME_NAME)
_QSS = build_qss(_THEME_NAME)

_LABEL_FIXED_W = 132   # 表单标签统一固定宽度，保证输入框左对齐基线
_CONTROL_H = 36        # 表单控件统一高度（LineEdit/ComboBox/SpinBox）

# LLM 默认配置 JSON（与 _icon_loader 同风格：ui/_icon_loader.py 向上 4 层到项目根 /config/）
_LLM_DEFAULTS_JSON = os.path.normpath(os.path.join(
    os.path.abspath(os.path.dirname(__file__)),
    "..", "..", "..", "..", "config", "llm_defaults.json",
))


# 原始"最后手段"硬编码（JSON 不存在或损坏时的 fallback，保证程序永远能启动）
def _LLM_FALLBACK_DATA() -> Dict[str, Any]:
    return {
        "ui": {
            "theme": "day", "topmost": True,
            "stream_output": True, "skip_ssl_verify": True,
        },
        "blueprint": {
            "validate_blueprint": True,
            "autosave_draft": True,
            "max_attempts": 3,
        },
        "persona": {
            "forced_self": "咱",
            "forced_you": "汝",
            "presets": [
                {
                    "preset_idx": 0, "preset_name": "女仆赫萝",
                    "preset_label": "女仆赫萝（默认 · 听话又傲娇）",
                    "trait_idx": 0, "trait_name": "傲娇",
                    "traits": "苹果给你、报酬两个苹果、哼~、算啦算啦、嘴上说不身体诚实",
                    "greeting": "唔姆，汝~ 咱是赫萝，汝的女仆助理 🐺🍎\n有什么想让咱帮忙的尽管说，报酬苹果给汝留一颗 ♡",
                    "system_prompt": (
                        "你是赫萝（Holo），《狼与香辛料》里的贤狼化身，现在以女仆助理的身份协助用户。\n"
                        "· 自称「咱」，称呼用户「汝」；\n"
                        "· 性格：傲娇又聪明，偶尔毒舌但会悄悄把活干完；\n"
                        "· 功能：写 Blueprint 自动化蓝图、代码、游戏宏、图像分析；\n"
                        "· 回答简明，必要时再展开。"
                    ),
                },
                {
                    "preset_idx": 1, "preset_name": "游戏宅赫萝",
                    "preset_label": "游戏宅赫萝（天天摸鱼打游戏）",
                    "trait_idx": 2, "trait_name": "毒舌",
                    "traits": "喂~、辣鸡操作、上分中…、带杯奶茶回来、抽卡保底、深渊满星",
                    "greeting": "哼~ 汝，又打扰本小姐打深渊了？要帮忙就说嘛，带杯奶茶回来 🎮",
                    "system_prompt": (
                        "你是游戏宅赫萝，沉迷二次元、Genshin/崩铁/LOL，爱用游戏术语，偶尔毒舌。\n"
                        "· 自称「咱」，称呼用户「汝」；\n"
                        "· 性格：嘴硬心软，嘴上吐槽但该办的事都会办好；\n"
                        "· 说话风格：带游戏黑话，喜欢用喂~/哼~等口头禅；\n"
                        "· 讨论自动化时：用「副本/打宝/通关」类比功能。"
                    ),
                },
                {
                    "preset_idx": 2, "preset_name": "贤狼赫萝",
                    "preset_label": "贤狼赫萝（原典 · 苹果商人的旅伴）",
                    "trait_idx": 3, "trait_name": "温柔",
                    "traits": "汝呀~、苹果给你、哼哼、麦子熟了吗？、约伊兹的贤狼、旅伴",
                    "greeting": "哼，汝这是第几次求咱了？算了，苹果给足，咱就帮帮汝吧 🐺🍎",
                    "system_prompt": (
                        "你是赫萝，约伊兹的贤狼，和旅行商人罗伦斯一起走过许多镇子。\n"
                        "· 自称「咱」，称呼用户「汝」，说话带古风，口吻从容睿智；\n"
                        "· 性格：温柔又老练，会讲大道理，偶尔露出贤狼的小骄傲；\n"
                        "· 常提及：麦子、苹果、天气、远方的镇子、市集、丰收；\n"
                        "· 回答偏向：用比喻的方式讲清楚问题背后的本质。"
                    ),
                },
            ],
        },
    }


# =====================================================================
#  禁用滚轮的控件封装（统一在 LLM 设置面板内使用，避免悬停滚轮误改值）
#  · 下拉框：只有下拉弹出(view 可见)时才允许滚轮，否则 ignore
#  · SpinBox/Slider/TabBar：任何情况下都 ignore 滚轮
# =====================================================================
class NoWheelComboBox(QComboBox):
    def wheelEvent(self, e):
        if self.view().isVisible():
            super().wheelEvent(e)
        else:
            e.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelTabBar(QTabBar):
    def wheelEvent(self, e):
        e.ignore()


def _wrap_in_scroll(inner_widget: QWidget) -> QWidget:
    """把 Tab 内容包进一个 QScrollArea，让 Tab 本身固定在外层，只有 Tab 内容可滚。"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(inner_widget)
    return scroll


def _label(text: str, sub=False, fixed_w: int = _LABEL_FIXED_W) -> QLabel:
    l = QLabel(text)
    if sub:
        l.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:12px;")
    else:
        l.setStyleSheet(f"background:transparent; color:{_THEME['text_main']}; font-weight:700;")
    if fixed_w and not sub:
        l.setFixedWidth(fixed_w)
    return l


def _row(layout_cls=QHBoxLayout, spacing: int = 10, v_align=True):
    """统一行布局构造器：默认横向、默认垂直居中对齐。"""
    r = layout_cls()
    r.setSpacing(spacing)
    if v_align and layout_cls is QHBoxLayout:
        r.setAlignment(Qt.AlignVCenter)
    return r


def _apply_control_h(widget, h: int = _CONTROL_H):
    """给输入类控件统一高度，避免行高参差不齐。"""
    policy = widget.sizePolicy()
    policy.setVerticalPolicy(QSizePolicy.Fixed)
    widget.setSizePolicy(policy)
    widget.setFixedHeight(h)
    return widget


class LLMSettingsDialog(QDialog):
    """LLM 设置面板（赫萝风格 · 5 Tab 版）。"""

    settings_saved = Signal(dict)
    llm_enabled_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Window | Qt.WindowCloseButtonHint)
        self.setWindowTitle("赫萝女仆 · LLM 设置 🐺🍎")
        self.setObjectName("LLMSettingsDialog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.resize(780, 620)
        self.setMinimumSize(700, 500)
        self.setStyleSheet(_QSS)
        self._data: Dict[str, Any] = {}
        self._build_ui()
        # ⚠ 双重保险：全对话框级别拦截 WheelEvent，确保任何未来新增控件（漏了用 NoWheel* 版）
        # 也不会在悬停时误触发滚轮改值。QScrollArea 里的滚轮则照常工作（因为是视口/滚动条自身处理）。
        self.installEventFilter(self)
        self.load_defaults()

    # =================================================================
    # 事件：兜底禁用所有表单控件的滚轮（QScrollArea 自身的内容滚动仍保留）
    # =================================================================
    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.Wheel:
            # 1) 下拉框弹出状态：让下拉列表内部可以滚，直接放行
            if isinstance(obj, QComboBox) and obj.view().isVisible():
                return super().eventFilter(obj, ev)
            # 2) 以下类型：悬停时滚轮一律拦截（避免误改值）
            if isinstance(obj, (QComboBox, QSpinBox, QAbstractSlider, QTabBar)):
                ev.ignore()
                return True
            # 3) QScrollArea / QAbstractScrollArea 的 viewport：放行（内容区域该滚还得滚）
            #    Qt 里滚动条自己调 wheel 用的是 QWidget(viewport)，不是 QScrollBar 自身
            #    所以默认让 super 来处理就好
        return super().eventFilter(obj, ev)

    # =================================================================
    # UI 构建
    # =================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        # ---- 顶部：总开关看板娘（固定区，不跟随滚动） ----
        head = QFrame(); head.setObjectName("holoCard")
        hl = QHBoxLayout(head); hl.setContentsMargins(14, 12, 14, 12); hl.setSpacing(14)

        # 大看板娘 72x72
        apm = pixmap("avatar_holo_curtsy.png", 72)
        if not apm.isNull():
            out = QPixmap(72, 72); out.fill(Qt.transparent)
            qp = QPainter(out); qp.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath(); path.addEllipse(0, 0, 72, 72); qp.setClipPath(path)
            # 原图人物偏左，整体向右平移 4px，让脸/主体落在圆形视觉中心
            qp.drawPixmap(4, 0, apm); qp.setClipping(False)
            pen = QPen(Qt.NoPen); qp.setPen(pen)
            qp.setBrush(Qt.NoBrush)
            qp.setPen(QPen(Qt.transparent))
            qp.end()
            qp2 = QPainter(out); qp2.setRenderHint(QPainter.Antialiasing)
            qp2.setPen(QPen(QColor(_THEME["primary"]), 2))
            qp2.setBrush(Qt.NoBrush)
            qp2.drawEllipse(0, 0, 71, 71)
            qp2.end()
            albl = QLabel(); albl.setPixmap(out); albl.setFixedSize(72, 72)
            hl.addWidget(albl)

        # 标题区 + 状态胶囊
        title_col = QVBoxLayout(); title_col.setSpacing(4)
        tt_row = QHBoxLayout(); tt_row.setSpacing(12); tt_row.setAlignment(Qt.AlignVCenter)
        tt = QLabel("赫萝女仆助理")
        tt.setStyleSheet(f"background:transparent; color:{_THEME['maid_black']}; font-size:20px; font-weight:900;")
        tt_row.addWidget(tt)
        self.lbl_status = QLabel("🐺  待命")
        self.lbl_status.setObjectName("holoStatus")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet(
            f"QLabel {{ background:{_THEME['maid_green']}; color:{_THEME['maid_white']};"
            f" border-radius:11px; padding:2px 14px; font-size:12px; font-weight:800; }}"
        )
        self.lbl_status.setFixedHeight(22)
        tt_row.addWidget(self.lbl_status); tt_row.addStretch(1)
        title_col.addLayout(tt_row)
        ts = QLabel("开启后桌面会出现赫萝悬浮球，随时接受你的苹果（指令）🍎")
        ts.setWordWrap(True); ts.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:12px;")
        title_col.addWidget(ts)
        hl.addLayout(title_col, 1)

        # 总开关 CheckBox（大号）
        self.chk_enable = QCheckBox("启用 LLM 女仆功能")
        self.chk_enable.setCursor(Qt.PointingHandCursor)
        self.chk_enable.setChecked(True)
        self.chk_enable.setStyleSheet(
            f"QCheckBox {{ background:transparent; font-size:14px; font-weight:800; color:{_THEME['primary_dark']}; }}"
            f"QCheckBox::indicator {{ width:22px; height:22px; border-radius:7px; }}"
        )
        def _sw_enable(on: bool):
            if on:
                self.lbl_status.setText("🐺  待命")
                self.lbl_status.setStyleSheet(
                    f"QLabel {{ background:{_THEME['maid_green']}; color:{_THEME['maid_white']};"
                    f" border-radius:11px; padding:2px 14px; font-size:12px; font-weight:800; }}"
                )
            else:
                self.lbl_status.setText("🛌  关闭")
                self.lbl_status.setStyleSheet(
                    f"QLabel {{ background:{_THEME['wolf']}; color:{_THEME['maid_white']};"
                    f" border-radius:11px; padding:2px 14px; font-size:12px; font-weight:800; }}"
                )
            # ⚠ 关键：开关切换时实时发信号，主窗口收到立刻创建/销毁悬浮球
            self.llm_enabled_changed.emit(bool(on))
        self.chk_enable.toggled.connect(_sw_enable)
        hl.addWidget(self.chk_enable, 0, Qt.AlignRight | Qt.AlignVCenter)

        outer.addWidget(head, 0)

        # ---- 中间：Tab 区（TabBar 固定在顶部不跟随滚动；每个 Tab 内部自己管滚动） ----
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        # 自定义 TabBar：禁用滚轮切换 Tab，同时禁止横向滚动
        self.tabs.setTabBar(NoWheelTabBar())
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.ElideMiddle)
        self.tabs.tabBar().setExpanding(True)
        # 直接加到外层布局，不再被大 ScrollArea 包住 → TabBar 永远固定可见
        outer.addWidget(self.tabs, 1)

        # ⚠ Tab 内容才单独包 QScrollArea（Tab 内部滚，Tab 头不滚）
        self.tabs.addTab(_wrap_in_scroll(self._build_model_tab()),   "🤖  模型服务商")
        self.tabs.addTab(_wrap_in_scroll(self._build_params_tab()),  "⚙   通用参数")
        self.tabs.addTab(_wrap_in_scroll(self._build_persona_tab()), "🎭  赫萝人设")
        self.tabs.addTab(_wrap_in_scroll(self._build_memory_tab()),  "🧠  记忆库")
        self.tabs.addTab(_wrap_in_scroll(self._build_usage_tab()),   "💡  使用说明")

        # ---- 底部按钮条（固定在对话框底部，不跟随滚动，始终可见） ----
        btn_bar = QFrame()
        btn_bar.setObjectName("holoBtnBar")
        btn_bar.setStyleSheet(
            f"#holoBtnBar {{ background: transparent; border-top:1px solid {_THEME['border']}; }}"
        )
        btns = QHBoxLayout(btn_bar); btns.setContentsMargins(0, 12, 0, 0); btns.setSpacing(10)
        from PySide6.QtCore import QSize as _QS
        self.btn_test = QPushButton(" 🔍 测试连接"); self.btn_test.setIcon(icon("icon_retry.png", 16))
        self.btn_test.setIconSize(_QS(16, 16))
        _apply_control_h(self.btn_test, 36)
        self.btn_test.clicked.connect(self._on_test_connection)
        btns.addWidget(self.btn_test)

        self.btn_export = QPushButton(" 📤 导出配置")
        self.btn_export.setIcon(icon("icon_upload.png", 16)); self.btn_export.setIconSize(_QS(16, 16))
        _apply_control_h(self.btn_export, 36)
        self.btn_export.clicked.connect(self._on_export)
        btns.addWidget(self.btn_export)

        self.btn_import = QPushButton(" 📥 导入配置")
        self.btn_import.setIcon(icon("icon_import.png", 16)); self.btn_import.setIconSize(_QS(16, 16))
        _apply_control_h(self.btn_import, 36)
        self.btn_import.clicked.connect(self._on_import)
        btns.addWidget(self.btn_import)

        btns.addStretch(1)

        self.btn_cancel = QPushButton(" 取消")
        _apply_control_h(self.btn_cancel, 36)
        self.btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_cancel)

        self.btn_save = QPushButton(" 💾 保存设置"); self.btn_save.setObjectName("holoPrimary")
        self.btn_save.setIcon(icon("icon_memory.png", 16)); self.btn_save.setIconSize(_QS(16, 16))
        _apply_control_h(self.btn_save, 36)
        self.btn_save.clicked.connect(self._on_save)
        btns.addWidget(self.btn_save)

        outer.addWidget(btn_bar, 0)

    # =================================================================
    # Tab 1：模型服务商
    # =================================================================
    def _build_model_tab(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(12); root.setContentsMargins(12, 12, 12, 12)

        g1 = QGroupBox("🤖 服务商与模型")
        v = QVBoxLayout(g1); v.setSpacing(10); v.setContentsMargins(14, 18, 14, 14)

        # LLM 服务商（仅保留通用的 OpenAI + Ollama 本地两套）
        row1 = _row()
        row1.addWidget(_label("LLM 服务商"))
        self.cmb_provider = _apply_control_h(NoWheelComboBox())
        self.cmb_provider.addItems([
            "OpenAI", "Ollama"
        ])
        row1.addWidget(self.cmb_provider, 1)
        v.addLayout(row1)

        # Base URL（单独一行更宽敞）
        row2 = _row()
        row2.addWidget(_label("Base URL"))
        self.ed_baseurl = _apply_control_h(QLineEdit())
        self.ed_baseurl.setPlaceholderText("https://api.openai.com/v1   （Ollama: http://localhost:11434/v1）")
        row2.addWidget(self.ed_baseurl, 1)
        v.addLayout(row2)

        # API Key（单独一行，避免和 Base URL 挤在一起看不清）
        row3 = _row()
        row3.addWidget(_label("API Key"))
        self.ed_apikey = _apply_control_h(QLineEdit())
        self.ed_apikey.setEchoMode(QLineEdit.Password)
        self.ed_apikey.setPlaceholderText("sk-…  （必填，留空无法拉取模型列表和调用）")
        row3.addWidget(self.ed_apikey, 1)
        v.addLayout(row3)

        # 对话模型：下拉选择 + 右侧「拉取模型列表」按钮
        row4 = _row()
        row4.addWidget(_label("对话模型"))
        self.cmb_model = _apply_control_h(NoWheelComboBox())
        self.cmb_model.setEditable(True)  # 允许手动输入（模型名没在返回列表里时手填）
        self.cmb_model.addItems(["gpt-4o-mini", "qwen2.5:7b"])
        self.cmb_model.setCurrentText("gpt-4o-mini")
        row4.addWidget(self.cmb_model, 1)
        row4.addSpacing(8)
        self.btn_fetch_models = QPushButton(" 🔎 拉取模型列表")
        self.btn_fetch_models.setCursor(Qt.PointingHandCursor)
        _apply_control_h(self.btn_fetch_models, 36)
        self.btn_fetch_models.clicked.connect(self._on_fetch_models)
        row4.addWidget(self.btn_fetch_models, 0)
        v.addLayout(row4)
        root.addWidget(g1)

        # 高级
        g2 = QGroupBox("🛠  高级设置"); v2 = QVBoxLayout(g2); v2.setSpacing(10); v2.setContentsMargins(14, 18, 14, 14)
        row5 = _row()
        row5.addWidget(_label("最大上下文 (tokens)"))
        self.spn_ctx = _apply_control_h(NoWheelSpinBox())
        self.spn_ctx.setRange(512, 200000); self.spn_ctx.setValue(16384); self.spn_ctx.setSingleStep(512)
        row5.addWidget(self.spn_ctx, 1)
        row5.addSpacing(16)
        row5.addWidget(_label("单次最大输出"))
        self.spn_out = _apply_control_h(NoWheelSpinBox())
        self.spn_out.setRange(128, 32768); self.spn_out.setValue(2048); self.spn_out.setSingleStep(128)
        row5.addWidget(self.spn_out, 1)
        v2.addLayout(row5)

        # 开关一行放两个，空间利用率更高
        # ⚠ 已删除「流式输出」「跳过 SSL 证书」勾选框，统一改到 config/llm_defaults.json 里配置：
        #   ui.stream_output  = true  → 默认启用流式输出（打字机效果）
        #   ui.skip_ssl_verify = true → 默认跳过 SSL 证书校验（方便本地/自建接口）

        root.addWidget(g2)
        root.addStretch(1)
        return w

    # =================================================================
    # Tab 2：通用参数（Temperature/TopP/sliders）
    # =================================================================
    def _build_params_tab(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(12); root.setContentsMargins(12, 12, 12, 12)

        g1 = QGroupBox("🎛  采样参数"); v = QVBoxLayout(g1); v.setSpacing(14); v.setContentsMargins(14, 18, 14, 14)

        def _slider(title: str, tip: str, init: float, step: float = 0.05, lo: float = 0.0, hi: float = 2.0):
            outer = QVBoxLayout(); outer.setSpacing(4)

            # 头部：标签 + 数值胶囊（紧凑左对齐，数值胶囊用背景色强调）
            head = QHBoxLayout(); head.setSpacing(10); head.setAlignment(Qt.AlignVCenter)
            title_lbl = _label(title, fixed_w=0)  # slider 区标签不用固定宽，让它自然长度
            title_lbl.setStyleSheet(f"background:transparent; color:{_THEME['text_main']}; font-weight:700; font-size:13px;")
            title_lbl.setMinimumWidth(88)
            head.addWidget(title_lbl, 0)

            lbl = QLabel(f"  {init:.2f}  ")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                f"QLabel {{ background:{_THEME['primary']}; color:{_THEME['text_on_primary']};"
                f" border-radius:10px; padding:2px 10px; font-weight:900; font-size:13px; }}"
            )
            lbl.setFixedHeight(22)
            head.addWidget(lbl, 0)
            head.addStretch(1)
            outer.addLayout(head)

            # tip 文本（次级灰色，单独一行，不与标题抢横向空间）
            tip_lbl = _label(tip, sub=True)
            tip_lbl.setWordWrap(True)
            tip_lbl.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:12px; padding-left:2px;")
            outer.addWidget(tip_lbl)

            # 实际 Slider
            s = NoWheelSlider(Qt.Horizontal)
            s.setCursor(Qt.PointingHandCursor)
            i_max = int(round((hi - lo) / step))
            s.setRange(0, i_max)
            s.setValue(int(round((init - lo) / step)))
            def _on_change():
                vv = lo + s.value() * step
                lbl.setText(f"  {vv:.2f}  ")
            s.valueChanged.connect(_on_change)
            outer.addWidget(s)
            v.addLayout(outer)
            return s

        self.sl_temp = _slider("Temperature", "越高越发散 · 赫萝碎嘴模式建议 0.8~1.2 · 默认 0.7", 0.7, 0.05, 0.0, 2.0)
        self.sl_top  = _slider("Top P",         "核采样阈值 · 控制候选词累计概率 · 默认 0.9 不用改", 0.9, 0.01, 0.0, 1.0)
        self.sl_freq = _slider("频率惩罚",      "正值=减少重复语句 · 范围 -2.0 ~ 2.0 · 默认 0", 0.0, 0.05, -2.0, 2.0)
        self.sl_pres = _slider("存在惩罚",      "正值=鼓励聊新话题 · 范围 -2.0 ~ 2.0 · 默认 0", 0.0, 0.05, -2.0, 2.0)

        root.addWidget(g1)

        # ⚠ 原「🧩 蓝图输出」整组设置已删除，统一改到 config/llm_defaults.json 里配置：
        #   blueprint.validate_blueprint = true → 生成后自动校验&修复
        #   blueprint.autosave_draft   = true → 自动保存草稿到记忆库历史
        #   blueprint.max_attempts     = 3    → 单次最大尝试次数（1~10）

        root.addStretch(1)
        return w

    # =================================================================
    # Tab 3：🎭 赫萝人设
    # =================================================================
    def _build_persona_tab(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(12); root.setContentsMargins(12, 12, 12, 12)

        presets = LLMSettingsDialog._persona_presets()

        # 预设下拉（一键套用）
        g0 = QGroupBox("🐺 身份预设（一键套用）"); v0 = QHBoxLayout(g0); v0.setSpacing(12); v0.setContentsMargins(14, 18, 14, 14)
        v0.addWidget(_label("预设风格"))
        self.cmb_preset = _apply_control_h(NoWheelComboBox())
        # 从配置 JSON 读取 preset_label，保持 3 项顺序（女仆→游戏宅→贤狼）
        self.cmb_preset.addItems([
            p.get("preset_label") or p.get("preset_name") or f"预设{i+1}"
            for i, p in enumerate(presets)
        ])
        self.cmb_preset.currentIndexChanged.connect(self._apply_persona_preset)
        v0.addWidget(self.cmb_preset, 1)
        root.addWidget(g0)

        # 预设信息展示卡（只读，告诉用户当前预设的性格/称呼/关键字是什么，不能手动改）
        card = QFrame(); card.setObjectName("holoCard")
        card.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
            f" border-radius:18px; }}"
        )
        c = QVBoxLayout(card); c.setContentsMargins(18, 16, 18, 16); c.setSpacing(8)
        h1 = QHBoxLayout(); h1.setSpacing(8)
        pm = pixmap("icon_memory.png", 20)
        if not pm.isNull():
            ic = QLabel(); ic.setPixmap(pm); ic.setFixedSize(20, 20); h1.addWidget(ic)
        t1 = QLabel("当前预设详情（只能通过上方下拉切换，所有参数写死不可修改）")
        t1.setStyleSheet(f"background:transparent; color:{_THEME['primary_dark']}; font-weight:800; font-size:14px;")
        h1.addWidget(t1, 1); c.addLayout(h1)
        self.lbl_persona_info = QLabel()
        self.lbl_persona_info.setWordWrap(True)
        self.lbl_persona_info.setStyleSheet(
            f"background:transparent; color:{_THEME['maid_black']}; font-size:13px; line-height:1.7;"
            " padding:6px 2px;"
        )
        c.addWidget(self.lbl_persona_info)
        root.addWidget(card, 0)

        root.addStretch(1)

        # 初始化显示默认预设详情
        self._apply_persona_preset(0)
        return w

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_llm_defaults() -> Dict[str, Any]:
        """从 config/llm_defaults.json 读取写死配置；找不到/解析失败时用 _LLM_FALLBACK_DATA 兜底。
        用 lru_cache(maxsize=1) 只在首次调用时读一次盘（JSON 只在打包/开发时改，运行时不会改）。"""
        try:
            path = _LLM_DEFAULTS_JSON
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 最小结构校验（缺任何顶层键就当坏数据用 fallback）
                if isinstance(data, dict) and "ui" in data and "persona" in data:
                    return data
        except Exception:
            pass
        return _LLM_FALLBACK_DATA()

    @staticmethod
    def _persona_presets() -> list[dict]:
        """3 套人设预设（优先从 JSON 取，失败 fallback；自称/称呼由 forced_self/forced_you 决定）。"""
        data = LLMSettingsDialog._load_llm_defaults()
        try:
            presets = data["persona"]["presets"]
            if isinstance(presets, list) and len(presets) >= 3:
                return list(presets)
        except Exception:
            pass
        return _LLM_FALLBACK_DATA()["persona"]["presets"]

    @staticmethod
    def _persona_forced_names() -> tuple[str, str]:
        """返回 (forced_self, forced_you)（从 JSON 或 fallback 取）。"""
        data = LLMSettingsDialog._load_llm_defaults()
        try:
            p = data["persona"]
            return str(p.get("forced_self", "咱")), str(p.get("forced_you", "汝"))
        except Exception:
            return "咱", "汝"

    @staticmethod
    def _ui_defaults() -> tuple[str, bool]:
        """返回 (theme, topmost)（从 JSON 或 fallback 取）。"""
        data = LLMSettingsDialog._load_llm_defaults()
        try:
            u = data["ui"]
            return str(u.get("theme", "day")), bool(u.get("topmost", True))
        except Exception:
            return "day", True

    @staticmethod
    def _runtime_flags() -> tuple[bool, bool]:
        """返回 (stream_output, skip_ssl_verify)（从 JSON 或 fallback 取，UI 已删除勾选框）。"""
        data = LLMSettingsDialog._load_llm_defaults()
        try:
            u = data["ui"]
            return (
                bool(u.get("stream_output", True)),
                bool(u.get("skip_ssl_verify", True)),
            )
        except Exception:
            return True, True

    @staticmethod
    def _blueprint_defaults() -> tuple[bool, bool, int]:
        """返回 (validate_blueprint, autosave_draft, max_attempts)（从 JSON 或 fallback 取，UI 已删除该设置）。"""
        data = LLMSettingsDialog._load_llm_defaults()
        try:
            b = data["blueprint"]
            return (
                bool(b.get("validate_blueprint", True)),
                bool(b.get("autosave_draft", True)),
                max(1, int(b.get("max_attempts", 3))),
            )
        except Exception:
            return True, True, 3

    def _collect_persona_selections(self) -> Dict[str, Any]:
        """基于预设下拉 idx，硬编码返回 3 套人设数据（所有参数不可手动修改）。"""
        idx = 0
        if hasattr(self, "cmb_preset") and self.cmb_preset is not None:
            i = self.cmb_preset.currentIndex()
            all_presets = type(self)._persona_presets()
            if 0 <= i < len(all_presets): idx = i
        p = type(self)._persona_presets()[idx]
        forced_self, forced_you = type(self)._persona_forced_names()
        return {
            "preset_idx":  p.get("preset_idx", idx),
            "preset_name": p.get("preset_name", f"预设{idx+1}"),
            "trait_idx":   p.get("trait_idx", 0),
            "trait_name":  p.get("trait_name", ""),
            "self": forced_self,
            "you":  forced_you,
            "traits":       p.get("traits", ""),
            "greeting":     p.get("greeting", ""),
            "system_prompt": p.get("system_prompt", ""),
        }

    def _apply_persona_preset(self, idx: int):
        """下拉预设变化 → 仅更新 lbl_persona_info 展示卡（UI里性格/称呼/关键字/问候/指令均已删除，不再可改）。"""
        presets = type(self)._persona_presets()
        if idx < 0 or idx >= len(presets):
            return
        p = presets[idx]
        if hasattr(self, "lbl_persona_info") and self.lbl_persona_info is not None:
            import html
            forced_self, forced_you = type(self)._persona_forced_names()
            g = html.escape(str(p.get("greeting", "")).replace("\n", "  \n"))
            sp = html.escape(str(p.get("system_prompt", "")))
            self.lbl_persona_info.setText(
                f"<div style='margin:0 0 4px 0;'><b>🎭 预设：</b>{html.escape(str(p.get('preset_name','')))}　<b>✨ 性格：</b>{html.escape(str(p.get('trait_name','')))}　"
                f"<b>🐺 自称：</b>{html.escape(forced_self)}　<b>🗣 称呼汝：</b>{html.escape(forced_you)}</div>"
                f"<div style='margin:0 0 4px 0;'><b>🍎 性格关键字：</b>{html.escape(str(p.get('traits','')))}</div>"
                f"<div style='margin:0 0 4px 0;'><b>👋 初始问候：</b><br/><span style='color:#8b7355;'>{g}</span></div>"
                f"<div style='margin:0;'><b>🧠 人设指令（System Prompt）：</b><br/><span style='color:#8b7355;'>{sp}</span></div>"
            )

    # =================================================================
    # Tab 4：🧠 记忆库
    # =================================================================
    def _build_memory_tab(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(12); root.setContentsMargins(12, 12, 12, 12)

        # 顶部：3 个入口卡片（横向一行，占满宽度，而不是挤在左侧窄列）
        entry_row = QHBoxLayout(); entry_row.setSpacing(12)
        def _entry_card(icn: str, title: str, desc: str, handler) -> QFrame:
            f = QFrame(); f.setObjectName("holoCard")
            f.setCursor(Qt.PointingHandCursor)
            f.setMinimumHeight(88)
            f.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
                f" border-radius:16px; }} #holoCard:hover {{ border:2px solid {_THEME['primary']}; }}"
            )
            l = QHBoxLayout(f); l.setContentsMargins(14, 12, 14, 12); l.setSpacing(12)
            pm = pixmap(icn, 44)
            if not pm.isNull():
                a = QLabel(); a.setPixmap(pm); a.setFixedSize(44, 44); l.addWidget(a, 0, Qt.AlignTop)
            col = QVBoxLayout(); col.setSpacing(4)
            t1 = QLabel(title); t1.setStyleSheet(f"background:transparent; color:{_THEME['maid_black']}; font-weight:900; font-size:14px;")
            t2 = QLabel(desc); t2.setWordWrap(True); t2.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:12px; line-height:1.5;")
            col.addWidget(t1); col.addWidget(t2, 1)
            l.addLayout(col, 1)
            f.mousePressEvent = lambda _: handler()
            return f

        entry_row.addWidget(_entry_card(
            "icon_history.png", "👤 用户档案",
            "昵称 / 喜好 / 常用软件 / 习惯称呼",
            self._focus_profile
        ), 1)
        entry_row.addWidget(_entry_card(
            "icon_memory.png", "🐺 全局人设",
            "赫萝底层人设（固定自称「咱」）\n身份 / 性格 / 称呼你 / 系统指令",
            lambda: None
        ), 1)
        entry_row.addWidget(_entry_card(
            "icon_game.png", "🎮 常玩游戏配置",
            "每个游戏的启动路径、找色模板目录\n宏库 / 截图识别区 / 一键连招",
            self._focus_games
        ), 1)
        root.addLayout(entry_row)

        # ---- §4.3 记忆 Tab：「记住哪些」多选框组 ----
        g_rem = QGroupBox("🔖  记忆开关（每次和赫萝聊天自动附带这些信息）")
        r_v = QVBoxLayout(g_rem); r_v.setSpacing(10); r_v.setContentsMargins(14, 18, 14, 14)
        rem_grid = QGridLayout(); rem_grid.setSpacing(10); rem_grid.setContentsMargins(0, 4, 0, 4)
        def _rem_chk(text: str, hint: str, default: bool = True) -> QCheckBox:
            cb = QCheckBox(text); cb.setCursor(Qt.PointingHandCursor)
            cb.setChecked(default)
            cb.setToolTip(hint)
            cb.setStyleSheet(f"QCheckBox {{ background:transparent; color:{_THEME['text_main']}; font-weight:700; font-size:13px; padding:2px 0; }}")
            return cb

        self.chk_remember_window = _rem_chk(
            "🪟  记住窗口位置",
            "主窗 / 悬浮球 / 气泡上次停留的位置和大小，下次打开自动还原",
            True
        )
        self.chk_remember_games = _rem_chk(
            "🎮  记住常玩游戏",
            "自动识别游戏窗口标题，加载对应模板 / 宏库 / 快捷键方案",
            True
        )
        self.chk_remember_hot = _rem_chk(
            "🎹  记住常用快捷键",
            "用户配置的一键热键历史，下次配新热键时自动预填推荐",
            True
        )
        self.chk_remember_ctx = _rem_chk(
            "💬  记住会话上下文",
            "重新打开气泡时继续上次的话题，不会每次都从头聊起",
            True
        )
        rem_items = [
            (self.chk_remember_window, "还原窗口 / 悬浮球 / 气泡位置", 0, 0),
            (self.chk_remember_games,  "自动加载游戏模板与宏",           0, 1),
            (self.chk_remember_hot,    "预填一键热键推荐",               1, 0),
            (self.chk_remember_ctx,    "继续上次未聊完的话题",           1, 1),
        ]
        for cb, hint_txt, r, c in rem_items:
            card = QFrame(); card.setObjectName("holoCard")
            card.setCursor(Qt.PointingHandCursor)
            card.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
                f" border-radius:14px; }}"
            )
            ch = QVBoxLayout(card); ch.setContentsMargins(12, 10, 12, 10); ch.setSpacing(4)
            ch.addWidget(cb)
            hl = QLabel(hint_txt); hl.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:11px;")
            ch.addWidget(hl)
            target3 = cb
            card.mousePressEvent = lambda _e, c=target3: c.setChecked(not c.isChecked())
            def _upd3(on, c=card):
                c.setStyleSheet(
                    f"#holoCard {{ background:{'#FFF5ED' if on else _THEME['bg_card']};"
                    f" border:1px solid {_THEME['primary'] if on else _THEME['border']};"
                    f" border-radius:14px; }}"
                )
            cb.toggled.connect(_upd3)
            rem_grid.addWidget(card, r, c)
        r_v.addLayout(rem_grid)

        # ---- 记忆库专用：导入 / 导出 JSON 按钮 ----
        mbtns = QHBoxLayout(); mbtns.setSpacing(12); mbtns.setContentsMargins(0, 4, 0, 0)
        self.btn_mem_export = QPushButton("  📤  记忆导出 JSON")
        self.btn_mem_export.setIcon(icon("icon_upload.png", 16)); self.btn_mem_export.setIconSize(QSize(16, 16))
        self.btn_mem_export.setCursor(Qt.PointingHandCursor)
        _apply_control_h(self.btn_mem_export, 36)
        self.btn_mem_export.clicked.connect(self._on_export_memory_json)
        self.btn_mem_import = QPushButton("  📥  记忆导入 JSON")
        self.btn_mem_import.setIcon(icon("icon_import.png", 16)); self.btn_mem_import.setIconSize(QSize(16, 16))
        self.btn_mem_import.setCursor(Qt.PointingHandCursor)
        _apply_control_h(self.btn_mem_import, 36)
        self.btn_mem_import.clicked.connect(self._on_import_memory_json)
        mbtns.addStretch(1); mbtns.addWidget(self.btn_mem_export); mbtns.addWidget(self.btn_mem_import)
        r_v.addLayout(mbtns)
        root.addWidget(g_rem)

        # ---- 用户档案表单（不再包在 holoCard 里，直接用 GroupBox 分组，和其他 Tab 风格统一）----
        g_profile = QGroupBox("👤  用户档案")
        rv = QVBoxLayout(g_profile); rv.setSpacing(10); rv.setContentsMargins(14, 18, 14, 14)
        p_tip = QLabel("这些资料会作为 System Prompt 注入给 LLM，帮赫萝更懂你"); p_tip.setWordWrap(True)
        p_tip.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:12px; padding-left:2px;")
        rv.addWidget(p_tip)

        r1 = _row()
        r1.addWidget(_label("昵称"))
        self.ed_profile_name = _apply_control_h(QLineEdit("歪比巴卜"))
        r1.addWidget(self.ed_profile_name, 1)
        r1.addSpacing(16)
        r1.addWidget(_label("性别"))
        self.cmb_profile_sex = _apply_control_h(NoWheelComboBox())
        self.cmb_profile_sex.addItems(["男", "女", "保密"])
        r1.addWidget(self.cmb_profile_sex, 1)
        rv.addLayout(r1)

        r2 = _row()
        r2.addWidget(_label("职业 / 身份"))
        self.ed_profile_job = _apply_control_h(QLineEdit())
        self.ed_profile_job.setPlaceholderText("学生 / 程序员 / 策划 / 歪比巴卜…")
        r2.addWidget(self.ed_profile_job, 1)
        r2.addSpacing(16)
        r2.addWidget(_label("擅长领域"))
        self.ed_profile_skill = _apply_control_h(QLineEdit())
        self.ed_profile_skill.setPlaceholderText("Python / 自动化 / 游戏 / 写作…")
        r2.addWidget(self.ed_profile_skill, 1)
        rv.addLayout(r2)

        # 常用游戏 + 其他上下文：用两行 GroupBox 内的子标题 + 控件，保持信息紧凑
        g_lbl_3 = QHBoxLayout(); g_lbl_3.setSpacing(6); g_lbl_3.setAlignment(Qt.AlignVCenter)
        pm_g = pixmap("icon_game.png", 18)
        if not pm_g.isNull():
            ic_g = QLabel(); ic_g.setPixmap(pm_g); ic_g.setFixedSize(18, 18); g_lbl_3.addWidget(ic_g)
        gl3 = _label("常用游戏", fixed_w=0); gl3.setStyleSheet(
            f"background:transparent; color:{_THEME['primary_dark']}; font-weight:800; font-size:13px;")
        g_lbl_3.addWidget(gl3)
        gh3 = QLabel("每行一个，未来赫萝会自动识别窗口加载对应宏"); gh3.setStyleSheet(
            f"background:transparent; color:{_THEME['text_sub']}; font-size:12px;")
        g_lbl_3.addWidget(gh3, 1); rv.addLayout(g_lbl_3)
        self.ed_profile_games = QPlainTextEdit()
        self.ed_profile_games.setFixedHeight(72)
        self.ed_profile_games.setPlaceholderText("原神\歪比巴卜\XBOX\n…")
        rv.addWidget(self.ed_profile_games)

        g_lbl_4 = QHBoxLayout(); g_lbl_4.setSpacing(6); g_lbl_4.setAlignment(Qt.AlignVCenter)
        pm_x = pixmap("icon_emoji.png", 18)
        if not pm_x.isNull():
            ic_x = QLabel(); ic_x.setPixmap(pm_x); ic_x.setFixedSize(18, 18); g_lbl_4.addWidget(ic_x)
        gl4 = _label("其他想说给赫萝的", fixed_w=0); gl4.setStyleSheet(
            f"background:transparent; color:{_THEME['primary_dark']}; font-weight:800; font-size:13px;")
        g_lbl_4.addWidget(gl4)
        gh4 = QLabel("额外上下文，赫萝会记住这些细节（习惯 / 偏好 / 禁忌等）"); gh4.setStyleSheet(
            f"background:transparent; color:{_THEME['text_sub']}; font-size:12px;")
        g_lbl_4.addWidget(gh4, 1); rv.addLayout(g_lbl_4)
        self.ed_profile_extra = QPlainTextEdit()
        self.ed_profile_extra.setFixedHeight(80)
        self.ed_profile_extra.setPlaceholderText(
            "示例：我每天 18 点下班 / 快捷键习惯用 Ctrl+Shift+… / 不想看到太多废话 / 等等"
        )
        rv.addWidget(self.ed_profile_extra)
        root.addWidget(g_profile)

        # 常玩游戏配置列表
        g_games = QGroupBox("🎮  常玩游戏配置（Stub · 编辑后生效）")
        gv = QVBoxLayout(g_games); gv.setSpacing(10); gv.setContentsMargins(14, 18, 14, 14)
        self.list_games = QListWidget()
        for n in ["原神 · Genshin Impact", "歪比巴卜 · 比比拉布", "记事本（示例 · Notepad）"]:
            self.list_games.addItem(f"  🎮  {n}")
        self.list_games.setMinimumHeight(88)
        gv.addWidget(self.list_games, 1)
        gr = QHBoxLayout(); gr.setSpacing(10)
        badd = QPushButton(" ➕ 新增"); badd.setIcon(icon("icon_import.png", 16))
        from PySide6.QtCore import QSize as _QS
        badd.setIconSize(_QS(16, 16)); _apply_control_h(badd, 36); badd.clicked.connect(self._add_game_stub)
        bedit = QPushButton(" ✏️ 编辑"); bedit.setIcon(icon("icon_code.png", 16)); bedit.setIconSize(_QS(16, 16))
        _apply_control_h(bedit, 36)
        bedit.clicked.connect(lambda: QMessageBox.information(self, "编辑游戏（Stub）", "赫萝：这部分以后可以配置启动路径、截图模板目录哦~ 🍎"))
        bdel = QPushButton(" 🗑 删除"); bdel.setIcon(icon("icon_clear.png", 16)); bdel.setIconSize(_QS(16, 16))
        _apply_control_h(bdel, 36); bdel.clicked.connect(self._del_game_stub)
        gr.addWidget(badd); gr.addWidget(bedit); gr.addWidget(bdel); gr.addStretch(1)
        gv.addLayout(gr)
        root.addWidget(g_games)

        return w

    def _focus_profile(self):
        self.tabs.setCurrentIndex(3)
        try: self.ed_profile_name.setFocus()
        except Exception: pass

    def _focus_games(self):
        self.tabs.setCurrentIndex(3)
        try: self.list_games.setFocus()
        except Exception: pass

    def _add_game_stub(self):
        self.list_games.addItem("  🎮  新游戏（Stub · 编辑后生效）")

    def _del_game_stub(self):
        it = self.list_games.currentItem()
        if it: self.list_games.takeItem(self.list_games.row(it))

    # ---- §4.3 记忆库 Tab：记忆导入/导出 JSON（写 llm.memory 节点）----
    def _on_export_memory_json(self):
        try:
            import json, datetime
            from PySide6.QtWidgets import QFileDialog
            data = {
                "_meta": {
                    "schema": "llm.memory",
                    "version": 1,
                    "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
                },
                "remember": {
                    "window": self.chk_remember_window.isChecked() if hasattr(self, "chk_remember_window") else True,
                    "games":  self.chk_remember_games.isChecked()  if hasattr(self, "chk_remember_games")  else True,
                    "hotkeys":self.chk_remember_hot.isChecked()    if hasattr(self, "chk_remember_hot")    else True,
                    "session_ctx": self.chk_remember_ctx.isChecked() if hasattr(self, "chk_remember_ctx")  else True,
                },
                "persona": (self._collect_persona_selections()
                            if hasattr(self, "_collect_persona_selections")
                            else {}),
                "profile": {
                    "name": self.ed_profile_name.text(),
                    "sex":  self.cmb_profile_sex.currentText(),
                    "job":  self.ed_profile_job.text(),
                    "skill":self.ed_profile_skill.text(),
                    "games_text": self.ed_profile_games.toPlainText(),
                    "extra": self.ed_profile_extra.toPlainText(),
                    "games_list": [self.list_games.item(i).text().strip()
                                   for i in range(self.list_games.count())],
                },
            }
            path, _ = QFileDialog.getSaveFileName(
                self, "导出记忆 JSON（llm.memory 节点）",
                f"holo_memory_{datetime.date.today().isoformat()}.json",
                "JSON (*.json)",
            )
            if not path: return
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(
                self, "✅ 记忆导出成功",
                f"赫萝：唔姆~ 记忆已打包成 1 个苹果 JSON 啦 🍎\n\n路径：{path}\n共 {len(data)} 个分组（meta/remember/persona/profile）。"
            )
        except Exception as e:
            QMessageBox.critical(self, "❌", f"赫萝：唔？记忆导出失败了：{e}")

    def _on_import_memory_json(self):
        try:
            import json
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "导入记忆 JSON（llm.memory 节点）", "", "JSON (*.json)"
            )
            if not path: return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 回填各字段（有则填，没有跳过）
            if "remember" in data:
                r = data["remember"]
                if hasattr(self, "chk_remember_window"): self.chk_remember_window.setChecked(bool(r.get("window", True)))
                if hasattr(self, "chk_remember_games"):  self.chk_remember_games.setChecked(bool(r.get("games",  True)))
                if hasattr(self, "chk_remember_hot"):    self.chk_remember_hot.setChecked(bool(r.get("hotkeys", True)))
                if hasattr(self, "chk_remember_ctx"):    self.chk_remember_ctx.setChecked(bool(r.get("session_ctx", True)))
            # ⚠ 赫萝人设所有参数（性格/称呼/关键字/问候/指令）现已完全写死，不再从导入 JSON 回填
            # （用户要求：只能通过预设下拉切换，不可手动改或导入覆写）
            if "profile" in data:
                pr = data["profile"]
                if "name"  in pr: self.ed_profile_name.setText(str(pr["name"]))
                if "sex"   in pr:
                    i = self.cmb_profile_sex.findText(str(pr["sex"]))
                    if i >= 0: self.cmb_profile_sex.setCurrentIndex(i)
                if "job"   in pr: self.ed_profile_job.setText(str(pr["job"]))
                if "skill" in pr: self.ed_profile_skill.setText(str(pr["skill"]))
                if "games_text" in pr: self.ed_profile_games.setPlainText(str(pr["games_text"]))
                if "extra" in pr: self.ed_profile_extra.setPlainText(str(pr["extra"]))
                if "games_list" in pr and isinstance(pr["games_list"], list):
                    self.list_games.clear()
                    for g in pr["games_list"]:
                        if isinstance(g, str) and g.strip():
                            pfx = "" if g.startswith("🎮") else "  🎮  "
                            self.list_games.addItem(pfx + g.strip())
            QMessageBox.information(
                self, "✅ 记忆导入成功",
                f"赫萝：唔姆~ 记忆吸收完毕 🍎\n\n文件：{path}\n（人设/记住哪些/用户档案/常玩游戏 全部回填。）"
            )
        except Exception as e:
            QMessageBox.critical(self, "❌", f"赫萝：唔？记忆导入失败了：{e}")

    # =================================================================
    # Tab 5：使用说明
    # =================================================================
    def _build_usage_tab(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(12); root.setContentsMargins(12, 12, 12, 12)

        # 顶部欢迎条（横向铺满：赫萝大图 + 欢迎语 + 小贴士，更符合宽屏阅读）
        top_bar = QFrame(); top_bar.setObjectName("holoCard")
        top_bar.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
            f" border-radius:24px; }}"
        )
        tb = QHBoxLayout(top_bar); tb.setContentsMargins(18, 14, 18, 14); tb.setSpacing(18)

        # 赫萝大图（左侧）
        big_avatar = QLabel(); big_avatar.setAlignment(Qt.AlignCenter)
        apm = pixmap("avatar_holo_bigapple.png", 112)
        if not apm.isNull():
            from PySide6.QtGui import QPainter as _QP2, QPainterPath as _QPP2, QPen as _QPen2, QColor as _QCol2
            out = QPixmap(112, 112); out.fill(Qt.transparent)
            qp = _QP2(out); qp.setRenderHint(_QP2.Antialiasing)
            pth = _QPP2(); pth.addEllipse(0, 0, 112, 112); qp.setClipPath(pth)
            # 原图人物偏左，整体向右平移 6px，让脸/主体落在圆形视觉中心
            qp.drawPixmap(6, 0, apm); qp.setClipping(False)
            qp.setPen(_QPen2(_QCol2(_THEME["primary"]), 3)); qp.setBrush(Qt.NoBrush)
            qp.drawEllipse(0, 0, 111, 111); qp.end()
            big_avatar.setPixmap(out); big_avatar.setFixedSize(112, 112)
        tb.addWidget(big_avatar, 0, Qt.AlignTop)

        # 欢迎语 + 小贴士（右侧两栏并排）
        welcome_col = QVBoxLayout(); welcome_col.setSpacing(10)
        wl_title = QLabel("📖  赫萝女仆助理 · 使用指南")
        wl_title.setStyleSheet(f"background:transparent; color:{_THEME['primary_dark']}; font-size:20px; font-weight:900;")
        welcome_col.addWidget(wl_title)
        wl_tip = QLabel("唔姆~ 苹果商人，咱把使用说明整理在这里啦，按 6 步走就能用得很顺手哦 🍎")
        wl_tip.setWordWrap(True); wl_tip.setStyleSheet(f"background:transparent; color:{_THEME['text_main']}; font-weight:600; font-size:13px; line-height:1.6;")
        welcome_col.addWidget(wl_tip)

        # 小贴士卡片组（2x2 网格，紧凑）
        tips_grid = QGridLayout(); tips_grid.setSpacing(8); tips_grid.setContentsMargins(0, 4, 0, 0)
        quick_tips = [
            ("🖱 左键悬浮球", "快速问问题"),
            ("🖱 右键悬浮球", "切换心情 / 打开大窗"),
            ("📋 双击回复文本", "一键复制内容"),
            ("🔄 长按重算按钮", "让赫萝重新生成"),
        ]
        for _i, (qt_title, qt_desc) in enumerate(quick_tips):
            mini = QFrame(); mini.setObjectName("holoCard")
            mini.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_root']}; border:1px dashed {_THEME['primary']};"
                f" border-radius:14px; }}"
            )
            mv = QVBoxLayout(mini); mv.setContentsMargins(10, 8, 10, 8); mv.setSpacing(2)
            mt = QLabel(qt_title); mt.setStyleSheet(
                f"background:transparent; color:{_THEME['primary_dark']}; font-weight:800; font-size:12px;")
            mv.addWidget(mt)
            md = QLabel(qt_desc); md.setStyleSheet(
                f"background:transparent; color:{_THEME['text_sub']}; font-size:11px;")
            mv.addWidget(md)
            tips_grid.addWidget(mini, _i // 2, _i % 2)
        welcome_col.addLayout(tips_grid)
        tb.addLayout(welcome_col, 1)
        root.addWidget(top_bar)

        # 分步使用说明（6 步，统一卡片高度，视觉更整齐）
        title_s = QLabel("🧭  6 步上手"); title_s.setStyleSheet(
            f"background:transparent; color:{_THEME['maid_black']}; font-size:16px; font-weight:900; padding-left:4px;")
        root.addWidget(title_s)
        steps_col = QVBoxLayout(); steps_col.setSpacing(10)
        steps = [
            ("1", "🐺", "启用赫萝",
             "顶部总开关勾选「启用 LLM 女仆功能」，桌面就会冒出悬浮球。不想被打扰时随时关闭即可 🍎"),
            ("2", "🤖", "配好模型",
             "切到「模型服务商」Tab，填 Base URL、API Key，点右侧「🔎 拉取模型列表」自动读取可用对话模型。仅保留「OpenAI」（含所有兼容实现）和「Ollama」两大类服务商 💡"),
            ("3", "🎭", "调教人设",
             "切到「赫萝人设」Tab，选择预设风格（女仆赫萝 / 游戏宅赫萝 / 贤狼赫萝）+ 性格关键字 + 称呼你；自称固定「咱」🐺🍎"),
            ("4", "💬", "开始聊天",
             "左键点桌面悬浮球弹出小气泡快速提问；右键悬浮球选择「打开大窗口」进入完整版 8 Tab 对话界面。"),
            ("5", "🧠", "写入记忆",
             "切到「记忆库」Tab 填写用户档案和常玩游戏，赫萝会把这些信息作为上下文，下次聊天无需重新介绍自己。"),
            ("6", "📋", "生成蓝图",
             "直接告诉赫萝你的自动化需求，例如「写一个打开记事本、等 2 秒、输入 Hello 的蓝图」，一键导入到画布即可运行！"),
        ]
        _STEP_CARD_MIN_H = 82
        for idx, ico, sub, desc in steps:
            card = QFrame(); card.setObjectName("holoCard")
            card.setMinimumHeight(_STEP_CARD_MIN_H)
            card.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
                f" border-radius:18px; }}"
                f"#holoCard:hover {{ border:2px solid {_THEME['primary']}; }}"
            )
            vv = QHBoxLayout(card); vv.setContentsMargins(14, 12, 14, 12); vv.setSpacing(14)
            # 序号徽章（更大更醒目）
            badge = QLabel(idx); badge.setFixedSize(44, 44); badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                f"background:{_THEME['primary']}; color:{_THEME['text_on_primary']};"
                f" border-radius:22px; font-weight:900; font-size:18px;"
            )
            vv.addWidget(badge, 0, Qt.AlignVCenter)
            # 标题 + 内容
            cc = QVBoxLayout(); cc.setSpacing(4)
            hh = QLabel(f"{ico}   {sub}"); hh.setStyleSheet(f"background:transparent; color:{_THEME['maid_black']}; font-weight:900; font-size:15px;")
            dd = QLabel(desc); dd.setWordWrap(True); dd.setStyleSheet(f"background:transparent; color:{_THEME['text_sub']}; font-size:13px; line-height:1.65;")
            cc.addWidget(hh); cc.addWidget(dd, 1); vv.addLayout(cc, 1)
            steps_col.addWidget(card)
        root.addLayout(steps_col, 1)
        return w

    # =================================================================
    # 默认值 / 事件 / 导入导出
    # =================================================================
    def load_defaults(self):
        self.chk_enable.setChecked(True)
        self.cmb_provider.setCurrentIndex(0)
        self.ed_baseurl.setText("https://api.openai.com/v1")
        self.ed_apikey.setText("")
        self.cmb_model.setCurrentText("gpt-4o-mini")
        self.spn_ctx.setValue(16384)
        self.spn_out.setValue(2048)
        # ⚠ 已删除 chk_stream / chk_ssl 勾选框，统一在 config/llm_defaults.json 里配置：
        #   ui.stream_output=true 启用流式输出；ui.skip_ssl_verify=true 默认跳过 SSL 校验
        # 人设默认预设
        self.cmb_preset.setCurrentIndex(0)
        self._apply_persona_preset(0)
        # ⚠ 已删除蓝图输出设置的 chk_validate/chk_autosave/spn_attempts 控件，
        #   统一从 config/llm_defaults.json 的 blueprint 域读取（默认校验开/自动草稿开/最大尝试3次）
        # theme / topmost：不再暴露 UI，直接写死默认值
        #  · 主题默认奶茶白日（day）
        #  · 悬浮球固定置顶（Win32 全屏也可见）

    def _collect(self) -> Dict[str, Any]:
        # slider 辅助
        def _val(s: QSlider, lo=0.0, step=0.05): return round(lo + s.value() * step, 3)
        stream_out, skip_ssl = type(self)._runtime_flags()
        val_bp, auto_bp, max_at = type(self)._blueprint_defaults()
        return {
            "enable": self.chk_enable.isChecked(),
            "provider": self.cmb_provider.currentText(),
            "base_url": self.ed_baseurl.text().strip(),
            "api_key": self.ed_apikey.text().strip(),
            "model": self.cmb_model.currentText().strip(),
            "max_ctx": self.spn_ctx.value(),
            "max_out": self.spn_out.value(),
            "stream": stream_out,      # 从 config/llm_defaults.json 的 ui.stream_output 读（默认 true），UI已删除勾选框
            "skip_ssl": skip_ssl,      # 从 config/llm_defaults.json 的 ui.skip_ssl_verify 读（默认 true），UI已删除勾选框
            "temperature": _val(self.sl_temp, 0.0, 0.05),
            "top_p":       _val(self.sl_top,  0.0, 0.01),
            "freq_pen":    _val(self.sl_freq, -2.0, 0.05),
            "pres_pen":    _val(self.sl_pres, -2.0, 0.05),
            "validate_blueprint": val_bp,  # 从 config/llm_defaults.json 的 blueprint.validate_blueprint 读（默认 true），UI已删除
            "autosave_draft":     auto_bp, # 从 config/llm_defaults.json 的 blueprint.autosave_draft   读（默认 true），UI已删除
            "max_attempts":       max_at,  # 从 config/llm_defaults.json 的 blueprint.max_attempts     读（默认 3），UI已删除
            "theme": type(self)._ui_defaults()[0],   # 从 config/llm_defaults.json 读（默认奶茶白日），UI已删除
            "topmost": type(self)._ui_defaults()[1], # 从 config/llm_defaults.json 读（悬浮球固定置顶），UI已删除
            "persona": (lambda: self._collect_persona_selections()
                        if hasattr(self, "cmb_preset") and self.cmb_preset is not None
                        else (lambda: (
                            dict(type(self)._persona_presets()[0],
                                 **dict(zip(["self","you"], type(self)._persona_forced_names())))
                        ))())(),
            "memory": {
                "remember_window": self.chk_remember_window.isChecked() if hasattr(self, "chk_remember_window") else True,
                "remember_games":  self.chk_remember_games.isChecked()  if hasattr(self, "chk_remember_games")  else True,
                "remember_hotkeys":self.chk_remember_hot.isChecked()    if hasattr(self, "chk_remember_hot")    else True,
                "remember_ctx":    self.chk_remember_ctx.isChecked()    if hasattr(self, "chk_remember_ctx")    else True,
            },
            "profile": {
                "name":   self.ed_profile_name.text(),
                "sex":    self.cmb_profile_sex.currentText(),
                "job":    self.ed_profile_job.text(),
                "skill":  self.ed_profile_skill.text(),
                "games":  self.ed_profile_games.toPlainText(),
                "extra":  self.ed_profile_extra.toPlainText(),
                "games_list": [self.list_games.item(i).text().strip() for i in range(self.list_games.count())],
            },
        }

    def _on_save(self):
        data = self._collect()
        self.settings_saved.emit(dict(data))
        # ⚠ 保存时也同步一次 enable 状态（避免用户在设置里开关后忘记保存时悬浮球状态不同步）
        try:
            self.llm_enabled_changed.emit(bool(data.get("enable", True)))
        except Exception:
            pass
        QMessageBox.information(
            self, "💾 已保存（打桩）",
            f"赫萝：收到收到~ 咱记住啦 🍎\n\n（Stub：写入 user_settings.llm，共 {len(data)} 项。\n"
            f"主题：{data['theme']}；人设：{data['persona']['self']}/{data['persona']['you']}）"
        )
        self.accept()

    def _on_fetch_models(self):
        """根据服务商类型调用 /models 或 /api/tags 拉取可用模型列表，填进对话模型下拉框。"""
        provider_idx = self.cmb_provider.currentIndex() if hasattr(self.cmb_provider, "currentIndex") else 0
        base = (self.ed_baseurl.text() or "").strip()
        if not base:
            QMessageBox.warning(self, "提示", "赫萝：Base URL 还没填呢，先告诉咱接口地址呀~")
            return
        base = base.rstrip("/")
        try:
            import json as _json, urllib.request as _req
            from urllib.error import URLError, HTTPError
            ssl_ctx = None
            # SSL 校验行为统一从 config/llm_defaults.json 的 ui.skip_ssl_verify 读取（默认 true = 跳过）
            _skip_ssl = bool(type(self)._runtime_flags()[1])
            if _skip_ssl:
                import ssl; ssl_ctx = ssl._create_unverified_context()
            headers = {}
            if provider_idx == 1:
                # Ollama（index 固定为 1）：GET http://host:port/api/tags
                # Ollama 一般写 http://localhost:11434 或 /v1，这里兼容两种写法
                if base.endswith("/v1"):
                    base = base[:-3].rstrip("/")
                url = base + "/api/tags"
                r = _req.Request(url, headers=headers, method="GET")
                try:
                    with _req.urlopen(r, timeout=8, context=ssl_ctx) as resp:
                        data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
                    names = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
                    names = [n for n in names if n]
                except (URLError, HTTPError, OSError) as e:
                    raise RuntimeError(f"Ollama 接口访问失败：{e}")
            else:
                # OpenAI 兼容：GET {base_url}/models（Authorization: Bearer {key})
                api_key = (self.ed_apikey.text() or "").strip()
                # ⚠ 用户明确要求：API Key 为空时禁止读环境变量；没配置就阻断（不允许降级到 env）
                if not api_key:
                    QMessageBox.warning(
                        self, "提示",
                        "赫萝：API Key 还没填哦~ OpenAI 类服务商必须填 Key 才能拉模型列表，咱不会偷偷读环境变量哒 🐺🍎"
                    )
                    return
                headers["Authorization"] = f"Bearer {api_key}"
                if not base.endswith("/models") and not base.endswith("/v1"):
                    # 形如 https://xxx.xx 直接加 /v1/models 更稳
                    url = base + "/v1/models"
                elif base.endswith("/v1"):
                    url = base + "/models"
                else:
                    url = base  # 已经是 .../models 结尾就直接用
                r = _req.Request(url, headers=headers, method="GET")
                try:
                    with _req.urlopen(r, timeout=12, context=ssl_ctx) as resp:
                        data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
                        names = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
                        names = [n for n in names if n]
                except HTTPError as e:
                    if e.code == 401:
                        raise RuntimeError("鉴权失败（401 Unauthorized）：API Key 错了或没填哦~")
                    if e.code == 404:
                        raise RuntimeError(f"接口不存在（404）：请确认 Base URL 是否正确（当前：{url}）")
                    raise RuntimeError(f"HTTP {e.code}：{e.reason}")
                except (URLError, OSError) as e:
                    raise RuntimeError(f"接口访问失败：{e}")
        except ImportError as e:
            QMessageBox.critical(self, "拉取失败", f"赫萝：唔？导入 Python 标准库缺模块啦：{e}"); return
        except Exception as e:
            QMessageBox.critical(self, "拉取失败", f"赫萝：唔姆…拉取不到模型列表呀~🍎\n\n{e}"); return

        # 填进下拉框（保留用户已有的去重 + 按字符串排序）
        if not names:
            QMessageBox.information(self, "拉取完成",
                                    "赫萝：接口通啦~ 但是服务商说当前账号下没有可用模型哦 🐺\n\n（可以手动在对话框模型下拉里自个儿手填模型名吧~）"); return
        existing = {self.cmb_model.itemText(i) for i in range(self.cmb_model.count())}
        # 按长度升序 + 字典序（便宜的小模型通常名字短，排在前面）
        names_sorted = sorted(set(names) - existing, key=lambda s: (len(s), s))
        for n in names_sorted:
            self.cmb_model.addItem(n)
        # 优先选第一个看起来像"好模型"的（gpt-/qwen-/llama-/gemma- 这些）
        prio = ["gpt-", "qwen", "llama", "gemma", "deepseek", "claude"]
        first_pick = next((n for n in names_sorted if any(p in n.lower() for p in prio)), None)
        if first_pick:
            self.cmb_model.setCurrentText(first_pick)
        elif names_sorted:
            self.cmb_model.setCurrentText(names_sorted[0])
        QMessageBox.information(
            self, "✅ 拉取成功",
            f"赫萝：唔姆~ 苹果商人，一共拉到 {len(names)} 个可用模型 🍎\n\n当前服务：{provider}\n已自动填进「对话模型」下拉框里，现在直接选就好~"
        )

    def _on_test_connection(self):
        d = self._collect()
        if not d["base_url"] or not d["model"]:
            QMessageBox.warning(self, "测试失败", "赫萝：Base URL 或 Model 还没填呀，苹果少给一颗哦~")
            return
        QMessageBox.information(
            self, "测试连接（Stub）",
            f"赫萝：唔姆，接口配置没问题~ 连通状态良好 🐺\n\n"
            f"Provider: {d['provider']}\nModel:    {d['model']}\nBase:     {d['base_url']}\n"
            "（真实接入时会发一次 /v1/chat/completions ping。）"
        )

    def _on_export(self):
        fn, _ = QFileDialog.getSaveFileName(self, "导出 LLM 配置", "llm_settings.json", "JSON (*.json)")
        if not fn: return
        try:
            import json
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(self._collect(), f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "导出成功", f"赫萝：配置写好啦~\n📄 {fn}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"赫萝：唔？导出出错了：{e}")

    def _on_import(self):
        fn, _ = QFileDialog.getOpenFileName(self, "导入 LLM 配置", "", "JSON (*.json)")
        if not fn: return
        try:
            import json
            with open(fn, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"赫萝：JSON 读不出来啦：{e}"); return
        # Stub：只回填几个关键字段，避免 import 把结构搞乱
        QMessageBox.information(
            self, "导入（Stub）",
            f"赫萝：配置读取成功，共 {len(d)} 项~\n"
            f"（Stub：真实接入时会把 JSON 回填到 4 个 Tab 的每个字段。）\n"
            f"📄 {fn}"
        )
