import os
import sys
import time
import traceback
import copy
import json
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional


# ============================================================
# 【必须放在任何 PySide6 import 之前！】
# Windows DPI 感知设置：在 PySide6 的 qt.qpa.window 初始化之前抢占设置，
# 避免 pynput/pyautogui 等第三方库提前锁定 DPI 级别导致：
#   qt.qpa.window: SetProcessDpiAwarenessContext() failed: 拒绝访问。
# ============================================================
if os.name == "nt":
    try:
        import ctypes
        try:
            # Windows 10 1607+: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


from PySide6.QtCore import Qt, QObject, QPointF, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ============== 项目根 = sys.path 首位 ==============
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CURRENT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.blueprint.core.graph_store import BlueprintStore
from src.blueprint.core.models import BlueprintGraph, Node
from src.blueprint.core.node_library import NODE_LIBRARY
from src.blueprint.project_manager import ProjectManager
from src.blueprint.runtime.executor import BlueprintRunner
from src.blueprint.runtime.validator import (
    validate_graph,
    has_errors,
    format_issues,
)
from src.blueprint.ui.element_split_dialog import ElementSplitDialog
from src.blueprint.ui.graphics_scene import NodeScene, NodeView
from src.blueprint.ui.inspector_panel import InspectorPanel
from src.blueprint.ui.log_panel import LogPanel
from src.blueprint.ui.node_palette import NodePalette
from src.blueprint.ui.project_startup_dialog import ProjectStartupDialog
from src.blueprint.ui.settings_dialog import SettingsDialog
# 全局热键 + 用户设置
from src.tools.hotkey_manager import GlobalHotkeyManager
from src.config.user_settings import (
    get_hotkey_capture_target,
    set_hotkey_capture_target,
    get_shortcut,
    set_shortcut,
    all_shortcut_keys,
    is_global_shortcut,
    shortcut_label,
    pynput_to_qkeysequence,
    read_user_settings,
    save_user_settings,
    normalize_pynput_hotkey,
    _DEFAULT_BY_KEY,
    SHORTCUT_REGISTRY,
)
from src.tools.window_utils import list_visible_windows, start_exe


# ============================================================
# 辅助：禁用滚轮误切换的 ComboBox
#   - 下拉收起时：悬停滚轮 -> 忽略（防误切）
#   - 下拉弹出时：滚轮照常滚动选项列表
# ============================================================
class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        try:
            view = self.view()
            if view is not None and view.isVisible():
                # 下拉列表打开时，允许滚轮翻列表
                super().wheelEvent(event)
                return
        except Exception:
            pass
        event.ignore()


