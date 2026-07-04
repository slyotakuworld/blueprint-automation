from typing import Dict, Optional

from PySide6.QtCore import Qt, QMimeData, Signal
from PySide6.QtGui import QColor, QDrag, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.node_library import (
    CATEGORIES_ORDER,
    NODE_LIBRARY,
    NODE_TYPES_ORDERED,
)
from .graphics_scene import NodeView


class _DraggableNodeTree(QTreeWidget):
    """
    必须子类化 QTreeWidget 重写 startDrag：
      - QAbstractItemView.startDrag() 是 protected virtual，
        在**外层 QWidget（NodePalette）** 上 override 是无效的，
        因为 Qt 拖拽是 viewport 里的 item 触发，调用的是 this._tree.startDrag(supportedActions)，
        而不是外层 widget 的 startDrag。之前写在 NodePalette 那个 startDrag 永远不会被调用！
    """

    def __init__(self, owner: "NodePalette", parent=None):
        super().__init__(parent)
        self._owner = owner

    def startDrag(self, supportedActions):
        it = self.currentItem()
        if not it:
            return
        ntype = it.data(0, Qt.UserRole)
        if not ntype:
            return
        md = QMimeData()
        md.setData(NodeView.NODE_MIME, ntype.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(md)
        pix = self._owner._grab_icon(ntype)
        if pix is not None:
            drag.setPixmap(pix)
            drag.setHotSpot(pix.rect().center())
        drag.exec(Qt.CopyAction)


class NodePalette(QWidget):
    """左侧节点面板：分类展示节点，支持拖拽到画布创建。"""

    info_message = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        title = QLabel("节点库")
        f = QFont()
        f.setBold(True)
        f.setPointSize(11)
        title.setFont(f)
        title.setStyleSheet("color: #C9D1D9;")
        outer.addWidget(title)

        tip = QLabel("拖拽节点到画布（或双击创建）")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8B949E; font-size: 9px;")
        outer.addWidget(tip)

        self._tree: _DraggableNodeTree = _DraggableNodeTree(self, self)
        self._tree.setHeaderHidden(True)
        self._tree.setDragEnabled(True)
        self._tree.setDragDropMode(QTreeWidget.DragOnly)
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet(self._tree_style())
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        outer.addWidget(self._tree, 1)

        self._fill_tree()

    def _tree_style(self) -> str:
        return (
            "QTreeWidget {"
            "  background-color: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 6px;"
            "  font-size: 10px;"
            "}"
            "QTreeWidget::item { height: 22px; padding: 2px 4px; }"
            "QTreeWidget::item:hover { background-color: #2D333B; }"
            "QTreeWidget::item:selected { background-color: #0969DA; color: white; }"
            "QTreeWidget::branch:has-children:!has-siblings:closed,"
            "QTreeWidget::branch:closed:has-children:has-siblings {"
            "  border-image: none; image: none; color: #8B949E;"
            "}"
            "QHeaderView::section { background: #1C2128; border: 0; padding: 0; }"
        )

    def _fill_tree(self):
        self._tree.clear()
        category_items: Dict[str, QTreeWidgetItem] = {}
        for cat in CATEGORIES_ORDER:
            it = QTreeWidgetItem([cat])
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            if "废弃" in cat or "deprecated" in cat.lower():
                it.setForeground(0, QColor("#F78166"))
                it.setToolTip(0, "该分类下的节点已废弃，仅用于兼容加载旧蓝图，不建议新蓝图使用")
            else:
                it.setForeground(0, QColor("#8B949E"))
            it.setFlags(it.flags() & ~Qt.ItemIsDragEnabled)
            self._tree.addTopLevelItem(it)
            category_items[cat] = it

        from ..core.node_library import CATEGORY_DEPRECATED
        for ntype in NODE_TYPES_ORDERED:
            meta = NODE_LIBRARY.get(ntype)
            if not meta:
                continue
            # 1) 新建隐藏节点：跳过（旧蓝图可加载，NODE_LIBRARY仍可正常解析
            if getattr(meta, "hidden_in_palette", False):
                continue
            parent = category_items.get(meta.category)
            if parent is None:
                continue
            title_display = meta.title
            is_deprecated = bool(getattr(meta, "deprecated", False))
            it = QTreeWidgetItem(parent, [title_display])
            it.setData(0, Qt.UserRole, ntype)
            tip_parts = [ntype, meta.description or ""]
            if is_deprecated:
                f = it.font(0)
                f.setStrikeOut(True)
                it.setFont(0, f)
                it.setForeground(0, QColor("#6E7681"))
                tip_parts.insert(0, "【⚠️ 已废弃，不推荐】")
            else:
                it.setForeground(0, QColor("#C9D1D9"))
            if meta.category == CATEGORY_DEPRECATED:
                tip_parts.insert(0, "【⚠️ 废弃分类，仅兼容旧蓝图】")
            it.setToolTip(0, "\n".join(t for t in tip_parts if t))
        self._tree.expandAll()
        # 如果废弃分类无任何子项，隐藏整组：保持显示提示
        for cat, it in category_items.items():
            if it.childCount() == 0 and "废弃" not in cat:
                continue  # 保留废弃组即使空也显示（提示用户分类存在，除非完全没废弃节点时折叠

    def current_node_type(self) -> Optional[str]:
        it = self._tree.currentItem()
        if not it:
            return None
        return it.data(0, Qt.UserRole)

    def _grab_icon(self, ntype: str):
        meta = NODE_LIBRARY.get(ntype)
        if not meta:
            return None
        from PySide6.QtGui import QPixmap, QPainter
        pix = QPixmap(120, 28)
        pix.fill(QColor(meta.color))
        p = QPainter(pix)
        p.setPen(QColor("#FFFFFF"))
        f = QFont()
        f.setBold(True)
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignCenter, meta.title)
        p.end()
        return pix

    def _on_double_click(self, item: QTreeWidgetItem, column: int):
        ntype = item.data(0, Qt.UserRole)
        if not ntype:
            return
        self.info_message.emit(
            f"双击了节点「{NODE_LIBRARY[ntype].title}」，可拖到画布创建。",
            "INFO",
        )
