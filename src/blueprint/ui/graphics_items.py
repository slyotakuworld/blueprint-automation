import os
from collections import OrderedDict
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QMenu,
    QStyle,
    QStyleOptionGraphicsItem,
)

from ..core.models import Edge, Node, Port, PortDirection, PortType
from ..core.node_library import NODE_LIBRARY


# ============================================================
# 颜色常量
# ============================================================
COL_BG = QColor("#22272E")
COL_BG_DARK = QColor("#1C2128")
COL_GRID = QColor("#2D333B")
COL_SELECTED = QColor("#58A6FF")
COL_HIGHLIGHT_RUN = QColor("#3FB950")

# ========== 线条高亮颜色方案 ==========
# 临时连线（连线中）：暗淡灰，表示还没连上
COL_TEMP_WIRE = QColor("#6E7681")
# 选中节点时关联线条高亮：亮绿色，醒目识别
COL_NODE_LINK_HL = QColor("#39FF14")
# 选中线条本身高亮：橙色，和搜索高亮一致
COL_EDGE_SELECTED_HL = QColor("#FF9800")

COL_EXEC = QColor("#FFFFFF")
COL_BOOL = QColor("#F778BA")
COL_INT = QColor("#79C0FF")
COL_FLOAT = QColor("#A371F7")
COL_TUPLE2 = QColor("#D29922")
COL_TUPLE4 = QColor("#F0B429")
COL_PATH = QColor("#56D4DD")
COL_STRING = QColor("#54AEFF")
COL_ANY = QColor("#8B949E")


# ============================================================
# 辅助工具函数：预览图取图（带内容级缓存）
# ============================================================
# paint() 会在平移/缩放/选中/悬停/搜索高亮时被**高频**调用，而画一张预览图原本每次都要：
#   base64 解码 → PIL LANCZOS 缩略 → QImage → QPixmap → SmoothTransformation 缩放。
# 实测：单个 194×139 模板「每次 paint ≈ 3ms」，4 张参考图的检测节点 ≈ 5.5ms；
# 60 个预览节点整场重绘 200ms+（≈5fps）—— 这就是画布拖动/缩放卡顿的根源。
# 这里按「输入内容」缓存最终 QPixmap：参数不变时 paint 只做一次 drawPixmap，0 解码。
# key 里带内容本身，所以用户重新框选模板（b64 变）自然命中新 key，绝不会读到旧图。
_PIX_CACHE: "OrderedDict[tuple, object]" = OrderedDict()
_PIX_CACHE_MAX = 128
_PIX_NULL = object()   # 占位：表示「这张图取不到」，避免每次 paint 反复重试解码


def _cache_get(key):
    val = _PIX_CACHE.get(key)
    if val is not None:
        _PIX_CACHE.move_to_end(key)
    return val


def _cache_put(key, val):
    _PIX_CACHE[key] = val
    _PIX_CACHE.move_to_end(key)
    while len(_PIX_CACHE) > _PIX_CACHE_MAX:
        _PIX_CACHE.popitem(last=False)
    return val


def _decode_b64_pixmap(b64_text: str, size: int) -> Optional[QPixmap]:
    """真正的解码实现（无缓存）：base64 → PIL 缩略 → QPixmap。失败返回 None。"""
    try:
        import base64, io
        from PIL import Image as PILImage
        data = base64.b64decode(b64_text)
        pil = PILImage.open(io.BytesIO(data))
        pil.thumbnail((size, size), PILImage.LANCZOS)
        # ⚠ 必须统一转成 RGBA 再喂 QImage：Qt 的 QImage(uchar*, w, h, fmt) 会按
        #   4 字节对齐解读每行。用 RGB(3 字节/像素) 且「宽×3 不是 4 的倍数」时，
        #   Qt 算出的行宽与实际数据不一致 → 每行整体错位，画布预览表现为
        #   「色彩错乱 + 斜切拉伸变形」。RGBA 每像素 4 字节天然对齐，绕开这个坑。
        if pil.mode != "RGBA":
            pil = pil.convert("RGBA")
        w, h = pil.size
        from PySide6.QtGui import QImage
        qimg = QImage(pil.tobytes("raw", "RGBA"), w, h, QImage.Format_RGBA8888)
        return QPixmap.fromImage(qimg.copy())
    except Exception:
        return None


def _b64_to_qpixmap_safe(b64_text: str, size: int = 64) -> Optional[QPixmap]:
    """将base64图片转为QPixmap，限定最大长宽为size；失败返回None（带缓存）。"""
    if not b64_text:
        return None
    key = ("b64", b64_text, int(size))
    hit = _cache_get(key)
    if hit is not None:
        return None if hit is _PIX_NULL else hit
    pix = _decode_b64_pixmap(b64_text, size)
    _cache_put(key, pix if pix is not None else _PIX_NULL)
    return pix