class BlueprintMainWindow(QMainWindow):
    """蓝图编辑器主窗口：左节点面板 / 中画布 / 右Inspector / 底日志。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("蓝图自动化工具")
        self.resize(1560, 920)
        self._current_path: Optional[str] = None
        self._graph: Optional[BlueprintGraph] = None
        self._last_saved_graph_hash: Optional[str] = None  # 上一次保存/加载时的蓝图哈希，None=从未落盘
        self._runner: Optional[BlueprintRunner] = None
        self._active_hwnd: Optional[int] = None
        self._active_hwnd_title_key: Optional[str] = None
        # 工程系统
        self._pm: ProjectManager = ProjectManager.instance()
        # 热键/切分相关
        self._user_settings: Dict[str, Any] = read_user_settings()
        self._hotkey_mgr = GlobalHotkeyManager(qt_parent=self)
        self._split_dialog_ref: Optional[ElementSplitDialog] = None
        self._last_capture_png: Optional[str] = None
        # 剪贴板 + 撤销/重做栈（硬编码标准快捷键：Ctrl+C/V/Z，不在设置界面暴露）
        self._clipboard_nodes: list = []
        self._undo_stack: list = []
        self._redo_stack: list = []

        self._build_central()
        self._build_toolbar()
        self._build_statusbar()
        self._wire_signals()
        # 绑定：全局热键 ID -> 回调（统一 QTimer.singleShot 立即到主线程，避免重入）
        for hid, cb in self._global_hotkey_id_to_callback().items():
            self._hotkey_mgr.set_callback(hid, lambda c=cb: QTimer.singleShot(0, c))
        self._reload_target_windows()

        # 启动：如果当前已经有打开的工程（main里弹过ProjectStartupDialog设置过），
        # 就加载该工程下的 <工程名>.bp.json；否则创建一张未保存的空白图（理论不会走到这，main会强制先选工程）
        self._load_or_new_graph_for_current_project()

        # 最后：把 settings 中所有快捷键应用（global 注册到 _hotkey_mgr + local 到 QAction）
        self._apply_shortcuts_from_settings(changed_keys=None, log_success=True)

        # 程序图标双保险：从全局 QApplication 取已经加载好的多尺寸图标，应用到主窗口
        # （main() 里已经 app.setWindowIcon 并存到 property("app_icon_multi") 了，这里优先拿多尺寸的）
        try:
            from PySide6.QtWidgets import QApplication as _QApp
            from PySide6.QtCore import QVariant as _QVar
            _a = _QApp.instance()
            multi_icon = _a.property("app_icon_multi")
            if multi_icon is not None and not isinstance(multi_icon, _QVar):
                # 直接拿到 QIcon 对象，非空就用
                try:
                    if not multi_icon.isNull():
                        self.setWindowIcon(multi_icon)
                except Exception:
                    pass
            else:
                # 兜底：老方式，从 windowIcon 拿（可能是单尺寸，但总比没有强）
                fallback = _a.windowIcon()
                if fallback is not None and not fallback.isNull():
                    self.setWindowIcon(fallback)
        except Exception:
            pass

    # ==================== 工程系统辅助 ====================
    def _ensure_project_open(self, action_desc: str = "该操作") -> bool:
        """
        所有「写资源」的操作（截图、切图、运行蓝图、保存模板）前必须调用：
        没工程就弹警告 + 返回 False；有工程返回 True。
        """
        if self._pm.is_open():
            return True
        QMessageBox.warning(
            self, "请先选择工程",
            f"要执行「{action_desc}」必须先有工程。\n"
            f"不同工程的截图/模板/缓存/运行日志 100% 独立保存，互不干扰。\n\n"
            f"请点左上角 『工程 ▾』 下拉菜单，选择「新建工程…」或「打开工程…」再继续。",
        )
        return False

    def _load_or_new_graph_for_current_project(self):
        """当前已经有开工程：按三级优先级加载蓝图，绝不自动创建落盘的 .bp.json 文件（首次create_project除外）
        优先级 1：last_opened_blueprint（用户上次打开/保存的那个）
        优先级 2：工程同名默认蓝图（第一次创建工程时自动生成的那张）
        优先级 3：工程内没有任何蓝图 → 仅显示内存中的空画布，不会在磁盘上创建任何 .bp.json（想新建蓝图请点工程菜单→新建蓝图手动创建）"""
        if self._pm.is_open():
            proj_name = self._pm.project_name or "Untitled"
            # ===== 优先级 1：最近使用的蓝图 =====
            last_bp = self._pm.get_last_opened_blueprint()
            if last_bp and os.path.isfile(last_bp):
                try:
                    g = BlueprintStore.load(last_bp)
                    if g is not None:
                        self._current_path = last_bp
                        self._mark_graph_clean()  # 刚加载的文件，视为干净状态（在 bind 之前先写 hash 以免后续误判）
                        self._bind_new_graph(g, zoom_fit=True)  # 统一绑定 + 清空Inspector残留
                        self._log.append_log(f"已加载最近使用的蓝图：{last_bp}", "SUCCESS")
                        self._update_title()
                        return
                except Exception as e:
                    self._log.append_log(
                        f"加载最近蓝图失败，回退尝试默认蓝图：{type(e).__name__}: {e}",
                        "WARN",
                    )
                    print("[WARN] 加载最近蓝图详细异常：")
                    traceback.print_exc()
            # ===== 优先级 2：工程同名默认蓝图（首次创建工程时自动生成的那张）=====
            default_bp = self._pm.blueprint_path()
            if default_bp and os.path.isfile(default_bp):
                try:
                    g = BlueprintStore.load(default_bp)
                    if g is not None:
                        self._current_path = default_bp
                        self._mark_graph_clean()  # 刚加载的文件，视为干净状态
                        # 顺便把这张默认蓝图记为 last（下次就会走优先级 1 了）
                        try:
                            self._pm.set_last_opened_blueprint(default_bp)
                        except Exception:
                            pass
                        self._bind_new_graph(g, zoom_fit=True)  # 统一绑定
                        self._log.append_log(f"已加载默认蓝图：{default_bp}", "SUCCESS")
                        self._update_title()
                        return
                except Exception as e:
                    self._log.append_log(
                        f"加载默认蓝图失败：{type(e).__name__}: {e}",
                        "WARN",
                    )
                    print("[WARN] 加载默认蓝图详细异常：")
                    traceback.print_exc()
            # ===== 优先级 3：工程内没有任何可用蓝图 → 仅显示内存空画布，绝不落盘创建文件 =====
            name = proj_name
            g = BlueprintStore.new_graph(name)
            self._current_path = None
            self._last_saved_graph_hash = None  # 空新图，hash 置空=从未保存
            self._bind_new_graph(g, zoom_fit=False)  # 空图无需缩放
            self._log.append_log(
                f"当前工程内暂未创建任何蓝图文件，请点左上角『工程 ▾』→ 新建蓝图（或 Ctrl+S 保存为.bp.json）手动创建。",
                "INFO"
            )
            self._update_title()
            return
        # 兜底：没工程
        self._new_graph()

    def _on_new_project(self):
        """顶栏按钮：重新弹出 ProjectStartupDialog 到『新建工程』页"""
        self._open_project_startup(start_tab=0)

    def _on_open_project(self):
        """顶栏按钮：重新弹出 ProjectStartupDialog 到『打开工程/最近工程』页"""
        self._open_project_startup(start_tab=1)

    def _open_project_startup(self, start_tab: int = 0):
        """重新弹工程选择框，成功则切换工程并重载蓝图"""
        dlg = ProjectStartupDialog(self)
        try:
            tw = dlg.findChild(QTabWidget)
            if tw is not None and 0 <= start_tab < tw.count():
                tw.setCurrentIndex(start_tab)
        except Exception:
            pass
        if dlg.exec() != QDialog.Accepted:
            return
        # 切工程前：先问用户要不要保存当前修改
        if not self._confirm_discard():
            return
        root = dlg.result_project_root
        if not root:
            return
        # ProjectManager.open_project 已经在 ProjectStartupDialog 里执行过，
        # 会自动卸载旧provider、注册新provider、发信号。
        self._load_or_new_graph_for_current_project()
        # 新工程/开工程成功后刷新「最近工程」菜单（把本次置顶到第一位）
        try:
            self._refresh_recent_projects_menu()
        except Exception:
            pass

    # ==================== UI 构建 ====================
    def _build_central(self):
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ========== 4面板QDockWidget布局（Qflow风格 + Qt增强可停靠可浮动） ==========
        # 中央只有【画布】NodeView（和Qflow FlowEditor对等）
        self._scene = NodeScene(self)
        self._view = NodeView(self._scene)
        center_wrap = QWidget()
        cl = QVBoxLayout(center_wrap)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self._view)
        root_layout.addWidget(center_wrap, 1)

        # Dock 选项：允许Tab停靠+动画+嵌套
        self.setDockOptions(
            QMainWindow.AllowNestedDocks
            | QMainWindow.AllowTabbedDocks
            | QMainWindow.AnimatedDocks
            | QMainWindow.GroupedDragging
        )
        _dock_stylesheet = (
            "QDockWidget { color: #C9D1D9; background: #161B22; titlebar-close-icon: none; }"
            "QDockWidget::title { background: #21262D; padding: 4px 10px; border-bottom: 1px solid #30363D; }"
            "QDockWidget::close-button, QDockWidget::float-button { background: transparent; padding: 2px; }"
        )

        # 左：📚节点库（和Qflow toolbox对等，240宽+最小160）
        self._palette = NodePalette()
        dock_left = QDockWidget("📚 节点库", self)
        dock_left.setObjectName("dockPalette")
        dock_left.setWidget(self._palette)
        dock_left.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock_left.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        dock_left.setMinimumWidth(180)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock_left)
        try:
            self.resizeDocks([dock_left], [260], Qt.Horizontal)
        except Exception:
            pass
        dock_left.setStyleSheet(_dock_stylesheet)
        self._dock_palette = dock_left

        # 右：🎛 属性面板（和Qflow PropertyPanel对等，340宽+最小280）
        self._inspector = InspectorPanel()
        dock_right = QDockWidget("🎛 属性面板", self)
        dock_right.setObjectName("dockInspector")
        dock_right.setWidget(self._inspector)
        dock_right.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        dock_right.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        dock_right.setMinimumWidth(300)
        self.addDockWidget(Qt.RightDockWidgetArea, dock_right)
        try:
            self.resizeDocks([dock_right], [360], Qt.Horizontal)
        except Exception:
            pass
        dock_right.setStyleSheet(_dock_stylesheet)
        self._dock_inspector = dock_right

        # 底：📜执行日志（和Qflow LogPanel对等，最小80高初始160）
        self._log = LogPanel()
        dock_bottom = QDockWidget("📜 执行日志", self)
        dock_bottom.setObjectName("dockLog")
        dock_bottom.setWidget(self._log)
        dock_bottom.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        dock_bottom.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        dock_bottom.setMinimumHeight(80)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock_bottom)
        try:
            self.resizeDocks([dock_bottom], [180], Qt.Vertical)
        except Exception:
            pass
        dock_bottom.setStyleSheet(_dock_stylesheet)
        self._dock_log = dock_bottom

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        self.addToolBar(tb)

        def mk_act(icon_color, text, slot):
            a = QAction(text, self)
            a.triggered.connect(slot)
            a.setIcon(self._make_icon(icon_color))
            return a

        def add_toolbtn(action, bg="#1F242C", color="#C9D1D9", hover_bg="#2D333B", hover_border="#3A3F4B", wmin=None):
            btn = QToolButton()
            btn.setDefaultAction(action)
            # 【关键】手动 addWidget 到 QToolBar 里的 QToolButton，必须显式设样式才显示文字
            # 否则默认继承 ToolBar 的 ToolButtonStyle，但 QAction/icon 渲染会出错
            if action.icon().isNull():
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            else:
                btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            style = (
                f"QToolButton {{ background:{bg}; color:{color}; border:1px solid #30363D; border-radius:4px; padding:4px 10px; }}"
                f"QToolButton:hover {{ background:{hover_bg}; border:1px solid {hover_border}; }}"
                f"QToolButton:disabled {{ color:#484F58; background:#1C2128; }}"
            )
            btn.setStyleSheet(style)
            btn.setCursor(Qt.PointingHandCursor)
            if wmin:
                btn.setMinimumWidth(wmin)
            tb.addWidget(btn)
            return btn

        # ============================================================
        # 【前置：工程/蓝图级 QAction 先定义完毕】
        #  蓝图按钮快捷键按用户要求：全部移除，一个不留（全局 Ctrl+S/Ctrl+Z/Ctrl+C/Ctrl+V 另有独立 QAction 绑）
        # ============================================================
        self._act_save_as = mk_act("#8250DF", "另存蓝图…", self._on_save_as)

        # 蓝图级 4 个操作 + 删除（无快捷键、无顶栏按钮、只在「工程 ▾」下拉里出现）
        self._act_new = mk_act("#238636", "新建蓝图", self._on_new)
        self._act_open = mk_act("#0969DA", "加载蓝图…", self._on_open)
        self._act_delete = mk_act("#DA3633", "删除蓝图…", self._on_delete_blueprint)
        self._act_save = mk_act("#0969DA", "保存蓝图", self._on_save)

        # 工程级 2 个操作
        self._act_new_project = mk_act("#7C3AED", "新建工程…", self._on_new_project)
        self._act_open_project = mk_act("#7C3AED", "打开工程…", self._on_open_project)

        # === 构建：工程 ▾ 顶层菜单 ===
        self._menu_proj_saveas = QMenu(self)

        # ---------- 第 1 组：📦 工程级（按用户要求：打开工程 与 最近工程 同级，最近工程放在打开工程下面） ----------
        self._menu_proj_saveas.addAction(self._act_new_project)
        self._menu_proj_saveas.addAction(self._act_open_project)
        # 🗂  最近工程：直接作为顶层子菜单挂在 工程▾ 下，与「打开工程…」同级（不是它的子节点）
        self._menu_recent_projects = QMenu("最近工程", self)
        self._menu_recent_projects.setToolTipsVisible(True)
        self._menu_proj_saveas.addMenu(self._menu_recent_projects)

        # ---------- 分隔符：工程级 / 蓝图级 ----------
        self._menu_proj_saveas.addSeparator()

        # ---------- 第 2 组：🗺  蓝图级（无分组标题、描述极简化、一个快捷键都不留） ----------
        self._menu_proj_saveas.addAction(self._act_new)
        self._menu_proj_saveas.addAction(self._act_save)
        self._menu_proj_saveas.addAction(self._act_save_as)
        self._menu_proj_saveas.addAction(self._act_open)
        self._menu_proj_saveas.addAction(self._act_delete)

        # === 把构建好的菜单挂到 ToolButton ===
        tb.addSeparator()
        self._btn_proj_menu = QToolButton()
        self._btn_proj_menu.setText("工程 ▾")
        self._btn_proj_menu.setToolTip("新建/打开工程、管理工程内蓝图文件")
        self._btn_proj_menu.setMenu(self._menu_proj_saveas)
        self._btn_proj_menu.setPopupMode(QToolButton.InstantPopup)
        self._btn_proj_menu.setCursor(Qt.PointingHandCursor)
        # 只有文字按钮 → 强制 TextOnly，避免左边留 icon 空白
        self._btn_proj_menu.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._btn_proj_menu.setStyleSheet(
            "QToolButton { background:#7C3AED; color:#FFFFFF; border:1px solid #5B21B6; border-radius:4px; padding:4px 14px; font-weight:600; min-height:22px; }"
            "QToolButton:hover { background:#6D28D9; }"
        )
        tb.addWidget(self._btn_proj_menu)
        tb.addSeparator()

        # --------------- 左2：设置按钮（用户指定"设置放左边第二个位置"） ---------------
        self._btn_settings = QToolButton()
        self._btn_settings.setText("⚙ 设置")
        self._btn_settings.setToolTip("全局设置：快捷键、主题等")
        self._btn_settings.setCursor(Qt.PointingHandCursor)
        self._btn_settings.setToolButtonStyle(Qt.ToolButtonTextOnly)  # 只有文字，同上
        self._btn_settings.setStyleSheet(
            "QToolButton { background:#161B22; color:#C9D1D9; border:1px solid #30363D; border-radius:4px; padding:4px 12px; min-height:22px; }"
            "QToolButton:hover { border-color:#58A6FF; background:#1F6FEB; color:#FFFFFF; }"
        )
        self._btn_settings.clicked.connect(self._on_open_settings)
        tb.addWidget(self._btn_settings)
        tb.addSeparator()

        # ============================================================
        # 【全局标准快捷键：按用户上一轮的硬性要求保留的 4 个
        #   （这 4 个不是"蓝图按钮"的快捷键，是全局通用的，所以不删）
        #   Ctrl+S 保存 / Ctrl+Z 撤销 / Ctrl+C 复制 / Ctrl+V 粘贴
        #   —— 只有快捷键，顶栏没有按钮，符合用户要求。】
        # 另外按之前需求保留：F5 运行 / F6 暂停 / F7 停止 / F8 截图切分（全局热键，非蓝图按钮）
        # 而"蓝图按钮"的快捷键：Ctrl+N / Ctrl+O / Ctrl+Shift+S 这 3 个，按本轮用户要求，一个不留，全部移除。
        # ============================================================
        # 3 个新增：撤销 / 复制 / 粘贴（只有快捷键，不加工具栏按钮）——保留
        self._act_undo = mk_act("#8B949E", "撤销", self._on_undo)
        self._act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self._act_copy = mk_act("#8B949E", "复制", self._on_copy_nodes)
        self._act_copy.setShortcut(QKeySequence("Ctrl+C"))
        self._act_paste = mk_act("#8B949E", "粘贴", self._on_paste_nodes)
        self._act_paste.setShortcut(QKeySequence("Ctrl+V"))
        # 加到主窗口 action 列表（快捷键才会生效），但不 add_toolbtn → 顶栏上看不到按钮
        self.addAction(self._act_undo)
        self.addAction(self._act_copy)
        self.addAction(self._act_paste)
        # Ctrl+S 全局保存：保留（这是用户专门指定要保留的 4 个标准快捷键之一）
        self._act_save_global = mk_act("#0969DA", "保存", self._on_save)
        self._act_save_global.setShortcut(QKeySequence("Ctrl+S"))
        self.addAction(self._act_save_global)

        # --------------- 运行控制组（Qflow风格：暂停 → 运行/停止） ---------------
        self._act_pause = mk_act("#9A6700", "⏸ 暂停", self._on_toggle_pause)
        self._act_pause.setEnabled(False)
        self._act_pause.setToolTip("运行中可点，临时冻结所有步骤；再点一次继续（不会丢状态）")
        self._act_run = mk_act("#238636", "▶ 运行", self._on_run_clicked)
        self._act_stop = mk_act("#DA3633", "■ 停止", self._on_stop_clicked)
        # 【2026-07-03 用户明确：运行/停止/暂停的全局热键用 GlobalHotkeyManager(pynput) 跨窗口注册，
        #  QAction 层面不再设置局部 shortcut（局部只有主窗口激活才生效，游戏前台按不到，违背全局热键的初衷）】
        #  L519-L520 旧的 F5/F8 局部硬编码已删除。
        self._act_run.setShortcut(QKeySequence())
        self._act_stop.setShortcut(QKeySequence())
        self._act_stop.setEnabled(False)
        # 暂停按钮单独保存引用：会动态切"⏸暂停/▶继续"和颜色
        self._btn_pause = add_toolbtn(self._act_pause, bg="#5B4200", color="#F0B429", hover_bg="#875F00", hover_border="#9A6700")
        add_toolbtn(self._act_run, bg="#0F3321", color="#3FB950", hover_bg="#194B31", hover_border="#238636")
        add_toolbtn(self._act_stop, bg="#3B1314", color="#F85149", hover_bg="#58151C", hover_border="#DA3633")
        tb.addSeparator()

        # --------------- 缩放 / 视图工具 / 切图工具组（BP特有切图引擎 双入口） ---------------
        self._act_fit = mk_act("#8B949E", "缩放适应", self._view.zoom_fit)
        self._act_zoom_1 = mk_act("#8B949E", "重置100%", self._view.reset_zoom)
        # 【2026-07-03 用户明确：只保留4个标准快捷键 Ctrl+S/Z/C/V；其他缩放快捷键移除，按钮仍可点击】
        self._act_fit.setShortcut(QKeySequence())
        self._act_zoom_1.setShortcut(QKeySequence())
        add_toolbtn(self._act_fit)
        add_toolbtn(self._act_zoom_1)
        tb.addSeparator()
        self._act_split = mk_act("#7C3AED", "✂ 切分UI元素", self._on_open_split_dialog)
        add_toolbtn(self._act_split, bg="#2A1B4F", color="#A371F7", hover_bg="#381E6F", hover_border="#7C3AED")

        # --------------- 右侧: 目标程序 全局选择器 ---------------
        tb.addSeparator()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        target_hint = QLabel("🎯 目标程序:")
        target_hint.setStyleSheet("color:#C9D1D9; padding: 0 4px 0 8px;")
        tb.addWidget(target_hint)

        self._target_combo = NoWheelComboBox()
        self._target_combo.setMinimumWidth(360)
        self._target_combo.setStyleSheet("QComboBox { background:#161B22; color:#C9D1D9; padding:3px 8px; border:1px solid #30363D; border-radius:4px; } QComboBox:hover { border-color:#484F58; } QComboBox::drop-down { border:none; width:20px; }")
        self._target_combo.currentIndexChanged.connect(self._on_target_window_changed)
        tb.addWidget(self._target_combo)

        btn_refresh = QPushButton("↻ 刷新")
        btn_refresh.setStyleSheet("QPushButton { background:#21262D; color:#C9D1D9; border:1px solid #30363D; border-radius:4px; padding:3px 10px; } QPushButton:hover { background:#30363D; }")
        btn_refresh.clicked.connect(self._reload_target_windows)
        tb.addWidget(btn_refresh)

        btn_launch = QPushButton("🚀 选EXE启动")
        btn_launch.setStyleSheet("QPushButton { background:#21262D; color:#C9D1D9; border:1px solid #30363D; border-radius:4px; padding:3px 10px; } QPushButton:hover { background:#30363D; }")
        btn_launch.clicked.connect(self._on_target_launch_exe)
        tb.addWidget(btn_launch)

        # --------------- 右侧最后：全屏快捷开关（按你截图还原，之前重构工具栏弄丢了） ---------------
        self._chk_fullscreen = QCheckBox("全屏(所有屏幕)")
        self._chk_fullscreen.setStyleSheet("QCheckBox { color:#C9D1D9; padding: 2px 12px 2px 6px; spacing: 6px; }")
        self._chk_fullscreen.setToolTip("勾选 = 强制目标为全屏（所有屏幕），忽略下拉框选择；取消 = 按下方下拉框选择目标窗口")
        self._chk_fullscreen.toggled.connect(self._on_toggle_fullscreen_shortcut)
        tb.addWidget(self._chk_fullscreen)

        # 【构建完菜单后】先填一次"最近工程"子菜单内容
        self._refresh_recent_projects_menu()

    # ---------------- 目标程序选择器 ----------------
    def get_active_hwnd(self) -> Optional[int]:
        return self._active_hwnd

    def _reload_target_windows(self):
        c = self._target_combo
        c.blockSignals(True)
        prev_id = c.currentData()
        c.clear()
        c.addItem("🖥 全屏（所有屏幕）", None)
        wins = list_visible_windows()
        wins_sorted = sorted(wins, key=lambda w: w["title"].lower())
        for w in wins_sorted:
            title = w["title"].strip() or "(无标题)"
            if len(title) > 70:
                title = title[:67] + "..."
            label = f"[{w['hwnd']:08X}] {title}"
            c.addItem(label, int(w["hwnd"]))
        # 恢复之前选中的
        match = prev_id and c.findData(prev_id) >= 0
        if match:
            c.setCurrentIndex(c.findData(prev_id))
        else:
            c.setCurrentIndex(0)
        self._active_hwnd = c.currentData()
        c.blockSignals(False)
        self._status_lbl.setText(f"目标窗口列表已刷新，共 {len(wins_sorted)} 个窗口" + (f"，当前：HWND={self._active_hwnd:08X}" if self._active_hwnd else "，当前：全屏"))

    def _on_target_window_changed(self, idx: int):
        self._active_hwnd = self._target_combo.currentData()
        if self._active_hwnd:
            # 记录下拉框标题文本，用于后续HWND失效自动重新匹配
            txt = self._target_combo.currentText() or ""
            title_only = txt.split("] ", 1)[1] if "] " in txt else txt
            self._active_hwnd_title_key = title_only
            self._status_lbl.setText(f"已选定目标窗口 HWND={self._active_hwnd:08X}")
            self._log.append_log(f"[Target] 目标窗口已选定：HWND={self._active_hwnd:08X}  → {self._target_combo.currentText()}", "INFO")
        else:
            self._active_hwnd_title_key = None
            self._status_lbl.setText("目标未指定：截图/找图将使用全屏")

    def _on_target_launch_exe(self):
        fp, _ = QFileDialog.getOpenFileName(self, "选择要启动的EXE（自动化目标程序）", "", "应用程序 (*.exe);;所有文件 (*)")
        if not fp:
            return
        ok, pid, msg = start_exe(fp, wait_sec_after_launch=1.5, log_fn=lambda m, l="INFO": self._log.append_log(f"[Target] {m}", l))
        if not ok:
            QMessageBox.warning(self, "启动失败", msg)
            return
        QMessageBox.information(self, "已启动", f"{os.path.basename(fp)}\nPID={pid}\n\n将在刷新后自动加入目标窗口列表")
        QTimer.singleShot(600, self._reload_target_windows)

    # ---------------- 目标程序：全屏快捷开关 ----------------
    def _on_toggle_fullscreen_shortcut(self, checked: bool):
        """勾选 = 目标强制全屏（所有屏幕），并禁用下拉框避免误选；取消则恢复可选择状态。"""
        try:
            if not hasattr(self, "_target_combo"):
                return
            c = self._target_combo
            if checked:
                self._last_non_fullscreen_idx = c.currentIndex() if c.currentData() is not None else None
                c.blockSignals(True)
                try:
                    c.setCurrentIndex(0)
                finally:
                    c.blockSignals(False)
                c.setEnabled(False)
                self._active_hwnd = None
                self._active_hwnd_title_key = None
                self._status_lbl.setText("目标已切换：全屏（所有屏幕）[快捷开关已锁定]")
            else:
                c.setEnabled(True)
                if getattr(self, "_last_non_fullscreen_idx", None) is not None:
                    c.blockSignals(True)
                    try:
                        c.setCurrentIndex(max(0, int(self._last_non_fullscreen_idx)))
                    finally:
                        c.blockSignals(False)
                self._active_hwnd = c.currentData()
                self._active_hwnd_title_key = None
                if self._active_hwnd:
                    self._status_lbl.setText(f"目标已恢复选择：HWND={self._active_hwnd:08X}")
                else:
                    self._status_lbl.setText("目标已恢复选择：全屏（所有屏幕）")
        except Exception as e:
            self._log.append_log(f"[Target] 全屏开关异常：{e}", "WARN")

    # ---------------- 工程 ▾：最近工程菜单 ----------------
    def _refresh_recent_projects_menu(self):
        """根据 ProjectManager.recent_projects 重建下拉里的最近工程子菜单。"""
        try:
            menu = getattr(self, "_menu_recent_projects", None)
            if menu is None:
                return
            menu.clear()
            pm = self._pm
            lst = pm.recent_projects(limit=8)
            if not lst:
                act_empty = QAction("（暂无最近工程，打开/新建后会出现在这里）", menu)
                act_empty.setEnabled(False)
                menu.addAction(act_empty)
                return
            for i, path in enumerate(lst):
                name = None
                try:
                    meta_path = os.path.join(path, ".bp", "project_meta.json")
                    if os.path.isfile(meta_path):
                        with open(meta_path, "r", encoding="utf-8") as f:
                            md = json.load(f)
                        if isinstance(md, dict) and md.get("project_name"):
                            name = str(md["project_name"]).strip()
                except Exception:
                    name = None
                if not name:
                    name = os.path.basename(os.path.normpath(path)) or path
                text_prefix = f"&{i+1}. " if i < 9 else f"    "
                action = QAction(f"{text_prefix}{name}", menu)
                action.setToolTip(path)
                action.setData(path)
                action.triggered.connect(lambda _checked=False, p=path: self._on_open_recent_project(p))
                menu.addAction(action)
        except Exception as e:
            try:
                self._log.append_log(f"[Project] 最近工程菜单刷新失败：{e}", "WARN")
            except Exception:
                pass

    def _on_open_recent_project(self, project_root: str):
        """点最近工程菜单项 → 直接 open_project（不弹选择框）。"""
        try:
            if not project_root or not os.path.isdir(project_root):
                QMessageBox.warning(self, "打开失败", f"工程目录已不存在：\n{project_root}\n\n下次刷新菜单会自动移除")
                self._refresh_recent_projects_menu()
                return
            if not self._confirm_discard():
                return
            ok = self._pm.open_project(project_root)
            if not ok:
                QMessageBox.warning(self, "打开失败", f"不是合法的 Blueprint 工程目录：\n{project_root}")
                return
            self._load_or_new_graph_for_current_project()
            self._refresh_recent_projects_menu()
        except Exception as e:
            QMessageBox.critical(self, "打开失败", f"打开最近工程时出错：\n{e}")

    def _build_statusbar(self):
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status_lbl = QLabel("就绪")
        self._status_lbl.setStyleSheet("color: #C9D1D9; font-size: 10px; padding: 0 8px;")
        self._status.addWidget(self._status_lbl, 1)
        self._status_run = QLabel("状态: 待机")
        self._status_run.setStyleSheet("color: #8B949E; font-size: 10px; padding: 0 8px;")
        self._status.addPermanentWidget(self._status_run)
        # 全局热键常驻提示（让用户记住快捷键，不用点编辑器按钮）
        self._status_hotkey_hint = QLabel("🎯 正在加载热键设置...")
        self._status_hotkey_hint.setStyleSheet(
            "color: #D29922; font-size: 10px; padding: 0 14px 0 10px; border-left: 1px solid #2D333B; margin-left: 8px;"
        )
        self._status.addPermanentWidget(self._status_hotkey_hint)
        self._refresh_hotkey_status_hint()

    # ==================== 信号连接 ====================
    def _wire_signals(self):
        self._scene.node_selected.connect(self._on_scene_node_selected)
        self._scene.graph_changed.connect(self._update_title)
        self._inspector.param_changed.connect(self._on_param_changed)
        self._inspector.request_pick_template.connect(self._on_pick_template)
        self._inspector.request_fill_from_main_target.connect(self._on_fill_target_from_main)
        self._inspector.request_launch_exe_for_node.connect(self._on_launch_exe_for_node)
        self._palette.info_message.connect(lambda m, l: self._log.append_log(m, l))
        # 注意：全局热键已由 GlobalHotkeyManager.set_callback 直接绑定到主线程 QTimer.singleShot，
        # 无需额外 bridge.connect

    # ==================== 基础动作 ====================
    def _bind_new_graph(self, graph: BlueprintGraph, zoom_fit: bool = True):
        """把新蓝图 graph 绑定到画布和所有关联面板，统一清理残留状态。
        作用：避免切换蓝图后 Inspector 仍显示旧节点参数、画布选中高亮残留等。
        """
        # 1) 先清理旧选中状态：画布取消所有高亮 + Inspector面板清空
        try:
            self._scene.clearSelection()
        except Exception:
            pass
        try:
            self._inspector.set_node(None)  # 关键！避免属性面板显示旧蓝图节点参数
        except Exception:
            pass
        # 2) 绑定到 scene（会清空旧节点/边，重新渲染）
        self._scene.set_graph(graph)
        self._graph = graph
        # 3) 按要求缩放视图
        if zoom_fit and self._view is not None:
            try:
                self._view.zoom_fit()
            except Exception:
                pass

    def _new_graph(self):
        g = BlueprintStore.new_graph("Untitled")
        self._current_path = None
        self._last_saved_graph_hash = None  # 新建=从未保存过，hash置空
        self._bind_new_graph(g, zoom_fit=False)  # 新空图不用缩放
        self._log.append_log("新建空白蓝图。", "SUCCESS")
        self._update_title()

    def _on_new(self):
        if not self._confirm_discard():
            return
        self._new_graph()

    def _on_open(self):
        if not self._confirm_discard():
            return
        # 默认路径优先级：
        #   1) 已有蓝图所在的目录/文件（用户切换工程内其他蓝图方便）
        #   2) 当前工程根目录（用户刚进工程，第一次加载就定位到正确目录，不会跑到项目根）
        #   3) 兜底 os.getcwd()
        default_open_dir = self._current_path
        if not default_open_dir:
            default_open_dir = self._pm.project_root if self._pm and self._pm.project_root else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "打开蓝图",
            default_open_dir,
            "蓝图文件 (*.bp.json *.json)",
        )
        if not path:
            return
        g = BlueprintStore.load(path)
        if g is None:
            QMessageBox.warning(self, "打开失败", "无法读取蓝图文件，格式可能已损坏。")
            return
        self._current_path = path
        self._mark_graph_clean()  # 刚加载，视为干净状态（在 bind 前设置 hash）
        self._bind_new_graph(g, zoom_fit=True)  # 统一绑定 + 清空Inspector残留 + 缩放
        try:
            self._pm.set_last_opened_blueprint(path)
        except Exception:
            pass
        self._log.append_log(f"已加载蓝图: {path}", "SUCCESS")
        self._update_title()

    def _on_save(self):
        if self._graph is None:
            return
        if not self._current_path:
            self._on_save_as()
            return
        ok = BlueprintStore.save(self._graph, self._current_path)
        if ok:
            self._mark_graph_clean()  # 保存成功，哈希更新=干净了
            try:
                self._pm.set_last_opened_blueprint(self._current_path)
            except Exception:
                pass
            self._log.append_log(f"已保存: {self._current_path}", "SUCCESS")
            self._update_title()
        else:
            QMessageBox.warning(self, "保存失败", "写入文件失败，请检查路径与权限。")

    def _on_save_as(self):
        if self._graph is None:
            return
        default_dir = self._pm.project_root or os.getcwd()
        default = self._current_path or BlueprintStore.default_path(self._graph, default_dir)
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", default,
            "蓝图文件 (*.bp.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".bp.json"):
            path = path + ".bp.json"
        ok = BlueprintStore.save(self._graph, path)
        if ok:
            self._current_path = path
            self._mark_graph_clean()  # 另存成功，哈希更新=干净了
            try:
                self._pm.set_last_opened_blueprint(path)
            except Exception:
                pass
            self._log.append_log(f"另存为: {path}", "SUCCESS")
            self._update_title()
        else:
            QMessageBox.warning(self, "保存失败", "写入文件失败。")

    def _on_delete_blueprint(self):
        """从磁盘上删除当前蓝图文件（真删，不可恢复）。"""
        if self._graph is None:
            QMessageBox.information(self, "提示", "当前没有蓝图可删除。")
            return
        if not self._current_path:
            QMessageBox.warning(
                self, "无法删除",
                "当前蓝图是尚未保存过的空白蓝图，不需要删除，直接点「工程 ▾ → 新建蓝图」换新即可。"
            )
            return
        path = self._current_path
        disp = path
        if not os.path.isfile(path):
            ret = QMessageBox.question(
                self, "文件不存在",
                f"记录的蓝图路径不存在（可能已手动被外部删除：\n{disp}\n\n是否清空当前编辑内容并置为空白蓝图？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if ret == QMessageBox.Yes:
                self._new_graph()
            return
        base = os.path.basename(path)
        ret = QMessageBox.warning(
            self, "⚠ 删除蓝图（不可恢复）",
            f"确定要从磁盘上永久删除这个蓝图文件吗？\n\n  · {disp}\n\n⚠此操作不可恢复！\n（当前蓝图中的节点/连线将一起被删掉，且不会进回收站。）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            import send2trash  # type: ignore
            has_trash = True
        except Exception:
            has_trash = False
        ok = False
        err = ""
        if has_trash:
            try:
                send2trash.send2trash(path)
                ok = True
            except Exception as e:
                err = f"移动到回收站失败：{e}；改为彻底删除？"
        if not ok:
            if err:
                r2 = QMessageBox.question(
                    self, "回收站不可用", err + "\n\n是否仍要彻底删除？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if r2 != QMessageBox.Yes:
                    return
            try:
                os.remove(path)
                ok = True
            except Exception as e:
                ok = False
                err = str(e)
        if not ok:
            QMessageBox.critical(self, "删除失败", f"删除蓝图文件失败：\n{err}")
            return
        self._log.append_log(f"🗑 蓝图文件已从磁盘删除：{disp}", "WARN")
        # 如果删的就是最近使用的蓝图，清空 last_opened 避免下次打开崩溃
        try:
            last_bp = self._pm.get_last_opened_blueprint()
            if path and last_bp and os.path.abspath(path) == os.path.abspath(last_bp):
                self._pm.set_last_opened_blueprint(None)
        except Exception:
            pass
        # 清成空白蓝图
        self._new_graph()

    def _confirm_discard(self) -> bool:
        """切换/新建蓝图前确认是否丢弃当前修改。
        只有『未保存过的新蓝图（_current_path为空）』才提示，
        已经保存过的蓝图（即使有修改）暂不做复杂脏状态追踪，后续可加。
        """
        if self._graph is None:
            return True
        if not self._need_save():
            return True  # 确定不需要保存，直接放行不弹窗
        path_disp = self._current_path or "未保存的蓝图"
        ret = QMessageBox.question(
            self, "提示",
            f"是否丢弃当前蓝图的未保存修改？\n\n{path_disp}",
            QMessageBox.Discard | QMessageBox.Cancel | QMessageBox.Save,
            QMessageBox.Save,
        )
        if ret == QMessageBox.Save:
            self._on_save()
            return not self._need_save()
        return ret == QMessageBox.Discard

    def _hash_graph(self, graph) -> Optional[str]:
        """把蓝图序列化成 JSON 后算 SHA1 哈希，用于快速判断内容是否改动过。
        用 sort_keys=True 保证同一份内容每次算出来哈希都一样，不受字段顺序影响。
        """
        if graph is None:
            return None
        try:
            data = graph.to_dict()
            # 和 BlueprintStore.save 完全一致：sort_keys + ensure_ascii + indent=2
            canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2)
            return hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _mark_graph_clean(self):
        """把当前图标记为『干净状态』（未改动）——在加载成功/保存成功后调用。"""
        self._last_saved_graph_hash = self._hash_graph(self._graph)

    def _need_save(self) -> bool:
        """当前蓝图是否需要保存。
        规则：
          ① 从未落盘过（_current_path 为空，且 _last_saved_graph_hash 也为空）→ 必须保存
          ② 已落盘过 → 对比当前蓝图哈希 vs 上次保存哈希：不一致=内容被改动过，要保存
          ③ 其他情况 → 不用保存
        """
        if self._graph is None:
            return False
        # 从未落盘的新蓝图（或者新建了一张还没保存）
        if not self._current_path:
            return True
        # 已落盘过：对比哈希判断有没有改动过
        current_hash = self._hash_graph(self._graph)
        if self._last_saved_graph_hash is None:
            # 理论不会走到这（已有 path 但没记录 hash），保守提示需要保存
            return True
        return current_hash != self._last_saved_graph_hash

    def _update_title(self):
        app_name = "蓝图自动化工具"
        # 1. 工程名部分
        project_part = ""
        if self._pm.is_open() and self._pm.project_name:
            project_part = str(self._pm.project_name).strip()
        # 2. 蓝图名部分
        blueprint_name = ""
        saved_mark = ""
        if self._graph is not None:
            blueprint_name = str(self._graph.name or "未命名蓝图").strip() or "未命名蓝图"
            if self._need_save():
                saved_mark = " *"  # 未保存 / 有改动 → 标题栏加星号
        # 3. 拼接标题逻辑：有工程就放最前面；/号只在「同时有工程和蓝图」时显示，避免多余符号
        if project_part and blueprint_name:
            title = f"{project_part}  /  {blueprint_name}{saved_mark} — {app_name}"
        elif project_part:
            title = f"{project_part} — {app_name}"
        elif blueprint_name:
            title = f"{blueprint_name}{saved_mark} — {app_name}"
        else:
            title = app_name
        self.setWindowTitle(title)
        # 状态栏：节点数/连线数
        self._status_lbl.setText(
            f"节点: {len(self._graph.nodes) if self._graph else 0}  "
            f"连线: {len(self._graph.edges) if self._graph else 0}"
        )

    # ==================== 画布事件 ====================
    def _on_scene_node_selected(self, node_id: str):
        if self._graph is None or node_id not in self._graph.nodes:
            self._inspector.set_node(None)
            return
        self._inspector.set_node(self._graph.nodes[node_id])

    def _on_param_changed(self, node_id: str, param_name: str, value):
        if self._graph is None or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]
        node.params[param_name] = value
        ni = self._scene.node_item(node_id)
        if ni is not None:
            # FindAnyImage 多模板分支动态更新（保留一个简化：只做简单日志）
            pass
        self._scene.graph_changed.emit()

    def _on_pick_template(self, node_id: str, param_name: str):
        if not self._ensure_project_open("裁剪模板保存到工程templates/"):
            return
        self._log.append_log("调用裁图工具 (region_selector)...", "INFO")
        try:
            from src.tools.region_selector import RegionSelector
            sel = RegionSelector(None)
            result = sel.select()
            if result is None:
                self._log.append_log("裁图已取消。", "INFO")
                return
            img_bgr, region = result
            # ✅ 保存到「当前工程的 templates/」目录（而不是全局 APP_DIR/images/1080p，避免串工程
            from src.config.app_paths import template_dir
            save_dir = template_dir()
            os.makedirs(save_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ms = int(time.time() * 1000) % 1000
            filename = f"tpl_{ts}_{ms:03d}.png"
            full = os.path.join(save_dir, filename)
            import cv2
            ok, buf = cv2.imencode(".png", img_bgr)
            if ok:
                buf.tofile(full)
            self._on_param_changed(node_id, param_name, filename)
            self._log.append_log(f"模板已裁到: {full}", "SUCCESS")
            # 重新 set_node 刷新表单
            if self._graph and node_id in self._graph.nodes:
                self._inspector.set_node(self._graph.nodes[node_id])
        except Exception as e:
            self._log.append_log(f"裁图失败: {e}", "ERROR")
            traceback.print_exc()

    # ---------- TargetSetup 快捷填充（Inspector面板动作条） ----------
    def _on_fill_target_from_main(self, node_id: str):
        if not self._graph or node_id not in self._graph.nodes:
            return
        hwnd = self.get_active_hwnd()
        if not hwnd:
            QMessageBox.information(self, "没选目标窗口", "请先在工具栏右侧「🎯 目标程序」下拉里选一个窗口。")
            return
        try:
            import win32gui
            title = win32gui.GetWindowText(hwnd) or ""
            try:
                _tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            params = {
                "window_title": title.strip()[:80] if title.strip() else "",
                "resolution_w": int(width),
                "resolution_h": int(height),
            }
            if pid:
                try:
                    import psutil
                    p = psutil.Process(pid)
                    exe_path = p.exe()
                    if exe_path and os.path.isfile(exe_path):
                        params["exe_path"] = exe_path
                except Exception:
                    pass
            self._inspector.apply_params_merge(params)
            self._log.append_log(f"[TargetSetup] 从主界面目标填充 window_title=\"{params.get('window_title')}\"  res={params.get('resolution_w')}x{params.get('resolution_h')}  exe={params.get('exe_path') or '(未填)'}", "SUCCESS")
            # 刷新 Inspector 表单显示
            node = self._graph.nodes.get(node_id)
            if node:
                self._inspector.set_node(node)
        except Exception as e:
            self._log.append_log(f"[TargetSetup] 填充失败: {e}", "ERROR")
            traceback.print_exc()

    def _on_launch_exe_for_node(self, node_id: str):
        if not self._graph or node_id not in self._graph.nodes:
            return
        fp, _ = QFileDialog.getOpenFileName(self, "选择要启动的EXE（自动化目标程序）", "", "应用程序 (*.exe);;所有文件 (*)")
        if not fp or not os.path.isfile(fp):
            return
        self._inspector.apply_params_merge({"exe_path": fp, "wait_sec_after_launch": 1.5})
        # 立即启动一次，方便用户看到效果，并加入目标下拉
        ok, pid, msg = start_exe(fp, wait_sec_after_launch=1.5, log_fn=lambda m, l="INFO": self._log.append_log(f"[TargetSetup] {m}", l))
        if not ok:
            QMessageBox.warning(self, "启动失败", msg)
            return
        self._log.append_log(f"[TargetSetup] 已写入exe_path={fp}", "SUCCESS")
        QTimer.singleShot(900, self._reload_target_windows)
        # 刷新 Inspector 表单显示
        node = self._graph.nodes.get(node_id)
        if node:
            self._inspector.set_node(node)

    # ==================== 运行/停止 ====================
    def _on_run_clicked(self):
        if self._graph is None:
            QMessageBox.warning(self, "无法运行", "当前没有蓝图。")
            return
        if not self._ensure_project_open("运行蓝图（会在工程目录下生成debug缓存"):
            return
        if self._runner is not None and self._runner.isRunning():
            self._log.append_log("已有运行中的蓝图，先停止。", "WARN")
            return
        # 1. 静态校验（§九）
        issues = validate_graph(self._graph)
        if issues:
            msg = format_issues(issues)
            if has_errors(issues):
                ret = QMessageBox.question(
                    self, "静态校验发现错误",
                    f"发现以下问题（含ERROR）：\n\n{msg}\n\n强制运行（不推荐）？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if ret != QMessageBox.Yes:
                    self._log.append_log("静态校验未通过，已取消运行。", "WARN")
                    return
            else:
                self._log.append_log("静态校验提示：\n" + msg, "WARN")
        # 2. 启动 runner（QThread 硬约束 §4）
        runner = BlueprintRunner(self._graph, self)
        runner.log_message.connect(self._log.append_log, Qt.QueuedConnection)
        runner.highlight_node.connect(
            lambda nid, s=self._scene: s.set_running_node(nid), Qt.QueuedConnection,
        )
        runner.highlight_clear.connect(
            lambda: self._scene.clear_running(), Qt.QueuedConnection,
        )
        runner.progress_step.connect(
            lambda step, ntype: self._status_lbl.setText(f"执行 Step {step}  {ntype}"),
            Qt.QueuedConnection,
        )
        runner.finished_ok.connect(self._on_runner_finished)
        runner.finished_err.connect(self._on_runner_error)
        runner.finished.connect(lambda: self._on_runner_ended())
        self._runner = runner
        runner.pause_changed.connect(self._apply_pause_state_to_ui)
        self._scene.clear_running()
        self._act_run.setEnabled(False)
        self._act_pause.setEnabled(True)
        self._apply_pause_state_to_ui(False)
        self._act_stop.setEnabled(True)
        stop_qt   = pynput_to_qkeysequence(get_shortcut("stop_blueprint",  self._user_settings))
        pause_qt  = pynput_to_qkeysequence(get_shortcut("pause_blueprint", self._user_settings))
        run_qt    = pynput_to_qkeysequence(get_shortcut("run_blueprint",   self._user_settings))
        self._status_run.setText(f"状态: 运行中  [{stop_qt} 停止 | {pause_qt} 暂停/继续]")
        self._status_run.setStyleSheet("color: #3FB950; font-size: 10px; padding: 0 8px;")
        runner.start()
        self._log.append_log(
            f"▶ 蓝图运行中（运行快捷键={run_qt}），按 {stop_qt} 优雅停止，按 {pause_qt} 暂停/继续当前蓝图。",
            "INFO",
        )

    def _on_toggle_pause(self):
        if self._runner is None or not self._runner.isRunning():
            self._log.append_log("当前没有运行中的蓝图，无法暂停/继续。", "INFO")
            return
        now_paused = self._runner.toggle_pause()
        self._log.append_log(
            "⏸ 已暂停，运行冻结（不丢状态 / 不变量）。再点「继续」恢复。"
            if now_paused else "▶ 已恢复执行。",
            "WARN" if now_paused else "INFO",
        )

    def _apply_pause_state_to_ui(self, paused: bool):
        """根据暂停/继续，动态切换暂停按钮的文字/背景色/状态栏提示。"""
        try:
            if hasattr(self, "_act_pause"):
                if paused:
                    self._act_pause.setText("▶ 继续")
                    self._act_pause.setIcon(self._make_icon("#238636"))
                else:
                    self._act_pause.setText("⏸ 暂停")
                    self._act_pause.setIcon(self._make_icon("#9A6700"))
            # 暂停按钮背景色：暂停=黄→继续=绿
            if hasattr(self, "_btn_pause") and self._btn_pause is not None:
                if paused:
                    self._btn_pause.setStyleSheet(
                        "QToolButton { background:#0F3321; color:#3FB950; border:1px solid #238636; border-radius:4px; padding:4px 10px; }"
                        "QToolButton:hover { background:#194B31; }"
                        "QToolButton:disabled { color:#484F58; background:#1C2128; }"
                    )
                else:
                    self._btn_pause.setStyleSheet(
                        "QToolButton { background:#5B4200; color:#F0B429; border:1px solid #9A6700; border-radius:4px; padding:4px 10px; }"
                        "QToolButton:hover { background:#875F00; }"
                        "QToolButton:disabled { color:#484F58; background:#1C2128; }"
                    )
            if paused:
                self._status_run.setText("状态: 已暂停  [按⏸按钮继续]")
                self._status_run.setStyleSheet("color: #F0B429; font-size: 10px; padding: 0 8px;")
        except Exception:
            pass

    def _on_stop_clicked(self):
        if self._runner is not None and self._runner.isRunning():
            stop_qt = pynput_to_qkeysequence(get_shortcut("stop_blueprint", self._user_settings))
            self._log.append_log(f"■ 请求优雅停止 ({stop_qt})，等待已入队节点完成...", "WARN")
            self._runner.request_stop()
        else:
            self._log.append_log("当前没有运行中的蓝图。", "INFO")

    def _on_runner_finished(self):
        self._log.append_log("✔ 蓝图执行完成。", "SUCCESS")

    def _on_runner_error(self, err: str):
        self._log.append_log(f"✘ 蓝图执行异常: {err}", "ERROR")
        try:
            QMessageBox.critical(self, "运行异常", f"蓝图执行发生异常：\n\n{err}")
        except Exception:
            pass

    def _on_runner_ended(self):
        self._act_run.setEnabled(True)
        self._act_pause.setEnabled(False)
        self._apply_pause_state_to_ui(False)
        self._act_stop.setEnabled(False)
        self._status_run.setText("状态: 待机")
        self._status_run.setStyleSheet("color: #8B949E; font-size: 10px; padding: 0 8px;")
        try:
            self._scene.clear_running()
        except Exception:
            pass
        self._status_lbl.setText(
            f"节点: {len(self._graph.nodes) if self._graph else 0}  "
            f"连线: {len(self._graph.edges) if self._graph else 0}"
        )
        self._runner = None

    # ==================== 辅助 ====================
    @staticmethod
    def _make_icon(hex_color: str):
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(hex_color))
        p.drawRoundedRect(1, 1, 14, 14, 3, 3)
        p.end()
        return QIcon(pix)

    # ==================== 截图 / 切分工具 (S3 / S4 hooks) ====================
    def _build_tool_hooks(self) -> dict:
        return {
            "create_region_node": self._hook_create_region_node,
            "create_find_image_node": self._hook_create_find_image_node,
            "get_current_context_node_type": self._hook_current_node_type,
            "patch_current_node": self._hook_patch_current_node,
            "get_active_hwnd": self.get_active_hwnd,
        }

    def _on_open_split_dialog(self):
        if self._scene is None:
            return
        if not self._ensure_project_open("切分UI元素（结果要保存到工程 screenshots/"):
            return
        # 用户手动点按钮/菜单打开切图界面：不强制传 initial_hwnd（避免"我没选图就自动切了一张不知道哪来的图 + 找不到图在哪"）
        dlg = ElementSplitDialog(
            hooks=self._build_tool_hooks(), parent=self,
            initial_hwnd=None,
        )
        dlg.exec()

    # ============================================================
    # 标准快捷键实现（硬编码，不在设置暴露）：Ctrl+C 复制 / Ctrl+V 粘贴 / Ctrl+Z 撤销
    #   - MVP 级：复制选中节点的 type/params/pos → 粘贴时 x+20,y+20 新建 → 撤销=删除最近粘贴的节点
    #   - 后续 P0.2 整体扩展撤销栈时，会增加「拖动位置 / 连线 / 删除节点 / 参数修改」等操作支持
    # ============================================================
    def _on_copy_nodes(self):
        if self._scene is None or self._scene.graph is None:
            return
        ids: List[str] = self._scene.selected_node_ids()
        if not ids:
            return
        snap = []
        for nid in ids:
            node = self._scene.graph.nodes.get(nid)
            if node is None:
                continue
            snap.append({
                "type": node.type,
                "params": copy.deepcopy(node.params or {}),
                "x": int(node.pos[0]) if node.pos else 200,
                "y": int(node.pos[1]) if node.pos else 200,
            })
        self._clipboard_nodes = snap
        self._log.append_log(f"[剪贴板] 已复制 {len(snap)} 个节点（Ctrl+V 粘贴）", "INFO")

    def _on_paste_nodes(self):
        if self._scene is None or self._scene.graph is None:
            return
        snap = self._clipboard_nodes or []
        if not snap:
            return
        if not self._ensure_project_open("粘贴节点（会修改蓝图）"):
            return
        new_ids: List[str] = []
        for item in snap:
            ntype = item.get("type")
            if not ntype or ntype not in NODE_LIBRARY:
                continue
            x = int(item.get("x", 200)) + 20
            y = int(item.get("y", 200)) + 20
            node = self._scene.add_node_by_type(ntype, QPointF(x, y))
            if node is None:
                continue
            params = copy.deepcopy(item.get("params") or {})
            if params:
                node.params.update(params)
                # 强制刷新属性面板（如果当前有节点被选中 / Inspector 绑着）
                try:
                    self._scene.node_param_changed.emit(node.id, "", None)
                except Exception:
                    pass
            new_ids.append(node.id)
        if not new_ids:
            return
        # 清空选中 -> 选中全部粘贴出来的节点 -> 如果有 Inspector 绑到第一个
        try:
            for it in list(self._scene.selectedItems()):
                try:
                    it.setSelected(False)
                except Exception:
                    pass
        except Exception:
            pass
        for nid in new_ids:
            self._scene.select_node_id(nid)
        if new_ids and hasattr(self, "_inspector"):
            first = self._scene.graph.nodes.get(new_ids[0])
            if first is not None:
                self._inspector.set_node(first)
        # Push undo
        self._undo_stack.append({"kind": "add_nodes", "node_ids": new_ids})
        # 任何写入操作清空 redo
        self._redo_stack.clear()
        self._scene.graph_changed.emit()
        self._log.append_log(f"[粘贴] 已粘贴 {len(new_ids)} 个节点（Ctrl+Z 可撤销本次粘贴）", "INFO")

    def _on_undo(self):
        if self._scene is None or self._scene.graph is None:
            return
        if not self._undo_stack:
            self._log.append_log("[撤销] 撤销栈为空（没有可撤销的操作）", "WARN")
            return
        action = self._undo_stack.pop()
        kind = action.get("kind")
        if kind == "add_nodes":
            # 撤销粘贴：把刚粘贴的节点全删掉
            nids = action.get("node_ids") or []
            for nid in nids:
                self._scene.remove_node(nid)
            self._redo_stack.append(action)
            self._log.append_log(f"[撤销] 撤销粘贴，删除 {len(nids)} 个节点（如需恢复可用右键菜单「重做」，后续会绑 Ctrl+Y）", "INFO")
        else:
            # 其它操作暂未实现（P0.2 统一补齐），把 action 塞回撤销栈避免丢记录
            self._undo_stack.append(action)
            self._log.append_log(f"[撤销] 暂不支持撤销「{kind}」，后续版本会补齐。", "WARN")
        self._scene.graph_changed.emit()

    # ---------- Hooks implementation ----------
    def _hook_create_region_node(self, region_tuple4):
        try:
            from PySide6.QtCore import QPointF
            x, y, w, h = region_tuple4
            scene_center = self._view.mapToScene(
                self._view.viewport().rect().center()
            )
            if scene_center is None:
                scene_center = QPointF(400, 300)
            node = self._scene.add_node_by_type("RegionLiteral", scene_center)
            if node is not None:
                node.params.update({"x": x, "y": y, "w": w, "h": h})
                self._scene.select_node_id(node.id)
                self._inspector.set_node(node)
                self._scene._on_node_moved(node.id)
                try:
                    self._scene.node_created.emit(node.id)
                except Exception:
                    pass
                self._scene.graph_changed.emit()
            return node.id if node else None
        except Exception as e:
            self._log.append_log(f"[Hook] create_region_node 失败: {e}", "ERROR")
            return None

    def _hook_create_find_image_node(self, template_path, region_tuple4_or_None):
        try:
            from PySide6.QtCore import QPointF
            scene_center = self._view.mapToScene(
                self._view.viewport().rect().center()
            )
            if scene_center is None:
                scene_center = QPointF(500, 300)
            node = self._scene.add_node_by_type("FindImage", scene_center)
            if node is None:
                return None
            node.params["template"] = template_path
            node.params["threshold"] = float(node.params.get("threshold", 0.75) or 0.75)
            if region_tuple4_or_None is not None and len(region_tuple4_or_None) == 4:
                rx, ry, rw, rh = region_tuple4_or_None
                node.params["region"] = (int(rx), int(ry), int(rw), int(rh))
            self._scene.select_node_id(node.id)
            self._inspector.set_node(node)
            self._scene._on_node_moved(node.id)
            try:
                self._scene.node_created.emit(node.id)
            except Exception:
                pass
            self._scene.graph_changed.emit()
            return node.id
        except Exception as e:
            self._log.append_log(f"[Hook] create_find_image_node 失败: {e}", "ERROR")
            return None

    def _hook_current_node_type(self) -> Optional[str]:
        _nid, ntype = self._inspector.get_current_node_info()
        return ntype

    def _hook_patch_current_node(self, params_to_merge: dict):
        ok = self._inspector.apply_params_merge(params_to_merge)
        # 同步刷新 scene 里 NodeItem 的显示（如果有根据 param 的显示）
        try:
            nid, _ntype = self._inspector.get_current_node_info()
            if nid:
                ni = self._scene.node_item(nid)
                if ni:
                    ni.update()
        except Exception:
            pass
        return ok

    # ==================== 全局热键 & 游戏内无弹窗截图 ====================
    def closeEvent(self, event):
        """关闭窗口时释放 pynput 全局钩子，避免进程残留/键盘异常。"""
        try:
            self._hotkey_mgr.unregister()
        except Exception:
            pass
        try:
            if self._split_dialog_ref is not None:
                try:
                    self._split_dialog_ref.close()
                except Exception:
                    pass
        except Exception:
            pass
        super().closeEvent(event)

    def _global_hotkey_id_to_callback(self) -> Dict[str, Any]:
        """全局热键ID -> 回调（1-param 0参函数）。对应 user_settings SHORTCUT_REGISTRY 里 scope=global 的4项。"""
        return {
            "capture_target":     self._hotkey_capture_target,
            "run_blueprint":      self._on_run_clicked,
            "pause_blueprint":    self._on_toggle_pause,
            "stop_blueprint":     self._on_stop_clicked,
        }

    def _local_shortcut_key_to_action(self) -> Dict[str, QAction]:
        """
        【2026-07-03 按用户要求：局部快捷键已全部"硬编码标准操作"，不再通过 user_settings / SHORTCUT_REGISTRY 管理。
         以下标准快捷键在 BlueprintMainWindow._build_toolbar() 中直接绑到 QAction.setShortcut：
           Ctrl+N 新建 / Ctrl+O 打开 / Ctrl+S 保存 / Ctrl+Shift+S 另存为 / Ctrl+0 适应 / Ctrl+1 100%
           Ctrl+Z 撤销 / Ctrl+C 复制 / Ctrl+V 粘贴
         此函数返回 {} 作为兼容占位（_apply_shortcuts_from_settings 遍历 SHORTCUT_REGISTRY，
         现在 SHORTCUT_REGISTRY 里没有 scope=local 条目，所以实际不会被调用）。】
        """
        return {}

    def _refresh_hotkey_status_hint(self):
        """刷新状态栏：显示所有4个全局热键的Qt格式显示。"""
        parts = []
        gmap = self._global_hotkey_id_to_callback()
        for key_id in sorted(gmap.keys(), key=lambda k: (
            0 if k == "capture_target" else
            1 if k == "run_blueprint" else
            2 if k == "pause_blueprint" else
            3
        )):
            pyn = get_shortcut(key_id, self._user_settings)
            qt = pynput_to_qkeysequence(pyn) or "(?)"
            parts.append(f"{qt}={shortcut_label(key_id).split('（',1)[0].strip()}")
        self._status_hotkey_hint.setText(
            "🎛 全局热键：" + "   |   ".join(parts) + "   | 点 ⚙设置 修改"
        )

    def _apply_shortcuts_from_settings(self, changed_keys=None, log_success=False):
        """
        根据 self._user_settings 统一绑定快捷键：
          - scope=global：_hotkey_mgr.register(key_id, pynput)
          - scope=local：对应QAction.setShortcut(QKeySequence(qt_str))
        changed_keys=None 表示「全部重新应用」（启动时用）；否则只应用 changed_keys（设置对话框保存后用）。
        """
        s = self._user_settings
        changed_set = set(changed_keys) if changed_keys else None
        global_ok_cnt = 0
        global_fail: list = []
        local_applied = 0

        # ============================================================
        # 【全局热键冲突自动修复】
        # 仅在 changed_keys=None（启动时全量应用）时触发；如果是用户刚在设置对话框里改完并保存的 diff，
        # SettingsDialog 自己已有冲突检测，这里不强制覆盖用户刚选的结果。
        # 策略：按 SHORTCUT_REGISTRY 顺序 = 优先级从高到低；同一个 normalized_pynput 被后出现的低优先级 key_id 占用时，
        #       低优先级 key_id 重置回 _DEFAULT_BY_KEY 默认值并持久化保存，下次启动不再冲突。
        # ============================================================
        if changed_set is None:
            hotkeys_map = s.get("hotkeys") or {}
            need_save = False
            seen_norm: Dict[str, str] = {}
            fixed: list = []
            for row in SHORTCUT_REGISTRY:
                k, _label, _def, scope = row
                if scope != "global":
                    continue
                raw = hotkeys_map.get(k, _def)
                norm = normalize_pynput_hotkey(raw)
                if norm in seen_norm:
                    winner = seen_norm[norm]
                    new_val = _DEFAULT_BY_KEY.get(k, raw)
                    hotkeys_map[k] = new_val
                    need_save = True
                    fixed.append(
                        f"{shortcut_label(k).split('（',1)[0].strip()} {pynput_to_qkeysequence(raw)} 与 "
                        f"{shortcut_label(winner).split('（',1)[0].strip()} 撞键 → 自动改为 {pynput_to_qkeysequence(new_val)}"
                    )
                else:
                    seen_norm[norm] = k
            if need_save:
                s["hotkeys"] = hotkeys_map
                try:
                    save_user_settings(s)
                except Exception as _e:
                    self._log.append_log(
                        f"[热键] 冲突修复后保存 user_settings.json 失败：{_e}，可手动在 ⚙设置 里重新保存。",
                        "ERROR",
                    )
                for msg in fixed:
                    self._log.append_log(f"[热键] ⚠ 冲突自动修复 → {msg}", "WARN")
                self._user_settings = s

        gmap = self._global_hotkey_id_to_callback()
        lmap = self._local_shortcut_key_to_action()
        for k in all_shortcut_keys():
            if changed_set is not None and k not in changed_set:
                continue
            pyn = get_shortcut(k, s)
            qt = pynput_to_qkeysequence(pyn)
            if is_global_shortcut(k):
                if k not in gmap:
                    continue
                try:
                    ok = self._hotkey_mgr.register(k, pyn)
                    if ok:
                        global_ok_cnt += 1
                    else:
                        global_fail.append((k, qt))
                except Exception as e:
                    global_fail.append((k, f"{qt}（{e}）"))
            else:
                act = lmap.get(k)
                if act is None:
                    continue
                try:
                    act.setShortcut(QKeySequence(qt))
                    local_applied += 1
                except Exception as e:
                    global_fail.append((k, f"{qt} 绑定QAction失败 {e}"))

        if log_success:
            if global_ok_cnt:
                self._log.append_log(
                    f"[热键] 已成功注册 {global_ok_cnt} 个全局热键（跨窗口，游戏前台也能按到）",
                    "SUCCESS",
                )
            if global_fail:
                lines = "; ".join(f"{shortcut_label(k).split('（',1)[0]}={q}" for k, q in global_fail)
                self._log.append_log(
                    f"[热键] 以下全局热键注册失败（pynput未安装/无权限/热键被系统占用）：{lines}。"
                    f"可手动点对应按钮执行，或点 ⚙设置 换键后重试。",
                    "WARN",
                )
            if local_applied:
                self._log.append_log(
                    f"[热键] 已应用 {local_applied} 个编辑器局部快捷键（仅主窗口激活时生效）",
                    "INFO",
                )
        elif global_fail:
            lines = "; ".join(f"{shortcut_label(k).split('（',1)[0]}={q}" for k, q in global_fail)
            self._log.append_log(f"[设置] 这些热键应用失败：{lines}", "WARN")

        self._refresh_hotkey_status_hint()

    def _on_open_settings(self):
        """打开设置窗口（现在支持修改全部11个快捷键）。"""
        # 用当前内存里的 user_settings 初始化（SettingsDialog会自己read_user_settings，但保持capture_target兼容行为）
        dlg = SettingsDialog(parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        new_settings = dlg.result_settings
        if not new_settings:
            return
        changed = dlg.changed_shortcuts or {}
        # 更新内存（SettingsDialog已经save_user_settings了，但主窗口内存也要同步）
        self._user_settings = new_settings
        # 立即应用 diff（global replace / local setShortcut）
        if changed:
            self._log.append_log(
                f"[设置] 检测到 {len(changed)} 个快捷键修改，正在立即应用…",
                "INFO",
            )
            self._apply_shortcuts_from_settings(changed_keys=list(changed.keys()), log_success=False)
            nice = []
            for k, (old, new) in changed.items():
                nice.append(f"  · {shortcut_label(k).split('（',1)[0].strip()}:  {pynput_to_qkeysequence(old)}  →  {pynput_to_qkeysequence(new)}")
            self._log.append_log("[设置] 快捷键已更新：\n" + "\n".join(nice), "SUCCESS")
        else:
            self._log.append_log("[设置] 已保存（快捷键与之前一致，无需重新绑定）。", "INFO")
        self._refresh_hotkey_status_hint()

    def _on_emergency_kill(self):
        """紧急杀：不管当前蓝图有没有在优雅停止，直接强制结束（F6）。"""
        r = self._runner
        if r is None:
            self._log.append_log("当前没有运行中的蓝图，无需紧急终止。", "INFO")
            return
        # 先尝试 request_stop + wait 极短时间
        try:
            r.request_stop()
        except Exception:
            pass
        killed = False
        if hasattr(r, "isRunning") and callable(r.isRunning) and r.isRunning():
            try:
                r.quit()
            except Exception:
                pass
            if not r.wait(500):
                # 退出事件循环还在跑：强制 terminate（最后手段）
                try:
                    r.terminate()
                    r.wait(1000)
                    killed = True
                except Exception as e:
                    self._log.append_log(f"[紧急杀] terminate 失败：{e}", "ERROR")
        self._log.append_log(
            f"⚠ 紧急停止已执行（F6）。{'（已强制终止执行线程）' if killed else '（执行线程已退出）'}。"
            f"节点侧若有外部进程/子线程可能仍在运行，请自行确认。",
            "WARN",
        )
        # 重置UI状态
        self._act_run.setEnabled(True)
        self._act_stop.setEnabled(False)
        self._status_run.setText("状态: 已紧急停止 (F6)")
        self._status_run.setStyleSheet("color: #F85149; font-size: 10px; padding: 0 8px;")
        try:
            self._scene.clear_running()
        except Exception:
            pass
        self._runner = None

    def _hotkey_capture_target(self):
        if not self._ensure_project_open("截图保存到工程 screenshots/"):
            # 没工程：把警告当用户打日志也写警告到状态栏且不往下执行（因为写资源要先建工程）
            return
        """
        按主界面「🎯目标程序」下拉选中的 HWND 直接截图：
          1. 不弹窗、不抢焦点（所以游戏不会被切到暂停界面）
          2. 默认 BitBlt 前台帧（D3D/Vulkan 游戏不黑屏）
          3. 保存 PNG 到 screenshots/（裁剪为模板小图单独点「裁图为模板」才写 templates/）
          4. 非模态显示 ElementSplitDialog（但不激活），加载并自动切分
          5. 最后 SetForegroundWindow 把焦点还给游戏窗口
        """
        from src.tools.window_utils import capture_hwnd_bgr, write_png_cn
        from src.config.app_paths import screenshot_dir
        import numpy as np

        hwnd = self._active_hwnd
        if not hwnd:
            msg = "[热键] 请先在主界面「🎯目标程序」下拉里选好要截的窗口！（按 ↻ 刷新后选）"
            self._log.append_log(msg, "WARN")
            self._status_lbl.setText(msg.replace("[热键] ", ""))
            return

        # ========== 预校验 & 自动恢复 ==========
        #   1. IsWindow 失效 → 说明用户关掉/重启过目标程序，旧HWND句柄已被系统回收
        #      → 自动刷新目标列表 + 按上次选中的"标题文本"尝试重新匹配同程序窗口
        #   2. IsIconic 最小化 → 提前警告用户用 PrintWindow 兜底（可能D3D游戏是黑的）
        try:
            import win32gui
            is_valid = bool(win32gui.IsWindow(hwnd))
            is_minimized = bool(is_valid and win32gui.IsIconic(hwnd))
        except Exception:
            is_valid = False
            is_minimized = False

        if not is_valid:
            old_hwnd = hwnd
            old_title = self._active_hwnd_title_key
            self._log.append_log(
                f"[热键] 目标 HWND={old_hwnd:#X} 已失效（窗口已关闭/重启过，旧句柄不再可用），"
                f"正在自动刷新目标列表并尝试按标题匹配新窗口...",
                "WARN",
            )
            self._reload_target_windows()
            # 刷新后再按 old_title 精确/模糊匹配
            match_idx = -1
            c = self._target_combo
            if old_title:
                for i in range(c.count()):
                    it_txt = c.itemText(i) or ""
                    title = it_txt.split("] ", 1)[1] if "] " in it_txt else it_txt
                    if title == old_title:
                        match_idx = i
                        break
                if match_idx < 0:
                    for i in range(c.count()):
                        it_txt = c.itemText(i) or ""
                        title = it_txt.split("] ", 1)[1] if "] " in it_txt else it_txt
                        if (title and old_title and
                            (old_title in title or title in old_title)):
                            match_idx = i
                            break
            if match_idx >= 0:
                c.setCurrentIndex(match_idx)
                # _on_target_window_changed 内部会刷新 self._active_hwnd 和标题key
                hwnd = self._active_hwnd
                if hwnd:
                    self._log.append_log(
                        f"[热键] ✅ 已自动重新匹配到同名窗口：HWND={hwnd:#X} → {c.currentText()}，继续截图。",
                        "SUCCESS",
                    )
                else:
                    msg = (
                        f"[热键] ❌ 原目标窗口已关闭/重启（旧HWND={old_hwnd:#X}），"
                        f"已自动刷新下拉框但未匹配到同名窗口，请在主界面「🎯目标程序」下拉中重新选择。"
                    )
                    self._log.append_log(msg, "ERROR")
                    self._status_lbl.setText(msg.replace("[热键] ", ""))
                    return
            else:
                self._active_hwnd = None
                self._active_hwnd_title_key = None
                msg = (
                    f"[热键] ❌ 原目标窗口已关闭/重启（旧HWND={old_hwnd:#X}），"
                    f"已自动刷新目标程序下拉框，但未找到同名窗口。"
                    f"请在主界面「🎯目标程序」下拉中重新选择要截的窗口。"
                )
                self._log.append_log(msg, "ERROR")
                self._status_lbl.setText(msg.replace("[热键] ", ""))
                return
        elif is_minimized:
            self._log.append_log(
                f"[热键] ⚠️ 目标 HWND={hwnd:#X} 当前最小化，尝试后台PrintWindow策略兜底。"
                f"如截出来是黑图，请先Alt+Tab把目标窗口还原到前台，再按热键截图。",
                "WARN",
            )

        # 1. 截图（默认：优先前台 BitBlt，后台回退 PrintWindow）
        res = capture_hwnd_bgr(hwnd, prefer_front=True)
        if res is None:
            msg = f"[热键] 截图失败：HWND={hwnd:#x} 窗口不存在或被最小化。"
            self._log.append_log(msg, "ERROR")
            self._status_lbl.setText(msg.replace("[热键] ", ""))
            return
        bgr, pil_img, (w, h) = res
        if w <= 0 or h <= 0:
            msg = f"[热键] 截图失败：HWND={hwnd:#x} 返回空尺寸 {w}x{h}。"
            self._log.append_log(msg, "ERROR")
            return

        # 2. 生成文件名：HWND十六进制 + 时间戳
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        fname = f"hotkey_HWND{hwnd:08X}_{ts}.png"
        shots_dir = screenshot_dir()
        os.makedirs(shots_dir, exist_ok=True)
        shot_path = os.path.join(shots_dir, fname)

        # 3. 截图只存 screenshots/ 一份；templates/ 目录放的是用「裁剪为模板」按钮裁出来的小图
        #    不再把同一张全量截图重复写入 templates/，避免：
        #      a) screenshots/ 与 templates/ 重复浪费磁盘
        #      b) templates/ 混进大量非模板级的全屏大图，导致 FindImage 搜索域混乱
        ok1 = write_png_cn(shot_path, bgr[:, :, ::-1])
        self._last_capture_png = shot_path if ok1 else None

        strategy = "FRONT_BITBLT"
        try:
            import win32gui
            if win32gui.GetForegroundWindow() != hwnd:
                strategy = "BACK_PRINTWINDOW"
        except Exception:
            pass

        msg = (
            f"[Ctrl+Shift+A] 截图 OK：HWND={hwnd:#X}  {w}x{h}  策略={strategy}"
            f"  saved→{shot_path}"
        )
        self._log.append_log(msg, "SUCCESS")
        self._status_lbl.setText(
            f"🎯 已截目标窗口 {w}x{h}，策略={strategy}，已打开切分工具但未抢焦点（切回编辑器可看到元素列表）"
        )

        # 4. 非模态打开 ElementSplitDialog，自动加载刚截的图并切分
        try:
            dlg = self._get_or_create_split_dialog()
            dlg.load_and_split_bgr(bgr, source_path_hint=shot_path)
        except Exception as e:
            self._log.append_log(f"[热键] 切分工具加载失败：{e}", "WARN")

        # 5. ⚠️ 还焦点给游戏！（关键：任何游戏一切后台就暂停，所以我们截完、做完切分后，
        #    必须把前台窗口恢复成目标游戏窗口，这样用户感觉不到被切过。
        try:
            import win32gui
            if hwnd and win32gui.IsWindow(hwnd):
                # 轻微延时一下：确保切分工具的 show() 已经发出去但不会跟抢焦点冲突
                def _restore_fg():
                    try:
                        # 允许最小化的情况：先恢复
                        try:
                            placement = win32gui.GetWindowPlacement(hwnd)
                            if placement and len(placement) >= 2 and placement[1] == 2:
                                # SW_SHOWNORMAL = 1 还原最小化
                                win32gui.ShowWindow(hwnd, 1)
                        except Exception:
                            pass
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
                QTimer.singleShot(80, _restore_fg)
        except Exception:
            pass

    def _get_or_create_split_dialog(self) -> ElementSplitDialog:
        """
        单例非模态切分工具窗口：
          - show()而非exec_()（不阻塞、不强制前台）
          - WA_ShowWithoutActivating：打开但不抢焦点
        """
        if self._split_dialog_ref is not None:
            try:
                if not self._split_dialog_ref.isVisible():
                    self._split_dialog_ref.setAttribute(Qt.WA_ShowWithoutActivating, True)
                    self._split_dialog_ref.show()
                    self._split_dialog_ref.raise_()
                return self._split_dialog_ref
            except Exception:
                self._split_dialog_ref = None

        hooks = self._build_tool_hooks()
        # 注意：这里不传 initial_hwnd！
        # 热键路径已经自己调用 capture_hwnd_bgr 抓好了，直接 dlg.load_and_split_bgr(bgr) 即可。
        # 如果传了 initial_hwnd，ElementSplitDialog.__init__ 会再抓一次 + 自动切一次，
        # 然后我们再 load_and_split_bgr 会第二次切，导致元素列表被第二次的结果覆盖。
        dlg = ElementSplitDialog(parent=self, hooks=hooks, initial_hwnd=None)
        dlg.setWindowTitle("✂ 切分UI元素（Ctrl+Shift+A 游戏内直接切到这里）")
        dlg.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 非模态 show()，用户不切回编辑器时，这个窗口在任务栏上但不激活，不会影响游戏
        dlg.show()
        dlg.raise_()
        self._split_dialog_ref = dlg
        # 关闭时自动清引用
        try:
            dlg.destroyed.connect(lambda *_: self._clear_split_dialog_ref())
        except Exception:
            pass
        return dlg

    def _clear_split_dialog_ref(self):
        try:
            self._split_dialog_ref = None
        except Exception:
            pass


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    # ============================================================
    # Windows 图标设置（双保险：AppUserModelID + 全局 WindowIcon）
    #   AppUserModelID：解决 Windows 任务栏显示 Python 解释器图标的经典坑——
    #                   不设这个的话，就算 setWindowIcon，任务栏依然会显示 pythonw.exe 的图标
    # ============================================================
    try:
        if sys.platform.startswith("win"):
            import ctypes
            # AppUserModelID：随便写一个唯一字符串就行，推荐 "公司名.产品名.版本"
            my_appid = "BlueprintAutomation.Holo.Editor.v1"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_appid)
    except Exception:
        pass
    # 加载项目根目录下的 icon.png（用户指定的图标）
    try:
        # 当前 blueprint_app.py 在 src/ 下，上一级就是项目根目录 e:\BlueprintAutomation
        project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon_png = os.path.join(project_root_dir, "icon.png")
        if os.path.isfile(icon_png):
            from PySide6.QtGui import QIcon, QPixmap, Qt as _Qt
            # ============================================================
            # 解决 Windows 任务栏图标太小/模糊：QIcon 必须包含多种尺寸
            # Windows 会自动选最匹配的：16(标题栏)/24/32(任务栏)/48(大图标)/64/128/256(高DPI)
            # ============================================================
            original = QPixmap(icon_png)
            app_icon = QIcon()
            if not original.isNull():
                # 【Windows 全场景尺寸表】
                # 从小到大 / 从常用到少用，覆盖 100%~500% DPI：
                #   16    = 标题栏小图标（100% DPI）
                #   18/20 = 标题栏（125%/150% DPI）
                #   22/24 = 标题栏大（175%/200% DPI）
                #   32    = 任务栏标准（100% DPI）
                #   36/40 = 任务栏中（125%/150% DPI）
                #   48    = 任务栏大/Alt+Tab（100% DPI）
                #   60/64 = Alt+Tab 大预览
                #   72/80 = 超大图标视图
                #   96    = 200% DPI 大任务栏
                #   128/160 = 高DPI超大
                #   192   = 300% DPI 任务栏
                #   256   = 标准最大
                #   384/512 = 400%/500% DPI 或缩略图
                _all_sizes = (
                    16, 18, 20, 22, 24,
                    32, 36, 40, 48,
                    60, 64, 72, 80, 96,
                    128, 160, 192, 256, 384, 512,
                )
                for w in _all_sizes:
                    scaled = original.scaled(
                        w, w,
                        _Qt.KeepAspectRatio,
                        _Qt.SmoothTransformation,
                    )
                    app_icon.addPixmap(scaled)
                # 额外塞一张原图（保证最大尺寸不丢细节）
                app_icon.addPixmap(original)
            if not app_icon.isNull():
                app.setWindowIcon(app_icon)
                # 再把这个多尺寸 icon 存到 app 上，方便主窗口直接拿（避免主窗口再重新缩放一遍）
                app.setProperty("app_icon_multi", app_icon)
                print(f"[BOOT] ✅ 已加载多尺寸程序图标（20 种尺寸+原图）: {icon_png}")
            else:
                print(f"[BOOT] ⚠️  图标文件损坏，未加载: {icon_png}")
        else:
            print(f"[BOOT] ℹ️  未找到 icon.png（路径：{icon_png}），使用系统默认图标")
    except Exception as e:
        print(f"[BOOT] ⚠️  加载 icon.png 失败（不影响使用）：{e}")

    qss = """
    QMainWindow, QWidget { background: #22272E; color: #C9D1D9; }
    QToolBar { background: #1C2128; border: 0; padding: 2px; spacing: 4px; }
    QToolBar QToolButton {
        color: #C9D1D9; background: transparent; border: 1px solid transparent;
        padding: 3px 10px; border-radius: 4px; font-size: 10px;
    }
    QToolBar QToolButton:hover { background: #2D333B; border: 1px solid #3A3F4B; }
    QToolBar QToolButton:disabled { color: #484F58; }
    QStatusBar { background: #1C2128; border-top: 1px solid #2D333B; }
    QMenuBar { background: #1C2128; color: #C9D1D9; }
    QMenuBar::item:selected { background: #2D333B; }
    QDockWidget { titlebar-close-icon: transparent; }
    QToolTip { background: #1C2128; color: #C9D1D9; border: 1px solid #3A3F4B; padding: 4px; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
    QScrollBar::handle:vertical { background: #3A3F4B; min-height: 30px; border-radius: 5px; }
    QScrollBar::handle:vertical:hover { background: #58A6FF; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    """
    app.setStyleSheet(qss)

    # ============================================================
    # 启动流程（按用户最新要求：不直接自动打开第 1 条最近工程，而是先弹『所有最近工程列表』，
    #           让用户自己选一个再进去；想新建/打开其他也能切 Tab。）
    #   实现：复用 ProjectStartupDialog 自带的 3 个 Tab
    #         Tab0 = 新建工程 / Tab1 = 打开工程 / Tab2 = 最近工程列表
    #     有最近工程 → 默认显示 Tab2（最近工程列表），用户选/双击才打开
    #     没有最近工程 → 默认显示 Tab0（新建工程）引导用户创建第一个
    # ============================================================
    pm = ProjectManager.instance()
    startup = ProjectStartupDialog(None)
    try:
        recent_has_data = bool(pm.recent_projects(limit=1))
        tw = startup.findChild(QTabWidget)
        if tw is not None and tw.count() >= 3:
            tw.setCurrentIndex(2 if recent_has_data else 0)
    except Exception:
        pass
    if startup.exec() != QDialog.Accepted:
        # 用户点「退出程序」或者右上角× → 直接退出，不让进
        return 0
    if not startup.result_project_root or not pm.is_open():
        # 理论不会走到这（accept返回时pm已经open了）
        return 1

    win = BlueprintMainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
