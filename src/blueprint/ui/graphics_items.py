import math
import uuid
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QStyle,
    QStyleOptionGraphicsItem,
    QWidget,
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
# 尺寸常量
# ============================================================
NODE_WIDTH = 200
NODE_HEADER_H = 30
NODE_PORT_H = 22
NODE_ROUND = 8
PORT_RADIUS = 6
PORT_MARGIN_X = 8


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
        self._build_ports()
        x, y = int(node.pos[0]), int(node.pos[1])
        self.setPos(x, y)

    # ---------------- 结构 ----------------
    def _meta(self):
        return NODE_LIBRARY.get(self.node.type)

    def node_height(self) -> int:
        n_in = max(1, len(self.node.inputs))
        n_out = max(1, len(self.node.outputs))
        rows = max(n_in, n_out)
        return NODE_HEADER_H + rows * NODE_PORT_H + 10

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
        # 标题文本
        painter.setPen(QPen(QColor("#FFFFFF")))
        f = QFont()
        f.setBold(True)
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(
            QRectF(10, 0, NODE_WIDTH - 20, NODE_HEADER_H),
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
            y = NODE_HEADER_H + 5 + i * NODE_PORT_H
            label = p.label or p.name
            tx = PORT_MARGIN_X + PORT_RADIUS + 4
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2, NODE_WIDTH / 2, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )
        for i, p in enumerate(self.node.outputs):
            y = NODE_HEADER_H + 5 + i * NODE_PORT_H
            label = p.label or p.name
            tx = NODE_WIDTH / 2
            painter.drawText(
                QRectF(tx, y - NODE_PORT_H / 2,
                       NODE_WIDTH / 2 - PORT_MARGIN_X - PORT_RADIUS - 4, NODE_PORT_H),
                Qt.AlignVCenter | Qt.AlignRight,
                label,
            )

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
        self.prepareGeometryChange()
        self.update()

    # ---------------- 数据接口 ----------------
    def set_running(self, running: bool):
        self._running = bool(running)
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
        参考 Qflow 样式：从端口水平出发，到目标端口水平进入。"""
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
