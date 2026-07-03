import uuid
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QMimeData
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QPainter,
    QPen,
    QColorConstants,
)
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QGraphicsItem,
    QWidget,
)

from ..core.models import BlueprintGraph, Edge, Node, PortDirection
from ..core.node_library import create_node, NODE_LIBRARY
from .graphics_items import EdgeItem, NodeItem, PortItem


GRID_SIZE = 20


class NodeScene(QGraphicsScene):
    """节点画布：保存 NodeItem/EdgeItem，维护 BlueprintGraph 数据模型与 UI 视图的双向同步。"""

    graph_changed = Signal()
    node_selected = Signal(str)
    edge_created = Signal(str)
    edge_removed = Signal(str)
    node_created = Signal(str)
    node_removed = Signal(str)
    node_will_remove = Signal(str, dict, list)  # node_id, node_snapshot, related_edge_snapshots
    edge_will_remove = Signal(str, dict)  # edge_id, edge_snapshot
    nodes_dragged = Signal(list)  # list of (node_id, old_pos_tuple, new_pos_tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(QRectF(-5000, -5000, 10000, 10000))
        self.graph: Optional[BlueprintGraph] = None
        self._node_items: Dict[str, NodeItem] = {}
        self._edge_items: Dict[str, EdgeItem] = {}
        self._connecting_edge: Optional[EdgeItem] = None
        self._connecting_from: Optional[PortItem] = None
        self._wire_invalid = False
        self._drag_start_positions: Optional[Dict[str, tuple]] = None

        self.setBackgroundBrush(QBrush(QColor("#1C2128")))
        self.selectionChanged.connect(self._on_selection_changed)

    # ---------------- 背景网格 ----------------
    def drawBackground(self, painter: QPainter, rect: QRectF):
        painter.fillRect(rect, QColor("#1C2128"))
        pen = QPen(QColor("#2D333B"))
        pen.setWidth(1)
        painter.setPen(pen)
        g = GRID_SIZE
        left = int(rect.left() - (rect.left() % g)) - g
        right = int(rect.right()) + g
        top = int(rect.top() - (rect.top() % g)) - g
        bottom = int(rect.bottom()) + g
        for x in range(left, right, g):
            painter.drawLine(x, top, x, bottom)
        for y in range(top, bottom, g):
            painter.drawLine(left, y, right, y)

    # ---------------- 蓝图绑定 ----------------
    def set_graph(self, graph: BlueprintGraph):
        self.clear_all()
        self.graph = graph
        for n in graph.nodes.values():
            self._add_node_item(n, emit=False)
        for e in graph.edges.values():
            self._add_edge_item(e, emit=False)
        self.graph_changed.emit()

    def clear_all(self):
        for ei in list(self._edge_items.values()):
            self.removeItem(ei)
        for ni in list(self._node_items.values()):
            self.removeItem(ni)
        self._node_items.clear()
        self._edge_items.clear()
        self.graph = None

    def get_graph(self) -> Optional[BlueprintGraph]:
        return self.graph

    # ---------------- 查询 ----------------
    def node_item(self, node_id: str) -> Optional[NodeItem]:
        return self._node_items.get(node_id)

    def edge_item(self, edge_id: str) -> Optional[EdgeItem]:
        return self._edge_items.get(edge_id)

    def selected_node_ids(self) -> List[str]:
        return [
            n.node.id for n in self.selectedItems()
            if isinstance(n, NodeItem) and n.node.id in self._node_items
        ]

    # ---------------- 快照辅助 ----------------
    def snapshot_node(self, node_id: str) -> Optional[dict]:
        if self.graph is None or node_id not in self.graph.nodes:
            return None
        n = self.graph.nodes[node_id]
        return {
            "id": n.id,
            "type": n.type,
            "pos": tuple(n.pos),
            "params": dict(n.params),
            "inputs": [p.to_dict() for p in n.inputs],
            "outputs": [p.to_dict() for p in n.outputs],
        }

    def snapshot_edge(self, edge_id: str) -> Optional[dict]:
        if self.graph is None or edge_id not in self.graph.edges:
            return None
        e = self.graph.edges[edge_id]
        return {
            "id": e.id,
            "from_node": e.from_node,
            "from_port": e.from_port,
            "to_node": e.to_node,
            "to_port": e.to_port,
        }

    # ---------------- 节点增删 ----------------
    def add_node_by_type(self, node_type: str, scene_pos: QPointF) -> Optional[Node]:
        if node_type not in NODE_LIBRARY:
            return None
        if self.graph is None:
            return None
        pos = (int(scene_pos.x()), int(scene_pos.y()))
        node = create_node(node_type, pos=pos)
        self.graph.nodes[node.id] = node
        self._add_node_item(node, emit=True)
        return node

    def _add_node_item(self, node: Node, emit: bool = True):
        ni = NodeItem(node)
        self.addItem(ni)
        self._node_items[node.id] = ni
        ni.node_position_changed.connect(lambda nid, p, s=self: s._on_node_moved(nid))
        ni.node_selected_changed.connect(self._on_node_sel)
        for pi in ni.all_port_items():
            pi.port_pressed.connect(self._on_port_pressed)
            pi.port_released.connect(self._on_port_released)
        if emit:
            self.node_created.emit(node.id)
            self.graph_changed.emit()

    def remove_node(self, node_id: str):
        if self.graph is None or node_id not in self.graph.nodes:
            return
        # 删除前：先给外部一个 will_remove 信号（带节点快照+关联边快照），方便撤销栈记录
        node_snap = self.snapshot_node(node_id)
        related_edges = [
            eid for eid, e in self.graph.edges.items()
            if e.from_node == node_id or e.to_node == node_id
        ]
        edge_snaps = [self.snapshot_edge(eid) for eid in related_edges if self.snapshot_edge(eid) is not None]
        if node_snap is not None:
            self.node_will_remove.emit(node_id, node_snap, edge_snaps)
        # 然后才是真正删除关联边+节点
        for eid in related_edges:
            self.remove_edge(eid, _emit_will=False)
        ni = self._node_items.pop(node_id, None)
        if ni:
            self.removeItem(ni)
        self.graph.nodes.pop(node_id, None)
        self.node_removed.emit(node_id)
        self.graph_changed.emit()

    def _on_node_moved(self, node_id: str):
        self.graph_changed.emit()

    def _on_node_sel(self, node_id: str, selected: bool):
        if selected:
            self.node_selected.emit(node_id)

    # ---------------- 连线增删 ----------------
    def connect_ports(self, from_pi: PortItem, to_pi: PortItem) -> Optional[Edge]:
        if self.graph is None:
            return None
        # 统一方向：OUT -> IN
        if from_pi.port.direction != PortDirection.OUT:
            from_pi, to_pi = to_pi, from_pi
        if not from_pi.compatible_with(to_pi):
            return None
        # 去重：同一对端口不要建两次
        fn, tn = from_pi.node_item().node.id, to_pi.node_item().node.id
        fp, tp = from_pi.port_name(), to_pi.port_name()
        for e in self.graph.edges.values():
            if e.from_node == fn and e.to_node == tn and e.from_port == fp and e.to_port == tp:
                return None
        edge = Edge(
            id=uuid.uuid4().hex[:12],
            from_node=fn, from_port=fp,
            to_node=tn, to_port=tp,
        )
        self.graph.edges[edge.id] = edge
        self._add_edge_item(edge, emit=True)
        return edge

    def _add_edge_item(self, edge: Edge, emit: bool = True):
        ei = EdgeItem(edge)
        from_ni = self._node_items.get(edge.from_node)
        to_ni = self._node_items.get(edge.to_node)
        if from_ni:
            pi = from_ni.port_item(edge.from_port)
            if pi:
                ei.set_from_port(pi)
        if to_ni:
            pi = to_ni.port_item(edge.to_port)
            if pi:
                ei.set_to_port(pi)
        self.addItem(ei)
        self._edge_items[edge.id] = ei
        if emit:
            self.edge_created.emit(edge.id)
            self.graph_changed.emit()

    def remove_edge(self, edge_id: str, _emit_will: bool = True):
        if self.graph is None or edge_id not in self.graph.edges:
            return
        if _emit_will:
            snap = self.snapshot_edge(edge_id)
            if snap is not None:
                self.edge_will_remove.emit(edge_id, snap)
        ei = self._edge_items.pop(edge_id, None)
        if ei:
            self.removeItem(ei)
        self.graph.edges.pop(edge_id, None)
        self.edge_removed.emit(edge_id)
        self.graph_changed.emit()

    def refresh_all_edges(self):
        for ei in self._edge_items.values():
            ei.update_path_from_ports()

    # ---------------- 端口连线交互 ----------------
    def _on_port_pressed(self, pi: PortItem):
        if self.graph is None:
            return
        temp = EdgeItem()
        # 如果按住的是输出端口，from就是它；否则from留空，等释放时再确定方向
        if pi.port.direction == PortDirection.OUT:
            temp.set_from_port(pi)
            temp.set_temp_to(pi.global_center())
        else:
            temp.set_to_port(pi)
            temp._from_pos = pi.global_center()
            temp._rebuild_bezier()
        self._connecting_from = pi
        self._connecting_edge = temp
        self.addItem(temp)

    def _on_port_released(self, pi: PortItem):
        temp = self._connecting_edge
        from_p = self._connecting_from
        self._connecting_edge = None
        self._connecting_from = None
        self._wire_invalid = False
        if temp:
            self.removeItem(temp)
        if not (temp and from_p and from_p != pi):
            return
        self.connect_ports(from_p, pi)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            items_under = self.items(event.scenePos())
            has_node = any(isinstance(it, NodeItem) for it in items_under)
            if has_node:
                snap = {}
                for ni in self._node_items.values():
                    if ni.isSelected() or (any(it is ni for it in items_under)):
                        p = ni.pos()
                        snap[ni.node.id] = (p.x(), p.y())
                if snap:
                    self._drag_start_positions = snap
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._connecting_edge is not None and self._connecting_from is not None:
            pos = event.scenePos()
            from_p = self._connecting_from
            # 检查悬停端口
            items = self.items(pos)
            target_ok = False
            hover = None
            for it in items:
                if isinstance(it, PortItem) and it is not from_p:
                    hover = it
                    if from_p.compatible_with(it):
                        target_ok = True
                    break
            # 设置颜色：兼容绿/不兼容红
            if hover is None:
                self._connecting_edge.set_base_color(QColor("#8B949E"))
            elif target_ok:
                self._connecting_edge.set_base_color(QColor("#3FB950"))
            else:
                self._connecting_edge.set_base_color(QColor("#F85149"))
                self._wire_invalid = True
            if from_p.port.direction == PortDirection.OUT:
                self._connecting_edge.set_temp_to(pos)
            else:
                self._connecting_edge._from_pos = pos
            self._connecting_edge._rebuild_bezier()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # 先让 Qt 完成默认的节点移动等行为
        super().mouseReleaseEvent(event)

        # 拖拽结束：对比快照里每个节点的 old/new pos
        if event.button() == Qt.LeftButton and self._drag_start_positions:
            changed = []
            for nid, old_xy in self._drag_start_positions.items():
                ni = self._node_items.get(nid)
                if ni is None:
                    continue
                p = ni.pos()
                new_xy = (p.x(), p.y())
                if new_xy[0] != old_xy[0] or new_xy[1] != old_xy[1]:
                    changed.append((nid, old_xy, new_xy))
            self._drag_start_positions = None
            if changed:
                self.nodes_dragged.emit(changed)
            event.accept()
            return

        if self._connecting_edge:
            pos = event.scenePos()
            has_port_under_cursor = any(
                isinstance(it, PortItem) for it in self.items(pos)
            )
            if has_port_under_cursor:
                super().mouseReleaseEvent(event)
                return
            temp = self._connecting_edge
            self._connecting_edge = None
            self._connecting_from = None
            self._wire_invalid = False
            if temp:
                self.removeItem(temp)
            event.accept()
            return

    # ---------------- 选择/键盘 ----------------
    def _on_selection_changed(self):
        ids = self.selected_node_ids()
        if ids:
            self.node_selected.emit(ids[0])

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            for it in list(self.selectedItems()):
                if isinstance(it, EdgeItem) and it.edge:
                    self.remove_edge(it.edge.id)
                elif isinstance(it, NodeItem):
                    self.remove_node(it.node.id)
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------------- 运行时高亮 ----------------
    def set_running_node(self, node_id: Optional[str]):
        for nid, ni in self._node_items.items():
            ni.set_running(nid == node_id)

    def clear_running(self):
        for ni in self._node_items.values():
            ni.set_running(False)

    # ---------------- 选中节点 ----------------
    def select_node_id(self, node_id: Optional[str]):
        try:
            self.blockSignals(True)
            self.clearSelection()
            if node_id and node_id in self._node_items:
                ni = self._node_items[node_id]
                ni.setSelected(True)
        finally:
            self.blockSignals(False)
        # 手动触发选中信号同步 Inspector
        if node_id:
            self._on_node_sel(node_id, True)


