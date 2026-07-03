import os
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Node
from ..core.node_library import (
    NODE_LIBRARY,
    ParamDef,
    EDITOR_INT,
    EDITOR_FLOAT,
    EDITOR_STRING,
    EDITOR_BOOL,
    EDITOR_PATH,
    EDITOR_ENUM,
    EDITOR_TUPLE2,
    EDITOR_TUPLE4,
    EDITOR_TEMPLATE_PICKER,
)


class InspectorPanel(QWidget):
    """右侧属性面板：选中节点后动态生成参数表单。"""

    param_changed = Signal(str, str, object)  # node_id, param_name, value
    request_pick_template = Signal(str, str)  # node_id, param_name
    request_fill_from_main_target = Signal(str)  # node_id（TargetSetup：从主界面当前目标填充）
    request_launch_exe_for_node = Signal(str)  # node_id（TargetSetup：选EXE启动并填充）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMaximumWidth(420)
        self._current_node: Optional[Node] = None
        self._widgets: Dict[str, QWidget] = {}
        self._build_ui()
        self.set_node(None)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        title = QLabel("属性检查器")
        f = QFont()
        f.setBold(True)
        f.setPointSize(11)
        title.setFont(f)
        title.setStyleSheet("color: #C9D1D9;")
        outer.addWidget(title)

        self._header = QLabel("未选择节点")
        self._header.setWordWrap(True)
        self._header.setStyleSheet(
            "background-color: #1C2128; color: #8B949E;"
            "padding: 8px 10px; border-radius: 6px;"
            "border: 1px solid #2D333B; font-size: 10px;"
        )
        outer.addWidget(self._header)

        # ---- 节点动作条（根据节点类型动态填充） ----
        self._action_host = QWidget()
        self._action_layout = QHBoxLayout(self._action_host)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(6)
        self._action_host.hide()
        outer.addWidget(self._action_host)

        self._form_host = QWidget()
        self._form_host.setStyleSheet("background-color: transparent;")
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(4, 4, 4, 4)
        self._form.setHorizontalSpacing(10)
        self._form.setVerticalSpacing(8)
        self._form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        scroll = QScrollArea()
        scroll.setWidget(self._form_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: #1C2128; border: 1px solid #2D333B; border-radius: 6px; }"
            "QScrollBar:vertical { width: 10px; }"
        )
        outer.addWidget(scroll, 1)

    # ---------------- 动作条辅助 ----------------
    def _clear_action_bar(self):
        while self._action_layout.count() > 0:
            it = self._action_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _populate_action_bar(self, node: Node):
        self._clear_action_bar()
        if node is None:
            self._action_host.hide()
            return
        if node.type == "TargetSetup":
            btn_fill = QPushButton("🎯 用主界面选定的目标填充")
            btn_fill.setStyleSheet(self._btn_style("#1F6FEB", "#388BFD"))
            btn_fill.clicked.connect(lambda _=False, nid=node.id: self.request_fill_from_main_target.emit(nid))
            btn_launch = QPushButton("🚀 选EXE启动")
            btn_launch.setStyleSheet(self._btn_style("#8957E5", "#A371F7"))
            btn_launch.clicked.connect(lambda _=False, nid=node.id: self.request_launch_exe_for_node.emit(nid))
            self._action_layout.addWidget(btn_fill)
            self._action_layout.addWidget(btn_launch)
            self._action_layout.addStretch(1)
            self._action_host.show()
        else:
            self._action_host.hide()

    def set_node(self, node: Optional[Node]):
        self._clear_form()
        self._populate_action_bar(node)
        self._current_node = node
        if node is None:
            self._header.setText("未选择节点\n\n点击画布上的节点以编辑参数。")
            self._form.addRow(QLabel("（无）"))
            return

        meta = NODE_LIBRARY.get(node.type)
        title = meta.title if meta else node.type
        desc = meta.description if meta else ""
        self._header.setText(
            f"<b style='color:#58A6FF'>{title}</b>  <span style='color:#8B949E'>[{node.type}]</span>"
            f"<br><span style='color:#8B949E; font-size: 9px'>ID: {node.id}</span>"
            f"<br>{desc}"
        )
        self._header.setTextFormat(Qt.RichText)
        params = meta.params if meta else []
        for pd in params:
            current = node.params.get(pd.name, pd.default)
            w = self._build_editor(pd, current)
            if w is None:
                continue
            lbl = QLabel(pd.label or pd.name)
            lbl.setStyleSheet("color: #C9D1D9; font-size: 9px;")
            if pd.required:
                lbl.setText(f"<span style='color:#F85149'>* </span>{lbl.text()}")
                lbl.setTextFormat(Qt.RichText)
            self._form.addRow(lbl, w)
            self._widgets[pd.name] = w
        if not params:
            self._form.addRow(QLabel("（该节点无可编辑参数）"))

    # ---------------- 编辑器 ----------------
    def _clear_form(self):
        self._widgets.clear()
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _build_editor(self, pd: ParamDef, current: Any) -> Optional[QWidget]:
        node = self._current_node
        nid = node.id if node else ""
        editor = pd.editor

        if editor == EDITOR_INT:
            sp = QSpinBox()
            if pd.min is not None:
                sp.setMinimum(int(pd.min))
            else:
                sp.setMinimum(-10_000_000)
            if pd.max is not None:
                sp.setMaximum(int(pd.max))
            else:
                sp.setMaximum(10_000_000)
            sp.setSingleStep(int(max(1, pd.step)))
            try:
                sp.setValue(int(current) if current is not None else int(pd.default or 0))
            except Exception:
                sp.setValue(0)
            sp.valueChanged.connect(lambda v, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, int(v)))
            sp.setStyleSheet(self._edit_style())
            return sp

        if editor == EDITOR_FLOAT:
            sp = QDoubleSpinBox()
            if pd.min is not None:
                sp.setMinimum(float(pd.min))
            else:
                sp.setMinimum(-1e9)
            if pd.max is not None:
                sp.setMaximum(float(pd.max))
            else:
                sp.setMaximum(1e9)
            sp.setDecimals(4)
            sp.setSingleStep(float(pd.step))
            try:
                sp.setValue(float(current) if current is not None else float(pd.default or 0.0))
            except Exception:
                sp.setValue(0.0)
            sp.valueChanged.connect(lambda v, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, float(v)))
            sp.setStyleSheet(self._edit_style())
            return sp

        if editor == EDITOR_STRING:
            le = QLineEdit(str(current or ""))
            le.textChanged.connect(lambda t, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, t))
            le.setStyleSheet(self._edit_style())
            if pd.name == "text":
                le.setPlaceholderText("便签内容（支持换行）")
            return le

        if editor == EDITOR_BOOL:
            cb = QCheckBox("启用")
            try:
                cb.setChecked(bool(current))
            except Exception:
                cb.setChecked(False)
            cb.toggled.connect(lambda v, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, bool(v)))
            cb.setStyleSheet("color: #C9D1D9;")
            return cb

        if editor == EDITOR_ENUM:
            cb = QComboBox()
            if pd.options:
                for op in pd.options:
                    cb.addItem(str(op), op)
            idx = cb.findData(current)
            if idx < 0:
                idx = 0
            cb.setCurrentIndex(idx)
            cb.currentIndexChanged.connect(
                lambda i, nid=nid, pn=pd.name, combo=cb: self.param_changed.emit(nid, pn, combo.currentData())
            )
            cb.setStyleSheet(self._edit_style())
            return cb

        if editor == EDITOR_PATH:
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            le = QLineEdit(str(current or ""))
            le.setStyleSheet(self._edit_style())
            le.textChanged.connect(lambda t, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, t))
            btn = QPushButton("…")
            btn.setFixedWidth(28)
            btn.setStyleSheet(self._btn_style())
            btn.clicked.connect(lambda _=False, line=le: self._pick_file(line))
            row.addWidget(le, 1)
            row.addWidget(btn, 0)
            return host

        if editor == EDITOR_TEMPLATE_PICKER:
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            le = QLineEdit(str(current or ""))
            le.setStyleSheet(self._edit_style())
            le.textChanged.connect(lambda t, nid=nid, pn=pd.name: self.param_changed.emit(nid, pn, t))
            btn_pick = QPushButton("裁图")
            btn_pick.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_pick.clicked.connect(lambda _=False, nid=nid, pn=pd.name: self.request_pick_template.emit(nid, pn))
            btn_file = QPushButton("…")
            btn_file.setFixedWidth(28)
            btn_file.setStyleSheet(self._btn_style())
            btn_file.clicked.connect(lambda _=False, line=le: self._pick_file(line))
            row.addWidget(le, 1)
            row.addWidget(btn_pick, 0)
            row.addWidget(btn_file, 0)
            return host

        if editor in (EDITOR_TUPLE2, EDITOR_TUPLE4):
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            size = 2 if editor == EDITOR_TUPLE2 else 4
            values = [0] * size
            if isinstance(current, (list, tuple)):
                for i in range(min(size, len(current))):
                    try:
                        values[i] = int(current[i])
                    except Exception:
                        pass
            spins = []
            labels = ["X", "Y"] if size == 2 else ["X", "Y", "W", "H"]
            for i in range(size):
                s = QSpinBox()
                s.setRange(-100000, 100000)
                s.setValue(values[i])
                s.setPrefix(f"{labels[i]} ")
                s.setStyleSheet(self._edit_style())
                spins.append(s)
                row.addWidget(s, 1)

            def _on_change(*_, nid=nid, pn=pd.name, sps=spins, sz=size):
                vals = [s.value() for s in sps]
                t = tuple(vals)
                self.param_changed.emit(nid, pn, t)
            for s in spins:
                s.valueChanged.connect(_on_change)
            return host

        return None

    def _pick_file(self, line_edit: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择模板图片", line_edit.text() or ".",
            "图片文件 (*.png *.jpg *.jpeg *.bmp)",
        )
        if path:
            line_edit.setText(path)

    @staticmethod
    def _edit_style() -> str:
        return (
            "QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {"
            "  background-color: #22272E; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px;"
            "  padding: 3px 6px; font-size: 10px;"
            "}"
            "QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {"
            "  border: 1px solid #58A6FF;"
            "}"
        )

    @staticmethod
    def _btn_style(bg: str = "#2D333B", hover: str = "#3A3F4B") -> str:
        return (
            f"QPushButton {{ background-color: {bg}; color: #C9D1D9;"
            f" border: 1px solid #2D333B; border-radius: 4px;"
            f" padding: 3px 8px; font-size: 10px; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
        )

    # ============= 工具方法（供截图/切图工具对话框回填参数）=============
    def get_current_node_info(self):
        """返回 (node_id or None, node_type or None)"""
        if self._current_node is None:
            return None, None
        return self._current_node.id, self._current_node.type

    def apply_params_merge(self, merge: Dict[str, Any]) -> bool:
        """
        合并参数到当前节点，并通过 param_changed 逐个发信号同步给Scene，最后重载表单。
        返回是否应用成功。
        """
        if self._current_node is None or not merge:
            return False
        nid = self._current_node.id
        for pname, pval in merge.items():
            self._current_node.params[pname] = pval
            try:
                self.param_changed.emit(nid, pname, pval)
            except Exception:
                pass
        # 重载表单让UI显示最新值
        self.set_node(self._current_node)
        return True
