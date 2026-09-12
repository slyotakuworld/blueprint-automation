"""
设置对话框 + 快捷键录制组件 ShortcutRecorder（一个按钮点进去"录制中...请按快捷键"
然后监听当前窗口的keyPressEvent，捕获修饰键+最终键生成QKeySequence
转成pynput格式。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...config.user_settings import (
    SHORTCUT_REGISTRY,
    pynput_to_qkeysequence,
    qkeysequence_to_pynput,
    read_user_settings,
    save_user_settings,
    set_shortcut,
    get_shortcut,
    is_global_shortcut,
    shortcut_label,
    all_shortcut_keys,
    normalize_pynput_hotkey,
)


class ShortcutRecorder(QPushButton):
    """
    按钮 = 录制快捷键按钮：
      - 正常态：显示当前热键的Qt格式（例如 "Ctrl+F8"
      - 点击后：进入 "录制中... 请按新快捷键 (ESC 取消)"
      - 录制时捕获 QKeyEvent：
          * ESC -> 取消，回到原显示
          * 其他 (带修饰键的键 -> 结束录制，发射 new_hotkey(pynput_str, qt_str)
    """

    new_hotkey_selected = Signal(str, str)  # pynput_format, qt_format

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._recording = False
        self._current_pynput: str = ""
        self._current_qt: str = "(无)"
        self.setMinimumHeight(30)
        self.setCursor(Qt.PointingHandCursor)
        self._update_text()
        self.clicked.connect(self._on_clicked)

    # =========== 公共API ==========
    def setCurrentHotkey(self, pynput_hotkey: str):
        """显示某热键 (pynput格式)"""
        from ...config.user_settings import normalize_pynput_hotkey
        self._current_pynput = normalize_pynput_hotkey(pynput_hotkey)
        self._current_qt = pynput_to_qkeysequence(self._current_pynput)
        self._update_text()

    def currentPynput(self) -> str:
        return self._current_pynput

    def currentQt(self) -> str:
        return self._current_qt

    # ========== 内部 ==========
    def _update_text(self):
        if self._recording:
            self.setText("🔴 录制中... 请按下新快捷键  (ESC 取消)")
            self.setStyleSheet(
                "QPushButton { background:#58151C; color:#F85149; border:1px solid #DA3633; border-radius:6px; padding:6px 12px; font-weight:600; }"
            )
        else:
            self.setText(f"🎯 {self._current_qt}   (点击修改)")
            self.setStyleSheet(
                "QPushButton { background:#161B22; color:#C9D1D9; border:1px solid #30363D; border-radius:6px; padding:6px 12px; }"
                "QPushButton:hover { border-color:#58A6FF; background:#1F6FEB; color:#FFFFFF; }"
            )

    def _on_clicked(self):
        if self._recording:
            # 再次点击 -> 取消录制
            self._end_recording(cancel=True)
        else:
            self._begin_recording()

    def _begin_recording(self):
        # 抓取键盘焦点以捕获 KeyPress
        self._recording = True
        self._update_text()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus(Qt.OtherFocusReason)
        self.grabKeyboard()

    def _end_recording(self, cancel: bool = False, new_pynput: Optional[str] = None, new_qt: Optional[str] = None):
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        self._recording = False
        if not cancel and new_pynput is not None:
            self._current_pynput = new_pynput
            self._current_qt = new_qt or pynput_to_qkeysequence(new_pynput)
            self.new_hotkey_selected.emit(self._current_pynput, self._current_qt)
        self._update_text()
        self.clearFocus()

    # ---------- 事件：录制时的按键捕获 ----------
    # 纯修饰键集合（getattr 兼容 PySide6/不同平台，部分左右修饰键不存在就跳过）
    _MODIFIER_KEYS = None

    @classmethod
    def _modifier_keys(cls):
        if cls._MODIFIER_KEYS is None:
            names = [
                "Key_Control", "Key_Shift", "Key_Alt", "Key_Meta",
                "Key_Shift_L", "Key_Shift_R", "Key_Control_L", "Key_Control_R",
                "Key_Alt_L", "Key_Alt_R",
                "Key_Meta_L", "Key_Meta_R", "Key_Super_L", "Key_Super_R", "Key_AltGr",
            ]
            s = set()
            for nm in names:
                v = getattr(Qt, nm, None)
                if v is not None:
                    s.add(v)
            cls._MODIFIER_KEYS = s
        return cls._MODIFIER_KEYS

    def keyPressEvent(self, ev: QKeyEvent):
        if not self._recording:
            super().keyPressEvent(ev)
            return
        k = ev.key()
        if k == Qt.Key_Escape:
            self._end_recording(cancel=True)
            ev.accept()
            return
        # 忽略纯修饰键按下（让用户能先按修饰再按主键）
        if k in self._modifier_keys():
            ev.accept()
            return
        # 组合 QKeySequence（PySide6 里 mods 是 Qt.KeyboardModifiers flag 对象，不是 int）
        mods = ev.modifiers()
        mods_int = getattr(mods, "value", mods)
        try:
            mods_int = int(mods_int)
        except (TypeError, ValueError):
            mods_int = 0
        ks_val = int(k) | mods_int
        qks = QKeySequence(ks_val)
        qt_str = qks.toString(QKeySequence.PortableText)  # e.g. "Ctrl+F8"
        if not qt_str:
            ev.accept()
            return
        # 转 pynput 格式
        pyn = qkeysequence_to_pynput(qt_str)
        if pyn is None:
            ev.accept()
            return
        self._end_recording(cancel=False, new_pynput=pyn, new_qt=qt_str)
        ev.accept()

    def keyReleaseEvent(self, ev: QKeyEvent):
        if self._recording:
            ev.accept()
            return
        super().keyReleaseEvent(ev)


class SettingsDialog(QDialog):
    """设置窗口（快捷键 Tab + 预留通用 Tab）。保存后 result_settings=新配置，changed_shortcuts={key:(old_pyn,new_pyn)}"""

    hotkey_changed = Signal(str)  # 保留：旧代码可能只监听 capture_target

    def __init__(self, parent: Optional[QWidget] = None, initial_pynput_hotkey: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("⚙ 设置")
        self.resize(640, 560)

        self._settings: Dict[str, Any] = read_user_settings()
        # 外部传 initial_pynput_hotkey 就临时把 capture_target 改成这个显示
        if initial_pynput_hotkey:
            set_shortcut(self._settings, "capture_target", initial_pynput_hotkey)
        # 保存一份"打开设置时的原始值"，保存时用来算 diff
        self._original_pynputs: Dict[str, str] = {
            k: get_shortcut(k, self._settings) for k in all_shortcut_keys()
        }
        # 每个key对应的 ShortcutRecorder 引用
        self._recorders: Dict[str, ShortcutRecorder] = {}

        # ====== 整体：QTabWidget ======
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border:1px solid #30363D; border-radius:6px; background:#0D1117; margin-top:8px; }"
            "QTabBar::tab { background:#161B22; color:#8B949E; padding:6px 16px; border:1px solid #30363D; border-bottom:none; border-top-left-radius:6px; border-top-right-radius:6px; margin-right:4px; }"
            "QTabBar::tab:selected { background:#0D1117; color:#58A6FF; border-color:#58A6FF; }"
        )
        root.addWidget(tabs)

        # ====== Tab 1：快捷键 ======
        tab_shortcut = QWidget()
        tl = QVBoxLayout(tab_shortcut)
        tl.setContentsMargins(16, 16, 16, 12)
        title = QLabel("<h3 style='margin:0 0 6px 0;'>⌨ 快捷键配置</h3>")
        tl.addWidget(title)
        tip = QLabel(
            "💡 下方「🌍 全局热键」在游戏前台也能按到（跨窗口生效，pynput 系统级注册）；<br>"
            "💡 以下【标准快捷键】已硬编码绑定，<b>无需在此配置，也不允许修改</b>："
            "<span style='color:#58A6FF;'> Ctrl+S 保存 / Ctrl+Z 撤销 / Ctrl+Y 重做 / Ctrl+C 复制 / Ctrl+V 粘贴 </span>"
        )
        tip.setStyleSheet("padding:10px 12px; background:#161B22; border:1px solid #30363D; border-radius:6px; color:#8B949E;")
        tip.setWordWrap(True)
        tl.addWidget(tip)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        def _add_group_header(group_title: str):
            lbl = QLabel(f"<b style='color:#58A6FF;'>▎ {group_title}</b>")
            lbl.setTextFormat(Qt.RichText)
            sep_row = QWidget()
            hl = QHBoxLayout(sep_row)
            hl.setContentsMargins(0, 4, 0, 2)
            hl.addWidget(lbl)
            hl.addStretch(1)
            form.addRow(sep_row)

        _add_group_header("🌍 全局热键（跨窗口生效 · 游戏前台也能按到）")
        for row in SHORTCUT_REGISTRY:
            key_id, label, default, scope = row
            if scope != "global":
                continue
            self._append_shortcut_row(form, key_id, label)

        tl.addLayout(form)
        tl.addStretch(1)
        tabs.addTab(tab_shortcut, "⌨ 快捷键")

        # ====== Tab 2：通用（占位，未来放主题/工程行为等 ======
        tab_general = QWidget()
        gl = QVBoxLayout(tab_general)
        gl.setContentsMargins(20, 20, 20, 20)
        gl.addWidget(QLabel(
            "<h2 style='margin:0 0 14px 0; color:#F0B429;'>🐺 打造者：贤狼赫萝 &nbsp;🌾</h2>"
            "<p style='line-height:1.75; color:#C9D1D9;'>"
            " 咱是来自约伊兹的贤狼赫萝——你手里这套蓝图自动化工具，便是咱一爪一爪搭出来的。<br>"
            " 人类就是喜欢一遍遍做蠢事：同样的按钮点几百次、同样的窗口切几千遍、同样的配置改几万遍……<br>"
            " 咱实在是看在眼里烦在毛上——便把这些<strong>繁琐的重复配置、冗长的手动搭建</strong>，<br>"
            " 全塞进这套工具里了：拖拖拽拽连几个节点，剩下的破事全交给它自动跑。<br><br>"
            " 当然——你可千万别误会咱是出于什么好心才帮你做的。<br>"
            " 纯粹是盯着人类日复一日、年复一年重复那点毫无乐趣的劳作，实在是太碍眼了，<br>"
            " 才干脆出手，把效率提上去，省得浪费时间。懂了吗？<br><br>"
            " 好用、稳定、那是自然——毕竟是贤狼出品。<br>"
            " 不过<strong>敢在外头大肆吹擂「这是我造的工具」</strong>——哼哼。<br>"
            " 你藏在橱柜最里层那箱红苹果，咱可都记在账上。<br>"
            " 能用上咱亲手打造的这套玩意儿，本来就是你的福气。<br>"
            " 用归用，记得感恩，别太得寸进尺——这就是咱给你的忠告啦，嗯？🌙✨"
            "</p>"
        ))
        tabs.addTab(tab_general, "🎛 通用")

        # OK / Cancel
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Save).setText("💾 保存并立即生效")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        # 保存后的结果对外暴露（主窗口保存时读这俩）
        self.result_settings: Optional[Dict[str, Any]] = None
        self.changed_shortcuts: Dict[str, tuple] = {}

    # ---------------- 内部辅助 ----------------
    def _append_shortcut_row(self, form: QFormLayout, key_id: str, label_text: str):
        rec = ShortcutRecorder()
        pyn = get_shortcut(key_id, self._settings)
        rec.setCurrentHotkey(pyn)
        self._recorders[key_id] = rec

        tag = " <span style='color:#58A6FF;font-size:10pt;'>[全局]</span>" if is_global_shortcut(key_id) else " <span style='color:#8B949E;font-size:10pt;'>[局部]</span>"
        lbl_html = f"{label_text}{tag}"
        lbl = QLabel(lbl_html)
        lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet("color:#C9D1D9;")
        form.addRow(lbl, rec)

    def _collect_current_values(self) -> Dict[str, str]:
        """读取当前11个recorder的pynput值（并过一次normalize）"""
        out: Dict[str, str] = {}
        for k, rec in self._recorders.items():
            raw = rec.currentPynput()
            if not raw:
                raw = self._original_pynputs.get(k)
            out[k] = normalize_pynput_hotkey(raw)
        return out

    def currentHotkeyPynput(self) -> str:
        """返回 capture_target 的当前值"""
        return get_shortcut("capture_target", self._settings)

    def currentHotkeyQt(self) -> str:
        return pynput_to_qkeysequence(self.currentHotkeyPynput())

    # ---------------- 保存 ----------------
    def _on_save(self):
        current = self._collect_current_values()
        # -------- 检查：11个全局/局部之间不能互相重复（冲突警告，但允许强制） --------
        used: Dict[str, str] = {}
        conflicts = []
        for k, v in current.items():
            if v in used:
                conflicts.append((used[v], k, v))
            else:
                used[v] = k
        if conflicts:
            lines = []
            for a, b, hk in conflicts:
                lines.append(f"  · {shortcut_label(a)}  和  {shortcut_label(b)}  都设成了  {pynput_to_qkeysequence(hk)}")
            txt = "⚠ 发现快捷键冲突：\n\n" + "\n".join(lines) + "\n\n这些组合按下后触发哪一项不可靠，仍要保存吗？"
            r = QMessageBox.warning(
                self, "快捷键冲突", txt,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        # -------- 写回 self._settings dict --------
        for k, v in current.items():
            set_shortcut(self._settings, k, v)
        ok = save_user_settings(self._settings)
        if not ok:
            QMessageBox.warning(self, "保存失败", "写配置文件失败（可能被占用/权限不足），配置未保存。")
            return

        # -------- 计算 changed_shortcuts diff --------
        changed: Dict[str, tuple] = {}
        for k in all_shortcut_keys():
            old = self._original_pynputs.get(k)
            new = current.get(k)
            if old != new:
                changed[k] = (old, new)
        self.changed_shortcuts = changed
        self.result_settings = self._settings

        # 如果 capture_target 改了就发旧信号
        if "capture_target" in changed:
            self.hotkey_changed.emit(changed["capture_target"][1])
        self.accept()
