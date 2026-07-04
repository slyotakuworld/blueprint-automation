import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QSize, Signal, QEvent, QStandardPaths
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...config.app_paths import get_app_dir, screenshot_dir, template_dir
from ...automation.cv.splitter_integration import (
    list_elements_in_dir,
    run_splitter_on_image_bgr,
)
from ...tools.window_utils import write_png_cn


def _bgr_to_qpixmap(bgr_img: np.ndarray, max_width: Optional[int] = None) -> QPixmap:
    h, w = bgr_img.shape[:2]
    if max_width and w > max_width:
        scale = max_width / w
        new_w, new_h = max_width, int(h * scale)
        resized = cv2.resize(bgr_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        resized = bgr_img
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    h2, w2, ch = rgb.shape
    qimg = QImage(rgb.data, w2, h2, ch * w2, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


def _read_bgr_file(path: str) -> Optional[np.ndarray]:
    try:
        if not path or not os.path.isfile(path):
            return None
        arr = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


# ============================================================
# 左侧完整图里的"元素矩形"：点击可切换 enabled / 也可联动右侧
# ============================================================
class ElementRectItem(QGraphicsObject):
    clicked = Signal(object)  # 把自己传出去

    def __init__(self, element: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._e = element
        x, y, w, h = float(element["x"]), float(element["y"]), float(element["w"]), float(element["h"])
        self._rect = QRectF(x, y, w, h)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._hover = False

    # ---- QGraphicsObject mandatory ----
    def boundingRect(self) -> QRectF:
        pad = 2.0
        return self._rect.adjusted(-pad, -pad, pad, pad)

    def paint(self, painter: QPainter, option, widget=None):
        enabled = bool(self._e.get("enabled", True))
        if enabled:
            if self._hover:
                color = QColor(0, 255, 80, 255)
                fill = QColor(0, 255, 80, 60)
            else:
                color = QColor(0, 220, 0, 255)
                fill = QColor(0, 220, 0, 40)
            pen_w = 2
        else:
            color = QColor(100, 100, 100, 220)
            fill = QColor(0, 0, 0, 140)  # 深灰半透明遮罩：视觉上就是"隐藏/取消"了
            pen_w = 1
        pen = QPen(color)
        pen.setWidth(pen_w)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRect(self._rect)
        # 左上小角标：idx
        painter.setPen(color)
        f = painter.font()
        f.setBold(True)
        f.setPointSize(10)
        painter.setFont(f)
        tag = f"#{int(self._e.get('idx', -1)):03d}"
        if self._e.get("manual"):
            tag += " ✎"
        painter.drawText(self._rect.adjusted(2, 2, -2, -2), Qt.AlignLeft | Qt.AlignTop, tag)

    # ---- Events ----
    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()
        super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(e)

    def mousePressEvent(self, e: QGraphicsSceneMouseEvent):
        changed = False
        if e.button() == Qt.LeftButton:
            # 左键 = 明确保留（enabled=True）
            if not bool(self._e.get("enabled", True)):
                self._e["enabled"] = True
                changed = True
            else:
                self.update()
        elif e.button() == Qt.RightButton:
            # 右键 = 明确取消（enabled=False）
            if bool(self._e.get("enabled", True)):
                self._e["enabled"] = False
                changed = True
            else:
                self.update()
        if changed:
            self.update()
            self.clicked.emit(self._e)
            return
        super().mousePressEvent(e)


# ============================================================
# 切分工具主窗口
# ============================================================
class ElementSplitDialog(QDialog):
    """
    UI元素切分面板（极简版）：
      - 顶栏：📂 选择图片  +  ☑ 手动拆分
      - 左：完整图（点击矩形=保留/取消；勾选手动拆分可拖拽加元素）
      - 右：碎图列表（每个元素自带Checkbox保留/取消，与左侧双向联动）
      - 底：💾保存模板 / 🔎生成 FindImage / 📥回填当前节点 / 关闭
    """

    def __init__(self, hooks=None, parent=None, initial_hwnd=None):
        super().__init__(parent)
        self.setWindowTitle("✂ 切分UI元素（仅负责切图）")
        self.resize(1320, 840)
        self._hooks = hooks or {}
        self._initial_hwnd = initial_hwnd if initial_hwnd else None

        self._src_bgr: Optional[np.ndarray] = None
        self._split_result: Dict[str, Any] = {}
        self._elements: List[Dict[str, Any]] = []
        self._filter_mode: str = "all"  # 筛选模式：all/checked/unchecked/custom，默认=全部
        self._current_idx = -1
        self._last_path: str = ""
        self._rect_items: List[ElementRectItem] = []

        # 手动拆分
        self._manual_drag_from: Optional[QPointF] = None
        self._manual_drag_to: Optional[QPointF] = None
        self._manual_drag_item: Optional[QGraphicsRectItem] = None

        self._build_ui()

    # ============================================================
    # UI build
    # ============================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ===== 顶部工具栏（极简：只有 选择图片 + 重新自动切分） =====
        bar = QHBoxLayout()

        btn_file = QPushButton("📂 选择图片")
        btn_file.setMinimumHeight(32)
        btn_file.clicked.connect(self._on_open_file)
        bar.addWidget(btn_file)

        bar.addSpacing(10)

        btn_split_again = QPushButton("↻ 对当前源图重新自动切分")
        btn_split_again.clicked.connect(self._re_run_splitter)
        bar.addWidget(btn_split_again)

        bar.addSpacing(10)

        btn_cleanup_cache = QPushButton("🗑 清理无效碎图缓存")
        btn_cleanup_cache.setToolTip(
            "扫描 screenshots 下所有自动切分生成的碎图目录，\n"
            "如果某个目录的源图文件（.source.json 里记录的路径）已经不存在，\n"
            "或者拷贝到 screenshots/imported_images 的源图已经不在磁盘上，\n"
            "就删除该碎图目录，避免缓存越积越大。"
        )
        btn_cleanup_cache.clicked.connect(self._on_cleanup_orphan_caches)
        bar.addWidget(btn_cleanup_cache)

        bar.addStretch(1)

        self._lbl_info = QLabel("（还没有加载图片：点📂选择图片 / 或在主界面选中目标后按全局热键）")
        self._lbl_info.setStyleSheet("color:#8B949E;")
        self._lbl_info.setToolTip("加载源图后可把鼠标悬停在这里查看源图的完整绝对路径。")
        bar.addWidget(self._lbl_info)

        root.addLayout(bar)

        # ===== 中间：QSplitter =====
        split = QSplitter(Qt.Horizontal)

        # --- 左：完整图预览 ---
        left_wrap = QWidget()
        left_l = QVBoxLayout(left_wrap)
        left_l.setContentsMargins(0, 0, 0, 0)

        header = QLabel("<b>🖼 完整图（左键矩形 = 保留 / 右键矩形 = 取消；可直接拖拽画框新增元素）</b>")
        header.setStyleSheet("padding:4px 2px;")
        left_l.addWidget(header)

        self._preview_scene = QGraphicsScene(self)
        self._preview_view = QGraphicsView(self._preview_scene)
        self._preview_view.setRenderHints(
            self._preview_view.renderHints()
            | QPainter.Antialiasing
            | QPainter.SmoothPixmapTransform
        )
        # 手动拆分默认始终开启：DragMode用NoDrag但通过eventFilter自己处理拖拽（避免默认拖拽）
        self._preview_view.setDragMode(QGraphicsView.NoDrag)
        self._preview_view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._preview_view.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        left_l.addWidget(self._preview_view, 1)

        self._lbl_cnt = QLabel("元素总数：0   |   保留：0   |   取消：0   |   手动：0")
        self._lbl_cnt.setStyleSheet("color:#8B949E;padding:4px 2px;")
        left_l.addWidget(self._lbl_cnt)

        split.addWidget(left_wrap)

        # --- 右：碎图预览（网格卡片：左键保留 / 右键取消） ---
        right_wrap = QWidget()
        right_l = QVBoxLayout(right_wrap)
        right_l.setContentsMargins(0, 0, 0, 0)

        header2 = QLabel("<b>🧩 碎图列表（左键卡片 = 选中+保留 / 右键卡片 = 选中+取消）</b>")
        header2.setStyleSheet("padding:4px 2px;")
        right_l.addWidget(header2)

        self._list = QListWidget()
        self._list.setViewMode(QListWidget.IconMode)
        self._list.setResizeMode(QListWidget.Fixed)
        self._list.setMovement(QListWidget.Static)
        self._list.setIconSize(QSize(110, 110))
        self._list.setGridSize(QSize(150, 180))
        self._list.setSpacing(8)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setUniformItemSizes(True)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.setMinimumWidth(480)
        self._list.itemClicked.connect(self._on_right_item_clicked)
        self._list.itemDoubleClicked.connect(self._on_right_item_double_clicked)
        self._list.customContextMenuRequested.connect(self._on_right_item_context_menu)
        right_l.addWidget(self._list, 1)

        # 元素详情标签：固定高度/最大高度，防止路径文本把右侧撑大、挤压左侧
        self._lbl_item = QLabel("（未选择元素）")
        self._lbl_item.setWordWrap(True)
        self._lbl_item.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._lbl_item.setMinimumHeight(54)
        self._lbl_item.setMaximumHeight(74)
        self._lbl_item.setStyleSheet(
            "padding:8px;background:#0D1117;border:1px solid #30363D;border-radius:6px;"
        )
        right_l.addWidget(self._lbl_item)

        # 底部操作按钮（移除"切换当前元素"按钮，左右键已直接操作）
        row1 = QHBoxLayout()
        self._btn_save_tpl = QPushButton("💾 保存PNG")
        self._btn_save_tpl.clicked.connect(self._on_save_template_png)
        self._btn_as_find = QPushButton("🔎 生成节点")
        self._btn_as_find.clicked.connect(self._on_create_findimage_node)
        row1.addWidget(self._btn_save_tpl)
        row1.addWidget(self._btn_as_find)
        right_l.addLayout(row1)

        # 右侧底部：批量操作（筛选 + 全选 / 全不选 / 导出选中）
        right_l.addSpacing(10)
        # 第 1 行：🧰 批量操作 标题（左）+ 🔍 筛选下拉框（右）—— 与标题同一行
        batch_header_row = QHBoxLayout()
        batch_lbl = QLabel("<b>🧰 批量操作</b>")
        batch_lbl.setStyleSheet("padding:2px;")
        batch_header_row.addWidget(batch_lbl)
        batch_header_row.addStretch(1)  # 把筛选挤到最右边
        filter_lbl = QLabel("🔍 筛选:")
        filter_lbl.setStyleSheet("color:#C9D1D9; padding:0 4px 0 8px;")
        self._cmb_filter = QComboBox()
        self._cmb_filter.addItem("� 全部", "all")
        self._cmb_filter.addItem("✅ 仅勾选（保留）", "checked")
        self._cmb_filter.addItem("🚫 仅不勾选（取消）", "unchecked")
        self._cmb_filter.addItem("✎ 仅自定义切图", "custom")
        self._cmb_filter.setToolTip(
            "仅影响右侧🧩碎图卡片的显示（不改变元素本身的保留/取消状态，也不影响全选/全不选）：\n"
            "  · 全部 → 显示所有自动切出来的 + 手动拖出来的\n"
            "  · 仅勾选 → 只看左侧有蓝色矩形=开的那些\n"
            "  · 仅不勾选 → 只看左侧没蓝色矩形=关的那些\n"
            "  · 仅自定义切图 → 只看你在左侧手动拖拽画出来的（右下角显示 ✎ 标记）"
        )
        self._cmb_filter.setCurrentIndex(0)  # 默认选「全部」
        self._cmb_filter.currentIndexChanged.connect(self._on_filter_changed)
        filter_wrapper = QHBoxLayout()
        filter_wrapper.setContentsMargins(0, 0, 0, 0)
        filter_wrapper.addWidget(filter_lbl)
        filter_wrapper.addWidget(self._cmb_filter)
        batch_header_row.addLayout(filter_wrapper)
        right_l.addLayout(batch_header_row)

        batch_row1 = QHBoxLayout()
        btn_sel_all = QPushButton("✅ 全选（保留全部）")
        btn_sel_all.setToolTip("把右侧🧩碎图列表里所有元素设为【保留=开启】（相当于点一遍每个元素的左键）")
        btn_sel_all.clicked.connect(self._on_select_all_elements)
        btn_sel_none = QPushButton("❌ 全不选（取消全部）")
        btn_sel_none.setToolTip("把所有元素设为【取消=关闭】（相当于点一遍每个元素的右键）")
        btn_sel_none.clicked.connect(self._on_select_none_elements)
        batch_row1.addWidget(btn_sel_all)
        batch_row1.addWidget(btn_sel_none)
        right_l.addLayout(batch_row1)

        btn_export_sel = QPushButton("💾 导出选中的碎图")
        btn_export_sel.setMinimumHeight(34)
        btn_export_sel.setStyleSheet(
            "QPushButton{background:#1F6FEB;color:#fff;border:1px solid #388BFD;border-radius:6px;padding:6px 10px;font-weight:600;}"
            "QPushButton:hover{background:#2F7CF5;}"
        )
        btn_export_sel.setToolTip(
            "导出所有【被标记为保留（左侧有蓝色矩形=开）】的碎图为PNG。\n"
            "⚠️ 每次导出都必须重新选择一个导出目录（不记忆），没选目录不给导出。"
        )
        btn_export_sel.clicked.connect(self._on_export_selected_elements)
        right_l.addWidget(btn_export_sel)

        split.addWidget(right_wrap)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 7)
        split.setStretchFactor(1, 5)
        # 给左右两栏设最小宽度，防止某一侧被挤压
        left_wrap.setMinimumWidth(560)
        right_wrap.setMinimumWidth(500)
        # 初始比例：左略大于右（预览完整图需要更大空间）
        split.setSizes([900, 500])
        self._split = split
        root.addWidget(split, 1)

        # 预览 scene 事件过滤器（用于手动拆分的鼠标事件）
        self._preview_view.viewport().installEventFilter(self)

        # 兼容：如果主界面传了 initial_hwnd，自动抓当前目标窗口一帧切分（用户习惯Ctrl+Shift+X打开就看到图）
        # ⚠️ 修复：不再用假字符串"(target hwnd auto capture)"当source_path——抓完必须真实存PNG到 screenshots/autocap_autosave/ 下
        #    再把真实磁盘路径传给 load_and_split_bgr，这样：
        #      1) 用户能找到源图在哪（有真实文件在screenshots下）
        #      2) 下次再打同一张图（或缓存命中逻辑）能正确匹配 .source.json
        if self._initial_hwnd:
            try:
                from ...tools.window_utils import capture_hwnd_bgr
                res = capture_hwnd_bgr(int(self._initial_hwnd), prefer_front=True)
                if res is not None:
                    bgr, _, hwnd_used = res
                    real_png = self._save_autocap_bgr_to_png(bgr, hwnd_used or self._initial_hwnd)
                    self.load_and_split_bgr(bgr, source_path_hint=real_png)
            except Exception:
                pass

    def _save_autocap_bgr_to_png(self, bgr: np.ndarray, hwnd_used: Any) -> str:
        """initial_hwnd / 对话框内📸按钮 抓到的 BGR → 真实存一份PNG到 screenshots/autocap_autosave/，返回绝对路径（避免用户找不到源图）"""
        import datetime as _dt
        ss_root = screenshot_dir()
        out_dir = os.path.join(ss_root, "autocap_autosave")
        os.makedirs(out_dir, exist_ok=True)
        hwnd_hex = 0
        try:
            hwnd_hex = int(hwnd_used) & 0xFFFFFFFF
        except Exception:
            hwnd_hex = 0
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        fname = f"autocap_HWND{hwnd_hex:08X}_{ts}.png"
        target = os.path.join(out_dir, fname)
        try:
            from ...tools.window_utils import write_png_cn
            ok = write_png_cn(target, bgr[:, :, ::-1])
            if ok and os.path.isfile(target):
                return os.path.abspath(target)
        except Exception:
            pass
        # 兜底：opencv直接写（可能有中文路径问题，但路径全ASCII应该没问题）
        try:
            cv2.imwrite(target, bgr)
            if os.path.isfile(target):
                return os.path.abspath(target)
        except Exception:
            pass
        # 最终兜底：即使存失败，也返回一个合法的（不存在也没关系，至少source_path是真实文件名）
        return os.path.abspath(target)

    def _apply_source_ui_state(self, source_path: Optional[str]):
        """每次成功加载源图后调用：给 lbl_info 加完整绝对路径的 tooltip 方便用户看完整路径"""
        p = source_path or (self._last_path if self._last_path else "")
        if p and os.path.isfile(p):
            self._lbl_info.setToolTip(f"当前源图完整路径：\n{p}")
        else:
            self._lbl_info.setToolTip("（还没加载源图 / 源图文件不在磁盘上）")

    # ============================================================
    # 切分缓存管理（避免重复生成碎图目录导致缓存爆炸）
    # ============================================================
    @staticmethod
    def _norm_path(p: Optional[str]) -> str:
        if not p:
            return ""
        return os.path.normcase(os.path.abspath(p))

    @staticmethod
    def _write_source_info_file(out_dir: str, source_path: Optional[str]):
        """在新生成的碎图目录里写一个 .source.json，记录源图绝对路径 + 保存时间"""
        if not out_dir:
            return
        os.makedirs(out_dir, exist_ok=True)
        import datetime as _dt
        data = {
            "source_path": os.path.abspath(source_path) if source_path else "",
            "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        fpath = os.path.join(out_dir, ".source.json")
        try:
            import json as _json
            with open(fpath, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ElementSplitDialog] 写.source.json 失败 {fpath}: {e}")

    @staticmethod
    def _read_source_info(out_dir: str) -> Optional[dict]:
        fpath = os.path.join(out_dir, ".source.json")
        if not os.path.isfile(fpath):
            return None
        try:
            import json as _json
            with open(fpath, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return None

    @staticmethod
    def _dir_size_bytes(d: str) -> int:
        total = 0
        try:
            for root, dirs, files in os.walk(d):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    @staticmethod
    def _iter_split_cache_dirs() -> List[str]:
        """枚举 screenshots 下所有以 split_elements_ 开头的碎图目录（自动切分产物）"""
        root = screenshot_dir()
        out: List[str] = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_dir() and entry.name.startswith("split_elements_"):
                        out.append(entry.path)
        except OSError:
            pass
        return out

    @staticmethod
    def _default_export_dir() -> str:
        """导出碎图时默认起始目录：优先下载 → 桌面 → 系统图片目录 → 用户主目录；绝不使用 screenshots 内部目录，避免污染截图缓存。"""
        candidates = []
        try:
            down = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
            if down: candidates.append(down)
        except Exception:
            pass
        try:
            desk = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
            if desk: candidates.append(desk)
        except Exception:
            pass
        try:
            pics = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
            if pics: candidates.append(pics)
        except Exception:
            pass
        home = os.path.expanduser("~")
        if home: candidates.append(home)
        for c in candidates:
            if c and os.path.isdir(c):
                return c
        # 兜底：系统临时目录
        import tempfile as _tf
        return _tf.gettempdir()

    # ============================================================
    # 碎图缓存复用：同一张PNG再次打开不再重复切分（真正点"重新切分"才会重算）
    # ============================================================
    def _find_existing_split_dir_for_source(self, source_path: Optional[str]) -> Optional[str]:
        """
        在 screenshots/split_elements_* 目录里寻找「.source.json.source_path 和 source_path 归一化后相同」的缓存目录。
        找到多个时取 saved_at 最晚的一个；找不到返回 None。
        """
        if not source_path:
            return None
        target = self._norm_path(source_path)
        if not target:
            return None
        best_dir: Optional[str] = None
        best_ts = None
        import datetime as _dt
        for d in self._iter_split_cache_dirs():
            info = self._read_source_info(d)
            if not info:
                continue
            src = self._norm_path(info.get("source_path") or "")
            if src != target:
                continue
            ts_s = info.get("saved_at") or ""
            try:
                ts = _dt.datetime.fromisoformat(ts_s)
            except Exception:
                try:
                    ts = _dt.datetime.fromtimestamp(os.path.getmtime(d))
                except Exception:
                    ts = _dt.datetime(1970, 1, 1)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_dir = d
        return best_dir

    def _load_from_existing_split_dir(self, split_dir: str, source_path: Optional[str],
                                       fallback_bgr: Optional[np.ndarray] = None) -> bool:
        """
        复用既有 split_elements_* 缓存目录 → 直接从磁盘读图和元素，不重新跑切分算法。
        加载成功返回 True，失败（目录缺PNG/源图读不出来）返回 False。
        """
        if not split_dir or not os.path.isdir(split_dir):
            return False
        # 1. 读源图 BGR：优先从 source_path 读磁盘（保证与真实文件一致）；失败才 fallback_bgr
        bgr = None
        actual_source = source_path or ""
        if actual_source and os.path.isfile(actual_source):
            bgr = _read_bgr_file(actual_source)
        if bgr is None and fallback_bgr is not None:
            bgr = fallback_bgr
            if not actual_source:
                actual_source = ""
        if bgr is None:
            return False
        # 2. 列出目录里的元素PNG（必须至少有1个才能用）
        files = list_elements_in_dir(split_dir)
        if not files:
            return False
        # 3. 填 self._elements
        elements: List[Dict[str, Any]] = []
        for f in files:
            e = {
                "idx": int(f.get("idx", -1)),
                "x": int(f.get("x", 0)),
                "y": int(f.get("y", 0)),
                "w": int(f.get("w", 0)),
                "h": int(f.get("h", 0)),
                "area": int(f.get("w", 0)) * int(f.get("h", 0)),
                "path": f.get("path"),
                "crop": None,
                "enabled": True,
                "manual": False,
            }
            if e["idx"] < 0 or e["w"] <= 0 or e["h"] <= 0:
                continue
            elements.append(e)
        if not elements:
            return False
        # 4. 赋值内部状态
        self._src_bgr = bgr
        self._last_path = actual_source
        self._split_result = {
            "ok": True,
            "output_dir": split_dir,
            "source_path": actual_source,
            "cached": True,
            "elements": list(elements),
        }
        # 5. 补 crop（没 path 的或没 crop 的从原图切）
        by_idx = {f["idx"]: f["path"] for f in files}
        for e in elements:
            p = by_idx.get(e.get("idx", -1))
            if p:
                e["path"] = p
            if e.get("crop") is None:
                e["crop"] = self._crop_from_src(e["x"], e["y"], e["w"], e["h"])
        self._elements = elements
        # 6. 渲染
        self._render_scene()
        self._render_right_list()
        self._update_cnt_label()
        h, w = bgr.shape[:2]
        base = f"源图 {w}x{h}  加载缓存切分结果 {len(elements)} 个元素（左键=保留  右键=取消）"
        if actual_source:
            base += f"   源图：{os.path.basename(actual_source)}"
        self._lbl_info.setText(base)
        self._apply_source_ui_state(actual_source)
        return True

    def _cleanup_split_caches_for_source(self, source_path: Optional[str],
                                         exclude_dir: Optional[str] = None) -> List[str]:
        """删除 screenshots 下「source_path 相同」的旧碎图缓存目录（保留刚新建的 exclude_dir）"""
        if not source_path:
            return []
        target = self._norm_path(source_path)
        exclude_norm = self._norm_path(exclude_dir) if exclude_dir else ""
        deleted: List[str] = []
        for d in self._iter_split_cache_dirs():
            if self._norm_path(d) == exclude_norm:
                continue
            info = self._read_source_info(d) or {}
            if self._norm_path(info.get("source_path")) == target:
                try:
                    import shutil as _sh
                    _sh.rmtree(d, ignore_errors=True)
                    deleted.append(d)
                except Exception as e:
                    print(f"[ElementSplitDialog] 清理旧缓存失败 {d}: {e}")
        return deleted

    def _cleanup_orphan_split_caches(self) -> Tuple[List[str], int]:
        """
        清理 screenshots 下 split_elements_* 开头的垃圾/冗余碎图缓存目录（释放空间）。
        返回 (删掉的目录列表, 总释放字节数)。

        规则（按执行顺序）：
        ① 根本没有 .source.json（来路不明，可能是用户在资源管理器手动复制的「XXX - 副本」「解压缩产物」） → 直接删。
        ② .source.json 里记录的源图文件已经不存在（源图被删了，碎图也没用了） → 删。
        ③ 多个目录的 .source.json 指向同一个源图路径 → 只保留 saved_at（或目录mtime）最晚的 1 个，其余全部删除（
           用来干掉同一张源图反复切出来的历史缓存，以及用户手动复制出来的「split_elements_XXX - 副本」）。
        """
        deleted_dirs: List[str] = []
        freed_bytes = 0

        def _remove_dir(d: str):
            nonlocal freed_bytes
            try:
                freed_bytes += self._dir_size_bytes(d)
                import shutil as _sh
                _sh.rmtree(d, ignore_errors=True)
                deleted_dirs.append(d)
            except Exception as e:
                print(f"[ElementSplitDialog] 清理缓存失败 {d}: {e}")

        # 把所有 split_elements_* 目录按"源图path"分组；同时记录一份排序键（saved_at/目录mtime）
        all_dirs = self._iter_split_cache_dirs()
        import datetime as _dt

        def _info_sort_key(info_d):
            saved_s = (info_d.get("saved_at") or "") if isinstance(info_d, dict) else ""
            try:
                return _dt.datetime.fromisoformat(saved_s)
            except Exception:
                # fallback: 目录本身的修改时间
                try:
                    return _dt.datetime.fromtimestamp(os.path.getmtime(info_d.get("__dir_path__")))
                except Exception:
                    return _dt.datetime(1970, 1, 1)

        # 分组：同 source_path 的放一组；无 source.json 的单独一组（rule ① 直接全删）
        by_source: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        no_source_json_dirs: List[str] = []
        for d in all_dirs:
            info = self._read_source_info(d)
            if info is None:
                no_source_json_dirs.append(d)
                continue
            # rule ①之外也要用 saved_at，为后续 rule ③ 比较新旧，补充 __dir_path__ 用 fallback
            info["__dir_path__"] = d
            src = self._norm_path(info.get("source_path") or "")
            by_source.setdefault(src, []).append((d, info))

        # Rule ①：无 .source.json → 全部删除
        for d in no_source_json_dirs:
            _remove_dir(d)

        # Rule ②+③：对每个source分组，先检查"源图是否还在"（若不在则整组都可以清理，或保留一个最新也行——用户要删缓存那就整组清）
        for src, entries in by_source.items():
            # entries: List[(dir_path, info_dict)]
            # 先按排序键从新→旧排序（第一个=最新）
            entries_sorted = sorted(entries, key=lambda kv: _info_sort_key(kv[1]), reverse=True)
            src_exists = bool(src and os.path.isfile(src))

            if not src_exists:
                # Rule ②：源图不存在，整组（包括最新）都没有存在价值 → 全删
                for d, _ in entries_sorted:
                    _remove_dir(d)
            else:
                # Rule ③：源图还在，但用户可能有多个副本（-副本 / 重切N次） → 只留最新那个，其余全部删除
                first = True
                for d, _ in entries_sorted:
                    if first:
                        first = False
                        continue  # 保留最新的
                    _remove_dir(d)

        return deleted_dirs, freed_bytes

    def _on_cleanup_orphan_caches(self):
        """按钮：一键清理无效碎图缓存"""
        dirs, freed = self._cleanup_orphan_split_caches()
        if not dirs:
            QMessageBox.information(self, "清理结果", "没有检测到需要清理的无效碎图目录（全部源图都健在）。")
            return
        mb = round(freed / (1024 * 1024), 2)
        msg = f"已清理 {len(dirs)} 个无效碎图目录，释放 ≈ {mb} MB：\n\n"
        msg += "\n".join(f"  · {d}" for d in dirs[:12])
        if len(dirs) > 12:
            msg += f"\n  · ……以及另外 {len(dirs) - 12} 个目录"
        QMessageBox.information(self, "清理完成", msg)

    # ============================================================
    # 公共 API（供热键/外部调用）
    # ============================================================
    def load_and_split_bgr(self, bgr_ndarray, source_path_hint: Optional[str] = None) -> bool:
        """
        外部直接传 BGR ndarray → 优先复用同源的split缓存目录；缓存不存在才会重新自动切分 + 渲染。
        不弹提示框，失败返回 False。
        """
        if bgr_ndarray is None:
            return False
        try:
            self._last_path = source_path_hint or ""
            # 先尝试复用已有 split_elements_* 缓存（节省CPU+不新建目录）
            cached_dir = self._find_existing_split_dir_for_source(source_path_hint) if source_path_hint else None
            if cached_dir:
                ok = self._load_from_existing_split_dir(cached_dir, source_path_hint,
                                                         fallback_bgr=bgr_ndarray.copy())
                if ok:
                    return True
            # 没命中缓存 → 重新切分（新建目录）
            self._run_split(bgr_ndarray.copy(), source_path=source_path_hint)
            return self._split_result and bool(self._split_result.get("ok", False))
        except Exception as e:
            print(f"[ElementSplitDialog] load_and_split_bgr 失败: {e}")
            return False

    def current_elements(self, only_enabled: bool = False) -> List[Dict[str, Any]]:
        if only_enabled:
            return [e for e in (self._elements or []) if e.get("enabled", True)]
        return list(self._elements or [])

    # ============================================================
    # 顶栏按钮
    # ============================================================
    @staticmethod
    def _fast_file_hash(path: str, max_read_bytes: int = 2 * 1024 * 1024) -> str:
        """
        给图片文件快速算个"内容指纹"：大小 + md5(最多读前2MB + 末尾1MB + 中间1MB)。
        用于判断两次选的外部PNG是不是**完全同一文件内容**：同内容就复用imported copy+复用split缓存，
        不会每次生成新copy导致split缓存对不上。
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            return "ERR"
        try:
            import hashlib
            h = hashlib.md5()
            h.update(str(size).encode("ascii"))
            to_read = min(max_read_bytes, size)
            with open(path, "rb") as f:
                head = f.read(to_read // 3)
                h.update(head)
                if size > max_read_bytes:
                    try:
                        f.seek(size - to_read // 3)
                        tail = f.read(to_read // 3)
                        h.update(tail)
                        f.seek(size // 2)
                        mid = f.read(to_read // 3)
                        h.update(mid)
                    except OSError:
                        pass
                else:
                    rest = f.read()
                    h.update(rest)
            return h.hexdigest()
        except Exception:
            return f"sz_{size}"

    def _find_imported_copy_by_content(self, import_dir: str, src_path: str) -> Optional[str]:
        """
        在 screenshots/imported_images 目录里找"和src_path文件内容完全相同"的既有copy：
        先通过文件大小快速过滤，再用 _fast_file_hash 比对内容。命中就返回该copy的绝对路径，否则None。
        """
        if not import_dir or not os.path.isdir(import_dir):
            return None
        if not src_path or not os.path.isfile(src_path):
            return None
        try:
            target_size = os.path.getsize(src_path)
        except OSError:
            return None
        target_hash = self._fast_file_hash(src_path)
        try:
            with os.scandir(import_dir) as it:
                entries = list(it)
        except OSError:
            return None
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size != target_size:
                continue
            # 大小一致 → 再比内容哈希（大概率同内容）
            cand = e.path
            if self._fast_file_hash(cand) == target_hash:
                return cand
        return None

    def _on_open_file(self):
        default_dir = screenshot_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择PNG/JPG图片", default_dir,
            "图片文件 (*.png *.jpg *.jpeg *.bmp)",
        )
        if not path or not os.path.isfile(path):
            return
        use_path = path
        # 如果不在 screenshots 子目录下 → 拷贝一份到 screenshots/imported_images/ 再处理
        ss_root_norm = self._norm_path(default_dir)
        chosen_norm = self._norm_path(path)
        if not (chosen_norm == ss_root_norm or chosen_norm.startswith(ss_root_norm + os.sep)):
            import_dir = os.path.join(default_dir, "imported_images")
            os.makedirs(import_dir, exist_ok=True)
            # 1) 先看 imported_images 里已经有同内容的 copy 没？有就直接复用，不重复拷贝（不重复命名→保证use_path不变，下次还能命中split缓存）
            existing_copy = self._find_imported_copy_by_content(import_dir, path)
            if existing_copy and os.path.isfile(existing_copy):
                use_path = existing_copy
            else:
                # 2) 没命中，才新建拷贝；文件名用「原basename_内容哈希前10位.ext」，不用当前时间戳！
                #    作用：如果同内容源图因为"扫imported失败"（比如imported目录临时挂了）再进来，
                #    target_name 依然会和上次生成的完全相同 → 不会重复写多份；也保证 use_path 稳定。
                base, ext = os.path.splitext(os.path.basename(path))
                content_hash_10 = self._fast_file_hash(path)[:10]
                target_name = f"{base}_{content_hash_10}{ext}"
                target_path = os.path.join(import_dir, target_name)
                # 如果同名但内容不同的极小概率情况，加后缀（基本不会发生，因为有10位哈希前缀）
                suffix = 1
                while os.path.exists(target_path):
                    if os.path.getsize(target_path) == os.path.getsize(path) and \
                            self._fast_file_hash(target_path) == self._fast_file_hash(path):
                        break  # 命中同内容，直接用这个
                    target_path = os.path.join(import_dir, f"{base}_{content_hash_10}_{suffix}{ext}")
                    suffix += 1
                try:
                    import shutil as _sh
                    if not os.path.exists(target_path):
                        _sh.copy2(path, target_path)
                    use_path = target_path
                except Exception as e:
                    QMessageBox.warning(
                        self, "拷贝失败",
                        f"选中的图片不在 screenshots 目录下，本想拷贝到：\n{target_path}\n\n拷贝失败：{e}\n\n已改用原路径直接处理。"
                    )
                    use_path = path
        img = _read_bgr_file(use_path)
        if img is None:
            QMessageBox.warning(self, "读取失败", f"无法读取: {use_path}")
            return
        self._last_path = use_path
        # 先复用已有 split_elements_* 缓存（这张图曾经切过 → 不重新切，直接加载，节省时间+不新建目录）
        cached_dir = self._find_existing_split_dir_for_source(use_path)
        if cached_dir and self._load_from_existing_split_dir(cached_dir, use_path, fallback_bgr=img):
            return
        # 没命中缓存 → 重新切分（会新建目录）
        self._run_split(img, source_path=use_path)

    def _re_run_splitter(self):
        if self._src_bgr is None:
            QMessageBox.information(self, "提示", "当前还没有加载图片。")
            return
        self._run_split(self._src_bgr.copy(), source_path=self._last_path or None)

    # ============================================================
    # 切分 + 渲染 主流程
    # ============================================================
    def _run_split(self, bgr: np.ndarray, source_path: Optional[str] = None):
        if bgr is None:
            QMessageBox.warning(self, "无图", "未能获得图像数据。")
            return
        self._src_bgr = bgr
        h, w = bgr.shape[:2]
        self._lbl_info.setText(f"源图 {w}x{h}  正在自动切分...")
        self.repaint()
        try:
            res = run_splitter_on_image_bgr(bgr, padding=5, shot_dir=screenshot_dir())
        except Exception as e:
            QMessageBox.warning(self, "切分失败", f"splitter 异常: {e}")
            return
        self._split_result = res or {}
        if not res.get("ok", False):
            err = res.get("error") or "切分失败（未知原因）"
            QMessageBox.warning(self, "切分失败", err)
            return
        # 写源图关联 + 删除同一张图之前的旧碎图目录（避免缓存越来越大）
        out_dir = res.get("output_dir")
        actual_source = source_path or (self._last_path if self._last_path else None)
        if out_dir:
            self._write_source_info_file(out_dir, actual_source)
            # 清理同源图的旧缓存（保留刚生成这个out_dir本身）
            self._cleanup_split_caches_for_source(actual_source, exclude_dir=out_dir)
        # 给每个元素补上 enabled=True, manual=False
        elements = list(res.get("elements", []) or [])
        for e in elements:
            e["enabled"] = True
            e["manual"] = False
        self._elements = elements
        # 补 path
        if out_dir:
            files = list_elements_in_dir(out_dir)
            by_idx = {f["idx"]: f["path"] for f in files}
            for e in self._elements:
                p = by_idx.get(e.get("idx", -1))
                if p:
                    e["path"] = p
                elif e.get("crop") is None:
                    e["path"] = None
        # 对于自动切分但没写crop/没写path的，按 x,y,w,h 自己切一下
        for e in self._elements:
            if e.get("crop") is None:
                crop = self._crop_from_src(int(e.get("x", 0)), int(e.get("y", 0)),
                                           int(e.get("w", 0)), int(e.get("h", 0)))
                e["crop"] = crop
        # 渲染
        self._render_scene()
        self._render_right_list()
        self._update_cnt_label()
        base_info = f"源图 {w}x{h}  自动切分得到 {len(self._elements)} 个元素（左键=保留  右键=取消，全窗口联动）"
        if actual_source and out_dir:
            import os as _os
            base_info += f"   源图：{_os.path.basename(actual_source)}"
        self._lbl_info.setText(base_info)
        self._apply_source_ui_state(actual_source)

    # ============================================================
    # 左：完整图 scene
    # ============================================================
    def _render_scene(self):
        scene = self._preview_scene
        scene.clear()
        self._rect_items = []
        if self._src_bgr is None:
            return
        rgb = cv2.cvtColor(self._src_bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(qimg)
        item = QGraphicsPixmapItem(pix)
        item.setZValue(-10)
        scene.addItem(item)
        scene.setSceneRect(0, 0, w, h)

        for e in self._elements:
            ri = ElementRectItem(e)
            ri.setZValue(1)
            ri.clicked.connect(self._on_left_rect_toggled)
            scene.addItem(ri)
            self._rect_items.append(ri)

        # fit in view
        self._preview_view.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)

    def _refresh_scene_items_only(self):
        # 只刷新矩形显示（不rebuild整个scene，保留缩放/滚动）
        for ri in self._rect_items:
            ri.update()
        self._update_cnt_label()

    def _update_cnt_label(self):
        total = len(self._elements)
        kept = sum(1 for e in self._elements if e.get("enabled", True))
        manual = sum(1 for e in self._elements if e.get("manual", False))
        self._lbl_cnt.setText(
            f"元素总数：{total}   |   保留：{kept}   |   取消：{total - kept}   |   手动：{manual}"
        )

    # ============================================================
    # 右：碎图列表
    # ============================================================
    def _render_right_list(self):
        # ============================================================
        # 滚动位置还原：两种策略，优先级从高到低：
        #
        #   策略 1（主用）【锚定视口顶部第一张卡片 + Y 偏移像素】✅
        #     刷新前：itemAt(viewport 左上角) 取当前视口最顶部的可见卡片
        #             记 eid = anchor_eid，以及这张卡片距离 viewport 顶部偏移
        #             top_offset_pixel（一般是负数，表示这张卡片被滚动条裁掉了多少像素）
        #     刷新后：在新列表里找 anchor_eid
        #             ① 找得到 → 把这张卡片滚到 viewport 顶部 + 同样 top_offset_pixel 裁掉多少
        #             ② 找不到（比如用户右键取消的就是这张 → 被过滤了）
        #                → 找 anchor_eid 在 self._elements 原始顺序里【下一张仍然符合筛选的】
        #                  也就是"自然顶上来补位那张"，滚到顶部同样的偏移
        #     效果：用户取消的那张卡片消失后，它下面一张顶上来补位，
        #           用户的视口整体位置不变，视觉上就是"卡片往上靠了"
        #
        #   策略 2（兜底）比例法 ratio = value/maximum
        #     极端情况：锚定的卡片后面全都被过滤了（找不到任何能显示的），
        #     或者用户本来就在顶部（还没出现滚动条），此时用比例法兜底。
        #
        # 另外 3 个之前的修复依然保留：
        #   A. setUpdatesEnabled(False) 包 clear → addItem 全过程，防闪空白
        #   B. selectionModel + QItemSelectionModel.NoUpdate，不让 setCurrent 自动 scroll
        #   C. itemDoubleClicked 在 _build_ui 初始化只连 1 次，不重复挂槽
        # ============================================================

        vp = self._list.viewport()
        vp_rect = vp.rect() if vp is not None else QRect()

        # ---------- ① 刷新前：取锚点（anchor_eid + top_offset_pixel） + 比例兜底 ----------
        anchor_eid: int = -1
        top_offset_px: int = 0
        anchor_li_before = None
        if vp is not None and not vp_rect.isEmpty():
            # 左上角取一张，往里缩 2px 避免边界命中空
            anchor_li_before = self._list.itemAt(QPoint(2, 2))
            if anchor_li_before is None:
                anchor_li_before = self._list.itemAt(vp_rect.topLeft())
        if anchor_li_before is not None:
            anchor_eid = int(anchor_li_before.data(Qt.UserRole) or -1)
            # 算这张卡片距离 viewport 顶部的偏移（<0 表示卡片的顶部被滚动条裁掉了多少）
            v_rect_before = self._list.visualItemRect(anchor_li_before)
            top_offset_px = v_rect_before.top() - vp_rect.top()

        # 比例法兜底（极端情况用）
        vscroll = self._list.verticalScrollBar()
        hscroll = self._list.horizontalScrollBar()
        v_max_before = max(1, int(vscroll.maximum())) if vscroll is not None else 1
        h_max_before = max(1, int(hscroll.maximum())) if hscroll is not None else 1
        v_ratio = (int(vscroll.value()) / v_max_before) if vscroll is not None else 0.0
        h_ratio = (int(hscroll.value()) / h_max_before) if hscroll is not None else 0.0
        selected_idx_before = self._current_idx

        # ---------- ② 屏蔽中间重绘，clear → 重新构造卡片 ----------
        self._list.setUpdatesEnabled(False)
        try:
            self._list.clear()
            mode = getattr(self, "_filter_mode", "all")
            # 存：每一张仍然显示的卡片 → 它对应的 eid（按显示顺序）
            visible_eids: List[int] = []
            visible_li_map: Dict[int, QListWidgetItem] = {}
            selected_keep_row_in_new = -1
            row_no = -1
            for e in self._elements:
                if mode == "checked" and not bool(e.get("enabled", True)):
                    continue
                if mode == "unchecked" and bool(e.get("enabled", True)):
                    continue
                if mode == "custom" and not bool(e.get("manual", False)):
                    continue
                crop = e.get("crop")
                if crop is None and e.get("path"):
                    crop = _read_bgr_file(e["path"])
                if crop is None:
                    crop = self._crop_from_src(
                        int(e.get("x", 0)), int(e.get("y", 0)),
                        int(e.get("w", 0)), int(e.get("h", 0)),
                    )
                    e["crop"] = crop
                pix = _bgr_to_qpixmap(crop, max_width=110) if crop is not None else QPixmap()
                icon = QIcon(pix)
                manual_tag = " ✎" if e.get("manual") else ""
                enabled_tag = "" if e.get("enabled", True) else " 🚫"
                eid = int(e.get("idx", -1))
                label = f"#{eid:03d}  {e.get('w', 0)}×{e.get('h', 0)}{manual_tag}{enabled_tag}"
                li = QListWidgetItem(icon, label)
                li.setData(Qt.UserRole, eid)
                li.setSizeHint(QSize(148, 178))
                self._list.addItem(li)
                row_no += 1
                li.setToolTip(
                    f"元素 #{eid:03d}\n"
                    f"位置: ({e.get('x',0)},{e.get('y',0)})  大小: {e.get('w',0)}×{e.get('h',0)}\n"
                    f"当前: {'保留 (enabled)' if e.get('enabled',True) else '取消 (disabled)'}\n"
                    f"快捷操作：左键卡片=保留   右键卡片=取消   双击卡片=切换"
                )
                visible_eids.append(eid)
                visible_li_map[eid] = li
                if eid == selected_idx_before:
                    selected_keep_row_in_new = row_no

            # ---------- ③ 恢复滚动位置：优先策略 1（锚定顶卡片 + 偏移） ----------
            restored = False
            if anchor_eid >= 0 and visible_eids:
                # case A: anchor 卡片自己还在新列表里（说明这张没被过滤）
                target_eid = -1
                if anchor_eid in visible_li_map:
                    target_eid = anchor_eid
                else:
                    # case B: anchor 自己被过滤掉了 → 找 self._elements 里 anchor_eid 【后面】第一张仍然显示的
                    # （就是"顶上来补位的那张"）
                    found_anchor = False
                    for e in self._elements:
                        eid = int(e.get("idx", -1))
                        if not found_anchor:
                            if eid == anchor_eid:
                                found_anchor = True
                            continue
                        # 进入 anchor 之后的区域了
                        if eid in visible_li_map:
                            target_eid = eid
                            break
                    # 如果锚点后面全没了（锚点就是最后一张后面没东西了），兜底用新列表最后一张
                    if target_eid < 0 and visible_eids:
                        target_eid = visible_eids[-1]

                if target_eid > 0 and target_eid in visible_li_map:
                    # 先让这个 target 滚到视口顶部
                    target_li = visible_li_map[target_eid]
                    self._list.scrollToItem(target_li, QAbstractItemView.PositionAtTop)
                    # 再微调 top_offset_px：还原"这张卡片顶部被裁掉多少像素"
                    vp_now = self._list.viewport()
                    vp_now_rect = vp_now.rect() if vp_now is not None else QRect()
                    if vp_now is not None and not vp_now_rect.isEmpty():
                        vis_rect = self._list.visualItemRect(target_li)
                        current_top_offset = vis_rect.top() - vp_now_rect.top()
                        delta = current_top_offset - top_offset_px
                        vs = self._list.verticalScrollBar()
                        if vs is not None:
                            old_v = int(vs.value())
                            new_v = old_v + delta
                            vmax = int(vs.maximum())
                            new_v = max(0, min(vmax, new_v))
                            vs.blockSignals(True)
                            try:
                                vs.setValue(new_v)
                            finally:
                                vs.blockSignals(False)
                    restored = True

            # 策略 2 兜底：没锚定或者锚定后面也全没了 → 比例法
            if not restored:
                v_max_after = int(vscroll.maximum()) if vscroll is not None else 0
                h_max_after = int(hscroll.maximum()) if hscroll is not None else 0
                v_new_val = min(v_max_after, int(round(v_ratio * max(1, v_max_after)))) if v_max_after > 0 else 0
                h_new_val = min(h_max_after, int(round(h_ratio * max(1, h_max_after)))) if h_max_after > 0 else 0
                if vscroll is not None:
                    vscroll.blockSignals(True)
                    try:
                        vscroll.setValue(v_new_val)
                    finally:
                        vscroll.blockSignals(False)
                if hscroll is not None:
                    hscroll.blockSignals(True)
                    try:
                        hscroll.setValue(h_new_val)
                    finally:
                        hscroll.blockSignals(False)

            # ---------- ④ 恢复选中（仍然用 NoUpdate 绝对不 auto-scroll！ ----------
            from PySide6.QtCore import QItemSelectionModel
            if selected_keep_row_in_new >= 0:
                keep_eid = visible_eids[selected_keep_row_in_new]
                keep_li = visible_li_map.get(keep_eid)
                keep_idx = self._list.indexFromItem(keep_li) if keep_li is not None else None
                sm = self._list.selectionModel()
                if keep_idx is not None and sm is not None:
                    sm.select(keep_idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                    sm.setCurrentIndex(keep_idx, QItemSelectionModel.NoUpdate)
        finally:
            # 保证无论上面异常不会让 list widget 永久不重绘
            self._list.setUpdatesEnabled(True)

    def _on_filter_changed(self, _idx: int):
        """筛选下拉框变化 → 更新当前筛选模式，然后重新渲染右侧列表（实时显示）"""
        cmb = getattr(self, "_cmb_filter", None)
        if cmb is None:
            return
        mode = cmb.currentData()
        if isinstance(mode, str):
            self._filter_mode = mode
        self._render_right_list()

    def _re_render_if_filter_depends_on_enabled(self):
        """【筛选刷新规则】
        按用户要求：
        - 仅当当前筛选模式是「✅ 仅勾选（checked）」或「🚫 仅不勾选（unchecked）」时，
          元素的 enabled 状态变化会直接影响"哪些卡片能显示/不能显示"
          → 必须立即 _render_right_list 重新走一遍过滤，不然用户点卡片切换开关，
            但因为筛选过滤，卡片依然留在列表里或不消失，体验断档。
        - 「📋 全部（all）」和「✎ 仅自定义（custom）」筛选条件和 enabled 无关
          → enabled 变了不用重渲染，维持原有行为（只刷新文字标签和左侧矩形）。
        - 『✅ 全选 / ❌ 全不选』两个批量按钮，用户明确说"全选筛选和自定义勾选情况不用做刷新"
          → 对应调用点（_on_select_all_elements / _on_select_none_elements）不调用本函数。
        """
        fm = getattr(self, "_filter_mode", "all")
        if fm in ("checked", "unchecked"):
            self._render_right_list()

    # ============================================================
    # 事件：左↔右 联动
    # ============================================================
    def _on_left_rect_toggled(self, element: Dict[str, Any]):
        # 刷新右侧列表 item 的 tag + tooltip
        self._refresh_right_item_text()
        self._refresh_scene_items_only()
        # 如果当前选中的是这个 idx，也刷新 _lbl_item
        if self._current_idx == element.get("idx"):
            self._refresh_current_lbl()

    def _on_right_item_clicked(self, item: QListWidgetItem):
        idx = int(item.data(Qt.UserRole) or -1)
        self._current_idx = idx
        # 左键点击右侧卡片 → 明确设置为 保留（enabled=True）
        changed = False
        e = self._find_element(idx)
        if e is not None and not bool(e.get("enabled", True)):
            e["enabled"] = True
            changed = True
        self._refresh_current_lbl()
        if changed:
            # 筛选只依赖 enabled 的模式先重渲染（重渲染后内容全变了，不需要再 refresh_right_item_text 这种局部刷新）
            fm = getattr(self, "_filter_mode", "all")
            if fm in ("checked", "unchecked"):
                self._render_right_list()
                self._refresh_scene_items_only()
            else:
                self._refresh_right_item_text()
                self._refresh_scene_items_only()

    def _on_right_item_context_menu(self, pos):
        """右键点右侧卡片 → 选中 + 明确取消（enabled=False）。"""
        item = self._list.itemAt(pos)
        if item is None:
            return
        # 先视觉上选中当前右键的 item，让用户清楚"对哪个元素在操作"
        self._list.setCurrentItem(item)
        idx = int(item.data(Qt.UserRole) or -1)
        self._current_idx = idx
        e = self._find_element(idx)
        if e is None:
            return
        changed = False
        if bool(e.get("enabled", True)):
            e["enabled"] = False
            changed = True
        self._refresh_current_lbl()
        if changed:
            fm = getattr(self, "_filter_mode", "all")
            if fm in ("checked", "unchecked"):
                self._render_right_list()
                self._refresh_scene_items_only()
            else:
                self._refresh_right_item_text()
                self._refresh_scene_items_only()

    def _on_right_item_double_clicked(self, item: QListWidgetItem):
        idx = int(item.data(Qt.UserRole) or -1)
        # 双击：如果当前是保留 → 取消；是取消 → 保留（仅作为额外快捷，不主推）
        self._toggle_element(idx)

    def _on_right_check_changed(self, idx: int, state: int):
        # 预留（IconMode下未使用）：防止未来切 ListMode 失效
        enabled = bool(state == Qt.Checked)
        e = self._find_element(idx)
        if e is None:
            return
        e["enabled"] = enabled
        fm = getattr(self, "_filter_mode", "all")
        if fm in ("checked", "unchecked"):
            self._render_right_list()
        else:
            self._refresh_right_item_text()
        self._refresh_scene_items_only()

    def _on_toggle_current(self):
        if self._current_idx < 0:
            QMessageBox.information(self, "未选中", "请先在右侧列表中点击一个元素。")
            return
        self._toggle_element(self._current_idx)

    def _toggle_element(self, idx: int):
        e = self._find_element(idx)
        if e is None:
            return
        e["enabled"] = not bool(e.get("enabled", True))
        fm = getattr(self, "_filter_mode", "all")
        if fm in ("checked", "unchecked"):
            self._render_right_list()
        else:
            self._refresh_right_item_text()
        self._refresh_scene_items_only()
        self._refresh_current_lbl()

    def _refresh_right_item_text(self):
        n = self._list.count()
        for i in range(n):
            li = self._list.item(i)
            idx = int(li.data(Qt.UserRole) or -1)
            e = self._find_element(idx)
            if e is None:
                continue
            manual_tag = "  ✎" if e.get("manual") else ""
            enabled_tag = "" if e.get("enabled", True) else "  🚫取消"
            label = (
                f"#{int(e.get('idx',-1)):03d} {e.get('w',0)}x{e.get('h',0)} "
                f"@{e.get('x',0)},{e.get('y',0)}{manual_tag}{enabled_tag}"
            )
            li.setText(label)
            li.setToolTip(
                f"元素 #{int(e.get('idx',-1)):03d}\n"
                f"位置: ({e.get('x',0)},{e.get('y',0)})  大小: {e.get('w',0)}x{e.get('h',0)}\n"
                f"当前: {'保留 (enabled)' if e.get('enabled',True) else '取消 (disabled)'}\n"
                f"快捷操作：左键卡片=保留   右键卡片=取消   双击卡片=切换"
            )
            # 颜色：取消的 → 灰色文字
            li.setForeground(QColor("#8B949E") if not e.get("enabled", True) else QColor("#C9D1D9"))

    def _refresh_current_lbl(self):
        import os as _os
        if self._current_idx < 0:
            self._lbl_item.setText("（未选择元素）")
            self._lbl_item.setToolTip("")
            return
        e = self._find_element(self._current_idx)
        if e is None:
            self._lbl_item.setText("（未选择元素）")
            self._lbl_item.setToolTip("")
            return
        enabled_tag = "✅ 保留" if e.get("enabled", True) else "🚫 取消"
        src_tag = f"手动拆分✎" if e.get("manual", False) else f"自动切分"
        p = e.get("path") or ""
        basename = _os.path.basename(p) if p else ""
        # 只显示文件名（避免长绝对路径导致右侧宽度被撑大，挤压左侧）
        file_text = f"文件: {basename}" if basename else "文件: （未保存）"
        self._lbl_item.setText(
            f"#{int(e.get('idx', -1)):03d}  [{src_tag}]  [{enabled_tag}]   "
            f"位置=({e.get('x',0)},{e.get('y',0)})  大小={e.get('w',0)}x{e.get('h',0)}  面积={e.get('area','-')}\n"
            f"{file_text}"
        )
        # 完整路径放 tooltip（悬停查看），不再显示在文本里
        self._lbl_item.setToolTip(f"完整路径: {p}" if p else "")

    # ============================================================
    # 手动拆分：勾选后在左侧预览拖拽画框 → 新元素
    # ============================================================
    def eventFilter(self, obj, event: QEvent):
        # 手动拆分默认始终开启：只有在预览 viewport 的鼠标事件才处理
        if obj is self._preview_view.viewport():
            t = event.type()
            if t == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    scene_pt = self._preview_view.mapToScene(event.position().toPoint())
                    self._manual_drag_from = scene_pt
                    self._manual_drag_to = None
                    self._clear_manual_drag_rect()
                    return True
            elif t == QEvent.MouseMove:
                if self._manual_drag_from is not None:
                    scene_pt = self._preview_view.mapToScene(event.position().toPoint())
                    self._manual_drag_to = scene_pt
                    self._update_manual_drag_rect()
                    return True
            elif t == QEvent.MouseButtonRelease:
                if event.button() == Qt.LeftButton and self._manual_drag_from is not None:
                    scene_pt = self._preview_view.mapToScene(event.position().toPoint())
                    self._manual_drag_to = scene_pt
                    self._apply_manual_rect_as_element()
                    self._manual_drag_from = None
                    self._manual_drag_to = None
                    self._clear_manual_drag_rect()
                    return True
        return super().eventFilter(obj, event)

    def _update_manual_drag_rect(self):
        if self._manual_drag_from is None or self._manual_drag_to is None:
            return
        rect = QRectF(self._manual_drag_from, self._manual_drag_to).normalized()
        if self._manual_drag_item is None:
            pen = QPen(QColor(255, 100, 0, 255))
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
            brush = QBrush(QColor(255, 100, 0, 60))
            it = QGraphicsRectItem(rect)
            it.setZValue(10)
            it.setPen(pen)
            it.setBrush(brush)
            self._preview_scene.addItem(it)
            self._manual_drag_item = it
        else:
            self._manual_drag_item.setRect(rect)

    def _clear_manual_drag_rect(self):
        if self._manual_drag_item is not None:
            try:
                self._preview_scene.removeItem(self._manual_drag_item)
            except Exception:
                pass
            self._manual_drag_item = None

    def _apply_manual_rect_as_element(self):
        if self._manual_drag_from is None or self._manual_drag_to is None or self._src_bgr is None:
            return
        rect = QRectF(self._manual_drag_from, self._manual_drag_to).normalized()
        # 限制在源图内
        H, W = self._src_bgr.shape[:2]
        x = max(0, int(rect.x()))
        y = max(0, int(rect.y()))
        w = min(W - x, int(rect.width()))
        h = min(H - y, int(rect.height()))
        if w < 3 or h < 3:
            return
        # 新 idx = 当前最大 idx+1
        cur_idx = max([int(e.get("idx", -1)) for e in self._elements] or [-1]) + 1
        crop = self._crop_from_src(x, y, w, h)
        # 额外：把这个手动元素保存一份PNG（写入screenshots/manual_elements 目录下）
        out_dir = os.path.join(screenshot_dir(), "manual_elements")
        os.makedirs(out_dir, exist_ok=True)
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"manual_{cur_idx:04d}_{x}_{y}_{w}x{h}_{ts}.png"
        fpath = os.path.join(out_dir, fname)
        ok_path = fpath if (crop is not None and write_png_cn(fpath, crop)) else None
        new_e: Dict[str, Any] = {
            "idx": cur_idx,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "area": w * h,
            "enabled": True,
            "manual": True,
            "crop": crop,
            "path": ok_path,
        }
        self._elements.append(new_e)

        # 增量渲染：scene 里追加一个 ElementRectItem，右侧列表追加一个 item
        ri = ElementRectItem(new_e)
        ri.setZValue(1)
        ri.clicked.connect(self._on_left_rect_toggled)
        self._preview_scene.addItem(ri)
        self._rect_items.append(ri)

        # 右侧列表：重新渲染一次更简单（list clear/rebuild 成本不大）
        self._render_right_list()
        self._update_cnt_label()
        self._lbl_info.setText(
            f"✅ 手动拆分新增元素 #{cur_idx:03d}（{w}x{h} @{x},{y}），"
            f"当前总元素={len(self._elements)}；继续在左侧拖拽画框可直接再添加。"
        )

    # ============================================================
    # 底部按钮：保存模板 / 生成FindImage / 回填节点
    # ============================================================
    def _find_element(self, idx):
        for e in self._elements:
            if e.get("idx") == idx:
                return e
        return None

    # ============================================================
    # 右侧批量：全选 / 全不选 / 导出选中
    # ============================================================
    def _on_select_all_elements(self):
        if not self._elements:
            QMessageBox.information(self, "提示", "当前还没有任何碎图元素。请先打开/切分一张图片。")
            return
        changed = False
        for e in self._elements:
            if not bool(e.get("enabled", True)):
                e["enabled"] = True
                changed = True
        if not changed:
            QMessageBox.information(self, "提示", f"当前 {len(self._elements)} 张碎图本来就都是【保留】状态，无需修改。")
            return
        self._render_scene()
        self._render_right_list()
        self._update_cnt_label()
        self._refresh_current_lbl()

    def _on_select_none_elements(self):
        if not self._elements:
            QMessageBox.information(self, "提示", "当前还没有任何碎图元素。请先打开/切分一张图片。")
            return
        changed = False
        for e in self._elements:
            if bool(e.get("enabled", True)):
                e["enabled"] = False
                changed = True
        if not changed:
            QMessageBox.information(self, "提示", f"当前 {len(self._elements)} 张碎图本来就都是【取消】状态，无需修改。")
            return
        self._render_scene()
        self._render_right_list()
        self._update_cnt_label()
        self._refresh_current_lbl()

    def _on_export_selected_elements(self):
        if not self._elements:
            QMessageBox.information(self, "提示", "当前还没有任何碎图元素。请先打开/切分一张图片。")
            return
        selected = [e for e in self._elements if bool(e.get("enabled", True))]
        if not selected:
            QMessageBox.warning(
                self, "没有可导出的碎图",
                "当前【保留=开启】的碎图数量是 0。\n请先在左侧完整图上点矩形 / 或右侧卡片上点左键标记保留，再导出。"
            )
            return
        # 必须每次选目录（不记忆），不选不给导出。默认起始目录使用桌面/系统图片目录（绝不指向 screenshots 内部，避免和截图缓存混在一起）
        out_dir = QFileDialog.getExistingDirectory(
            self,
            "请选择【碎图PNG导出目录】（每次导出都重新选，不记忆）",
            self._default_export_dir(),
        )
        if not out_dir:
            return
        self._export_selected_elements_to(out_dir)

    def _export_selected_elements_to(self, out_dir: str) -> Tuple[List[str], List[str]]:
        """
        真正的导出逻辑：在用户选择的目录 out_dir 下，先自动建一个带时间戳的子文件夹
        「碎图导出_YYYYMMDD_HHMMSS」，再把所有 enabled=True 的元素作为 PNG 写到那个子文件夹里。
        这样避免把几十张碎图不小心散落进「下载」或「桌面」的根目录里。
        返回 (导出成功列表(完整路径), 失败描述列表)。UI按钮通过先选目录再调这个实现，smoke可以直接调。
        """
        if not out_dir:
            return [], []
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "目录不可用", f"无法创建导出目录 {out_dir}: {e}")
                return [], []
        # 在 out_dir 下创建带时间戳的子文件夹（保证每次导出都有独立的小文件夹，不污染根目录）
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        sub_folder_name = f"碎图导出_{ts}"
        actual_dir = os.path.join(out_dir, sub_folder_name)
        try:
            os.makedirs(actual_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "目录不可用", f"无法在 {out_dir} 下创建子文件夹 {sub_folder_name}: {e}")
            return [], []
        selected = [e for e in (self._elements or []) if bool(e.get("enabled", True))]
        if not selected:
            return [], []
        exported: List[str] = []
        failed: List[str] = []
        for e in selected:
            idx = int(e.get("idx", -1))
            x = int(e.get("x", 0)); y = int(e.get("y", 0))
            w = int(e.get("w", 0)); h = int(e.get("h", 0))
            fname = f"e{idx:04d}_{x}_{y}_{w}x{h}_{ts}.png"
            target = os.path.join(actual_dir, fname)
            ok = False
            src_path = e.get("path")
            if src_path and os.path.isfile(src_path):
                try:
                    import shutil as _sh
                    _sh.copy2(src_path, target)
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                crop = e.get("crop")
                if crop is None:
                    crop = self._crop_from_src(x, y, w, h)
                if crop is not None and write_png_cn(target, crop):
                    ok = True
            if ok:
                exported.append(target)
            else:
                failed.append(f"#{idx:03d} ({x},{y}) {w}x{h}")
        # 结果提示（因为smoke也会走这个helper，同样保留提示用于断言）
        if exported:
            summary = f"已导出 {len(exported)} 张碎图到目录：\n{actual_dir}\n\n"
            for p in exported[:10]:
                summary += f"  · {os.path.basename(p)}\n"
            if len(exported) > 10:
                summary += f"  · ……另外 {len(exported) - 10} 张已导出"
        else:
            summary = "（实际没导出任何一张）"
        if failed:
            summary += f"\n\n⚠️ 以下 {len(failed)} 张导出失败：\n"
            summary += "\n".join(f"  · {f}" for f in failed)
        QMessageBox.information(self, "导出完成", summary)
        return exported, failed

    def _crop_from_src(self, x, y, w, h) -> Optional[np.ndarray]:
        if self._src_bgr is None or w <= 0 or h <= 0:
            return None
        H, W = self._src_bgr.shape[:2]
        x0 = max(0, x); y0 = max(0, y)
        x1 = min(W, x + w); y1 = min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return self._src_bgr[y0:y1, x0:x1, :].copy()

    def _current_element(self):
        if self._current_idx < 0:
            QMessageBox.information(self, "未选中", "请先在右侧列表中点击一个元素。")
            return None
        e = self._find_element(self._current_idx)
        if e is None:
            QMessageBox.information(self, "未选中", "请先在右侧列表中点击一个元素。")
            return None
        if not e.get("enabled", True):
            ret = QMessageBox.question(
                self, "当前元素已取消",
                "你选中的元素当前处于『取消（隐藏）』状态，仍然继续操作吗？\n"
                "是=继续操作  否=先回到底部『✓ 切换当前元素的 保留/取消』改回来。",
            )
            if ret != QMessageBox.Yes:
                return None
        return e

    def _ensure_element_has_crop_and_path(self, e) -> Tuple[Optional[np.ndarray], Optional[str]]:
        crop = e.get("crop")
        if crop is None:
            crop = _read_bgr_file(e.get("path") or "") if e.get("path") else None
        if crop is None:
            crop = self._crop_from_src(int(e.get("x", 0)), int(e.get("y", 0)),
                                       int(e.get("w", 0)), int(e.get("h", 0)))
            e["crop"] = crop
        if crop is None:
            return None, None
        # 确保保存了文件
        if not e.get("path") or not os.path.isfile(e["path"]):
            import datetime
            name = (
                f"element_{int(e.get('idx', 0)):04d}_"
                f"{e.get('x', 0)}_{e.get('y', 0)}_{e.get('w', 0)}x{e.get('h', 0)}.png"
            )
            dst = os.path.join(screenshot_dir(), "elements", name)
            if write_png_cn(dst, crop):
                e["path"] = dst
        return crop, e.get("path")

    def _save_element_as_template_png(self, e) -> Optional[str]:
        crop, _ = self._ensure_element_has_crop_and_path(e)
        if crop is None:
            QMessageBox.warning(self, "无法保存", "该元素没有可用的图像数据。")
            return None
        import datetime
        name = (
            f"split_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_"
            f"{int(e.get('idx', 0)):03d}.png"
        )
        try:
            tpl_dir = template_dir()
        except Exception as e_tpl:
            QMessageBox.warning(
                self, "保存失败",
                f"无法获取工程 templates/ 目录：\n{e_tpl}\n\n请先新建或打开一个 BlueprintAutomation 工程。"
            )
            return None
        dst = os.path.join(tpl_dir, name)
        if not write_png_cn(dst, crop):
            QMessageBox.warning(self, "保存失败", f"无法写文件: {dst}")
            return None
        return dst

    @staticmethod
    def _bgr_to_b64_preview(crop_bgr: np.ndarray, max_side: int = 220) -> str:
        """把 BGR numpy 数组（element crop）缩成 (max_side, max_side) 以内的缩略图 PNG，输出 base64 ASCII 串。
        用于回填 FindImage 节点的 template_b64 参数，让 inspector 直接显示预览图。
        """
        if crop_bgr is None or not isinstance(crop_bgr, np.ndarray) or crop_bgr.size == 0:
            return ""
        try:
            import cv2 as _cv
            import base64 as _b64
            h, w = crop_bgr.shape[:2]
            scale = 1.0
            if max(h, w) > max_side:
                scale = max_side / float(max(h, w))
            if abs(scale - 1.0) > 1e-3:
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                small = _cv.resize(crop_bgr, (nw, nh), interpolation=_cv.INTER_AREA)
            else:
                small = crop_bgr
            # BGR -> PNG bytes (imencode 支持中文路径无关的内存编码)
            ok, buf = _cv.imencode(".png", small)
            if not ok or buf is None:
                return ""
            return _b64.b64encode(buf.tobytes()).decode("ascii")
        except Exception:
            return ""

    def _on_save_template_png(self):
        """【💾 保存PNG】只写模板PNG文件到 templates/，不做其他事。
        与『🔎 生成节点』严格区分：
          - 保存PNG  = 纯导出图片（用户只想拿素材文件，不想在画布上加东西）
          - 生成节点 = 保存PNG + 在蓝图画布上自动生成 FindImage 节点并填好参数
        """
        e = self._current_element()
        if not e:
            return
        dst = self._save_element_as_template_png(e)
        if not dst:
            return
        QMessageBox.information(
            self, "保存成功",
            f"✅ 模板PNG已保存：\n  {dst}\n\n"
            f"（只保存了图片，画布上没有新增节点；要生成找图节点请点【🔎 生成节点】。）"
        )

    def _on_create_findimage_node(self):
        """【🔎 生成节点】保存模板PNG + 在画布中央生成 FindImage 节点（template/region/template_b64 全部预填好）。"""
        e = self._current_element()
        if not e:
            return
        # 1) 确保 crop 已经有
        crop, _ = self._ensure_element_has_crop_and_path(e)
        if crop is None:
            QMessageBox.warning(self, "无法生成", "该元素没有可用的图像数据。")
            return
        # 2) 保存为 templates/*.png（复用保存函数，避免重复写文件）
        dst = self._save_element_as_template_png(e)
        if not dst:
            return
        # 3) 生成 b64 预览缩略图（直接用已经裁剪好的 BGR numpy）
        thumb_b64 = ElementSplitDialog._bgr_to_b64_preview(crop)
        # 4) 调主窗口钩子生成节点
        fn = self._hooks.get("create_find_image_node") if isinstance(self._hooks, dict) else None
        region = (int(e["x"]), int(e["y"]), int(e["w"]), int(e["h"]))
        node_id = None
        if callable(fn):
            try:
                node_id = fn(dst, region, thumb_b64)
            except TypeError:
                # 兼容旧版只接收 2 参数的钩子（万一升级不完整）
                try:
                    node_id = fn(dst, region)
                except Exception as _e:
                    QMessageBox.warning(self, "生成节点失败", f"回调执行异常：\n{_e}")
                    return
            except Exception as _e:
                QMessageBox.warning(self, "生成节点失败", f"回调执行异常：\n{_e}")
                return
        # 5) 提示（区分两种情况：是否成功调用到主窗口）
        if node_id is not None:
            QMessageBox.information(
                self, "已生成节点",
                f"✅ 已在画布中央生成 FindImage 节点：\n"
                f"  · 模板：{dst}\n"
                f"  · region：{region}\n"
                f"  · 预览：已附上（inspector 中可直接看到缩略图）\n\n"
                f"（图片也已保存到 templates 目录，若只想要图片下次点【💾 保存PNG】即可。）"
            )
        else:
            # 没有主窗口挂钩子的情况（比如单独启动 Dialog），只做存图提示
            QMessageBox.information(
                self, "模板已保存",
                f"✅ 模板已保存到:\n  {dst}\n\n"
                f"（未找到主窗口的「生成节点」回调，未在画布上新增节点。）"
            )

    def _resolve_hook(self, name: str):
        """返回指定 name 的 hook 回调：兼容 dict / 对象属性两种方式。"""
        h = self._hooks
        if isinstance(h, dict):
            v = h.get(name)
            if callable(v):
                return v
            return None
        # 对象方式
        fn = getattr(h, name, None)
        if callable(fn):
            return fn
        return None
