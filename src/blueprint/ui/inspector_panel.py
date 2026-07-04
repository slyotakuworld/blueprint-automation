import base64
import io
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]

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
    EDITOR_MULTILINE,
)


class InspectorPanel(QWidget):
    """右侧属性面板：选中节点后动态生成参数表单。"""

    param_changed = Signal(str, str, object)  # node_id, param_name, value
    request_pick_template = Signal(str, str)  # node_id, param_name
    request_fill_from_main_target = Signal(str)  # node_id（TargetSetup：从主界面当前目标填充）
    request_launch_exe_for_node = Signal(str)  # node_id（TargetSetup：选EXE启动并填充）
    request_snip_for_node = Signal(str, str)  # node_id, mode（"if_img_add" / "if_static_roi" / "if_img_replace_<idx>"）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMaximumWidth(460)
        self._current_node: Optional[Node] = None
        self._widgets: Dict[str, QWidget] = {}
        self._ifimg_host: Optional[QWidget] = None
        self._ifstatic_host: Optional[QWidget] = None
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

    # ---------------- 赫萝口吻（属性面板描述用）----------------
    @staticmethod
    def _holo_speak(node_title: str, node_type: str, raw_desc: str) -> str:
        raw_desc = (raw_desc or "").strip()
        t = node_type
        # ---------- 分类模板（按节点类型匹配不同赫萝开场白/意象） ----------
        if t in ("Start",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯哼哼～旅途的号角吹响啦，汝的冒险就从这儿启程哟。"
            flair = "这一路是丰收满舱还是空手而归，可全看汝怎么编排啦～可别在头一步就摔个跟头呐，哼！"
        elif t in ("End",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>呼，一路奔波，总算到歇脚的小酒馆啦～"
            flair = "到了这儿，咱就把麦穗收好算总账咯。汝这条分支走到此为止，不会再向下流转啦，嗯。"
        elif t in ("Loop", "Sequence", "VarSwitch", "If", "Wait"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯，盘盘算算、思前想后，这就是商人的本事哟。"
            flair = "条件算不清的话，可是要赔上一整袋麦子的唷～汝可得像精明的行商一样，把每条分支都瞧清楚咯。"
        elif t in ("VarSet", "VarGet", "VarCalc"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>记好账、算明白金币，这可是旅途中顶顶重要的事。"
            flair = "把变量的账本理清楚，后面的伙伴才能不被糊涂账坑害呀。咱最讨厌糊涂的商人了，哼。"
        elif t in ("Mouse", "Keyboard", "PressKey"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>哟，要亲自动手啦？咱这爪子可是比凡人麻利得多唷。"
            flair = "别慢吞吞的，该出手时就出手。不过要是手滑点错了地方，可别赖咱没提醒哟。"
        elif t in ("OpenApp", "BindWindow", "CloseApp", "WebBrowser"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯，找对店家、推对门，这是跑腿的第一要义。"
            flair = "店门没找对就瞎闯，可是会被掌柜的当成小贼轰出来哒。绑定窗口这事，马虎不得哟。"
        elif t in ("HttpRequest",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>哟，要让商队驿站给远方捎封信啦？咱可盯着别被半道的风刮歪了唷。"
            flair = "驿站地址、封条、正文可都得写清楚咯，回来的回信咱帮汝拆成一袋袋小银子（web_ 变量），汝只管拿去用便是。"
        elif t in ("Notify",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>想让远方的人听见汝的声音？这便如酒馆公告板上的告示哟。"
            flair = "消息写清楚、说大声，才不会传歪了。可别写些含糊其辞的玩意儿，被咱笑话哟。"
        elif t in ("Delay",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯，忙里偷闲，也是旅人的智慧唷。"
            flair = "等一等、缓一缓，后面的路才好走。不过睡过头错过了集市，可别怪咱没叫汝哟。"
        elif t in ("FindAnyImage", "IfImageDetect", "CaptureRegion", "StillnessDetect"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>哼哼，论眼力，咱这双狼之眼可从不输谁哟～"
            flair = "风吹草动、蛛丝马迹，咱都能瞧得一清二楚。可要是参考图选得歪，再利的眼也白搭呐，哼。"
        elif t in ("SoundDetect",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>侧耳倾听——风声、脚步声、金币落袋声，可逃不过咱的耳朵哟。"
            flair = "音量够不够、调门对不对，咱这对狼耳一辨便知。设备可别选错了，不然咱也帮不了汝呐。"
        else:
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯？汝选中了个有意思的小玩意儿嘛。"
            flair = "咱也不知该说啥好，总之一路顺风，别把麦子弄丢了哟～"

        # ---------- 只保留黄色赫萝部分，不再输出原始白字描述 ----------
        # （用户要求：属性检查器描述里删掉白字原描述 + 金色左竖线的 body，只留黄色 opening + flair）
        return (
            f"<div style='color:#FFD27A;'>{opening}</div>"
            f"<div style='color:#F4B740; margin-top:2px;'>{flair}</div>"
        )

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
        if node.type == "Notify":
            btn = QPushButton("🔍 预览通知内容")
            btn.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn.clicked.connect(lambda _=False: self._show_notify_preview())
            self._action_layout.addWidget(btn)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "Sequence":
            btn_reset = QPushButton("♻️ 重置步数计数器")
            btn_reset.setStyleSheet(self._btn_style("#57606A", "#6E7681"))
            btn_reset.setToolTip("运行时 Sequence 节点内部维护 seq_counters，蓝图未运行时不生效")
            btn_reset.clicked.connect(lambda _=False: self._reset_sequence_counter())
            self._action_layout.addWidget(btn_reset)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "VarSwitch":
            btn_toggle = QPushButton("🔄 切换简单/多分支模式")
            btn_toggle.setStyleSheet(self._btn_style("#57606A", "#6E7681"))
            btn_toggle.clicked.connect(lambda _=False: self._toggle_varswitch_mode())
            self._action_layout.addWidget(btn_toggle)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "IfImageDetect":
            btn_add = QPushButton("📷 截图新增一张")
            btn_add.setStyleSheet(self._btn_style("#ef6c00", "#ff9800"))
            btn_add.setToolTip("全屏框选一张参考图，追加到下方列表")
            nid = node.id
            btn_add.clicked.connect(lambda _=False, n=nid: self.request_snip_for_node.emit(n, "if_img_add"))
            self._action_layout.addWidget(btn_add)
            btn_import = QPushButton("📂 导入图片")
            btn_import.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_import.setToolTip("从本地文件选择一张或多张图片导入")
            btn_import.clicked.connect(lambda _=False: self._ifimg_import_files())
            self._action_layout.addWidget(btn_import)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "StillnessDetect":
            btn_roi = QPushButton("📍 框选ROI区域")
            btn_roi.setStyleSheet(self._btn_style("#d84315", "#ff7043"))
            btn_roi.setToolTip("全屏框选一个矩形区域作为静止检测ROI")
            nid = node.id
            btn_roi.clicked.connect(lambda _=False, n=nid: self.request_snip_for_node.emit(n, "if_static_roi"))
            self._action_layout.addWidget(btn_roi)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        self._action_host.hide()

    def _open_dev_doc(self):
        doc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "Qflow对齐改造_开发设计文档.md"))
        if not os.path.exists(doc):
            QMessageBox.information(self, "提示", f"设计文档不在期望位置：\n{doc}")
            return
        try:
            import subprocess, sys, os
            if sys.platform.startswith("win"):
                os.startfile(doc)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", doc])
            else:
                subprocess.Popen(["xdg-open", doc])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开设计文档：\n{e}")

    def _reset_sequence_counter(self):
        if self._current_node is None:
            return
        QMessageBox.information(
            self, "Sequence 说明（短路OR分支）",
            "● 工作方式：按顺序 尝试 #1 → #2 → … → #N 整条路径\n"
            "● 短路逻辑：任一路径完整走通（到达执行终点，且未触发失败出口）→ 立即跳 out_done 完成\n"
            "● 失败语义出口：走到  fail / timeout / not_found / no  其中任一端口 → 该路径判负，自动尝试下一条\n"
            "● 全部失败：所有路径都命中失败语义 → 走 else 端口\n\n"
            "（Inspector 中调整 num_paths 后，上方端口列表会即时刷新显示）"
        )

    def _toggle_varswitch_mode(self):
        if self._current_node is None:
            return
        node = self._current_node
        current_has_var_name = bool(str(node.params.get("var_name", "") or "").strip())
        if current_has_var_name:
            # 切到多分支：清空 var_name / var_expr，填一个示例 cases
            merge = {"var_name": "", "var_expr": "", "cases": "case_a = 100\ncase_b = hello\ncase_c = {some_var}"}
        else:
            # 切到简单模式：清空 cases，填示例 var_name+var_expr
            merge = {"cases": "", "var_name": "my_var", "var_expr": "{= {my_var} > 10 }"}
        self.apply_params_merge(merge)

    def _show_notify_preview(self):
        """Notify 两阶段预览：先 disable ALL_VARS 做普通插值，最后一步再单独替换 {ALL_VARS}"""
        if self._current_node is None or self._current_node.type != "Notify":
            return
        node = self._current_node
        try:
            from src.blueprint.core.variable_engine import VariableEngine, assemble_runtime_params
            # 取当前蓝图的 variables 字典（如果有）；取不到就空
            vars_now: Dict[str, Any] = {}
            try:
                mw = getattr(self.window(), '__self__', None) or self.window()
                if hasattr(mw, "project") and hasattr(mw.project, "variables"):
                    vars_now = dict(mw.project.variables or {})
            except Exception:
                vars_now = {}
            # 预览用：临时给点示例变量，让用户看得清效果
            preview_vars = dict(vars_now)
            if "user" not in preview_vars:
                preview_vars["user"] = "Alice"
            if "port" not in preview_vars:
                preview_vars["port"] = 8080
            engine = VariableEngine(preview_vars)
            title_raw = str(node.params.get("title", "") or "")
            msg_raw = str(node.params.get("message", "") or "")
            level_raw = str(node.params.get("level", "INFO") or "INFO").upper()
            title = engine.interpolate(title_raw) if engine.has_any_marker(title_raw) else title_raw
            msg_stage1 = engine.interpolate(msg_raw, disable_all_vars=True) if engine.has_any_marker(msg_raw) else msg_raw
            all_vars = engine._render_all_vars()
            def _av(t: str) -> str:
                if not isinstance(t, str): return t
                return t.replace("${ALL_VARS}", all_vars).replace("{ALL_VARS}", all_vars)
            title_final = _av(title if isinstance(title, str) else str(title))
            msg_final = _av(msg_stage1 if isinstance(msg_stage1, str) else str(msg_stage1))
        except Exception as e:
            QMessageBox.critical(self, "预览失败", f"预览异常：\n{e}")
            return
        # 弹对话框
        dlg = QDialog(self)
        dlg.setWindowTitle(f"[预览] Notify  [{level_raw}] {title_final}")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        info = QLabel(
            f"<b>标题：</b><span style='color:#58A6FF'>{title_final}</span><br>"
            f"<b>级别：</b>{level_raw}<br>"
            f"<span style='color:#8B949E; font-size:9px;'>(预览用示例变量 user=Alice, port=8080；实际运行时用蓝图里的变量)</span>"
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        lay.addWidget(info)
        box = QGroupBox("最终展开内容（{ALL_VARS} 已在最后一步单独替换）")
        vlay = QVBoxLayout(box)
        te = QPlainTextEdit()
        te.setReadOnly(True)
        te.setPlainText(str(msg_final) if msg_final is not None else "")
        te.setStyleSheet(self._edit_style())
        vlay.addWidget(te)
        lay.addWidget(box, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

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
        raw_desc = meta.description if meta else ""
        desc = self._holo_speak(title, node.type, raw_desc)
        self._header.setText(
            f"<b style='color:#58A6FF'>{title}</b>  <span style='color:#8B949E'>[{node.type}]</span>"
            f"<br><span style='color:#8B949E; font-size: 9px'>ID: {node.id}</span>"
            f"<br><div style='margin-top:4px; line-height:1.5;'>{desc}</div>"
        )
        self._header.setTextFormat(Qt.RichText)
        params = meta.params if meta else []
        rows_added: List[str] = []

        # --- 额外名称（手动插在所有参数最前面，不进NODE_LIBRARY的params列表，避免分类ORDER/端口动态刷新受影响）---
        custom_name_val = str(node.params.get("custom_name", "") or "")
        cn_edit = QLineEdit(custom_name_val)
        cn_edit.setPlaceholderText("（可选）给这个节点起昵称，会显示在画布标题三段式最后一段")
        try:
            cn_edit.setStyleSheet(self._edit_style())
        except Exception:
            pass
        cn_edit.editingFinished.connect(
            lambda w=cn_edit, n=node: self.param_changed.emit(n.id, "custom_name", w.text())
        )
        cn_lbl = QLabel("额外名称")
        cn_lbl.setStyleSheet("color: #FFA657; font-size: 9px; font-weight: bold;")
        cn_lbl.setToolTip("画布标题展示：ID · 节点标题 · 额外名称（为空时只显示前两段）")
        self._form.addRow(cn_lbl, cn_edit)
        self._widgets["custom_name"] = cn_edit
        rows_added.append("custom_name")

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
            rows_added.append(pd.name)
        if not params:
            self._form.addRow(QLabel("（该节点无可编辑参数）"))

        # ================================================================
        # 节点专属后处理：参数渲染完成后，追加增强 UI
        # ================================================================
        # 1) 通用：动态端口节点 → 追加"当前输出端口列表"Label
        is_dynamic = bool(meta is not None and getattr(meta, "has_dynamic_ports", False))
        if node.type in ("FindAnyImage",):
            is_dynamic = True
        if is_dynamic:
            try:
                from ..core.node_library import _resolve_output_ports_default
                ports = _resolve_output_ports_default(node.type, dict(node.params or {}))
                names = [p.name for p in ports]
                lab = QLabel("🔌 当前输出端口：" + "  →  ".join(names))
                lab.setWordWrap(True)
                lab.setStyleSheet(
                    "color:#58A6FF; background:#0B3A5C33; padding:6px 8px;"
                    "border:1px dashed #0B3A5C; border-radius:4px; font-size:9px;"
                )
                self._form.addRow(lab)
                # 实时更新：如果存在 num_paths / cases / templates 控件，连信号刷新端口列表
                self._bind_dynamic_ports_refresh(node, lab, rows_added)
            except Exception:
                pass
        # 2) VarSwitch 专属：var_name 非空 → 隐藏 cases 行（简单模式）
        if node.type == "VarSwitch":
            self._apply_varswitch_mode_visibility()
            # 连 var_name 文本变化 → 实时切 cases 显隐
            vn_widget = self._widgets.get("var_name")
            if isinstance(vn_widget, QLineEdit):
                vn_widget.textChanged.connect(lambda _t, s=self: s._apply_varswitch_mode_visibility())

        # 3) Sequence 专属：num_paths 变化时，上面加的端口列表 Label 也要实时更新（在 _bind_dynamic_ports_refresh 里已经处理）

        # 4) IfImageDetect 专属：隐藏 images_json 文本框（用户不用手敲JSON），下方追加缩略图网格
        if node.type == "IfImageDetect":
            try:
                self._hide_param_row("images_json")
                self._append_ifimg_grid()
            except Exception as e:
                print(f"[Inspector] IfImageDetect UI 出错: {e}")

        # 5) StillnessDetect 专属：隐藏 roi_preview_b64 文本框，下方追加 ROI 预览图
        if node.type == "StillnessDetect":
            try:
                self._hide_param_row("roi_preview_b64")
                self._append_ifstatic_preview()
            except Exception as e:
                print(f"[Inspector] StillnessDetect UI 出错: {e}")

        # 6) SoundDetect 专属：追加 设备枚举/自动校准/试听 工具栏面板
        if node.type == "SoundDetect":
            try:
                self._append_ifsound_panel()
            except Exception as e:
                print(f"[Inspector] SoundDetect UI 出错: {e}")

        # 7) Mouse 专属：根据 action 分组动态显隐参数，追加提示Label
        if node.type == "Mouse":
            try:
                self._apply_mouse_action_visibility(initial=True)
                # 绑定 action 下拉框变化，实时切分组
                w_action = self._widgets.get("action")
                if isinstance(w_action, QComboBox):
                    w_action.currentIndexChanged.connect(lambda *_: self._apply_mouse_action_visibility(initial=False))
            except Exception as e:
                print(f"[Inspector] Mouse UI 出错: {e}")

    # ================================================================
    # 辅助：根据参数名隐藏对应行（images_json / roi_preview_b64 这种不该让用户手敲的）
    # ================================================================
    def _hide_param_row(self, param_name: str):
        w = self._widgets.get(param_name)
        if w is None:
            return
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w:
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                if lab_item and lab_item.widget():
                    lab_item.widget().setVisible(False)
                if fld_w:
                    fld_w.setVisible(False)
                break

    # ================================================================
    # 辅助：PIL / b64 / QPixmap 转换
    # ================================================================
    @staticmethod
    def _b64_to_pil(b64_str: str):
        if not b64_str or Image is None:
            return None
        try:
            raw = base64.b64decode(b64_str)
            return Image.open(io.BytesIO(raw))
        except Exception:
            return None

    @staticmethod
    def _pil_to_b64(pil_img) -> str:
        if pil_img is None:
            return ""
        try:
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return ""

    @staticmethod
    def _pil_to_qpixmap(pil_img, size: int = 80) -> Optional[QPixmap]:
        if pil_img is None:
            return None
        try:
            img = pil_img.copy()
            img.thumbnail((size, size), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS)
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            data = img.tobytes("raw", "RGBA")
            from PySide6.QtGui import QImage
            qimg = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
            return QPixmap.fromImage(qimg.copy())
        except Exception:
            return None

    # ================================================================
    # IfImageDetect：images_json 解析 / 写回 / 缩略图网格
    # ================================================================
    def _parse_ifimg_list(self) -> List[Dict[str, Any]]:
        node = self._current_node
        if node is None:
            return []
        raw = node.params.get("images_json", "[]")
        if isinstance(raw, str) and raw:
            try:
                arr = json.loads(raw)
                return arr if isinstance(arr, list) else []
            except Exception:
                return []
        elif isinstance(raw, list):
            return raw
        return []

    def _write_ifimg_list(self, arr: List[Dict[str, Any]]):
        node = self._current_node
        if node is None:
            return
        s = json.dumps(arr, ensure_ascii=False, sort_keys=True)
        self.param_changed.emit(node.id, "images_json", s)

    def apply_params_merge(self, merge: Dict[str, Any]):
        node = self._current_node
        if node is None:
            return
        for k, v in (merge or {}).items():
            self.param_changed.emit(node.id, k, v)

    def _ifimg_import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的图片（可多选）",
            os.path.expanduser("~"),
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not paths:
            return
        arr = self._parse_ifimg_list()
        for p in paths:
            try:
                pil = Image.open(p) if Image is not None else None
                w, h = pil.size if pil is not None else (0, 0)
                item: Dict[str, Any] = {
                    "id": self._gen_id(),
                    "path": str(p),
                    "w": int(w),
                    "h": int(h),
                }
                if pil is not None:
                    b64 = self._pil_to_b64(pil)
                    if b64:
                        item["b64"] = b64
                arr.append(item)
            except Exception as e:
                print(f"[Inspector] 导入图片失败 {p}: {e}")
        self._write_ifimg_list(arr)
        # 刷新网格显示
        if self._current_node is not None:
            self.set_node(self._current_node)

    @staticmethod
    def _gen_id() -> str:
        import uuid
        return uuid.uuid4().hex[:8]

    def _append_ifimg_grid(self):
        """在表单底部追加：统计Label + 缩略图网格 + 清空按钮"""
        node = self._current_node
        if node is None:
            return
        arr = self._parse_ifimg_list()
        n = len(arr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        title_row = QWidget()
        th = QHBoxLayout(title_row)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(6)
        stat = QLabel(f"🖼️ 参考图列表（共 {n} 张）：全部存在才走 yes；缺任何一张走 no")
        stat.setStyleSheet("color: #ff9800; font-weight: bold; font-size: 10px;")
        th.addWidget(stat, 1)
        if n > 0:
            btn_clear = QPushButton("🗑️ 清空全部")
            btn_clear.setStyleSheet(self._btn_style("#6E312D", "#8A3A34"))
            btn_clear.clicked.connect(self._ifimg_clear_all)
            th.addWidget(btn_clear)
        self._form.addRow(title_row)

        if n == 0:
            empty = QLabel("（还没有添加参考图。点上面工具栏 [📷 截图新增一张] 或 [📂 导入图片]）")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color: #8B949E; background:#1C2128; padding:8px;"
                "border:1px dashed #2D333B; border-radius:4px; font-size:9px;"
            )
            self._form.addRow(empty)
            return

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(8)
        COLS = 3
        for idx, item in enumerate(arr):
            card = self._ifimg_make_card(idx, item)
            if card is None:
                continue
            r, c = divmod(idx, COLS)
            grid.addWidget(card, r, c)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        self._ifimg_host = host
        self._form.addRow(host)

    def _ifimg_make_card(self, idx: int, item: Dict[str, Any]) -> Optional[QWidget]:
        if not isinstance(item, dict):
            return None
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background:#1C2128; border:1px solid #2D333B; border-radius:6px; }"
            "QFrame:hover { border:1px solid #ef6c00; }"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # 缩略图
        pix = None
        pil = None
        b64 = item.get("b64") or item.get("image_b64")
        if isinstance(b64, str) and b64:
            pil = self._b64_to_pil(b64)
        path = item.get("path", "")
        if pil is None and isinstance(path, str) and path and os.path.exists(path) and Image is not None:
            try:
                pil = Image.open(path)
            except Exception:
                pil = None
        w = int(item.get("w", 0) or 0)
        h = int(item.get("h", 0) or 0)
        if pil is None and w > 0 and h > 0:
            pix_label = QLabel(f"({w}×{h})\n(无预览图)")
            pix_label.setAlignment(Qt.AlignCenter)
            pix_label.setStyleSheet(
                "color:#8B949E; background:#22272E;"
                "border:1px dashed #444C56; border-radius:4px;"
                "padding:12px 4px; font-size:9px;"
            )
            pix_label.setFixedSize(96, 96)
        else:
            pix = self._pil_to_qpixmap(pil, size=88)
            pix_label = QLabel()
            pix_label.setAlignment(Qt.AlignCenter)
            if pix is not None:
                pix_label.setPixmap(pix)
            pix_label.setFixedSize(96, 96)
            pix_label.setStyleSheet(
                "QLabel { background:#22272E; border:1px solid #2D333B; border-radius:4px; }"
            )
            if w > 0 and h > 0:
                pix_label.setToolTip(f"原始尺寸：{w} × {h} 像素")
        v.addWidget(pix_label, alignment=Qt.AlignCenter)

        # 尺寸标签
        size_label = QLabel(f"{w}×{h}" if w and h else f"#{idx+1}")
        size_label.setAlignment(Qt.AlignCenter)
        size_label.setStyleSheet("color:#8B949E; font-size:9px;")
        v.addWidget(size_label)

        # 按钮行
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(4)

        btn_rep = QToolButton()
        btn_rep.setText("🔄")
        btn_rep.setToolTip("重新框选替换这张图")
        btn_rep.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
        nid = self._current_node.id if self._current_node else ""
        btn_rep.clicked.connect(
            lambda _=False, n=nid, i=idx: self.request_snip_for_node.emit(n, f"if_img_replace_{i}")
        )
        br.addWidget(btn_rep)

        btn_del = QToolButton()
        btn_del.setText("✖")
        btn_del.setToolTip("删除这张参考图")
        btn_del.setStyleSheet(self._btn_style("#6E312D", "#8A3A34"))
        btn_del.clicked.connect(lambda _=False, i=idx: self._ifimg_delete_at(i))
        br.addWidget(btn_del)
        v.addWidget(btn_row)
        return card

    def _ifimg_delete_at(self, idx: int):
        arr = self._parse_ifimg_list()
        if 0 <= idx < len(arr):
            del arr[idx]
            self._write_ifimg_list(arr)
            if self._current_node is not None:
                self.set_node(self._current_node)

    def _ifimg_clear_all(self):
        if not self._parse_ifimg_list():
            return
        ret = QMessageBox.question(
            self, "确认清空",
            "确定要清空所有参考图吗？此操作可按 Ctrl+Z 撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            self._write_ifimg_list([])
            if self._current_node is not None:
                self.set_node(self._current_node)

    # ================================================================
    # StillnessDetect：ROI 预览 + 数值显示
    # ================================================================
    def _append_ifstatic_preview(self):
        node = self._current_node
        if node is None:
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        roi_raw = node.params.get("roi", "[0,0,0,0]")
        try:
            if isinstance(roi_raw, str) and roi_raw:
                roi = [int(x) for x in json.loads(roi_raw)]
            elif isinstance(roi_raw, (list, tuple)) and len(roi_raw) >= 4:
                roi = [int(x) for x in roi_raw[:4]]
            else:
                roi = [0, 0, 0, 0]
        except Exception:
            roi = [0, 0, 0, 0]
        if len(roi) < 4:
            roi += [0] * (4 - len(roi))
        x, y, w, h = roi
        has_roi = (w > 0 and h > 0)

        title = QLabel("⏸️ 静止检测 ROI（Region Of Interest）")
        title.setStyleSheet("color: #ff7043; font-weight: bold; font-size: 10px;")
        self._form.addRow(title)

        info_row = QWidget()
        ir = QHBoxLayout(info_row)
        ir.setContentsMargins(0, 0, 0, 0)
        ir.setSpacing(8)
        pos_lbl = QLabel(f"📍 位置: ({x}, {y})")
        pos_lbl.setStyleSheet("color:#C9D1D9; font-size:9px;")
        size_lbl = QLabel(f"📐 尺寸: {w} × {h}")
        size_lbl.setStyleSheet("color:#C9D1D9; font-size:9px;")
        hint_lbl = QLabel("（未设置，请点上面工具栏 [📍 框选ROI区域]）" if not has_roi else "（已设置）")
        hint_lbl.setStyleSheet(
            f"font-size:9px; color: {'#F85149' if not has_roi else '#81C784'};"
        )
        ir.addWidget(pos_lbl)
        ir.addWidget(size_lbl)
        ir.addStretch(1)
        ir.addWidget(hint_lbl)
        self._form.addRow(info_row)

        b64 = node.params.get("roi_preview_b64", "")
        pil = self._b64_to_pil(b64) if isinstance(b64, str) and b64 else None
        if pil is None:
            empty = QLabel("（无预览）")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:#8B949E; background:#1C2128; padding:16px;"
                "border:1px dashed #2D333B; border-radius:4px; font-size:10px;"
                "min-height: 80px;"
            )
            self._form.addRow(empty)
            return
        pix = self._pil_to_qpixmap(pil, size=220)
        if pix is not None:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setPixmap(pix)
            lbl.setStyleSheet(
                "QLabel { background:#000; border:2px solid #d84315; border-radius:4px; padding:2px; }"
            )
            if w > 0 and h > 0:
                lbl.setToolTip(f"ROI 预览：实际区域 {w}×{h} 像素")
            self._form.addRow(lbl)

    # ================================================================
    # SoundDetect 专属：枚举设备 / 自动校准 / 试听
    # ================================================================
    def _append_ifsound_panel(self):
        node = self._current_node
        if node is None:
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        title = QLabel("🔊 声音检测 · 可视化调参工具")
        title.setStyleSheet("color: #00acc1; font-weight: bold; font-size: 10px;")
        self._form.addRow(title)

        # 工具按钮行
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)

        b_enum = QPushButton("🎧 枚举设备")
        b_enum.setToolTip("枚举所有扬声器/麦克风+环回设备，选择后自动填入设备ID")
        b_enum.clicked.connect(self._sound_enum_devices)
        bl.addWidget(b_enum)

        b_calib = QPushButton("📏 自动校准阈值")
        b_calib.setToolTip("采样5秒环境音，把振幅99分位+6dB作为新阈值（推荐首次使用点一下）")
        b_calib.clicked.connect(self._sound_auto_calibrate)
        bl.addWidget(b_calib)

        b_listen = QPushButton("🎵 试听 & 立即判定")
        b_listen.setToolTip("不跑蓝图，当场采一段+显示判定结果（YES/NO）+ASCII波形图")
        b_listen.clicked.connect(self._sound_listen_now)
        bl.addWidget(b_listen)

        bl.addStretch(1)
        self._form.addRow(btn_row)

        # 模式显隐联动：非 spectral_band 模式下隐藏频段上下限
        self._sound_apply_band_visibility()
        mode_w = self._widgets.get("judge_mode")
        if isinstance(mode_w, QComboBox):
            try: mode_w.currentIndexChanged.disconnect()
            except Exception: pass
            mode_w.currentIndexChanged.connect(lambda _i: self._sound_apply_band_visibility())

    def _sound_apply_band_visibility(self):
        node = self._current_node
        if node is None or node.type != "SoundDetect":
            return
        mode_val = str(node.params.get("judge_mode", "amplitude_max") or "amplitude_max")
        w_mode = self._widgets.get("judge_mode")
        if isinstance(w_mode, QComboBox):
            mode_val = str(w_mode.currentText() or mode_val)
        is_band = (mode_val == "spectral_band")
        for pname in ("spectral_band_low_hz", "spectral_band_high_hz"):
            w = self._widgets.get(pname)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(is_band)
                    if fld_w:
                        fld_w.setVisible(is_band)
                    break

    def _sound_enum_devices(self):
        items: List[str] = ["default_loopback (默认扬声器-录系统播放声)"]
        try:
            import soundcard as sc
            for mic in sc.all_microphones(include_loopback=True):
                name = getattr(mic, "name", str(mic.id) if hasattr(mic, "id") else str(mic))
                is_loopback = "loopback" in str(getattr(mic, "id", "")).lower() or "环回" in name or "Loopback" in name
                tag = " [环回·可录系统声]" if is_loopback else " [麦克风]"
                items.append(f"{name}{tag}  [id={getattr(mic, 'id', '')}]")
        except Exception as e:
            items.append(f"(soundcard不可用：{e})")
        try:
            import pyaudio
            p = pyaudio.PyAudio()
            n = p.get_device_count()
            for i in range(n):
                info = p.get_device_info_by_index(i)
                name = info.get("name", "")
                is_input = bool(int(info.get("maxInputChannels", 0) or 0))
                if is_input:
                    items.append(f"(PyAudio) {name}  [idx={i}]")
            p.terminate()
        except Exception:
            pass
        choice, ok = QInputDialog.getItem(
            self, "🎧 选择音频设备",
            "可选设备列表（扬声器环回设备可以录系统自己播放的声音，推荐优先）：",
            items, 0, False,
        )
        if not ok or not choice:
            return
        dev_id = "default_loopback"
        m = re.search(r"\[id=([^\]]+)\]", choice)
        if m:
            dev_id = m.group(1)
        else:
            m2 = re.search(r"\[idx=(\d+)\]", choice)
            if m2:
                dev_id = f"pyaudio_idx_{m2.group(1)}"
        # 写入设备参数
        w = self._widgets.get("device")
        if isinstance(w, QLineEdit):
            w.setText(dev_id)
        else:
            if self._current_node is not None:
                self.apply_params_merge({"device": dev_id})

    def _sound_auto_calibrate(self):
        import numpy as np
        if self._current_node is None or self._current_node.type != "SoundDetect":
            return
        device = str(self._current_node.params.get("device", "default_loopback") or "default_loopback")
        w_dev = self._widgets.get("device")
        if isinstance(w_dev, QLineEdit):
            device = w_dev.text()
        QMessageBox.information(
            self, "📏 自动校准",
            "将用 5 秒采集当前环境底噪，\n请保持安静（或播放你预期'应该触发'的最小音量声音），\n点确定后开始采样…"
        )
        from src.blueprint.runtime.executor import _capture_sound_blocking
        sr = 44100
        chunk = _capture_sound_blocking(device, 5.0, sr)
        if chunk is None:
            QMessageBox.critical(self, "校准失败", "采集失败，请检查：\n1. pip install soundcard PyAudio\n2. 设备ID是否正确\n3. 扬声器/麦克风是否正常工作")
            return
        peak_db = 20.0 * np.log10(float(np.max(np.abs(chunk)) + 1e-8))
        mean_db = 20.0 * np.log10(float(np.mean(np.abs(chunk)) + 1e-8))
        p99_db = 20.0 * np.log10(float(np.percentile(np.abs(chunk), 99)) + 1e-8)
        # 阈值 = p99 + 6dB（比底噪高一点就触发），并限定在 [-85, -3]
        thr_db = float(np.clip(p99_db + 6.0, -85.0, -3.0))
        # UI 显示
        msg = (
            f"校准采样完成（5秒 {len(chunk)} 采样点）：\n"
            f"  Peak   = {peak_db:.1f} dB\n"
            f"  Mean   = {mean_db:.1f} dB\n"
            f"  P99    = {p99_db:.1f} dB\n"
            f"→ 推荐阈值 = P99 + 6dB = {thr_db:.1f} dB\n\n是否填入并把模式设为 amplitude_max ？"
        )
        ret = QMessageBox.question(self, "📏 校准结果", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ret != QMessageBox.Yes:
            return
        merge: Dict[str, Any] = {"threshold_db": round(thr_db, 1), "judge_mode": "amplitude_max"}
        # 填控件文本
        w_thr = self._widgets.get("threshold_db")
        if isinstance(w_thr, (QDoubleSpinBox, QSpinBox)):
            try: w_thr.setValue(float(thr_db))
            except Exception: pass
        w_mode = self._widgets.get("judge_mode")
        if isinstance(w_mode, QComboBox):
            idx = w_mode.findText("amplitude_max")
            if idx >= 0: w_mode.setCurrentIndex(idx)
        self.apply_params_merge(merge)
        self._sound_apply_band_visibility()

    def _sound_listen_now(self):
        import numpy as np
        if self._current_node is None or self._current_node.type != "SoundDetect":
            return
        # 取当前控件值（实时编辑未保存也可以生效）
        p: Dict[str, Any] = dict(self._current_node.params or {})
        for pname in ("device", "sample_ms", "threshold_db", "judge_mode",
                       "spectral_band_low_hz", "spectral_band_high_hz", "debug_visual"):
            w = self._widgets.get(pname)
            if w is None:
                continue
            if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                p[pname] = w.value()
            elif isinstance(w, QLineEdit):
                p[pname] = w.text()
            elif isinstance(w, QComboBox):
                p[pname] = w.currentText()
            elif isinstance(w, QCheckBox):
                p[pname] = w.isChecked()
        device = str(p.get("device", "default_loopback") or "default_loopback")
        sample_ms = max(100, int(p.get("sample_ms", 1000) or 1000))
        mode = str(p.get("judge_mode", "amplitude_max") or "amplitude_max")
        thr_db = float(p.get("threshold_db", -30.0) or -30.0)
        band_low = int(p.get("spectral_band_low_hz", 500) or 500)
        band_high = int(p.get("spectral_band_high_hz", 2000) or 2000)
        sr = 44100
        from src.blueprint.runtime.executor import _capture_sound_blocking, _ascii_waveform
        chunk = _capture_sound_blocking(device, sample_ms / 1000.0, sr)
        if chunk is None:
            QMessageBox.critical(self, "🎵 试听失败",
                "采集失败，请检查：\n1. pip install soundcard PyAudio\n"
                "2. PyAudio 安装失败可用：pip install pipwin && pipwin install pyaudio\n"
                "3. 设备ID是否正确")
            return
        # 执行和 execute_SoundDetect 一样的判定
        yes = False
        note = ""
        if mode == "amplitude_max":
            peak = float(np.max(np.abs(chunk)) + 1e-8)
            peak_db = 20.0 * np.log10(peak)
            yes = peak_db >= thr_db
            note = f"Peak = {peak_db:.1f} dB   阈值 = {thr_db:.0f} dB"
        elif mode == "amplitude_mean":
            mean = float(np.mean(np.abs(chunk)) + 1e-8)
            mean_db = 20.0 * np.log10(mean)
            yes = mean_db >= thr_db
            note = f"Mean = {mean_db:.1f} dB   阈值 = {thr_db:.0f} dB"
        else:  # spectral_band
            n = len(chunk)
            if n < 4:
                yes = False
                note = "采样过短"
            else:
                win = np.hanning(n).astype(np.float32)
                spec = np.abs(np.fft.rfft(chunk * win))
                freqs = np.fft.rfftfreq(n, 1.0 / sr)
                lo, hi = sorted([band_low, band_high])
                mask = (freqs >= lo) & (freqs <= hi)
                band_energy = float(np.mean(spec[mask]) + 1e-8) if np.any(mask) else 0.0
                thr_amp = (10.0 ** (thr_db / 20.0)) * 0.05
                yes = band_energy >= thr_amp
                note = f"频段 {lo}-{hi} Hz 能量 = {band_energy:.4f}   阈值振幅 = {thr_amp:.4f}"
        # ASCII 波形
        wave = _ascii_waveform(chunk, peak_marker_db=thr_db)
        verdict = "✅ YES（触发有声）" if yes else "❌ NO（无声/未达阈值）"
        color = "#3fb950" if yes else "#F85149"
        msgbox = QMessageBox(self)
        msgbox.setWindowTitle("🎵 试听判定结果")
        msgbox.setText(
            f'<p style="color:{color}; font-size:14px; font-weight:bold;">{verdict}</p>'
            f"<p><b>{note}</b></p>"
            f"<pre style=\"font-family:Consolas,monospace; background:#0D1117; padding:8px; color:#C9D1D9;\">"
            f"{wave}</pre>"
        )
        msgbox.exec()

    # ================================================================
    # Mouse 专属：按 action 分组显隐参数（避免20+参数一次性糊给用户）
    # ================================================================
    def _apply_mouse_action_visibility(self, initial: bool = False):
        node = self._current_node
        if node is None or node.type != "Mouse":
            return
        # 取 action 当前值：优先 UI 控件实时值，其次 params 保存值
        action_val = str(node.params.get("action", "click") or "click")
        w_action = self._widgets.get("action")
        if isinstance(w_action, QComboBox):
            t = w_action.currentText()
            if t: action_val = t
        # 分组定义：key=参数名, value=哪些action下显示；通用=[]=所有action都显示
        groups: Dict[str, List[str]] = {
            # --- 通用(空list = 永远显示) ---
            "action": [],
            "bind_mode": [],
            # --- click / press / move / scroll ---
            "pos_x": ["click", "press", "move", "scroll"],
            "pos_y": ["click", "press", "move", "scroll"],
            "click_rel": ["click"],
            "offset_x": ["click", "press", "move", "scroll", "drag"],
            "offset_y": ["click", "press", "move", "scroll", "drag"],
            # --- drag 独有 ---
            "from_x": ["drag"],
            "from_y": ["drag"],
            "to_x": ["drag"],
            "to_y": ["drag"],
            "drag_duration_ms": ["drag"],
            # --- 按钮/时长相关 ---
            "button": ["click", "press", "release", "drag"],
            "click_count": ["click"],
            "hold_ms_min": ["click", "press"],
            "hold_ms_max": ["click", "press"],
            "move_speed_ms": ["move", "drag"],
            # --- scroll 独有 ---
            "scroll_dy": ["scroll"],
            "scroll_dx": ["scroll"],
            "scroll_steps": ["scroll"],
        }
        # 初始调用时：在 form 顶部加一个 action 分组提示标题（用QGroupBox更清晰，这里用一个Label简化）
        if initial:
            # 遍历找到第几个参数，在 action 下面插入提示
            tip_text_map = {
                "click": "🖱 【点击模式】：在目标位置单击/双击/三击指定按键",
                "press": "⏳ 【按住模式】：在目标位置按住指定按键（可配N毫秒自动抬）",
                "release": "🆓 【释放模式】：松开前一次按住的按键（不需要坐标）",
                "move": "✈️ 【移动模式】：把光标移到目标位置（不点）",
                "drag": "🔗 【拖拽模式】：从起点按住→移动→释放到终点（原子组合）",
                "scroll": "🎡 【滚动模式】：在目标位置滚滚轮（垂直/水平）",
            }
            title_lbl = QLabel("🖱 鼠标操作 · 按当前 action 显示/隐藏对应参数")
            title_lbl.setStyleSheet("color: #ff9800; font-weight: bold; font-size: 10px;")
            sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setStyleSheet("color: #2D333B;")
            # 在 form 顶部（标题下面）插入分隔 + 提示
            existing_rows = self._form.rowCount()
            if existing_rows > 0:
                self._form.insertRow(0, sep)
                self._form.insertRow(0, title_lbl)
        # 遍历所有分组参数，按 action 控制显隐
        for param_name, visible_actions in groups.items():
            w = self._widgets.get(param_name)
            if w is None:
                continue
            # visible_actions=[] 代表通用，永远显示；否则判断当前 action 是否在列表
            if len(visible_actions) == 0:
                visible = True
            else:
                visible = action_val in visible_actions
            # 遍历 form 找该行，隐藏 label + widget
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(visible)
                    if fld_w:
                        fld_w.setVisible(visible)
                    break

    def _apply_varswitch_mode_visibility(self):
        """VarSwitch：简单模式（var_name 非空）隐藏 cases 行；多分支模式隐藏 var_expr 行，title 写清楚"""
        if self._current_node is None or self._current_node.type != "VarSwitch":
            return
        var_name_val = str(self._current_node.params.get("var_name", "") or "")
        vn_le = self._widgets.get("var_name")
        if isinstance(vn_le, QLineEdit):
            var_name_val = vn_le.text()
        simple = bool(var_name_val.strip())
        cases_w = self._widgets.get("cases")
        var_expr_w = self._widgets.get("var_expr")
        # 遍历 form 找 label-widget 对应行，设置该行显隐
        for row_i in range(self._form.rowCount()):
            lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if cases_w is not None and fld_w is cases_w:
                visible = not simple
                if lab_item and lab_item.widget(): lab_item.widget().setVisible(visible)
                if fld_w: fld_w.setVisible(visible)
            if var_expr_w is not None and fld_w is var_expr_w:
                visible = simple
                if lab_item and lab_item.widget(): lab_item.widget().setVisible(visible)
                if fld_w: fld_w.setVisible(visible)

    def _bind_dynamic_ports_refresh(self, node: Node, label: QLabel, rows_added: List[str]):
        """监听动态参数控件（num_paths/templates/cases/var_name），实时刷新端口列表 Label"""
        from ..core.node_library import _resolve_output_ports_default

        def _refresh(*_):
            # 从 UI 控件上取"正在编辑还没保存到params"的值作为候选（更实时）
            p_live: Dict[str, Any] = dict(node.params or {})
            for pname in ("num_paths", "templates", "cases", "var_name"):
                w = self._widgets.get(pname)
                if w is None: continue
                if isinstance(w, QSpinBox):
                    p_live[pname] = w.value()
                elif isinstance(w, QLineEdit):
                    p_live[pname] = w.text()
                elif isinstance(w, QPlainTextEdit):
                    p_live[pname] = w.toPlainText()
            try:
                ports = _resolve_output_ports_default(node.type, p_live)
                names = [p.name for p in ports]
                label.setText("🔌 当前输出端口：" + "  →  ".join(names))
            except Exception:
                pass

        # 绑定关键控件
        triggers = ["num_paths", "templates", "cases", "var_name"]
        for pname in triggers:
            if pname not in rows_added:
                continue
            w = self._widgets.get(pname)
            if w is None: continue
            if isinstance(w, QSpinBox):
                try: w.valueChanged.disconnect()
                except Exception: pass
                w.valueChanged.connect(_refresh)
            elif isinstance(w, QLineEdit):
                try: w.textChanged.disconnect()
                except Exception: pass
                w.textChanged.connect(_refresh)
            elif isinstance(w, QPlainTextEdit):
                try: w.textChanged.disconnect()
                except Exception: pass
                w.textChanged.connect(_refresh)

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

        if editor == EDITOR_MULTILINE:
            te = QPlainTextEdit(str(current or ""))
            te.setMinimumHeight(100)
            if pd.name == "batch_text":
                te.setPlaceholderText("# 每行 name=value\n# 示例：\nport = 8080\nmsg  = hello world\ncount= {= 1+2 }")
            elif pd.name == "message":
                te.setPlaceholderText("通知内容，支持 {var} ${var} {= expr } 插值\n{ALL_VARS} 会在最后一步展开所有变量")
            elif pd.name == "cases":
                te.setPlaceholderText("多分支：每行 case_id = value\n# 示例：\ncase_a = 100\ncase_b = hello\n# 或直接写 JSON 数组:\n# [{\"id\":\"case_a\",\"value\":\"100\"}, {\"id\":\"case_b\",\"value\":\"hello\"}]")
            # textChanged 信号去抖：批量粘贴时不刷屏（直接连即可，QPlainTextEdit 本来就轻）
            def _on_text(nid=nid, pn=pd.name, widget=te):
                self.param_changed.emit(nid, pn, widget.toPlainText())
            te.textChanged.connect(_on_text)
            te.setStyleSheet(self._edit_style())
            return te

        return None

    def _pick_file(self, line_edit: QLineEdit):
        # 根据当前正在编辑的 param_name / node_type，智能决定打开什么过滤
        node = self._current_node
        node_type = node.type if node else ""
        # 找到对应的 param_name（通过 line_edit 在 self._widgets 的反向查找）
        param_name = ""
        for k, w in self._widgets.items():
            if w is line_edit:
                param_name = k
                break
            # 如果是 PathEditor 的 host 里面的子 QLineEdit，遍历 children
            if isinstance(w, QWidget):
                try:
                    kids = w.findChildren(QLineEdit)
                    if any(kid is line_edit for kid in kids):
                        param_name = k
                        break
                except Exception:
                    pass
        # 默认：图片
        caption = "选择模板图片"
        filt = "图片文件 (*.png *.jpg *.jpeg *.bmp)"
        if node_type == "OpenApp" and param_name == "path":
            caption = "选择可执行文件 (EXE)"
            filt = "可执行文件 (*.exe *.bat *.cmd *.lnk);;所有文件 (*.*)"
        elif param_name in ("working_dir",):
            d = QFileDialog.getExistingDirectory(self, "选择工作目录", line_edit.text() or os.path.expanduser("~"))
            if d:
                line_edit.setText(d)
            return
        elif node_type == "BindWindow" and param_name in ("title", "exe_name", "class_name"):
            caption = "选择文件"
            filt = "所有文件 (*.*)"
        elif param_name in ("template", "template_path", "roi_image_path"):
            caption = "选择模板图片"
            filt = "图片文件 (*.png *.jpg *.jpeg *.bmp)"
        elif param_name in ("path", "exe_path"):
            caption = "选择文件"
            filt = "所有文件 (*.*)"
        path, _ = QFileDialog.getOpenFileName(
            self, caption, line_edit.text() or ".",
            filt,
        )
        if path:
            line_edit.setText(path)

    @staticmethod
    def _edit_style() -> str:
        return (
            "QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {"
            "  background-color: #22272E; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px;"
            "  padding: 3px 6px; font-size: 10px;"
            "}"
            "QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {"
            "  border: 1px solid #58A6FF;"
            "}"
            "QPlainTextEdit { font-family: Consolas, 'Courier New', monospace; }"
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