# ============================================================
# NodeView
# ============================================================
class NodeView(QGraphicsView):
    """QGraphicsView 封装：滚轮缩放 + 中键平移 + 拖入节点（MIME data = application/x-node-type）。"""

    NODE_MIME = "application/x-node-type"

    def __init__(self, scene: NodeScene, parent: Optional[QWidget] = None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self._panning = False
        self._last_pan = QPointF()
        self.setAcceptDrops(True)
        self.setBackgroundBrush(QBrush(QColor("#1C2128")))
        self.setFrameShape(QGraphicsView.NoFrame)

    # ---------------- 缩放 ----------------
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier or event.angleDelta().y():
            factor = 1.0
            d = event.angleDelta().y()
            if d > 0:
                factor = 1.15
            elif d < 0:
                factor = 1.0 / 1.15
            current = self.transform().m11()
            if factor > 1 and current > 3.0:
                return
            if factor < 1 and current < 0.2:
                return
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    # ---------------- 中键/右键平移 ----------------
    def mousePressEvent(self, event):
        if event.button() in (Qt.MiddleButton, Qt.RightButton):
            self._panning = True
            self._last_pan = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._last_pan
            self._last_pan = event.position()
            hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
            hb.setValue(int(hb.value() - delta.x()))
            vb.setValue(int(vb.value() - delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.MiddleButton, Qt.RightButton) and self._panning:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---------------- 拖入节点 ----------------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasFormat(self.NODE_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasFormat(self.NODE_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        md = event.mimeData()
        if md.hasFormat(self.NODE_MIME):
            node_type = bytes(md.data(self.NODE_MIME)).decode("utf-8")
            scene_pos = self.mapToScene(event.position().toPoint())
            scene = self.scene()
            if isinstance(scene, NodeScene):
                scene.add_node_by_type(node_type, scene_pos)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    # ---------------- 工具 ----------------
    def zoom_fit(self):
        scene = self.scene()
        if scene:
            rect = scene.sceneRect()
            items_rect = scene.itemsBoundingRect()
            if not items_rect.isEmpty():
                rect = items_rect.adjusted(-80, -80, 80, 80)
            self.fitInView(rect, Qt.KeepAspectRatio)

    def reset_zoom(self):
        self.resetTransform()
