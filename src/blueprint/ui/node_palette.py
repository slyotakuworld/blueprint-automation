from typing import Dict, Optional

from PySide6.QtCore import Qt, QMimeData, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
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
        self.setMaximumWidth(220)
        self._context_nodes: list = []  # 子画布上下文额外节点（如循环体里的 Continue/Break）
        self._node_items: list = []     # [(QTreeWidgetItem, node_type)]，供搜索框切换可见性
        self._filter = ""               # 当前搜索词（重建树后需要重新应用）
        self._build_ui()

    def set_context_nodes(self, extra_types):
        """设置子画布上下文额外节点（如循环体子画布里的 Continue/Break）。
        传空列表清空。会重新填充节点树。"""
        self._context_nodes = list(extra_types or [])
        self._fill_tree()

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

        # ---- 搜索框：输入即过滤节点（匹配名称/类型/描述/分类）----
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 搜索节点…")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            "QLineEdit { background:#161B22; color:#C9D1D9; border:1px solid #30363D;"
            " border-radius:4px; padding:3px 6px; font-size:10px; }"
            "QLineEdit:focus { border-color:#1F6FEB; }"
        )
        self._search.textChanged.connect(self._on_search_changed)
        outer.addWidget(self._search)
        # Esc → 清空搜索（并让焦点回到列表，方便继续用键盘）
        _sc = QShortcut(QKeySequence(Qt.Key_Escape), self._search)
        _sc.activated.connect(self._clear_search)

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
        self._node_items = []
        category_items: Dict[str, QTreeWidgetItem] = {}
        for cat in CATEGORIES_ORDER:
            it = QTreeWidgetItem([cat])
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            it.setForeground(0, QColor("#8B949E"))
            it.setFlags(it.flags() & ~Qt.ItemIsDragEnabled)
            self._tree.addTopLevelItem(it)
            category_items[cat] = it

        for ntype in NODE_TYPES_ORDERED:
            meta = NODE_LIBRARY.get(ntype)
            if not meta:
                continue
            # 1) 新建隐藏节点：跳过（蓝图加载仍可正常解析，NODE_LIBRARY保有完整定义）
            if getattr(meta, "hidden_in_palette", False):
                continue
            parent = category_items.get(meta.category)
            if parent is None:
                continue
            title_display = meta.title
            it = QTreeWidgetItem(parent, [title_display])
            it.setData(0, Qt.UserRole, ntype)
            tip_parts = [ntype, meta.description or ""]
            it.setForeground(0, QColor("#C9D1D9"))
            it.setToolTip(0, "\n".join(t for t in tip_parts if t))
            self._node_items.append((it, ntype))
        # 2) 子画布上下文节点（如循环体里的 Continue/Break）追加到各自分类
        for ntype in self._context_nodes:
            meta = NODE_LIBRARY.get(ntype)
            if not meta:
                continue
            parent = category_items.get(meta.category)
            if parent is None:
                continue
            it = QTreeWidgetItem(parent, [meta.title])
            it.setData(0, Qt.UserRole, ntype)
            it.setForeground(0, QColor("#A371F7"))
            it.setToolTip(0, f"{ntype}\n{meta.description or ''}")
            self._node_items.append((it, ntype))
        self._tree.expandAll()
        # 重建后重新套用当前搜索词（否则刷新一下过滤就丢了）
        self._apply_filter(self._filter)

    # ============================================================
    # 搜索过滤
    # ============================================================
    def _on_search_changed(self, text: str):
        self._filter = (text or "").strip()
        self._apply_filter(self._filter)

    def _clear_search(self):
        if self._search.text():
            self._search.clear()
        else:
            self._tree.setFocus()

    def _apply_filter(self, keyword: str):
        """按关键词过滤节点项：命中名称 / 类型名 / 描述 / 分类名。

        空关键词 = 全部显示并展开。有关键词时收起无命中的分类，
        并自动展开命中的分类，方便一眼看到结果。
        """
        kw = (keyword or "").strip().lower()
        cat_visible: Dict[QTreeWidgetItem, int] = {}
        for it, ntype in self._node_items:
            meta = NODE_LIBRARY.get(ntype)
            if kw:
                hay = " ".join(filter(None, [
                    meta.title if meta else "",
                    ntype,
                    (meta.category if meta else "") or "",
                    (meta.description if meta else "") or "",
                ])).lower()
                hit = kw in hay
            else:
                hit = True
            it.setHidden(not hit)
            parent = it.parent()
            if parent is not None:
                cat_visible[parent] = cat_visible.get(parent, 0) + (1 if hit else 0)

        for i in range(self._tree.topLevelItemCount()):
            cat = self._tree.topLevelItem(i)
            n = cat_visible.get(cat, 0)
            cat.setHidden(kw != "" and n == 0)
            if kw:
                cat.setExpanded(n > 0)
            else:
                cat.setExpanded(True)

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
