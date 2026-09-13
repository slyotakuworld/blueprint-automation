"""🗂 工程资源管理器（AssetPanel）

类似 Cocos Creator 的资源面板：以目录树展示当前工程的资源（蓝图 / 组件 / 图片 / 文档），
在这里完成蓝图的 新建 / 打开 / 重命名 / 删除，不再依赖「工程 ▾」下拉菜单。

设计约定：
  - 面板只做「展示 + 发出意图」，所有文件写操作由主窗口 BlueprintApp 执行，
    保持「路径解析 / 备份 / 回收站 / 脏状态」策略集中在主窗口一处。
  - 扫描时剪枝隐藏目录与大体积运行产物（cache/debug/log），并限制递归深度，
    保证展开/刷新不卡顿（旧「加载蓝图」菜单首次展开卡顿就是全量 os.walk 导致的）。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, QFileSystemWatcher, QMimeData, QTimer, Signal
from PySide6.QtGui import QAction, QDrag, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 剪枝：这些目录不展示（运行产物 / 隐藏目录），避免资源树臃肿、扫描变慢
_PRUNE_DIRS = {
    "cache", "debug", "log", "logs", "__pycache__",
    ".git", ".bp", ".idea", ".vscode", ".workbuddy",
    "backup", "backups", "dist", "build",
}
# 展示的扩展名（其余文件不进资源树）
_ASSET_EXTS = (
    ".bp.json", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
    ".txt", ".md", ".json", ".csv",
)
_MAX_DEPTH = 6          # 递归深度上限（超出不再展开，防止异常目录结构拖慢）
_MAX_ITEMS = 3000       # 资源树最大条数（超出截断，保证 UI 不卡）
_MAX_WATCH_DIRS = 200   # 文件监听最多注册的目录数（防止超深目录树拖慢）
_MAX_RECENT = 10        # 「最近打开」保留条数


class _AssetTree(QTreeWidget):
    """资源树：额外提供「拖蓝图到画布」的拖拽起手（MIME = NodeView.ASSET_MIME）。"""

    ASSET_MIME = "application/x-bp-asset"   # 与 NodeView.ASSET_MIME 保持一致（画布侧接收）

    def startDrag(self, supportedActions):
        items = self.selectedItems() or []
        paths = [it.data(0, Qt.UserRole) for it in items]
        paths = [p for p in paths if isinstance(p, str) and p.lower().endswith(".bp.json")]
        if not paths:
            return
        md = QMimeData()
        # 多选时只拖第一个（画布侧一次建一个节点，避免误拖一堆）
        md.setData(self.ASSET_MIME, paths[0].encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)


class AssetPanel(QWidget):
    """工程资源面板：展示工程目录树并提供蓝图管理入口（只发意图，不直接写文件）。"""

    open_blueprint = Signal(str)      # 打开蓝图（主窗口负责 confirm_discard + 加载）
    request_new = Signal(str, str)    # (kind: "blueprint"|"component"|"folder", 目标目录绝对路径)
    request_rename = Signal(str)      # 重命名某个文件/目录（绝对路径）
    request_delete = Signal(list)     # 删除选中的文件/目录（list[绝对路径]）
    save_current = Signal()           # 保存当前蓝图
    save_as_component = Signal()      # 把当前蓝图另存为组件（原「工程 ▾」里的入口迁到这里）
    open_from_disk = Signal()         # 从文件浏览打开任意位置的蓝图（原「加载蓝图 → 从文件浏览打开…」）
    reveal_path = Signal(str)         # 在系统资源管理器中定位
    request_reverse_deps = Signal(str)  # 谁引用了它（传被引用的蓝图绝对路径）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project_root: Optional[str] = None
        self._project_name: str = ""
        self._current_path: Optional[str] = None
        self._expanded: Set[str] = set()      # 展开状态（路径集合），刷新时保留
        self._recent: List[str] = []          # 最近打开的蓝图（绝对路径，最新在前）
        self._watcher: Optional[QFileSystemWatcher] = None
        self._watch_dirs: List[str] = []
        self._need_rewatch = False             # 事件里出现「新目录」时才重新注册监听
        self._suppress_fs_event = 0           # 自己触发的刷新期间抑制文件事件
        self._fs_timer = QTimer(self)
        self._fs_timer.setSingleShot(True)
        self._fs_timer.setInterval(700)       # 防抖：一次批量写入只刷新一次
        self._fs_timer.timeout.connect(self._on_fs_debounce)
        self._build_ui()

    # ============================================================
    # UI
    # ============================================================
    def _build_ui(self):
        self.setStyleSheet(
            "AssetPanel { background:#0D1117; }"
            "QLabel#assetTitle { color:#C9D1D9; font-weight:600; font-size:10px; }"
            "QLabel#assetFooter { color:#6E7681; font-size:9px; padding:2px 6px; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶部：标题 + 操作按钮 ----
        head = QWidget()
        head.setStyleSheet("background:#161B22; border-bottom:1px solid #30363D;")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 4, 6, 4)
        hl.setSpacing(4)
        title = QLabel("🗂 工程资源")
        title.setObjectName("assetTitle")
        hl.addWidget(title)
        hl.addStretch(1)
        for text, tip, kind in (
            ("＋蓝图", "在工程根目录新建蓝图", "blueprint"),
            ("＋组件", "在 components/ 目录新建组件", "component"),
            ("📁", "新建文件夹", "folder"),
            ("🔄", "刷新资源树", "refresh"),
        ):
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setFixedHeight(20)
            btn.setStyleSheet(
                "QPushButton { background:#21262D; color:#C9D1D9; border:1px solid #30363D;"
                " border-radius:3px; padding:0 6px; font-size:9px; }"
                "QPushButton:hover { background:#1F6FEB; color:#FFFFFF; border-color:#1F6FEB; }"
            )
            btn.clicked.connect(lambda _=False, k=kind: self._on_head_button(k))
            hl.addWidget(btn)
        root.addWidget(head)

        # ---- 中部：资源树 ----
        self._tree = _AssetTree()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(14)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemCollapsed.connect(self._on_item_collapsed)
        # 拖蓝图到画布 = 生成/填充「调用组件」节点
        self._tree.setDragEnabled(True)
        self._tree.setDragDropMode(QTreeWidget.DragOnly)
        self._tree.setStyleSheet(
            "QTreeWidget { background:#0D1117; color:#C9D1D9; border:none; font-size:10px; }"
            "QTreeWidget::item { padding:2px 2px; }"
            "QTreeWidget::item:selected { background:#1F6FEB; color:#FFFFFF; }"
            "QTreeWidget::item:hover { background:#1C2430; }"
            "QTreeWidget::branch { background:transparent; }"
        )
        root.addWidget(self._tree, 1)

        # ---- 底部：统计/提示 ----
        self._footer = QLabel("（未打开工程）")
        self._footer.setObjectName("assetFooter")
        self._footer.setWordWrap(True)
        root.addWidget(self._footer)

    # ============================================================
    # 对外接口
    # ============================================================
    def set_project(self, project_root: Optional[str], project_name: str = ""):
        """工程切换时调用：更新根目录并重建资源树。"""
        self._project_root = project_root or None
        self._project_name = project_name or ""
        self._expanded.clear()
        self._recent = [p for p in self._recent if self._project_root and self._under_root(p)]
        self._setup_watcher()
        self.refresh()

    def set_current_path(self, path: Optional[str]):
        """标记当前正在编辑的蓝图（📌 高亮显示），并记入「最近打开」。"""
        self._current_path = path or None
        self._push_recent(self._current_path)
        self._apply_current_mark()

    def _under_root(self, path: str) -> bool:
        if not self._project_root or not path:
            return False
        try:
            return os.path.normcase(os.path.abspath(path)).startswith(
                os.path.normcase(os.path.abspath(self._project_root)) + os.sep)
        except Exception:
            return False

    def _push_recent(self, path: Optional[str]):
        """把蓝图记入最近打开（去重 + 置顶 + 上限），并在树里刷新出来。"""
        if not path or not self._under_root(path):
            return
        if not os.path.isfile(path):
            return
        self._recent = [p for p in self._recent if not self._same_path(p, path)]
        self._recent.insert(0, path)
        del self._recent[_MAX_RECENT:]
        self._rebuild_recent_group()

    # ============================================================
    # 文件监听（自动刷新）
    # ============================================================
    def _setup_watcher(self):
        """监听工程目录：外部增删改文件后自动刷新资源树，省掉手动点 🔄。"""
        if self._watcher is not None:
            try:
                self._watcher.directoryChanged.disconnect()
                self._watcher.fileChanged.disconnect()
            except Exception:
                pass
        if not self._project_root or not os.path.isdir(self._project_root):
            self._watcher = None
            return
        if self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
        dirs = [self._project_root]
        dirs.extend(self._collect_dirs(self._project_root))
        added = self._watcher.addPaths(dirs[:_MAX_WATCH_DIRS])
        self._watch_dirs = dirs[:_MAX_WATCH_DIRS] if added is not False else []
        self._watcher.directoryChanged.connect(self._on_fs_event)
        self._watcher.fileChanged.connect(self._on_fs_event)

    def _collect_dirs(self, root: str) -> List[str]:
        """收集需要监听的子目录（剪枝隐藏/运行产物目录，限制数量）。"""
        out: List[str] = []
        stack = [(root, 1)]
        while stack and len(out) < _MAX_WATCH_DIRS:
            cur, depth = stack.pop()
            if depth > _MAX_DEPTH - 2:
                continue
            try:
                for ent in os.scandir(cur):
                    if not ent.is_dir():
                        continue
                    name = ent.name
                    if name.lower() in _PRUNE_DIRS or name.startswith("."):
                        continue
                    out.append(ent.path)
                    stack.append((ent.path, depth + 1))
            except OSError:
                continue
        return out

    def _on_fs_event(self, path: str = ""):
        if self._suppress_fs_event:
            return
        # 目录内容变化 → 可能新增/删除了子目录，需要补注册监听（重注册有成本，下面按时间节流）。
        try:
            if path and os.path.isdir(path):
                self._need_rewatch = True
        except Exception:
            pass
        self._fs_timer.start()

    def _on_fs_debounce(self):
        if self._suppress_fs_event:
            return
        if self._need_rewatch:
            self._need_rewatch = False
            # 只有「目录集合真的变了」（新建/删除文件夹）才重新注册监听，避免无谓开销
            try:
                root = self._project_root or ""
                if root and os.path.isdir(root):
                    cur = {os.path.normcase(os.path.abspath(root))}
                    cur |= {os.path.normcase(os.path.abspath(d)) for d in self._collect_dirs(root)}
                    known = {os.path.normcase(os.path.abspath(d)) for d in self._watch_dirs}
                    if cur != known:
                        self._setup_watcher()
            except Exception:
                pass
        self.refresh()

    def refresh(self):
        """重建资源树（保留展开状态）。文件增删改后由主窗口调用。"""
        self._tree.clear()
        root_path = self._project_root
        if not root_path or not os.path.isdir(root_path):
            it = QTreeWidgetItem(["（未打开工程，请先新建/打开工程）"])
            it.setFlags(Qt.NoItemFlags)
            self._tree.addTopLevelItem(it)
            self._footer.setText("（未打开工程）")
            return
        count = 0
        # 顶部：🕘 最近打开（有记录时才出现）
        recent_valid = [p for p in self._recent if os.path.isfile(p)]
        if recent_valid:
            self._recent = recent_valid
            self._tree.addTopLevelItem(self._make_recent_item())
        root_item = QTreeWidgetItem([f"📦 {self._project_name or os.path.basename(root_path)}"])
        root_item.setData(0, Qt.UserRole, root_path)
        root_item.setData(0, Qt.UserRole + 1, "dir")
        root_item.setToolTip(0, root_path)
        root_item.setExpanded(True)
        self._expanded.add(root_path)
        self._tree.addTopLevelItem(root_item)
        count = self._fill_dir(root_item, root_path, depth=1)
        self._apply_current_mark()
        self._footer.setText(f"{count} 项资源 · {root_path}")

    def _make_recent_item(self) -> QTreeWidgetItem:
        grp = QTreeWidgetItem(["🕘 最近打开"])
        f = grp.font(0)
        f.setBold(True)
        grp.setFont(0, f)
        for p in self._recent[:_MAX_RECENT]:
            name = os.path.basename(p)
            is_comp = self._is_under_components(p)
            icon = "🧩" if is_comp else "🗺"
            child = QTreeWidgetItem([f"{icon} {self._bp_display_name(name, is_comp)}"])
            child.setData(0, Qt.UserRole, p)
            child.setData(0, Qt.UserRole + 1, "component" if is_comp else "bp")
            child.setToolTip(0, p)
            grp.addChild(child)
        grp.setExpanded(True)
        return grp

    def _rebuild_recent_group(self):
        """只更新「🕘 最近打开」分组，避免整棵树重建（保持用户展开状态）。"""
        try:
            for i in range(self._tree.topLevelItemCount()):
                it = self._tree.topLevelItem(i)
                if it.text(0).startswith("🕘"):
                    self._tree.takeTopLevelItem(i)
                    break
            if self._recent and self._project_root:
                self._tree.insertTopLevelItem(0, self._make_recent_item())
        except Exception:
            pass

    # ============================================================
    # 资源树构建
    # ============================================================
    def _fill_dir(self, parent_item: QTreeWidgetItem, dir_path: str, depth: int) -> int:
        """递归填充目录（剪枝隐藏/运行产物目录，限制深度与总条数）。返回新增条数。"""
        if depth > _MAX_DEPTH:
            return 0
        added = 0
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return 0
        for ent in entries:
            if added >= _MAX_ITEMS:
                break
            name = ent.name
            try:
                is_dir = ent.is_dir()
            except OSError:
                continue
            if is_dir:
                if name.lower() in _PRUNE_DIRS or name.startswith("."):
                    continue
                sub = QTreeWidgetItem([f"📁 {name}"])
                sub.setData(0, Qt.UserRole, ent.path)
                sub.setData(0, Qt.UserRole + 1, "dir")
                sub.setToolTip(0, ent.path)
                parent_item.addChild(sub)
                added += 1
                added += self._fill_dir(sub, ent.path, depth + 1)
                if sub.childCount() == 0 and name.lower() not in ("components", "screenshots", "templates"):
                    # 空目录只保留 components 这类约定目录，其余折叠不显示展开箭头
                    sub.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
                if ent.path in self._expanded:
                    sub.setExpanded(True)
            else:
                low = name.lower()
                if not low.endswith(_ASSET_EXTS) or ".backup_" in low or low.endswith(".tmp"):
                    continue
                if low.endswith(".bp.json"):
                    is_component = self._is_under_components(ent.path)
                    icon = "🧩" if is_component else "🗺"
                    label = f"{icon} {self._bp_display_name(name, is_component)}"
                elif low.endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")):
                    label = f"🖼 {name}"
                elif low.endswith(".md"):
                    label = f"📘 {name}"
                else:
                    label = f"📄 {name}"
                it = QTreeWidgetItem([label])
                it.setData(0, Qt.UserRole, ent.path)
                it.setData(0, Qt.UserRole + 1, "component" if (low.endswith(".bp.json") and self._is_under_components(ent.path)) else ("bp" if low.endswith(".bp.json") else "file"))
                it.setToolTip(0, ent.path)
                parent_item.addChild(it)
                added += 1
        return added

    @staticmethod
    def _bp_display_name(filename: str, is_component: bool) -> str:
        """蓝图显示名：去掉 .bp.json 后缀（组件加前缀标识）。"""
        stem = filename[: -len(".bp.json")] if filename.lower().endswith(".bp.json") else filename
        return f"[组件] {stem}" if is_component else stem

    def _is_under_components(self, path: str) -> bool:
        if not self._project_root:
            return False
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(self._project_root))
        except Exception:
            return False
        parts = rel.replace("\\", "/").split("/")
        return len(parts) > 1 and parts[0].lower() == "components"

    # ============================================================
    # 交互
    # ============================================================
    def _on_head_button(self, kind: str):
        if kind == "refresh":
            self.refresh()
            return
        if not self._project_root:
            self._footer.setText("（请先新建/打开工程，才能创建资源）")
            return
        if kind == "component":
            target = os.path.join(self._project_root, "components")
        else:
            target = self._project_root
        self.request_new.emit(kind, target)

    def _on_item_expanded(self, item: QTreeWidgetItem):
        p = item.data(0, Qt.UserRole)
        if p:
            self._expanded.add(p)

    def _on_item_collapsed(self, item: QTreeWidgetItem):
        p = item.data(0, Qt.UserRole)
        if p:
            self._expanded.discard(p)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int):
        path = item.data(0, Qt.UserRole)
        kind = item.data(0, Qt.UserRole + 1)
        if not path:
            return
        if kind == "dir":
            item.setExpanded(not item.isExpanded())
            return
        if os.path.isfile(path) and path.lower().endswith(".bp.json"):
            self.open_blueprint.emit(path)
        elif os.path.isfile(path):
            self.reveal_path.emit(path)

    def _selected_paths(self) -> List[str]:
        out: List[str] = []
        for it in self._tree.selectedItems():
            p = it.data(0, Qt.UserRole)
            if p and os.path.exists(p):
                out.append(p)
        return out

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        path = item.data(0, Qt.UserRole) if item is not None else None
        kind = item.data(0, Qt.UserRole + 1) if item is not None else None
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#21262D; color:#C9D1D9; border:1px solid #30363D; padding:4px; }"
            "QMenu::item { padding:6px 18px; border-radius:3px; }"
            "QMenu::item:selected { background:#1F6FEB; color:#FFFFFF; }"
            "QMenu::item:disabled { color:#484F58; }"
            "QMenu::separator { height:1px; background:#30363D; margin:4px 6px; }"
        )
        proj_root = self._project_root
        has_proj = bool(proj_root and os.path.isdir(proj_root))
        is_dir = (kind == "dir")

        def _act(text: str, cb, enabled: bool = True):
            a = QAction(text, menu)
            a.setEnabled(enabled)
            a.triggered.connect(lambda _c=False: cb())
            menu.addAction(a)
            return a

        if isinstance(path, str) and path:
            # 落在具体条目上
            if os.path.isdir(path):
                _act("＋ 在此新建蓝图…", lambda: self.request_new.emit("blueprint", path), has_proj)
                _act("🧩 在此新建组件…", lambda: self.request_new.emit("component", path), has_proj)
                _act("📁 在此新建文件夹…", lambda: self.request_new.emit("folder", path), has_proj)
                menu.addSeparator()
                _act("✏ 重命名…", lambda: self.request_rename.emit(path))
                _act("🗑 删除文件夹…", lambda: self.request_delete.emit([path]))
                menu.addSeparator()
                _act("📂 在资源管理器中打开", lambda: self.reveal_path.emit(path))
            else:
                is_bp = path.lower().endswith(".bp.json")
                _act("打开蓝图", lambda: self.open_blueprint.emit(path), is_bp)
                _act("💾 保存（当前蓝图）", lambda: self.save_current.emit(), is_bp and self._same_path(path, self._current_path))
                if is_bp:
                    _act("🔗 谁引用了它…", lambda: self.request_reverse_deps.emit(path))
                menu.addSeparator()
                _act("✏ 重命名…", lambda: self.request_rename.emit(path))
                sel = self._selected_paths()
                del_targets = sel if (len(sel) > 1 and path in sel) else [path]
                _act(f"🗑 删除（{len(del_targets)} 项）" if len(del_targets) > 1 else "🗑 删除",
                     lambda: self.request_delete.emit(del_targets))
                menu.addSeparator()
                _act("📂 在资源管理器中定位", lambda: self.reveal_path.emit(path))
                _act("📋 复制完整路径", lambda: self._copy_path(path))
        else:
            # 空白处
            _act("＋ 新建蓝图…", lambda: self.request_new.emit("blueprint", proj_root or ""), has_proj)
            _act("🧩 新建组件…", lambda: self.request_new.emit("component", proj_root or ""), has_proj)
            _act("📁 新建文件夹…", lambda: self.request_new.emit("folder", proj_root or ""), has_proj)
            menu.addSeparator()
            _act("💾 保存当前蓝图", lambda: self.save_current.emit(), bool(self._current_path))
            _act("📦 将当前蓝图另存为组件…", lambda: self.save_as_component.emit(), bool(self._current_path))
            _act("📂 从文件浏览打开…", lambda: self.open_from_disk.emit())
            menu.addSeparator()
            _act("🔄 刷新", lambda: self.refresh())
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _same_path(a: Optional[str], b: Optional[str]) -> bool:
        if not a or not b:
            return False
        try:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
        except Exception:
            return False

    def _copy_path(self, path: str):
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(path)
            self._footer.setText(f"已复制路径：{path}")
        except Exception:
            pass

    # ============================================================
    # 当前蓝图高亮
    # ============================================================
    def _apply_current_mark(self):
        """遍历资源树，把当前编辑的蓝图标成粗体 + 📌。"""
        cur = self._current_path
        bold = QFont()
        bold.setBold(True)
        plain = QFont()

        def walk(item: QTreeWidgetItem):
            path = item.data(0, Qt.UserRole)
            if isinstance(path, str) and path.lower().endswith(".bp.json"):
                base = item.text(0).replace("📌 ", "", 1)
                if self._same_path(path, cur):
                    item.setText(0, "📌 " + base)
                    item.setFont(0, bold)
                else:
                    item.setText(0, base)
                    item.setFont(0, plain)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