def _fit_preview_pixmap(b64_text: str, box_w: int, box_h: int) -> Optional[QPixmap]:
    """把预览图等比缩放到「填满 box_w×box_h」，尽量铺满显示区域（带缓存，失败返回 None）。

    取图时按 box 的 2 倍尺寸解码，避免 _b64_to_qpixmap_safe 先把图缩到很小再放大（糊）。
    """
    if not b64_text:
        return None
    box_w, box_h = int(box_w), int(box_h)
    key = ("fit", b64_text, box_w, box_h)
    hit = _cache_get(key)
    if hit is not None:
        return None if hit is _PIX_NULL else hit
    pix = _b64_to_qpixmap_safe(b64_text, size=max(box_w, box_h) * 2)
    out = None
    if pix is not None and not pix.isNull():
        out = pix.scaled(box_w, box_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _cache_put(key, out if out is not None else _PIX_NULL)
    return out


def _b64_from_path_cached(path: str) -> str:
    """读磁盘图片文件并转成 base64（按 mtime/大小缓存，文件不变只读一次）。

    FindImage 模板极少情况下只有文件路径、没有内联 b64。原实现是在 paint() 里
    「每次都读盘 + 重新 PNG 编码 + base64」——一旦触发就是拖影级卡顿（磁盘 IO 在绘制线程）。
    这里改成按 (路径, mtime, 大小) 缓存；文件被替换时 mtime 变化自动失效。
    """
    if not path:
        return ""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = ("path", path, int(st.st_mtime_ns), int(st.st_size))
    hit = _cache_get(key)
    if hit is not None:
        return "" if hit is _PIX_NULL else hit
    out = ""
    try:
        import base64 as _b64, io as _io
        from PIL import Image as _PILImg
        with _PILImg.open(path) as im:
            _buf = _io.BytesIO()
            im.save(_buf, format="PNG")
        out = _b64.b64encode(_buf.getvalue()).decode("ascii")
    except Exception:
        out = ""
    _cache_put(key, out if out else _PIX_NULL)
    return out


def color_for_data(dt: str) -> QColor:
    return {
        "bool": COL_BOOL,
        "int": COL_INT,
        "float": COL_FLOAT,
        "tuple2": COL_TUPLE2,
        "tuple4": COL_TUPLE4,
        "path": COL_PATH,
        "string": COL_STRING,
        "list_path": COL_PATH,
        "any": COL_ANY,
    }.get(dt, COL_ANY)


# ============================================================
# 摘要共建：五个视觉节点共用的「范围」显示口径
# ============================================================
def range_display(p: dict) -> str:
    """画布摘要：把「范围」渲染成一行短文本（全屏 / 目标窗口 / 未设置）。

    找图 / 检测 / 截图 / 静止 / 文字 五个"视觉节点"共用同一口径 —— 这五个节点
    运行时定位窗口走的是同一套 `executor._find_window_3d`（title 子串 / exe 子串 /
    class 全等），所以画布上显示的也必须是同一句话，不能各写各的。
    """
    p = p or {}
    if str(p.get("range_mode", "window") or "window") == "fullscreen":
        return "全屏"
    return (str(p.get("target_title", "") or "").strip()
            or str(p.get("target_exe", "") or "").strip()
            or str(p.get("target_class", "") or "").strip()
            or "未设置")


def _param_num(value, default):
    """画布摘要取数：只把「真·缺失」当默认值，合法的 0 原样保留。

    与 executor.param_num 同一口径（这里刻意不跨层 import，UI 模块不依赖 runtime）。
    典型受害者：Wait 的 seconds/min_seconds、Loop 的 interval_sec —— 参数 min=0，
    画布上原本会把用户填的 0 显示成默认值 1，与运行时实际等待时间对不上。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ============================================================
# 尺寸常量
# ============================================================
NODE_WIDTH = 200
NODE_HEADER_H = 30
NODE_PORT_H = 22
NODE_ROUND = 8
PORT_RADIUS = 6
PORT_MARGIN_X = 8

# 预览图区域（FindImage 模板 / StillnessDetect ROI 预览）：
# 「铺满」而不是「缩到 72px 塞进去」—— 原来固定 size=72，在 176 宽的框里只用到约 41%，很浪费。
# 现在按原分辨率取图后等比缩放到填满这个框（SmoothTransformation），居中绘制。
# ⚠ 改 PREVIEW_BAND_H 必须同步 _preview_extra_height()：那边少算了高度，
#   boundingRect 就比实际绘制矮，拖动节点会留下拖影（QGraphicsView 用 BoundingRectViewportUpdate）。
PREVIEW_BAND_W = NODE_WIDTH - 24   # 176
PREVIEW_BAND_H = 100
PREVIEW_BAND_PAD = 4               # 图片与深色底框之间的留白


# ============================================================
# PortItem
# ============================================================
class PortItem(QGraphicsObject):
    """节点上的端口小圆圈/三角，被按下时开始画线，在另一端口释放则创建连线。"""

    port_pressed = Signal(object)         # 左键按下端口（scene 据此记录"待连线起点"）
    port_right_clicked = Signal(object)   # 右键点击端口（scene 弹上下文菜单）

    def __init__(self, port: Port, parent: "NodeItem"):
        super().__init__(parent)
        self.port = port
        self._node_item = parent
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.setZValue(2)
        flags = self.flags()
        self.setFlags(flags)

    # ---------------- 属性 ----------------
    def is_exec(self) -> bool:
        return self.port.type == PortType.EXEC

    def is_output(self) -> bool:
        return self.port.direction == PortDirection.OUT

    def data_type(self) -> str:
        return self.port.data_type

    def port_name(self) -> str:
        return self.port.name

    def node_item(self) -> "NodeItem":
        return self._node_item

    def compatible_with(self, other: "PortItem") -> bool:
        if other is self or self._node_item is other._node_item:
            return False
        if self.port.type != other.port.type:
            return False
        # 方向必须相反
        if self.port.direction == other.port.direction:
            return False
        # EXEC 只要是 exec 都能接
        if self.port.type == PortType.EXEC:
            return True
        # DATA 必须 data_type 相同或一方为 any
        a, b = self.port.data_type, other.port.data_type
        return a == b or a == "any" or b == "any"

    def global_center(self) -> QPointF:
        return self.mapToScene(self.boundingRect().center())

    # ---------------- 外观 ----------------
    def boundingRect(self) -> QRectF:
        s = PORT_RADIUS * 2 + 4
        return QRectF(-s / 2, -s / 2, s, s)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.is_exec():
            self._paint_exec(painter)
        else:
            self._paint_data(painter)

    def _base_color(self) -> QColor:
        if self.is_exec():
            return COL_EXEC
        return color_for_data(self.port.data_type)

    def _paint_data(self, painter: QPainter):
        col = self._base_color()
        radius = PORT_RADIUS + (1 if self._hover else 0)
        pen = QPen(col)
        pen.setWidth(2 if self._hover else 1)
        painter.setPen(pen)
        painter.setBrush(QBrush(col if self._hover else QColor("#22272E")))
        painter.drawEllipse(QPointF(0, 0), radius, radius)

    def _paint_exec(self, painter: QPainter):
        col = self._base_color()
        r = PORT_RADIUS + 1
        if self.port.direction == PortDirection.OUT:
            tri = QPolygonF([QPointF(r, 0), QPointF(-r * 0.6, -r), QPointF(-r * 0.6, r)])
        else:
            tri = QPolygonF([QPointF(-r, 0), QPointF(r * 0.6, -r), QPointF(r * 0.6, r)])
        pen = QPen(col)
        pen.setWidth(2 if self._hover else 1)
        painter.setPen(pen)
        painter.setBrush(QBrush(col if self._hover else QColor("#22272E")))
        painter.drawPolygon(tri)

    # ---------------- 事件 ----------------
    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            self.port_pressed.emit(self)
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self.port_right_clicked.emit(self)
            event.accept()
            return
        super().mousePressEvent(event)

    # 注：连线创建的释放判定统一由 NodeScene.mouseReleaseEvent 处理
    # （Qt 鼠标抓取机制下，port_released 信号只会发给按下的端口，无法识别目标端口）
    # 因此这里不再需要 mouseReleaseEvent 发射 port_released 信号。


# ============================================================
# NodeItem
# ============================================================
class NodeItem(QGraphicsObject):
    """节点卡片：标题栏 + 端口列表。"""

    node_position_changed = Signal(str, QPointF)
    node_selected_changed = Signal(str, bool)
    request_delete = Signal(str)
    breakpoint_toggled = Signal(str, bool)   # (node_id, new_state) 断点切换后通知外部（scene 可更新 persist 缓存）
    node_double_clicked = Signal(str)        # node_id，双击节点（用于进入子图/循环体子画布）
    request_find_in_log = Signal(str)        # node_id，右键「在日志中查找」
    request_open_component = Signal(str)     # node_id，右键「打开引用的组件」（Component 节点）

    def __init__(self, node: Node):
        super().__init__()
        self.node = node
        self._port_items: Dict[str, PortItem] = {}
        self._running: bool = False
        self._highlighted: bool = False              # 搜索匹配高亮（橙色）
        self._edge_link_highlighted: bool = False    # 关联边被选中时的联动高亮（蓝色虚线）
        self._flash_until_ms: int = 0
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemSendsGeometryChanges
            | QGraphicsItem.ItemIsFocusable
        )
        self.setZValue(1)
        # 节点级绘制缓存（关键性能开关）：
        # 每次视口重绘（滚轮缩放、拖动、fit 全图、选中联动的整体刷新）都会对每个可见节点
        # 重跑一遍 paint：圆角卡片描边 + 标题栏 + 端口 + 摘要文字 + 预览图，实测单节点
        # ~1.5ms，40 个节点整屏重绘要 50~90ms（10~20fps，肉眼就是「卡顿不流畅」）。
        # DeviceCoordinateCache 把节点按设备像素渲染成位图，**移位（平移/拖动）时直接贴图、不重绘**；
        # 只有内容真正变化（选中/高亮/断点/运行/参数/缩放）才重建一次。实测 40 节点整屏重绘
        # 从 ~55ms 降到 ~20ms（50fps），60 节点内存仅 +3.8MB。已验证：所有状态变化都能正确
        # 失效缓存（不会画过期画面），且与不缓存时逐像素一致。
        self.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        self._build_ports()
        x, y = int(node.pos[0]), int(node.pos[1])
        self.setPos(x, y)

    # ---------------- 断点切换 ----------------
    def toggle_breakpoint(self) -> bool:
        """切换当前节点的断点状态，返回新状态（True=开断点）。
        状态直接写入底层 Node.breakpoint（与 models.Node.to_dict/from_dict 兼容，
        保存蓝图时自动序列化到 JSON，无需 scene 额外同步）。"""
        new_state = not bool(getattr(self.node, "breakpoint", False))
        self.node.breakpoint = new_state
        try:
            self.breakpoint_toggled.emit(self.node.id, new_state)
        except Exception:
            pass
        self.update()
        return new_state

    # ---------------- 右键菜单（QMenu 上下文菜单） ----------------
    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent):
        """节点右键：切换断点 + 在日志中查找 + （组件节点）打开引用的组件。"""
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background: #21262D; color: #C9D1D9; border: 1px solid #30363D; padding: 4px; }"
            "QMenu::item { padding: 6px 18px; border-radius: 3px; }"
            "QMenu::item:selected { background: #1F6FEB; color: #FFFFFF; }"
            "QMenu::item:disabled { color: #484F58; }"
        )
        # 菜单项 1：切换断点（Checkable，显示 Check 勾号）
        is_on = bool(getattr(self.node, "breakpoint", False))
        act_bp = QAction("切换断点 (Breakpoint)", menu)
        act_bp.setCheckable(True)
        act_bp.setChecked(is_on)
        act_bp.setIconText("🔴" if is_on else "⭘")
        def _do_toggle():
            self.toggle_breakpoint()
        act_bp.triggered.connect(_do_toggle)
        menu.addAction(act_bp)

        # 菜单项 2：在运行日志里查找本节点（日志行 ↔ 画布节点 双向定位）
        act_find = QAction("🔍 在日志中查找该节点", menu)
        act_find.triggered.connect(lambda _=False: self.request_find_in_log.emit(self.node.id))
        menu.addAction(act_find)

        # 菜单项 3：Component 节点专属 —— 打开它引用的那张组件蓝图
        if getattr(self.node, "type", "") == "Component":
            ref = str((getattr(self.node, "params", None) or {}).get("blueprint_path", "") or "")
            act_open = QAction("🧩 打开引用的组件", menu)
            act_open.setEnabled(bool(ref))
            act_open.triggered.connect(lambda _=False: self.request_open_component.emit(self.node.id))
            menu.addAction(act_open)

        # 在鼠标处弹出
        menu.exec(QCursor.pos())
        super().contextMenuEvent(event)

    # ---------------- 结构 ----------------
    def _meta(self):
        return NODE_LIBRARY.get(self.node.type)

    def mouseDoubleClickEvent(self, event):
        """节点双击：发射 node_double_clicked，供外部进入子图/循环体子画布。
        不拦截事件，继续交给默认处理（选中等）。"""
        try:
            self.node_double_clicked.emit(self.node.id)
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)

    def _summary_text(self) -> str:
        """返回节点关键参数摘要（显示在画布节点端口区域下方）。
        各节点类型显示不同关键信息，便于用户直观识别节点用途。"""
        n = self.node
        p = n.params or {}
        t = n.type
        if t == "OpenApp":
            path = str(p.get("path", "") or "").strip()
            if not path:
                return ""
            name = os.path.basename(path)
            args = str(p.get("args", "") or "").strip()
            return f"▶ {name} {args}".strip()
        if t == "WebBrowser":
            url = str(p.get("url", "") or "").strip()
            if not url:
                return ""
            if len(url) > 42:
                url = url[:40] + "…"
            return url
        if t == "Command":
            mode = str(p.get("shell_mode", "") or "").strip()
            cap = p.get("capture_streams", True)
            cap_txt = "捕获输出" if cap else "不捕获"
            mode_map = {"cmd_c": "CMD", "powershell": "PowerShell", "no_shell": "直执行"}
            mode_txt = mode_map.get(mode, mode)
            return f"[{mode_txt}] {cap_txt}"
        if t == "HttpRequest":
            method = str(p.get("method", "") or "").strip()
            url = str(p.get("url", "") or "").strip()
            if not url:
                return f"[{method}]" if method else ""
            if len(url) > 36:
                url = url[:34] + "…"
            return f"[{method}] {url}" if method else url
        if t == "Component":
            # 调用组件：画布上直接显示"调的是谁"，双击可跳转过去编辑
            bp = str(p.get("blueprint_path", "") or "").strip()
            if not bp:
                return "🧩 未选择组件（双击进入 / 属性面板下拉选择）"
            stem = bp.replace("\\", "/").rsplit("/", 1)[-1]
            if stem.lower().endswith(".bp.json"):
                stem = stem[: -len(".bp.json")]
            return f"🧩 组件：{stem}"
        if t == "Parallel":
            return "并行体（双击进入子画布）"
        if t == "Loop":
            infinite = bool(p.get("infinite", False))
            interval = _param_num(p.get("interval_sec"), 1.0)
            if infinite:
                return f"∞ 无限循环  间隔{interval:g}s"
            max_it = int(p.get("max_iterations", 5) or 5)
            return f"循环{max_it}次  间隔{interval:g}s"
        if t == "Sequence":
            return "顺序体（双击进入子画布）"
        if t == "Notify":
            msg = str(p.get("message", "") or "").strip()
            duration = float(p.get("duration_sec", 3.0) or 3.0)
            sound = bool(p.get("play_sound", False))
            popup = bool(p.get("show_popup", False))
            tags = []
            if popup:
                tags.append("弹窗")
            if sound:
                tags.append("🔊")
            tag_txt = f" {' '.join(tags)}" if tags else ""
            msg_preview = msg.replace("\n", " ")[:30]
            if len(msg) > 30:
                msg_preview += "…"
            return f"{duration:g}s{tag_txt} {msg_preview}"
        if t == "VarSwitch":
            from ..core.node_library import parse_varswitch_conditions
            raw = str(p.get("cases", "") or "")
            # 新格式解析
            conds = parse_varswitch_conditions(raw if isinstance(raw, str) else "")
            valid = [c for c in conds if not c.get("error")]
            if valid:
                labels = [c["label"] for c in valid]
                preview = " ".join(labels[:6])
                if len(labels) > 6:
                    preview += f"…+{len(labels)-6}"
                return f"{len(valid)}条件: {preview}"
            return "（未设置条件）"
        if t == "Clipboard":
            mode = str(p.get("mode", "read") or "read")
            mode_map = {"read": "读取", "write": "写入", "clear": "清空"}
            return f"[{mode_map.get(mode, mode)}]"
        if t == "PressKey":
            kb_mode = str(p.get("kb_mode", "text") or "text")
            if kb_mode == "text":
                text = str(p.get("text", "") or "").strip()
                use_paste = bool(p.get("use_paste", True))
                press_enter = bool(p.get("press_enter", False))
                tags = []
                if use_paste:
                    tags.append("粘贴")
                if press_enter:
                    tags.append("↵")
                tag_txt = f" [{' '.join(tags)}]" if tags else ""
                preview = text.replace("\n", " ")[:24]
                if len(text) > 24:
                    preview += "…"
                return f"[输入文本]{tag_txt} {preview}".strip()
            else:
                # 读 key_combo（JSON数组）
                keys_display = ""
                key_combo_raw = str(p.get("key_combo", "[]") or "[]")
                try:
                    import json as _json
                    kc = _json.loads(key_combo_raw) if isinstance(key_combo_raw, str) else key_combo_raw
                    if isinstance(kc, list) and any(k.strip() for k in kc if isinstance(k, str)):
                        keys_display = "+".join(k.strip() for k in kc if k.strip())
                except Exception:
                    pass
                count = int(p.get("count", 1) or 1)
                cnt_txt = f" x{count}" if count > 1 else ""
                return f"[按键组合] {keys_display}{cnt_txt}"
        if t == "Wait":
            delay_type = str(p.get("delay_type", "fixed") or "fixed")
            if delay_type == "random":
                min_s = _param_num(p.get("min_seconds"), 1.0)
                max_s = _param_num(p.get("max_seconds"), 3.0)
                return f"[随机] {min_s:g}~{max_s:g}s"
            else:
                sec = _param_num(p.get("seconds"), 1.0)
                return f"[固定] {sec:g}s"
        return ""

    def _summary_lines(self) -> List[str]:
        """返回多行摘要（用于 SetVariable / VarSwitch / StillnessDetect / IfImageDetect 等需要多行显示的节点）。
        每行一个字符串，节点高度会根据行数自适应。"""
        n = self.node
        p = n.params or {}
        t = n.type
        if t == "Mouse":
            # 画布只展示「坐标基准 + 动作类型 + 鼠标按键 + 次数」四件事，
            # 刻意**不写具体坐标**（用户明确）：坐标是每次执行都可能变的细节，
            # 画布上留给能一眼认出"这个鼠标节点是干嘛的"的信息。
            #
            # 坐标基准的展示口径与「文字 / 检测 / 静止 / 截图」的「范围」**完全一致**：
            # 首行 `🎯 <range_display>`（全屏 / 目标窗口标题(或程序名/类名) / 未设置），
            # 而不是只写 `[窗口]/[全屏]` 这种分叉标记 —— 用户明确要求"参考文字节点"。
            # （Mouse 的 `range_mode` + 定位三件套与视觉节点同键，可直接复用 range_display。）
            action = str(p.get("action", "click") or "click")
            action_map = {
                "click": "点击", "move": "移动", "drag": "拖拽", "scroll": "滚动",
            }
            parts: List[str] = ["🖱", action_map.get(action, action)]
            btn = str(p.get("button", "left") or "left")
            if action in ("click", "drag"):
                parts.append({"left": "左键", "right": "右键"}.get(btn, btn))
            if action == "click":
                # 次数始终显示（x1 / x2 / x3）：用户点名「次数」属于要在画布上看到的信息，
                # 单击时的 x1 也照写，避免"这一次是几连击"要靠默认值去猜。
                cnt = int(p.get("click_count", 1) or 1)
                parts.append(f"x{cnt}")
            elif action == "scroll":
                # 滚动没有按键，这里用「次数」位表达方向+重复次数（方向不是坐标，保留）
                steps = int(p.get("scroll_steps", 1) or 1)
                dy = int(p.get("scroll_dy", 0) or 0)
                parts.append(f"{'上' if dy > 0 else '下'}x{steps}" if dy else f"x{steps}")
            return [
                f"🎯 {range_display(p)}",
                " ".join(parts),
            ]
        if t == "StillnessDetect":
            still_s = float(p.get("stillness_seconds", 5.0) or 5.0)
            samp_s = float(p.get("sample_interval_seconds", 1.0) or 1.0)
            # 只写「在哪儿检测」+「等多久」。
            # · 超时不写（用户明确：静止节点画布不需要超时信息）；
            # · ROI 也不写文字（用户明确：这三个视觉节点只要"范围 + ROI 预览图"），
            #   ROI 内容由下面的缩略图直接体现。
            # ⚠ 静止 + 间隔挤一行要量宽度：实测最坏（3600s / 60s）140px < 可用 184px，安全。
            lines = [
                f"🎯 {range_display(p)}",
                f"⏱ 静止 {still_s:g}s   📷 间隔 {samp_s:g}s",
            ]
            # 如果有预览图，预留预览占位行（实际图像由paint方法绘制）
            b64 = str(p.get("roi_preview_b64", "") or "")
            if b64:
                lines.append("[__PREVIEW__:stillness]")  # 仅用于高度计算
            return lines
        if t == "FindImage":
            algo = str(p.get("match_strategy", "hybrid") or "hybrid")
            algo_map = {"hybrid": "智能混合", "template": "模板匹配", "gray": "灰度匹配"}
            algo_txt = algo_map.get(algo, algo)
            # 目标窗口（只读定位三件套；全屏模式直接写「全屏」）
            win_txt = range_display(p)
            # 找到后执行
            act = str(p.get("after_action", "none") or "none")
            act_map = {
                "none": "不执行", "click": "单击", "double_click": "双击",
                "right_click": "右键",
            }
            act_txt = act_map.get(act, act)
            # 特征识别辅助
            feat = str(p.get("use_feature_assist", "off") or "off")
            feat_map = {"off": "关", "verify": "验证", "fallback": "兜底"}
            feat_txt = feat_map.get(feat, feat)
            lines = [
                f"🎯 {win_txt}",
                f"算法:{algo_txt}  找到后:{act_txt}",
                f"特征:{feat_txt}",
            ]
            # 如果有模板b64预览，加预览占位
            tb = str(p.get("template_b64", "") or "")
            if tb:
                lines.append("[__PREVIEW__:findimg]")
            return lines
        if t == "IfImageDetect":
            mode = str(p.get("eval_mode", "complete") or "complete")
            if mode == "complete":
                mode_txt = "完全求值"
            elif mode == "or_true":
                mode_txt = "一真即真"
            else:
                mode_txt = "短路模式"
            algo = str(p.get("match_strategy", "hybrid") or "hybrid")
            algo_map = {"hybrid": "智能混合", "template": "模板匹配", "gray": "灰度匹配"}
            algo_txt = algo_map.get(algo, algo)
            # 检测范围（与其它视觉节点同口径：全屏模式直接写「全屏」）
            win_txt = range_display(p)
            # 图片数（只用于决定要不要画缩略图预览，不再写进摘要文本）
            img_count = 0
            images_raw = p.get("images_json", "[]")
            try:
                if isinstance(images_raw, str) and images_raw:
                    import json as _json
                    imgs = _json.loads(images_raw)
                elif isinstance(images_raw, list):
                    imgs = images_raw
                else:
                    imgs = []
                img_count = len(imgs) if isinstance(imgs, list) else 0
            except Exception:
                img_count = 0
            # 顺序按「先看在哪检测 → 再看用什么算法、怎么求值」。
            # 刻意**不写**「参考图 N 张 / 相似度」——张数和阈值属于参数细节，
            # 画布上留给"在哪检测、怎么判"这两件真正影响读懂流程的事；
            # 参考图本身仍以缩略图形式画出来（见 paint），但不再配文字。
            lines = [
                f"🎯 {win_txt}",
                f"算法:{algo_txt}  求值:{mode_txt}",
            ]
            # 如果有图片，加预览标记行（用于高度计算；实际图像由paint绘制）
            if img_count > 0:
                lines.append("[__PREVIEW__:ifimg]")
            return lines
        if t == "Screenshot":
            # 截图：只写「截取范围」。ROI **不写文字**（用户明确：只要"范围 + ROI 预览图"），
            # ROI 内容由下面的缩略图直接体现。
            lines = [f"🎯 {range_display(p)}"]
            if str(p.get("roi_preview_b64", "") or ""):
                lines.append("[__PREVIEW__:screenshot]")
            return lines
        if t == "OcrRecognize":
            # 文字：识别范围 + 匹配方式（正则/包含）+ 匹配文本。
            # ROI 同样不写文字，只出预览图（见 Screenshot 的说明）。
            lines = [f"🎯 {range_display(p)}"]
            mode = str(p.get("match_mode", "contains") or "contains")
            mtext = str(p.get("match_text", "") or "").strip()
            if not mtext:
                lines.append("🔎 匹配: （未设置）")
            elif mode == "regex":
                lines.append(f"🔎 正则: {mtext}")
            else:
                lines.append(f"🔎 包含: {mtext}")
            if str(p.get("roi_preview_b64", "") or ""):
                lines.append("[__PREVIEW__:ocr]")
            return lines
        if t == "SetVariable":
            batch = str(p.get("batch_text", "") or "").strip()
            if not batch:
                return ["（未设置变量）"]
            lines = []
            for line in batch.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name = line.split("=", 1)[0].strip()
                    val = line.split("=", 1)[1].strip()
                    if len(val) > 20:
                        val = val[:18] + "…"
                    lines.append(f"{name} = {val}")
                else:
                    lines.append(line)
                if len(lines) >= 8:
                    lines.append("…")
                    break
            return lines if lines else ["（未设置变量）"]
        if t == "VarSwitch":
            from ..core.node_library import parse_varswitch_conditions
            raw = str(p.get("cases", "") or "")
            conds = parse_varswitch_conditions(raw if isinstance(raw, str) else "")
            lines: List[str] = []
            err_count = 0
            for c in conds:
                if c.get("error"):
                    err_count += 1
                    continue
                lab = str(c.get("label", ""))
                if len(lab) > 40:
                    lab = lab[:38] + "…"
                lines.append(f"● {lab}")
                if len(lines) >= 6:
                    break
            # 还有没显示的条件，追加计数
            shown_ok = len([c for c in conds if not c.get("error")])
            if shown_ok > len(lines):
                lines.append(f"  …还有 {shown_ok - len(lines)} 条条件")
            if err_count > 0:
                lines.append(f"⚠ {err_count} 处格式错误（点「检查格式」查看）")
            if not lines:
                lines.append("（未设置条件）")
            return lines
        return []

    def _has_summary(self) -> bool:
        """是否支持摘要显示区域（用于固定节点高度，避免有/无内容时高度跳变）。

        ⚠ 这里必须与 _summary_text()/_summary_lines() 支持的节点类型保持一致：
        只要 paint() 会画摘要，就必须在此登记。否则 node_height() 不预留摘要高度，
        boundingRect() 会比实际绘制内容矮一截，摘要文字被画到卡片外——
        而 QGraphicsView 用的是 BoundingRectViewportUpdate，超出 boundingRect 的像素
        在节点移动时不会被重绘，表现为拖动节点时留下一串"拖影"。
        （Component / Parallel 之前就是漏登记的两种，已补上。）
        """
        return self.node.type in (
            "OpenApp", "WebBrowser", "Command", "HttpRequest",
            "Loop", "Sequence", "Notify", "VarSwitch", "Clipboard", "SetVariable",
            "Mouse", "PressKey", "Wait",
            "StillnessDetect", "IfImageDetect", "FindImage",
            "Screenshot", "OcrRecognize",
            "Component", "Parallel",
        )

    def _preview_extra_height(self) -> int:
        """如果节点有预览图（静止/截图/文字的 ROI 预览、FindImage 模板、检测参考图），返回额外高度。"""
        t = self.node.type
        p = self.node.params or {}
        if t in ("StillnessDetect", "Screenshot", "OcrRecognize"):
            # 这三个节点的 ROI 预览共用同一套画法：roi_preview_b64 有图就占一条预览带
            b64 = str(p.get("roi_preview_b64", "") or "")
            if not b64:
                return 0
            return PREVIEW_BAND_H + 16  # 预览框高度 + 上下padding（必须与 paint() 一致）
        if t == "FindImage":
            # 仅模板预览（搜索区域功能已移除）
            tb64 = str(p.get("template_b64", "") or "")
            has_tpl = bool(tb64)
            if not has_tpl:
                path = str(p.get("template", "") or "")
                import os as _os
                if isinstance(path, str) and path and _os.path.exists(path):
                    has_tpl = True
            return (PREVIEW_BAND_H + 16) if has_tpl else 0
        if t == "IfImageDetect":
            raw = p.get("images_json", "[]")
            imgs = []
            try:
                if isinstance(raw, str) and raw:
                    import json as _json
                    imgs = _json.loads(raw)
                elif isinstance(raw, list):
                    imgs = raw
            except Exception:
                imgs = []
            if not isinstance(imgs, list) or len(imgs) == 0:
                return 0
            # 每行最多显示4张，每张约60px高 + padding
            cols = 4
            rows = (len(imgs) + cols - 1) // cols
            return 16 + rows * 64
        return 0

    def node_height(self) -> int:
        n_in = max(1, len(self.node.inputs))
        n_out = max(1, len(self.node.outputs))
        rows = max(n_in, n_out)
        h = NODE_HEADER_H + rows * NODE_PORT_H + 10
        # 支持摘要的节点类型固定预留摘要区域高度，保持高度一致
        if self._has_summary():
            # 多行摘要：根据行数动态调整高度
            multi = self._summary_lines()
            # 过滤预览标记行（[__PREVIEW__:*]），这些行的高度由_preview_extra_height控制
            text_lines = [l for l in multi if not (isinstance(l, str) and l.startswith("[__PREVIEW__:"))]
            if text_lines:
                h += 18 + len(text_lines) * 16
            else:
                h += 28
            # 预览图额外高度
            h += self._preview_extra_height()
        return h

    def _build_ports(self):
        h = self.node_height()
        n_in = len(self.node.inputs)
        n_out = len(self.node.outputs)
        in_y0 = NODE_HEADER_H + 5 + (NODE_PORT_H / 2)
        out_y0 = NODE_HEADER_H + 5 + (NODE_PORT_H / 2)
        for i, p in enumerate(self.node.inputs):
            pi = PortItem(p, self)
            pi.setParentItem(self)
            y = in_y0 + i * NODE_PORT_H
            pi.setPos(PORT_MARGIN_X, y)
            self._port_items[p.name] = pi
        for i, p in enumerate(self.node.outputs):
            pi = PortItem(p, self)
            pi.setParentItem(self)
            y = out_y0 + i * NODE_PORT_H
            pi.setPos(NODE_WIDTH - PORT_MARGIN_X, y)
            self._port_items[p.name] = pi

    def port_item(self, port_name: str) -> Optional[PortItem]:
        return self._port_items.get(port_name)

    def all_port_items(self) -> List[PortItem]:
        return list(self._port_items.values())

    # ---------------- 外观 ----------------
    def boundingRect(self) -> QRectF:
        pad = 4
        return QRectF(
            -pad, -pad,
            NODE_WIDTH + pad * 2,
            self.node_height() + pad * 2,
        )

    def global_center(self) -> QPointF:
        """返回节点在场景坐标下的中心点（用于搜索跳转居中）。"""
        return self.mapToScene(self.boundingRect().center())

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0, 0, NODE_WIDTH, self.node_height()),
            NODE_ROUND, NODE_ROUND,
        )
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0, 0, NODE_WIDTH, self.node_height())
        meta = self._meta()
        title_color = QColor(meta.color) if meta else QColor("#3A3F4B")
        accent = QColor(meta.accent) if meta else QColor("#58A6FF")
        base_title = meta.title if meta else self.node.type
        extra_name = str(self.node.params.get("custom_name", "") or "").strip()
        if extra_name:
            title = f"{self.node.id} · {base_title} · {extra_name}"
        else:
            title = f"{self.node.id} · {base_title}"

        # 选中描边
        is_sel = bool(option.state & QStyle.State_Selected)
        if is_sel:
            painter.setPen(QPen(COL_SELECTED, 2))
        else:
            painter.setPen(QPen(QColor("#2D333B"), 1))
        painter.setBrush(QBrush(QColor(COL_BG_DARK)))
        painter.drawRoundedRect(rect, NODE_ROUND, NODE_ROUND)

        # 标题栏
        header_rect = QRectF(0, 0, NODE_WIDTH, NODE_HEADER_H)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(title_color))
        path = QPainterPath()
        path.addRoundedRect(header_rect, NODE_ROUND, NODE_ROUND)
        clip = QPainterPath()
        clip.addRect(0, 0, NODE_WIDTH, NODE_HEADER_H)
        painter.drawPath(path & clip)
        # ================ 断点视觉标识（标题栏左上角红色圆点 / IDE 风格） ================
        # breakpoint=True: 实心红圆 + 白色描边（醒目，像 VSCode 断点）
        # breakpoint=False: 不画任何东西（保持界面整洁，用户右键切换到 ON 才显示）
        is_bp_on = bool(getattr(self.node, "breakpoint", False))
        if is_bp_on:
            painter.setRenderHint(QPainter.Antialiasing, True)
            bp_cx, bp_cy, bp_r = 16, (NODE_HEADER_H / 2), 7
            # 1. 白色描边（在彩色标题栏上清晰可见）
            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.setBrush(QBrush(QColor("#F85149")))   # 番茄红 = GitHub 危险色，和 IDE 断点一致
            painter.drawEllipse(QPointF(bp_cx, bp_cy), bp_r, bp_r)
        # 标题文本（左边留 32px 给断点圆点，避免文字与红点重叠）
        painter.setPen(QPen(QColor("#FFFFFF")))
        f = QFont()
        f.setBold(True)
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(
            QRectF(32, 0, NODE_WIDTH - 44, NODE_HEADER_H),
            Qt.AlignVCenter | Qt.AlignLeft,
            title,
        )

        # 端口标签
        f2 = QFont()
        f2.setPointSize(8)
        painter.setFont(f2)
        pen = QPen(QColor("#C9D1D9"))
        painter.setPen(pen)

        for i, p in enumerate(self.node.inputs):
            y = NODE_HEADER_H + 5 + NODE_PORT_H / 2 + i * NODE_PORT_H
            label = p.label or p.name
            tx = PORT_MARGIN_X + PORT_RADIUS + 4
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2, NODE_WIDTH / 2, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )
        for i, p in enumerate(self.node.outputs):
            y = NODE_HEADER_H + 5 + NODE_PORT_H / 2 + i * NODE_PORT_H
            label = p.label or p.name
            tx = NODE_WIDTH / 2
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2,
                       NODE_WIDTH / 2 - PORT_MARGIN_X - PORT_RADIUS - 4, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignRight,
                label,
            )

        # 关键参数摘要（端口区域下方，浅色小字）
        summary = self._summary_text()
        multi_lines = self._summary_lines()
        # 过滤预览标记行，这些不是真的文本行
        text_lines = [l for l in multi_lines if not (isinstance(l, str) and l.startswith("[__PREVIEW__:"))]
        if summary or text_lines:
            n_in = max(1, len(self.node.inputs))
            n_out = max(1, len(self.node.outputs))
            rows = max(n_in, n_out)
            sy = NODE_HEADER_H + 5 + rows * NODE_PORT_H + 4
            f3 = QFont()
            f3.setPointSize(8)
            painter.setFont(f3)
            painter.setPen(QPen(QColor("#8B949E")))
            cursor_y = sy
            _fm_sum = painter.fontMetrics()
            _sum_w = NODE_WIDTH - 16
            if text_lines:
                # 多行摘要：逐行绘制（超宽自动省略，避免长标题溢出节点）
                for i, line in enumerate(text_lines):
                    painter.drawText(
                        QRectF(8, cursor_y, _sum_w, 16),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        _fm_sum.elidedText(line, Qt.ElideRight, _sum_w),
                    )
                    cursor_y += 16
            else:
                painter.drawText(
                    QRectF(8, cursor_y, _sum_w, 22),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    _fm_sum.elidedText(str(summary), Qt.ElideRight, _sum_w),
                )
                cursor_y += 22
            # 预览图绘制（StillnessDetect / FindImage / IfImageDetect）
            t = self.node.type
            p = self.node.params or {}
            if t in ("StillnessDetect", "Screenshot", "OcrRecognize"):
                # 静止 / 截图 / 文字：三者都是「目标窗口/全屏 + ROI」的视觉节点，
                # ROI 预览图共用同一套画法（居中铺满深色底框）
                b64 = str(p.get("roi_preview_b64", "") or "")
                if b64:
                    cursor_y += 8
                    box_w = PREVIEW_BAND_W - PREVIEW_BAND_PAD * 2
                    box_h = PREVIEW_BAND_H - PREVIEW_BAND_PAD * 2
                    band_x = (NODE_WIDTH - PREVIEW_BAND_W) // 2
                    painter.fillRect(band_x, cursor_y - 2, PREVIEW_BAND_W, PREVIEW_BAND_H, QColor("#0D1117"))
                    pix = _fit_preview_pixmap(b64, box_w, box_h)
                    if pix is not None:
                        px = band_x + (PREVIEW_BAND_W - pix.width()) // 2
                        py = (cursor_y - 2) + (PREVIEW_BAND_H - pix.height()) // 2
                        painter.drawPixmap(px, py, pix)
            elif t == "FindImage":
                # 模板预览（template_b64 或有 template 路径）
                tb64 = str(p.get("template_b64", "") or "")
                # 没有内联 b64 时才退回磁盘路径；结果按 (路径,mtime) 缓存，不会每次 paint 都读盘+编码
                if not tb64:
                    tb64 = _b64_from_path_cached(str(p.get("template", "") or ""))
                if tb64:
                    cursor_y += 8
                    box_w = PREVIEW_BAND_W - PREVIEW_BAND_PAD * 2
                    box_h = PREVIEW_BAND_H - PREVIEW_BAND_PAD * 2
                    # 深色底框先铺，再把等比放大到铺满的模板图画在正中间
                    painter.fillRect(12, cursor_y - 2, PREVIEW_BAND_W, PREVIEW_BAND_H, QColor("#0D1117"))
                    pix = _fit_preview_pixmap(tb64, box_w, box_h)
                    if pix is not None:
                        px = 12 + (PREVIEW_BAND_W - pix.width()) // 2
                        py = (cursor_y - 2) + (PREVIEW_BAND_H - pix.height()) // 2
                        painter.drawPixmap(px, py, pix)
            elif t == "IfImageDetect":
                raw = p.get("images_json", "[]")
                imgs = []
                try:
                    if isinstance(raw, str) and raw:
                        import json as _json
                        imgs = _json.loads(raw)
                    elif isinstance(raw, list):
                        imgs = raw
                except Exception:
                    imgs = []
                if isinstance(imgs, list) and len(imgs) > 0:
                    cursor_y += 8
                    cols = 4
                    thumb_w = (NODE_WIDTH - 16 - (cols - 1) * 4) // cols
                    thumb_h = 56
                    for idx, img in enumerate(imgs):
                        col = idx % cols
                        row = idx // cols
                        bx = 8 + col * (thumb_w + 4)
                        by = cursor_y + row * (thumb_h + 4)
                        b64_img = ""
                        if isinstance(img, dict):
                            b64_img = str(img.get("b64", "") or "")
                        if b64_img:
                            pix = _b64_to_qpixmap_safe(b64_img, size=min(thumb_w, thumb_h))
                            if pix is not None:
                                px = bx + (thumb_w - pix.width()) // 2
                                py = by + (thumb_h - pix.height()) // 2
                                painter.drawPixmap(px, py, pix)
                            else:
                                painter.fillRect(bx, by, thumb_w, thumb_h, QColor("#2D333B"))
                        else:
                            painter.fillRect(bx, by, thumb_w, thumb_h, QColor("#2D333B"))

        # 运行高亮
        if self._running:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(COL_HIGHLIGHT_RUN, 3)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, NODE_ROUND, NODE_ROUND)

        # 搜索匹配高亮（橙色3px）
        if self._highlighted:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor("#ff9800"), 3)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), NODE_ROUND + 1, NODE_ROUND + 1)

        # 关联边被选中时的联动高亮（蓝色虚线2px）
        # 当用户选中一条连线时，两端节点会显示这个高亮，便于在线多时识别关联节点
        if self._edge_link_highlighted:
            painter.setBrush(Qt.NoBrush)
            pen = QPen(QColor("#58A6FF"), 2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), NODE_ROUND + 3, NODE_ROUND + 3)

        # 闪烁（红色3px，到期自动恢复）
        try:
            import time
            now_ms = int(time.time() * 1000)
            if now_ms < self._flash_until_ms:
                painter.setBrush(Qt.NoBrush)
                # 0.5s方波闪烁
                on = ((now_ms // 250) % 2) == 0
                pen = QPen(QColor("#f44336"), 4 if on else 2)
                painter.setPen(pen)
                painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), NODE_ROUND + 2, NODE_ROUND + 2)
                # 触发下一帧重绘，直到到期
                if now_ms < self._flash_until_ms - 50:
                    try:
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(80, self.update)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------------- 搜索匹配/闪烁接口 ----------------
    def set_highlighted(self, on: bool):
        """画布搜索框实时高亮：画一圈橙色3px边"""
        on = bool(on)
        if self._highlighted != on:
            self._highlighted = on
            self.update()

    def set_edge_link_highlighted(self, on: bool):
        """关联边被选中时的联动高亮：画一圈蓝色虚线边（问题3）。"""
        on = bool(on)
        if self._edge_link_highlighted != on:
            self._edge_link_highlighted = on
            self.update()

    def flash(self, duration_ms: int = 2000):
        """红色方框闪烁（跳转后强调效果）"""
        try:
            import time
            self._flash_until_ms = int(time.time() * 1000) + max(100, int(duration_ms))
            self.update()
        except Exception:
            pass

    # ---------------- 事件 ----------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            p = self.pos()
            self.node.pos = (int(p.x()), int(p.y()))
            self.node_position_changed.emit(self.node.id, p)
        elif change == QGraphicsItem.ItemSelectedChange:
            self.node_selected_changed.emit(self.node.id, bool(value))
        return super().itemChange(change, value)

    def sync_from_node(self):
        """当 node.inputs / node.outputs 变更后（例如 FindAnyImage 动态分支），重建端口。"""
        for pi in self.all_port_items():
            pi.setParentItem(None)
            scene = pi.scene()
            if scene:
                scene.removeItem(pi)
        self._port_items.clear()
        self._build_ports()
        self.refresh_geometry()

    def mark_geometry_dirty(self) -> Optional[QRectF]:
        """在修改 node.params **之前**调用：返回当前（旧的）几何在场景中的矩形。

        为什么必须「提前」拿：
        QGraphicsItem::update(rect) 传进去的 rect 会被 Qt 裁剪到调用时刻的
        boundingRect 内（实测确认：传一个超大矩形，scene 也只按当前 boundingRect 记脏区）。
        而 node_height() 是**即时计算**的——一旦写进新的 params，boundingRect() 立刻
        变成缩小后的尺寸，此时再想标记「旧的那块」已经晚了，那块区域永远进不了脏区，
        视图不会重绘它 → 画布上留下一片残影。

        因此正确顺序是：
            old = item.mark_geometry_dirty()   # 改参数前
            node.params[...] = new_value       # 改参数
            item.refresh_geometry(old)         # 刷新几何 + 重绘旧区域
        """
        old_scene_rect = None
        try:
            old_scene_rect = self.mapToScene(self.boundingRect()).boundingRect()
        except Exception:
            old_scene_rect = None
        try:
            self.update(self.boundingRect())
        except Exception:
            self.update()
        return old_scene_rect

    def refresh_geometry(self, old_scene_rect: Optional[QRectF] = None):
        """参数变化后刷新节点几何（高度随摘要行数 / 预览图变化）+ 重绘。

        配合 mark_geometry_dirty() 使用（见那里的完整说明）。做三件事：
          · prepareGeometryChange()：作废旧几何，让场景索引 / 命中判定 / 连线吸附
            跟着新尺寸走（否则节点变矮后旧区域仍被索引，点上去会命中空处）；
          · update()：把新几何记为待重绘；
          · 把 old_scene_rect 映射回每个视图的视口并显式 update：
            这一步是清残影的关键——只靠 scene 的脏区合并，节点变矮时旧区域
            经常被收缩掉、不参与重绘；直接点名让视口重画那块，才一定干净。
        """
        self.prepareGeometryChange()
        self.update()
        if old_scene_rect is not None and not old_scene_rect.isEmpty():
            try:
                sc = self.scene()
                if sc is not None:
                    for v in sc.views():
                        try:
                            r = v.mapFromScene(old_scene_rect).boundingRect()
                            v.viewport().update(r)
                        except Exception:
                            pass
            except Exception:
                pass

    # ---------------- 数据接口 ----------------
    def set_running(self, running: bool):
        # 只在状态真正变化时刷新：运行时每一步都会遍历所有节点调本方法（见 NodeScene.set_running_node），
        # 原实现无条件 update() → 每个节点每步都作废重绘一次，节点一多就是毫无意义的整屏刷新。
        running = bool(running)
        if self._running == running:
            return
        self._running = running
        self.update()


# ============================================================
# EdgeItem（贝塞尔曲线）
# ============================================================
class EdgeItem(QGraphicsPathItem):
    """连接两个端口的贝塞尔连线。"""

    def __init__(self, edge: Optional[Edge] = None, parent=None):
        super().__init__(parent)
        self.edge = edge
        self._from_pos = QPointF()
        self._to_pos = QPointF()
        self._from_port: Optional[PortItem] = None
        self._to_port: Optional[PortItem] = None
        self._temp_mode = edge is None
        # ZValue=-2：低于 PortItem(2)/NodeItem(1)，点击时端口永远优先命中，
        # 避免连线命中区盖住端口导致用户点不到端口
        self.setZValue(-2)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._base_color = COL_ANY
        self._hover = False
        # 关联节点被选中时的联动高亮（问题5）：加粗+加亮
        self._node_link_highlighted = False
        self._update_pen()

    def _update_pen(self):
        """线条颜色优先级（从高到低）：
        1. 选中线条本身 → 橙色高亮 COL_EDGE_SELECTED_HL（加粗）
        2. 关联节点被选中 → 亮绿色高亮 COL_NODE_LINK_HL（加粗）
        3. hover → 基础色 lighter 130%
        4. 普通 → 基础色（临时连线默认暗淡灰，悬停端口时变绿/红反馈）
        """
        if self.isSelected():
            # 选中线条本身：橙色高亮 + 加粗
            pen = QPen(COL_EDGE_SELECTED_HL)
            pen.setWidth(3)
            self.setPen(pen)
            return
        if self._node_link_highlighted:
            # 关联节点被选中：亮绿色高亮 + 加粗
            pen = QPen(COL_NODE_LINK_HL)
            pen.setWidth(3)
            self.setPen(pen)
            return
        # 普通状态：基础色（hover 时加亮加粗）
        # 临时连线默认 _base_color=COL_TEMP_WIRE（暗淡灰），悬停端口时由 mouseMoveEvent 改为绿/红
        color = self._base_color if not self._hover else self._base_color.lighter(130)
        width = 2 if not self._hover else 3
        pen = QPen(color)
        pen.setWidth(width)
        self.setPen(pen)

    def set_node_link_highlighted(self, on: bool):
        """关联节点被选中时的联动高亮（问题5）。"""
        on = bool(on)
        if self._node_link_highlighted != on:
            self._node_link_highlighted = on
            self._update_pen()

    def set_base_color(self, color: QColor):
        self._base_color = color
        self._update_pen()

    def set_from_port(self, p: PortItem):
        self._from_port = p
        if p.port.type == PortType.EXEC:
            self.set_base_color(COL_EXEC)
        else:
            self.set_base_color(color_for_data(p.port.data_type))
        self.update_path_from_ports()

    def set_to_port(self, p: PortItem):
        self._to_port = p
        if p.port.type == PortType.EXEC:
            self.set_base_color(COL_EXEC)
        else:
            self.set_base_color(color_for_data(p.port.data_type))
        self.update_path_from_ports()

    def from_port_item(self) -> Optional[PortItem]:
        return self._from_port

    def to_port_item(self) -> Optional[PortItem]:
        return self._to_port

    def update_path_from_ports(self):
        if self._from_port:
            self._from_pos = self._from_port.global_center()
        if self._to_port:
            self._to_pos = self._to_port.global_center()
        self._rebuild_bezier()

    def set_temp_to(self, pos: QPointF):
        self._to_pos = pos
        self._rebuild_bezier()

    def _rebuild_bezier(self):
        """统一用贝塞尔曲线绘制（EXEC/DATA 都用曲线）。
        曲线控制点在水平方向偏移，避免多条线重叠成一根线（问题4）。
        曲线绘制：从端口水平出发，到目标端口水平进入。"""
        p1 = self._from_pos
        p2 = self._to_pos
        # 控制点偏移量：水平方向，至少 40px 保证曲线有明显弧度
        # 距离越远偏移越大，但不超过 200px（避免曲线过于夸张）
        dx = max(40, min(200, abs(p2.x() - p1.x()) * 0.5))
        path = QPainterPath(p1)
        # 全部用三次贝塞尔曲线（cubicTo），不再区分 EXEC/DATA
        # 两条控制点都在水平方向：从 p1 水平向右/左延伸 dx，从 p2 水平向左/右延伸 dx
        # 这样曲线从端口水平出发，到目标端口水平进入，符合节点连线视觉习惯
        path.cubicTo(
            QPointF(p1.x() + dx, p1.y()),
            QPointF(p2.x() - dx, p2.y()),
            p2,
        )
        self.setPath(path)
        # 触发 boundingRect 重算（shape() 命中区依赖 path）
        self.prepareGeometryChange()

    def shape(self) -> QPainterPath:
        """重写：命中检测区域使用 8px 宽的描边路径，方便用户点中连线选中/删除。
        8px 是平衡值：太宽会盖住端口附近点击；太窄用户点不中。"""
        stroker = QPainterPathStroker()
        stroker.setWidth(8)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        """重写：boundingRect 也要足够大以容纳 8px 宽的命中区域。"""
        pad = 8
        base = self.path().boundingRect()
        return base.adjusted(-pad, -pad, pad, pad)

    def hoverEnterEvent(self, event):
        self._hover = True
        self._update_pen()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self._update_pen()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        # 用 ItemSelectedHasChanged 而非 ItemSelectedChange：
        # 后者在选中状态"即将改变"时触发，此时 isSelected() 还返回旧值，
        # 导致 _update_pen 里 self.isSelected() 取到旧状态，颜色不立即切换
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._update_pen()
        return super().itemChange(change, value)
