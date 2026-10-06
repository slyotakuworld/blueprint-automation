import uuid
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QAction,
    QKeyEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QWidget,
    QLineEdit,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QCheckBox,
    QComboBox,
)

from ..core.models import BlueprintGraph, Edge, Node, PortDirection
from ..core.node_library import NODE_LIBRARY, NODE_TYPES_ORDERED
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
    node_double_clicked = Signal(str)  # node_id，双击节点（进入子图/循环体子画布）
    node_find_in_log = Signal(str)     # node_id，节点右键「在日志中查找」
    node_open_component = Signal(str)  # node_id，节点右键「打开引用的组件」
    asset_dropped = Signal(str, object)  # (蓝图绝对路径, 场景坐标 QPointF)，从资源面板拖入画布

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(QRectF(-5000, -5000, 10000, 10000))
        self.graph: Optional[BlueprintGraph] = None
        self._node_items: Dict[str, NodeItem] = {}
        self._edge_items: Dict[str, EdgeItem] = {}
        # ========== 连线交互状态机（方案A：阈值化启动） ==========
        # _pending_port: 左键按下端口但还没拖动超过阈值时的"待连线起点端口"
        # _pending_press_pos: 按下时的场景坐标，用于和当前鼠标位置计算拖动距离
        # _connecting_edge: 拖动超过阈值后创建的临时连线（None=未进入连线模式）
        # _connecting_from: 已进入连线模式时的起点端口（即 _pending_port 转正）
        self._pending_port: Optional[PortItem] = None
        self._pending_press_pos: Optional[QPointF] = None
        self._connecting_edge: Optional[EdgeItem] = None
        self._connecting_from: Optional[PortItem] = None
        # 拖动阈值（px）：超过这个距离才从"按下端口"切换为"开始连线"
        # 5px 是业界常用值（Unreal/Blender），既不敏感也不迟钝
        self._wire_drag_threshold = 5.0
        self._drag_start_positions: Optional[Dict[str, tuple]] = None

        self.setBackgroundBrush(QBrush(QColor("#1C2128")))
        self.selectionChanged.connect(self._on_selection_changed)

    # ---------------- 背景网格 ----------------
    def drawBackground(self, painter: QPainter, rect: QRectF):
        painter.fillRect(rect, QColor("#1C2128"))
        g = GRID_SIZE
        # 缩得过小时线间距会小于 ~6 设备像素：既白画成百上千条线拖慢帧率，又糊成一片摩尔纹。
        # painter 的 worldTransform 把场景坐标映射到设备像素，m11() 就是当前缩放倍数。
        try:
            if abs(painter.worldTransform().m11()) * g < 6.0:
                return  # 只铺底色，不画网格（多数节点编辑器在缩小到一定程度后同样隐藏网格）
        except Exception:
            pass
        pen = QPen(QColor("#2D333B"))
        pen.setWidth(1)
        painter.setPen(pen)
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
        # ★ 不再 graph_changed.emit()：绑定蓝图 ≠ 用户修改了内容，
        #   之前发射会让 _update_title 在 self._graph 还是旧对象时就提前跑，误判 dirty。

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
        # 分配自增数字id（1,2,3…），每张蓝图独立计数
        from ..core.graph_store import BlueprintStore
        node = BlueprintStore.add_node(self.graph, node_type, pos=pos)
        self._add_node_item(node, emit=True)
        return node

    def _add_node_item(self, node: Node, emit: bool = True):
        ni = NodeItem(node)
        self.addItem(ni)
        self._node_items[node.id] = ni
        ni.node_position_changed.connect(lambda nid, p, s=self: s._on_node_moved(nid))
        ni.node_selected_changed.connect(self._on_node_sel)
        ni.node_double_clicked.connect(self.node_double_clicked.emit)
        ni.request_find_in_log.connect(self.node_find_in_log.emit)
        ni.request_open_component.connect(self.node_open_component.emit)
        # 给所有 PortItem 连接交互信号（端口拖动连线 + 右键菜单）
        self._connect_port_signals_for_node(node.id)
        if emit:
            self.node_created.emit(node.id)
            self.graph_changed.emit()

    def _connect_port_signals_for_node(self, node_id: str):
        """给指定节点当前所有的 PortItem 连接交互信号。
        注意：每次 sync_from_node 重建端口后，必须重新调用这个函数，否则新创建的 PortItem 是没有交互能力的，拖不出线、右键不出菜单、也进不了落点命中。"""
        ni = self._node_items.get(node_id)
        if ni is None:
            return
        for pi in ni.all_port_items():
            try:
                pi.port_pressed.connect(self._on_port_pressed)
            except Exception:
                pass
            try:
                pi.port_right_clicked.connect(self._on_port_right_clicked)
            except Exception:
                pass

    # ---------------- 右键端口：QMenu 上下文菜单（方案C） ----------------
    def disconnect_all_from_port(self, pi: PortItem) -> int:
        """解除指定端口关联的所有连线，返回被删除的连线数量。"""
        if self.graph is None:
            return 0
        nid = pi.node_item().node.id
        pname = pi.port_name()
        removed = 0
        # 要避免在遍历 dict 时删除它，先收集再删
        target_edge_ids = []
        for eid, e in self.graph.edges.items():
            # 输入端口：作为 to_port 出现
            # 输出端口：作为 from_port 出现
            if (e.from_node == nid and e.from_port == pname) or \
               (e.to_node == nid and e.to_port == pname):
                target_edge_ids.append(eid)
        for eid in target_edge_ids:
            self.remove_edge(eid)
            removed += 1
        return removed

    def _count_edges_of_port(self, pi: PortItem) -> int:
        """统计指定端口关联的连线数量。"""
        if self.graph is None:
            return 0
        nid = pi.node_item().node.id
        pname = pi.port_name()
        count = 0
        for e in self.graph.edges.values():
            if (e.from_node == nid and e.from_port == pname) or \
               (e.to_node == nid and e.to_port == pname):
                count += 1
        return count

    def _on_port_right_clicked(self, pi: PortItem):
        """右键端口：弹 QMenu 上下文菜单（方案C），用户点菜单项才执行解除。
        符合所有桌面软件的右键习惯，不再用模态 QMessageBox 阻塞主线程。"""
        if self.graph is None:
            return
        nid = pi.node_item().node.id
        pname = pi.port_name()
        count = self._count_edges_of_port(pi)
        # 构建右键菜单
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background: #21262D; color: #C9D1D9; border: 1px solid #30363D; padding: 4px; }"
            "QMenu::item { padding: 6px 18px; border-radius: 3px; }"
            "QMenu::item:selected { background: #1F6FEB; color: #FFFFFF; }"
            "QMenu::item:disabled { color: #484F58; }"
        )
        # 主菜单项：解除所有连线
        act_disconnect = QAction(
            f"解除所有连线 ({count} 条)" if count > 0 else "解除所有连线（无连线）",
            menu,
        )
        act_disconnect.setEnabled(count > 0)
        act_disconnect.triggered.connect(lambda _checked=False, p=pi: self._do_disconnect_all_from_port(p))
        menu.addAction(act_disconnect)
        # 在鼠标位置弹出（mapToGlobal 需要 view 的坐标转换）
        views = self.views()
        if views:
            view = views[0]
            # 用上次鼠标位置（QCursor.pos() 全局坐标）
            from PySide6.QtGui import QCursor
            menu.exec(QCursor.pos())
        else:
            menu.exec()

    def _do_disconnect_all_from_port(self, pi: PortItem):
        """实际执行解除连线（由 QMenu 菜单项触发）。"""
        nid = pi.node_item().node.id
        pname = pi.port_name()
        removed = self.disconnect_all_from_port(pi)
        if removed:
            # 不再弹模态提示框，状态由 scene.graph_changed 信号驱动主窗口状态栏更新即可
            # 这里仅打印到 stdout（可选，便于调试）
            try:
                print(f"[Edge] 已解除端口 {nid}.{pname} 的 {removed} 条连线（Ctrl+Z 可撤销）")
            except Exception:
                pass

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
        """节点移动后：更新所有关联的边（让连线跟随节点移动）+ 发 graph_changed 信号。"""
        # 关键：节点移动后必须刷新关联边的路径，否则线条还停留在原位置
        for ei in self._edge_items.values():
            # 只更新与该节点关联的边（性能优化：避免全量刷新）
            if ei.edge and (ei.edge.from_node == node_id or ei.edge.to_node == node_id):
                ei.update_path_from_ports()
        self.graph_changed.emit()

    def _on_node_sel(self, node_id: str, selected: bool):
        if selected:
            self.node_selected.emit(node_id)

    # ---------------- 连线增删 ----------------
    def _is_input_port_occupied(self, to_node_id: str, to_port_name: str) -> bool:
        """检查某个节点的 INPUT 端口（按名）是否已经被占用（即已连过一根线进来）。
        所有 INPUT 端口默认只允许一根输入线。"""
        if self.graph is None:
            return False
        for e in self.graph.edges.values():
            if e.to_node == to_node_id and e.to_port == to_port_name:
                return True
        return False

    def connect_ports(self, from_pi: PortItem, to_pi: PortItem) -> Optional[Edge]:
        if self.graph is None:
            return None
        # 统一方向：OUT -> IN
        if from_pi.port.direction != PortDirection.OUT:
            from_pi, to_pi = to_pi, from_pi
        if not from_pi.compatible_with(to_pi):
            return None
        fn, tn = from_pi.node_item().node.id, to_pi.node_item().node.id
        fp, tp = from_pi.port_name(), to_pi.port_name()
        # 去重：同一对端口不要建两次
        for e in self.graph.edges.values():
            if e.from_node == fn and e.to_node == tn and e.from_port == fp and e.to_port == tp:
                return None
        # 关键约束：DATA 类型的 INPUT 端口只能连一根线；EXEC 类型允许多根（如循环节点回环）
        if to_pi.port.direction == PortDirection.IN and not to_pi.is_exec() and self._is_input_port_occupied(tn, tp):
            # 如果用户就是要接这个已占用 input，可以让它先自动断开旧的再连新的？这里按用户要求不允许，直接拒绝。
            # 底部日志面板提示一下用户
            try:
                from_node_title = getattr(from_pi.node_item(), "_title_label", None)
                from_title = from_node_title.text() if from_node_title else from_pi.node_item().node.type
                to_node_title = getattr(to_pi.node_item(), "_title_label", None)
                to_title = to_node_title.text() if to_node_title else to_pi.node_item().node.type
                msg = f"[连线被拒] 端口「{to_title}.{tp}」已连过一根输入线，每个INPUT端口只能接一根上游线。请先断开旧线，或改连其他空输入端口。"
                print(msg)
            except Exception:
                pass
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
        # 连线会改变两端节点的画布摘要（如「文本内容」为空时改为提示「← 端口传入」），
        # 这里显式刷一次两端节点，否则新线的效果要等下次几何变化才画出来。
        for ni in (from_ni, to_ni):
            if ni is not None:
                try:
                    ni.update()
                except Exception:
                    pass
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
        removed_edge = self.graph.edges.pop(edge_id, None)
        # 断线同样会改变两端节点的画布摘要（「← 端口传入」要变回「(空)」）
        if removed_edge is not None:
            for nid in (removed_edge.from_node, removed_edge.to_node):
                ni = self._node_items.get(nid)
                if ni is not None:
                    try:
                        ni.update()
                    except Exception:
                        pass
        self.edge_removed.emit(edge_id)
        self.graph_changed.emit()

    def reconnect_node_edges(self, node_id: str) -> int:
        """当某节点的 PortItem 被全量重建后（例如 sync_from_node），
        ①把与该节点关联的所有 EdgeItem 重新挂到新的同名 PortItem 上并刷新路径；
        ②重新给新建的 PortItem 连接交互信号（拖线/右键菜单）。
        返回被成功重连的边数。"""
        if self.graph is None or node_id not in self._node_items:
            return 0
        ni = self._node_items[node_id]
        # 先连信号：新 PortItem 如果没连信号，哪怕线条重连对了也拖不出线、右键没菜单
        self._connect_port_signals_for_node(node_id)
        count = 0
        for ei in self._edge_items.values():
            if ei.edge is None:
                continue
            e = ei.edge
            need_refresh = False
            if e.from_node == node_id:
                pi = ni.port_item(e.from_port)
                if pi is not None:
                    ei.set_from_port(pi)
                    need_refresh = True
            if e.to_node == node_id:
                pi = ni.port_item(e.to_port)
                if pi is not None:
                    ei.set_to_port(pi)
                    need_refresh = True
            if need_refresh:
                ei.update_path_from_ports()
                count += 1
        # 触发节点/场景刷新，保证线条位置立即更新
        ni.update()
        self.invalidate(self.sceneRect(), QGraphicsScene.ItemLayer | QGraphicsScene.ForegroundLayer)
        return count

    def refresh_all_edges(self):
        # 兜底：refresh 前先重连所有节点，避免某些节点重建端口后 EdgeItem 仍挂在旧 PortItem 上
        if self.graph is not None:
            for nid in list(self._node_items.keys()):
                self.reconnect_node_edges(nid)
        for ei in self._edge_items.values():
            ei.update_path_from_ports()

    # ---------------- 端口连线交互（方案A：阈值化启动 + 方案B：统一临时连线方向） ----------------
    def _on_port_pressed(self, pi: PortItem):
        """左键按下端口：只记录"待连线起点"状态，不立即创建临时连线。
        真正的临时连线要等 mouseMoveEvent 中拖动距离超过 _wire_drag_threshold 才创建。
        这样按下端口不拖动直接释放 = 什么都不做（符合 Unreal/Blender 习惯）。"""
        if self.graph is None:
            return
        # 清理可能残留的上一轮状态（防御性）
        self._cancel_pending_wire()
        self._pending_port = pi
        self._pending_press_pos = QPointF(pi.global_center())

    def _start_wire_drag(self, from_pi: PortItem):
        """拖动超过阈值后真正进入连线模式：创建临时 EdgeItem。
        方案B：无论 from_pi 是输入还是输出端口，临时连线都从 from_pi 出发画到鼠标位置。
        - from_pi 是输出端口(OUT)：temp.from_port = from_pi，temp.to_pos 跟随鼠标
        - from_pi 是输入端口(IN)：temp.to_port = from_pi，temp.from_pos 跟随鼠标
          视觉上都是从 from_pi 出发画到鼠标（因为 from_pos=鼠标位置，to_pos=from_pi 位置）。
        """
        temp = EdgeItem()
        if from_pi.port.direction == PortDirection.OUT:
            # 输出端口：from_port 固定为 from_pi，to_pos 跟随鼠标
            temp.set_from_port(from_pi)
            temp.set_temp_to(from_pi.global_center())
        else:
            # 输入端口：to_port 固定为 from_pi，from_pos 跟随鼠标
            temp.set_to_port(from_pi)
            temp._from_pos = from_pi.global_center()
            temp._rebuild_bezier()
        # 临时连线默认暗淡灰（连线中状态），悬停端口时由 mouseMoveEvent 改为绿/红反馈
        from .graphics_items import COL_TEMP_WIRE
        temp.set_base_color(COL_TEMP_WIRE)
        self._connecting_from = from_pi
        self._connecting_edge = temp
        self.addItem(temp)

    def _find_port_at(self, scene_pos: QPointF, ignore_pi: Optional[PortItem] = None) -> Optional[PortItem]:
        """查找 scene 坐标下的端口图元（忽略指定的端口，避免自己连自己）。"""
        for it in self.items(scene_pos):
            if isinstance(it, PortItem) and it is not ignore_pi:
                return it
        return None

    def _cancel_pending_wire(self):
        """取消"待连线"状态：清理 pending 状态 + 临时连线（如果有）。"""
        self._pending_port = None
        self._pending_press_pos = None
        if self._connecting_edge is not None:
            try:
                self.removeItem(self._connecting_edge)
            except Exception:
                pass
            self._connecting_edge = None
        self._connecting_from = None

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            items_under = self.items(event.scenePos())
            # 方案F：只有"光标下有 NodeItem 但没有 PortItem"时才记录节点拖动快照。
            # 否则按下端口会同时触发节点拖动快照 + 连线 pending 状态，逻辑路径很脏。
            has_node = any(isinstance(it, NodeItem) for it in items_under)
            has_port = any(isinstance(it, PortItem) for it in items_under)
            if has_node and not has_port:
                snap = {}
                for ni in self._node_items.values():
                    if ni.isSelected() or (any(it is ni for it in items_under)):
                        p = ni.pos()
                        snap[ni.node.id] = (p.x(), p.y())
                if snap:
                    self._drag_start_positions = snap
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # ========== 方案A：阈值化启动连线 ==========
        # 按下了端口但还没进入连线模式 → 检查拖动距离是否超过阈值
        if (self._pending_port is not None and
                self._connecting_edge is None and
                self._pending_press_pos is not None):
            cur_pos = event.scenePos()
            dx = cur_pos.x() - self._pending_press_pos.x()
            dy = cur_pos.y() - self._pending_press_pos.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist >= self._wire_drag_threshold:
                # 超过阈值 → 真正进入连线模式
                self._start_wire_drag(self._pending_port)
                # 注意：不 return，继续走下面的"已进入连线模式"分支更新临时线位置

        # ========== 已进入连线模式：更新临时连线终点 + 颜色（方案B） ==========
        if self._connecting_edge is not None and self._connecting_from is not None:
            pos = event.scenePos()
            from_p = self._connecting_from
            # 检查悬停端口
            target_pi = self._find_port_at(pos, ignore_pi=from_p)
            target_ok = False
            target_hint = ""
            if target_pi is not None and from_p.compatible_with(target_pi):
                # 先统一成 OUT->IN 方向，判断目标是否是 INPUT 端口
                a, b = from_p, target_pi
                if a.port.direction != PortDirection.OUT:
                    a, b = b, a
                # 关键：如果最终要接到的那个 INPUT 端口已经被占了，也判定为不兼容
                # 但 EXEC 类型 INPUT 允许多根连线（如循环节点回环、多上游触发同一节点）
                if b.port.direction == PortDirection.IN and not b.is_exec():
                    occupied = self._is_input_port_occupied(b.node_item().node.id, b.port_name())
                    if occupied:
                        target_ok = False
                        target_hint = "（该数据输入端口已被占用，请先断开旧线）"
                    else:
                        target_ok = True
                else:
                    target_ok = True
            # 设置颜色：兼容绿/不兼容红/暗淡灰（连线中默认暗淡）
            if target_pi is None:
                self._connecting_edge.set_base_color(QColor("#6E7681"))
            elif target_ok:
                self._connecting_edge.set_base_color(QColor("#3FB950"))
            else:
                self._connecting_edge.set_base_color(QColor("#F85149"))
            # 方案B：无论 from_p 是输入还是输出端口，临时连线都从 from_p 出发画到鼠标位置
            if from_p.port.direction == PortDirection.OUT:
                # 输出端口：from_port 固定，to_pos 跟随鼠标
                self._connecting_edge.set_temp_to(pos)
            else:
                # 输入端口：to_port 固定为 from_p，from_pos 跟随鼠标
                # 视觉效果：从鼠标位置画到输入端口（=从输入端口出发画到鼠标的反向，
                #           但用户体感是"线从我按下的端口出发，跟着鼠标走"）
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

        # ========== 连线释放判定（统一入口） ==========
        if event.button() == Qt.LeftButton and self._pending_port is not None:
            pos = event.scenePos()
            from_p = self._pending_port
            # 查找光标下的目标端口
            target_pi = self._find_port_at(pos, ignore_pi=from_p)
            # 清理临时连线状态（无论成功与否都要清）
            self._cancel_pending_wire()
            # 方案A关键：如果按下后没拖动超过阈值（_connecting_edge 始终为 None），
            # 且释放时不在别的端口上 → 什么都不做（避免误触创建连线）
            # 如果拖动了且在目标端口上 → 创建连线
            if from_p is not None and target_pi is not None:
                self.connect_ports(from_p, target_pi)
            event.accept()
            return

    # ---------------- 选择/键盘 ----------------
    def _on_selection_changed(self):
        """选择变化时联动高亮（问题3+问题5）：
        - 选中 EdgeItem → 高亮两端 NodeItem（蓝色虚线边框）
        - 选中 NodeItem → 高亮关联的所有 EdgeItem（加粗加亮）
        - 清空选择 → 清除所有联动高亮
        """
        sel_items = self.selectedItems()
        sel_edges = [it for it in sel_items if isinstance(it, EdgeItem)]
        sel_nodes = [it for it in sel_items if isinstance(it, NodeItem)]

        # ===== 问题3：选中连线 → 高亮两端节点 =====
        # 先清空所有节点的边关联高亮
        for ni in self._node_items.values():
            ni.set_edge_link_highlighted(False)
        # 如果选中了边，高亮其两端节点
        for ei in sel_edges:
            if ei.edge:
                from_ni = self._node_items.get(ei.edge.from_node)
                to_ni = self._node_items.get(ei.edge.to_node)
                if from_ni:
                    from_ni.set_edge_link_highlighted(True)
                if to_ni:
                    to_ni.set_edge_link_highlighted(True)

        # ===== 问题5：选中节点 → 高亮关联边 =====
        # 先清空所有边的节点关联高亮
        for ei in self._edge_items.values():
            ei.set_node_link_highlighted(False)
        # 如果选中了节点，高亮其关联的所有边
        if sel_nodes:
            selected_node_ids = {ni.node.id for ni in sel_nodes}
            for ei in self._edge_items.values():
                if ei.edge and (ei.edge.from_node in selected_node_ids or
                                ei.edge.to_node in selected_node_ids):
                    ei.set_node_link_highlighted(True)

        # 原有逻辑：发节点选中信号
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
    ASSET_MIME = "application/x-bp-asset"   # 从资源面板拖入的蓝图绝对路径（生成/填充「调用组件」节点）

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

        # 画布节点搜索框（画布最上方悬浮条，不遮挡中间画布操作区域）——阶段3-⑤
        self._search_le: Optional[QLineEdit] = None
        self._search_count_lbl: Optional[QLabel] = None
        self._search_results: List[str] = []
        self._search_idx: int = -1
        self._top_bar: Optional[QWidget] = None   # 搜索条容器（贴顶固定，随窗口resize自动对齐）
        self._build_search_bar()

    # ---------------- 节点搜索框（阶段3-⑤） ----------------
    def _build_search_bar(self):
        # 独立 Widget 挂在 NodeView 自身上（不走scene/viewport），固定在最上方不随缩放/平移移动
        self._top_bar = QWidget(self)
        self._top_bar.setObjectName("searchTopBar")
        self._top_bar.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._top_bar.setStyleSheet("""
            QWidget#searchTopBar {
                background: rgba(28, 33, 40, 215);
                border: 1px solid #30363D;
                border-radius: 8px;
            }
        """)

        outer = QVBoxLayout(self._top_bar)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(0)
        bar = QHBoxLayout()
        bar.setSpacing(6)

        lbl = QLabel("🔍 节点:")
        lbl.setStyleSheet("color:#8b949e;")
        bar.addWidget(lbl)

        self._search_le = QLineEdit()
        self._search_le.setPlaceholderText(
            "搜索 (勾选下方分类)；Enter跳转下一个；Esc清空；Ctrl+F聚焦"
        )
        self._search_le.setStyleSheet("""
            QLineEdit {
                background:#2D333B; color:#C9D1D9; border:1px solid #444C56;
                border-radius:6px; padding:4px 8px; selection-background-color:#1f6feb;
            }
            QLineEdit:focus { border:1px solid #ff9800; }
        """)
        self._search_le.textChanged.connect(self._on_search_changed)
        self._search_le.returnPressed.connect(self._on_search_next)
        bar.addWidget(self._search_le, 1)

        # 分类搜索控件：
        # - ID 勾选框：勾选后按节点 ID 子串匹配
        # - Title 勾选框：勾选后按节点标题子串匹配
        # - Type 下拉框：选择具体节点类型过滤（"全部"=不过滤）
        cb_style = (
            "QCheckBox { color:#8b949e; spacing:4px; padding:2px 6px;"
            "  background:#21262D; border:1px solid #30363D; border-radius:4px; }"
            "QCheckBox:checked { color:#58A6FF; border:1px solid #1F6FEB; }"
            "QCheckBox::indicator { width:12px; height:12px; }"
        )
        self._cb_search_id = QCheckBox("ID")
        self._cb_search_id.setChecked(True)
        self._cb_search_id.setStyleSheet(cb_style)
        self._cb_search_id.toggled.connect(self._on_search_changed)
        bar.addWidget(self._cb_search_id)

        self._cb_search_title = QCheckBox("Title")
        self._cb_search_title.setChecked(True)
        self._cb_search_title.setStyleSheet(cb_style)
        self._cb_search_title.toggled.connect(self._on_search_changed)
        bar.addWidget(self._cb_search_title)

        # Type 下拉框：包含"全部" + 所有节点库类型
        combo_style = (
            "QComboBox { color:#8b949e; padding:2px 8px;"
            "  background:#21262D; border:1px solid #30363D; border-radius:4px;"
            "  min-width:70px; }"
            "QComboBox:hover { border:1px solid #1F6FEB; }"
            "QComboBox::drop-down { border:none; width:16px; }"
            "QComboBox QAbstractItemView { background:#21262D; color:#C9D1D9;"
            "  selection-background-color:#1F6FEB; border:1px solid #30363D; }"
        )
        self._cb_search_type = QComboBox()
        self._cb_search_type.setStyleSheet(combo_style)
        self._cb_search_type.setToolTip("按节点类型过滤（全部=不过滤，顺序与左侧节点库一致）")
        self._cb_search_type.addItem("全部类型")
        for ntype in NODE_TYPES_ORDERED:
            meta = NODE_LIBRARY.get(ntype)
            if not meta:
                continue
            self._cb_search_type.addItem(meta.title, ntype)
        self._cb_search_type.currentIndexChanged.connect(self._on_search_changed)
        bar.addWidget(self._cb_search_type)

        prev_btn = QToolButton()
        prev_btn.setText("▲")
        prev_btn.setToolTip("上一个匹配")
        prev_btn.setStyleSheet(
            "QToolButton{background:#30363D;color:#C9D1D9;border:1px solid #444C56;"
            "border-radius:4px;padding:3px 8px;}QToolButton:hover{background:#3A3F4B;}"
        )
        prev_btn.clicked.connect(self._on_search_prev)
        bar.addWidget(prev_btn)

        next_btn = QToolButton()
        next_btn.setText("▼")
        next_btn.setToolTip("下一个匹配")
        next_btn.setStyleSheet(
            "QToolButton{background:#30363D;color:#C9D1D9;border:1px solid #444C56;"
            "border-radius:4px;padding:3px 8px;}QToolButton:hover{background:#3A3F4B;}"
        )
        next_btn.clicked.connect(self._on_search_next)
        bar.addWidget(next_btn)

        clear_btn = QToolButton()
        clear_btn.setText("✕")
        clear_btn.setToolTip("清空并取消高亮")
        clear_btn.setStyleSheet(
            "QToolButton{background:#30363D;color:#C9D1D9;border:1px solid #444C56;"
            "border-radius:4px;padding:3px 8px;}QToolButton:hover{background:#f44336;color:#fff;}"
        )
        clear_btn.clicked.connect(self._clear_search)
        bar.addWidget(clear_btn)

        self._search_count_lbl = QLabel("0")
        self._search_count_lbl.setStyleSheet(
            "color:#8b949e;background:#21262D;padding:2px 8px;border-radius:4px;min-width:36px;"
            "qproperty-alignment:AlignCenter;"
        )
        bar.addWidget(self._search_count_lbl)

        outer.addLayout(bar)
        self._top_bar.show()
        # 初始化计算一次位置（后续靠 resizeEvent 自动保持）
        self._position_top_bar()

    def _position_top_bar(self):
        """把搜索条固定在画布最上方（QGraphicsView可视区域顶部，不随缩放/平移移动，水平居中但留边）。"""
        if self._top_bar is None:
            return
        vw = max(320, int(self.width()))
        w = min(vw - 16, 920)           # 宽度不超过920（加了勾选框需要更宽），窄屏自适应
        x = 8 + max(0, (vw - 16 - w) // 2)
        y = 8
        h = 48                          # 高度从44→48，容纳勾选框
        self._top_bar.setGeometry(x, y, w, h)
        self._top_bar.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_top_bar()

    def _iter_node_items(self) -> List[NodeItem]:
        sc = self.scene()
        if isinstance(sc, NodeScene):
            return list(sc._node_items.values())
        return []

    def _node_match(self, ni: NodeItem, kw: str) -> bool:
        """根据搜索条件匹配节点：
        - Type 下拉框选了具体类型 → 先按类型过滤
        - ID/Title 勾选框 → 决定按哪些字段做子串匹配
        如果关键词为空但选了具体类型，也返回 True（纯类型过滤）。"""
        n = ni.node
        # Type 下拉框过滤（index 0 = "全部类型"，>0 = 具体类型）
        type_idx = self._cb_search_type.currentIndex() if self._cb_search_type else 0
        if type_idx > 0:
            selected_type = self._cb_search_type.itemData(type_idx)
            if n.type != selected_type:
                return False
            # 选了具体类型但关键词为空 → 纯类型过滤，直接匹配
            if not kw:
                return True
        # 读取 ID/Title 勾选状态
        check_id = self._cb_search_id.isChecked() if self._cb_search_id else True
        check_title = self._cb_search_title.isChecked() if self._cb_search_title else True
        # 没有关键词且没选类型 → 不匹配
        if not kw:
            return False
        # ID/Title 都没勾 → 不匹配
        if not (check_id or check_title):
            return False
        kw_lower = kw.lower()
        if check_id and n.id and kw_lower in n.id.lower():
            return True
        if check_title:
            meta = ni._meta()
            if meta and meta.title and kw_lower in meta.title.lower():
                return True
        return False

    def _on_search_changed(self, *args):
        """搜索框文本变化或勾选框/下拉框状态变化时触发。
        实时高亮匹配节点 + 更新计数，但不跳转画布（按回车或前进/后退按钮才跳转）。
        *args 兼容 textChanged(str)、toggled(bool)、currentIndexChanged(int) 三种信号。"""
        kw = (self._search_le.text() if self._search_le else "").strip()
        # 判断是否有搜索条件：有关键词 或 选了具体类型
        type_idx = self._cb_search_type.currentIndex() if self._cb_search_type else 0
        has_condition = bool(kw) or type_idx > 0
        self._search_results = []
        for ni in self._iter_node_items():
            hit = self._node_match(ni, kw) if has_condition else False
            try:
                ni.set_highlighted(hit)
            except Exception:
                pass
            if hit:
                self._search_results.append(ni.node.id)
        # 重置索引到第一个匹配（但不跳转，等用户按回车）
        self._search_idx = 0 if (has_condition and self._search_results) else -1
        self._search_jumpedin = False  # 重置"已跳转"标志，下次回车跳到第一个
        if self._search_count_lbl:
            total = len(self._search_results)
            if total and self._search_idx >= 0:
                self._search_count_lbl.setText(f"{self._search_idx + 1}/{total}")
            else:
                self._search_count_lbl.setText(str(total))

    def _on_search_next(self):
        if not self._search_results:
            return
        # 首次按回车（还没跳转过）→ 跳到当前索引（第一个）；后续按回车 → 跳到下一个
        if not getattr(self, "_search_jumpedin", False):
            self._search_jumpedin = True
        else:
            self._search_idx = (self._search_idx + 1) % len(self._search_results)
        self._goto_search_result(self._search_idx, flash_ok=True)

    def _on_search_prev(self):
        if not self._search_results:
            return
        if not getattr(self, "_search_jumpedin", False):
            self._search_jumpedin = True
        else:
            self._search_idx = (self._search_idx - 1) % len(self._search_results)
        self._goto_search_result(self._search_idx, flash_ok=True)

    def _goto_search_result(self, idx: int, flash_ok: bool):
        if not (0 <= idx < len(self._search_results)):
            return
        node_id = self._search_results[idx]
        sc = self.scene()
        ni = sc._node_items.get(node_id) if isinstance(sc, NodeScene) else None
        if ni is None:
            return
        # 先确保缩放合适（避免缩放后中心偏移），再居中
        self._zoom_ensure_visible(ni)
        # centerOn 将节点的场景坐标居中显示在视口中
        self.centerOn(ni.global_center())
        # 选中节点
        try:
            ni.setSelected(True)
        except Exception:
            pass
        if flash_ok:
            try:
                ni.flash(1600)
            except Exception:
                pass
        total = len(self._search_results)
        if self._search_count_lbl:
            self._search_count_lbl.setText(f"{idx + 1}/{total}")

    # ---------------- 公开方法：画布跳节点（供 BlueprintApp 命中断点/Step 自动暂停后自动跳画布用） ----------------
    def jump_to_node_by_id(self, node_id: str, flash_ms: int = 2400) -> bool:
        """【外部调用】跳到指定 node_id 所在画布位置：缩放居中 + 选中 + flash 高亮
        返回 True=找到了节点并跳转，False=没找到。
        注意：这个是纯 UI 跳转，不触发任何执行/暂停。"""
        ni = None
        try:
            sc = self.scene()
            if isinstance(sc, NodeScene) and hasattr(sc, "_node_items"):
                ni = sc._node_items.get(node_id)
        except Exception:
            ni = None
        if ni is None:
            for cand in self._iter_node_items():
                try:
                    if cand.node.id == node_id:
                        ni = cand
                        break
                except Exception:
                    continue
        if ni is None:
            return False
        try:
            self._zoom_ensure_visible(ni)
        except Exception:
            pass
        try:
            self.centerOn(ni.global_center())
        except Exception:
            pass
        try:
            ni.setSelected(True)
        except Exception:
            pass
        try:
            ni.flash(int(flash_ms))
        except Exception:
            pass
        return True

    def _zoom_ensure_visible(self, ni: NodeItem):
        # 如果当前缩放太小导致节点几乎不可见，则适当放大
        try:
            cur = self.transform().m11()
            if cur < 0.5:
                self.scale(0.5 / cur, 0.5 / cur)
        except Exception:
            pass
        br = ni.sceneBoundingRect()
        self.ensureVisible(br, 80, 80)

    def _clear_search(self):
        if self._search_le:
            self._search_le.blockSignals(True)
            self._search_le.setText("")
            self._search_le.blockSignals(False)
        for ni in self._iter_node_items():
            try:
                ni.set_highlighted(False)
            except Exception:
                pass
        self._search_results = []
        self._search_idx = -1
        self._search_jumpedin = False
        if self._search_count_lbl:
            self._search_count_lbl.setText("0")

    def focus_search_box(self):
        """外部调用（Ctrl+F / 菜单）聚焦搜索框"""
        if self._search_le:
            self._search_le.setFocus()
            self._search_le.selectAll()

    # ---------------- 快捷键 ----------------
    def keyPressEvent(self, event):
        # 搜索框快捷键（阶段3-⑤）
        if event.key() == Qt.Key_Escape:
            if self._search_le and self._search_le.text():
                self._clear_search()
                event.accept()
                return
        elif (event.modifiers() & Qt.ControlModifier) and event.key() == Qt.Key_F:
            self.focus_search_box()
            event.accept()
            return
        super().keyPressEvent(event)

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
        # 【右键端口冲突处理】右键点击时，如果光标下有端口（PortItem），
        # 就不要进入平移模式，把事件交给scene→PortItem处理，
        # 这样用户右键点端口才能弹"解除所有连线"菜单。
        if event.button() == Qt.RightButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            scene = self.scene()
            has_port_under = False
            if isinstance(scene, NodeScene):
                for it in scene.items(scene_pos):
                    if isinstance(it, PortItem):
                        has_port_under = True
                        break
            if has_port_under:
                # 有端口：不进入panning，走默认Qt分发，让PortItem收到右键事件
                super().mousePressEvent(event)
                return
            # 空白区域右键：照常进入平移
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
        md = event.mimeData()
        if md.hasFormat(self.NODE_MIME) or md.hasFormat(self.ASSET_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent):
        md = event.mimeData()
        if md.hasFormat(self.NODE_MIME) or md.hasFormat(self.ASSET_MIME):
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
        if md.hasFormat(self.ASSET_MIME):
            # 资源面板拖入的蓝图：交给主窗口处理（算相对路径 + 进撤销栈 + 刷新画布）
            try:
                asset_path = bytes(md.data(self.ASSET_MIME)).decode("utf-8")
            except Exception:
                asset_path = ""
            scene = self.scene()
            if asset_path and isinstance(scene, NodeScene):
                scene_pos = self.mapToScene(event.position().toPoint())
                scene.asset_dropped.emit(asset_path, scene_pos)
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
