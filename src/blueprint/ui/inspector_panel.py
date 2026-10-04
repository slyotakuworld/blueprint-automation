import base64
import io
import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
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
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QLayout,
)
from PySide6.QtCore import QRect, QPoint, QRectF

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


class _FlowLayout(QLayout):
    """自动换行的流式布局，用于按键 chips 显示。"""
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        for item in self._items:
            wid = item.widget()
            if wid is None:
                continue
            hint = wid.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class _ClickableImageLabel(QLabel):
    """可点击的图片标签，用于从模板预览图上拾取偏移量。
    点击时发出 clicked(normalized_x, normalized_y) 信号，值域 [0,1]。"""
    clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pick_mode = False
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_pick_mode(self, on: bool):
        self._pick_mode = on
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        if on:
            self.setStyleSheet(
                "QLabel { background:#000; border:2px solid #F78166; border-radius:4px; padding:2px; }"
            )
        else:
            self.setStyleSheet(
                "QLabel { background:#000; border:2px solid #0969DA; border-radius:4px; padding:2px; }"
            )

    def mousePressEvent(self, event):
        if not self._pick_mode:
            return super().mousePressEvent(event)
        if self.pixmap() is None:
            return
        pix = self.pixmap()
        pw, ph = pix.width(), pix.height()
        if pw <= 0 or ph <= 0:
            return
        # pixmap 居中显示，需要计算 pixmap 在控件中的实际偏移
        cw, ch = self.width(), self.height()
        # 减去 padding/border（样式里写了 padding:2px, border:2px）
        pad = 4  # 2px padding + 2px border
        cw -= pad * 2
        ch -= pad * 2
        offset_x = (cw - pw) / 2.0 + pad
        offset_y = (ch - ph) / 2.0 + pad
        # 点击坐标减去 pixmap 偏移，再除以 pixmap 尺寸
        click_x = event.position().x() - offset_x
        click_y = event.position().y() - offset_y
        x = max(0.0, min(1.0, click_x / pw))
        y = max(0.0, min(1.0, click_y / ph))
        self.clicked.emit(x, y)


class _NoWheelFilter(QObject):
    """事件过滤器：吞掉滚轮事件，防止在属性面板滚动时误改 QSpinBox/QComboBox 值。"""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            return True
        return False


# 全局单例，所有 QSpinBox/QComboBox 共用
_NO_WHEEL_FILTER = _NoWheelFilter()


def _disable_wheel(widget):
    """给控件装上滚轮过滤器"""
    widget.installEventFilter(_NO_WHEEL_FILTER)
    return widget


# ============================================================
# 「选中节点 → 面板重建」的节奏参数（配合 set_node_deferred 使用）
# ============================================================
# 面板重建要 50~250ms（销毁+重建全部控件），是一次**阻塞主线程**的活儿。
# 若每次点选都立刻重建，连续切换节点时重建就会插在两次点击之间 → 点一次顿一下。
# 对策：连续点选时把重建延后到「用户停手」之后，期间显示转圈加载页。
_SEL_RAPID_GAP_MS = 450     # 距上次点选 < 该值 → 认为用户在「连续切换节点」
_SEL_DEBOUNCE_MAX_MS = 700  # 去抖上限，避免节奏很慢时面板迟迟不刷新


# ============================================================
# 「永不建编辑器的参数」：值由专属 UI（预览图卡片 / 子画布）管理，且**总是被隐藏**
# ============================================================
# 这些字段一律是机器托管的 blob（图片 base64 / 图片列表 JSON / 子画布 JSON），
# 用户从不手敲，节点专属 UI 已经把它们可视化了。
#
# ⚠ 为什么必须「不建」而不是「建完再 hide」：这些值动辄几百 KB ~ 几 MB，
#    而 `QLineEdit(str(huge))` 的构造要按文本长度算 sizeHint，
#    实测耗时与长度**成正比、约 1ms/KB**（1 万字 7.7ms / 10 万字 79ms / 60 万字 568ms）。
#    历史上这里是「先 addRow 再 _hide_param_row」，hide 发生在建完之后，
#    于是这笔钱白付了——「找图/检测节点带模板图、参考图时属性面板极慢」就是这么来的。
#    （IfImageDetect 带 6 张 640×480 参考图：set_node 实测 1964ms，其中 ~1.9s 是白付的。）
#
# 这些名字都**只可能**出现在「一定会隐藏它们」的节点类型上（已逐个核对）：
#   template_b64/features_json → FindImage；images_json → IfImageDetect；
#   roi_preview_b64 → StillnessDetect/Screenshot/OcrRecognize；*_body → Loop/Sequence/Parallel。
# ⚠ 反例：PressKey 的 key_combo 只在「按键模式」隐藏、切到「文本模式」要显示，故**不能**进这个集合。
_NO_EDITOR_PARAMS = frozenset({
    "template_b64", "template", "features_json",
    "images_json", "roi_preview_b64",
    "feature_capture_width", "feature_template_w", "feature_template_h",
    "loop_body", "seq_body", "par_body",
})


# ============================================================
# b64 → 缩略图 QPixmap 的**内容级 LRU 缓存**
# ============================================================
# 图片解码（尤其 PNG）是纯 CPU 活儿：实测 640×480 一张要 ~7ms，
# 而面板每次「切节点 / 重建 / 重跑专属 UI」都会把**同一批**参考图、模板图、ROI 再解一遍。
# Qt 自带解码并不比 PIL 快（实测 6.85ms vs 6.80ms/张），所以换库没用——**缓存才是解法**。
# key 用「内容本身 + 目标尺寸」，内容变自然换 key（无需手动失效）。
_PIX_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_PIX_CACHE_MAX = 64


def _b64_thumb(b64: str, size: int) -> Optional[QPixmap]:
    """b64 图片 → 等比缩放到 size 以内的 QPixmap（带 LRU 缓存）。失败返回 None。

    ⚠ 返回的是**缓存里的共享对象**（QPixmap 隐式共享，赋值给 QLabel 安全），
      调用方不得就地修改它。
    """
    if not b64 or not isinstance(b64, str):
        return None
    key = (size, b64)
    hit = _PIX_CACHE.get(key)
    if hit is not None:
        _PIX_CACHE.move_to_end(key)
        return hit
    try:
        raw = base64.b64decode(b64)
        qimg = QImage.fromData(raw)
        if qimg.isNull():
            return None
        pm = QPixmap.fromImage(qimg).scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
    except Exception:
        return None
    _PIX_CACHE[key] = pm
    while len(_PIX_CACHE) > _PIX_CACHE_MAX:
        _PIX_CACHE.popitem(last=False)
    return pm


class _Spinner(QWidget):
    """转圈加载指示器（纯 QPainter 绘制，不依赖任何图片资源）。"""

    def __init__(self, diameter: int = 30, color: str = "#58A6FF", parent=None):
        super().__init__(parent)
        self._d = int(diameter)
        self._color = QColor(color)
        self._track = QColor("#30363D")
        self._angle = 0
        self.setFixedSize(self._d, self._d)
        self._timer = QTimer(self)
        # 40ms ≈ 25fps：视觉上够顺滑，开销可以忽略（只在加载页可见时跑）
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            d = self._d
            rect = QRectF(2.0, 2.0, d - 4.0, d - 4.0)
            track = QPen(self._track)
            track.setWidth(3)
            track.setCapStyle(Qt.RoundCap)
            p.setPen(track)
            p.drawArc(rect, 0, 360 * 16)
            arc = QPen(self._color)
            arc.setWidth(3)
            arc.setCapStyle(Qt.RoundCap)
            p.setPen(arc)
            p.drawArc(rect, -self._angle * 16, 100 * 16)
        finally:
            p.end()


