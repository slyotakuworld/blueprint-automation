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

    port_pressed = Signal(object)
    port_released = Signal(object)

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
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            self.port_released.emit(self)
            event.accept()
            return
        super().mouseReleaseEvent(event)


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
        title = meta.title if meta else self.node.type

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
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._base_color = COL_ANY
        self._hover = False
        self._update_pen()

    def _update_pen(self):
        if self.isSelected():
            color = COL_SELECTED
        else:
            color = self._base_color if not self._hover else self._base_color.lighter(130)
        pen = QPen(color)
        pen.setWidth(2 if not self._hover else 3)
        self.setPen(pen)

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
        p1 = self._from_pos
        p2 = self._to_pos
        dx = max(40, abs(p2.x() - p1.x()) * 0.5)
        path = QPainterPath(p1)
        if self._from_port and self._from_port.is_exec():
            # 直角折线（更像控制流）
            mid_x = (p1.x() + p2.x()) / 2
            path.lineTo(QPointF(mid_x, p1.y()))
            path.lineTo(QPointF(mid_x, p2.y()))
            path.lineTo(p2)
        else:
            path.cubicTo(
                QPointF(p1.x() + dx, p1.y()),
                QPointF(p2.x() - dx, p2.y()),
                p2,
            )
        self.setPath(path)

    def hoverEnterEvent(self, event):
        self._hover = True
        self._update_pen()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self._update_pen()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedChange:
            self._update_pen()
        return super().itemChange(change, value)
