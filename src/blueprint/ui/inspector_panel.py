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
    request_snip_for_node = Signal(str, str)  # node_id, mode（"if_img_add" / "if_static_roi" / "if_img_replace_<idx>"）
    request_pick_coord = Signal(str, str)  # node_id, param_name（Mouse 点属性 📍 坐标拾取）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(460)
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
        elif t in ("SetVariable",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>记好账、算明白金币，这可是旅途中顶顶重要的事。"
            flair = "把变量的账本理清楚，后面的伙伴才能不被糊涂账坑害呀。咱最讨厌糊涂的商人了，哼。"
        elif t in ("Mouse", "Keyboard", "PressKey"):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>哟，要亲自动手啦？咱这爪子可是比凡人麻利得多唷。"
            flair = "别慢吞吞的，该出手时就出手。不过要是手滑点错了地方，可别赖咱没提醒哟。"
        elif t in ("OpenApp",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯哼，要套好马车出发咯？把马儿喂饱、把货箱装稳才好上路哟。"
            flair = "程序路径可别写错啦，缰绳拉错方向可是会一头扎进沟里哒～开起来等几秒，让马儿喘口气再跑嘛。"
        elif t in ("BindWindow",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>汝要找的是哪家旅店？门头招牌、掌柜姓名、门牌号——三样对得上，才好推门进去哟。"
            flair = "三个条件填了就得全对上才算配对成功哒。绑错了窗口，后面敲破了门也是对牛弹琴呐，可得瞧仔细咯。"
        elif t in ("WebBrowser",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>要让驿站快马送汝去远方的公告栏？那可得把地址说清楚，别让马儿跑错了方向哟。"
            flair = "URL填好咯，要访问的网址可别错哟～想用哪家浏览器送汝去也可以挑一挑，马儿跑起来别把汝甩下车啦哼。"
        elif t in ("HttpRequest",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>哟，要让商队驿站给远方捎封信啦？咱可盯着别被半道的风刮歪了唷。"
            flair = "驿站地址、封条、正文可都得写清楚咯，回来的回信咱帮汝拆成一袋袋小银子（web_ 变量），汝只管拿去用便是。"
        elif t in ("Notify",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>想让远方的人听见汝的声音？这便如酒馆公告板上的告示哟。"
            flair = "消息写清楚、说大声，才不会传歪了。可别写些含糊其辞的玩意儿，被咱笑话哟。"
        elif t in ("Delay",):
            opening = "🐺 <b style='color:#E3B341'>赫萝：</b>嗯，忙里偷闲，也是旅人的智慧唷。"
            flair = "等一等、缓一缓，后面的路才好走。不过睡过头错过了集市，可别怪咱没叫汝哟。"
        elif t in ("IfImageDetect", "CaptureRegion", "StillnessDetect"):
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
        if node.type == "BindWindow":
            btn_pick = QPushButton("🎯 智能拾取窗口")
            btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
            btn_pick.setToolTip("点击后用鼠标点击目标窗口，自动回填标题/程序名/类名")
            btn_pick.clicked.connect(lambda _=False: self._pick_window_for_bind())
            self._action_layout.addWidget(btn_pick)
            btn_list = QPushButton("📋 从列表选择")
            btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_list.setToolTip("列出当前所有可见窗口供选择")
            btn_list.clicked.connect(lambda _=False: self._list_windows_for_bind())
            self._action_layout.addWidget(btn_list)
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
            btn_test = QPushButton("🔍 测试当前屏幕匹配度")
            btn_test.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn_test.setToolTip("用当前屏幕画面和全部参考图做一次匹配，查看每张图的分数")
            btn_test.clicked.connect(lambda _=False: self._ifimg_test_screen_match())
            self._action_layout.addWidget(btn_test)
            # 仅 search_mode=relative 时显示临时目标窗口拾取按钮
            search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
            if search_mode_val == "relative":
                btn_pick = QPushButton("🎯 拾取临时窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击目标窗口，自动回填临时目标标题/程序名/类名\n（留空=用全局目标程序）")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_ifimg_temp())
                self._action_layout.addWidget(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为临时目标")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_ifimg_temp())
                self._action_layout.addWidget(btn_list)
                btn_clear = QPushButton("🗑 清空临时目标")
                btn_clear.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
                btn_clear.setToolTip("清空临时目标，恢复使用全局目标程序")
                btn_clear.clicked.connect(lambda _=False: self._clear_ifimg_temp_target())
                self._action_layout.addWidget(btn_clear)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "FindImage":
            btn_snip = QPushButton("📸 截取目标")
            btn_snip.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
            btn_snip.setToolTip("全屏框选一个目标区域，自动保存为PNG到templates文件夹并回填模板路径+预览图")
            nid = node.id
            btn_snip.clicked.connect(lambda _=False, n=nid: self.request_snip_for_node.emit(n, "findimg_target"))
            self._action_layout.addWidget(btn_snip)
            btn_import = QPushButton("📂 导入图片")
            btn_import.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_import.setToolTip("从本地文件选择一张图片作为模板，默认打开工程目录templates文件夹")
            btn_import.clicked.connect(lambda _=False: self._findimg_import_image())
            self._action_layout.addWidget(btn_import)
            btn_test = QPushButton("🔍 测试匹配")
            btn_test.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn_test.setToolTip("用当前屏幕与模板做一次匹配，查看结果/分数（不运行蓝图）")
            btn_test.clicked.connect(lambda _=False: self._findimg_test_match())
            self._action_layout.addWidget(btn_test)
            # 仅 search_mode=relative 时显示临时目标窗口拾取按钮
            search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
            if search_mode_val == "relative":
                btn_pick = QPushButton("🎯 拾取临时窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击目标窗口，自动回填临时目标标题/程序名/类名\n（留空=用全局目标程序）")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_findimg_temp())
                self._action_layout.addWidget(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为临时目标")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_findimg_temp())
                self._action_layout.addWidget(btn_list)
                btn_clear = QPushButton("🗑 清空临时目标")
                btn_clear.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
                btn_clear.setToolTip("清空临时目标，恢复使用全局目标程序")
                btn_clear.clicked.connect(lambda _=False: self._clear_findimg_temp_target())
                self._action_layout.addWidget(btn_clear)
            # 特征识别辅助：勾选时显示截取特征按钮
            use_feat = bool(node.params.get("use_feature_assist", False))
            if use_feat:
                btn_feat = QPushButton("✂ 从模板截取特征")
                btn_feat.setStyleSheet(self._btn_style("#BF8700", "#FFD700"))
                btn_feat.setToolTip("在模板图片上框选区域作为特征，自动裁剪并记录相对坐标\n（需要先截取/导入模板图片）")
                btn_feat.clicked.connect(lambda _=False: self._findimg_snip_features())
                self._action_layout.addWidget(btn_feat)
                btn_feat_list = QPushButton("📋 查看特征列表")
                btn_feat_list.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
                btn_feat_list.setToolTip("查看/删除已截取的特征")
                btn_feat_list.clicked.connect(lambda _=False: self._findimg_show_feature_list())
                self._action_layout.addWidget(btn_feat_list)
            self._action_layout.addStretch(1)
            self._action_host.show()
            return
        if node.type == "StillnessDetect":
            # 仅 search_mode=relative 时显示临时目标窗口拾取按钮
            search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
            if search_mode_val == "relative":
                btn_pick = QPushButton("🎯 拾取临时窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击目标窗口，自动回填临时目标标题/程序名/类名\n（留空=用全局目标程序）")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_ifstatic_temp())
                self._action_layout.addWidget(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为临时目标")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_ifstatic_temp())
                self._action_layout.addWidget(btn_list)
                btn_clear = QPushButton("🗑 清空临时目标")
                btn_clear.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
                btn_clear.setToolTip("清空临时目标，恢复使用全局目标程序")
                btn_clear.clicked.connect(lambda _=False: self._clear_ifstatic_temp_target())
                self._action_layout.addWidget(btn_clear)
                self._action_layout.addStretch(1)
                self._action_host.show()
                return
            self._action_host.hide()
            return
        if node.type == "Mouse":
            # 仅 coord_mode=relative 时显示临时目标窗口拾取按钮
            coord_mode_val = str(node.params.get("coord_mode", "relative") or "relative")
            if coord_mode_val == "relative":
                btn_pick = QPushButton("🎯 拾取临时窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击目标窗口，自动回填临时目标标题/程序名/类名\n（留空=用全局目标程序）")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_mouse_temp())
                self._action_layout.addWidget(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为临时目标")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_mouse_temp())
                self._action_layout.addWidget(btn_list)
                btn_clear = QPushButton("🗑 清空临时目标")
                btn_clear.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
                btn_clear.setToolTip("清空临时目标，恢复使用全局目标程序")
                btn_clear.clicked.connect(lambda _=False: self._clear_mouse_temp_target())
                self._action_layout.addWidget(btn_clear)
                self._action_layout.addStretch(1)
                self._action_host.show()
                return
            self._action_host.hide()
            return
        self._action_host.hide()

    def _list_windows_for_bind(self):
        """列出当前所有可见窗口，让用户从列表中选择一个，回填 BindWindow 节点参数。"""
        if self._current_node is None or self._current_node.type != "BindWindow":
            return
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows()
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何可见窗口。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择目标窗口")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)

        tip = QLabel("双击一行选择窗口，或选中后点击「确定」：")
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(tip)

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidgetItem,
            QTableWidget,
        )
        table = QTableWidget(len(wins), 4)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 130)
        table.setColumnWidth(2, 130)
        table.setColumnWidth(3, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            table.setItem(i, 0, QTableWidgetItem(str(w.get("title", ""))))
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
        table.doubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(table, 1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        lay.addWidget(btn_box)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        w = wins[idx]
        merge = {
            "title": str(w.get("title", "") or ""),
            "exe_name": str(w.get("exe_name", "") or ""),
            "class_name": str(w.get("class_name", "") or ""),
        }
        self.apply_params_merge(merge)

    def _pick_window_for_bind(self):
        """智能拾取窗口：全屏半透明遮罩，鼠标移动高亮目标窗口，点击拾取。
        窗口拾取交互。"""
        if self._current_node is None or self._current_node.type != "BindWindow":
            return

        # 找到主窗口（用于隐藏后恢复）
        app_window = self.window()

        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "title": str(res.get("title", "") or ""),
                "exe_name": str(res.get("exe_name", "") or ""),
                "class_name": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            # 延迟显示成功提示，等主窗口恢复后再弹
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            cls_n = str(res.get("class_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "拾取成功",
                    f"已绑定窗口：\n标题：{title}\n程序：{exe}\n类名：{cls_n}"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    # ---- Mouse 临时目标窗口拾取 ----
    def _pick_window_for_mouse_temp(self):
        """智能拾取窗口 → 回填 Mouse 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "temp_target_title": str(res.get("title", "") or ""),
                "temp_target_exe": str(res.get("exe_name", "") or ""),
                "temp_target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "临时目标拾取成功",
                    f"已设置临时目标窗口：\n标题：{title}\n程序：{exe}\n\n此鼠标节点将以此为坐标基准（不影响全局目标程序）。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_mouse_temp(self):
        """从列表选择窗口 → 回填 Mouse 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows()
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何可见窗口。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择临时目标窗口")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)
        tip = QLabel("双击一行选择窗口作为临时目标（留空=用全局目标程序）：")
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(tip)

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidgetItem,
            QTableWidget,
        )
        table = QTableWidget(len(wins), 4)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 130)
        table.setColumnWidth(2, 130)
        table.setColumnWidth(3, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            table.setItem(i, 0, QTableWidgetItem(str(w.get("title", ""))))
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
        table.doubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(table, 1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        lay.addWidget(btn_box)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        w = wins[idx]
        merge = {
            "temp_target_title": str(w.get("title", "") or ""),
            "temp_target_exe": str(w.get("exe_name", "") or ""),
            "temp_target_class": str(w.get("class_name", "") or ""),
        }
        self.apply_params_merge(merge)

    def _clear_mouse_temp_target(self):
        """清空 Mouse 节点的临时目标参数。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        merge = {
            "temp_target_title": "",
            "temp_target_exe": "",
            "temp_target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- FindImage 临时目标窗口拾取 ----
    def _pick_window_for_findimg_temp(self):
        """智能拾取窗口 → 回填 FindImage 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "temp_target_title": str(res.get("title", "") or ""),
                "temp_target_exe": str(res.get("exe_name", "") or ""),
                "temp_target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "临时目标拾取成功",
                    f"已设置临时目标窗口：\n标题：{title}\n程序：{exe}\n\n此找图节点将以此为截图范围和坐标基准（不影响全局目标程序）。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_findimg_temp(self):
        """从列表选择窗口 → 回填 FindImage 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows()
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何可见窗口。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择临时目标窗口")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)
        tip = QLabel("双击一行选择窗口作为找图临时目标（留空=用全局目标程序）：")
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(tip)

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidgetItem,
            QTableWidget,
        )
        table = QTableWidget(len(wins), 4)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 130)
        table.setColumnWidth(2, 130)
        table.setColumnWidth(3, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            table.setItem(i, 0, QTableWidgetItem(str(w.get("title", ""))))
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
        table.doubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(table, 1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        lay.addWidget(btn_box)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        w = wins[idx]
        merge = {
            "temp_target_title": str(w.get("title", "") or ""),
            "temp_target_exe": str(w.get("exe_name", "") or ""),
            "temp_target_class": str(w.get("class_name", "") or ""),
        }
        self.apply_params_merge(merge)

    def _clear_findimg_temp_target(self):
        """清空 FindImage 节点的临时目标参数。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        merge = {
            "temp_target_title": "",
            "temp_target_exe": "",
            "temp_target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- FindImage 特征识别辅助 ----
    def _findimg_snip_features(self):
        """打开模板图片对话框，用户在上面框选多个区域作为特征，自动裁剪+记录相对坐标。"""
        node = self._current_node
        if node is None or node.type != "FindImage":
            return

        # 获取模板图片路径
        tpl_path = str(node.params.get("template", "") or "")
        if not tpl_path or not os.path.exists(tpl_path):
            QMessageBox.warning(self, "无法截取特征", "请先截取或导入一张模板图片。")
            return

        # 加载模板图
        try:
            from PIL import Image as PILImage
            tpl_img = PILImage.open(tpl_path).convert("RGBA")
        except Exception as e:
            QMessageBox.warning(self, "模板图片加载失败", f"无法打开模板图片：\n{e}")
            return

        tw, th = tpl_img.size

        # 打开特征截取对话框
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QListWidget, QListWidgetItem, QScrollArea, QSplitter,
        )
        from PySide6.QtCore import Qt, QRect, QPointF
        from PySide6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QBrush

        dlg = QDialog(self)
        dlg.setWindowTitle("从模板截取特征")
        dlg.resize(900, 650)
        lay = QVBoxLayout(dlg)

        tip = QLabel("在模板图片上拖动鼠标框选特征区域。可框选多个。选完后点「✅ 确认保存」。\n"
                      "特征会记录在模板中的相对位置，运行时用于交叉验证。")
        tip.setStyleSheet("color: #8B949E; font-size: 10px; margin-bottom: 4px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 画布区域
        class FeatureCanvas(QLabel):
            """可框选的模板画布"""
            rects_changed = None  # Signal placeholder

            def __init__(self, pixmap, parent=None):
                super().__init__(parent)
                self._orig_pixmap = pixmap
                self._scaled_pixmap = pixmap.scaledToWidth(800, Qt.SmoothTransformation)
                self.setPixmap(self._scaled_pixmap)
                self.setFixedSize(self._scaled_pixmap.size())
                self._rects = []  # [(x_ratio, y_ratio, w_ratio, h_ratio)]
                self._drawing = False
                self._start = QPointF()
                self._current = QPointF()
                # 缩放比
                self._sx = pixmap.width() / self._scaled_pixmap.width()
                self._sy = pixmap.height() / self._scaled_pixmap.height()

            def mousePressEvent(self, ev):
                if ev.button() == Qt.LeftButton:
                    self._drawing = True
                    self._start = ev.position()
                    self._current = ev.position()

            def mouseMoveEvent(self, ev):
                if self._drawing:
                    self._current = ev.position()
                    self._redraw()

            def mouseReleaseEvent(self, ev):
                if ev.button() == Qt.LeftButton and self._drawing:
                    self._drawing = False
                    self._current = ev.position()
                    # 计算原始图坐标
                    x1 = min(self._start.x(), self._current.x()) * self._sx
                    y1 = min(self._start.y(), self._current.y()) * self._sy
                    x2 = max(self._start.x(), self._current.x()) * self._sx
                    y2 = max(self._start.y(), self._current.y()) * self._sy
                    if (x2 - x1) > 3 and (y2 - y1) > 3:
                        self._rects.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
                    self._redraw()

            def _redraw(self):
                p = QPixmap(self._scaled_pixmap)
                painter = QPainter(p)
                # 已确认的矩形（绿色）
                pen_done = QPen(QColor("#2EA043"), 2)
                for (rx, ry, rw, rh) in self._rects:
                    painter.setPen(pen_done)
                    painter.setBrush(QBrush(QColor(46, 160, 67, 50)))
                    painter.drawRect(int(rx / self._sx), int(ry / self._sy),
                                     int(rw / self._sx), int(rh / self._sy))
                # 当前正在画的矩形（黄色虚线）
                if self._drawing:
                    pen_cur = QPen(QColor("#FFD700"), 2, Qt.DashLine)
                    painter.setPen(pen_cur)
                    painter.setBrush(QBrush(QColor(255, 215, 0, 30)))
                    x1 = min(self._start.x(), self._current.x())
                    y1 = min(self._start.y(), self._current.y())
                    x2 = max(self._start.x(), self._current.x())
                    y2 = max(self._start.y(), self._current.y())
                    painter.drawRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))
                painter.end()
                self.setPixmap(p)

            def get_rects(self):
                return list(self._rects)

            def remove_last(self):
                if self._rects:
                    self._rects.pop()
                    self._redraw()

            def clear_all(self):
                self._rects.clear()
                self._redraw()

        # 将 PIL -> QPixmap
        qimg = QImage(tpl_img.tobytes(), tw, th, QImage.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimg)

        scroll = QScrollArea()
        canvas = FeatureCanvas(pixmap)
        scroll.setWidget(canvas)
        scroll.setWidgetResizable(False)
        lay.addWidget(scroll, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_undo = QPushButton("↩ 撤销上一个")
        btn_undo.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_undo.clicked.connect(lambda: canvas.remove_last())
        btn_row.addWidget(btn_undo)
        btn_clear_all = QPushButton("🗑 清除全部")
        btn_clear_all.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_clear_all.clicked.connect(lambda: canvas.clear_all())
        btn_row.addWidget(btn_clear_all)
        btn_row.addStretch(1)

        count_label = QLabel(f"模板尺寸: {tw}×{th}")
        count_label.setStyleSheet("color: #8B949E; font-size: 11px;")
        btn_row.addWidget(count_label)
        lay.addLayout(btn_row)

        # 底部确认/取消
        btn_box = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_cancel.clicked.connect(dlg.reject)
        btn_box.addWidget(btn_cancel)
        btn_box.addStretch(1)
        btn_ok = QPushButton("✅ 确认保存特征")
        btn_ok.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_ok.clicked.connect(dlg.accept)
        btn_box.addWidget(btn_ok)
        lay.addLayout(btn_box)

        if dlg.exec() != QDialog.Accepted:
            return

        # 收集框选矩形，裁剪特征
        rects = canvas.get_rects()
        if not rects:
            QMessageBox.information(self, "提示", "没有框选任何特征区域。")
            return

        import json, base64, io
        # 读取已有的特征列表
        raw = node.params.get("features_json", "[]")
        try:
            features = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            features = []

        for (rx, ry, rw, rh) in rects:
            # 裁剪特征图
            try:
                crop = tpl_img.crop((rx, ry, rx + rw, ry + rh))
                buf = io.BytesIO()
                crop.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                features.append({
                    "b64": b64,
                    "rel_x": rx,
                    "rel_y": ry,
                    "w": rw,
                    "h": rh,
                })
            except Exception as e:
                print(f"[FeatureSnip] 裁剪特征失败: {e}")

        # 写回参数
        merge = {
            "features_json": json.dumps(features, ensure_ascii=False),
            "feature_template_w": tw,
            "feature_template_h": th,
        }
        # 记录截图时窗口宽（用于缩放回退）
        try:
            from PySide6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.geometry()
                merge["feature_capture_width"] = geo.width()
        except Exception:
            pass

        self.apply_params_merge(merge)
        QMessageBox.information(
            self, "特征保存成功",
            f"已新增 {len(rects)} 个特征（共 {len(features)} 个）。\n"
            f"模板尺寸: {tw}×{th}\n\n"
            f"运行时将用这些特征做交叉验证匹配。"
        )

    def _findimg_show_feature_list(self):
        """查看/删除已截取的特征列表。"""
        node = self._current_node
        if node is None or node.type != "FindImage":
            return

        import json, base64, io
        raw = node.params.get("features_json", "[]")
        try:
            features = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            features = []

        if not features:
            QMessageBox.information(self, "特征列表", "当前没有特征。请先点「✂ 从模板截取特征」添加。")
            return

        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTableWidget, QTableWidgetItem, QAbstractItemView,
            QHeaderView,
        )
        from PySide6.QtGui import QImage

        dlg = QDialog(self)
        dlg.setWindowTitle(f"特征列表（共 {len(features)} 个）")
        dlg.resize(700, 450)
        lay = QVBoxLayout(dlg)

        table = QTableWidget(len(features), 5)
        table.setHorizontalHeaderLabels(["#", "相对位置", "特征尺寸", "预览", ""])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        table.setColumnWidth(0, 40)
        table.setColumnWidth(4, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
        )

        for i, feat in enumerate(features):
            table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            rx = feat.get("rel_x", 0)
            ry = feat.get("rel_y", 0)
            table.setItem(i, 1, QTableWidgetItem(f"({rx}, {ry})"))
            fw = feat.get("w", 0)
            fh = feat.get("h", 0)
            table.setItem(i, 2, QTableWidgetItem(f"{fw}×{fh}"))
            # 预览缩略图
            b64 = feat.get("b64", "")
            if b64:
                try:
                    img_data = base64.b64decode(b64)
                    qimg = QImage.fromData(img_data)
                    if not qimg.isNull():
                        pm = QPixmap.fromImage(qimg).scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        lbl = QLabel()
                        lbl.setPixmap(pm)
                        table.setCellWidget(i, 3, lbl)
                    else:
                        table.setItem(i, 3, QTableWidgetItem("(解码失败)"))
                except Exception:
                    table.setItem(i, 3, QTableWidgetItem("(加载失败)"))
            else:
                table.setItem(i, 3, QTableWidgetItem("(空)"))
            # 删除按钮
            btn_del = QPushButton("删除")
            btn_del.setStyleSheet(self._btn_style("#B62324", "#FF7B72"))
            row_i = i
            def _del_row(checked=False, r=row_i):
                table.removeRow(r)
            btn_del.clicked.connect(_del_row)
            table.setCellWidget(i, 4, btn_del)

        lay.addWidget(table, 1)

        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        btn_save = QPushButton("保存修改")
        btn_save.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_box.addWidget(btn_save)
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_box.addWidget(btn_cancel)
        lay.addLayout(btn_box)

        def _save():
            # 从表格重建特征列表（排除被删除的行）
            remaining = []
            for r in range(table.rowCount()):
                idx_item = table.item(r, 0)
                if idx_item is None:
                    continue
                orig_idx = int(idx_item.text()) - 1
                if 0 <= orig_idx < len(features):
                    remaining.append(features[orig_idx])
            self.apply_params_merge({"features_json": json.dumps(remaining, ensure_ascii=False)})
            dlg.accept()

        btn_save.clicked.connect(_save)
        btn_cancel.clicked.connect(dlg.reject)
        dlg.exec()

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
            # 精简 label 后，详细说明通过 tooltip 显示
            if pd.help:
                lbl.setToolTip(pd.help)
                w.setToolTip(pd.help)
            self._form.addRow(lbl, w)
            self._widgets[pd.name] = w
            rows_added.append(pd.name)
        if not params:
            self._form.addRow(QLabel("（该节点无可编辑参数）"))

        # ================================================================
        # 节点专属后处理：参数渲染完成后，追加增强 UI
        # ================================================================
        # 1) 通用：动态端口节点 → 追加"当前输入/输出端口列表"Label
        # 注意：Sequence虽然 has_dynamic_ports=True（为了画布刷新端口），但不要求在 Inspector 里显示端口Label、+/-按钮 等UI，所以这里排除它。
        is_dynamic = bool(meta is not None and getattr(meta, "has_dynamic_ports", False))
        if node.type == "Sequence":
            is_dynamic = False
        if is_dynamic:
            try:
                from ..core.node_library import _resolve_input_ports_default, _resolve_output_ports_default
                in_ports = _resolve_input_ports_default(node.type, dict(node.params or {}))
                out_ports = _resolve_output_ports_default(node.type, dict(node.params or {}))
                in_names = [p.name for p in in_ports if p.type.value == "data"]
                out_names = [p.name for p in out_ports]
                parts = []
                if in_names:
                    parts.append("📥 数据输入：" + "  ←  ".join(in_names))
                if out_names:
                    parts.append("📤 输出：" + "  →  ".join(out_names))
                txt = "  |  ".join(parts) if parts else "（无动态端口）"
                lab = QLabel("🔌 " + txt)
                lab.setWordWrap(True)
                lab.setStyleSheet(
                    "color:#58A6FF; background:#0B3A5C33; padding:6px 8px;"
                    "border:1px dashed #0B3A5C; border-radius:4px; font-size:9px;"
                )
                self._form.addRow(lab)
                # 实时更新：如果存在 num_data_inputs / num_paths 控件，连信号刷新端口列表
                self._bind_dynamic_ports_refresh(node, lab, rows_added)
            except Exception:
                pass

        # 3) Sequence 专属：num_paths 变化时，上面加的端口列表 Label 也要实时更新（在 _bind_dynamic_ports_refresh 里已经处理）

        # 4) IfImageDetect 专属：隐藏 images_json 文本框（用户不用手敲JSON），下方追加缩略图网格
        if node.type == "IfImageDetect":
            try:
                self._hide_param_row("images_json")
                self._append_ifimg_grid()
                # 绑定 search_mode 下拉框变化，切换临时目标参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("search_mode")
                if isinstance(w_sm, QComboBox):
                    w_sm.currentIndexChanged.connect(lambda *_: self._on_ifimg_search_mode_changed())
                self._apply_ifimg_search_visibility()
            except Exception as e:
                print(f"[Inspector] IfImageDetect UI 出错: {e}")

        # 5) StillnessDetect 专属：隐藏 roi_preview_b64 文本框，下方追加 ROI 预览图
        if node.type == "StillnessDetect":
            try:
                self._hide_param_row("roi_preview_b64")
                self._append_ifstatic_preview()
                # 绑定 search_mode 下拉框变化，切换临时目标参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("search_mode")
                if isinstance(w_sm, QComboBox):
                    w_sm.currentIndexChanged.connect(lambda *_: self._on_ifstatic_search_mode_changed())
                self._apply_ifstatic_search_visibility()
            except Exception as e:
                print(f"[Inspector] StillnessDetect UI 出错: {e}")

        # 6) SoundDetect 专属：追加 设备枚举/自动校准/试听 工具栏面板
        if node.type == "SoundDetect":
            try:
                self._replace_ifsound_device_with_combobox()
                self._append_ifsound_panel()
            except Exception as e:
                print(f"[Inspector] SoundDetect UI 出错: {e}")

        # 6.5) FindImage 专属：隐藏 template_b64 文本框，追加预览图+滚动/动作显隐
        if node.type == "FindImage":
            try:
                self._hide_param_row("template_b64")
                self._hide_param_row("template")
                self._append_findimage_panel()
                # 注意：每次 set_node 创建的是全新控件（_clear_form 已销毁旧控件），
                # 不存在重复连接问题，所以不需要 disconnect()。
                # 如果 disconnect()，会断掉 _build_editor 中连接的 param_changed.emit 信号，
                # 导致用户切换 after_action / auto_scroll 时新值无法写入 node.params！
                w_auto = self._widgets.get("auto_scroll")
                if isinstance(w_auto, QCheckBox):
                    w_auto.stateChanged.connect(lambda _s: self._findimg_apply_param_visibility())
                w_act = self._widgets.get("after_action")
                if isinstance(w_act, QComboBox):
                    w_act.currentIndexChanged.connect(lambda _i: self._findimg_apply_param_visibility())
                # 绑定 search_mode 下拉框变化，切换临时目标参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("search_mode")
                if isinstance(w_sm, QComboBox):
                    w_sm.currentIndexChanged.connect(lambda *_: self._on_findimg_search_mode_changed())
                # 绑定 use_feature_assist 勾选，切换特征参数显隐 + 工具栏按钮
                w_ufa = self._widgets.get("use_feature_assist")
                if isinstance(w_ufa, QCheckBox):
                    w_ufa.stateChanged.connect(lambda _s: self._on_findimg_feature_assist_changed())
                self._findimg_apply_param_visibility(initial=True)
            except Exception as e:
                print(f"[Inspector] FindImage UI 出错: {e}")

        # 7) Mouse 专属：根据 action 分组动态显隐参数，追加提示Label
        if node.type == "Mouse":
            try:
                self._apply_mouse_action_visibility(initial=True)
                # 绑定 action 下拉框变化，实时切分组
                w_action = self._widgets.get("action")
                if isinstance(w_action, QComboBox):
                    w_action.currentIndexChanged.connect(lambda *_: self._apply_mouse_action_visibility(initial=False))
                # 绑定 coord_mode 下拉框变化，切换临时目标参数显隐 + 工具栏按钮
                w_cm = self._widgets.get("coord_mode")
                if isinstance(w_cm, QComboBox):
                    w_cm.currentIndexChanged.connect(lambda *_: self._on_mouse_coord_mode_changed())
            except Exception as e:
                print(f"[Inspector] Mouse UI 出错: {e}")

        # 8) Loop 专属：infinite=True 时隐藏 max_iterations 行
        if node.type == "Loop":
            try:
                self._apply_loop_infinite_visibility()
                w_inf = self._widgets.get("infinite")
                if isinstance(w_inf, QCheckBox):
                    w_inf.toggled.connect(lambda _: self._apply_loop_infinite_visibility())
            except Exception as e:
                print(f"[Inspector] Loop UI 出错: {e}")

        # 9) Sequence 专属：num_paths 改用 +/- 按钮调整，不要原生 SpinBox
        if node.type == "Sequence":
            try:
                self._replace_seq_num_paths_with_buttons()
            except Exception as e:
                print(f"[Inspector] Sequence UI 出错: {e}")

        # 10) Clipboard 专属：根据 mode 隐藏不相关参数
        if node.type == "Clipboard":
            try:
                self._apply_clipboard_mode_visibility()
                w_mode = self._widgets.get("mode")
                if isinstance(w_mode, QComboBox):
                    w_mode.currentIndexChanged.connect(lambda *_: self._apply_clipboard_mode_visibility())
            except Exception as e:
                print(f"[Inspector] Clipboard UI 出错: {e}")

        # 11) SetVariable 专属：num_data_inputs 改用 +/- 按钮调整 + 追加检查格式按钮
        if node.type == "SetVariable":
            try:
                self._replace_num_data_inputs_with_buttons()
                self._append_setvar_save_button()
            except Exception as e:
                print(f"[Inspector] SetVariable UI 出错: {e}")

        # 11.5) VarSwitch 专属：num_data_inputs 改用 +/- 按钮调整 + 追加检查格式按钮
        if node.type == "VarSwitch":
            try:
                self._replace_num_data_inputs_with_buttons()
                self._append_varswitch_check_button()
            except Exception as e:
                print(f"[Inspector] VarSwitch UI 出错: {e}")

        # 12) PressKey 专属：根据 kb_mode 隐藏不相关参数
        if node.type == "PressKey":
            try:
                self._apply_presskey_mode_visibility()
                w_kb = self._widgets.get("kb_mode")
                if isinstance(w_kb, QComboBox):
                    w_kb.currentIndexChanged.connect(lambda *_: self._apply_presskey_mode_visibility())
            except Exception as e:
                print(f"[Inspector] PressKey UI 出错: {e}")

        # 13) Wait 专属：根据 delay_type 隐藏不相关参数
        if node.type == "Wait":
            try:
                self._apply_wait_mode_visibility()
                w_dt = self._widgets.get("delay_type")
                if isinstance(w_dt, QComboBox):
                    w_dt.currentIndexChanged.connect(lambda *_: self._apply_wait_mode_visibility())
            except Exception as e:
                print(f"[Inspector] Wait UI 出错: {e}")

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

    def _get_project_templates_dir(self) -> str:
        """获取当前工程的 templates 目录路径。
        有工程时：返回 <project>/templates（不存在则自动创建）。
        无工程时：返回用户主目录。
        """
        try:
            mw = self.window()
            pm = getattr(mw, "_pm", None)
            if pm and pm.is_open():
                root = pm.project_root
                if root and os.path.isdir(root):
                    tpl_dir = os.path.join(root, "templates")
                    os.makedirs(tpl_dir, exist_ok=True)
                    return tpl_dir
        except Exception:
            pass
        return os.path.expanduser("~")

    def _ifimg_import_files(self):
        default_dir = self._get_project_templates_dir()
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的图片（可多选）",
            default_dir,
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
        """在表单底部追加：统计Label + 缩略图网格 + 逐张删除按钮"""
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
        stat = QLabel(f"🖼️ 参考图列表（共 {n} 张）：全部存在才走 yes；缺任何一张走 no ｜ 单张卡片右下角 ✖ 可删除（不支持一次性全删）")
        stat.setStyleSheet("color: #ff9800; font-weight: bold; font-size: 10px;")
        stat.setWordWrap(True)
        th.addWidget(stat, 1)
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
        # 注意：用户要求必须一张一张删除，此函数保留但UI不再使用（删除按钮已从界面移除）
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

    def _apply_ifimg_search_visibility(self):
        """IfImageDetect：仅 search_mode=relative 时显示临时目标参数"""
        node = self._current_node
        if node is None or node.type != "IfImageDetect":
            return
        search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
        w_sm = self._widgets.get("search_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                search_mode_val = str(d)
        temp_visible = (search_mode_val == "relative")
        for tname in ("temp_target_title", "temp_target_exe", "temp_target_class"):
            w = self._widgets.get(tname)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(temp_visible)
                    if fld_w:
                        fld_w.setVisible(temp_visible)
                    break

    def _on_ifimg_search_mode_changed(self):
        """search_mode 切换时：刷新临时目标参数显隐 + 刷新工具栏按钮"""
        self._apply_ifimg_search_visibility()
        self._populate_action_bar(self._current_node)

    # ---- IfImageDetect 临时目标窗口拾取 ----
    def _pick_window_for_ifimg_temp(self):
        """智能拾取窗口 → 回填 IfImageDetect 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "temp_target_title": str(res.get("title", "") or ""),
                "temp_target_exe": str(res.get("exe_name", "") or ""),
                "temp_target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "临时目标拾取成功",
                    f"已设置临时目标窗口：\n标题：{title}\n程序：{exe}\n\n此检测节点将仅在此窗口范围内截图检测（不影响全局目标程序）。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_ifimg_temp(self):
        """从列表选择窗口 → 回填 IfImageDetect 节点的 temp_target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows()
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何可见窗口。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("选择检测临时目标窗口")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)
        tip = QLabel("双击一行选择窗口作为检测范围（留空=用全局目标程序）：")
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(tip)

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidgetItem,
            QTableWidget,
        )
        table = QTableWidget(len(wins), 4)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 130)
        table.setColumnWidth(2, 130)
        table.setColumnWidth(3, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            table.setItem(i, 0, QTableWidgetItem(str(w.get("title", ""))))
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
        table.doubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(table, 1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        lay.addWidget(btn_box)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        w = wins[idx]
        merge = {
            "temp_target_title": str(w.get("title", "") or ""),
            "temp_target_exe": str(w.get("exe_name", "") or ""),
            "temp_target_class": str(w.get("class_name", "") or ""),
        }
        self.apply_params_merge(merge)

    def _clear_ifimg_temp_target(self):
        """清空 IfImageDetect 节点的临时目标参数。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        merge = {
            "temp_target_title": "",
            "temp_target_exe": "",
            "temp_target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- StillnessDetect 临时目标窗口 + search_mode 显隐 ----
    def _apply_ifstatic_search_visibility(self):
        """StillnessDetect：仅 search_mode=relative 时显示临时目标参数"""
        node = self._current_node
        if node is None or node.type != "StillnessDetect":
            return
        search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
        w_sm = self._widgets.get("search_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                search_mode_val = str(d)
        temp_visible = (search_mode_val == "relative")
        for tname in ("temp_target_title", "temp_target_exe", "temp_target_class"):
            w = self._widgets.get(tname)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(temp_visible)
                    if fld_w:
                        fld_w.setVisible(temp_visible)
                    break

    def _on_ifstatic_search_mode_changed(self):
        self._apply_ifstatic_search_visibility()
        self._populate_action_bar(self._current_node)

    def _pick_window_for_ifstatic_temp(self):
        if self._current_node is None or self._current_node.type != "StillnessDetect":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay
        def _on_result(res):
            if not res:
                return
            self.apply_params_merge({
                "temp_target_title": str(res.get("title", "") or ""),
                "temp_target_exe": str(res.get("exe_name", "") or ""),
                "temp_target_class": str(res.get("class_name", "") or ""),
            })
            from PySide6.QtCore import QTimer
            def _show():
                QMessageBox.information(self, "临时目标拾取成功",
                    f"已设置临时目标窗口：\n标题：{res.get('title','')}\n程序：{res.get('exe_name','')}\n\nROI 坐标将基于此窗口。")
            QTimer.singleShot(250, _show)
        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_ifstatic_temp(self):
        if self._current_node is None or self._current_node.type != "StillnessDetect":
            return
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows()
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何可见窗口。")
            return
        from PySide6.QtWidgets import (
            QAbstractItemView, QHeaderView, QTableWidgetItem, QTableWidget,
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("选择静止检测临时目标窗口")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)
        tip = QLabel("双击一行选择窗口（留空=用全局目标程序）：")
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(tip)
        table = QTableWidget(len(wins), 4)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 130)
        table.setColumnWidth(2, 130)
        table.setColumnWidth(3, 60)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            table.setItem(i, 0, QTableWidgetItem(str(w.get("title", ""))))
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
        table.doubleClicked.connect(lambda *_: dlg.accept())
        lay.addWidget(table, 1)
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        lay.addWidget(btn_box)
        if dlg.exec() != QDialog.Accepted:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        w = wins[rows[0].row()]
        self.apply_params_merge({
            "temp_target_title": str(w.get("title", "") or ""),
            "temp_target_exe": str(w.get("exe_name", "") or ""),
            "temp_target_class": str(w.get("class_name", "") or ""),
        })

    def _clear_ifstatic_temp_target(self):
        if self._current_node is None or self._current_node.type != "StillnessDetect":
            return
        self.apply_params_merge({
            "temp_target_title": "",
            "temp_target_exe": "",
            "temp_target_class": "",
        })

    def _ifimg_test_screen_match(self):
        """「测试当前屏幕匹配度」按钮：当场截全屏与每张参考图做模板匹配并弹窗显示分数"""
        arr = self._parse_ifimg_list()
        if not arr:
            QMessageBox.information(self, "提示", "还没有添加参考图，先截图新增或导入图片～")
            return
        # 拿当前参数
        node = self._current_node
        if node is None:
            return
        p = node.params or {}
        threshold = float(p.get("threshold", 0.9) or 0.9)
        strategy = str(p.get("match_strategy", "hybrid") or "hybrid")
        algo_txt = {"hybrid": "混合匹配", "template": "模板匹配", "feature": "特征匹配"}.get(strategy, strategy)

        # 先最小化主窗口，避免蓝图工具遮挡要识别的画面
        main_win = self.window()
        win_visible = True
        try:
            if main_win and hasattr(main_win, "isVisible"):
                win_visible = bool(main_win.isVisible())
                if win_visible:
                    main_win.hide()
            # 等窗口完全隐藏后再截图
            from PySide6.QtCore import QCoreApplication
            for _ in range(5):
                QCoreApplication.processEvents()
            import time as _t
            _t.sleep(0.3)
        except Exception:
            pass
        try:
            # 1) 先截一张当前屏幕
            import tempfile
            shot_png = ""
            screen_pil = None
            # 优先用全局截图工具（尝试多种方法）
            try:
                from src.blueprint.runtime.screen_capture import DefaultScreenCapture
                cap = DefaultScreenCapture()
                screen_pil = cap.capture_pil()
            except Exception:
                screen_pil = None
            # 兜底：用 PIL.ImageGrab（仅Windows/macOS可用）
            if screen_pil is None:
                try:
                    from PIL import ImageGrab
                    screen_pil = ImageGrab.grab(all_screens=True)
                except Exception as e2:
                    QMessageBox.critical(self, "截图失败", f"无法抓取当前屏幕：\n{e2}")
                    return
            fd, shot_png = tempfile.mkstemp(suffix=".png", prefix="ba_testshot_")
            os.close(fd)
            screen_pil.save(shot_png, format="PNG")
        except Exception as e:
            QMessageBox.critical(self, "截图失败", f"无法抓取当前屏幕：\n{e}")
            return
        finally:
            # 恢复主窗口显示
            try:
                if main_win and win_visible and hasattr(main_win, "show"):
                    main_win.show()
                    from PySide6.QtCore import QCoreApplication
                    for _ in range(3):
                        QCoreApplication.processEvents()
            except Exception:
                pass

        # 2) 逐张匹配，做模板匹配拿最大相关系数
        results: List[str] = []
        import numpy as np
        try:
            import cv2  # opencv做模板匹配，快速
            screen_cv2 = None
            try:
                arr_np = np.fromfile(shot_png, dtype=np.uint8)
                screen_cv2 = cv2.imdecode(arr_np, cv2.IMREAD_COLOR)
            except Exception:
                screen_cv2 = None
            if screen_cv2 is None:
                QMessageBox.critical(self, "读取失败", "无法读取当前截图为OpenCV图像")
                return
            screen_gray = cv2.cvtColor(screen_cv2, cv2.COLOR_BGR2GRAY)

            for idx, item in enumerate(arr):
                name = f"[{idx}]"
                # 读取参考图
                ref_pil = None
                b64 = item.get("b64") if isinstance(item, dict) else None
                if isinstance(b64, str) and b64:
                    try:
                        ref_pil = self._b64_to_pil(b64)
                    except Exception:
                        ref_pil = None
                if ref_pil is None and isinstance(item, dict):
                    path = item.get("path", "") or ""
                    if isinstance(path, str) and path and os.path.exists(path) and Image is not None:
                        try:
                            ref_pil = Image.open(path)
                        except Exception:
                            ref_pil = None
                if ref_pil is None:
                    results.append(f"{name} （无参考图数据）  score=0.00  ✗")
                    continue
                # 转 OpenCV
                if ref_pil.mode not in ("RGB", "L"):
                    ref_pil = ref_pil.convert("RGB")
                ref_cv2 = cv2.cvtColor(np.array(ref_pil), cv2.COLOR_RGB2BGR)
                h, w = ref_cv2.shape[:2]
                sh, sw = screen_cv2.shape[:2]
                if h > sh or w > sw:
                    results.append(f"{name} 参考图({w}x{h})>屏幕({sw}x{sh})  score=0.00  ✗")
                    continue
                try:
                    if strategy == "feature":
                        # 特征匹配：简化版直接模板兜底（不做复杂特征）
                        ref_g = cv2.cvtColor(ref_cv2, cv2.COLOR_BGR2GRAY)
                        res = cv2.matchTemplate(screen_gray, ref_g, cv2.TM_CCOEFF_NORMED)
                    else:
                        # template / hybrid：灰度模板匹配
                        ref_g = cv2.cvtColor(ref_cv2, cv2.COLOR_BGR2GRAY)
                        res = cv2.matchTemplate(screen_gray, ref_g, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    score = float(max_val)
                    hit = score >= threshold
                    tag = "✓ PASS" if hit else "✗ FAIL"
                    loc_txt = f"位置:{max_loc}" if hit else ""
                    results.append(f"{name} {w}x{h}  score={score:.3f} (阈值≥{threshold:.2f}) {tag} {loc_txt}")
                except Exception as e:
                    results.append(f"{name} 匹配出错={e}")
        except ImportError:
            # 没有OpenCV，做PIL+NCC简化版
            results.append("⚠ 未安装opencv-python，改用简化判断：仅检测图片是否完整读取成功")
            for idx, item in enumerate(arr):
                ref_pil = None
                b64 = item.get("b64") if isinstance(item, dict) else None
                if isinstance(b64, str) and b64:
                    try:
                        ref_pil = self._b64_to_pil(b64)
                    except Exception:
                        ref_pil = None
                ok = ref_pil is not None
                results.append(f"[{idx}] 参考图{'可用' if ok else'缺失'}")
        finally:
            try:
                if shot_png and os.path.exists(shot_png):
                    os.unlink(shot_png)
            except Exception:
                pass

        # 弹窗显示结果
        dlg = QDialog(self)
        dlg.setWindowTitle("🔍 当前屏幕匹配结果")
        dlg.resize(560, 420)
        lay = QVBoxLayout(dlg)
        info = QLabel(f"算法：{algo_txt}  |  阈值：≥{threshold:.2f}  |  共 {len(arr)} 张")
        info.setStyleSheet("color:#C9D1D9; font-weight:bold;")
        lay.addWidget(info)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setStyleSheet(
            "QPlainTextEdit { background:#0D1117; color:#C9D1D9;"
            " border:1px solid #30363D; border-radius:4px; padding:8px; font-family: Consolas, monospace; font-size:10px; }"
        )
        edit.setPlainText("\n".join(results))
        lay.addWidget(edit, 1)
        btn_ok = QPushButton("关闭")
        btn_ok.clicked.connect(dlg.accept)
        lay.addWidget(btn_ok)
        dlg.exec()

    # ================================================================
    # FindImage：模板预览 + 滚动/动作显隐 + 测试匹配
    # ================================================================
    def _append_findimage_panel(self):
        node = self._current_node
        if node is None:
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        title = QLabel("🖼️  模板预览")
        title.setStyleSheet("color: #58A6FF; font-weight: bold; font-size: 10px;")
        self._form.addRow(title)

        # 模板信息
        tpl_path = str(node.params.get("template", "") or "")
        info_row = QWidget()
        ir = QHBoxLayout(info_row)
        ir.setContentsMargins(0, 0, 0, 0)
        ir.setSpacing(6)
        if tpl_path:
            try:
                exists = os.path.exists(tpl_path)
            except Exception:
                exists = False
            display = os.path.basename(tpl_path) if exists else (tpl_path[:80] + ("…" if len(tpl_path) > 80 else ""))
            path_lbl = QLabel(f"📂 {display}" + ("" if exists else " ⚠路径不存在"))
        else:
            path_lbl = QLabel("（未设置模板，点 [📸 截取目标] 或在上方填写路径）")
        path_lbl.setStyleSheet("color:#C9D1D9; font-size:9px;")
        path_lbl.setWordWrap(True)
        ir.addWidget(path_lbl, 1)
        self._form.addRow(info_row)

        # 预览图：优先用 template_b64，否则尝试从 template 路径读
        b64 = str(node.params.get("template_b64", "") or "")
        pil = None
        if isinstance(b64, str) and b64:
            try:
                pil = self._b64_to_pil(b64)
            except Exception:
                pil = None
        if pil is None and isinstance(tpl_path, str) and tpl_path:
            try:
                if os.path.exists(tpl_path) and Image is not None:
                    pil = Image.open(tpl_path)
            except Exception:
                pil = None
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
        try:
            pil.thumbnail((220, 220), Image.LANCZOS if Image else None)
        except Exception:
            pass
        pix = self._pil_to_qpixmap(pil, size=220)
        if pix is not None:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setPixmap(pix)
            lbl.setStyleSheet(
                "QLabel { background:#000; border:2px solid #0969DA; border-radius:4px; padding:2px; }"
            )
            try:
                lbl.setToolTip(f"模板实际尺寸：{pil.width} × {pil.height} 像素")
            except Exception:
                pass
            self._form.addRow(lbl)

    def _findimg_apply_param_visibility(self, initial: bool = False):
        """根据 auto_scroll / after_action，通过 self._widgets 精准定位行并显隐。
        直接从控件读当前值，避免信号触发时 node.params 还没更新的时序问题。
        """
        node = self._current_node
        if node is None:
            return

        # 直接从控件读取当前值（解决 currentIndexChanged 信号触发时 node.params 未更新的时序问题）
        auto_scroll = bool(node.params.get("auto_scroll", False))
        w_auto = self._widgets.get("auto_scroll")
        if isinstance(w_auto, QCheckBox):
            auto_scroll = w_auto.isChecked()

        after_action = str(node.params.get("after_action", "none") or "none")
        w_act = self._widgets.get("after_action")
        if isinstance(w_act, QComboBox):
            data = w_act.currentData()
            if data is not None:
                after_action = str(data)
            else:
                after_action = str(w_act.currentText() or "none")

        show_offsets = after_action != "none"

        # 特征识别辅助参数：仅 use_feature_assist 勾选时显示
        use_feat = bool(node.params.get("use_feature_assist", False))
        w_ufa = self._widgets.get("use_feature_assist")
        if isinstance(w_ufa, QCheckBox):
            use_feat = w_ufa.isChecked()

        form = self._form
        if form is None:
            return

        # 建立 "widget对象 -> 参数名" 的反向表（EDITOR_TUPLE2/容器类：widget本身就是字段容器）
        widget_to_name = {}
        for name, w in self._widgets.items():
            if isinstance(w, QWidget):
                widget_to_name[id(w)] = name

        # 参数名 -> 是否可见 的映射表
        vis_map = {
            "scroll_dy": auto_scroll,
            "scroll_dx": auto_scroll,
            "offset": show_offsets,
            # 特征识别辅助参数
            "features_json": use_feat,
            "feature_tolerance": use_feat,
            "feature_min_hits": use_feat,
            "feature_capture_width": use_feat,
            "feature_template_w": use_feat,
            "feature_template_h": use_feat,
        }

        # 临时目标参数：仅 search_mode=relative 时显示
        search_mode_val = str(node.params.get("search_mode", "relative") or "relative")
        w_sm = self._widgets.get("search_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                search_mode_val = str(d)
        temp_visible = (search_mode_val == "relative")
        for tname in ("temp_target_title", "temp_target_exe", "temp_target_class"):
            vis_map[tname] = temp_visible

        for i in range(form.rowCount()):
            li = form.itemAt(i, QFormLayout.LabelRole)
            fi = form.itemAt(i, QFormLayout.FieldRole)
            lw = li.widget() if li else None
            fw = fi.widget() if fi else None
            if fw is None:
                continue
            matched_name = widget_to_name.get(id(fw))
            if matched_name is None or matched_name not in vis_map:
                continue
            target_vis = vis_map[matched_name]
            if isinstance(lw, QWidget):
                lw.setVisible(target_vis)
            if isinstance(fw, QWidget):
                fw.setVisible(target_vis)

    def _on_findimg_search_mode_changed(self):
        """search_mode 切换时：刷新临时目标参数显隐 + 刷新工具栏按钮"""
        self._findimg_apply_param_visibility(initial=False)
        self._populate_action_bar(self._current_node)

    def _on_findimg_feature_assist_changed(self):
        """use_feature_assist 勾选时：刷新特征参数显隐 + 刷新工具栏按钮"""
        self._findimg_apply_param_visibility(initial=False)
        self._populate_action_bar(self._current_node)

    def _findimg_test_match(self):
        """「🔍 测试匹配」按钮：不运行蓝图，现场截全屏做一次匹配。"""
        node = self._current_node
        if node is None:
            return
        p = node.params or {}
        tpl_path = str(p.get("template", "") or "")
        tpl_b64 = str(p.get("template_b64", "") or "")
        threshold = float(p.get("threshold", 0.75) or 0.75)
        strategy = str(p.get("match_strategy", "hybrid") or "hybrid")
        algo_txt = {"hybrid": "智能混合", "template": "模板匹配", "feature": "特征匹配"}.get(strategy, strategy)

        # 找到模板pil
        tpl_pil = None
        if isinstance(tpl_b64, str) and tpl_b64:
            try:
                tpl_pil = self._b64_to_pil(tpl_b64)
            except Exception:
                tpl_pil = None
        if tpl_pil is None and isinstance(tpl_path, str) and tpl_path:
            try:
                if os.path.exists(tpl_path) and Image is not None:
                    tpl_pil = Image.open(tpl_path)
            except Exception:
                tpl_pil = None
        if tpl_pil is None:
            QMessageBox.information(self, "提示", "还没有模板，请先点 [📸 截取目标] 或填写模板路径")
            return

        # 先最小化主窗口，避免蓝图工具遮挡要识别的画面
        main_win = self.window()
        win_visible = True
        try:
            if main_win and hasattr(main_win, "isVisible"):
                win_visible = bool(main_win.isVisible())
                if win_visible:
                    main_win.hide()
            from PySide6.QtCore import QCoreApplication
            for _ in range(5):
                QCoreApplication.processEvents()
            import time as _t
            _t.sleep(0.3)
        except Exception:
            pass

        # 截全屏
        import tempfile
        shot_png = ""
        try:
            from PIL import ImageGrab
            screen_pil = ImageGrab.grab(all_screens=True)
            fd, shot_png = tempfile.mkstemp(suffix=".png", prefix="ba_findtest_")
            os.close(fd)
            screen_pil.save(shot_png, format="PNG")
        except Exception as e:
            try:
                if main_win and win_visible and hasattr(main_win, "show"):
                    main_win.show()
            except Exception:
                pass
            QMessageBox.critical(self, "截图失败", f"无法抓取当前屏幕：\n{e}")
            try:
                if shot_png and os.path.exists(shot_png):
                    os.unlink(shot_png)
            except Exception:
                pass
            return
        finally:
            # 恢复主窗口显示（截图完就立刻恢复，不用等匹配完成）
            try:
                if main_win and win_visible and hasattr(main_win, "show"):
                    main_win.show()
                    from PySide6.QtCore import QCoreApplication
                    for _ in range(3):
                        QCoreApplication.processEvents()
            except Exception:
                pass

        # 把模板也存临时
        tpl_png = ""
        try:
            fd, tpl_png = tempfile.mkstemp(suffix=".png", prefix="ba_tpltest_")
            os.close(fd)
            tpl_pil.save(tpl_png, format="PNG")
        except Exception as e:
            QMessageBox.critical(self, "临时文件失败", f"{e}")
            try:
                for _p in (shot_png, tpl_png):
                    if _p and os.path.exists(_p):
                        os.unlink(_p)
            except Exception:
                pass
            return

        # OpenCV 模板匹配（简化版，和 IfImageDetect 测试一致）
        result_txt = ""
        import numpy as np
        try:
            import cv2
            screen_arr = np.fromfile(shot_png, dtype=np.uint8)
            screen_cv2 = cv2.imdecode(screen_arr, cv2.IMREAD_COLOR)
            tpl_arr = np.fromfile(tpl_png, dtype=np.uint8)
            tpl_cv2 = cv2.imdecode(tpl_arr, cv2.IMREAD_COLOR)
            if screen_cv2 is None or tpl_cv2 is None:
                result_txt = "ERROR: 读取图像解码失败"
            else:
                th, tw = tpl_cv2.shape[:2]
                sh, sw = screen_cv2.shape[:2]
                if th > sh or tw > sw:
                    result_txt = (
                        f"模板({tw}x{th}) 大于 屏幕({sw}x{sh})，不可能匹配\n"
                        f"建议重新截小一点的目标"
                    )
                else:
                    screen_g = cv2.cvtColor(screen_cv2, cv2.COLOR_BGR2GRAY)
                    tpl_g = cv2.cvtColor(tpl_cv2, cv2.COLOR_BGR2GRAY)
                    res = cv2.matchTemplate(screen_g, tpl_g, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    score = float(max_val)
                    ok = score >= threshold
                    status = "✅ 找到" if ok else "❌ 未找到"
                    cx = int(max_loc[0]) + tw // 2
                    cy = int(max_loc[1]) + th // 2
                    result_txt = (
                        f"算法: {algo_txt}\n"
                        f"阈值: ≥{threshold:.2f}\n"
                        f"模板尺寸: {tw}x{th}\n"
                        f"屏幕尺寸: {sw}x{sh}\n"
                        f"最高分数: {score:.3f}\n"
                        f"结果: {status}\n"
                        f"命中位置: 左上角={max_loc}  (中心=({cx}, {cy}))"
                    )
        except ImportError:
            result_txt = "⚠ 未安装 opencv-python，无法测试匹配。\n请先 pip install opencv-python。"
        except Exception as e:
            result_txt = f"匹配出错: {e}"
        finally:
            for _p in (shot_png, tpl_png):
                if _p and os.path.exists(_p):
                    try:
                        os.unlink(_p)
                    except Exception:
                        pass

        dlg = QDialog(self)
        dlg.setWindowTitle("🔍 找图-测试匹配结果")
        dlg.resize(480, 320)
        lay = QVBoxLayout(dlg)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setStyleSheet(
            "QPlainTextEdit { background:#0D1117; color:#C9D1D9;"
            " border:1px solid #30363D; border-radius:4px; padding:8px;"
            " font-family: Consolas, monospace; font-size:10px; }"
        )
        edit.setPlainText(result_txt)
        lay.addWidget(edit, 1)
        btn_ok = QPushButton("关闭")
        btn_ok.clicked.connect(dlg.accept)
        lay.addWidget(btn_ok)
        dlg.exec()

    # ---- FindImage 专属：导入图片 ----
    def _findimg_import_image(self):
        """导入一张本地图片作为模板，默认打开工程目录的 templates 文件夹"""
        node = self._current_node
        if node is None:
            return
        default_dir = self._get_project_templates_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择一张模板图片导入",
            default_dir,
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not (isinstance(path, str) and path and os.path.exists(path)):
            return
        # 读取图片并生成 b64 预览缩略图
        pil = None
        try:
            if Image is not None:
                pil = Image.open(path)
        except Exception:
            pil = None
        # 保存到 templates 目录（对齐「📸 截取目标」的行为）
        final_path = path
        try:
            import shutil, uuid, time
            from src.config.app_paths import template_dir
            tpl_dir = template_dir()
            os.makedirs(tpl_dir, exist_ok=True)
            ext = os.path.splitext(path)[1] or ".png"
            fname = f"findimg_import_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
            fpath = os.path.join(tpl_dir, fname)
            shutil.copy2(path, fpath)
            final_path = fpath
        except Exception as e_img:
            # 未开工程等场景：template_dir() 会直接抛 RuntimeError，必须显式拒绝而不是静默吞
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "导入模板失败",
                    f"无法将模板图片拷贝到工程 templates/ 目录：\n{e_img}\n\n"
                    "请先新建或打开一个 BlueprintAutomation 工程后再导入图片。"
                )
            except Exception:
                pass
            return
        # 生成预览 b64（缩略图，避免过大）
        b64 = ""
        if pil is not None:
            try:
                import base64 as _b64, io as _io
                preview = pil.copy()
                try:
                    preview.thumbnail((220, 220))
                except Exception:
                    pass
                buf = _io.BytesIO()
                preview.save(buf, format="PNG")
                b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                b64 = ""
        # 回填 template 路径 + template_b64
        self.param_changed.emit(node.id, "template", final_path)
        self.param_changed.emit(node.id, "template_b64", b64)
        # 刷新面板显示
        self.set_node(node)

    # ---- FindImage 专属：偏移量加「📍 定位偏移位置」按钮 ----
    def _findimg_add_offset_pick_button(self):
        """在 offset 区域下，单独加一行「📍 定位偏移位置」按钮，点后在屏幕取点，算出相对目标模板左上角的偏移。"""
        node = self._current_node
        if node is None or node.type != "FindImage":
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        btn = QPushButton("📍 定位偏移位置（取点）")
        btn.setStyleSheet(self._btn_style("#8957E5", "#A371F7"))
        btn.setToolTip(
            "点击后在屏幕上点一个位置，程序会把这个位置与模板左上角的差值写入偏移 X/Y。\n"
            "前提：先确定当前屏幕中模板所在的位置（或用找图的 out_pos 作为参考基准），再点目标点，两点的差值就是偏移。\n"
            "（简化版：点一下屏幕上的真实坐标 → X/Y 直接写入该点相对屏幕左上角的绝对值，方便调试）"
        )
        nid = node.id
        btn.clicked.connect(lambda _=False, n=nid: self.request_pick_coord.emit(n, "findimg_offset"))
        bl.addWidget(btn)
        bl.addStretch(1)
        self._form.addRow(btn_row)

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

        # 框选ROI按钮（放到静止检测ROI的上面）
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        btn_roi = QPushButton("📍 框选ROI区域")
        btn_roi.setStyleSheet(self._btn_style("#d84315", "#ff7043"))
        btn_roi.setToolTip("全屏框选一个矩形区域作为静止检测ROI")
        nid = node.id
        btn_roi.clicked.connect(lambda _=False, n=nid: self.request_snip_for_node.emit(n, "if_static_roi"))
        bl.addWidget(btn_roi)
        btn_clear_roi = QPushButton("🗑️ 清除ROI")
        btn_clear_roi.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_clear_roi.setToolTip("清除已选的ROI区域和预览图")
        btn_clear_roi.clicked.connect(lambda _=False, n=node: self._clear_ifstatic_roi(n.id))
        bl.addWidget(btn_clear_roi)
        bl.addStretch(1)
        self._form.addRow(btn_row)

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
        hint_lbl = QLabel("（未设置，请点上面 [📍 框选ROI区域]）" if not has_roi else "（已设置）")
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

    def _clear_ifstatic_roi(self, node_id: str):
        """清除静止检测ROI和预览图"""
        self.param_changed.emit(node_id, "roi", "[0,0,0,0]")
        self.param_changed.emit(node_id, "roi_preview_b64", "")
        # 刷新面板显示
        if self._current_node and self._current_node.id == node_id:
            self.set_node(self._current_node)

    # ================================================================
    # SoundDetect 专属：设备下拉框替换、枚举设备 / 自动校准 / 试听
    # ================================================================
    def _replace_ifsound_device_with_combobox(self):
        """SoundDetect：device 输入框替换为 QComboBox（打开面板时自动枚举设备，无刷新按钮）"""
        if self._current_node is None or self._current_node.type != "SoundDetect":
            return
        w_dev = self._widgets.get("device")
        if w_dev is None:
            return
        target_row = -1
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w_dev:
                target_row = row_i
                break
        if target_row < 0:
            return
        container = QWidget()
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(4)
        combo = QComboBox()
        combo.setStyleSheet(self._edit_style() + " QComboBox QAbstractItemView { min-width: 480px; }")
        combo.setEditable(False)
        combo.setMinimumWidth(260)
        combo.setMaximumWidth(260)
        hlay.addWidget(combo)
        cur_val = str(self._current_node.params.get("device", "default_loopback") or "default_loopback")
        if isinstance(w_dev, QLineEdit):
            try:
                cur_val = w_dev.text() or cur_val
            except Exception:
                pass
        self._form.removeRow(target_row)
        new_label = QLabel("音频设备ID")
        new_label.setStyleSheet("color: #C9D1D9; font-size: 9px;")
        new_label.setToolTip("选择音频输入/输出设备（扬声器环回可录制系统自己播放的声音；麦克风录制外界声音")
        self._form.insertRow(target_row, new_label, container)
        self._widgets["device"] = combo
        container._combo = combo  # type: ignore[attr-defined]
        self._sound_refresh_devices(combo, cur_val)
        def _on_change(_i, c=combo):
            try:
                data = c.currentData()
                val = str(data) if data is not None else c.currentText()
            except Exception:
                val = c.currentText()
            self.param_changed.emit(self._current_node.id, "device", str(val or "default_loopback"))
        try: combo.currentIndexChanged.disconnect()
        except Exception: pass
        combo.currentIndexChanged.connect(_on_change)

    def _sound_refresh_devices(self, combo: QComboBox, select_val: str = ""):
        """枚举音频设备并填充 QComboBox；仅用 soundcard（无PyAudio重复），按id去重避免一堆重复项。"""
        if combo is None:
            return
        try:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("🎙️ 默认扬声器（环回，录制系统播放声） [推荐]", "default_loopback")
            seen_ids = set()
            lb_items = []
            mic_items = []
            try:
                import soundcard as sc
                for mic in sc.all_microphones(include_loopback=True):
                    mid = str(getattr(mic, "id", ""))
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    name = getattr(mic, "name", mid)
                    is_lb = ("loopback" in mid.lower()) or ("Loopback" in name) or ("扬声器" in name) or ("Speakers" in name) or ("喇叭" in name) or ("Render" in name) or ("Output" in name)
                    # 跳过明显的驱动虚拟项（减少列表噪音）：Offload / Headset Endpoint / 纯数字通道别名
                    skip_keywords = ("Offload", "Headset Endpoint", "Digital Audio", "HDMI Out", "Optical Out", "S/PDIF")
                    if any(k in name for k in skip_keywords):
                        continue
                    show_name = str(name).strip() or mid
                    if is_lb:
                        lb_items.append((f"🔊 {show_name}  [扬声器·可录系统声]", mid))
                    else:
                        mic_items.append((f"🎤 {show_name}  [麦克风]", mid))
            except Exception:
                pass
            for show, mid in lb_items:
                combo.addItem(show, mid)
            for show, mid in mic_items:
                combo.addItem(show, mid)
            if combo.count() == 0:
                combo.addItem("default_loopback", "default_loopback")
                combo.addItem("（请先 pip install soundcard 后重新打开面板）", "")
            target_idx = -1
            sv = str(select_val)
            for i in range(combo.count()):
                d = None
                try:
                    d = combo.itemData(i)
                except Exception:
                    pass
                if d is not None and str(d) == sv:
                    target_idx = i
                    break
                if combo.itemText(i) == sv:
                    target_idx = i
                    break
            if target_idx >= 0:
                combo.setCurrentIndex(target_idx)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _sound_check_deps(self) -> bool:
        """检查 soundcard / PyAudio 是否安装，未安装弹提示并返回 False"""
        ok_sc = True
        ok_pa = True
        try:
            import soundcard  # noqa: F401
        except Exception:
            ok_sc = False
        try:
            import pyaudio  # noqa: F401
        except Exception:
            ok_pa = False
        if ok_sc and ok_pa:
            return True
        tips = []
        if not ok_sc:
            tips.append("soundcard 未安装")
        if not ok_pa:
            tips.append("PyAudio 未安装")
        msg = (
            "声音检测依赖以下 Python 库：\n\n"
            "  ❌ " + "；".join(tips) + "\n\n"
            "请先在终端执行：\n"
            "  pip install soundcard pyaudio\n\n"
            "如 PyAudio 安装失败，可尝试：\n"
            "  pip install pipwin && pipwin install pyaudio\n\n"
            "安装完成后请检查：\n"
            "  1. 重新选中本节点刷新设备列表\n"
            "  2. 扬声器/麦克风是否正常工作（系统音量面板能看到波形）"
        )
        QMessageBox.warning(self, "缺少音频依赖", msg)
        return False

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

        # 工具按钮行（去掉「枚举设备」：因为下拉框已经内置了🔄刷新）
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)

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
            d = w_mode.currentData()
            if d:
                mode_val = str(d)
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
        if not self._sound_check_deps():
            return
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
        if not self._sound_check_deps():
            return
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
                d = w.currentData()
                p[pname] = d if d is not None else w.currentText()
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
        # 取 action 当前值：优先 UI 控件实时值（currentData 返回英文 key），其次 params 保存值
        action_val = str(node.params.get("action", "click") or "click")
        w_action = self._widgets.get("action")
        if isinstance(w_action, QComboBox):
            d = w_action.currentData()
            if d: action_val = str(d)
        # 分组定义：key=参数名, value=哪些action下显示；通用=[]=所有action都显示
        groups: Dict[str, List[str]] = {
            # --- 通用(空list = 永远显示) ---
            "coord_mode": [],
            "action": [],
            # --- 目标点（click/press/move/scroll 用）---
            "pos": ["click", "press", "move", "scroll"],
            # --- 偏移量（click/press/move/scroll/drag 用）---
            "offset": ["click", "press", "move", "scroll", "drag"],
            # --- drag 独有 ---
            "from_pos": ["drag"],
            "to_pos": ["drag"],
            "drag_duration_ms": ["drag"],
            # --- 按钮/时长相关 ---
            "button": ["click", "press", "release", "drag"],
            "click_count": ["click"],
            "hold_ms_min": ["click", "press"],
            "hold_ms_max": ["click", "press"],
            "post_delay_min_ms": ["click", "press"],
            "post_delay_max_ms": ["click", "press"],
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

        # 临时目标参数：仅 coord_mode=relative 时显示
        coord_mode_val = str(node.params.get("coord_mode", "relative") or "relative")
        w_cm = self._widgets.get("coord_mode")
        if isinstance(w_cm, QComboBox):
            d = w_cm.currentData()
            if d:
                coord_mode_val = str(d)
        temp_visible = (coord_mode_val == "relative")
        for tname in ("temp_target_title", "temp_target_exe", "temp_target_class"):
            w = self._widgets.get(tname)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(temp_visible)
                    if fld_w:
                        fld_w.setVisible(temp_visible)
                    break

    def _on_mouse_coord_mode_changed(self):
        """coord_mode 切换时：刷新临时目标参数显隐 + 刷新工具栏按钮"""
        self._apply_mouse_action_visibility(initial=False)
        self._populate_action_bar(self._current_node)

    def _apply_loop_infinite_visibility(self):
        """Loop：infinite=True 时隐藏 max_iterations 行"""
        if self._current_node is None or self._current_node.type != "Loop":
            return
        w_inf = self._widgets.get("infinite")
        infinite = False
        if isinstance(w_inf, QCheckBox):
            infinite = w_inf.isChecked()
        else:
            infinite = bool(self._current_node.params.get("infinite", False))
        w_max = self._widgets.get("max_iterations")
        if w_max is None:
            return
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w_max:
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                if lab_item and lab_item.widget():
                    lab_item.widget().setVisible(not infinite)
                if fld_w:
                    fld_w.setVisible(not infinite)
                break

    def _apply_presskey_mode_visibility(self):
        """PressKey：text 模式显示 text/use_paste/press_enter；key 模式显示 key_combo/count/hold_ms/interval_ms"""
        if self._current_node is None or self._current_node.type != "PressKey":
            return
        kb_mode = str(self._current_node.params.get("kb_mode", "text") or "text")
        w_kb = self._widgets.get("kb_mode")
        if isinstance(w_kb, QComboBox):
            d = w_kb.currentData()
            if d:
                kb_mode = str(d)
        is_text = kb_mode == "text"
        # text 模式参数 / key 模式参数
        text_params = ["text", "use_paste", "press_enter"]
        key_params = ["key_combo", "count", "hold_ms", "interval_ms"]
        for row_i in range(self._form.rowCount()):
            lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is None:
                continue
            # 在 text_params / key_params 中按 widget 引用查找
            for pn in text_params:
                if fld_w is self._widgets.get(pn):
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(is_text)
                    fld_w.setVisible(is_text)
                    break
            for pn in key_params:
                if fld_w is self._widgets.get(pn):
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(not is_text)
                    fld_w.setVisible(not is_text)
                    break

        # key 模式时：隐藏原始 key_combo 文本框，追加自定义组合选择 UI
        if not is_text:
            self._hide_param_row("key_combo")
            self._append_keycombo_picker()
        else:
            # text 模式时移除组合选择 UI
            self._remove_keycombo_picker()

    # ---- PressKey 按键组合选择器 ----
    # 按键分类定义：(分类名, [(显示名, 键值), ...])
    _KEY_CATEGORIES = None  # 延迟初始化

    @classmethod
    def _get_key_categories(cls):
        """按键分类（延迟初始化，只构建一次）"""
        if cls._KEY_CATEGORIES is not None:
            return cls._KEY_CATEGORIES
        import string
        cats = []
        # 修饰键
        cats.append(("修饰键", [
            ("Ctrl", "ctrl"), ("Alt", "alt"), ("Shift", "shift"), ("Win", "win"),
        ]))
        # 字母
        cats.append(("字母", [(c, c) for c in string.ascii_lowercase]))
        # 数字
        cats.append(("数字", [(str(n), str(n)) for n in range(10)]))
        # 功能键
        cats.append(("功能键", [(f"F{n}", f"f{n}") for n in range(1, 13)]))
        # 方向键
        cats.append(("方向键", [
            ("↑", "up"), ("↓", "down"), ("←", "left"), ("→", "right"),
        ]))
        # 编辑控制
        cats.append(("编辑控制", [
            ("Enter", "enter"), ("Space", "space"), ("Esc", "esc"), ("Tab", "tab"),
            ("Backspace", "backspace"), ("Delete", "delete"), ("Insert", "insert"),
            ("Home", "home"), ("End", "end"), ("PageUp", "pageup"), ("PageDown", "pagedown"),
            ("CapsLock", "capslock"),
        ]))
        # 其他
        cats.append(("其他", [
            ("PrintScreen", "printscreen"), ("ScrollLock", "scrolllock"),
            ("NumLock", "numlock"), ("Pause", "pause"),
        ]))
        # 符号键
        cats.append(("符号键", [
            ("-_", "minus"), ("=+", "equals"), ("[{", "lbracket"),
            ("]}", "rbracket"), (";:", "semicolon"), ("'\"", "quote"),
            ("\\|", "backslash"), (",<", "comma"), (".>", "period"),
            ("/?", "slash"), ("`~", "backquote"),
        ]))
        cls._KEY_CATEGORIES = cats
        return cats

    def _append_keycombo_picker(self):
        """在 form 下方追加按键组合选择器 UI"""
        # 先移除旧的
        self._remove_keycombo_picker()

        node = self._current_node
        if node is None or node.type != "PressKey":
            return

        # 读取当前 key_combo
        import json
        raw = node.params.get("key_combo", "[]")
        try:
            current_keys = json.loads(raw) if isinstance(raw, str) and raw.strip() else []
        except Exception:
            current_keys = []
        # 向后兼容 key_name
        if not current_keys:
            old_kn = str(node.params.get("key_name", "") or "").strip()
            if old_kn:
                current_keys = [k.strip().lower() for k in old_kn.split("+") if k.strip()]

        # 创建容器
        container = QFrame()
        container.setObjectName("_keycombo_picker")
        container.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #2D333B; border-radius: 6px; }"
            "QLabel { color: #C9D1D9; }"
        )
        main_lay = QVBoxLayout(container)
        main_lay.setContentsMargins(8, 8, 8, 8)
        main_lay.setSpacing(6)

        # --- 当前组合显示区 ---
        combo_label = QLabel("当前组合：")
        combo_label.setStyleSheet("font-size: 11px; color: #8B949E;")
        main_lay.addWidget(combo_label)

        chips_lay = QHBoxLayout()
        chips_lay.setSpacing(4)
        chips_widget = QWidget()
        chips_widget.setLayout(chips_lay)
        chips_widget.setObjectName("_keycombo_chips")
        main_lay.addWidget(chips_widget)

        # --- 分类下拉框区 ---
        cat_label = QLabel("从下方选择按键添加到组合：")
        cat_label.setStyleSheet("font-size: 11px; color: #8B949E; margin-top: 4px;")
        main_lay.addWidget(cat_label)

        cats = self._get_key_categories()
        dropdowns_lay = QHBoxLayout()
        dropdowns_lay.setSpacing(6)
        dropdowns_lay.setContentsMargins(0, 0, 0, 0)
        dropdowns_widget = QWidget()
        dropdowns_widget.setLayout(dropdowns_lay)
        dropdowns_widget.setObjectName("_keycombo_dropdowns")

        for cat_name, items in cats:
            combo = QComboBox()
            combo.setToolTip(f"{cat_name}：选择后添加到组合")
            combo.addItem(f"+ {cat_name}", "")  # 占位项
            for display, value in items:
                combo.addItem(display, value)
            combo.setStyleSheet(
                "QComboBox { background-color: #21262D; color: #C9D1D9; "
                "border: 1px solid #2D333B; border-radius: 4px; padding: 2px 6px; "
                "font-size: 10px; min-width: 50px; }"
                "QComboBox::drop-down { border: none; }"
                "QComboBox QAbstractItemView { background-color: #1C2128; color: #C9D1D9; "
                "selection-background-color: #388BFD; }"
            )
            # 选择时添加到组合
            def _on_select(idx, c=combo):
                if idx <= 0:
                    return
                val = c.currentData()
                if val:
                    self._keycombo_add_key(val)
                    c.setCurrentIndex(0)  # 重置为占位项
            combo.currentIndexChanged.connect(_on_select)
            dropdowns_lay.addWidget(combo)
        dropdowns_lay.addStretch(1)
        main_lay.addWidget(dropdowns_widget)

        # --- 清空按钮 ---
        btn_row = QHBoxLayout()
        btn_clear = QPushButton("清空全部")
        btn_clear.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_clear.clicked.connect(lambda: self._keycombo_clear())
        btn_row.addWidget(btn_clear)
        btn_row.addStretch(1)
        main_lay.addLayout(btn_row)

        # 添加到 form 下方
        self._form.addRow(container)

        # 渲染当前已选按键的 chips
        self._keycombo_render_chips(current_keys)

    def _remove_keycombo_picker(self):
        """移除按键组合选择器"""
        # 在 form 中查找 _keycombo_picker
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is not None and fld_w.objectName() == "_keycombo_picker":
                self._form.removeRow(row_i)
                return

    def _keycombo_get_current_keys(self):
        """从 node.params 读取当前 key_combo 列表"""
        import json
        node = self._current_node
        if node is None:
            return []
        raw = node.params.get("key_combo", "[]")
        try:
            keys = json.loads(raw) if isinstance(raw, str) and raw.strip() else []
        except Exception:
            keys = []
        return [str(k).strip().lower() for k in keys if str(k).strip()]

    def _keycombo_save(self, keys):
        """保存 key_combo 到 node.params"""
        import json
        self.apply_params_merge({"key_combo": json.dumps(keys, ensure_ascii=False)})

    def _keycombo_add_key(self, key):
        """添加一个键到组合"""
        key = str(key).strip().lower()
        keys = self._keycombo_get_current_keys()
        if key not in keys:
            keys.append(key)
        self._keycombo_save(keys)
        self._keycombo_render_chips(keys)

    def _keycombo_remove_key(self, key):
        """从组合中移除一个键"""
        key = str(key).strip().lower()
        keys = self._keycombo_get_current_keys()
        keys = [k for k in keys if k != key]
        self._keycombo_save(keys)
        self._keycombo_render_chips(keys)

    def _keycombo_clear(self):
        """清空组合"""
        self._keycombo_save([])
        self._keycombo_render_chips([])

    def _keycombo_render_chips(self, keys):
        """渲染当前组合的按键标签"""
        # 找到 chips 容器
        picker = None
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is not None and fld_w.objectName() == "_keycombo_picker":
                picker = fld_w
                break
        if picker is None:
            return
        chips_widget = picker.findChild(QWidget, "_keycombo_chips")
        if chips_widget is None:
            return
        # 清空旧 chips
        lay = chips_widget.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not keys:
            empty = QLabel("（空）— 请从下方选择按键")
            empty.setStyleSheet("color: #6E7681; font-size: 10px; font-style: italic;")
            lay.addWidget(empty)
            lay.addStretch(1)
            return
        # 显示按键标签
        for k in keys:
            chip = QPushButton(f" {k} ×")
            chip.setStyleSheet(
                "QPushButton { background-color: #238636; color: #FFFFFF; "
                "border: none; border-radius: 10px; padding: 2px 10px; "
                "font-size: 10px; font-weight: bold; }"
                "QPushButton:hover { background-color: #2EA043; }"
            )
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"点击移除 {k}")
            key_val = k
            chip.clicked.connect(lambda _=False, kv=key_val: self._keycombo_remove_key(kv))
            lay.addWidget(chip)
        lay.addStretch(1)

    def _apply_wait_mode_visibility(self):
        """Wait：fixed 模式显示 seconds；random 模式显示 min_seconds/max_seconds"""
        if self._current_node is None or self._current_node.type != "Wait":
            return
        delay_type = str(self._current_node.params.get("delay_type", "fixed") or "fixed")
        w_dt = self._widgets.get("delay_type")
        if isinstance(w_dt, QComboBox):
            d = w_dt.currentData()
            if d:
                delay_type = str(d)
        is_fixed = delay_type == "fixed"
        fixed_params = ["seconds"]
        random_params = ["min_seconds", "max_seconds"]
        for row_i in range(self._form.rowCount()):
            lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is None:
                continue
            for pn in fixed_params:
                if fld_w is self._widgets.get(pn):
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(is_fixed)
                    fld_w.setVisible(is_fixed)
                    break
            for pn in random_params:
                if fld_w is self._widgets.get(pn):
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(not is_fixed)
                    fld_w.setVisible(not is_fixed)
                    break

    def _replace_seq_num_paths_with_buttons(self):
        """Sequence：num_paths 输入框替换为 +/- 按钮组合（必须用 +/- 控制，不能用原生 SpinBox 直接填）。"""
        if self._current_node is None or self._current_node.type != "Sequence":
            return
        w_np = self._widgets.get("num_paths")
        if w_np is None:
            return
        # 找到 num_paths 在 form 中的行
        target_row = -1
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w_np:
                target_row = row_i
                break
        if target_row < 0:
            return
        # 创建 +/- 按钮组合
        container = QWidget()
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(4)
        val_label = QLabel(str(self._current_node.params.get("num_paths", 3)))
        val_label.setStyleSheet("color:#58A6FF; font-weight:bold; font-size:11px; min-width:30px;")
        val_label.setAlignment(Qt.AlignCenter)
        btn_minus = QPushButton("－")
        btn_minus.setFixedSize(28, 24)
        btn_minus.setStyleSheet(self._btn_style("#57606A", "#6E7681"))
        btn_plus = QPushButton("＋")
        btn_plus.setFixedSize(28, 24)
        btn_plus.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        hlay.addWidget(btn_minus)
        hlay.addWidget(val_label)
        hlay.addWidget(btn_plus)
        hlay.addStretch(1)
        # 替换 form 中的 widget
        self._form.removeRow(target_row)
        new_label = QLabel("路径数")
        new_label.setStyleSheet("color: #C9D1D9; font-size: 9px;")
        new_label.setToolTip("顺序分支的路径数量（1~50），点击 +/- 调整")
        self._form.insertRow(target_row, new_label, container)
        self._widgets["num_paths"] = container
        container._val_label = val_label  # type: ignore[attr-defined]

        def _update_val(delta: int):
            if self._current_node is None:
                return
            cur = int(self._current_node.params.get("num_paths", 3) or 3)
            new_v = max(1, min(50, cur + delta))
            if new_v == cur:
                return
            val_label.setText(str(new_v))
            self.param_changed.emit(self._current_node.id, "num_paths", new_v)

        btn_minus.clicked.connect(lambda _=False: _update_val(-1))
        btn_plus.clicked.connect(lambda _=False: _update_val(1))

    def _replace_num_data_inputs_with_buttons(self):
        """VarSwitch / SetVariable：num_data_inputs 输入框替换为 +/- 按钮组合（复用 Sequence 逻辑）。"""
        if self._current_node is None or self._current_node.type not in ("VarSwitch", "SetVariable"):
            return
        w_ndi = self._widgets.get("num_data_inputs")
        if w_ndi is None:
            return
        # 找到 num_data_inputs 在 form 中的行
        target_row = -1
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w_ndi:
                target_row = row_i
                break
        if target_row < 0:
            return
        # 创建 +/- 按钮组合
        container = QWidget()
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(4)
        default_val = 1 if self._current_node.type == "VarSwitch" else 0
        _raw = self._current_node.params.get("num_data_inputs", default_val)
        cur_val = int(_raw) if _raw is not None else default_val
        val_label = QLabel(str(cur_val))
        val_label.setStyleSheet("color:#F778BA; font-weight:bold; font-size:11px; min-width:30px;")
        val_label.setAlignment(Qt.AlignCenter)
        btn_minus = QPushButton("－")
        btn_minus.setFixedSize(28, 24)
        btn_minus.setStyleSheet(self._btn_style("#57606A", "#6E7681"))
        btn_plus = QPushButton("＋")
        btn_plus.setFixedSize(28, 24)
        btn_plus.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        hlay.addWidget(btn_minus)
        hlay.addWidget(val_label)
        hlay.addWidget(btn_plus)
        hlay.addStretch(1)
        # 替换 form 中的 widget
        self._form.removeRow(target_row)
        new_label = QLabel("数据输入端口数")
        new_label.setStyleSheet("color: #C9D1D9; font-size: 9px;")
        new_label.setToolTip("数据输入端口数量（0~20），点击 +/- 增减端口，每个端口对应一个输入值\n临时变量名（所有节点统一写法，就这一种）：\n- 完整数组：__INPUTS__（list类型，可写 {__INPUTS__[0]}）\n- 按索引单值：__INPUTS__0 / __INPUTS__1 / __INPUTS__2 ...")
        self._form.insertRow(target_row, new_label, container)
        self._widgets["num_data_inputs"] = container
        container._val_label = val_label  # type: ignore[attr-defined]

        min_val = 0
        max_val = 20

        def _update_val(delta: int):
            if self._current_node is None:
                return
            _raw = self._current_node.params.get("num_data_inputs", default_val)
            cur = int(_raw) if _raw is not None else default_val
            new_v = max(min_val, min(max_val, cur + delta))
            if new_v == cur:
                return
            val_label.setText(str(new_v))
            self.param_changed.emit(self._current_node.id, "num_data_inputs", new_v)

        btn_minus.clicked.connect(lambda _=False: _update_val(-1))
        btn_plus.clicked.connect(lambda _=False: _update_val(1))

    def _apply_clipboard_mode_visibility(self):
        """Clipboard：根据 mode 隐藏不相关参数
        - read: 显示 output_var_name，隐藏 value
        - write: 显示 value，隐藏 output_var_name
        - clear: 隐藏 value 和 output_var_name
        """
        if self._current_node is None or self._current_node.type != "Clipboard":
            return
        w_mode = self._widgets.get("mode")
        mode_val = "read"
        if isinstance(w_mode, QComboBox):
            d = w_mode.currentData()
            mode_val = str(d) if d else "read"
        else:
            mode_val = str(self._current_node.params.get("mode", "read") or "read")
        show_value = (mode_val == "write")
        show_output_var = (mode_val == "read")
        for param_name, visible in [("value", show_value), ("output_var_name", show_output_var)]:
            w = self._widgets.get(param_name)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(visible)
                    if fld_w:
                        fld_w.setVisible(visible)
                    break

    def _append_setvar_save_button(self):
        """SetVariable：追加「检查格式」按钮，检查 batch_text 每行 name=value 是否合规"""
        if self._current_node is None or self._current_node.type != "SetVariable":
            return
        container = QWidget()
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(6)
        btn_check = QPushButton("✓ 检查格式")
        btn_check.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_check.setToolTip("检查每行变量定义格式是否合规：name = value")
        status_lbl = QLabel("")
        status_lbl.setStyleSheet("color: #8B949E; font-size: 9px;")
        status_lbl.setWordWrap(True)
        hlay.addWidget(btn_check)
        hlay.addWidget(status_lbl)
        hlay.addStretch(1)
        self._form.addRow(container)

        def _do_check():
            if self._current_node is None:
                return
            w = self._widgets.get("batch_text")
            if isinstance(w, QPlainTextEdit):
                batch = w.toPlainText()
            else:
                batch = str(self._current_node.params.get("batch_text", "") or "")
            lines = [ln.strip() for ln in batch.split("\n") if ln.strip()]
            if not lines:
                status_lbl.setText("⚠ 未设置任何变量")
                status_lbl.setStyleSheet("color: #F0883E; font-size: 9px;")
                return
            import re
            valid = 0
            errors = []
            for i, ln in enumerate(lines, 1):
                if "=" not in ln:
                    errors.append(f"第{i}行: 缺少 = 号")
                    continue
                name = ln.split("=", 1)[0].strip()
                value = ln.split("=", 1)[1].strip()
                if not name:
                    errors.append(f"第{i}行: 变量名为空")
                    continue
                if not re.match(r"^[a-zA-Z_]\w*$", name):
                    errors.append(f"第{i}行: 变量名 '{name}' 不合规（须字母/下划线开头）")
                    continue
                if not value:
                    errors.append(f"第{i}行: 变量 '{name}' 的值为空")
                    continue
                valid += 1
            if not errors:
                status_lbl.setText(f"✓ 全部合规（{valid} 个变量）")
                status_lbl.setStyleSheet("color: #3FB950; font-size: 9px;")
            else:
                err_preview = "; ".join(errors[:3])
                if len(errors) > 3:
                    err_preview += f" …等{len(errors)}处"
                status_lbl.setText(f"✗ {len(errors)}处问题: {err_preview}")
                status_lbl.setStyleSheet("color: #F85149; font-size: 9px;")

        btn_check.clicked.connect(lambda _=False: _do_check())

    def _append_varswitch_check_button(self):
        """VarSwitch：追加「检查格式」按钮，检查每行 变量名 运算符 值 是否合规（AND 多条件判断）"""
        if self._current_node is None or self._current_node.type != "VarSwitch":
            return
        from ..core.node_library import parse_varswitch_conditions
        container = QWidget()
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(6)
        btn_check = QPushButton("✓ 检查格式")
        btn_check.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_check.setToolTip("检查每行条件表达式格式是否合规：变量名 运算符 值（全部AND逻辑，同时满足才走yes）")
        status_lbl = QLabel("")
        status_lbl.setStyleSheet("color: #8B949E; font-size: 9px;")
        status_lbl.setWordWrap(True)
        hlay.addWidget(btn_check)
        hlay.addWidget(status_lbl)
        hlay.addStretch(1)
        self._form.addRow(container)

        def _do_check():
            if self._current_node is None:
                return
            w = self._widgets.get("cases")
            if isinstance(w, QPlainTextEdit):
                text = w.toPlainText()
            else:
                text = str(self._current_node.params.get("cases", "") or "")
            conditions = parse_varswitch_conditions(text if isinstance(text, str) else "")
            errors = [c for c in conditions if c.get("error")]
            valid = [c for c in conditions if not c.get("error")]
            if not conditions:
                status_lbl.setText("⚠ 未设置任何条件")
                status_lbl.setStyleSheet("color: #F0883E; font-size: 9px;")
                return
            if not errors:
                labels = [c["label"] for c in valid]
                preview = "、".join(labels[:4])
                if len(labels) > 4:
                    preview += f"…共{len(labels)}个"
                status_lbl.setText(f"✓ 全部合规（{preview}）")
                status_lbl.setStyleSheet("color: #3FB950; font-size: 9px;")
            else:
                err_msgs = [f"第{c['line_no']}行: {c['error']}" for c in errors]
                preview = "; ".join(err_msgs[:3])
                if len(err_msgs) > 3:
                    preview += f" …等{len(err_msgs)}处"
                status_lbl.setText(f"✗ {len(err_msgs)}处问题: {preview}")
                status_lbl.setStyleSheet("color: #F85149; font-size: 9px;")

        btn_check.clicked.connect(lambda _=False: _do_check())

    def _bind_dynamic_ports_refresh(self, node: Node, label: QLabel, rows_added: List[str]):
        """监听动态参数控件（num_paths/num_data_inputs/templates/cases/var_name），实时刷新端口列表 Label"""
        from ..core.node_library import _resolve_input_ports_default, _resolve_output_ports_default

        def _refresh(*_):
            # 从 UI 控件上取"正在编辑还没保存到params"的值作为候选（更实时）
            p_live: Dict[str, Any] = dict(node.params or {})
            for pname in ("num_paths", "num_data_inputs", "templates", "cases", "var_name"):
                w = self._widgets.get(pname)
                if w is None: continue
                # 兼容 +/- 按钮替换后的容器控件
                val_label = getattr(w, "_val_label", None)
                if val_label is not None and isinstance(val_label, QLabel):
                    try:
                        p_live[pname] = int(val_label.text())
                        continue
                    except Exception:
                        pass
                if isinstance(w, QSpinBox):
                    p_live[pname] = w.value()
                elif isinstance(w, QLineEdit):
                    p_live[pname] = w.text()
                elif isinstance(w, QPlainTextEdit):
                    p_live[pname] = w.toPlainText()
            try:
                in_ports = _resolve_input_ports_default(node.type, p_live)
                out_ports = _resolve_output_ports_default(node.type, p_live)
                in_names = [p.name for p in in_ports if p.type.value == "data"]
                out_names = [p.name for p in out_ports]
                parts = []
                if in_names:
                    parts.append("📥 数据输入：" + "  ←  ".join(in_names))
                if out_names:
                    parts.append("📤 输出：" + "  →  ".join(out_names))
                txt = "  |  ".join(parts) if parts else "（无动态端口）"
                label.setText("🔌 " + txt)
            except Exception:
                pass

        # 绑定关键控件
        triggers = ["num_paths", "num_data_inputs", "templates", "cases", "var_name"]
        for pname in triggers:
            if pname not in rows_added:
                continue
            w = self._widgets.get(pname)
            if w is None: continue
            # 兼容 +/- 按钮容器：从容器的子按钮里取点击信号
            val_label = getattr(w, "_val_label", None)
            if val_label is not None:
                # 对 +/- 按钮容器，直接监听内部按钮的点击事件（不做连接，实际信号由 param_changed 触发 inspector 刷新）
                continue
            if isinstance(w, QSpinBox):
                try: w.valueChanged.connect(_refresh)
                except Exception: pass
            elif isinstance(w, QLineEdit):
                try: w.textChanged.connect(_refresh)
                except Exception: pass
            elif isinstance(w, QPlainTextEdit):
                try: w.textChanged.connect(_refresh)
                except Exception: pass

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
            if pd.placeholder:
                le.setPlaceholderText(pd.placeholder)
            elif pd.name == "text":
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
                    label = op
                    if pd.option_labels and op in pd.option_labels:
                        label = pd.option_labels[op]
                    cb.addItem(str(label), op)
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
                # 限制宽度，确保右侧 📍 按钮不被遮挡（Inspector 最大宽 460px）
                s.setMaximumWidth(95 if size == 2 else 80)
                spins.append(s)
                row.addWidget(s, 0)  # stretch=0：靠左排列，不拉伸

            def _on_change(*_, nid=nid, pn=pd.name, sps=spins, sz=size):
                vals = [s.value() for s in sps]
                t = tuple(vals)
                self.param_changed.emit(nid, pn, t)
            for s in spins:
                s.valueChanged.connect(_on_change)

            # 点属性（tuple2）附加 📍 坐标拾取按钮
            if editor == EDITOR_TUPLE2 and getattr(pd, "pick_coord", False):
                btn_pick = QPushButton("📍")
                btn_pick.setFixedWidth(32)
                btn_pick.setToolTip("点击拾取屏幕坐标（根据坐标参考系自动保存相对/绝对坐标）")
                btn_pick.setStyleSheet(
                    "QPushButton { background: #238636; color: white; border: none;"
                    " border-radius: 4px; font-size: 14px; }"
                    "QPushButton:hover { background: #2EA043; }"
                )
                btn_pick.clicked.connect(
                    lambda _=False, nid=nid, pn=pd.name: self.request_pick_coord.emit(nid, pn)
                )
                row.addWidget(btn_pick)
            row.addStretch(1)  # 剩余空间推到右侧，让控件靠左对齐
            return host

        if editor == EDITOR_MULTILINE:
            te = QPlainTextEdit(str(current or ""))
            te.setMinimumHeight(100)
            if pd.placeholder:
                te.setPlaceholderText(pd.placeholder)
            elif pd.name == "batch_text":
                te.setPlaceholderText("# 每行 name=value\n# 示例：\nport = 8080\nmsg  = hello world\ncount= {= 1+2 }")
            elif pd.name == "message":
                te.setPlaceholderText("通知内容，支持 {var} ${var} {= expr } 插值\n{ALL_VARS} 会在最后一步展开所有变量")
            elif pd.name == "cases":
                te.setPlaceholderText("AND 多条件判断：每行 变量名 运算符 值\n# 示例（全部同时满足才走 yes）:\nhp >= 30\nhp <= 100\nscore >= 60\ngender != \"female\"\nlevel > {min_level}")
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