class InspectorPanel(QWidget):
    """右侧属性面板：选中节点后动态生成参数表单。"""

    param_changed = Signal(str, str, object)  # node_id, param_name, value
    request_pick_template = Signal(str, str)  # node_id, param_name
    request_snip_for_node = Signal(str, str)  # node_id, mode（"if_img_add" / "if_static_roi" / "if_img_replace_<idx>"）
    request_pick_coord = Signal(str, str)  # node_id, param_name（Mouse 点属性 📍 坐标拾取）
    request_open_component = Signal(str)   # 组件蓝图相对路径（点「打开这个组件」→ 主窗口跳转过去编辑）
    component_chosen = Signal(str, str)    # (node_id, 组件蓝图相对路径)：选完组件 → 主窗口做端口自动对齐 + 循环引用检测

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(460)
        self.setMaximumWidth(460)
        self._current_node: Optional[Node] = None
        self._widgets: Dict[str, QWidget] = {}
        self._ifimg_host: Optional[QWidget] = None
        self._ifstatic_host: Optional[QWidget] = None
        # 「选中节点 → 刷新面板」的延迟入口（见 set_node_deferred 的说明）
        self._pending_node: Optional[Node] = None
        self._last_select_at = 0.0    # 上次「点选节点」的时间戳，用于识别连续切换
        # 同类型节点复用控件（见 _try_reuse_form）：
        self._built_type: Optional[str] = None   # 当前表单是按哪个节点类型建的（复用判据）
        self._built_rows: List[str] = []         # 参数行名清单（复用路径重跑专属 UI 时要用）
        self._aux_conns: Dict[str, Any] = {}     # 专属信号的幂等连接表（见 _connect_aux）
        self._aux_widgets: List[QWidget] = []    # 专属 UI 新增的控件（复用前按它整段删行）
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        # 间隔由 set_node_deferred 动态决定：单击 → 0ms（立刻重建，面板不拖拍）；
        # 连续点选 → 跟随点击节奏去抖（见该方法的说明）。
        self._apply_timer.setInterval(0)
        self._apply_timer.timeout.connect(self._apply_pending_node)
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

        # ---- 节点动作条（根据节点类型动态填充，自动换行） ----
        self._action_host = QWidget()
        self._action_layout = QGridLayout(self._action_host)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(6)
        self._action_col = 0
        self._action_row = 0
        self._action_max_cols = 4
        self._action_btns: List[QPushButton] = []
        self._action_btn_w = 0  # 上次算出的「一行4个」等宽，用于新按钮即时生效
        # 左对齐（不要「均匀铺满整行」）：第 0..3 列只占按钮本身的固定宽度，
        # 余量全部塞给末尾的空气列。否则 QGridLayout 会把余量平摊给「有按钮的列」，
        # 一行只有 1~3 个按钮时就会出现被撑开 / 居中的排版（1 个按钮时直接居中）。
        for _c in range(self._action_max_cols):
            self._action_layout.setColumnStretch(_c, 0)
        self._action_layout.setColumnStretch(self._action_max_cols, 1)
        self._action_host.hide()

        # ---- 正文区（动作条 + 参数表单），与「加载页」一起放进堆叠容器 ----
        self._body = QWidget()
        body_lay = QVBoxLayout(self._body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(8)
        body_lay.addWidget(self._action_host)

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
        body_lay.addWidget(scroll, 1)

        # ---- 加载页：面板正在重建参数表单时显示（转圈 + 文案） ----
        # 用堆叠页而不是「半透明浮层」，是为了让旧节点的参数彻底消失，
        # 免得上一个节点的值留在屏幕上被误读；背景显式设成与表单一致的黑灰，
        # 否则独立渲染/父级未指定底色时会露出默认浅色窗口底，切换时闪一下白。
        self._loading_page = QWidget()
        self._loading_page.setStyleSheet("background-color: #1C2128;")
        lp = QVBoxLayout(self._loading_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(10)
        lp.addStretch(1)
        self._spinner = _Spinner(30, "#58A6FF", self._loading_page)
        spin_row = QHBoxLayout()
        spin_row.addStretch(1)
        spin_row.addWidget(self._spinner)
        spin_row.addStretch(1)
        lp.addLayout(spin_row)
        self._loading_label = QLabel("正在加载参数…")
        self._loading_label.setAlignment(Qt.AlignCenter)
        self._loading_label.setStyleSheet("color: #8B949E; font-size: 10px;")
        lp.addWidget(self._loading_label)
        lp.addStretch(1)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._body)          # index 0
        self._stack.addWidget(self._loading_page)  # index 1
        outer.addWidget(self._stack, 1)

    # ---------------- 加载页辅助 ----------------
    def _show_loading(self, text: str = "正在加载参数…"):
        """切换到加载页并启动转圈。重复调用无副作用。"""
        self._loading_label.setText(text)
        if self._stack.currentWidget() is self._loading_page:
            self._spinner.start()
            return
        self._stack.setCurrentWidget(self._loading_page)
        self._spinner.start()

    def _hide_loading(self):
        """收起加载页（停在当前页时也应调用：确保转圈定时器停掉、不空转）。"""
        self._spinner.stop()
        if self._stack.currentWidget() is not self._body:
            self._stack.setCurrentWidget(self._body)

    # ================================================================
    # 同类型节点复用控件：辅助方法
    # ================================================================
    def _nid(self) -> str:
        """当前正在编辑的节点 id。

        ⚠ 构建期**不要**把 node.id 捕获进闭包（别写 `nid=nid`）：同类型节点会复用控件，
        捕获下来的 id 会停在上一个节点上 —— 改一个 SpinBox 却写进了别的节点。
        统一在回调触发时用本方法取。
        """
        n = self._current_node
        return n.id if n is not None else ""

    def _connect_aux(self, key: str, signal, slot):
        """连接「节点专属 UI」的辅助信号，同 key 重复连接时先断开上一次。

        为什么需要：专属 UI 每次刷新都会重新走一遍（`_apply_node_specific_ui`），
        而它要连的却是**被复用**的参数控件。直接 connect 会越连越多 ——
        例如 range_mode 变化回调被连 3 次，切一次模式就重算三遍、
        +/- 按钮一次点击加 3 个端口。
        """
        old = self._aux_conns.pop(key, None)
        if old is not None:
            old_sig, old_slot = old
            try:
                old_sig.disconnect(old_slot)
            except (RuntimeError, TypeError):
                pass  # 控件已销毁：连接随对象一起没了
        try:
            signal.connect(slot)
        except Exception:
            return
        self._aux_conns[key] = (signal, slot)

    @staticmethod
    def _set_silently(widget, fn):
        """在屏蔽信号的前提下改控件值。

        复用路径是「把新节点的值写进已有控件」，这不是用户编辑，
        绝不能触发 param_changed 回写（否则会把 A 节点的值写进 B 节点）。
        """
        if widget is None:
            return
        old = widget.blockSignals(True)
        try:
            fn()
        finally:
            widget.blockSignals(old)

    def _push_one_value(self, w: QWidget, value: Any) -> None:
        """把单个参数值写进已有控件（按控件类型分派；复合容器按子控件分派）。"""
        try:
            if isinstance(w, QDoubleSpinBox):
                self._set_silently(w, lambda: w.setValue(float(value if value is not None else 0.0)))
            elif isinstance(w, QSpinBox):
                self._set_silently(w, lambda: w.setValue(int(value if value is not None else 0)))
            elif isinstance(w, QComboBox):
                def _set_combo():
                    idx = w.findData(value)
                    if idx < 0 and isinstance(value, str):
                        idx = w.findText(value)
                    w.setCurrentIndex(idx if idx >= 0 else 0)
                self._set_silently(w, _set_combo)
            elif isinstance(w, QCheckBox):
                self._set_silently(w, lambda: w.setChecked(bool(value)))
            elif isinstance(w, QLineEdit):
                self._set_silently(w, lambda: w.setText(str(value if value is not None else "")))
            elif isinstance(w, QPlainTextEdit):
                self._set_silently(w, lambda: w.setPlainText(str(value if value is not None else "")))
            else:
                # 复合容器（文件选择行 / 模板行 / 坐标行 / +/- 按钮行）
                val_lbl = getattr(w, "_val_label", None)   # num_paths / num_data_inputs 的 ± 行
                if val_lbl is not None:
                    self._set_silently(val_lbl, lambda: val_lbl.setText(str(value)))
                    return
                # ⚠ 顺序不能反：QSpinBox 内部**自带**一个 QLineEdit，
                #   `findChildren(QLineEdit)` 会把它查出来 —— 先查 QLineEdit 的话，
                #   坐标行（X/Y/W/H）会被当成文本框，整个元组被塞进第一个自旋框里。
                spins = w.findChildren(QSpinBox)
                if spins:
                    vals = list(value) if isinstance(value, (list, tuple)) else []
                    for i, s in enumerate(spins):
                        v = vals[i] if i < len(vals) else 0
                        try:
                            iv = int(v)
                        except Exception:
                            iv = 0
                        self._set_silently(s, lambda s=s, iv=iv: s.setValue(iv))
                    return
                les = w.findChildren(QLineEdit)
                if les:
                    self._set_silently(les[0], lambda: les[0].setText(str(value if value is not None else "")))
        except Exception:
            pass  # 单个控件写值失败不该拖垮整个面板刷新

    def _push_param_values(self, node: Node) -> None:
        """把 node 的参数值推进**已有**控件（同类型复用路径专用，不重建任何控件）。"""
        meta = NODE_LIBRARY.get(node.type)
        params = meta.params if meta else []
        cn = self._widgets.get("custom_name")
        if isinstance(cn, QLineEdit):
            v = str(node.params.get("custom_name", "") or "")
            self._set_silently(cn, lambda: cn.setText(v))
        for pd in params:
            w = self._widgets.get(pd.name)
            if w is None:
                continue
            self._push_one_value(w, node.params.get(pd.name, pd.default))

    def _form_widgets(self) -> List[QWidget]:
        """当前表单里出现在布局上的全部控件（Label / Field / Spanning 三种角色都算）。"""
        roles = (QFormLayout.LabelRole, QFormLayout.FieldRole, QFormLayout.SpanningRole)
        out: List[QWidget] = []
        seen = set()
        for i in range(self._form.rowCount()):
            for role in roles:
                it = self._form.itemAt(i, role)
                w = it.widget() if it is not None else None
                if w is not None and id(w) not in seen:
                    seen.add(id(w))
                    out.append(w)
        return out

    def _run_node_specific_ui(self, node: Node) -> None:
        """跑一遍节点专属 UI，并记下它这次新增了哪些控件（供 `_clear_aux_rows` 精确清理）。

        为什么用「新增控件」而不是「行里没有参数控件」来判定专属行：
        无参数节点（Continue/Break/Branch）有一行「（该节点无可编辑参数）」占位，
        它也不含参数控件，会被误判成专属行删掉。而 `_replace_*` 换进参数行的新控件
        （± 容器、组件下拉、新标签）会被 `_widgets` 认领，不算专属 —— 它们所在的行
        因为「不是整行都属于专属区」而被保留。
        """
        before = self._form_widgets()          # 持强引用：防止对象被回收后 id 复用
        before_ids = {id(w) for w in before}
        self._apply_node_specific_ui(node)
        param_ids = {id(v) for v in self._widgets.values() if v is not None}
        self._aux_widgets = [w for w in self._form_widgets()
                             if id(w) not in before_ids and id(w) not in param_ids]

    def _clear_aux_rows(self) -> None:
        """删掉上次记录的「节点专属 UI」那些行 —— 复用路径里它们要整段重建。

        判据：整行的控件都落在专属集合里（参数行总会含一个参数控件，不会被误删）。
        ⚠ 不能按「行号 ≥ N 就删」：Mouse 的分组提示是 `insertRow(0, …)` 插到表单最
        顶部的（参数行上面），按行号切会把最后几个参数行当成专属行删掉。
        """
        if self._aux_widgets:
            aux_ids = {id(w) for w in self._aux_widgets}
            roles = (QFormLayout.LabelRole, QFormLayout.FieldRole, QFormLayout.SpanningRole)
            for row_i in range(self._form.rowCount() - 1, -1, -1):
                ids = set()
                for role in roles:
                    it = self._form.itemAt(row_i, role)
                    if it is not None and it.widget() is not None:
                        ids.add(id(it.widget()))
                if ids and ids <= aux_ids:
                    self._form.removeRow(row_i)
        self._aux_widgets = []
        self._ifimg_host = None
        self._ifstatic_host = None

    def _set_header(self, node: Optional[Node]) -> None:
        if node is None:
            self._header.setText("未选择节点 · 点击画布上的节点以编辑参数")
            return
        meta = NODE_LIBRARY.get(node.type)
        title = meta.title if meta else node.type
        # 名称 / 类型 / ID 压在一行，省一行垂直空间（面板宽度固定，够放下）
        self._header.setText(
            f"<b style='color:#58A6FF'>{title}</b>"
            f" <span style='color:#8B949E'>[{node.type}]</span>"
            f" <span style='color:#6E7681; font-size:9px'>ID: {node.id}</span>"
        )
        self._header.setTextFormat(Qt.RichText)

    def _try_reuse_form(self, node: Optional[Node]) -> bool:
        """同类型节点切过来时，复用已有控件、只刷新值 —— 命中返回 True。

        为什么值得单开一条路径：`set_node` 的全量重建里，最贵的不是「创建控件」
        （`_build_editor` 实测仅 1.2ms），而是 QFormLayout 的 `removeRow`/`addRow`
        与**每控件单独 setStyleSheet**（子控件带样式表插入时会触发父级样式缓存重算）
        —— 25 行表单加起来 30ms 上下。同类型切换时这些控件的**结构完全一样**，
        唯一变的是值、预览图和专属面板内容，所以：
          · 参数行 → 原地写值（blockSignals，不触发回写）
          · 专属 UI（预览图/按键选择器/端口提示…）→ 整段删掉重建（很快，也就几个控件）
          · 动作条 → 照常重建（按钮集依赖 range_mode 等参数，可能不同）
        """
        if node is None or self._built_type is None or node.type != self._built_type:
            return False
        if not self._widgets:
            return False
        self._current_node = node            # 必须先设：后面的写入/专属 UI 都读它
        self._populate_action_bar(node)
        self._set_header(node)
        self._push_param_values(node)
        self._clear_aux_rows()
        self._run_node_specific_ui(node)
        return True

    # ---------------- 动作条辅助 ----------------
    def _clear_action_bar(self):
        """清空动作条（仅移除控件，不删布局里的 stretch 配置）。

        ⚠ 必须 hide() + setParent(None) 再 deleteLater()：
        deleteLater 是「延迟删除」，若只 takeAt 就完事，旧按钮仍是 host 的子控件，
        会按旧几何位置再被画一帧（和新按钮叠在一起）—— 这正是「按钮增减时闪烁」的来源。
        """
        while self._action_layout.count() > 0:
            it = self._action_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._action_btns = []
        self._action_col = 0
        self._action_row = 0

    def _add_action_btn(self, btn):
        """添加按钮到动作条，自动换行（每行最多4个）"""
        self._action_btns.append(btn)
        # 已有算好的等宽就立刻套上（动态重建动作条时不会先闪一下自然宽度）
        if self._action_btn_w > 0:
            btn.setFixedWidth(self._action_btn_w)
        self._action_layout.addWidget(btn, self._action_row, self._action_col)
        self._action_col += 1
        if self._action_col >= self._action_max_cols:
            self._action_col = 0
            self._action_row += 1

    def _compute_action_btn_width(self) -> int:
        """一行满 4 个时的按钮宽度（也是每个按钮的固定宽度）。"""
        host_w = self._action_host.width()
        if host_w <= 0:
            host_w = self.width() - 16  # 减去外层 8+8 边距
        if host_w <= 0:
            return 0
        sp = self._action_layout.spacing()
        return max(48, (host_w - sp * (self._action_max_cols - 1)) // self._action_max_cols)

    def _relayout_action_widths(self):
        """把所有动作按钮固定成「一行满 4 个」时的等宽，避免因每行数量不同而忽宽忽窄。"""
        w = self._compute_action_btn_width()
        if w <= 0:
            return
        self._action_btn_w = w
        for b in self._action_btns:
            if b.maximumWidth() != w or b.minimumWidth() != w:
                b.setFixedWidth(w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_action_widths()

    def showEvent(self, event):
        super().showEvent(event)
        # 面板首次显示时宽度才最终确定，补一次排布
        QTimer.singleShot(0, self._relayout_action_widths)

    def _populate_action_bar(self, node: Node):
        """动作条唯一入口：填充按钮 + 等宽/左对齐排布。

        ⚠ 所有动态刷新（range_mode / use_feature_assist 切换 …）都必须走这里，
        不能直接调 _populate_action_bar_impl —— 否则重建出来的按钮没有固定宽度，
        又会出现「每行数量不同 → 宽度忽宽忽窄」的老毛病。

        防闪烁：整个重建在「关闭重绘」的窗口里一次性完成（Qt 会把 updatesEnabled
        递归传给子控件，新按钮也在其中），期间旧按钮被同步 setParent(None) 摘除、
        新按钮宽度也已就位，最后只重绘一次 → 无明显中间态闪烁。
        """
        if self._action_btn_w <= 0:
            self._action_btn_w = self._compute_action_btn_width()
        updates_was_on = self.updatesEnabled()
        if updates_was_on:
            self.setUpdatesEnabled(False)
        try:
            self._populate_action_bar_impl(node)
            self._relayout_action_widths()  # 同步套宽，不等下一帧
        finally:
            if updates_was_on:
                self.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._relayout_action_widths)

    def _populate_action_bar_impl(self, node: Node):
        self._clear_action_bar()
        if node is None:
            self._action_host.hide()
            return
        if node.type == "Notify":
            btn = QPushButton("🔍 预览通知内容")
            btn.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn.clicked.connect(lambda _=False: self._show_notify_preview())
            self._add_action_btn(btn)

            self._action_host.show()
            return
        if node.type == "IfImageDetect":
            btn_add = QPushButton("📷 截图新增一张")
            btn_add.setStyleSheet(self._btn_style("#ef6c00", "#ff9800"))
            btn_add.setToolTip(
                "框选一张参考图，追加到下方列表\n"
                "若已设置「目标窗口」，框选范围将自动限定在该窗口内"
            )
            btn_add.clicked.connect(lambda _=False: self.request_snip_for_node.emit(self._nid(), "if_img_add"))
            self._add_action_btn(btn_add)
            btn_import = QPushButton("📂 导入图片")
            btn_import.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_import.setToolTip("从本地文件选择一张或多张图片导入")
            btn_import.clicked.connect(lambda _=False: self._ifimg_import_files())
            self._add_action_btn(btn_import)
            btn_test = QPushButton("🔍 测试匹配")
            btn_test.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn_test.setToolTip("用当前屏幕画面和全部参考图做一次匹配，查看每张图的分数")
            btn_test.clicked.connect(lambda _=False: self._ifimg_test_screen_match())
            self._add_action_btn(btn_test)
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            if str(node.params.get("range_mode", "window") or "window") == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_ifimg_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_ifimg_temp())
                self._add_action_btn(btn_list)

            self._action_host.show()
            return
        if node.type == "FindImage":
            btn_snip = QPushButton("📸 截取目标")
            btn_snip.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
            btn_snip.setToolTip(
                "框选一个目标区域，自动保存为PNG到templates文件夹并回填模板路径+预览图\n"
                "若已设置「目标窗口」，框选范围将自动限定在该窗口内，避免截到窗口外的画面"
            )
            btn_snip.clicked.connect(lambda _=False: self.request_snip_for_node.emit(self._nid(), "findimg_target"))
            self._add_action_btn(btn_snip)
            btn_import = QPushButton("📂 导入图片")
            btn_import.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_import.setToolTip("从本地文件选择一张图片作为模板，默认打开工程目录templates文件夹")
            btn_import.clicked.connect(lambda _=False: self._findimg_import_image())
            self._add_action_btn(btn_import)
            btn_test = QPushButton("🔍 测试匹配")
            btn_test.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn_test.setToolTip(
                "用当前屏幕画面与模板做一次匹配，查看结果/分数（不运行蓝图）\n"
                "与运行时同一条代码路径：匹配算法级联 + 特征识别辅助（验证/兜底）都会跑"
            )
            btn_test.clicked.connect(lambda _=False: self._findimg_test_match())
            self._add_action_btn(btn_test)
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            if str(node.params.get("range_mode", "window") or "window") == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_findimg_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_findimg_temp())
                self._add_action_btn(btn_list)
            # 特征识别辅助：非 off 时显示截取特征按钮
            use_feat = str(node.params.get("use_feature_assist", "off") or "off") != "off"
            if use_feat:
                btn_feat = QPushButton("✂ 从模板截取特征")
                btn_feat.setStyleSheet(self._btn_style("#BF8700", "#FFD700"))
                btn_feat.setToolTip("在模板图片上框选区域作为特征，自动裁剪并记录相对坐标\n（需要先截取/导入模板图片）")
                btn_feat.clicked.connect(lambda _=False: self._findimg_snip_features())
                self._add_action_btn(btn_feat)
                btn_feat_list = QPushButton("📋 查看特征列表")
                btn_feat_list.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
                btn_feat_list.setToolTip("查看/删除已截取的特征")
                btn_feat_list.clicked.connect(lambda _=False: self._findimg_show_feature_list())
                self._add_action_btn(btn_feat_list)

            self._action_host.show()
            return
        if node.type == "StillnessDetect":
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            if str(node.params.get("range_mode", "window") or "window") == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_ifstatic_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_ifstatic_temp())
                self._add_action_btn(btn_list)

                self._action_host.show()
                return
            self._action_host.hide()
            return
        if node.type == "Mouse":
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            if str(node.params.get("range_mode", "window") or "window") == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名\n（坐标将基于该窗口左上角）")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_mouse_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_mouse_temp())
                self._add_action_btn(btn_list)

                self._action_host.show()
                return
            self._action_host.hide()
            return
        if node.type == "Screenshot":
            btn_roi = QPushButton("🖱 框选ROI")
            btn_roi.setStyleSheet(self._btn_style("#0e3a5c", "#1f8dd6"))
            btn_roi.setToolTip("框选一个矩形区域作为截图 ROI\n若截取范围=目标窗口，框选范围将自动限定在该窗口内")
            btn_roi.clicked.connect(lambda _=False: self.request_snip_for_node.emit(self._nid(), "screenshot_roi"))
            self._add_action_btn(btn_roi)
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            if str(node.params.get("range_mode", "window") or "window") == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_screenshot_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_screenshot_temp())
                self._add_action_btn(btn_list)
            self._action_host.show()
            return
        if node.type == "OcrRecognize":
            btn_roi = QPushButton("🖱 框选ROI")
            btn_roi.setStyleSheet(self._btn_style("#0b3d61", "#29b6f6"))
            btn_roi.setToolTip("框选一个矩形区域作为识别 ROI\n若已设置「目标窗口」，框选范围将自动限定在该窗口内")
            btn_roi.clicked.connect(lambda _=False: self.request_snip_for_node.emit(self._nid(), "ocr_roi"))
            self._add_action_btn(btn_roi)
            btn_test = QPushButton("🔍 测试识别")
            btn_test.setStyleSheet(self._btn_style("#0B3A5C", "#0F4A77"))
            btn_test.setToolTip("按当前识别范围+ROI现场截一张图跑一次 OCR，显示识别结果（不运行蓝图）")
            btn_test.clicked.connect(lambda _=False: self._ocr_test_recognize())
            self._add_action_btn(btn_test)
            # 仅 range_mode=window 时显示目标窗口拾取按钮
            _ocr_range = str(node.params.get("range_mode", "window") or "window")
            if _ocr_range == "window":
                btn_pick = QPushButton("🎯 拾取目标窗口")
                btn_pick.setStyleSheet(self._btn_style("#8957E5", "#BC8CFF"))
                btn_pick.setToolTip("点击后用鼠标点击窗口，自动回填目标窗口标题/程序名/类名")
                btn_pick.clicked.connect(lambda _=False: self._pick_window_for_ocr_temp())
                self._add_action_btn(btn_pick)
                btn_list = QPushButton("📋 从列表选择")
                btn_list.setStyleSheet(self._btn_style("#238636", "#2EA043"))
                btn_list.setToolTip("列出当前所有可见窗口供选择作为目标窗口")
                btn_list.clicked.connect(lambda _=False: self._list_windows_for_ocr_temp())
                self._add_action_btn(btn_list)
            self._action_host.show()
            return
        self._action_host.hide()

    # ---- Mouse 目标窗口拾取 ----
    def _pick_window_for_mouse_temp(self):
        """智能拾取窗口 → 回填 Mouse 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "target_title": str(res.get("title", "") or ""),
                "target_exe": str(res.get("exe_name", "") or ""),
                "target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "目标窗口拾取成功",
                    f"已设置目标窗口：\n标题：{title}\n程序：{exe}\n\n此鼠标节点将以此为坐标基准。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _pick_target_window_from_list(self, dialog_title: str = "选择目标窗口",
                                      tip_text: str = ""):
        """统一的「从列表选择目标窗口」对话框（所有节点共用，别再复制粘贴第 6 份）。

        和「🎯 拾取目标窗口」的关键区别：**这里连最小化的窗口也列出来**。
        拾取要靠鼠标点，最小化的窗口点不到，所以那边排除；
        而「从列表选择」的常见用法恰好是「目标窗口现在最小化着，先把定位条件填好再让它跑」，
        所以必须列出来 —— 最小化的排在最下面，最后一列标注状态。
        """
        try:
            from .window_picker_overlay import enumerate_valid_windows
            wins = enumerate_valid_windows(include_minimized=True)
        except Exception as e:
            QMessageBox.warning(self, "窗口枚举失败", f"无法枚举窗口：\n{e}")
            return
        if not wins:
            QMessageBox.information(self, "提示", "未找到任何窗口（含最小化的）。")
            return
        # 正常显示的排前面、最小化的沉到最后；stable sort → 组内保持原有 z 序
        wins = sorted(wins, key=lambda w: bool(w.get("minimized")))
        n_min = sum(1 for w in wins if w.get("minimized"))

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidgetItem,
            QTableWidget,
        )
        from PySide6.QtGui import QColor as _QColor

        dlg = QDialog(self)
        dlg.setWindowTitle(dialog_title)
        dlg.resize(760, 480)
        lay = QVBoxLayout(dlg)
        tip = QLabel(
            (tip_text or "双击一行选择窗口作为目标窗口：")
            + (f"\n最小化的窗口也会列出（共 {n_min} 个，排在最后，状态列标「最小化」）。"
               if n_min else "")
        )
        tip.setStyleSheet("color: #8B949E; font-size: 10px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        table = QTableWidget(len(wins), 5)
        table.setHorizontalHeaderLabels(["窗口标题", "程序名", "类名", "PID", "状态"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 150)
        table.setColumnWidth(2, 170)
        table.setColumnWidth(3, 60)
        table.setColumnWidth(4, 64)
        table.setStyleSheet(
            "QTableWidget { background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 4px; gridline-color: #2D333B; }"
            "QHeaderView::section { background-color: #22272E; color: #C9D1D9;"
            "  padding: 4px; border: none; border-bottom: 1px solid #2D333B; }"
            "QTableWidget::item:selected { background-color: #58A6FF; color: #FFFFFF; }"
        )
        for i, w in enumerate(wins):
            minimized = bool(w.get("minimized"))
            t_item = QTableWidgetItem(str(w.get("title", "")))
            if minimized:
                t_item.setForeground(_QColor("#8B949E"))  # 最小化的标题压暗，一眼区分
            table.setItem(i, 0, t_item)
            table.setItem(i, 1, QTableWidgetItem(str(w.get("exe_name", ""))))
            table.setItem(i, 2, QTableWidgetItem(str(w.get("class_name", ""))))
            table.setItem(i, 3, QTableWidgetItem(str(w.get("pid", ""))))
            st_item = QTableWidgetItem("最小化" if minimized else "正常")
            if minimized:
                st_item.setForeground(_QColor("#D29922"))
            table.setItem(i, 4, st_item)
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
            "target_title": str(w.get("title", "") or ""),
            "target_exe": str(w.get("exe_name", "") or ""),
            "target_class": str(w.get("class_name", "") or ""),
        })

    def _list_windows_for_mouse_temp(self):
        """从列表选择窗口 → 回填 Mouse 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        self._pick_target_window_from_list(
            "选择目标窗口", "双击一行选择窗口作为目标窗口（留空=不按该条件匹配）：")

    def _clear_mouse_target(self):
        """清空 Mouse 节点的目标窗口参数。"""
        if self._current_node is None or self._current_node.type != "Mouse":
            return
        merge = {
            "target_title": "",
            "target_exe": "",
            "target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- FindImage 目标窗口拾取 ----
    def _pick_window_for_findimg_temp(self):
        """智能拾取窗口 → 回填 FindImage 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "target_title": str(res.get("title", "") or ""),
                "target_exe": str(res.get("exe_name", "") or ""),
                "target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "目标窗口拾取成功",
                    f"已设置目标窗口：\n标题：{title}\n程序：{exe}\n\n此找图节点将以此为截图范围和坐标基准。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_findimg_temp(self):
        """从列表选择窗口 → 回填 FindImage 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        self._pick_target_window_from_list(
            "选择目标窗口", "双击一行选择窗口作为找图目标窗口（留空=不按该条件匹配）：")
    def _clear_findimg_target(self):
        """清空 FindImage 节点的目标窗口参数。"""
        if self._current_node is None or self._current_node.type != "FindImage":
            return
        merge = {
            "target_title": "",
            "target_exe": "",
            "target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- FindImage 特征识别辅助 ----
    def _findimg_snip_features(self):
        """打开模板图片对话框，用户在上面框选多个区域作为特征，自动裁剪+记录相对坐标。"""
        node = self._current_node
        if node is None or node.type != "FindImage":
            return

        # 获取模板图片：节点里的 template 是「相对工程根」的路径，必须解析成真实路径再判断
        tpl_path = str(node.params.get("template", "") or "")
        real_path = self._real_img_path(tpl_path)
        tpl_img = None
        tpl_note = ""
        if real_path:
            try:
                from PIL import Image as PILImage
                tpl_img = PILImage.open(real_path).convert("RGBA")
            except Exception as e:
                QMessageBox.warning(self, "模板图片加载失败", f"无法打开模板图片：\n{e}")
                return
        else:
            # 路径解析不到（文件被删/工程未打开）时，退回节点里的「模板预览图」b64，
            # 否则用户明明在节点上看着有模板，这里却报「没有模板」。
            b64 = str(node.params.get("template_b64", "") or "")
            if b64:
                try:
                    tpl_img = self._b64_to_pil(b64)
                except Exception:
                    tpl_img = None
                if tpl_img is not None:
                    try:
                        tpl_img = tpl_img.convert("RGBA")
                    except Exception:
                        pass
                    tpl_note = ("\n⚠ 模板文件已找不到，本次改用节点里的「模板预览图」框选（缩略图分辨率偏低，"
                                "特征质量会下降）；建议先重新「📸 截取目标」再截取特征。")
        if tpl_img is None:
            QMessageBox.warning(self, "无法截取特征", "请先截取或导入一张模板图片。")
            return

        tw, th = tpl_img.size

        # 打开特征截取对话框
        from PySide6.QtWidgets import (
            QDialog,
            QVBoxLayout,
            QHBoxLayout,
            QLabel,
            QPushButton,
            QScrollArea,
        )
        from PySide6.QtCore import Qt, QRect, QPointF, QTimer
        from PySide6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QBrush

        dlg = QDialog(self)
        dlg.setWindowTitle("从模板截取特征")
        dlg.resize(900, 650)
        lay = QVBoxLayout(dlg)

        tip = QLabel("在模板图片上拖动鼠标框选特征区域。可框选多个。选完后点「✅ 确认保存」。\n"
                      "特征会记录在模板中的相对位置，运行时用于交叉验证。\n"
                      "模板太大/太小看不清时：用下面的「➕ 放大 / ➖ 缩小 / 🖥 适应窗口」，"
                      "或按住 Ctrl 滚滚轮缩放（不按 Ctrl 时滚轮用于滚动查看）。\n"
                      "🔍 想看画面某个角落：在图片上按住鼠标右键拖动即可平移（放大后特别好用）。" + tpl_note)
        tip.setStyleSheet("color: #8B949E; font-size: 10px; margin-bottom: 4px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 画布区域
        class FeatureCanvas(QLabel):
            """可框选的模板画布。

            支持缩放查看：显示尺寸 = 原图尺寸 × `_scale`，
            框选得到的矩形**始终换算回原图像素坐标**后再存进 `_rects`，
            所以缩放不影响最终特征数据的正确性。

            鼠标：
              - 左键拖动 = 框选特征区域
              - 右键按住拖动 = 平移画面（放大后看局部用，靠在 QScrollArea 上拨滚动条实现）
            """
            MIN_SCALE = 0.05
            MAX_SCALE = 8.0
            MAX_FIT_SCALE = 4.0   # 适应窗口时最多放大 4 倍，避免小图被糊成马赛克

            def __init__(self, pixmap, parent=None):
                super().__init__(parent)
                self._orig_pixmap = pixmap
                self._rects = []  # [(x, y, w, h)] 原图像素坐标
                self._drawing = False
                self._start = QPointF()
                self._current = QPointF()
                self._panning = False          # 右键平移中
                self._pan_origin = None        # 平移起点（全局坐标）
                self._pan_h0 = 0               # 平移开始时的水平滚动值
                self._pan_v0 = 0               # 平移开始时的垂直滚动值
                self._scale = 1.0   # 显示像素 / 原图像素
                self._sx = 1.0      # 原图像素 / 显示像素（画框时用）
                self._sy = 1.0
                self._scaled_pixmap = pixmap
                self.zoom_changed = None  # 外部注入：回调(scale)
                self._apply_scale(1.0)

            # ---------- 缩放 ----------
            def _apply_scale(self, scale):
                try:
                    scale = float(scale)
                except (TypeError, ValueError):
                    scale = 1.0
                scale = max(self.MIN_SCALE, min(self.MAX_SCALE, scale))
                self._scale = scale
                w = max(1, int(round(self._orig_pixmap.width() * scale)))
                h = max(1, int(round(self._orig_pixmap.height() * scale)))
                self._scaled_pixmap = self._orig_pixmap.scaled(
                    w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
                )
                if self._scaled_pixmap.isNull():
                    self._scaled_pixmap = self._orig_pixmap
                self.setFixedSize(self._scaled_pixmap.size())
                self._sx = self._orig_pixmap.width() / max(1, self._scaled_pixmap.width())
                self._sy = self._orig_pixmap.height() / max(1, self._scaled_pixmap.height())
                self._redraw()
                if callable(self.zoom_changed):
                    try:
                        self.zoom_changed(self._scale)
                    except Exception:
                        pass

            def set_scale(self, scale):
                self._apply_scale(scale)
                return self._scale

            def zoom_by(self, factor):
                return self.set_scale(self._scale * factor)

            def zoom_in(self):
                return self.zoom_by(1.25)

            def zoom_out(self):
                return self.zoom_by(1 / 1.25)

            def fit_scale(self, view_w, view_h, margin=20):
                """算出「整张模板完整放进视口」的缩放倍数。"""
                ow = max(1, self._orig_pixmap.width())
                oh = max(1, self._orig_pixmap.height())
                if view_w <= margin or view_h <= margin:
                    return self._scale
                return min((view_w - margin) / ow, (view_h - margin) / oh, self.MAX_FIT_SCALE)

            def reset_1to1(self):
                return self.set_scale(1.0)

            # ---------- 鼠标 ----------
            def wheelEvent(self, ev):
                # Ctrl + 滚轮 = 缩放；不按 Ctrl 就交给外层滚动条滚动
                if ev.modifiers() & Qt.ControlModifier:
                    dy = ev.angleDelta().y()
                    if dy:
                        self.zoom_by(1.15 if dy > 0 else (1 / 1.15))
                    ev.accept()
                    return
                ev.ignore()

            def _scroll_area(self):
                """找到外层 QScrollArea（右键平移靠它拨滚动条）。找不到就返回 None。"""
                w = self.parentWidget()
                while w is not None and not isinstance(w, QScrollArea):
                    w = w.parentWidget()
                return w

            def mousePressEvent(self, ev):
                if ev.button() == Qt.LeftButton:
                    self._drawing = True
                    self._start = ev.position()
                    self._current = ev.position()
                elif ev.button() == Qt.RightButton:
                    # 右键按住 = 平移画面（方便放大后查看图片局部再截图）
                    sa = self._scroll_area()
                    if sa is None:
                        return
                    self._panning = True
                    self._pan_origin = ev.globalPosition().toPoint()
                    self._pan_h0 = sa.horizontalScrollBar().value()
                    self._pan_v0 = sa.verticalScrollBar().value()
                    self.setCursor(Qt.ClosedHandCursor)

            def mouseMoveEvent(self, ev):
                if self._drawing:
                    self._current = ev.position()
                    self._redraw()
                elif self._panning:
                    sa = self._scroll_area()
                    if sa is None:
                        return
                    cur = ev.globalPosition().toPoint()
                    # 鼠标往右拖 → 画面往右走 → 滚动条值减小（反向位移）
                    sa.horizontalScrollBar().setValue(
                        self._pan_h0 - (cur.x() - self._pan_origin.x()))
                    sa.verticalScrollBar().setValue(
                        self._pan_v0 - (cur.y() - self._pan_origin.y()))

            def mouseReleaseEvent(self, ev):
                if ev.button() == Qt.RightButton:
                    if self._panning:
                        self._panning = False
                        self._pan_origin = None
                        self.unsetCursor()
                    return
                if ev.button() == Qt.LeftButton and self._drawing:
                    self._drawing = False
                    self._current = ev.position()
                    # 显示坐标 -> 原图坐标（缩放后 _sx/_sy 会跟着变）
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
                # 当前正在画的矩形（黄色虚线，已是显示坐标）
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

        # 将 PIL -> QPixmap（统一 RGBA：QImage 按 4 字节对齐读行，
        # RGB 的 3 字节/像素在「宽×3 非 4 倍数」时会让行错位 → 图像斜切、色彩错乱）
        if tpl_img.mode != "RGBA":
            tpl_img = tpl_img.convert("RGBA")
        qimg = QImage(tpl_img.tobytes("raw", "RGBA"), tw, th, QImage.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimg.copy())

        scroll = QScrollArea()
        canvas = FeatureCanvas(pixmap)
        scroll.setWidget(canvas)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)
        lay.addWidget(scroll, 1)

        # ---- 缩放工具条 ----
        zoom_row = QHBoxLayout()
        zoom_label = QLabel("100%")
        zoom_label.setStyleSheet("color: #C9D1D9; font-size: 11px; min-width: 130px;")

        def _on_zoom_changed(s):
            zoom_label.setText(f"缩放 {int(round(s * 100))}%   模板 {tw}×{th}")

        canvas.zoom_changed = _on_zoom_changed

        def _fit_canvas():
            vs = scroll.viewport().size()
            canvas.set_scale(canvas.fit_scale(vs.width(), vs.height()))

        btn_zoom_out = QPushButton("➖ 缩小")
        btn_zoom_out.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_zoom_out.setToolTip("缩小显示（快捷键：Ctrl + 鼠标滚轮向下）")
        btn_zoom_out.clicked.connect(lambda: canvas.zoom_out())
        zoom_row.addWidget(btn_zoom_out)

        btn_zoom_in = QPushButton("➕ 放大")
        btn_zoom_in.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_zoom_in.setToolTip("放大显示，便于框选小特征（快捷键：Ctrl + 鼠标滚轮向上）")
        btn_zoom_in.clicked.connect(lambda: canvas.zoom_in())
        zoom_row.addWidget(btn_zoom_in)

        btn_fit = QPushButton("🖥 适应窗口")
        btn_fit.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
        btn_fit.setToolTip("把整张模板缩放到刚好完整可见，方便总览全部区域")
        btn_fit.clicked.connect(_fit_canvas)
        zoom_row.addWidget(btn_fit)

        btn_1to1 = QPushButton("1:1 原始像素")
        btn_1to1.setStyleSheet(self._btn_style("#0969DA", "#58A6FF"))
        btn_1to1.setToolTip("按模板原始像素显示，最清晰")
        btn_1to1.clicked.connect(lambda: canvas.reset_1to1())
        zoom_row.addWidget(btn_1to1)

        zoom_row.addStretch(1)
        zoom_row.addWidget(zoom_label)
        lay.addLayout(zoom_row)
        _on_zoom_changed(canvas._scale)

        # 打开后先「适应窗口」，保证一进来就能看到整张模板
        QTimer.singleShot(0, _fit_canvas)

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
        """查看/删除已截取的特征列表（图片画廊式展示）。"""
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
            QScrollArea, QGridLayout, QFrame,
        )
        from PySide6.QtGui import QImage

        dlg = QDialog(self)
        dlg.setWindowTitle(f"特征列表（共 {len(features)} 个）")
        dlg.resize(600, 450)
        lay = QVBoxLayout(dlg)

        # 顶部说明
        info = QLabel(f"共 {len(features)} 个特征（偏移信息已自动记录，无需手动管理）")
        info.setStyleSheet("color: #8B949E; font-size: 10px;")
        lay.addWidget(info)

        # 滚动区域 + 网格图片画廊
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #2D333B; border-radius: 4px; background: #1C2128; }"
        )
        gallery_host = QWidget()
        gallery_host.setStyleSheet("background: transparent;")
        grid = QGridLayout(gallery_host)
        grid.setSpacing(8)
        grid.setContentsMargins(8, 8, 8, 8)

        deleted = set()

        for i, feat in enumerate(features):
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #21262D; border: 1px solid #2D333B; border-radius: 6px; }"
            )
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(4, 4, 4, 4)
            card_lay.setSpacing(4)

            # 图片
            b64 = feat.get("b64", "")
            if b64:
                try:
                    img_data = base64.b64decode(b64)
                    qimg = QImage.fromData(img_data)
                    if not qimg.isNull():
                        pm = QPixmap.fromImage(qimg).scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        lbl = QLabel()
                        lbl.setPixmap(pm)
                        lbl.setAlignment(Qt.AlignCenter)
                        lbl.setMinimumSize(120, 120)
                        lbl.setStyleSheet("background: #000; border-radius: 4px;")
                        card_lay.addWidget(lbl)
                    else:
                        card_lay.addWidget(QLabel("(解码失败)"))
                except Exception:
                    card_lay.addWidget(QLabel("(加载失败)"))
            else:
                empty = QLabel("（无图）")
                empty.setAlignment(Qt.AlignCenter)
                empty.setMinimumSize(120, 120)
                empty.setStyleSheet("background: #1C2128; border-radius: 4px; color: #8B949E;")
                card_lay.addWidget(empty)

            # 序号 + 删除按钮
            bottom = QHBoxLayout()
            bottom.setContentsMargins(0, 0, 0, 0)
            num_lbl = QLabel(f"#{i + 1}")
            num_lbl.setStyleSheet("color: #58A6FF; font-size: 10px; font-weight: bold;")
            bottom.addWidget(num_lbl)
            bottom.addStretch(1)
            btn_del = QPushButton("🗑")
            btn_del.setFixedSize(28, 24)
            btn_del.setStyleSheet(self._btn_style("#B62324", "#FF7B72"))
            feat_idx = i
            def _del(checked=False, idx=feat_idx, c=card):
                c.hide()
                deleted.add(idx)
            btn_del.clicked.connect(_del)
            bottom.addWidget(btn_del)
            card_lay.addLayout(bottom)

            row, col = i // 4, i % 4
            grid.addWidget(card, row, col)

        scroll.setWidget(gallery_host)
        lay.addWidget(scroll, 1)

        # 底部按钮
        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        btn_save = QPushButton("保存")
        btn_save.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_box.addWidget(btn_save)
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_box.addWidget(btn_cancel)
        lay.addLayout(btn_box)

        def _save():
            remaining = [feat for i, feat in enumerate(features) if i not in deleted]
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
            from src.blueprint.core.variable_engine import VariableEngine
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

    # ------------------------------------------------------------
    # 选中节点 → 面板刷新：延迟入口
    # ------------------------------------------------------------
    def set_node_deferred(self, node: Optional[Node]):
        """「选中节点」专用的刷新入口：把面板重建推迟出鼠标事件那一帧。

        为什么需要它：set_node() 会销毁并重建面板里的全部控件（实测 50~250ms，
        随节点类型 / 模板图大小浮动）。如果直接在 selectionChanged 的信号链里同步跑，
        用户看到的就是「点下去之后画面顿一下才亮」——节点选中高亮、连线联动高亮
        全被压在面板重建后面，那就是「选中节点有卡顿感」的来源。

        推迟一轮事件循环后：选中高亮当帧就画出来，面板随后刷新。

        两档节奏：
        * **同类型节点**（面板已按该类型建过表）→ 走复用控件路径（~10ms），直接 0ms 刷新，
          不去抖也不亮菊花，面板始终跟手；
        * **跨类型**（必须全量重建，~50ms+）→ 见下面的去抖说明。

        **连续切换（图快时挨个点节点）另做去抖**：重建是阻塞主线程的，若每点一次都
        重建，重建就会插在两次点击之间——点一次顿一下，连点尤其明显。所以当「距上次点选
        不足 _SEL_RAPID_GAP_MS」时，把重建的等待时间设为「刚才那次点选间隔 + 100ms」：
        只要还按同样的节奏点，下一次点击总会赶在定时器之前到达并把它重启，重建就不会
        插进连点中间；等用户停手，才重建最后一次。等待期间显示转圈加载页。
        单击（间隔够久）仍走 0ms，面板不拖拍。

        注：其他调用方（新建节点后自动选中、参数变更后刷新等）仍用同步的 set_node()，
        那些场景用户就在等面板更新，不该再拖一拍。
        """
        now = time.monotonic()
        if node is self._current_node:
            # 又点回当前节点：排队请求作废，面板本来就是对的，不用重建
            self._pending_node = None
            self._apply_timer.stop()
            self._hide_loading()
            self._last_select_at = now
            return
        # 同类型节点走「复用控件」路径，刷新只要 ~10ms —— 没必要去抖、也不用亮菊花，
        # 直接下一轮事件循环刷新，面板始终跟手（连点也一直是同步的）。
        if node is not None and node.type == self._built_type and self._widgets:
            self._pending_node = node
            self._apply_timer.start(0)
            self._last_select_at = now
            return
        gap_ms = (now - self._last_select_at) * 1000.0
        self._last_select_at = now
        self._pending_node = node
        if gap_ms < _SEL_RAPID_GAP_MS:
            # 连续切换：跟随用户点击节奏去抖 + 亮出加载页
            wait = int(min(_SEL_DEBOUNCE_MAX_MS, max(200.0, gap_ms + 100.0)))
            self._apply_timer.start(wait)
            self._show_loading()
        else:
            self._apply_timer.start(0)  # 单击：下一轮事件循环立刻重建

    def _apply_pending_node(self):
        node, self._pending_node = self._pending_node, None
        # 先切回正文页再重建：重建是阻塞的，若留着加载页，用户会看到「转圈 → 顿住 →
        # 新内容」两段跳；先切页则重绘被推迟到重建之后，新旧直接一步到位。
        self._hide_loading()
        self.set_node(node)

    def set_node(self, node: Optional[Node]):
        # 同步刷新优先：作废排队中的延迟刷新，避免「先删了节点/切了蓝图，
        # 随后被一个更早排队的选中请求又把面板刷回去」。
        self._pending_node = None
        self._apply_timer.stop()
        self._hide_loading()
        # 同类型节点：复用已有控件、只刷新值（约 10ms，对比全量重建约 50ms）
        if self._try_reuse_form(node):
            return
        self._built_type = None
        self._built_rows = []
        self._clear_form()
        self._populate_action_bar(node)  # 内部已含等宽排布
        self._current_node = node
        if node is None:
            self._set_header(None)
            self._form.addRow(QLabel("（无）"))
            return

        meta = NODE_LIBRARY.get(node.type)
        self._set_header(node)
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
            lambda w=cn_edit: self.param_changed.emit(self._nid(), "custom_name", w.text())
        )
        cn_lbl = QLabel("额外名称")
        cn_lbl.setStyleSheet("color: #FFA657; font-size: 9px; font-weight: bold;")
        cn_lbl.setToolTip("画布标题展示：ID · 节点标题 · 额外名称（为空时只显示前两段）")
        self._form.addRow(cn_lbl, cn_edit)
        self._widgets["custom_name"] = cn_edit
        rows_added.append("custom_name")

        for pd in params:
            if pd.name in _NO_EDITOR_PARAMS:
                # 总是隐藏的机器托管字段（图片 base64 / 列表 JSON / 子画布 JSON）：
                # 干脆不建控件——建了再 hide 的话，那笔「按文本长度算 sizeHint」的
                # 构造成本（≈1ms/KB）已经付出去了。详见 _NO_EDITOR_PARAMS 的注释。
                continue
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

        # 参数行到此结束：记下类型与行清单，「同类型复用」靠它判断与重跑专属 UI。
        # 下面 `_apply_node_specific_ui` 追加的行都会被 `_clear_aux_rows()` 识别出来重建。
        self._built_type = node.type
        self._built_rows = rows_added
        self._run_node_specific_ui(node)

    def _apply_node_specific_ui(self, node: Node):
        """追加/刷新「节点专属 UI」：动态端口列表、预览图、按键选择器、显隐规则…

        ⚠ 这个方法会被调用多次（全量重建后一次；之后每次同类型切换一次），所以：
          · 它追加的行会被 `_clear_aux_rows()` 按「不含参数控件」判据整段识别出来
          · 它给**参数控件**连信号必须走 `_connect_aux()`，否则会越连越多
          · 它读节点/端口/预览一律走 `self._current_node`（或入参 node），不吃构建期快照
        """
        meta = NODE_LIBRARY.get(node.type)
        rows_added = self._built_rows
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
                # 绑定 range_mode 下拉框变化，切换目标窗口参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("range_mode")
                if isinstance(w_sm, QComboBox):
                    self._connect_aux("ifimg.range_mode", w_sm.currentIndexChanged,
                                      lambda *_: self._on_ifimg_range_mode_changed())
                self._apply_ifimg_range_visibility()
            except Exception as e:
                print(f"[Inspector] IfImageDetect UI 出错: {e}")

        # 5) StillnessDetect 专属：隐藏 roi_preview_b64 文本框，下方追加 ROI 预览图
        if node.type == "StillnessDetect":
            try:
                self._hide_param_row("roi_preview_b64")
                self._append_ifstatic_preview()
                # 绑定 range_mode 下拉框变化，切换目标窗口参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("range_mode")
                if isinstance(w_sm, QComboBox):
                    self._connect_aux("ifstatic.range_mode", w_sm.currentIndexChanged,
                                      lambda *_: self._on_ifstatic_range_mode_changed())
                self._apply_ifstatic_range_visibility()
            except Exception as e:
                print(f"[Inspector] StillnessDetect UI 出错: {e}")

        # 6) FindImage 专属：隐藏 template_b64 文本框，追加预览图+滚动/动作显隐
        if node.type == "FindImage":
            try:
                self._hide_param_row("template_b64")
                self._hide_param_row("template")
                self._hide_param_row("features_json")
                self._hide_param_row("feature_capture_width")
                self._hide_param_row("feature_template_w")
                self._hide_param_row("feature_template_h")
                self._append_findimage_panel()
                # 这三个专属连接给的是**参数控件**（复用路径下它们还活着），
                # 必须走 _connect_aux 去重；不能整体 disconnect()，那会连带断掉
                # _build_editor 里连的 param_changed.emit，导致切 after_action 写不回 node.params。
                w_act = self._widgets.get("after_action")
                if isinstance(w_act, QComboBox):
                    self._connect_aux("findimg.after_action", w_act.currentIndexChanged,
                                      lambda _i: self._findimg_apply_param_visibility())
                # 绑定 range_mode 下拉框变化，切换目标窗口参数显隐 + 工具栏按钮
                w_sm = self._widgets.get("range_mode")
                if isinstance(w_sm, QComboBox):
                    self._connect_aux("findimg.range_mode", w_sm.currentIndexChanged,
                                      lambda *_: self._on_findimg_range_mode_changed())
                # 绑定 use_feature_assist 下拉框，切换特征参数显隐 + 工具栏按钮
                w_ufa = self._widgets.get("use_feature_assist")
                if isinstance(w_ufa, QComboBox):
                    self._connect_aux("findimg.use_feature_assist", w_ufa.currentIndexChanged,
                                      lambda _i: self._on_findimg_feature_assist_changed())
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
                    self._connect_aux("mouse.action", w_action.currentIndexChanged,
                                      lambda *_: self._apply_mouse_action_visibility(initial=False))
                # 绑定 range_mode 下拉框变化，切换目标窗口参数显隐 + 工具栏按钮
                w_cm = self._widgets.get("range_mode")
                if isinstance(w_cm, QComboBox):
                    self._connect_aux("mouse.range_mode", w_cm.currentIndexChanged,
                                      lambda *_: self._on_mouse_range_mode_changed())
            except Exception as e:
                print(f"[Inspector] Mouse UI 出错: {e}")

        # 8) Loop 专属：infinite=True 时隐藏 max_iterations 行
        if node.type == "Loop":
            try:
                self._apply_loop_infinite_visibility()
                w_inf = self._widgets.get("infinite")
                if isinstance(w_inf, QCheckBox):
                    self._connect_aux("loop.infinite", w_inf.toggled,
                                      lambda _: self._apply_loop_infinite_visibility())
                # 循环体子画布内容（loop_body 只读字段）不进属性面板，双击节点进入编辑
                self._hide_param_row("loop_body")
                self._append_subgraph_hint("双击本节点进入「循环体」子画布编辑每圈要跑的内容")
            except Exception as e:
                print(f"[Inspector] Loop UI 出错: {e}")

        # 9) Sequence 专属：隐藏 seq_body 只读字段，追加「双击进入子画布」提示
        if node.type == "Sequence":
            try:
                self._hide_param_row("seq_body")
                self._append_subgraph_hint("双击本节点进入「顺序体」子画布编辑串行动作")
            except Exception as e:
                print(f"[Inspector] Sequence UI 出错: {e}")

        # 9.5) Parallel 专属：隐藏 par_body 只读字段，追加「双击进入子画布」提示
        #      （并行分支数由子画布里「分支入口」节点数量决定，属性面板不设数量参数）
        if node.type == "Parallel":
            try:
                self._hide_param_row("par_body")
                self._append_subgraph_hint("双击本节点进入「并行体」子画布编辑，放几个「分支入口」节点就有几条并行分支")
            except Exception as e:
                print(f"[Inspector] Parallel UI 出错: {e}")

        # 9.6) Component 专属：blueprint_path 改成下拉列表（扫描 components/ 目录）
        if node.type == "Component":
            try:
                self._replace_component_path_with_combobox()
            except Exception as e:
                print(f"[Inspector] Component UI 出错: {e}")

        # 9.7) Screenshot / OcrRecognize 专属：隐藏 roi_preview_b64 文本框，追加 ROI 预览，
        #      并按「识别/截取范围」切换目标窗口参数显隐
        if node.type == "Screenshot":
            try:
                self._hide_param_row("roi_preview_b64")
                self._append_snip_roi_preview(node.type)
                w_rm = self._widgets.get("range_mode")
                if isinstance(w_rm, QComboBox):
                    self._connect_aux("screenshot.range_mode", w_rm.currentIndexChanged,
                                      lambda *_: self._on_screenshot_range_mode_changed())
                self._apply_screenshot_range_visibility()
            except Exception as e:
                print(f"[Inspector] {node.type} UI 出错: {e}")

        if node.type == "OcrRecognize":
            try:
                self._hide_param_row("roi_preview_b64")
                self._append_snip_roi_preview(node.type)
                w_rm = self._widgets.get("range_mode")
                if isinstance(w_rm, QComboBox):
                    self._connect_aux("ocr.range_mode", w_rm.currentIndexChanged,
                                      lambda *_: self._on_ocr_range_mode_changed())
                self._apply_ocr_range_visibility()
            except Exception as e:
                print(f"[Inspector] {node.type} UI 出错: {e}")

        # 10) Clipboard 专属：根据 mode 隐藏不相关参数
        if node.type == "Clipboard":
            try:
                self._apply_clipboard_mode_visibility()
                w_mode = self._widgets.get("mode")
                if isinstance(w_mode, QComboBox):
                    self._connect_aux("clipboard.mode", w_mode.currentIndexChanged,
                                      lambda *_: self._apply_clipboard_mode_visibility())
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
                    self._connect_aux("presskey.kb_mode", w_kb.currentIndexChanged,
                                      lambda *_: self._apply_presskey_mode_visibility())
            except Exception as e:
                print(f"[Inspector] PressKey UI 出错: {e}")

        # 13) Wait 专属：根据 delay_type 隐藏不相关参数
        if node.type == "Wait":
            try:
                self._apply_wait_mode_visibility()
                w_dt = self._widgets.get("delay_type")
                if isinstance(w_dt, QComboBox):
                    self._connect_aux("wait.delay_type", w_dt.currentIndexChanged,
                                      lambda *_: self._apply_wait_mode_visibility())
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
        """图片选择对话框的默认目录：当前蓝图对应的资源目录 <工程根>/resource/<蓝图相对目录>。

        资源按蓝图所在目录镜像存放，所以「导入图片」默认就落在同目录蓝图对应的 res 子目录里，
        方便一个目录里的蓝图连同它的资源一起搬移。无工程时回退用户主目录。
        """
        try:
            from src.blueprint.core import asset_store

            mw = self.window()
            pm = getattr(mw, "_pm", None)
            if pm and pm.is_open() and pm.project_root:
                cur = getattr(mw, "_current_path", None)
                d = asset_store.assets_dir_for(pm.project_root, cur)
                if os.path.isdir(d):
                    return d
        except Exception:
            pass
        return os.path.expanduser("~")

    def _project_asset_rel(self, src_path: str, prefix: str) -> str:
        """把外部图片拷进「当前蓝图对应的 res 目录」，返回相对工程根的路径（失败返回空串）。"""
        try:
            from src.blueprint.core import asset_store

            mw = self.window()
            pm = getattr(mw, "_pm", None)
            if not (pm and pm.is_open() and pm.project_root):
                return ""
            return asset_store.copy_into_store(
                pm.project_root, getattr(mw, "_current_path", None), src_path, prefix=prefix
            )
        except Exception:
            return ""

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
                # 先拷进该蓝图对应的 res/ 目录：资源跟着蓝图走，搬移/清理都清爽
                rel = self._project_asset_rel(p, "ifimg_import")
                if not rel:
                    try:
                        from PySide6.QtWidgets import QMessageBox
                        QMessageBox.warning(
                            self, "导入失败",
                            f"无法把图片拷贝到工程的 res/ 资源目录：\n{p}\n\n"
                            "请先新建或打开一个 BlueprintAutomation 工程后再导入。"
                        )
                    except Exception:
                        pass
                    return
                pil = Image.open(p) if Image is not None else None
                w, h = pil.size if pil is not None else (0, 0)
                item: Dict[str, Any] = {
                    "id": self._gen_id(),
                    "path": rel,
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

    @staticmethod
    def _real_img_path(p: str) -> str:
        """把蓝图里存的图片路径解析成真实存在的路径（相对工程根的新式 res/ 路径也能解析）。"""
        if not isinstance(p, str) or not p:
            return ""
        try:
            from src.config.app_paths import get_img_path

            real = get_img_path(p)
            return real if os.path.exists(real) else ""
        except Exception:
            return p if os.path.exists(p) else ""

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
        stat = QLabel(f"🖼️ 参考图列表（共 {n} 张）：结合上方「求值模式」判定 —— AND(完全/短路)：全中才走 yes、缺任一张走 no；OR(一真即真)：任意一张命中就走 yes、全缺才走 no ｜ 单张卡片右下角 ✖ 可删除")
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

        # 缩略图：走内容级缓存（同一批参考图反复切节点不再重复解码，实测 6 张 41ms → 0ms）
        pix = None
        b64 = item.get("b64") or item.get("image_b64")
        if isinstance(b64, str) and b64:
            pix = _b64_thumb(b64, 88)
        path = item.get("path", "")
        real_path = self._real_img_path(path if isinstance(path, str) else "")
        if pix is None and real_path and Image is not None:
            try:
                pix = self._pil_to_qpixmap(Image.open(real_path), size=88)
            except Exception:
                pix = None
        w = int(item.get("w", 0) or 0)
        h = int(item.get("h", 0) or 0)
        if pix is None and w > 0 and h > 0:
            pix_label = QLabel(f"({w}×{h})\n(无预览图)")
            pix_label.setAlignment(Qt.AlignCenter)
            pix_label.setStyleSheet(
                "color:#8B949E; background:#22272E;"
                "border:1px dashed #444C56; border-radius:4px;"
                "padding:12px 4px; font-size:9px;"
            )
            pix_label.setFixedSize(96, 96)
        else:
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
        btn_rep.clicked.connect(
            lambda _=False, i=idx: self.request_snip_for_node.emit(self._nid(), f"if_img_replace_{i}")
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

    def _apply_ifimg_range_visibility(self):
        """IfImageDetect：仅 range_mode=window 时显示目标窗口参数"""
        node = self._current_node
        if node is None or node.type != "IfImageDetect":
            return
        range_mode_val = str(node.params.get("range_mode", "window") or "window")
        w_sm = self._widgets.get("range_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                range_mode_val = str(d)
        temp_visible = (range_mode_val == "window")
        for tname in ("target_title", "target_exe", "target_class"):
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

    def _on_ifimg_range_mode_changed(self):
        """range_mode 切换时：刷新目标窗口参数显隐 + 刷新工具栏按钮"""
        self._apply_ifimg_range_visibility()
        self._populate_action_bar(self._current_node)

    # ---- 通用：按参数名批量显隐参数行 ----
    def _set_param_rows_visible(self, names, visible: bool):
        """把 names 里的参数行（Label + 字段控件）整行设为可见/隐藏。"""
        for name in names:
            w = self._widgets.get(name)
            if w is None:
                continue
            for row_i in range(self._form.rowCount()):
                lab_item = self._form.itemAt(row_i, QFormLayout.LabelRole)
                fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
                fld_w = fld_item.widget() if fld_item else None
                if fld_w is w:
                    if lab_item and lab_item.widget():
                        lab_item.widget().setVisible(visible)
                    fld_w.setVisible(visible)
                    break

    # ---- Screenshot：截取范围=全屏时隐藏目标窗口参数 ----
    def _apply_screenshot_range_visibility(self):
        """截图：截取范围=全屏时，不显示「目标窗口」定位参数（只有窗口模式才用得上）。"""
        node = self._current_node
        if node is None or node.type != "Screenshot":
            return
        range_val = str(node.params.get("range_mode", "window") or "window")
        w_rm = self._widgets.get("range_mode")
        if isinstance(w_rm, QComboBox):
            d = w_rm.currentData()
            if d:
                range_val = str(d)
        self._set_param_rows_visible(
            ["target_title", "target_exe", "target_class"], range_val == "window"
        )

    def _on_screenshot_range_mode_changed(self):
        self._apply_screenshot_range_visibility()
        self._populate_action_bar(self._current_node)

    # ---- OcrRecognize：按识别范围切换目标窗口参数显隐 ----
    def _apply_ocr_range_visibility(self):
        """文字：
        识别范围=目标窗口 → 显示目标窗口定位参数；
        识别范围=全屏     → 隐藏目标窗口定位参数（它们不参与）。
        """
        node = self._current_node
        if node is None or node.type != "OcrRecognize":
            return
        range_val = str(node.params.get("range_mode", "window") or "window")
        w_rm = self._widgets.get("range_mode")
        if isinstance(w_rm, QComboBox):
            d = w_rm.currentData()
            if d:
                range_val = str(d)
        self._set_param_rows_visible(
            ["target_title", "target_exe", "target_class"], range_val == "window"
        )

    def _on_ocr_range_mode_changed(self):
        self._apply_ocr_range_visibility()
        self._populate_action_bar(self._current_node)

    # ---- IfImageDetect 目标窗口拾取 ----
    def _pick_window_for_ifimg_temp(self):
        """智能拾取窗口 → 回填 IfImageDetect 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            merge = {
                "target_title": str(res.get("title", "") or ""),
                "target_exe": str(res.get("exe_name", "") or ""),
                "target_class": str(res.get("class_name", "") or ""),
            }
            self.apply_params_merge(merge)
            from PySide6.QtCore import QTimer
            title = str(res.get("title", "") or "")
            exe = str(res.get("exe_name", "") or "")
            def _show_msg():
                QMessageBox.information(
                    self, "目标窗口拾取成功",
                    f"已设置目标窗口：\n标题：{title}\n程序：{exe}\n\n此检测节点将仅在此窗口范围内截图检测。"
                )
            QTimer.singleShot(250, _show_msg)

        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_ifimg_temp(self):
        """从列表选择窗口 → 回填 IfImageDetect 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        self._pick_target_window_from_list(
            "选择检测目标窗口", "双击一行选择窗口作为检测范围（留空=不按该条件匹配）：")
    def _clear_ifimg_target(self):
        """清空 IfImageDetect 节点的目标窗口参数。"""
        if self._current_node is None or self._current_node.type != "IfImageDetect":
            return
        merge = {
            "target_title": "",
            "target_exe": "",
            "target_class": "",
        }
        self.apply_params_merge(merge)

    # ---- StillnessDetect 目标窗口 + range_mode 显隐 ----
    def _apply_ifstatic_range_visibility(self):
        """StillnessDetect：仅 range_mode=window 时显示目标窗口参数"""
        node = self._current_node
        if node is None or node.type != "StillnessDetect":
            return
        range_mode_val = str(node.params.get("range_mode", "window") or "window")
        w_sm = self._widgets.get("range_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                range_mode_val = str(d)
        temp_visible = (range_mode_val == "window")
        for tname in ("target_title", "target_exe", "target_class"):
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

    def _on_ifstatic_range_mode_changed(self):
        self._apply_ifstatic_range_visibility()
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
                "target_title": str(res.get("title", "") or ""),
                "target_exe": str(res.get("exe_name", "") or ""),
                "target_class": str(res.get("class_name", "") or ""),
            })
            from PySide6.QtCore import QTimer
            def _show():
                QMessageBox.information(self, "目标窗口拾取成功",
                    f"已设置目标窗口：\n标题：{res.get('title','')}\n程序：{res.get('exe_name','')}\n\nROI 坐标将基于此窗口。")
            QTimer.singleShot(250, _show)
        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_for_ifstatic_temp(self):
        """从列表选择窗口 → 回填 StillnessDetect 节点的 target_title/exe/class。"""
        if self._current_node is None or self._current_node.type != "StillnessDetect":
            return
        self._pick_target_window_from_list(
            "选择静止目标窗口", "双击一行选择窗口（留空=不按该条件匹配）：")
    def _clear_ifstatic_target(self):
        if self._current_node is None or self._current_node.type != "StillnessDetect":
            return
        self.apply_params_merge({
            "target_title": "",
            "target_exe": "",
            "target_class": "",
        })

    # ============================================================
    # Screenshot / OcrRecognize 节点：临时窗口拾取 / 列表选择 / OCR 测试识别
    # ============================================================
    def _pick_window_for_screenshot_temp(self):
        """智能拾取窗口 → 回填 Screenshot 节点 target_*。"""
        if self._current_node is None or self._current_node.type != "Screenshot":
            return
        self._pick_window_generic_temp("截图")

    def _list_windows_for_screenshot_temp(self):
        if self._current_node is None or self._current_node.type != "Screenshot":
            return
        self._list_windows_generic_temp("截图")

    def _pick_window_for_ocr_temp(self):
        if self._current_node is None or self._current_node.type != "OcrRecognize":
            return
        self._pick_window_generic_temp("文字")

    def _list_windows_for_ocr_temp(self):
        if self._current_node is None or self._current_node.type != "OcrRecognize":
            return
        self._list_windows_generic_temp("文字")

    def _pick_window_generic_temp(self, node_title: str):
        """通用：智能拾取窗口 → 回填当前节点的 target_title/exe/class。"""
        app_window = self.window()
        from .window_picker_overlay import WindowPickerOverlay

        def _on_result(res):
            if not res:
                return
            self.apply_params_merge({
                "target_title": str(res.get("title", "") or ""),
                "target_exe": str(res.get("exe_name", "") or ""),
                "target_class": str(res.get("class_name", "") or ""),
            })
            from PySide6.QtCore import QTimer
            def _show():
                QMessageBox.information(self, "目标窗口拾取成功",
                    f"已设置目标窗口：\n标题：{res.get('title','')}\n程序：{res.get('exe_name','')}\n\n{node_title}将以该窗口为基准。")
            QTimer.singleShot(250, _show)
        try:
            overlay = WindowPickerOverlay.prepare_and_run(app_window, delay_ms=300)
            overlay.finished.connect(_on_result)
        except Exception as e:
            QMessageBox.warning(self, "拾取启动失败", f"无法启动窗口拾取：\n{e}")

    def _list_windows_generic_temp(self, node_title: str):
        """通用：从列表选择窗口 → 回填当前节点的 target_title/exe/class。"""
        self._pick_target_window_from_list(
            f"选择{node_title}目标窗口", "双击一行选择窗口作为目标窗口：")
    def _ocr_test_recognize(self):
        """「测试识别」按钮：按当前目标窗口+ROI 现场截图，交给**独立子进程**跑 OCR，弹窗显示结果。

        OCR 引擎（paddle）必须跑在子进程里：在 GUI 主进程内 import/初始化 paddle
        会与 PySide6 的原生 DLL 冲突直接崩溃（原生崩溃无法被 try/except 捕获，
        整个编辑器进程消失——实测踩坑），且首次加载模型可达 1~2 分钟，主线程
        等待会卡死界面。
        流程：主进程截图+ROI裁剪+写临时PNG（快，安全）→ 恢复主窗口 → 子进程跑
        OCR（结果写 JSON 文件）→ QTimer 轮询收结果弹窗。子进程崩溃不影响主程序。
        """
        if getattr(self, "_ocr_test_running", False):
            return
        self._ocr_test_running = True
        node = self._current_node
        if node is None:
            self._ocr_test_running = False
            return
        p = dict(node.params or {})
        lang = str(p.get("lang", "ch") or "ch")
        range_mode = str(p.get("range_mode", "window") or "window")

        main_win = self.window()

        # 先解析目标窗口（range_mode=window）：找不到就别开始测试，直接提示——
        # 与运行时口径一致，绝不退化为全屏截取。
        pre_target = None
        status = "fullscreen"
        if range_mode == "window":
            find_fn = getattr(main_win, "_find_node_target_window", None)
            tt, st = None, "none"
            if callable(find_fn):
                tt, st = find_fn(node)
            if st != "ok":
                self._ocr_test_running = False
                if st == "notfound":
                    t_st = str(p.get("target_title", "") or "")
                    e_st = str(p.get("target_exe", "") or "")
                    c_st = str(p.get("target_class", "") or "")
                    QMessageBox.warning(
                        self, "目标窗口未找到",
                        f"识别范围=目标窗口，但当前没匹配到窗口"
                        f"（标题={t_st!r} 程序={e_st!r} 类名={c_st!r}）。\n\n"
                        "运行时同样会走「失败」出口，不会退化为全屏截取。\n"
                        "常见原因：目标窗口未打开 / 最小化到托盘 / 标题内容已变化。",
                    )
                else:
                    QMessageBox.warning(
                        self, "未配置目标窗口",
                        "识别范围=目标窗口，但没有填目标窗口定位条件（标题/程序名/类名）。\n"
                        "请补上定位条件，或把识别范围改为「全屏」。",
                    )
                return
            pre_target = tt
            status = "ok"

        win_visible = bool(main_win.isVisible())
        tmp_path = None
        ocr_state = {"warn": None}  # 传给结果弹窗的告警（匹配失败原因等）
        try:
            from PySide6.QtCore import QCoreApplication
            import time as _t

            # 隐藏主窗口，避免遮挡目标
            if win_visible:
                main_win.hide()
            for _ in range(5):
                QCoreApplication.processEvents()
            _t.sleep(0.3)

            # 复用运行时截图 + ROI 裁剪逻辑（与 F5 运行时完全一致）
            from src.blueprint.runtime.screen_capture import ScreenCapture
            from src.blueprint.runtime.executor import _crop_roi

            # 目标窗口截图（window 模式）：
            # ① 优先后台截窗 PrintWindow(PW_RENDERFULLCONTENT)——窗口被遮挡时
            #    也能拿到实时画面，不前置、不抢焦点（与运行时同一实现）；
            # ② 失败回退：走 _resolve_snip_limit（三维匹配→拉起/还原→DWM 矩形）
            #    后全屏截取、按窗口矩形裁剪。
            rect = None
            img = None
            if pre_target is not None:
                try:
                    from src.blueprint.runtime.screen_capture import capture_window_bgr
                    img = capture_window_bgr(int(pre_target.get("hwnd") or 0))
                except Exception:
                    img = None

            if img is None:
                if pre_target is not None:
                    resolve_fn = getattr(main_win, "_resolve_snip_limit", None)
                    if callable(resolve_fn):
                        rect, _label, status = resolve_fn(node)
                    if status == "ok":
                        _t.sleep(0.15)  # 等待窗口置顶重绘完成再截图

                full = ScreenCapture().capture_full_bgr()
                if full is None:
                    raise RuntimeError("截图失败")

                img = full
                if status == "ok" and rect:
                    lx, ly, lw, lh = (int(v) for v in rect)
                    H, W = full.shape[:2]
                    l2, t2, r2, b2 = max(0, lx), max(0, ly), min(W, lx + lw), min(H, ly + lh)
                    if r2 > l2 and b2 > t2:
                        img = full[t2:b2, l2:r2]

            roi = p.get("roi", (0, 0, 0, 0))
            img = _crop_roi(img, roi)

            if img is None or getattr(img, "size", 0) == 0:
                raise RuntimeError("ROI 区域为空，请先框选 ROI")

            # 写临时 PNG（%TEMP% 下 ASCII 路径，子进程读取用）
            import cv2 as _cv2
            import tempfile as _tempfile
            ok, buf = _cv2.imencode(".png", img)
            if not ok:
                raise RuntimeError("临时图像编码失败")
            fd, tmp_path = _tempfile.mkstemp(prefix="bp_ocr_test_", suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(buf.tobytes())
        except Exception as e:
            self._ocr_test_running = False
            try:
                if win_visible:
                    main_win.show()
                    main_win.raise_()
                    main_win.activateWindow()
            except Exception:
                pass
            QMessageBox.warning(self, "测试识别失败", f"{e}")
            return

        # 恢复主窗口（OCR 已交给子进程，无需等待截图阶段结束）
        try:
            if win_visible:
                main_win.show()
                main_win.raise_()
                main_win.activateWindow()
        except Exception:
            pass

        # 启动 OCR 子进程
        import subprocess
        import sys as _sys
        if getattr(_sys, "frozen", False):
            # 发布版编辑器：主 exe 带 --bp-ocr-worker 参数进入 worker 模式（不启动 GUI）
            cmd = [_sys.executable, "--bp-ocr-worker", tmp_path, lang]
        else:
            worker_py = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))))),
                "src", "blueprint", "runtime", "ocr_test_worker.py")
            cmd = [_sys.executable, worker_py, tmp_path, lang]
        result_path = tmp_path + ".result.json"
        errlog_path = tmp_path + ".err.log"
        try:
            # stderr 落日志文件：paddle 日志量大，PIPE 不读会撑爆缓冲卡死子进程；
            # 落文件以便子进程崩溃时把尾部内容带进报错弹窗
            errlog_fh = open(errlog_path, "wb")
            proc = subprocess.Popen(cmd + [result_path], stdout=subprocess.DEVNULL,
                                    stderr=errlog_fh, creationflags=0x08000000)
        except Exception as e:
            self._ocr_test_running = False
            for fp in (tmp_path,):
                try:
                    os.remove(fp)
                except Exception:
                    pass
            QMessageBox.warning(self, "测试识别失败", f"无法启动 OCR 子进程：\n{e}")
            return

        from PySide6.QtCore import QTimer
        self._ocr_test_cancelled = False
        dlg = QProgressDialog(
            "正在识别…首次使用需加载 OCR 模型，可能需要 1~2 分钟，请稍候。",
            "取消", 0, 0, self)
        dlg.setWindowTitle("测试识别")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()

        def _remove_tmp():
            try:
                errlog_fh.close()  # 先关句柄，否则 Windows 上文件被占用删不掉
            except Exception:
                pass
            for fp in (tmp_path, result_path, errlog_path):
                try:
                    os.remove(fp)
                except Exception:
                    pass

        def _on_cancel():
            self._ocr_test_cancelled = True
            try:
                proc.kill()
            except Exception:
                pass

        dlg.canceled.connect(_on_cancel)

        def _read_errlog_tail(limit=800):
            try:
                with open(errlog_path, "rb") as f:
                    data = f.read()
                return data.decode("utf-8", errors="replace")[-limit:]
            except Exception:
                return ""

        def _poll():
            if proc.poll() is None:
                return
            timer.stop()
            try:
                dlg.canceled.disconnect(_on_cancel)
            except Exception:
                pass
            dlg.close()
            self._ocr_test_running = False
            cancelled = bool(getattr(self, "_ocr_test_cancelled", False))
            self._ocr_test_cancelled = False

            # ⚠ 顺序：先读结果文件（含被识别的裁剪图，用于画标注框），读完才能清理临时文件
            result = None
            try:
                with open(result_path, encoding="utf-8") as f:
                    result = json.load(f)
            except Exception:
                result = None
            img_bgr = None
            try:
                import numpy as _np
                import cv2 as _cv2
                _data = _np.fromfile(tmp_path, dtype=_np.uint8)
                img_bgr = _cv2.imdecode(_data, _cv2.IMREAD_COLOR)
            except Exception:
                img_bgr = None
            _remove_tmp()

            if result is None:
                if cancelled:
                    return  # 用户主动取消，静默
                err_tail = _read_errlog_tail()
                if proc.returncode != 0:
                    msg = (f"识别进程异常退出（退出码 {proc.returncode}）。\n"
                           f"命令：{' '.join(cmd + [result_path])}\n")
                    if err_tail.strip():
                        msg += f"\n---- 子进程 stderr 尾部 ----\n{err_tail}"
                    else:
                        msg += "\n可能是 paddle 原生崩溃或 OCR 模型缺失，请检查 ocr_runtime/models 是否完整。"
                    QMessageBox.warning(self, "OCR 子进程异常", msg)
                else:
                    msg = "识别进程未返回有效结果。\n" + " ".join(cmd + [result_path])
                    if err_tail.strip():
                        msg += f"\n---- 子进程 stderr 尾部 ----\n{err_tail}"
                    QMessageBox.warning(self, "OCR 子进程异常", msg)
                return
            self._show_ocr_test_result(result, img_bgr, ocr_state.get("warn"))

        timer = QTimer(self)
        timer.timeout.connect(_poll)
        timer.start(300)

    def _ocr_match_hint(self, text):
        """测试识别弹窗用：按当前文字的「匹配方式 / 匹配文本」算一次匹配结果。

        返回提示文案；没填「匹配文本」返回 ""（不显示横幅）。
        与运行时 execute_OcrRecognize 的判定口径一致（包含=区分大小写 / 正则=re.search）。
        """
        node = self._current_node
        if node is None or node.type != "OcrRecognize":
            return ""
        p = dict(node.params or {})
        pattern = str(p.get("match_text", "") or "")
        if not pattern:
            return ""
        mode = str(p.get("match_mode", "contains") or "contains")
        mode_cn = "正则表达式" if mode == "regex" else "包含文本"
        try:
            if mode == "regex":
                hit = re.search(pattern, text or "") is not None
            else:
                hit = pattern in (text or "")
        except Exception as e:
            return f"⚠ 匹配表达式非法（{mode_cn} = {pattern!r}）：{e}\n（运行时本节点会走「失败」出口）"
        return ("✅ 匹配到" if hit else "❌ 未匹配到") + f"　（匹配方式={mode_cn}　匹配文本={pattern!r}）"

    def _show_ocr_test_result(self, result: dict, img_bgr=None, warn=None):
        """弹窗显示 OCR 测试识别结果：标注图预览（红框+序号）+ 文字列表 + 一键复制。

        items 字段为 {text, conf, box, index}，box 为 {x,y,w,h}（相对被识别的截取图）。
        warn 非空时在顶部显示告警横幅（如目标窗口未匹配到、按全屏截取等）。
        """
        items = result.get("items") or []
        if result.get("error"):
            err = str(result["error"])
            box = QMessageBox(QMessageBox.Warning, "识别异常",
                              f"OCR 引擎返回异常：\n{err.splitlines()[0] if err else '(空)'}",
                              QMessageBox.Ok, self)
            # 完整错误（含 traceback）放进可展开的详细信息，便于定位
            box.setDetailedText(err)
            box.exec()
            return
        if not items and not warn:
            QMessageBox.information(self, "识别结果",
                                    "未识别到任何文字/数字。\n（运行时本节点会走「无文字」出口）")
            return
        if not items and warn:
            # 截取区域不对（如全屏退回）时，警告本身就解释了为什么没识别到
            QMessageBox.warning(self, "识别结果",
                                warn + "\n\n本次未识别到任何文字/数字。\n（运行时本节点会走「无文字」出口）")
            return

        from PySide6.QtGui import QImage, QPixmap, QFont
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPlainTextEdit, QHBoxLayout, QPushButton, QApplication
        from PySide6.QtCore import Qt

        dlg = QDialog(self)
        dlg.setWindowTitle(f"OCR 识别结果（共 {len(items)} 条）")
        dlg.setModal(True)
        v = QVBoxLayout(dlg)

        if warn:
            lbl_warn = QLabel(str(warn).replace("\n", "<br>"))
            lbl_warn.setWordWrap(True)
            lbl_warn.setStyleSheet(
                "background-color: #4d2a00; color: #ffb84d; border: 1px solid #b25e00;"
                "border-radius: 4px; padding: 6px 8px; font-size: 11px;")
            v.addWidget(lbl_warn)

        # 匹配结果横幅（节点填了「匹配文本」时才显示，方便当场验证正则/文本写得对不对）
        match_line = self._ocr_match_hint(result.get("text") or "")
        if match_line:
            lbl_match = QLabel(match_line.replace("\n", "<br>"))
            lbl_match.setWordWrap(True)
            _ok = match_line.startswith("✅")
            lbl_match.setStyleSheet(
                "background-color: %s; color: %s; border: 1px solid %s;"
                "border-radius: 4px; padding: 6px 8px; font-size: 11px;"
                % (("#12261a", "#7ee787", "#2ea043") if _ok else ("#2d1618", "#ff7b72", "#b62324"))
            )
            v.addWidget(lbl_match)

        # ---- 在裁剪图上画识别框（红框 + #序号），与下方列表一一对应 ----
        pixmap = None
        if img_bgr is not None and getattr(img_bgr, "size", 0) > 0:
            try:
                import cv2 as _cv2
                vis = img_bgr.copy()
                for it in items:
                    b = it.get("box") or {}
                    try:
                        x, y = int(float(b.get("x", 0))), int(float(b.get("y", 0)))
                        w, h = int(float(b.get("w", 0))), int(float(b.get("h", 0)))
                    except Exception:
                        continue
                    if w <= 0 or h <= 0:
                        continue
                    _cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
                    label = f"#{int(it.get('index', 0)) + 1}"
                    _cv2.putText(vis, label, (x, max(14, y - 4)),
                                 _cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, _cv2.LINE_AA)
                rgb = _cv2.cvtColor(vis, _cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                qimg = QImage(rgb.tobytes(), w, h, ch * w, QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(qimg)
            except Exception:
                pixmap = None

        if pixmap is not None:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            # 限制预览宽度，过大等比缩小
            max_w = 620
            if pixmap.width() > max_w:
                pixmap = pixmap.scaledToWidth(max_w, Qt.SmoothTransformation)
            lbl.setPixmap(pixmap)
            v.addWidget(lbl)

        # ---- 文字列表：#序号 + 内容 + 置信度 ----
        lines = []
        for it in items:
            txt = str(it.get("text", ""))
            conf = it.get("conf", -1)
            try:
                conf_s = f"{float(conf):.2f}"
            except Exception:
                conf_s = str(conf)
            lines.append(f"#{int(it.get('index', 0)) + 1}  {txt}   （置信度 {conf_s}）")
        full_text = str(result.get("text", "") or "")
        show_txt = "\n".join(lines)
        if full_text.strip():
            show_txt += "\n\n---- 合并文本（可直接复制） ----\n" + full_text
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        txt.setFont(f)
        txt.setPlainText(show_txt)
        v.addWidget(txt, 1)

        h = QHBoxLayout()
        btn_copy = QPushButton("📋 复制全部文本")
        btn_close = QPushButton("关闭")
        h.addStretch(1)
        h.addWidget(btn_copy)
        h.addWidget(btn_close)
        v.addLayout(h)

        copy_text = full_text.strip() if full_text.strip() else "\n".join(
            str(it.get("text", "")) for it in items)

        def _copy():
            QApplication.clipboard().setText(copy_text)
            btn_copy.setText("✓ 已复制")
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(1500, lambda: btn_copy.setText("📋 复制全部文本"))

        btn_copy.clicked.connect(_copy)
        btn_close.clicked.connect(dlg.accept)
        dlg.resize(680, 560)
        dlg.exec()

    def _ifimg_test_screen_match(self):
        """「测试当前屏幕匹配度」按钮：直接调用运行时 TemplateMatcher，保证测试=运行时预览。"""
        if getattr(self, "_ifimg_test_running", False):
            return
        self._ifimg_test_running = True
        arr = self._parse_ifimg_list()
        if not arr:
            self._ifimg_test_running = False
            QMessageBox.information(self, "提示", "还没有添加参考图，先截图新增或导入图片～")
            return
        node = self._current_node
        if node is None:
            self._ifimg_test_running = False
            return
        p = node.params or {}
        threshold = float(p.get("threshold", 0.9) or 0.9)
        strategy = str(p.get("match_strategy", "hybrid") or "hybrid")
        algo_txt = {"hybrid": "智能混合", "template": "模板匹配", "gray": "灰度匹配"}.get(strategy, strategy)
        # 测试范围：window=只在目标窗口内测试（与运行时口径一致）；fullscreen=整屏
        range_mode = str(p.get("range_mode", "window") or "window")

        from PySide6.QtCore import QCoreApplication

        # 隐藏主窗口，避免遮挡目标
        main_win = self.window()
        win_visible = True
        try:
            if main_win and hasattr(main_win, "isVisible"):
                win_visible = bool(main_win.isVisible())
                if win_visible:
                    main_win.hide()
            for _ in range(5):
                QCoreApplication.processEvents()
            import time as _t
            _t.sleep(0.3)
        except Exception:
            pass

        # 测试范围解析：window 模式先定位目标窗口；找不到直接提示并中止
        # （与运行时口径一致，绝不退化为全屏去检测）
        region = None
        if range_mode == "window":
            resolve_fn = getattr(main_win, "_resolve_snip_limit", None)
            rect, _rect_label, rect_status = (None, "", "none")
            if callable(resolve_fn):
                try:
                    rect, _rect_label, rect_status = resolve_fn(node)
                except Exception:
                    rect, rect_status = None, "notfound"
            if rect_status != "ok" or not rect:
                self._ifimg_test_running = False
                if rect_status == "notfound":
                    QMessageBox.warning(
                        self, "目标窗口未找到",
                        f"检测范围=目标窗口，但当前没匹配到窗口"
                        f"（标题={str(p.get('target_title', '') or '')!r} "
                        f"程序={str(p.get('target_exe', '') or '')!r} "
                        f"类名={str(p.get('target_class', '') or '')!r}）。\n\n"
                        "运行时同样会走「失败」出口，不会退化为全屏去检测。\n"
                        "常见原因：目标窗口未打开 / 最小化到托盘 / 标题内容已变化。",
                    )
                else:
                    QMessageBox.warning(
                        self, "未配置目标窗口",
                        "检测范围=目标窗口，但没有填目标窗口定位条件（标题/程序名/类名）。\n"
                        "请补上定位条件，或把检测范围改为「全屏」。",
                    )
                return
            lx, ly, lw, lh = (int(v) for v in rect)
            region = (lx, ly, lw, lh)
            try:
                import time as _t2
                _t2.sleep(0.15)  # 等窗口置顶重绘后截图
            except Exception:
                pass

        progress = QProgressDialog("正在匹配图像，请稍候...", None, 0, len(arr), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.setWindowTitle("匹配中")
        progress.setValue(0)
        progress.show()
        QCoreApplication.processEvents()

        results: List[str] = []
        tmp_paths: List[str] = []
        try:
            from src.blueprint.runtime.template_matcher import TemplateMatcher
            import pyautogui

            # 创建运行时 TemplateMatcher 实例（与 F5 运行时使用同一个类）
            matcher = TemplateMatcher(log_fn=lambda *a, **k: None, is_running_fn=lambda: True)
            screen_w, screen_h = pyautogui.size()
            matcher.set_fullscreen_region((0, 0, screen_w, screen_h))

            for idx, item in enumerate(arr):
                progress.setLabelText(f"正在匹配第 {idx + 1}/{len(arr)} 张参考图...")
                progress.setValue(idx)
                QCoreApplication.processEvents()

                name = f"[{idx}]"
                if not isinstance(item, dict):
                    results.append(f"{name} （无效项）  score=0.00  ✗")
                    continue

                # 获取模板路径：优先全分辨率路径，b64只是缩略图
                path = str(item.get("path", "") or "")
                b64 = str(item.get("b64", "") or "")
                use_path = None
                real_p = self._real_img_path(path)
                if real_p:
                    use_path = real_p
                elif isinstance(b64, str) and b64:
                    try:
                        tpl_pil = self._b64_to_pil(b64)
                        if tpl_pil is not None:
                            import tempfile
                            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ba_ifimg_test_")
                            os.close(fd)
                            tpl_pil.save(tmp, format="PNG")
                            tmp_paths.append(tmp)
                            use_path = tmp
                    except Exception:
                        use_path = None
                if not use_path:
                    results.append(f"{name} （无参考图数据）  score=0.00  ✗")
                    continue

                # 直接调用运行时匹配方法（与 executor.py 完全一致的策略分支）
                pos = None
                used_chain = []
                try:
                    if strategy == "template":
                        pos = matcher.find_image(use_path, region=region, threshold=threshold, fast_mode=True)
                        used_chain.append("color")
                    elif strategy == "gray":
                        pos = matcher.find_image_gray(use_path, region=region, threshold=threshold, fast_mode=True)
                        used_chain.append("gray")
                    else:  # hybrid: color → gray
                        pos = matcher.find_image(use_path, region=region, threshold=threshold, fast_mode=True)
                        used_chain.append("color")
                        if pos is None:
                            pos = matcher.find_image_gray(use_path, region=region, threshold=threshold, fast_mode=True)
                            used_chain.append("gray")
                except Exception as e:
                    results.append(f"{name} 匹配出错={e}")
                    continue

                # 格式化结果
                score_val = 0.0
                if pos is not None and isinstance(pos, dict):
                    s = pos.get("score")
                    if isinstance(s, (int, float)):
                        score_val = float(s)
                    else:
                        score_val = 1.0
                display = os.path.basename(path) if path else f"image[{idx}]"
                tag = "✓ PASS" if pos is not None else "✗ FAIL"
                loc_txt = ""
                if pos is not None:
                    left = int(pos.get("left", 0))
                    top = int(pos.get("top", 0))
                    w = int(pos.get("width", 0))
                    h = int(pos.get("height", 0))
                    loc_txt = f" 位置=({left},{top},{w},{h})"
                chain_txt = f" 级联=[{'→'.join(used_chain)}]"
                results.append(f"{name} {display}  score={score_val:.3f}{chain_txt} (阈值≥{threshold:.2f}) {tag}{loc_txt}")
                QCoreApplication.processEvents()

            progress.setValue(len(arr))
            progress.close()
        except Exception as e:
            progress.close()
            results.append(f"匹配出错: {e}")
        finally:
            # 恢复主窗口
            try:
                if main_win and win_visible and hasattr(main_win, "show"):
                    main_win.show()
                    for _ in range(3):
                        QCoreApplication.processEvents()
            except Exception:
                pass
            # 清理临时文件
            for tp in tmp_paths:
                try:
                    if tp and os.path.exists(tp):
                        os.unlink(tp)
                except Exception:
                    pass
            self._ifimg_test_running = False

        dlg = QDialog(self)
        dlg.setWindowTitle("🔍 当前屏幕匹配结果")
        dlg.resize(620, 460)
        lay = QVBoxLayout(dlg)
        _scope_txt = "目标窗口内" if range_mode == "window" else "整块屏幕"
        info = QLabel(f"算法：{algo_txt}  |  阈值：≥{threshold:.2f}  |  共 {len(arr)} 张  |  范围：{_scope_txt}  |  级联 fallback 已启用")
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
            # template 是「相对工程根」的路径，必须解析后才能判断存在性，
            # 否则工程里的模板会被误判成「路径不存在」。
            real_tpl_path = self._real_img_path(tpl_path)
            exists = bool(real_tpl_path)
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
                if real_tpl_path and Image is not None:
                    pil = Image.open(real_tpl_path)
            except Exception:
                pil = None
        # 记录模板原始尺寸（用于偏移拾取计算）
        self._findimg_tpl_size = (pil.width, pil.height) if pil is not None else None
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
        """根据 after_action / use_feature_assist，通过 self._widgets 精准定位行并显隐。
        直接从控件读当前值，避免信号触发时 node.params 还没更新的时序问题。
        """
        node = self._current_node
        if node is None:
            return

        after_action = str(node.params.get("after_action", "none") or "none")
        w_act = self._widgets.get("after_action")
        if isinstance(w_act, QComboBox):
            data = w_act.currentData()
            if data is not None:
                after_action = str(data)
            else:
                after_action = str(w_act.currentText() or "none")

        show_offsets = after_action != "none"

        # 特征识别辅助参数：仅 use_feature_assist 非 off 时显示
        use_feat = str(node.params.get("use_feature_assist", "off") or "off") != "off"
        w_ufa = self._widgets.get("use_feature_assist")
        if isinstance(w_ufa, QComboBox):
            d = w_ufa.currentData()
            ufa_val = str(d) if d is not None else str(w_ufa.currentText() or "off")
            use_feat = ufa_val != "off"

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
            "offset": show_offsets,
            "post_delay_ms": show_offsets,
            # 特征识别辅助参数（features_json / feature_capture_width / feature_template_w/h 已隐藏，不在此管理）
            "feature_tolerance": use_feat,
            "feature_min_hits": use_feat,
        }

        # 目标窗口参数：仅 range_mode=window 时显示
        range_mode_val = str(node.params.get("range_mode", "window") or "window")
        w_sm = self._widgets.get("range_mode")
        if isinstance(w_sm, QComboBox):
            d = w_sm.currentData()
            if d:
                range_mode_val = str(d)
        temp_visible = (range_mode_val == "window")
        for tname in ("target_title", "target_exe", "target_class"):
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

    def _on_findimg_range_mode_changed(self):
        """range_mode 切换时：刷新目标窗口参数显隐 + 刷新工具栏按钮"""
        self._findimg_apply_param_visibility(initial=False)
        self._populate_action_bar(self._current_node)

    def _on_findimg_feature_assist_changed(self):
        """use_feature_assist 勾选时：刷新特征参数显隐 + 刷新工具栏按钮"""
        self._findimg_apply_param_visibility(initial=False)
        self._populate_action_bar(self._current_node)

    def _findimg_test_match(self):
        """「🔍 测试匹配」按钮：直接调用运行时 TemplateMatcher + 运行时特征辅助函数，
        保证「测试 = 运行时预览」——匹配算法级联与 use_feature_assist（验证/兜底）都会真实跑一遍。"""
        if getattr(self, "_findimg_test_running", False):
            return
        self._findimg_test_running = True
        node = self._current_node
        if node is None:
            self._findimg_test_running = False
            return
        p = node.params or {}
        tpl_path = str(p.get("template", "") or "")
        tpl_b64 = str(p.get("template_b64", "") or "")
        threshold = float(p.get("threshold", 0.9) or 0.9)
        strategy = str(p.get("match_strategy", "hybrid") or "hybrid")
        algo_txt = {"hybrid": "智能混合", "template": "模板匹配", "gray": "灰度匹配"}.get(strategy, strategy)
        # 测试范围：window=只在目标窗口内测试（与运行时口径一致）；fullscreen=整屏
        range_mode = str(p.get("range_mode", "window") or "window")

        from PySide6.QtCore import QCoreApplication

        # 获取模板路径：优先全分辨率路径（相对工程根的路径需解析），b64只是220px缩略图仅供UI预览
        use_path = None
        tmp_tpl = ""
        real_tpl = self._real_img_path(tpl_path) if isinstance(tpl_path, str) else ""
        if real_tpl:
            use_path = real_tpl
        elif isinstance(tpl_b64, str) and tpl_b64:
            try:
                tpl_pil = self._b64_to_pil(tpl_b64)
                if tpl_pil is not None:
                    import tempfile
                    fd, tmp_tpl = tempfile.mkstemp(suffix=".png", prefix="ba_findtest_tpl_")
                    os.close(fd)
                    tpl_pil.save(tmp_tpl, format="PNG")
                    use_path = tmp_tpl
            except Exception:
                use_path = None
        if not use_path:
            self._findimg_test_running = False
            QMessageBox.information(self, "提示", "还没有模板，请先点 [📸 截取目标] 或填写模板路径")
            return

        # 隐藏主窗口，避免遮挡目标
        main_win = self.window()
        win_visible = True
        try:
            if main_win and hasattr(main_win, "isVisible"):
                win_visible = bool(main_win.isVisible())
                if win_visible:
                    main_win.hide()
            for _ in range(5):
                QCoreApplication.processEvents()
            import time as _t
            _t.sleep(0.3)
        except Exception:
            pass

        # 测试范围解析：window 模式先定位目标窗口；找不到直接提示并中止
        # （与运行时口径一致，绝不退化为全屏去找）
        region = None
        if range_mode == "window":
            resolve_fn = getattr(main_win, "_resolve_snip_limit", None)
            rect, _rect_label, rect_status = (None, "", "none")
            if callable(resolve_fn):
                try:
                    rect, _rect_label, rect_status = resolve_fn(node)
                except Exception:
                    rect, rect_status = None, "notfound"
            if rect_status != "ok" or not rect:
                self._findimg_test_running = False
                if rect_status == "notfound":
                    QMessageBox.warning(
                        self, "目标窗口未找到",
                        f"找图范围=目标窗口，但当前没匹配到窗口"
                        f"（标题={str(p.get('target_title', '') or '')!r} "
                        f"程序={str(p.get('target_exe', '') or '')!r} "
                        f"类名={str(p.get('target_class', '') or '')!r}）。\n\n"
                        "运行时同样会走「失败」出口，不会退化为全屏去找。\n"
                        "常见原因：目标窗口未打开 / 最小化到托盘 / 标题内容已变化。",
                    )
                else:
                    QMessageBox.warning(
                        self, "未配置目标窗口",
                        "找图范围=目标窗口，但没有填目标窗口定位条件（标题/程序名/类名）。\n"
                        "请补上定位条件，或把找图范围改为「全屏」。",
                    )
                return
            lx, ly, lw, lh = (int(v) for v in rect)
            region = (lx, ly, lw, lh)
            try:
                import time as _t2
                _t2.sleep(0.15)  # 等窗口置顶重绘完成再截图
            except Exception:
                pass

        progress = QProgressDialog("正在匹配图像，请稍候...", None, 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.setWindowTitle("匹配中")
        progress.setValue(5)
        progress.show()
        QCoreApplication.processEvents()

        result_txt = ""
        used_chain = []
        feature_lines: List[str] = []
        pos = None
        try:
            from src.blueprint.runtime.template_matcher import TemplateMatcher
            from src.blueprint.runtime.executor import _findimage_feature_match
            import pyautogui

            # 创建运行时 TemplateMatcher 实例（与 F5 运行时使用同一个类）
            matcher = TemplateMatcher(log_fn=lambda *a, **k: None, is_running_fn=lambda: True)
            screen_w, screen_h = pyautogui.size()
            matcher.set_fullscreen_region((0, 0, screen_w, screen_h))

            progress.setValue(15)
            QCoreApplication.processEvents()

            # 直接调用运行时匹配方法（与 executor.py 完全一致的策略分支）
            if strategy == "template":
                pos = matcher.find_image(str(use_path), region=region, threshold=threshold, fast_mode=True)
                used_chain.append("color")
                progress.setValue(60)
            elif strategy == "gray":
                pos = matcher.find_image_gray(str(use_path), region=region, threshold=threshold, fast_mode=True)
                used_chain.append("gray")
                progress.setValue(60)
            else:  # hybrid: color → gray
                pos = matcher.find_image(str(use_path), region=region, threshold=threshold, fast_mode=True)
                used_chain.append("color")
                progress.setValue(35)
                QCoreApplication.processEvents()
                if pos is None:
                    pos = matcher.find_image_gray(str(use_path), region=region, threshold=threshold, fast_mode=True)
                    used_chain.append("gray")
                    progress.setValue(60)
                    QCoreApplication.processEvents()

            progress.setValue(70)
            QCoreApplication.processEvents()

            # ---------- 特征识别辅助（直接复用 runtime 的 _findimage_feature_match，绝不另写一套）----------
            feature_assist_mode = str(p.get("use_feature_assist", "off") or "off")
            _off = p.get("offset", (0, 0))
            if isinstance(_off, (list, tuple)) and len(_off) >= 2:
                try:
                    offset_x, offset_y = int(_off[0]), int(_off[1])
                except Exception:
                    offset_x, offset_y = 0, 0
            else:
                offset_x, offset_y = 0, 0
            feat_logs: List[str] = []
            feat_score = None
            if feature_assist_mode in ("verify", "fallback"):
                feature_lines.append(
                    "特征识别辅助: " + ("验证模式（模板命中后额外校验）"
                                   if feature_assist_mode == "verify"
                                   else "兜底模式（模板没命中时用特征反推）")
                )
                try:
                    _fraw = p.get("features_json", "[]")
                    _feats = json.loads(_fraw) if isinstance(_fraw, str) else (_fraw or [])
                    _fn = len(_feats) if isinstance(_feats, list) else 0
                except Exception:
                    _fn = 0
                feature_lines.append(f"特征数据: {_fn} 个")

                class _TestCtx:
                    """仅为复用运行时函数而构造的最小 ExecutionContext 替身。"""

                    def __init__(self, _m):
                        self.matcher = _m
                        self.is_running = True
                        self.is_paused = False

                    def log(self, msg, level="INFO"):
                        if len(feat_logs) < 60:
                            feat_logs.append(str(msg))

                _ctx = _TestCtx(matcher)
                # 与运行时同一套触发条件：验证=模板已命中且是 dict 结果；兜底=模板未命中
                if feature_assist_mode == "verify":
                    _need_feat = pos is not None and isinstance(pos, dict)
                else:
                    _need_feat = pos is None
                if not _need_feat:
                    feature_lines.append("特征校验: 跳过（" + (
                        "模板未命中，验证模式无需校验" if feature_assist_mode == "verify"
                        else "模板已命中，兜底模式无需启用") + "）")
                else:
                    progress.setValue(80)
                    QCoreApplication.processEvents()
                    try:
                        _fr = _findimage_feature_match(
                            _ctx, p, region, 2.5, False, 0,
                            threshold, offset_x, offset_y, strategy,
                            template_pos=(pos if feature_assist_mode == "verify" else None),
                        )
                    except Exception as _fe:
                        _fr = None
                        feature_lines.append(f"特征校验: 出错 {_fe}")
                    if _fr is None:
                        feature_lines.append("特征校验: ❌ 未通过 → 本次判定为「未命中」")
                        pos = None
                    else:
                        feat_score = float(_fr[2])
                        if feature_assist_mode == "verify":
                            feature_lines.append(f"特征校验: ✅ 通过（特征平均分 {feat_score:.3f}，坐标仍取模板命中位置）")
                        else:
                            _fcx, _fcy = int(_fr[0]), int(_fr[1])
                            pos = {
                                "left": _fcx - offset_x, "top": _fcy - offset_y,
                                "width": 1, "height": 1, "_feature_assist": True,
                            }
                            feature_lines.append(f"特征兜底: ✅ 命中，特征反推中心=({_fcx},{_fcy})")

            progress.setValue(95)
            QCoreApplication.processEvents()

            # 格式化结果
            _feat_block = ("\n" + "\n".join(feature_lines)) if feature_lines else ""
            if pos is not None:
                left = int(pos.get("left", 0))
                top = int(pos.get("top", 0))
                w = int(pos.get("width", 0))
                h = int(pos.get("height", 0))
                if isinstance(pos, dict) and pos.get("_feature_assist"):
                    # 兜底模式：坐标由特征反推，pos 里存的是「中心−偏移」，w/h 无意义
                    cx = left + offset_x
                    cy = top + offset_y
                    w = h = 0
                    score = feat_score if feat_score is not None else 0.0
                else:
                    score = float(pos.get("score", 0.0))
                    cx = int(pos.get("cx", left + w // 2))
                    cy = int(pos.get("cy", top + h // 2))
                result_txt = (
                    f"算法: {algo_txt}  |  级联: [{'→'.join(used_chain)}]\n"
                    f"设定阈值: ≥{threshold:.2f}\n"
                    f"匹配分数: {score:.3f}\n"
                    f"命中矩形: ({left}, {top}, {w}, {h})\n"
                    f"中心坐标: ({cx}, {cy})\n"
                    f"结果: ✅ 找到" + _feat_block
                )
            else:
                scope_txt = "目标窗口内" if range_mode == "window" else "整块屏幕"
                result_txt = (
                    f"算法: {algo_txt}  |  级联: [{'→'.join(used_chain)}]\n"
                    f"设定阈值: ≥{threshold:.2f}\n"
                    f"结果: ❌ 未找到\n"
                    f"（本次测试范围={scope_txt}，与节点「找图范围」设置一致）" + _feat_block
                )
            if feat_logs:
                result_txt += "\n\n---- 特征辅助日志（与运行时同源）----\n" + "\n".join(feat_logs)
        except Exception as e:
            result_txt = f"匹配出错: {e}"
        finally:
            # 恢复主窗口
            try:
                if main_win and win_visible and hasattr(main_win, "show"):
                    main_win.show()
                    for _ in range(3):
                        QCoreApplication.processEvents()
            except Exception:
                pass
            # 清理临时文件
            if tmp_tpl and os.path.exists(tmp_tpl):
                try:
                    os.unlink(tmp_tpl)
                except Exception:
                    pass
            progress.setValue(100)
            progress.close()
            self._findimg_test_running = False

        # 显示结果对话框
        dlg = QDialog(self)
        dlg.setWindowTitle("🔍 找图-测试匹配结果")
        dlg.resize(540, 380)
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
        # 保存到该蓝图对应的 res 目录（对齐「📸 截取目标」的行为）
        final_path = path
        try:
            rel = self._project_asset_rel(path, "findimg_import")
            if not rel:
                raise RuntimeError("未打开工程或拷贝失败")
            final_path = rel
        except Exception as e_img:
            # 未开工程等场景：必须显式拒绝而不是静默吞（否则路径指向工程外的原图，搬移即失效）
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "导入模板失败",
                    f"无法把模板图片拷贝到工程的 res/ 资源目录：\n{e_img}\n\n"
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

    # ---- FindImage 专属：模板图偏移拾取弹窗 ----
    def _findimg_popup_template_pick_offset(self):
        """弹出模板图片窗口，用户在图上点击位置作为点击偏移量"""
        node = self._current_node
        if node is None or node.type != "FindImage":
            return

        import base64, io as _io
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

        # 获取模板图片
        tpl_path = str(node.params.get("template", "") or "")
        b64 = str(node.params.get("template_b64", "") or "")
        pil = None
        if isinstance(b64, str) and b64:
            try:
                pil = self._b64_to_pil(b64)
            except Exception:
                pil = None
        if pil is None and isinstance(tpl_path, str) and tpl_path:
            try:
                _real = self._real_img_path(tpl_path)
                if _real and Image is not None:
                    pil = Image.open(_real)
            except Exception:
                pil = None
        if pil is None:
            QMessageBox.warning(self, "无法拾取", "未找到模板图片，请先设置模板。")
            return

        tpl_w, tpl_h = pil.width, pil.height
        cx, cy = tpl_w / 2.0, tpl_h / 2.0

        # 弹窗
        dlg = QDialog(self)
        dlg.setWindowTitle("在模板上点击拾取偏移")
        dlg.setMinimumSize(600, 500)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(8, 8, 8, 8)

        hint = QLabel("在下方模板图上点击想要偏移到的位置\n偏移 = 点击位置 - 模板中心（Y正值=向下，负值=向上）")
        hint.setStyleSheet("color: #8B949E; font-size: 10px;")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)

        # 可点击的图片标签
        img_lbl = _ClickableImageLabel()
        img_lbl.set_pick_mode(True)
        img_lbl.setAlignment(Qt.AlignCenter)
        # 缩放显示，但保留原始尺寸用于计算
        display_pil = pil.copy()
        display_pil.thumbnail((500, 500), Image.LANCZOS if Image else None)
        disp_w, disp_h = display_pil.width, display_pil.height
        pix = self._pil_to_qpixmap(display_pil, size=500)
        if pix is None:
            QMessageBox.warning(self, "无法拾取", "模板图片转换失败。")
            return
        img_lbl.setPixmap(pix)

        info_lbl = QLabel(f"模板尺寸: {tpl_w} × {tpl_h} 像素 | 中心: ({int(cx)}, {int(cy)})")
        info_lbl.setStyleSheet("color: #58A6FF; font-size: 10px;")
        info_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(info_lbl)
        lay.addWidget(img_lbl)

        result_lbl = QLabel("（请在图上点击）")
        result_lbl.setStyleSheet("color: #C9D1D9; font-size: 11px;")
        result_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(result_lbl)

        picked = {"offset": None}

        def _on_pick(nx, ny):
            # nx, ny 是 0~1 归一化坐标
            click_x = nx * tpl_w
            click_y = ny * tpl_h
            offset_x = int(round(click_x - cx))
            offset_y = int(round(click_y - cy))
            picked["offset"] = (offset_x, offset_y)
            result_lbl.setText(f"✅ 偏移: X={offset_x}, Y={offset_y}  (点击「确定」保存)")
            result_lbl.setStyleSheet("color: #2EA043; font-size: 11px; font-weight: bold;")

        img_lbl.clicked.connect(_on_pick)

        # 底部按钮
        btn_box = QHBoxLayout()
        btn_box.addStretch(1)
        btn_ok = QPushButton("确定")
        btn_ok.setStyleSheet(self._btn_style("#238636", "#2EA043"))
        btn_cancel = QPushButton("取消")
        btn_cancel.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_box.addWidget(btn_ok)
        btn_box.addWidget(btn_cancel)
        lay.addLayout(btn_box)

        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)

        if dlg.exec() == QDialog.Accepted and picked["offset"] is not None:
            self.apply_params_merge({"offset": picked["offset"]})
            self.set_node(node)  # 刷新显示

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

        # 框选ROI按钮（放到静止ROI的上面）
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        btn_roi = QPushButton("📍 框选ROI区域")
        btn_roi.setStyleSheet(self._btn_style("#d84315", "#ff7043"))
        btn_roi.setToolTip(
            "框选一个矩形区域作为静止ROI\n"
            "若已设置「目标窗口」，框选范围将自动限定在该窗口内"
        )
        btn_roi.clicked.connect(lambda _=False: self.request_snip_for_node.emit(self._nid(), "if_static_roi"))
        bl.addWidget(btn_roi)
        btn_clear_roi = QPushButton("🗑️ 清除ROI")
        btn_clear_roi.setStyleSheet(self._btn_style("#6E7681", "#8B949E"))
        btn_clear_roi.setToolTip("清除已选的ROI区域和预览图")
        btn_clear_roi.clicked.connect(lambda _=False: self._clear_ifstatic_roi(self._nid()))
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

        title = QLabel("⏸️ 静止 ROI（Region Of Interest）")
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
        pix = _b64_thumb(b64, 220) if isinstance(b64, str) and b64 else None
        if pix is None:
            empty = QLabel("（无预览）")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:#8B949E; background:#1C2128; padding:16px;"
                "border:1px dashed #2D333B; border-radius:4px; font-size:10px;"
                "min-height: 80px;"
            )
            self._form.addRow(empty)
            return
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
        """清除静止ROI和预览图"""
        self.param_changed.emit(node_id, "roi", "[0,0,0,0]")
        self.param_changed.emit(node_id, "roi_preview_b64", "")
        # 刷新面板显示
        if self._current_node and self._current_node.id == node_id:
            self.set_node(self._current_node)

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
            "range_mode": [],
            "action": [],
            "post_delay_ms": [],
            # --- 目标点（click/move/scroll 用）---
            "pos": ["click", "move", "scroll"],
            # --- drag 独有 ---
            "from_pos": ["drag"],
            "to_pos": ["drag"],
            "drag_duration_ms": ["drag"],
            # --- 按钮/时长相关 ---
            "button": ["click", "drag"],
            "click_count": ["click"],
            "hold_ms": ["click"],
            # --- scroll 独有 ---
            "scroll_dy": ["scroll"],
            "scroll_steps": ["scroll"],
        }
        # 初始调用时：在 form 顶部加一个 action 分组提示标题（用QGroupBox更清晰，这里用一个Label简化）
        if initial:
            # 遍历找到第几个参数，在 action 下面插入提示
            tip_text_map = {
                "click": "🖱 【点击模式】：在目标位置单击/双击/三击指定按键",
                "move": "✈️ 【移动模式】：把光标移到目标位置（不点）",
                "drag": "🔗 【拖拽模式】：从起点按住→移动→释放到终点（原子组合）",
                "scroll": "🎡 【滚动模式】：在目标位置滚滚轮（垂直/水平）",
            }
            title_lbl = QLabel("🖱 鼠标 · 按当前 action 显示/隐藏对应参数")
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

        # 目标窗口参数：仅 range_mode=window 时显示
        range_mode_val = str(node.params.get("range_mode", "window") or "window")
        w_cm = self._widgets.get("range_mode")
        if isinstance(w_cm, QComboBox):
            d = w_cm.currentData()
            if d:
                range_mode_val = str(d)
        temp_visible = (range_mode_val == "window")
        for tname in ("target_title", "target_exe", "target_class"):
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

    def _on_mouse_range_mode_changed(self):
        """range_mode 切换时：刷新目标窗口参数显隐 + 刷新工具栏按钮"""
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

        chips_lay = _FlowLayout(spacing=4)
        chips_widget = QWidget()
        chips_widget.setLayout(chips_lay)
        chips_widget.setObjectName("_keycombo_chips")
        main_lay.addWidget(chips_widget)

        # --- 分类下拉框区 ---
        cat_label = QLabel("从下方选择按键添加到组合：")
        cat_label.setStyleSheet("font-size: 11px; color: #8B949E; margin-top: 4px;")
        main_lay.addWidget(cat_label)

        cats = self._get_key_categories()
        dropdowns_grid = QGridLayout()
        dropdowns_grid.setSpacing(6)
        dropdowns_grid.setContentsMargins(0, 0, 0, 0)
        dropdowns_widget = QWidget()
        dropdowns_widget.setLayout(dropdowns_grid)
        dropdowns_widget.setObjectName("_keycombo_dropdowns")

        for i, (cat_name, items) in enumerate(cats):
            combo = _disable_wheel(QComboBox())
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
                    try:
                        c.setCurrentIndex(0)  # 重置为占位项
                    except RuntimeError:
                        pass  # 控件可能已被重建销毁
            combo.currentIndexChanged.connect(_on_select)
            row, col = i // 4, i % 4
            dropdowns_grid.addWidget(combo, row, col)
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
        self._keycombo_update_dropdowns(current_keys)

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
        """保存 key_combo 到 node.params（不重建表单，避免销毁触发回调的下拉框控件）

        注意：这里**不能**先手动写 node.params 再发信号 —— 主窗口的撤销栈靠比较
        「旧值 vs 新值」决定是否入栈，提前写入会让新旧值相等、修改无法 Ctrl+Z 撤销。
        统一交给 param_changed 让主窗口写入。
        """
        import json
        node = self._current_node
        if node is None:
            return
        val = json.dumps(keys, ensure_ascii=False)
        try:
            self.param_changed.emit(node.id, "key_combo", val)
        except Exception:
            node.params["key_combo"] = val   # 兜底：没有任何接收方时至少把值落下去

    def _keycombo_add_key(self, key):
        """添加一个键到组合"""
        key = str(key).strip().lower()
        keys = self._keycombo_get_current_keys()
        if key not in keys:
            keys.append(key)
        self._keycombo_save(keys)
        self._keycombo_render_chips(keys)
        self._keycombo_update_dropdowns(keys)

    def _keycombo_remove_key(self, key):
        """从组合中移除一个键"""
        key = str(key).strip().lower()
        keys = self._keycombo_get_current_keys()
        keys = [k for k in keys if k != key]
        self._keycombo_save(keys)
        self._keycombo_render_chips(keys)
        self._keycombo_update_dropdowns(keys)

    def _keycombo_clear(self):
        """清空组合"""
        self._keycombo_save([])
        self._keycombo_render_chips([])
        self._keycombo_update_dropdowns([])

    def _keycombo_update_dropdowns(self, selected_keys):
        """更新下拉列表：隐藏已选按键项"""
        picker = None
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is not None and fld_w.objectName() == "_keycombo_picker":
                picker = fld_w
                break
        if picker is None:
            return
        dropdowns = picker.findChild(QWidget, "_keycombo_dropdowns")
        if dropdowns is None:
            return
        selected_set = set(selected_keys)
        for combo in dropdowns.findChildren(QComboBox):
            view = combo.view()
            for i in range(combo.count()):
                val = combo.itemData(i)
                if not val:
                    view.setRowHidden(i, False)
                    continue
                view.setRowHidden(i, val.lower() in selected_set)

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

    def _append_subgraph_hint(self, text: str):
        """子画布容器节点（Sequence/Parallel）专属：追加一个「双击进入子画布」提示条。"""
        hint = QLabel(f"📂 {text}")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#2DD4BF; background:#0E6B5C22; padding:8px 10px;"
            "border:1px dashed #0E6B5C; border-radius:4px; font-size:9px;"
        )
        self._form.addRow(hint)

    def _replace_component_path_with_combobox(self):
        """Component：blueprint_path 输入框替换为下拉列表（列出工程里所有「组件蓝图」）。"""
        if self._current_node is None or self._current_node.type != "Component":
            return
        w_bp = self._widgets.get("blueprint_path")
        if w_bp is None:
            return
        target_row = -1
        for row_i in range(self._form.rowCount()):
            fld_item = self._form.itemAt(row_i, QFormLayout.FieldRole)
            fld_w = fld_item.widget() if fld_item else None
            if fld_w is w_bp:
                target_row = row_i
                break
        if target_row < 0:
            return
        # 收集工程里所有「组件蓝图」（按 bp_type 判定，与所在目录无关）
        components = self._scan_component_blueprints()
        combo = _disable_wheel(QComboBox())
        combo.setStyleSheet(self._edit_style())
        current = str(self._current_node.params.get("blueprint_path", "") or "")
        # 第一项：占位「未选择」
        combo.addItem("（未选择组件）", "")
        for rel, abs_path in components:
            combo.addItem(rel, rel)
        idx = combo.findData(current)
        if idx < 0:
            idx = 0
        combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(
            lambda i, c=combo: self.param_changed.emit(self._nid(), "blueprint_path", c.currentData() or "")
        )
        # 选完组件后通知主窗口：自动对齐端口数 + 循环引用检测（在参数已写入之后触发）
        combo.currentIndexChanged.connect(
            lambda i, c=combo: self.component_chosen.emit(self._nid(), str(c.currentData() or ""))
        )
        # 替换
        self._form.removeRow(target_row)
        new_label = QLabel("组件蓝图")
        new_label.setStyleSheet("color: #C9D1D9; font-size: 9px;")
        new_label.setToolTip(
            "选择一张「组件蓝图」来调用。\n"
            "组件蓝图放在哪个目录都可以（资源树里右键蓝图 → 🧩 设为组件蓝图）。"
        )
        self._form.insertRow(target_row, new_label, combo)
        self._widgets["blueprint_path"] = combo
        # 便捷入口：直接跳过去编辑该组件（在画布上双击组件节点效果相同）
        btn_open = QPushButton("↗ 打开这个组件")
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_open.setToolTip("跳转到该组件蓝图编辑它（画布上双击组件节点同效）")
        btn_open.setStyleSheet(self._btn_style("#1F6FEB", "#388BFD"))
        btn_open.setEnabled(bool(str(combo.currentData() or "")))
        btn_open.clicked.connect(
            lambda _=False, c=combo: self.request_open_component.emit(str(c.currentData() or ""))
        )
        combo.currentIndexChanged.connect(
            lambda _i, b=btn_open, c=combo: b.setEnabled(bool(c.currentData() or ""))
        )
        self._form.addRow(btn_open)
        if not components:
            combo.setToolTip(
                "当前工程里还没有任何「组件蓝图」。\n"
                "在资源树里右键任意蓝图 → 🧩 设为组件蓝图，或点「＋组件」新建一张，"
                "它就会出现在这里。"
            )

    def _scan_component_blueprints(self) -> List[tuple]:
        """扫描整个工程里「被标记为组件蓝图」的 .bp.json，返回 [(相对工程根路径, 绝对路径)]。

        组件放哪个目录由用户决定，所以扫全工程（跳过 cache/debug 等运行产物目录），
        再按蓝图自身的 bp_type 元数据过滤 —— 不再看它在不在 components/ 目录下。
        """
        results: List[tuple] = []
        try:
            from src.blueprint.core import asset_store, bp_kind
            from src.blueprint.project_manager import ProjectManager

            pm = ProjectManager.instance()
            if not pm.is_open() or not pm.project_root:
                return results
            root = pm.project_root
            for bp in asset_store.iter_project_blueprints(root):
                if not bp_kind.is_component(bp, root):
                    continue
                try:
                    rel = os.path.relpath(bp, root).replace("\\", "/")
                except Exception:
                    rel = os.path.basename(bp)
                results.append((rel, bp))
            results.sort(key=lambda x: x[0].lower())
        except Exception:
            pass
        return results

    def _append_snip_roi_preview(self, node_type: str):
        """Screenshot/OcrRecognize：在 ROI 参数下追加一个 ROI 预览图（框选后显示）。"""
        node = self._current_node
        if node is None:
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2D333B;")
        self._form.addRow(sep)

        roi = node.params.get("roi", (0, 0, 0, 0))
        if isinstance(roi, str):
            try:
                roi = tuple(int(v) for v in json.loads(roi))
            except Exception:
                roi = (0, 0, 0, 0)
        if not isinstance(roi, (list, tuple)) or len(roi) < 4:
            roi = (0, 0, 0, 0)
        x, y, w, h = (int(v) for v in roi[:4])
        has_roi = (w > 0 and h > 0)

        title = QLabel("🖼️ ROI 预览")
        title.setStyleSheet("color:#29b6f6; font-weight:bold; font-size:10px;")
        self._form.addRow(title)

        info = QLabel(f"位置 ({x},{y})  ·  尺寸 {w}×{h}" + ("" if has_roi else "  （未框选）"))
        info.setStyleSheet("color:#C9D1D9; font-size:9px;")
        self._form.addRow(info)

        b64 = node.params.get("roi_preview_b64", "")
        pix = _b64_thumb(b64, 200) if isinstance(b64, str) and b64 else None
        if pix is None:
            empty = QLabel("（无预览，点上方「🖱 框选ROI」）")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:#8B949E; background:#1C2128; padding:12px;"
                "border:1px dashed #2D333B; border-radius:4px; font-size:10px; min-height:60px;"
            )
            self._form.addRow(empty)
            return
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setPixmap(pix)
        lbl.setStyleSheet("QLabel { background:#000; border:2px solid #29b6f6; border-radius:4px; padding:2px; }")
        self._form.addRow(lbl)

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
            # ⚠ 走 _connect_aux：这些是**参数控件**，复用路径下它们还活着，
            #   直接 connect 会连上一堆绑定到旧节点的 _refresh（端口列表越刷越乱）。
            if isinstance(w, QSpinBox):
                self._connect_aux(f"ports.{pname}", w.valueChanged, _refresh)
            elif isinstance(w, QLineEdit):
                self._connect_aux(f"ports.{pname}", w.textChanged, _refresh)
            elif isinstance(w, QPlainTextEdit):
                self._connect_aux(f"ports.{pname}", w.textChanged, _refresh)

    # ---------------- 编辑器 ----------------
    def _clear_form(self):
        self._widgets.clear()
        # 专属 UI 的连接表只在「整表重建」时清空：控件都销毁了，连接也随之失效。
        # （同类型复用路径不清，因为参数控件还活着，靠 _connect_aux 的 key 去去重）
        self._aux_conns.clear()
        self._aux_widgets = []
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _build_editor(self, pd: ParamDef, current: Any) -> Optional[QWidget]:
        node = self._current_node
        editor = pd.editor

        if editor == EDITOR_INT:
            sp = _disable_wheel(QSpinBox())
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
            sp.valueChanged.connect(lambda v, pn=pd.name: self.param_changed.emit(self._nid(), pn, int(v)))
            sp.setStyleSheet(self._edit_style())
            return sp

        if editor == EDITOR_FLOAT:
            sp = _disable_wheel(QDoubleSpinBox())
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
            sp.valueChanged.connect(lambda v, pn=pd.name: self.param_changed.emit(self._nid(), pn, float(v)))
            sp.setStyleSheet(self._edit_style())
            return sp

        if editor == EDITOR_STRING:
            le = QLineEdit(str(current or ""))
            le.textChanged.connect(lambda t, pn=pd.name: self.param_changed.emit(self._nid(), pn, t))
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
            cb.toggled.connect(lambda v, pn=pd.name: self.param_changed.emit(self._nid(), pn, bool(v)))
            cb.setStyleSheet("color: #C9D1D9;")
            return cb

        if editor == EDITOR_ENUM:
            cb = _disable_wheel(QComboBox())
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
                lambda i, pn=pd.name, combo=cb: self.param_changed.emit(self._nid(), pn, combo.currentData())
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
            le.textChanged.connect(lambda t, pn=pd.name: self.param_changed.emit(self._nid(), pn, t))
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
            le.textChanged.connect(lambda t, pn=pd.name: self.param_changed.emit(self._nid(), pn, t))
            btn_pick = QPushButton("裁图")
            btn_pick.setStyleSheet(self._btn_style("#238636", "#2EA043"))
            btn_pick.clicked.connect(lambda _=False, pn=pd.name: self.request_pick_template.emit(self._nid(), pn))
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
                s = _disable_wheel(QSpinBox())
                s.setRange(-100000, 100000)
                s.setValue(values[i])
                s.setPrefix(f"{labels[i]} ")
                s.setStyleSheet(self._edit_style())
                # 限制宽度，确保右侧 📍 按钮不被遮挡（Inspector 最大宽 460px）
                s.setMaximumWidth(95 if size == 2 else 80)
                spins.append(s)
                row.addWidget(s, 0)  # stretch=0：靠左排列，不拉伸

            def _on_change(*_, pn=pd.name, sps=spins, sz=size):
                vals = [s.value() for s in sps]
                t = tuple(vals)
                self.param_changed.emit(self._nid(), pn, t)
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
                    lambda _=False, pn=pd.name: self.request_pick_coord.emit(self._nid(), pn)
                )
                row.addWidget(btn_pick)

            # FindImage 的 offset 参数：附加 📍 从模板拾取偏移按钮
            if editor == EDITOR_TUPLE2 and node is not None and node.type == "FindImage" and pd.name == "offset":
                btn_tpl = QPushButton("📍")
                btn_tpl.setFixedWidth(32)
                btn_tpl.setToolTip(
                    "点击弹出模板图片窗口，在图上点选位置作为点击偏移量。\n"
                    "程序自动计算该点相对模板中心的偏移量 (X, Y) 并写入。"
                )
                btn_tpl.setStyleSheet(
                    "QPushButton { background: #238636; color: white; border: none;"
                    " border-radius: 4px; font-size: 14px; }"
                    "QPushButton:hover { background: #2EA043; }"
                )
                btn_tpl.clicked.connect(lambda _=False: self._findimg_popup_template_pick_offset())
                row.addWidget(btn_tpl)

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
            def _on_text(pn=pd.name, widget=te):
                self.param_changed.emit(self._nid(), pn, widget.toPlainText())
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
            f" padding: 3px 6px; font-size: 10px; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
        )

    # ============= 工具方法（供截图工具回填参数）=============
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
            # 不要提前写 params：撤销栈靠「旧值 vs 新值」比较决定是否入栈，
            # 提前写入会让新旧值相等而漏记撤销。统一交给 param_changed 由主窗口写入。
            try:
                self.param_changed.emit(nid, pname, pval)
            except Exception:
                pass
        # 重载表单让UI显示最新值
        self.set_node(self._current_node)
        return True
