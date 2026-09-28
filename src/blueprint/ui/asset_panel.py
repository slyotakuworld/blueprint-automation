"""🗂 工程资源管理器（AssetPanel）

以目录树展示当前工程的资源（蓝图 / 组件 / 图片 / 文档），
在这里完成蓝图的 新建 / 打开 / 重命名 / 删除 / 改类型，不再依赖「工程 ▾」下拉菜单。

设计约定：
  - 面板只做「展示 + 发出意图」，所有文件写操作由主窗口 BlueprintApp 执行，
    保持「路径解析 / 备份 / 回收站 / 脏状态」策略集中在主窗口一处。
  - 扫描时剪枝隐藏目录与大体积运行产物（cache/debug/log），并限制递归深度，
    保证展开/刷新不卡顿（旧「加载蓝图」菜单首次展开卡顿就是全量 os.walk 导致的）。
  - **蓝图建在哪个目录由用户决定**：顶栏 / 右键的「新建」一律落在当前选中的目录下，
    不再有 components/ 这种固定位置；「是不是组件」写在蓝图自己的 bp_type 元数据里。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, QFileSystemWatcher, QMimeData, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDrag, QFont
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

from ..core import asset_store, bp_kind

# 只剪掉「版本库 / 工具链 / 构建产物」这类非用户内容目录；
# 其余（含 cache/debug/log 等运行产物）都照常展示，除 project_meta.json 外一律可见。
_PRUNE_DIRS = {
    "__pycache__",
    ".git", ".bp", ".idea", ".vscode", ".workbuddy",
    "backup", "backups", "dist", "build", "node_modules",
}
# 在资源树中永远隐藏的文件（工程元数据，不是用户资源）
_HIDE_FILES = {"project_meta.json"}
# 展示的扩展名（其余文件不进资源树）
_ASSET_EXTS = (
    ".bp.json", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
    ".txt", ".md", ".json", ".csv",
)
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
_MAX_DEPTH = 6          # 递归深度上限（超出不再展开，防止异常目录结构拖慢）
# ⚠ 条数上限必须是「每个目录各自一份」，不能是全树累计：
# 历史上这里用的是全树累计计数（added 一路累加），结果一个大目录（比如 cache 里几千个日志）
# 会把额度吃光，排在它后面的目录一个子节点都建不出来 → 表现为「其它文件夹不显示文件，
# 双击也白搭」（双击只切换展开，不会补建节点）。现在改为：单目录各自 500 条，
# 全树 20000 条仅作为极端情况的兜底。
_MAX_ITEMS_PER_DIR = 500  # 单个目录最多显示多少条（超出显示「… 还有 N 项未显示」）
_MAX_ITEMS = 20000        # 整树兜底上限
_MAX_WATCH_DIRS = 200   # 文件监听最多注册的目录数（防止超深目录树拖慢）
_MAX_RECENT = 10        # 「最近打开」保留条数

# 类型配色（深色主题）：普通蓝图=蓝，组件蓝图=橙（与画布上 Component 节点同色系）
_COLOR_BP = "#79C0FF"
_COLOR_COMPONENT = "#FFA657"
_COLOR_IMAGE = "#7EE787"
_COLOR_DIR = "#8B949E"
# 空目录也要显示展开箭头的「约定目录」（新工程默认自带的文件夹）
_KEEP_EMPTY_DIRS = ("resource", "components", "blueprints")


def _bp_stem(filename: str) -> str:
    """蓝图显示名：去掉 .bp.json 后缀。"""
    low = (filename or "").lower()
    if low.endswith(".bp.json"):
        return filename[: -len(".bp.json")]
    return filename



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
    request_set_bp_type = Signal(str, str)  # 改蓝图类型（绝对路径, "blueprint"|"component"）
    request_cleanup_assets = Signal()  # 清理未被任何蓝图引用的资源
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
        self._last_root: Optional[str] = None   # set_project 上次设的根（用于判断是否需要重置展开）
        self._last_name: str = ""
        self._expanded: Set[str] = set()      # 展开状态（路径集合），刷新时保留
        self._recent: List[str] = []          # 最近打开的蓝图（绝对路径，最新在前）
        self._watcher: Optional[QFileSystemWatcher] = None
        self._watch_dirs: List[str] = []
        self._need_rewatch = False             # 事件里出现「新目录」时才重新注册监听
        self._suppress_fs_event = 0           # 自己触发的刷新期间抑制文件事件
        self._scan_errors: List[tuple] = []   # 本轮刷新里「读不出内容」的目录（带原因），用于诊断
        self._budget = _MAX_ITEMS              # 整树剩余额度（每建一个节点 -1，refresh 时重置）
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
            ("＋蓝图", "在选中的目录下新建普通蓝图（未选中则建在工程根）", "blueprint"),
            ("＋组件", "在选中的目录下新建组件蓝图（组件放哪由你决定）", "component"),
            ("📁", "新建文件夹", "folder"),
            ("🧹", "清理没有被任何蓝图引用的资源", "cleanup"),
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
    # 类型判定（统一走 bp_kind，别再靠目录猜）
    # ============================================================
    def _bp_type_of(self, path: str) -> str:
        return bp_kind.read_bp_type(path, self._project_root)

    def _type_label(self, path: str) -> str:
        """蓝图在树里的显示文案：图标 + [组件] 前缀 + 名称（类型一眼可辨）。"""
        stem = _bp_stem(os.path.basename(path))
        if self._bp_type_of(path) == bp_kind.BP_TYPE_COMPONENT:
            return f"🧩 [组件] {stem}"
        return f"🗺 {stem}"

    def _color_of(self, path: str) -> Optional[str]:
        low = (path or "").lower()
        if low.endswith(".bp.json"):
            return (_COLOR_COMPONENT
                    if self._bp_type_of(path) == bp_kind.BP_TYPE_COMPONENT
                    else _COLOR_BP)
        if low.endswith(_IMG_EXTS):
            return _COLOR_IMAGE
        return None

    # ============================================================
    # 对外接口
    # ============================================================
    def set_project(self, project_root: Optional[str], project_name: str = ""):
        """工程切换时调用：更新根目录并重建资源树。"""
        self._project_root = project_root or None
        self._project_name = project_name or ""
        self._expanded.clear()
        self._last_root = self._project_root
        self._last_name = self._project_name
        self._recent = [p for p in self._recent if self._project_root and self._under_root(p)]
        self.refresh()                      # 先建树：保证文件一定显示，不被下面监听的异常连累
        try:
            self._setup_watcher()           # 文件监听失败只影响「自动刷新」，绝不影响资源展示
        except Exception:
            pass

    def reveal(self, path: Optional[str]):
        """新建/保存后让某个文件在资源树里可见：展开其所有祖先目录并选中它。

        注意：refresh() 已把整棵树的节点都建进模型（只是折叠），
        所以递归搜索一定能找到目标项，不必依赖它是否已经展开。
        """
        if not path or not self._project_root:
            return
        try:
            ap = os.path.abspath(path)
            rp = os.path.abspath(self._project_root)
            if not (ap == rp or ap.startswith(rp + os.sep)):
                return
            # 自底向上收集祖先目录（不含根本身），再自顶向下逐个展开
            chain: List[str] = []
            cur = os.path.dirname(ap)
            while cur != rp and len(cur) > len(rp):
                chain.append(cur)
                cur = os.path.dirname(cur)
            chain.reverse()
            for d in chain:
                it = self._find_item_by_path(d)
                if it is not None:
                    it.setExpanded(True)
                    self._expanded.add(d)   # 记入展开集合，后续 refresh() 也保持展开
            target = self._find_item_by_path(ap)
            if target is not None:
                self._tree.setCurrentItem(target)
                self._tree.scrollToItem(target)
        except Exception:
            pass

    def _find_item_by_path(self, path: str) -> Optional["QTreeWidgetItem"]:
        """在资源树里递归查找 UserRole 命中 path 的节点（折叠节点也在模型里，能搜到）。

        注意：同一蓝图同时挂在「工程树」和「🕘 最近打开」两个地方，而在最外层
        遍历时「最近打开」分组排在前面，会先被搜到。reveal/高亮必须落到工程树里的
        真实节点——否则 setCurrentItem 会逼着 Qt 自动展开最近打开分组让其可视，
        表现为「建完蓝图最近打开自己弹开了」。故遇到 🕘 分组直接跳过其子树。
        """

        def walk(parent):
            root = parent if parent is not None else self._tree.invisibleRootItem()
            for i in range(root.childCount()):
                it = root.child(i)
                if it.text(0).startswith("🕘"):
                    continue  # 跳过「最近打开」分组子树
                p = it.data(0, Qt.UserRole)
                if isinstance(p, str) and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(path)):
                    return it
                deeper = walk(it)
                if deeper is not None:
                    return deeper
            return None

        return walk(None)

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
        """重建资源树。
        - 只移除并重建「工程节点（📦）」，保留「🕘 最近打开」分组节点，
          避免每次打开/保存文件时整棵树闪烁，也避免最近打开被强行折叠/展开。
        - 最近分组由 _rebuild_recent_group 原地更新（默认折叠，尊重用户手动展开态）。
        """
        self._scan_errors = []   # 新一轮刷新，清空诊断
        self._budget = _MAX_ITEMS    # 新一轮刷新，重置整树额度
        root_path = self._project_root
        # 仅移除「工程节点 / 占位提示」这类会变化的顶层节点，保留「🕘 最近打开」分组节点
        for i in range(self._tree.topLevelItemCount() - 1, -1, -1):
            it = self._tree.topLevelItem(i)
            is_proj = it.text(0).startswith("📦")
            is_placeholder = (it.flags() & Qt.ItemIsEnabled) == 0
            if is_proj or is_placeholder:
                self._tree.takeTopLevelItem(i)
        if not root_path or not os.path.isdir(root_path):
            ph = QTreeWidgetItem(["（未打开工程，请先新建/打开工程）"])
            ph.setFlags(Qt.NoItemFlags)
            self._tree.addTopLevelItem(ph)
            self._footer.setText("（未打开工程）")
        else:
            root_item = QTreeWidgetItem([f"📦 {self._project_name or os.path.basename(root_path)}"])
            root_item.setData(0, Qt.UserRole, root_path)
            root_item.setData(0, Qt.UserRole + 1, "dir")
            root_item.setToolTip(0, root_path)
            self._expanded.add(root_path)
            self._tree.addTopLevelItem(root_item)
            count = self._fill_dir(root_item, root_path, depth=1)
            # 必须在「加进树 + 填完子项」之后才 setExpanded，否则 Qt 不生效
            root_item.setExpanded(True)
            if self._scan_errors:
                errs = "；".join(f"{os.path.basename(p)}: {m}" for p, m in self._scan_errors[:3])
                more = f" 等{len(self._scan_errors)}处" if len(self._scan_errors) > 3 else ""
                self._footer.setText(
                    f"⚠ 有目录读取失败（{errs}{more}）· {count} 项资源\n{root_path}")
            else:
                self._footer.setText(f"{count} 项资源 · 🗺 蓝图 🧩 组件 🖼 资源\n{root_path}")
        # 原地更新最近打开分组（不重建顶层节点 → 不闪烁、不强制展开）
        self._rebuild_recent_group()
        self._apply_current_mark()

    def _rebuild_recent_group(self):
        """原地更新「🕘 最近打开」分组：不重建顶层节点，避免每次打开文件时树闪烁/自动展开。
        默认折叠；只尊重用户手动展开/折叠态，绝不在刷新时强行展开。"""
        # 定位已有分组节点（不重建，避免顶层结构跳变）
        grp = None
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.text(0).startswith("🕘"):
                grp = it
                break
        recent_valid = [p for p in self._recent if os.path.isfile(p)]
        if not recent_valid:
            if grp is not None:
                self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(grp))
            return
        if grp is None:
            grp = QTreeWidgetItem(["🕘 最近打开"])
            f = grp.font(0)
            f.setBold(True)
            grp.setFont(0, f)
            grp.setExpanded(False)   # 默认折叠，且之后绝不自动展开
            self._tree.insertTopLevelItem(0, grp)
        # 子项顺序/内容未变化时不重建（避免无谓闪烁）
        desired = recent_valid[:_MAX_RECENT]
        if grp.childCount() == len(desired):
            unchanged = True
            for i, p in enumerate(desired):
                if grp.child(i).data(0, Qt.UserRole) != p:
                    unchanged = False
                    break
            if unchanged:
                return
        grp.takeChildren()
        for p in desired:
            is_comp = self._bp_type_of(p) == bp_kind.BP_TYPE_COMPONENT
            child = QTreeWidgetItem([self._type_label(p)])
            child.setData(0, Qt.UserRole, p)
            child.setData(0, Qt.UserRole + 1, "component" if is_comp else "bp")
            child.setToolTip(0, p)
            color = self._color_of(p)
            if color:
                child.setForeground(0, QBrush(QColor(color)))
            grp.addChild(child)

    # ============================================================
    # 资源树构建
    # ============================================================
    def _fill_dir(self, parent_item: QTreeWidgetItem, dir_path: str, depth: int,
                  skip: Optional[Set[str]] = None) -> int:
        """递归填充目录（剪枝隐藏目录，限制深度与条数）。返回新增条数。

        :param skip: 可选，已存在节点的绝对路径集合（normcase 后），用于「展开时补扫缺失项」。

        健壮性要点：历史上这里用 `sorted(os.scandir(...), key=lambda e: (not e.is_dir(), ...))`，
        而 key 会对每个 DirEntry 调一次 `e.is_dir()`。一旦目录里**任意一条** entry 在 is_dir()/is_file()
        上抛 OSError（坏的符号链接、权限不足、reparse point、网络盘掉线等），整个 sorted() 就抛异常，
        该目录会被当成空目录处理——于是「文件夹里有文件却整层都不显示」。现在把扫描与构建拆开，
        逐条 try/except，单条坏 entry 只会跳过自己，绝不会连累同目录其它文件。
        """
        if depth > _MAX_DEPTH:
            return 0
        # ---- 1) 先安全地把目录扫成「目录 / 文件」两类，逐条容错 ----
        dirs: list = []
        files: list = []
        try:
            it = os.scandir(dir_path)
        except OSError as e:
            self._scan_errors.append((dir_path, f"{type(e).__name__}: {e}"))
            return 0
        for ent in it:
            try:
                name = ent.name
                try:
                    is_dir = ent.is_dir()
                except OSError as e:
                    # 单条 entry 连「是不是目录」都读不出（坏符号链接/权限/脱机盘等）：
                    # 跳过它并记录，绝不让它连累同目录的其它文件一起消失。
                    self._scan_errors.append((os.path.join(dir_path, name), f"{type(e).__name__}: {e}"))
                    continue
                if is_dir:
                    if name.lower() in _PRUNE_DIRS or name.startswith("."):
                        continue
                    dirs.append(ent)
                else:
                    if name in _HIDE_FILES or ".backup_" in name.lower() or name.lower().endswith(".tmp"):
                        continue
                    files.append(ent)
            except OSError as e:
                # 单条 entry 异常：记录但继续，不破坏同目录其它条目
                self._scan_errors.append((os.path.join(dir_path, getattr(ent, "name", "?")), f"{type(e).__name__}: {e}"))
        # 先目录后文件，各自按名排序（与旧逻辑一致：dirs 在前）
        dirs.sort(key=lambda e: e.name.lower())
        files.sort(key=lambda e: e.name.lower())

        # ---- 2) 构建节点 ----
        # cap = 本目录自己的额度（不再吃全树累计额度，避免大目录饿死后面的目录）
        added = 0
        own = 0
        cap = max(0, min(_MAX_ITEMS_PER_DIR, self._budget))
        for ent in dirs:
            if own >= cap:
                break
            name = ent.name
            try:
                if skip is not None and os.path.normcase(os.path.abspath(ent.path)) in skip:
                    continue
                sub = QTreeWidgetItem([f"📁 {name}"])
                sub.setData(0, Qt.UserRole, ent.path)
                sub.setData(0, Qt.UserRole + 1, "dir")
                sub.setForeground(0, QBrush(QColor(_COLOR_DIR)))
                sub.setToolTip(0, ent.path)
                parent_item.addChild(sub)
                added += 1
                own += 1
                self._budget -= 1
                added += self._fill_dir(sub, ent.path, depth + 1)
                if sub.childCount() == 0 and name.lower() not in _KEEP_EMPTY_DIRS:
                    # 空目录只保留 resource/components/blueprints 这类约定目录，其余折叠不显示展开箭头
                    sub.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
                if ent.path in self._expanded:
                    sub.setExpanded(True)
            except OSError as e:
                self._scan_errors.append((ent.path, f"{type(e).__name__}: {e}"))
        for ent in files:
            if own >= cap:
                break
            try:
                if skip is not None and os.path.normcase(os.path.abspath(ent.path)) in skip:
                    continue
                name = ent.name
                low = name.lower()
                is_bp = low.endswith(".bp.json")
                is_img = low.endswith(_IMG_EXTS)
                if is_bp:
                    is_component = self._bp_type_of(ent.path) == bp_kind.BP_TYPE_COMPONENT
                    label = self._type_label(ent.path)
                    tip = (f"{ent.path}\n（{'组件蓝图：可被「调用组件」节点引用' if is_component else '普通蓝图'}）"
                           "\n右键可切换类型")
                elif is_img:
                    label = f"🖼 {name}"
                    tip = ent.path
                elif low.endswith(".md"):
                    label = f"📘 {name}"
                    tip = ent.path
                else:
                    label = f"📄 {name}"
                    tip = ent.path
                it = QTreeWidgetItem([label])
                it.setData(0, Qt.UserRole, ent.path)
                it.setData(0, Qt.UserRole + 1,
                            ("component" if self._bp_type_of(ent.path) == bp_kind.BP_TYPE_COMPONENT else "bp")
                            if is_bp else "file")
                it.setToolTip(0, tip)
                color = self._color_of(ent.path)
                if color:
                    it.setForeground(0, QBrush(QColor(color)))
                parent_item.addChild(it)
                added += 1
                own += 1
                self._budget -= 1
            except OSError as e:
                self._scan_errors.append((ent.path, f"{type(e).__name__}: {e}"))
        # 截断提示：明确告诉用户「还有内容没显示」，而不是让它看起来像空目录
        total_entries = len(dirs) + len(files)
        if cap > 0 and own >= cap and total_entries > own:
            ph = QTreeWidgetItem([f"… 还有 {total_entries - own} 项未显示（目录内容过多）"])
            ph.setData(0, Qt.UserRole + 1, "trunc")
            ph.setForeground(0, QBrush(QColor(_COLOR_DIR)))
            ph.setToolTip(0, f"{dir_path}\n单目录最多显示 {_MAX_ITEMS_PER_DIR} 项")
            parent_item.addChild(ph)
        return added

    def _ensure_children(self, item: QTreeWidgetItem) -> None:
        """展开目录时的兜底补扫：保证「双击目录一定能看到里面的文件」。

        背景：树的子节点是在 refresh() 时一次性递归建好的。一旦某次构建因为条数上限截断、
        扫描竞态、或目录当时还没写入内容，节点就是空的——而双击只会切换展开状态、不会补建节点，
        于是表现为「双击这个目录也什么都不显示」。这里在展开时就地补扫一次缺失项。
        """
        if item is None:
            return
        if item.data(0, Qt.UserRole + 1) != "dir":
            return
        dir_path = item.data(0, Qt.UserRole)
        if not dir_path or not os.path.isdir(dir_path):
            return
        # 摘掉上一轮留下的「… 还有 N 项未显示」占位节点
        for i in range(item.childCount() - 1, -1, -1):
            if item.child(i).data(0, Qt.UserRole + 1) == "trunc":
                item.takeChild(i)
        skip = set()
        for i in range(item.childCount()):
            p = item.child(i).data(0, Qt.UserRole)
            if p:
                skip.add(os.path.normcase(os.path.abspath(p)))
        # 补扫只针对本目录，给一份独立的额度，不受整树余额影响
        saved = self._budget
        self._budget = _MAX_ITEMS_PER_DIR
        try:
            self._fill_dir(item, dir_path, depth=2, skip=skip)
        except Exception:
            pass
        finally:
            self._budget = saved
        if item.childCount() == 0:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        else:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenEmpty)

    # ============================================================
    # 交互
    # ============================================================
    def _current_target_dir(self) -> str:
        """新建资源的目标目录：选中目录→它本身；选中文件→所在目录；没选→工程根。"""
        for p in self._selected_paths():
            if os.path.isdir(p):
                return p
        for p in self._selected_paths():
            if os.path.isfile(p):
                return os.path.dirname(p)
        return self._project_root or ""

    def _on_head_button(self, kind: str):
        if kind == "refresh":
            self.refresh()
            return
        if kind == "cleanup":
            if not self._project_root:
                self._footer.setText("（请先新建/打开工程，才能清理资源）")
                return
            self.request_cleanup_assets.emit()
            return
        if not self._project_root:
            self._footer.setText("（请先新建/打开工程，才能创建资源）")
            return
        target = self._current_target_dir()
        if not target:
            self._footer.setText("（当前没有可用目录，请先在树里选一个文件夹）")
            return
        self.request_new.emit(kind, target)

    def _on_item_expanded(self, item: QTreeWidgetItem):
        p = item.data(0, Qt.UserRole)
        if p:
            self._expanded.add(p)
        # 展开时补扫一次：即便建树时被截断/竞态漏掉，也能在这儿补上，保证双击一定看得到文件
        try:
            self._ensure_children(item)
        except Exception:
            pass

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
            # 先补扫再展开：目录里真有文件时，双击一定能看到（不再出现「双击也空空如也」）
            try:
                self._ensure_children(item)
            except Exception:
                pass
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
                _act("🧹 清理未使用资源…", lambda: self.request_cleanup_assets.emit())
                _act("📂 在资源管理器中打开", lambda: self.reveal_path.emit(path))
            else:
                is_bp = path.lower().endswith(".bp.json")
                _act("打开蓝图", lambda: self.open_blueprint.emit(path), is_bp)
                _act("💾 保存（当前蓝图）", lambda: self.save_current.emit(), is_bp and self._same_path(path, self._current_path))
                if is_bp:
                    to_component = self._bp_type_of(path) != bp_kind.BP_TYPE_COMPONENT
                    _act(
                        "🧩 设为组件蓝图" if to_component else "🗺 设为普通蓝图",
                        lambda p=path, t=(
                            bp_kind.BP_TYPE_COMPONENT if to_component else bp_kind.BP_TYPE_BLUEPRINT
                        ): self.request_set_bp_type.emit(p, t),
                    )
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
            _act("🧩 将当前蓝图另存为组件…", lambda: self.save_as_component.emit(), bool(self._current_path))
            _act("🧹 清理未使用资源…", lambda: self.request_cleanup_assets.emit(), has_proj)
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
