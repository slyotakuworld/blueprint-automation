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
    QInputDialog,
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

import uuid

from src.blueprint.core.graph_store import BlueprintStore
from src.blueprint.core.models import BlueprintGraph, Node, Edge, Port
from src.blueprint.core.node_library import NODE_LIBRARY, create_node
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
        # 剪贴板 + 撤销/重做栈（硬编码标准快捷键：Ctrl+C/V/Z/Y，不在设置界面暴露）
        self._clipboard_nodes: list = []
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._applying_undo_redo: bool = False
        self._paste_batch_collecting: bool = False
        self._paste_batch_node_ids: list = []
        # ---- LLM 集成（UI 壳子） ----
        self._llm_settings_dialog_ref = None   # LLM 设置对话框（单例，避免点两次开两窗）
        self._floating_ball = None             # 悬浮球（None=未启用或被隐藏）

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

        # LLM 壳子：启动时按设置决定是否显示悬浮球（关闭开关时不创建，避免无意义窗口占用）
        try:
            from src.config.user_settings import is_llm_enabled
            en = is_llm_enabled()
            if en:
                self._ensure_floating_ball(show=True)
            try:
                # 无论成功失败都写一条日志到面板，让用户知道启动时发生了什么
                self._log.append_log(
                    f"[LLM] 启动时 enabled={'ON' if en else 'OFF'}，悬浮球{'已创建并显示（右下角）' if en and self._floating_ball is not None else '未显示'}",
                    "SUCCESS" if en and self._floating_ball is not None else "INFO"
                )
            except Exception:
                # 日志面板还没初始化好时用 print 兜底（PyCharm/终端能看到）
                print(f"[LLM][Boot] enabled={en}, floating_ball_created={self._floating_ball is not None}")
        except Exception as e:
            try:
                self._log.append_log(f"[LLM] 启动时创建悬浮球异常：{type(e).__name__}: {e}", "ERROR")
            except Exception:
                print(f"[LLM][Boot][ERROR] {type(e).__name__}: {e}", flush=True)

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

    def _finalize_project_switch(self) -> None:
        """工程切换成功后的统一收尾：清全局缓存、停运行中蓝图、日志提示、刷新菜单。

        必须在「所有切工程路径（_open_project_startup / _on_open_recent_project /
        _on_open_project 等）成功调用 _load_or_new_graph_for_current_project
        之后调用，避免新工程沿用旧工程的模块级缓存/运行状态，造成「串工程」。
        """
        # 1) 清 app_paths 里所有模块级缓存（最关键：get_img_path 的路径缓存）
        try:
            from src.config.app_paths import clear_all_project_caches
            clear_all_project_caches()
        except Exception as e:
            try:
                self._log.append_log(f"[Project] 清理工程缓存失败: {e}", "WARN")
            except Exception:
                pass
        # 2) 如果当前打开工程的蓝图还在跑 → 请求它停止（否则新工程截图/运行时，原有蓝图还在后台操作）
        try:
            r = getattr(self, "_runner", None)
            if r is not None and r.isRunning():
                self._log.append_log("[Project] 切换工程：已请求停止前工程正在运行的蓝图", "INFO")
                try:
                    r.request_stop()
                except Exception:
                    pass
        except Exception:
            pass
        # 3) 刷新最近工程菜单 + 标题/状态栏
        try:
            self._refresh_recent_projects_menu()
        except Exception:
            pass
        try:
            # 确保标题刷新（显示新工程名+蓝图名）
            self._update_title()
        except Exception:
            pass
        # 4) 明确的日志提示，用户确认真的切换到了哪个工程
        try:
            if self._pm and self._pm.is_open():
                name = self._pm.project_name or "未命名工程"
                root = self._pm.project_root or ""
                self._log.append_log(
                    f"[Project] 已切换到工程：【{name}】  @  {root}",
                    "SUCCESS",
                )
        except Exception:
            pass

    def _reset_caches_on_switch_project(self):
        """【工程切换核心清理】→ 解决"A工程资源/变量/模板图串到B工程"老问题。

        所有切工程路径最终都会走 _load_or_new_graph_for_current_project，
        所以把清理放在这个入口最前面，保证：
          - 工程A -> 切到工程B 时，A工程的所有内存状态都被完全擦除
          - 不会出现 B 工程里能看到 A 工程的变量、模板图、节点参数等"串资源"
        """
        def _warn(msg: str):
            """写日志（如果 _log 还没初始化就 print），不要静默丢异常。"""
            try:
                if getattr(self, "_log", None) is not None and hasattr(self._log, "append_log"):
                    self._log.append_log(f"[Project][WARN] {msg}", "WARN")
                    return
            except Exception:
                pass
            try:
                print(f"[WARN][switch_project] {msg}", flush=True)
            except Exception:
                pass

        # 1) 执行器：上一工程可能残留的 BlueprintRunner / matcher 模板缓存 / 运行态变量
        try:
            old_runner = getattr(self, "_runner", None)
            if old_runner is not None:
                try:
                    old_runner.request_stop()
                except Exception:
                    pass
                try:
                    old_runner.wait(200)
                except Exception:
                    pass
                self._runner = None
        except Exception as e:
            _warn(f"清理旧 runner 失败（不影响后续）: {type(e).__name__}: {e}")

        # 2) 蓝图状态：清空当前 graph / 当前路径 / 保存 hash / 撤销栈，避免 A 蓝图的 hash / undo 串到 B 工程
        self._graph = None
        self._current_path = None
        self._last_saved_graph_hash = None
        try:
            self._undo_stack.clear()
        except Exception as e:
            _warn(f"清理 undo_stack 失败（不影响后续）: {type(e).__name__}: {e}")
        try:
            self._redo_stack.clear()
        except Exception as e:
            _warn(f"清理 redo_stack 失败（不影响后续）: {type(e).__name__}: {e}")
        try:
            self._scene.clear_all()  # 画布图元/节点卡/边全部清空（scene.graph=None）
        except Exception as e:
            # ⚠ 这个是最危险的：如果 clear_all 抛异常且被静默，用户会以为切到 B 工程了
            #   但画布上还显示 A 工程的所有节点/连线，就会误以为"B加载了A的蓝图"（串资源）。
            #   至少一定要写一条明显的 WARN 日志，让用户/开发者在 debug 时能一眼看到。
            _warn(
                "scene.clear_all() 抛异常！"
                " 画布可能残留旧工程图元（会造成『串资源』视觉假象）："
                f" {type(e).__name__}: {e}"
            )
        try:
            self._scene.clearSelection()
        except Exception as e:
            _warn(f"清理 scene selection 失败（不影响后续）: {type(e).__name__}: {e}")

        # 3) Inspector：绝对不能留着上一工程的节点参数！否则用户切工程后选中
        #    新节点之前，属性面板还显示 A 工程某节点参数（看起来就像串资源）
        try:
            if getattr(self, "_inspector", None) is not None:
                self._inspector.set_node(None)
        except Exception as e:
            _warn(f"清理 inspector 失败（不影响后续）: {type(e).__name__}: {e}")

        # 4) 日志面板：切工程时把"正在运行/A工程的日志"清空（可选，但能减少迷惑）
        try:
            if getattr(self, "_log", None) is not None and hasattr(self._log, "clear"):
                clear_fn = getattr(self._log, "clear", None)
                if callable(clear_fn):
                    clear_fn()
        except Exception as e:
            _warn(f"清理日志面板 失败（不影响后续）: {type(e).__name__}: {e}")

        # 5) 第三方老代码/模块级全局缓存清理：
        #    - template_matcher 目前是 BlueprintRunner 每次 new（ctx级），没单例；
        #      但未来改单例的话这里加 clear_all_caches() 兜底
        try:
            from src.blueprint.runtime.template_matcher import TemplateMatcher
            clear_cls = getattr(TemplateMatcher, "clear_all_caches", None)
            if callable(clear_cls):
                clear_cls()
        except Exception:
            pass
        try:
            from src.config import app_paths as _ap
            c = getattr(_ap, "clear_cache", None)
            if callable(c):
                c()
        except Exception:
            pass

    def _load_or_new_graph_for_current_project(self):
        """当前已经有开工程：按三级优先级加载蓝图，绝不自动创建落盘的 .bp.json 文件（首次create_project除外）
        优先级 1：last_opened_blueprint（用户上次打开/保存的那个）
        优先级 2：工程同名默认蓝图（第一次创建工程时自动生成的那张）
        优先级 3：工程内没有任何蓝图 → 仅显示内存中的空画布，不会在磁盘上创建任何 .bp.json（想新建蓝图请点工程菜单→新建蓝图手动创建）"""
        # ★ 先做【工程切换缓存清理】！
        #   不管这次是"从A切到B"还是"第一次打开工程"，都把可能残留的上一工程内存状态全部清干净，
        #   从根源解决"不同工程串资源"的历史问题。
        self._reset_caches_on_switch_project()
        if self._pm.is_open():
            proj_name = self._pm.project_name or "Untitled"
            # ===== 优先级 1：最近使用的蓝图 =====
            last_bp = self._pm.get_last_opened_blueprint()
            if last_bp and os.path.isfile(last_bp):
                try:
                    g = BlueprintStore.load(last_bp)
                    if g is not None:
                        # JSON.name 是默认 Untitled 但文件名是中文/可读 → 回填
                        _, stem, _ = BlueprintStore._split_bp_path(last_bp)
                        if stem and (not g.name or g.name == "Untitled") and stem != "Untitled":
                            g.name = stem
                        self._current_path = last_bp
                        # ★ 必须先 bind（里面赋值 self._graph = g），再 mark_clean 并显式传 g！
                        #   否则在 bind 之前用 self._graph 还是旧对象，hash 算错→刚加载就dirty
                        self._bind_new_graph(g, zoom_fit=True)
                        self._mark_graph_clean(g)  # 刚加载的文件，视为干净状态（显式传g，不依赖 self._graph 此时赋值）
                        # 顺便把这张默认蓝图记为 last（下次就会走优先级 1 了）
                        try:
                            self._pm.set_last_opened_blueprint(last_bp)
                        except Exception:
                            pass
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
                        # 同样：把文件名回填到 JSON 默认的 Untitled
                        _, stem, _ = BlueprintStore._split_bp_path(default_bp)
                        if stem and (not g.name or g.name == "Untitled") and stem != "Untitled":
                            g.name = stem
                        self._current_path = default_bp
                        # ★ 同样：先 bind 再 mark_clean(g)
                        self._bind_new_graph(g, zoom_fit=True)
                        self._mark_graph_clean(g)  # 刚加载的文件，视为干净状态（显式传g）
                        # 顺便把这张默认蓝图记为 last（下次就会走优先级 1 了）
                        try:
                            self._pm.set_last_opened_blueprint(default_bp)
                        except Exception:
                            pass
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
        """重新弹工程选择框，成功则：先 confirm_discard 保存旧工程修改 → 再切工程 → 最后加载蓝图。

        ★ 严格时序（和「最近工程菜单项」保持一致），解决「串资源」的历史老问题：
          ① dlg.exec() → ProjectStartupDialog 只验证/创建工程（用 auto_open=False），**绝不提前切 PM**，
             只返回目标路径（result_project_root）。
          ② _confirm_discard() → 问用户要不要保存当前修改（此时 PM 还在旧工程，保存/set_last_opened
             都是写进旧工程的 meta，100% 正确）。
          ③ pm.open_project() → 真正把 ProjectManager 的 project_root/_meta 从旧工程切到新工程
             （这一步才真正卸载旧 provider + 注册新 provider + 发信号 + 写 recent）。
          ④ _load_or_new_graph_for_current_project() → 现在才读新工程的 meta/last_bp，不会串。
          ⑤ _finalize_project_switch() → 清全局缓存/停旧运行/刷新菜单/日志提示收尾。

        为什么不能先切工程再 confirm_discard？
          → _on_save() 里会 self._pm.set_last_opened_blueprint(self._current_path)，
            如果 PM 已经切到新工程，就会把旧工程的蓝图路径写到新工程的 meta.last_opened_blueprint 里，
            于是新工程一打开就会加载旧工程的图——这就是用户反馈的「game1 资源串到 game2」的根因。
        """
        dlg = ProjectStartupDialog(self)
        try:
            tw = dlg.findChild(QTabWidget)
            if tw is not None and 0 <= start_tab < tw.count():
                tw.setCurrentIndex(start_tab)
        except Exception:
            pass
        if dlg.exec() != QDialog.Accepted:
            return
        root = dlg.result_project_root
        if not root:
            return
        # —— 时序 1：先确认要不要保存旧工程修改（此时 PM 还在旧工程，保存不会写进新工程）
        if not self._confirm_discard():
            return
        # —— 时序 2：真正切工程（ProjectStartupDialog 没有切，现在才切）
        ok = self._pm.open_project(root)
        if not ok:
            QMessageBox.warning(self, "打开失败", f"不是合法的 Blueprint 工程目录：\n{root}")
            return
        # —— 时序 3：加载蓝图（此时 PM 已在新工程，last_bp 读取的是新工程自己的 meta）
        self._load_or_new_graph_for_current_project()
        # —— 时序 4：收尾
        self._finalize_project_switch()

    # ==================== UI 构建 ====================
    def _build_central(self):
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ========== 4面板QDockWidget布局（Qt增强可停靠可浮动） ==========
        # 中央只有【画布】NodeView（中央画布）
        self._scene = NodeScene(self)
        # ========== 撤销栈信号监听 ==========
        self._scene.node_created.connect(self._on_scene_node_created)
        self._scene.node_will_remove.connect(self._on_scene_node_will_remove)
        self._scene.edge_created.connect(self._on_scene_edge_created)
        self._scene.edge_will_remove.connect(self._on_scene_edge_will_remove)
        self._scene.nodes_dragged.connect(self._on_scene_nodes_dragged)

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

        # 左：📚节点库（固定最小宽度220，不可调节宽度）
        self._palette = NodePalette()
        dock_left = QDockWidget("📚 节点库", self)
        dock_left.setObjectName("dockPalette")
        dock_left.setWidget(self._palette)
        dock_left.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        # 只允许移动停靠位置，禁止关闭&禁止单独浮窗（防止用户误关后显示不出来）
        dock_left.setFeatures(QDockWidget.DockWidgetMovable)
        dock_left.setMinimumWidth(220)
        dock_left.setMaximumWidth(220)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock_left)
        dock_left.setStyleSheet(_dock_stylesheet)
        dock_left.show()
        self._dock_palette = dock_left

        # 右：🎛 属性面板（固定最大宽度460，不可调节宽度）
        self._inspector = InspectorPanel()
        dock_right = QDockWidget("🎛 属性面板", self)
        dock_right.setObjectName("dockInspector")
        dock_right.setWidget(self._inspector)
        dock_right.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        # 只允许移动停靠位置，禁止关闭&禁止单独浮窗
        dock_right.setFeatures(QDockWidget.DockWidgetMovable)
        dock_right.setMinimumWidth(460)
        dock_right.setMaximumWidth(460)
        self.addDockWidget(Qt.RightDockWidgetArea, dock_right)
        dock_right.setStyleSheet(_dock_stylesheet)
        dock_right.show()
        self._dock_inspector = dock_right

        # 底：📜执行日志（最小80高初始160）
        self._log = LogPanel()
        dock_bottom = QDockWidget("📜 执行日志", self)
        dock_bottom.setObjectName("dockLog")
        dock_bottom.setWidget(self._log)
        dock_bottom.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        # 只允许移动停靠位置，禁止关闭&禁止单独浮窗
        dock_bottom.setFeatures(QDockWidget.DockWidgetMovable)
        dock_bottom.setMinimumHeight(80)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock_bottom)
        try:
            self.resizeDocks([dock_bottom], [180], Qt.Vertical)
        except Exception:
            pass
        dock_bottom.setStyleSheet(_dock_stylesheet)
        dock_bottom.show()
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
        # 🗂  加载蓝图：直接作为顶层子菜单挂在工程▾下，与「新建/保存/另存/删除」同级（类似最近工程的下拉结构）
        # 第 1 项永远是『从文件浏览打开…』（保留原入口），后续是当前工程所有 .bp.json 按修改时间倒序，方便快速切换
        self._menu_load_blueprint = QMenu("加载蓝图", self)
        self._menu_load_blueprint.setToolTipsVisible(True)
        # 【关键】aboutToShow：每次展开子菜单时重新扫描工程目录，保证蓝图列表/修改时间 100% 实时
        # （不是启动时扫一次，避免外部创建蓝图、修改文件时间后列表不刷新）
        self._menu_load_blueprint.aboutToShow.connect(self._refresh_load_blueprint_menu)
        self._menu_proj_saveas.addMenu(self._menu_load_blueprint)
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
        self._act_redo = mk_act("#8B949E", "重做", self._on_redo)
        self._act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self._act_copy = mk_act("#8B949E", "复制", self._on_copy_nodes)
        self._act_copy.setShortcut(QKeySequence("Ctrl+C"))
        self._act_paste = mk_act("#8B949E", "粘贴", self._on_paste_nodes)
        self._act_paste.setShortcut(QKeySequence("Ctrl+V"))
        # 加到主窗口 action 列表（快捷键才会生效），但不 add_toolbtn → 顶栏上看不到按钮
        self.addAction(self._act_undo)
        self.addAction(self._act_redo)
        self.addAction(self._act_copy)
        self.addAction(self._act_paste)
        # Ctrl+S 全局保存：保留（这是用户专门指定要保留的 4 个标准快捷键之一）
        self._act_save_global = mk_act("#0969DA", "保存", self._on_save)
        self._act_save_global.setShortcut(QKeySequence("Ctrl+S"))
        self.addAction(self._act_save_global)

        # --------------- 运行控制组（暂停 → 运行 → 停止 → 下一步） ---------------
        self._act_pause = mk_act("#9A6700", "⏸ 暂停", self._on_toggle_pause)
        self._act_pause.setEnabled(False)
        self._act_pause.setToolTip("运行中可点，临时冻结所有步骤；再点一次继续（不会丢状态）")
        self._act_run = mk_act("#238636", "▶ 运行", self._on_run_clicked)
        self._act_stop = mk_act("#DA3633", "■ 停止", self._on_stop_clicked)
        # ---------------- ⏭ 下一步（Step Over 单步调试） ----------------
        self._act_step = mk_act("#1F6FEB", "⏭ 下一步", self._on_step_clicked)
        self._act_step.setToolTip("仅暂停状态下可用（命中断点或手动⏸暂停后）。步过下一个节点并自动暂停（类似IDE F10 Step Over）。无断点想一步步跑→先⏸暂停再点此按钮。")
        # 【2026-07-03 用户明确：运行/停止/暂停的全局热键用 GlobalHotkeyManager(pynput) 跨窗口注册，
        #  QAction 层面不再设置局部 shortcut（局部只有主窗口激活才生效，游戏前台按不到，违背全局热键的初衷）】
        #  L519-L520 旧的 F5/F8 局部硬编码已删除。
        self._act_run.setShortcut(QKeySequence())
        self._act_stop.setShortcut(QKeySequence())
        self._act_step.setShortcut(QKeySequence())
        self._act_stop.setEnabled(False)
        self._act_step.setEnabled(False)
        # 暂停按钮单独保存引用：会动态切"⏸暂停/▶继续"和颜色
        self._btn_pause = add_toolbtn(self._act_pause, bg="#5B4200", color="#F0B429", hover_bg="#875F00", hover_border="#9A6700")
        add_toolbtn(self._act_run, bg="#0F3321", color="#3FB950", hover_bg="#194B31", hover_border="#238636")
        add_toolbtn(self._act_stop, bg="#3B1314", color="#F85149", hover_bg="#58151C", hover_border="#DA3633")
        # 【用户要求位置：停止按钮和缩放适应之间插入 ⏭ 下一步】所以放 ■停止之后、tb.addSeparator() 之前
        self._btn_step = add_toolbtn(self._act_step, bg="#0D2957", color="#58A6FF", hover_bg="#1158C8", hover_border="#1F6FEB")
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

        # --------------- 右侧: LLM 入口按钮 + 目标程序 全局选择器 ---------------
        tb.addSeparator()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # ---- LLM 入口按钮（在🎯 目标程序 左边） ----
        btn_llm = QPushButton("🤖 LLM 设置")
        btn_llm.setCursor(Qt.PointingHandCursor)
        btn_llm.setStyleSheet(
            "QPushButton { background:#1F6FEB; color:#FFFFFF; border:1px solid #1158C8; border-radius:4px; padding:3px 12px; font-weight:600; }"
            "QPushButton:hover { background:#1158C8; border-color:#58A6FF; }"
            "QPushButton:pressed { background:#0F50B5; }"
        )
        btn_llm.setToolTip("打开 LLM 智能助手设置（启用后桌面右下角将出现悬浮球）")
        btn_llm.clicked.connect(self._on_llm_settings_clicked)
        tb.addWidget(btn_llm)
        # 视觉分隔：和目标程序下拉之间留点空间
        sep2 = QLabel("  ·  ")
        sep2.setStyleSheet("color:#30363D;")
        tb.addWidget(sep2)

        target_hint = QLabel("🎯 目标程序:")
        target_hint.setStyleSheet("color:#C9D1D9; padding: 0 4px 0 8px;")
        tb.addWidget(target_hint)

        self._target_combo = NoWheelComboBox()
        self._target_combo.setMinimumWidth(280)
        self._target_combo.setMaximumWidth(280)
        self._target_combo.setStyleSheet("QComboBox { background:#161B22; color:#C9D1D9; padding:3px 8px; border:1px solid #30363D; border-radius:4px; } QComboBox:hover { border-color:#484F58; } QComboBox::drop-down { border:none; width:20px; } QComboBox QAbstractItemView { min-width: 480px; }")
        self._target_combo.currentIndexChanged.connect(self._on_target_window_changed)
        tb.addWidget(self._target_combo)

        btn_refresh = QPushButton("↻ 刷新")
        btn_refresh.setStyleSheet("QPushButton { background:#21262D; color:#C9D1D9; border:1px solid #30363D; border-radius:4px; padding:3px 10px; } QPushButton:hover { background:#30363D; }")
        btn_refresh.clicked.connect(self._reload_target_windows)
        tb.addWidget(btn_refresh)

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
                    meta_path = os.path.join(path, "project_meta.json")
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
            # 收尾：清全局缓存、停旧运行、刷新菜单+日志提示
            self._finalize_project_switch()
        except Exception as e:
            QMessageBox.critical(self, "打开失败", f"打开最近工程时出错：\n{e}")

    # ---------------- 工程 ▾：加载蓝图子菜单（所有工程蓝图按修改时间倒序） ----------------
    def _list_project_blueprints(self, proj_root: str):
        """【扫描辅助】递归工程根目录下所有真正的蓝图文件（.bp.json），并过滤掉备份/临时文件。
        返回 list[(abs_path, rel_to_project, mtime_secs)]，已排除备份。"""
        if not proj_root or not os.path.isdir(proj_root):
            return []
        result = []
        for dirpath, dirnames, filenames in os.walk(proj_root):
            # 跳过隐藏目录（.bp、.git 等），避免把工程元数据当蓝图列出来
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                # 必须是蓝图扩展名
                if not fn.lower().endswith(BlueprintStore.EXT.lower()):
                    continue
                # 过滤备份：name.bp.backup_xxx.json → stem 里含 .backup_ → 跳过
                # BlueprintStore.save 生成的备份形如：<stem>.bp.backup_YYYYMMDD_HHMMSS.json
                if ".backup_" in fn:
                    continue
                # 过滤临时文件（~xxx.bp.json、xxx.bp.json.tmp 这种）
                if fn.startswith("~") or fn.lower().endswith(".tmp"):
                    continue
                abs_path = os.path.join(dirpath, fn)
                try:
                    st = os.stat(abs_path)
                    mtime = int(st.st_mtime)
                except OSError:
                    continue
                # 相对于工程根目录显示，方便知道蓝图在哪个子目录
                rel = os.path.relpath(abs_path, proj_root)
                # Windows 上 os.walk 返回反斜杠，统一成正斜杠（显示更清爽）
                rel = rel.replace("\\", "/")
                result.append((abs_path, rel, mtime))
        # 按 mtime 倒序（最近修改的排最前面）
        result.sort(key=lambda t: t[2], reverse=True)
        return result

    def _refresh_load_blueprint_menu(self):
        """『加载蓝图』子菜单 aboutToShow 时调用：
        每次展开都重新扫描一次，100% 实时（外部创建/删除/改了修改时间都会变）。
        结构：
          第1项. 📁 从文件浏览打开…（永远保留，用户要求）
          -------
          没工程：    （请先打开工程，才能列出工程下所有蓝图）
          有工程无蓝图：（工程目录下暂无蓝图，点上方「新建蓝图」创建）
          有工程有蓝图：按修改时间新→旧，当前已打开的蓝图前加 📌 视觉标记"""
        try:
            menu = getattr(self, "_menu_load_blueprint", None)
            if menu is None:
                return
            menu.clear()
            # ============== 第 1 项（永远在）：从文件浏览打开… ==============
            act_browse = QAction("📁 从文件浏览打开…", menu)
            act_browse.setToolTip("使用系统文件浏览器选择任意位置的蓝图文件（原入口）")
            # 点这个直接走原 _on_open，会弹 QFileDialog
            act_browse.triggered.connect(lambda _checked=False: self._on_open())
            menu.addAction(act_browse)
            # ============== 分隔符 ==============
            menu.addSeparator()
            # ============== 后续：工程下所有蓝图 ==============
            pm = self._pm
            proj_root = pm.project_root if pm and pm.is_open() else None
            if not proj_root:
                # 没工程 → 给禁用的提示项（用户选「从文件浏览打开」仍可跨工程打开任意蓝图）
                act_hint = QAction("（请先打开工程，才能列出工程下所有蓝图）", menu)
                act_hint.setEnabled(False)
                menu.addAction(act_hint)
                return
            all_bps = self._list_project_blueprints(proj_root)
            if not all_bps:
                act_empty = QAction("（工程目录下暂无蓝图，点「新建蓝图」创建）", menu)
                act_empty.setEnabled(False)
                menu.addAction(act_empty)
                return
            # 当前正在打开的蓝图路径 → 如果在列表里，加一个 📌 标记（用户一眼识别"当前在哪"）
            current_path_norm = None
            if self._current_path:
                try:
                    current_path_norm = os.path.normcase(os.path.abspath(self._current_path))
                except Exception:
                    current_path_norm = None
            # 格式化时间：YYYY-MM-DD HH:MM（本地时区，用户看着习惯）
            from datetime import datetime
            shown = 0
            for (abs_path, rel_display, mtime_secs) in all_bps:
                try:
                    dt_str = datetime.fromtimestamp(mtime_secs).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    dt_str = ""
                # 显示：(📌) 相对路径（子目录/蓝图A.bp.json） + 右侧小字是修改时间
                # （PySide6 QAction 不支持富文本/两列，所以统一拼到 text 里，用 8 个空格隔开时间列，视觉上有间隔感）
                time_suffix = f"        [{dt_str}]" if dt_str else ""
                is_current = False
                if current_path_norm:
                    try:
                        is_current = current_path_norm == os.path.normcase(os.path.abspath(abs_path))
                    except Exception:
                        is_current = False
                prefix = "📌 " if is_current else "     "
                # &1. 这种数字前缀给用户可以按 1/2/3 快速选择（和最近工程一样）
                idx_prefix = f"&{shown+1}. " if shown < 9 else "    "
                text = f"{idx_prefix}{prefix}{rel_display}{time_suffix}"
                act = QAction(text, menu)
                act.setToolTip(f"完整路径: {abs_path}\n最近修改: {dt_str}")
                act.setData(abs_path)
                # 点菜单项 → 先 confirm_discard → 是 → 直接加载（用抽出来的公共函数）
                act.triggered.connect(lambda _checked=False, p=abs_path: self._on_open_blueprint_from_menu(p))
                menu.addAction(act)
                shown += 1
        except Exception as e:
            try:
                self._log.append_log(f"[Blueprint] 加载蓝图菜单刷新失败：{e}", "WARN")
            except Exception:
                pass

    def _on_open_blueprint_from_menu(self, path: str):
        """点加载蓝图子菜单里的具体蓝图项 → 切换过去。"""
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "打开失败", f"蓝图文件已不存在（可能被外部删除）：\n{path}")
            return
        # 先问是否丢弃当前未保存的修改（和 _on_open / _on_new 保持一致）
        if not self._confirm_discard():
            return
        self._open_blueprint_path(path)

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
        self._inspector.request_snip_for_node.connect(self._on_snip_for_node)
        self._inspector.request_pick_coord.connect(self._on_pick_coord)
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
            self._inspector.set_node(None)  # 关键！避免属性面板显示之前蓝图的节点参数
        except Exception:
            pass
        # 2) 绑定到 scene（会清空旧节点/边，重新渲染）
        self._scene.set_graph(graph)
        self._graph = graph
        # 蓝图切换时：清空撤销/重做栈（跨蓝图撤销没有意义）
        self._undo_stack.clear()
        self._redo_stack.clear()
        # 3) 按要求缩放视图
        if zoom_fit and self._view is not None:
            try:
                self._view.zoom_fit()
            except Exception:
                pass
        # 4) 蓝图加载后刷新 🔴 断点计数（第一次打开蓝图时 NodeView 构造还没 graph，所以这里必须手动触发一次）
        if self._view is not None:
            try:
                self._view._breakpoint_idx = -1  # 重置跳转游标到最开始
                self._view._refresh_breakpoint_label()
            except Exception:
                pass

    def _new_graph(self):
        g = BlueprintStore.new_graph("Untitled")
        self._current_path = None
        self._last_saved_graph_hash = None  # 新建=从未保存过，hash置空
        self._bind_new_graph(g, zoom_fit=False)  # 新空图不用缩放
        self._log.append_log("新建空白蓝图（未保存）。", "SUCCESS")
        self._update_title()

    def _on_new(self):
        """『工程 ▾ → 新建蓝图』回调：
        先弹对话框让用户输入蓝图名称（默认建议名自动防重），点确定才在工程目录下创建 .bp.json；
        点取消就什么都不做（也不会生成任何文件）。"""
        # 1) 先确认要不要保存当前未保存的修改（任何情况下都要问）
        if not self._confirm_discard():
            return
        # 2) 没有工程 → 退回旧行为：内存空画布（没地方落盘）
        if not self._ensure_project_open("新建蓝图"):
            self._new_graph()
            return
        # 3) 有工程 → 先计算不重名的默认建议名："新蓝图 / 新蓝图 1 / 新蓝图 2..."
        proj_root = self._pm.project_root
        base_suggest = "新蓝图"
        i = 0
        while True:
            suggest_stem = base_suggest if i == 0 else f"{base_suggest} {i}"
            candidate = os.path.join(proj_root, suggest_stem + BlueprintStore.EXT)
            if not os.path.exists(candidate):
                default_stem = suggest_stem
                break
            i += 1
        # 4) 循环问用户蓝图名，直到：用户输入合法且不冲突（或点取消→直接放弃）
        chosen_stem: Optional[str] = None
        while True:
            user_stem, ok = QInputDialog.getText(
                self,
                "新建蓝图",                         # 标题
                "请输入新蓝图的名称（无需写 .bp.json 后缀）：",  # 提示
                text=default_stem,                 # 默认值=自动防重的建议名
            )
            if not ok:
                # 用户点取消：什么都不创建，直接返回
                return
            # 4.1 清洗用户输入：去掉首尾空白
            user_stem = (user_stem or "").strip()
            if not user_stem:
                QMessageBox.warning(self, "名称不能为空", "蓝图名称不能为空，请重新输入。")
                continue
            # 4.2 去掉用户不小心输入的 .bp.json 后缀（避免生成 "xxx.bp.json.bp.json"）
            if user_stem.lower().endswith(BlueprintStore.EXT.lower()):
                user_stem = user_stem[: -len(BlueprintStore.EXT)]
            # 4.3 Windows 文件系统非法字符替换成下划线（防止创建失败）
            invalid = '<>:"/\\|?*'
            safe_stem = "".join("_" if ch in invalid else ch for ch in user_stem).strip()
            if not safe_stem:
                QMessageBox.warning(self, "名称非法", "请输入不含特殊字符的蓝图名称。")
                continue
            # 4.4 重名检查：如果已有同名 .bp.json，问用户是否覆盖
            final_path = os.path.join(proj_root, safe_stem + BlueprintStore.EXT)
            if os.path.exists(final_path):
                r = QMessageBox.question(
                    self,
                    "蓝图名称已存在",
                    f"工程目录下已经存在同名蓝图文件：\n{final_path}\n\n是否覆盖？（覆盖后原文件会进备份，不会永久丢失。）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    # 不覆盖：回到输入循环（下一次默认值仍取用户这次输入的 safe_stem，用户能继续改）
                    default_stem = safe_stem
                    continue
                # 用户明确要覆盖 → 继续往下走，BlueprintStore.save 会自动做 .backup
            chosen_stem = safe_stem
            break
        if chosen_stem is None:
            return
        # 5) 用户选好名称了 → 创建 graph 对象，同步 graph.name = chosen_stem（中文写进 JSON.name，下次加载标题栏正确）
        g = BlueprintStore.new_graph(chosen_stem)
        g.name = chosen_stem
        ok = BlueprintStore.save(g, final_path)  # 重名时用户已选 Yes，make_backup 默认 True
        if not ok:
            QMessageBox.warning(
                self, "创建蓝图失败",
                f"无法在工程目录创建新蓝图文件，请检查目录权限：\n{final_path}"
            )
            # 即使落盘失败，也把 graph 绑到画布让用户不至于白等
            self._current_path = None
            self._last_saved_graph_hash = None
            self._bind_new_graph(g, zoom_fit=False)
            self._update_title()
            return
        # 6) 落盘成功 → 先 bind 再 mark_clean(g)，保证不显示未保存星号
        self._current_path = final_path
        self._bind_new_graph(g, zoom_fit=False)
        self._mark_graph_clean(g)
        try:
            self._pm.set_last_opened_blueprint(final_path)
        except Exception:
            pass
        self._log.append_log(f"✅ 已创建新蓝图：{final_path}", "SUCCESS")
        self._update_title()

    def _on_open(self):
        """原『加载蓝图』入口：弹文件浏览器选文件。"""
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
            "蓝图文件 (*.bp.json)",  # 收紧：只允许 .bp.json 蓝图文件，不让 project_meta/config 这种混进来
        )
        if not path:
            return
        self._open_blueprint_path(path)

    def _open_blueprint_path(self, path: str):
        """【公共入口】从一个绝对路径加载蓝图（文件对话框选的 / 菜单项点的，都走这里）。
        已做：confirm_discard 已在调用方（_on_open / 菜单项）处理，这里不重复问。"""
        g = BlueprintStore.load(path)
        if g is None:
            QMessageBox.warning(self, "打开失败", "无法读取蓝图文件，格式可能已损坏。")
            return
        # 如果 JSON.name 还是默认 Untitled，但文件名是中文/非 Untitled 的可读名称
        # → 用文件名 stem 覆盖 graph.name，让标题栏显示中文而不是 "Untitled"
        _, stem, _ = BlueprintStore._split_bp_path(path)
        if stem and (not g.name or g.name == "Untitled") and stem != "Untitled":
            g.name = stem
        self._current_path = path
        # ★ 必须先 bind（里面才给 self._graph=g 赋值），再 mark_clean(g) 传新对象；
        #   否则 bind 之前 self._graph 还是旧的，hash 算错→刚加载就dirty
        self._bind_new_graph(g, zoom_fit=True)  # 统一绑定 + 清空Inspector残留 + 缩放
        self._mark_graph_clean(g)  # 刚加载，干净状态（显式传 g = 新对象）
        try:
            self._pm.set_last_opened_blueprint(path)
        except Exception:
            pass
        self._log.append_log(f"已加载蓝图: {path}", "SUCCESS")
        self._update_title()
        # 工程下的蓝图列表可能因为"最近修改时间"重排，下次打开工程下拉时自动刷新（aboutToShow 已经连了刷新）

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
        # ----------------------------------------------------------
        # ★ 关键：把用户选的文件名（去掉 .bp.json 后缀）同步为蓝图名称
        #   否则另存为中文文件名后，graph.name 还是默认的 Untitled，
        #   保存进 JSON 的 "name" 字段仍是 Untitled → 下次重新加载
        #   后标题栏就显示 Untitled，用户就感觉『加载中文蓝图名没生效』
        # ----------------------------------------------------------
        _, stem, _ = BlueprintStore._split_bp_path(path)
        if stem and stem != getattr(self._graph, "name", None):
            self._graph.name = stem
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

    def _mark_graph_clean(self, graph=None):
        """把当前图标记为『干净状态』（未改动）——在加载成功/保存成功后调用。

        参数 graph：
          - 若显式传入（推荐在"绑定新 graph 之前"直接用新加载的 g），
            直接用传入对象算 hash，**不依赖 `self._graph` 此时的赋值状态**。
          - 若不传（保存场景：保存的就是当前 self._graph），用 self._graph。

        为什么要支持显式传参？
          因为 `_bind_new_graph(g)` 里才做 `self._graph = g`（第 777 行），
          如果在 bind 之前就调 `_mark_graph_clean()` 而不传参，算 hash 用的是
          旧的 self._graph（或 None），就会出现「刚加载完永远是未保存状态」的 bug。
        """
        target = graph if graph is not None else self._graph
        self._last_saved_graph_hash = self._hash_graph(target)

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
        # ================================================================
        # 变更入栈：Inspector 参数修改 → Ctrl+Z / Ctrl+Y 可撤销重做
        # ================================================================
        # 值相等的情况不做任何事（避免 textChanged 高频刷栈或递归入栈）
        # 这里用「字符串 repr 比较」做浅比较，兼容 dict/list 这类 Python 可变对象
        applying_undo = bool(getattr(self, "_applying_undo_redo", False))
        cur = self._graph.nodes[node_id].params.get(param_name, None)
        def _param_eq(a, b):
            try:
                return a == b
            except Exception:
                return repr(a) == repr(b)
        same = _param_eq(cur, value)
        if not applying_undo and not same:
            import time
            ts = time.time()
            MERGE_WINDOW_SEC = 2.0
            # 先深拷贝一份快照，避免用户继续编辑同一个 list/dict 时把 undo/redo 里的值给改掉
            import copy as _copy
            try:
                old_snap = _copy.deepcopy(cur)
                new_snap = _copy.deepcopy(value)
            except Exception:
                old_snap = cur
                new_snap = value
            # 合并窗口内：同一 (node_id, param_name) 的连续编辑 → 只保留一条（合并 new_value）
            merged = False
            if self._undo_stack:
                top = self._undo_stack[-1]
                if isinstance(top, dict) and top.get("kind") == "change_param" and \
                   top.get("node_id") == node_id and top.get("param_name") == param_name and \
                   (ts - float(top.get("ts", 0.0))) < MERGE_WINDOW_SEC:
                    top["new_value"] = new_snap
                    top["ts"] = ts
                    merged = True
            if not merged:
                self._undo_stack.append({
                    "kind": "change_param",
                    "node_id": node_id,
                    "param_name": param_name,
                    "old_value": old_snap,
                    "new_value": new_snap,
                    "ts": ts,
                })
                # 有新的入栈 → 清空 redo 栈（标准编辑器行为：新操作会截断 redo 历史）
                try:
                    self._redo_stack.clear()
                except Exception:
                    pass
        # ================================================================
        # 实际应用修改
        # ================================================================
        node = self._graph.nodes[node_id]
        node.params[param_name] = value
        ni = self._scene.node_item(node_id)
        if ni is not None:
            # 动态端口节点（仅 Sequence，它端口数会变）：关键参数变化时，重建端口集
            # 注意：VarSwitch 现在 has_dynamic_ports=False，yes/no 端口固定，不能进这里！
            #       否则每次打字 set_node 会把正在发 textChanged 信号的 QPlainTextEdit 删掉，
            #       导致 0xC0000005 ACCESS_VIOLATION 闪退。
            try:
                if node.type in ("Sequence",) and param_name in (
                    "num_paths"
                ):
                    from src.blueprint.core.node_library import _resolve_output_ports_default
                    p = dict(node.params or {})
                    new_out = _resolve_output_ports_default(node.type, p)
                    cur_out_names = [pp.name for pp in node.outputs]
                    new_out_names = [pp.name for pp in new_out]
                    if cur_out_names != new_out_names:
                        # Sequence：端口增减前，先删除「被移除端口」上的关联边
                        # 注意：Edge 的字段名是 from_node/from_port/to_node/to_port，不要写成 target_* 或 source_*
                        removed_out_set = set(cur_out_names) - set(new_out_names)
                        removed_in_set = set()
                        # 目前只有输出端口会变（输入端口保持不变），输入兼容判断只是兜底
                        new_in_names = [pp.name for pp in _resolve_output_ports_default
                                        and []]  # placeholder，实际输入没变化
                        if removed_out_set or removed_in_set:
                            edges_to_remove = []
                            for edge in list(self._graph.edges.values()):
                                if (edge.to_node == node_id and edge.to_port in removed_in_set) or \
                                   (edge.from_node == node_id and edge.from_port in removed_out_set):
                                    edges_to_remove.append(edge.id)
                            for eid in edges_to_remove:
                                if self._scene is not None:
                                    self._scene.remove_edge(eid, _emit_will=False)
                        node.outputs = list(new_out)
                        # 重建 PortItem（会全删旧的、新建同名的）
                        ni.sync_from_node()
                        # 重建后：所有关联的 EdgeItem 的 _from_port/_to_port 还指向旧的已删除 PortItem
                        # 必须按端口名重新挂到新 PortItem，并刷新路径，否则看起来就像"保留连线反而断了找不到端口"
                        if self._scene is not None:
                            self._scene.reconnect_node_edges(node_id)
                        # 端口变了 → 延迟刷新 inspector（必须退出当前信号栈再重建 UI，防止 sender 控件被中途删除闪退）
                        if (
                            getattr(self, "_inspector", None) is not None
                            and self._inspector._current_node is not None
                            and self._inspector._current_node.id == node.id
                        ):
                            nid_refresh = node.id
                            def _deferred_setnode(nid=nid_refresh):
                                try:
                                    if self._graph and nid in self._graph.nodes and \
                                       self._inspector._current_node and \
                                       self._inspector._current_node.id == nid:
                                        self._inspector.set_node(self._graph.nodes[nid])
                                except Exception as e:
                                    print(f"[WARN] _deferred_setnode 异常: {e}")
                            QTimer.singleShot(0, _deferred_setnode)
            except Exception as e:
                print(f"[WARN] _on_param_changed 端口刷新异常: {e}")
                import traceback; traceback.print_exc()
            # 任何参数变化 → 强制节点卡重绘（刷新标题三段式显示 + 摘要文本）
            ni.update()
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

    # ---------- IfImageDetect / StillnessDetect 全屏 SnipOverlay 处理 ----------
    def _on_snip_for_node(self, node_id: str, mode: str):
        """处理 Inspector 发来的截图请求：
        mode ∈ {
            "if_img_add",           # IfImageDetect 追加一张
            "if_img_replace_<idx>", # IfImageDetect 替换第 idx 张
            "if_static_roi",        # StillnessDetect 框选 ROI
            "findimg_target",       # FindImage 截取目标（保存PNG到templates）
        }
        """
        if not self._graph or node_id not in self._graph.nodes:
            return
        # findimg_target / if_img_add / if_img_replace 都需要 templates 目录保存文件，
        # 工程未打开时禁止操作（避免 fallback 到全局 APP_DIR 污染 src/）
        if not self._ensure_project_open("裁剪模板保存到工程templates/"):
            return
        try:
            from src.blueprint.ui.snip_overlay import SnipOverlay
        except Exception as e:
            self._log.append_log(f"SnipOverlay 加载失败: {e}（降级为 region_selector）", "WARN")
            return

        def _on_result(result, nid=node_id, m=mode):
            try:
                # 恢复主窗口
                try:
                    self.show()
                    self.raise_()
                    self.activateWindow()
                except Exception:
                    pass
                if result is None:
                    self._log.append_log("[Snip] 用户取消截图", "INFO")
                    return
                bbox = result.get("bbox_screen")
                pil = result.get("pil_img")
                if not bbox or pil is None:
                    self._log.append_log("[Snip] 截图结果为空", "WARN")
                    return
                x1, y1, x2, y2 = bbox
                w = max(0, x2 - x1)
                h = max(0, y2 - y1)
                if w < 5 or h < 5:
                    self._log.append_log("[Snip] 选区太小 (宽<5 或 高<5)，忽略", "WARN")
                    return
                node = self._graph.nodes.get(nid)
                if node is None:
                    return
                # --- 转 b64（先压缩成缩略图，避免b64过大导致UI卡顿）---
                try:
                    import base64, io
                    # 原图保存用于 FindImage 模板实际匹配；预览 b64 用缩略图
                    preview_pil = pil.copy()
                    try:
                        preview_pil.thumbnail((220, 220))
                    except Exception:
                        pass
                    buf = io.BytesIO()
                    preview_pil.save(buf, format="PNG")
                    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception as e:
                    self._log.append_log(f"[Snip] b64编码失败: {e}", "ERROR")
                    return
                # --- 按 mode 回填参数 ---
                if m == "if_static_roi":
                    self._on_param_changed(nid, "roi", json.dumps([int(x1), int(y1), int(w), int(h)]))
                    self._on_param_changed(nid, "roi_preview_b64", b64)
                    self._log.append_log(
                        f"[if_static] ROI框选完成: ({x1},{y1}) {w}×{h}", "SUCCESS"
                    )
                    self._inspector.set_node(node)
                    return
                # FindImage：findimg_target - 保存PNG到templates，回填template路径 + b64预览
                if m == "findimg_target":
                    try:
                        import uuid
                        # templates 目录：工程根/templates （不存在则创建）
                        from src.config.app_paths import template_dir
                        tpl_dir = template_dir()
                        os.makedirs(tpl_dir, exist_ok=True)
                        fname = f"findimg_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
                        fpath = os.path.join(tpl_dir, fname)
                        pil.save(fpath, format="PNG")
                        # 同时写入PIL runtime_attrs缓存
                        try:
                            if not hasattr(node, "runtime_attrs") or node.runtime_attrs is None:
                                node.runtime_attrs = {}
                            node.runtime_attrs["_pil_template"] = pil.copy()
                        except Exception:
                            pass
                        self._on_param_changed(nid, "template", fpath)
                        self._on_param_changed(nid, "template_b64", b64)
                        self._log.append_log(
                            f"[FindImage] 截取目标完成: {w}×{h} 已保存到 {fpath}", "SUCCESS"
                        )
                        self._inspector.set_node(node)
                    except Exception as e_fe:
                        self._log.append_log(f"[FindImage] 保存模板失败: {e_fe}", "ERROR")
                        traceback.print_exc()
                    return
                # FindImage：findimg_target 已在上面处理；不再支持 findimg_region（搜索区域功能已移除）
                # IfImageDetect：if_img_add / if_img_replace_<N>
                import uuid
                raw = node.params.get("images_json", "[]")
                try:
                    arr = json.loads(raw) if isinstance(raw, str) and raw else (list(raw) if isinstance(raw, list) else [])
                except Exception:
                    arr = []
                if not isinstance(arr, list):
                    arr = []
                new_item = {
                    "id": uuid.uuid4().hex[:8],
                    "b64": b64,
                    "w": int(w),
                    "h": int(h),
                }
                if m.startswith("if_img_replace_"):
                    try:
                        idx = int(m.split("_")[-1])
                    except Exception:
                        idx = -1
                    if 0 <= idx < len(arr):
                        old_id = ""
                        try:
                            old_id = str(arr[idx].get("id", "") or "")
                        except Exception:
                            pass
                        new_item["id"] = old_id or new_item["id"]
                        arr[idx] = new_item
                        self._log.append_log(f"[if_img] 替换第 {idx+1} 张参考图: {w}×{h}", "SUCCESS")
                    else:
                        arr.append(new_item)
                        self._log.append_log(f"[if_img] 追加新参考图: {w}×{h} (idx越界，改追加)", "WARN")
                else:  # if_img_add
                    arr.append(new_item)
                    self._log.append_log(f"[if_img] 追加第 {len(arr)} 张参考图: {w}×{h}", "SUCCESS")
                s = json.dumps(arr, ensure_ascii=False, sort_keys=True)
                self._on_param_changed(nid, "images_json", s)
                self._inspector.set_node(node)
            except Exception as e2:
                self._log.append_log(f"[Snip] 结果处理失败: {e2}", "ERROR")
                traceback.print_exc()

        try:
            overlay = SnipOverlay.prepare_and_run(self, delay_ms=400)
            overlay.finished.connect(_on_result)
            self._log.append_log("[Snip] 请在屏幕上框选矩形（ESC取消）...", "INFO")
        except Exception as e:
            self._log.append_log(f"[Snip] 启动失败: {e}", "ERROR")
            traceback.print_exc()

    # ---------- Mouse 点属性 📍 坐标拾取 ----------
    def _on_pick_coord(self, node_id: str, param_name: str):
        """启动 CoordOverlay 全屏遮罩拾取坐标，根据节点 coord_mode 决定保存相对/绝对坐标。"""
        if not self._graph or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]

        # FindImage 的 offset 参数：走视觉偏移拾取（以模板中心为基准），不走 CoordOverlay
        if node.type == "FindImage" and param_name == "offset":
            self._start_findimg_offset_pick(node_id)
            return

        coord_mode = str(node.params.get("coord_mode", "relative") or "relative")

        # 尝试获取当前绑定窗口 hwnd + rect（用于相对坐标模式）
        hwnd = self.get_active_hwnd()
        window_rect = None
        exe_name = ""
        if hwnd:
            try:
                import win32gui
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                window_rect = (int(l), int(t), int(r - l), int(b - t))
                try:
                    from src.tools.window_utils import get_exe_name_by_hwnd
                    exe_name = get_exe_name_by_hwnd(hwnd) or ""
                except Exception:
                    pass
            except Exception:
                hwnd = None

        # 若 coord_mode=absolute，强制不传 hwnd（让 overlay 走绝对模式）
        if coord_mode == "absolute":
            hwnd = None
            window_rect = None

        try:
            from src.blueprint.ui.coord_overlay import CoordOverlay
        except Exception as e:
            self._log.append_log(f"CoordOverlay 加载失败: {e}", "ERROR")
            return

        self._log.append_log(
            f"[📍] 坐标拾取启动（{ '相对绑定窗口' if hwnd else '绝对屏幕坐标'}模式），请在屏幕上点击目标位置...",
            "INFO",
        )

        try:
            overlay = CoordOverlay.prepare_and_run(
                self,
                hwnd=hwnd,
                window_rect=window_rect,
                exe_name=exe_name,
                delay_ms=300,
            )
            overlay.finished.connect(
                lambda result, nid=node_id, pn=param_name, cm=coord_mode: self._on_coord_picked(nid, pn, cm, result)
            )
        except Exception as e:
            self._log.append_log(f"[📍] CoordOverlay 启动失败: {e}", "ERROR")
            traceback.print_exc()

    def _on_coord_picked(self, node_id: str, param_name: str, coord_mode: str, result):
        """CoordOverlay 拾取完成回调：根据 coord_mode 保存 rel 或 abs 坐标。"""
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        if result is None:
            self._log.append_log("[📍] 坐标拾取已取消", "INFO")
            return
        # result 包含 abs 和 rel 两个坐标
        # coord_mode=relative 且有绑定窗口 → 保存 rel；否则保存 abs
        if coord_mode == "relative" and result.get("mode") == "bound":
            x, y = result.get("rel", (0, 0))
            self._log.append_log(
                f"[📍] 拾取完成：相对坐标 ({x}, {y})  [绝对: {result.get('abs')}]",
                "SUCCESS",
            )
        else:
            x, y = result.get("abs", (0, 0))
            self._log.append_log(
                f"[📍] 拾取完成：绝对坐标 ({x}, {y})", "SUCCESS",
            )
        # 写入 tuple 参数
        self._on_param_changed(node_id, param_name, (int(x), int(y)))
        # 刷新 inspector
        if self._graph and node_id in self._graph.nodes:
            self._inspector.set_node(self._graph.nodes[node_id])

    # ---------- FindImage 偏移视觉拾取（以模板中心为基准） ----------
    def _start_findimg_offset_pick(self, node_id: str):
        """找图节点 offset 偏移视觉拾取：
        隐藏主窗口 → 截全屏 → 用模板做一次匹配 → 显示截图+模板标记 →
        用户点目标位置 → 计算 offset = 目标点 - 模板中心 → 写入 offset 参数
        """
        if not self._graph or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]

        # 1) 获取模板 PIL Image
        tpl_b64 = str(node.params.get("template_b64", "") or "")
        tpl_path = str(node.params.get("template", "") or "")
        tpl_pil = None
        if tpl_b64:
            try:
                import base64 as _b64, io as _io
                from PIL import Image as _PILImg
                tpl_pil = _PILImg.open(_io.BytesIO(_b64.b64decode(tpl_b64)))
            except Exception:
                tpl_pil = None
        if tpl_pil is None and tpl_path and os.path.exists(tpl_path):
            try:
                from PIL import Image as _PILImg
                tpl_pil = _PILImg.open(tpl_path)
            except Exception:
                tpl_pil = None
        if tpl_pil is None:
            QMessageBox.information(self, "提示", "还没有模板图片，请先点「📸 截取目标」或「📂 导入图片」设置模板。")
            return

        # 2) 隐藏主窗口 → 截全屏
        self._log.append_log("[📍偏移拾取] 正在截取屏幕并匹配模板...", "INFO")
        self.hide()
        from PySide6.QtCore import QCoreApplication
        for _ in range(5):
            QCoreApplication.processEvents()
        import time as _t
        _t.sleep(0.3)

        screen_pil = None
        try:
            from PIL import ImageGrab
            screen_pil = ImageGrab.grab(all_screens=True)
        except Exception as e:
            self.show()
            QMessageBox.warning(self, "截图失败", f"截屏失败: {e}")
            return

        # 3) 用 OpenCV matchTemplate 在截屏上定位模板
        template_rect = None  # (left, top, w, h)
        try:
            import cv2
            import numpy as np
            screen_cv = cv2.cvtColor(np.array(screen_pil), cv2.COLOR_RGB2BGR)
            tpl_cv = cv2.cvtColor(np.array(tpl_pil), cv2.COLOR_RGB2BGR)
            # 模板不能大于截图
            th, tw = tpl_cv.shape[:2]
            sh, sw = screen_cv.shape[:2]
            if th > sh or tw > sw:
                raise ValueError("模板比屏幕大")
            result = cv2.matchTemplate(screen_cv, tpl_cv, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= 0.7:
                template_rect = (int(max_loc[0]), int(max_loc[1]), int(tw), int(th))
                self._log.append_log(
                    f"[📍偏移拾取] 模板匹配成功: 位置=({template_rect[0]},{template_rect[1]}) "
                    f"尺寸={template_rect[2]}×{template_rect[3]} 匹配度={max_val:.2f}",
                    "SUCCESS",
                )
        except Exception as e:
            self._log.append_log(f"[📍偏移拾取] 模板匹配失败: {e}", "WARN")

        if template_rect is None:
            self.show()
            QMessageBox.information(
                self,
                "未找到模板",
                "在当前屏幕上未找到模板图像（匹配度 < 0.7），无法进行偏移拾取。\n"
                "请确保：\n"
                "1. 模板图片在当前屏幕上可见\n"
                "2. 蓝图工具已最小化，未遮挡目标画面\n"
                "3. 模板图片清晰且有明显特征",
            )
            return

        # 4) 将 screen_pil → QPixmap
        try:
            from PySide6.QtGui import QImage
            if screen_pil.mode != "RGBA":
                screen_pil = screen_pil.convert("RGBA")
            data = screen_pil.tobytes("raw", "RGBA")
            qimg = QImage(data, screen_pil.width, screen_pil.height,
                          screen_pil.width * 4, QImage.Format_RGBA8888)
            screenshot_pix = QPixmap.fromImage(qimg)
        except Exception as e:
            self.show()
            QMessageBox.warning(self, "图像转换失败", f"截屏转 QPixmap 失败: {e}")
            return

        # 5) 启动 VisualOffsetOverlay
        from PySide6.QtCore import QRect
        from src.blueprint.ui.coord_overlay import VisualOffsetOverlay
        l, t, w, h = template_rect
        tpl_qrect = QRect(l, t, w, h)

        self._log.append_log("[📍偏移拾取] 请在屏幕上点击目标位置（offset = 目标点 - 模板中心）", "INFO")

        overlay = VisualOffsetOverlay.prepare_and_run(
            self,
            screenshot=screenshot_pix,
            template_rect=tpl_qrect,
            delay_ms=100,
        )
        overlay.finished.connect(
            lambda result, nid=node_id: self._on_findimg_offset_picked(nid, result)
        )

    def _on_findimg_offset_picked(self, node_id: str, result):
        """VisualOffsetOverlay 拾取完成回调：result = (dx, dy) 相对模板中心像素偏移，或 None"""
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        if result is None:
            self._log.append_log("[📍偏移拾取] 已取消", "INFO")
            return
        ox, oy = int(result[0]), int(result[1])
        self._on_param_changed(node_id, "offset", (ox, oy))
        self._log.append_log(
            f"[📍偏移拾取] 完成：offset = ({ox:+d}, {oy:+d})  [相对模板中心]",
            "SUCCESS",
        )
        if self._graph and node_id in self._graph.nodes:
            self._inspector.set_node(self._graph.nodes[node_id])

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
        runner = BlueprintRunner(self._graph, self, initial_hwnd=self.get_active_hwnd())
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
            "⏸ 已暂停，运行冻结（不丢状态 / 不变量）。再点「继续」恢复；或点⏭下一步单步执行一个节点。"
            if now_paused else "▶ 已恢复执行。",
            "WARN" if now_paused else "INFO",
        )

    def _on_step_clicked(self):
        """工具栏 ⏭ 下一步 (Step Over)：仅暂停状态下可用，单步执行 1 个节点后自动再次暂停。"""
        if self._runner is None or not self._runner.isRunning():
            self._log.append_log("⏭ 下一步需要蓝图运行中才能使用（请先▶运行）。", "INFO")
            return
        if not self._runner.is_paused:
            self._log.append_log("⏭ 下一步只在暂停状态下生效（先⏸暂停 / 命中断点自动暂停 → 再点⏭下一步步过下一个节点）。", "WARN")
            return
        ok = self._runner.request_step()
        if not ok:
            self._log.append_log("⏭ 下一步请求失败（蓝图可能已停止 / 上下文丢失）。", "WARN")

    def _apply_pause_state_to_ui(self, paused: bool):
        """根据暂停/继续，动态切换暂停按钮的文字/背景色/状态栏提示，以及⏭下一步按钮使能。
        【新增】任何原因进入 paused=True（命中🔴断点 / 手动⏸ / ⏭下一步单步自动暂停） → 自动把画布视图
        跳到当前执行节点 current_node_id 所在位置，和搜索栏「📍定位」按钮同一逻辑，不用再手动点按钮找节点。"""
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
            # ⏭ 下一步按钮：暂停=True 才能点（运行中还没停住 → 下一步没用；结束后也没用）
            if hasattr(self, "_act_step"):
                self._act_step.setEnabled(bool(paused))
            if paused:
                self._status_run.setText("状态: 已暂停  [按⏸继续 / ⏭下一步单步]")
                self._status_run.setStyleSheet("color: #F0B429; font-size: 10px; padding: 0 8px;")
                # 📍 自动跳画布到当前暂停的那个节点（3 种暂停都生效：断点命中 / 手动⏸ / ⏭单步完）
                try:
                    if self._view is not None and self._runner is not None:
                        curr_id = getattr(self._runner, "current_node_id", None)
                        if isinstance(curr_id, str) and len(curr_id) > 0:
                            ok = self._view.jump_to_node_by_id(curr_id, flash_ms=3200)
                            if ok:
                                ntype = getattr(self._runner, "current_node_type", None) or "节点"
                                self._log.append_log(
                                    f"📍画布已自动定位到暂停节点 → {ntype} id={curr_id[:8]}",
                                    "INFO",
                                )
                except Exception:
                    pass
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
            "create_find_image_node": self._hook_create_find_image_node,
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
        # 进入"批量粘贴"收集模式：期间的 node_created 不会单独记 undo，结束时再整体 push 一条
        self._paste_batch_collecting = True
        self._paste_batch_node_ids = []
        new_ids: List[str] = []
        try:
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
                    try:
                        self._scene.node_param_changed.emit(node.id, "", None)
                    except Exception:
                        pass
                new_ids.append(node.id)
        finally:
            self._paste_batch_collecting = False
            self._paste_batch_node_ids = []
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
        # Push undo（带节点完整快照，用于重做时重新创建）
        node_entries = []
        for nid in new_ids:
            s = self._scene.snapshot_node(nid)
            if s is not None:
                node_entries.append(s)
        self._push_undo({"kind": "add_nodes", "nodes": node_entries})
        self._scene.graph_changed.emit()
        self._log.append_log(f"[粘贴] 已粘贴 {len(new_ids)} 个节点（Ctrl+Z 可撤销本次粘贴）", "INFO")

    # ============================================================
    # 撤销/重做：公共辅助
    # ============================================================
    def _push_undo(self, action: dict):
        """任何"写操作"都通过这里 push 撤销项 → 自动清空 redo 栈"""
        if self._applying_undo_redo:
            return  # 正在执行撤销/重做本身：不要再产生新的撤销记录
        self._undo_stack.append(action)
        self._redo_stack.clear()

    def _restore_node_from_snapshot(self, snap: dict):
        """从快照恢复节点（不发撤销信号）——用于 undo/redo 重放阶段"""
        if self._scene is None or self._scene.graph is None:
            return None
        ntype = snap.get("type")
        if not ntype or ntype not in NODE_LIBRARY:
            return None
        pos = snap.get("pos") or (200, 200)
        # 强制使用原id（否则 redo 出来会是新 id，连线恢复对不上）
        orig_id = snap.get("id")
        if orig_id is None:
            # 原快照没 id：退化为自增分配（极少情况）
            from blueprint.core.graph_store import BlueprintStore
            node = BlueprintStore.add_node(self._scene.graph, ntype, pos=pos)
        else:
            node = create_node(ntype, pos=pos, node_id=str(orig_id))
        params = snap.get("params") or {}
        if params:
            node.params.update(dict(params))
        # 恢复 inputs/outputs 完整结构（端口定义 + 标签等）
        inputs_list = snap.get("inputs") or []
        outputs_list = snap.get("outputs") or []
        if inputs_list and isinstance(inputs_list, list):
            try:
                node.inputs = [Port.from_dict(pd) for pd in inputs_list]
            except Exception:
                pass
        if outputs_list and isinstance(outputs_list, list):
            try:
                node.outputs = [Port.from_dict(pd) for pd in outputs_list]
            except Exception:
                pass
        # redo 出来的节点注册到 graph；如果强制指定了非自增的数字id，要同步 bump _next 避免后续冲突
        g = self._scene.graph
        g.nodes[node.id] = node
        try:
            v = int(str(node.id))
            cur_next = int(getattr(g, "_next_node_id", 1))
            if v >= cur_next:
                g._next_node_id = v + 1
        except (TypeError, ValueError):
            pass
        # _add_node_item(emit=False) 保持撤销期静默
        self._scene._add_node_item(node, emit=False)
        return node

    def _restore_edge_from_snapshot(self, snap: dict):
        if self._scene is None or self._scene.graph is None:
            return None
        edge = Edge(
            id=snap.get("id") or uuid.uuid4().hex[:12],
            from_node=snap.get("from_node"),
            from_port=snap.get("from_port"),
            to_node=snap.get("to_node"),
            to_port=snap.get("to_port"),
        )
        if edge.from_node not in self._scene.graph.nodes or edge.to_node not in self._scene.graph.nodes:
            return None
        self._scene.graph.edges[edge.id] = edge
        self._scene._add_edge_item(edge, emit=False)
        return edge

    def _set_node_position(self, node_id: str, pos_tuple):
        if self._scene is None:
            return
        ni = self._scene._node_items.get(node_id)
        if ni is None:
            return
        x, y = pos_tuple
        ni.setPos(x, y)  # 会触发 itemChange，node.pos 会自动同步
        ni.node_position_changed.emit(node_id, ni.pos())

    # ============================================================
    # Scene 写操作监听 → 把操作压入撤销栈
    # ============================================================
    def _on_scene_node_created(self, node_id: str):
        if self._applying_undo_redo:
            return
        if self._paste_batch_collecting:
            self._paste_batch_node_ids.append(node_id)
            return
        # 单节点新增（如：从节点库拖进来）
        snap = self._scene.snapshot_node(node_id)
        if snap is None:
            return
        self._push_undo({"kind": "add_nodes", "nodes": [snap]})

    def _on_scene_edge_created(self, edge_id: str):
        if self._applying_undo_redo:
            return
        snap = self._scene.snapshot_edge(edge_id)
        if snap is None:
            return
        self._push_undo({"kind": "add_edges", "edges": [snap]})

    # ---- 删除操作的"合并"：Delete 键一次可能删多个节点/边，用 0ms Timer 合并成 1 条撤销项 ----
    def _on_scene_node_will_remove(self, node_id: str, node_snap: dict, related_edge_snaps: list):
        if self._applying_undo_redo:
            return
        # 关键修复：不能用 hasattr(self, "_pending_remove_nodes") 判断，
        # 因为 _flush_pending_removes 执行后属性还在（只是列表被清空），
        # 会导致第二次删除时定时器不注册，undo 栈永远不 push（问题1根因）。
        # 改用专门的 _remove_flush_scheduled 标志，在 _flush_pending_removes 结束时重置。
        if not getattr(self, "_remove_flush_scheduled", False):
            self._remove_flush_scheduled = True
            self._pending_remove_nodes = []
            self._pending_remove_edges = []
            QTimer.singleShot(0, self._flush_pending_removes)
        self._pending_remove_nodes.append((node_snap, related_edge_snaps or []))

    def _on_scene_edge_will_remove(self, edge_id: str, edge_snap: dict):
        if self._applying_undo_redo:
            return
        # 同上：用 _remove_flush_scheduled 标志而非 hasattr 判断
        if not getattr(self, "_remove_flush_scheduled", False):
            self._remove_flush_scheduled = True
            self._pending_remove_nodes = []
            self._pending_remove_edges = []
            QTimer.singleShot(0, self._flush_pending_removes)
        self._pending_remove_edges.append(edge_snap)

    def _flush_pending_removes(self):
        """将同一轮事件循环里的"删节点/删边"合并成一条撤销项"""
        node_list = getattr(self, "_pending_remove_nodes", None) or []
        edge_list = getattr(self, "_pending_remove_edges", None) or []
        self._pending_remove_nodes = []
        self._pending_remove_edges = []
        # 关键：重置标志，让下次删除操作能重新注册定时器（问题1修复）
        self._remove_flush_scheduled = False
        if not node_list and not edge_list:
            return
        # 构造 remove_nodes 项：节点快照 + 节点关联边 + 独立删除的边 放在一起
        all_node_entries = []
        all_edge_entries = list(edge_list)
        for n_snap, rel_edges in node_list:
            all_node_entries.append(n_snap)
            all_edge_entries.extend(rel_edges)
        # 去重：关联边可能和独立删除重复（按 edge id 唯一）
        seen_edge_ids = set()
        uniq_edges = []
        for e in all_edge_entries:
            eid = e.get("id")
            if eid in seen_edge_ids:
                continue
            seen_edge_ids.add(eid)
            uniq_edges.append(e)
        action = {"kind": "remove_nodes", "nodes": all_node_entries, "edges": uniq_edges}
        self._push_undo(action)

    def _on_scene_nodes_dragged(self, changed: list):
        """changed: list of (node_id, old_xy, new_xy)"""
        if self._applying_undo_redo:
            return
        if not changed:
            return
        entries = [
            {"node_id": nid, "old_pos": (ox, oy), "new_pos": (nx, ny)}
            for (nid, (ox, oy), (nx, ny)) in changed
        ]
        self._push_undo({"kind": "move_nodes", "nodes": entries})

    # ============================================================
    # 撤销 / 重做 主入口
    # ============================================================
    def _on_undo(self):
        if self._scene is None or self._scene.graph is None:
            return
        if not self._undo_stack:
            self._log.append_log("[撤销] 撤销栈为空（没有可撤销的操作）", "WARN")
            return
        action = self._undo_stack.pop()
        kind = action.get("kind")
        self._applying_undo_redo = True
        try:
            if kind == "add_nodes":
                # 撤销"新增节点"：删除这些节点
                node_entries = action.get("nodes") or []
                removed_ids = []
                for s in node_entries:
                    nid = s.get("id")
                    if nid and nid in self._scene.graph.nodes:
                        self._scene.remove_node(nid)
                        removed_ids.append(nid)
                self._redo_stack.append(action)
                self._log.append_log(f"[撤销] 撤销新增节点：删除 {len(removed_ids)} 个节点（Ctrl+Y 重做）", "INFO")
            elif kind == "remove_nodes":
                # 撤销"删除节点"：恢复节点 + 边
                node_entries = action.get("nodes") or []
                edge_entries = action.get("edges") or []
                restored_nodes = 0
                for s in node_entries:
                    if self._restore_node_from_snapshot(s) is not None:
                        restored_nodes += 1
                restored_edges = 0
                for es in edge_entries:
                    if self._restore_edge_from_snapshot(es) is not None:
                        restored_edges += 1
                self._redo_stack.append(action)
                self._log.append_log(f"[撤销] 撤销删除：恢复 {restored_nodes} 个节点、{restored_edges} 条连线（Ctrl+Y 重做）", "INFO")
            elif kind == "move_nodes":
                # 撤销"移动节点"：回到旧位置
                entries = action.get("nodes") or []
                for e in entries:
                    self._set_node_position(e["node_id"], e["old_pos"])
                self._redo_stack.append(action)
                self._log.append_log(f"[撤销] 撤销位置移动：{len(entries)} 个节点回到原位置（Ctrl+Y 重做）", "INFO")
            elif kind == "add_edges":
                # 撤销"新增连线"：删除这些连线
                edge_entries = action.get("edges") or []
                removed = 0
                for es in edge_entries:
                    eid = es.get("id")
                    if eid and eid in self._scene.graph.edges:
                        self._scene.remove_edge(eid, _emit_will=False)
                        removed += 1
                self._redo_stack.append(action)
                self._log.append_log(f"[撤销] 撤销新增连线：删除 {removed} 条（Ctrl+Y 重做）", "INFO")
            elif kind == "change_param":
                # 撤销"参数修改"：恢复 old_value
                nid = action.get("node_id")
                pname = action.get("param_name")
                old_v = action.get("old_value")
                if nid and (self._graph is None or nid not in self._graph.nodes):
                    # 节点不存在了（比如之后被删除）→ 把 action 塞回栈，提示不支持
                    self._undo_stack.append(action)
                    self._log.append_log(f"[撤销] 节点 {nid} 已不存在，无法撤销它的参数 {pname}", "WARN")
                    return
                # 应用 old_value：走 _on_param_changed 全流程（端口重建、边清理、inspector 刷新、画布重绘等）
                # _applying_undo_redo=True 会让它跳过压栈的部分，避免递归
                if self._applying_undo_redo:
                    self._on_param_changed(nid, pname, old_v)
                self._redo_stack.append(action)
                self._log.append_log(
                    f"[撤销] 恢复参数：节点 {nid} 的 {pname}",
                    "INFO",
                )
            else:
                # 不认识的操作：把 action 塞回栈
                self._undo_stack.append(action)
                self._log.append_log(f"[撤销] 暂不支持撤销「{kind}」，后续版本会补齐。", "WARN")
                return
        finally:
            self._applying_undo_redo = False
        self._scene.graph_changed.emit()

    def _on_redo(self):
        if self._scene is None or self._scene.graph is None:
            return
        if not self._redo_stack:
            self._log.append_log("[重做] 重做栈为空（没有可重做的撤销）", "WARN")
            return
        action = self._redo_stack.pop()
        kind = action.get("kind")
        self._applying_undo_redo = True
        try:
            if kind == "add_nodes":
                # 重做"新增节点"：恢复这些节点
                node_entries = action.get("nodes") or []
                restored = 0
                for s in node_entries:
                    if self._restore_node_from_snapshot(s) is not None:
                        restored += 1
                self._undo_stack.append(action)
                self._log.append_log(f"[重做] 重新新增 {restored} 个节点", "INFO")
            elif kind == "remove_nodes":
                # 重做"删除节点/边"：先删独立边（避免节点删除后关联边自动删重复处理），再删节点
                edge_entries = action.get("edges") or []
                removed_edges = 0
                for es in edge_entries:
                    eid = es.get("id")
                    if eid and eid in self._scene.graph.edges:
                        self._scene.remove_edge(eid, _emit_will=False)
                        removed_edges += 1
                node_entries = action.get("nodes") or []
                removed_nodes = 0
                for s in node_entries:
                    nid = s.get("id")
                    if nid and nid in self._scene.graph.nodes:
                        self._scene.remove_node(nid)
                        removed_nodes += 1
                self._undo_stack.append(action)
                self._log.append_log(
                    f"[重做] 重新删除 {removed_nodes} 个节点、{removed_edges} 条连线",
                    "INFO",
                )
            elif kind == "move_nodes":
                # 重做"移动节点"：移到新位置
                entries = action.get("nodes") or []
                for e in entries:
                    self._set_node_position(e["node_id"], e["new_pos"])
                self._undo_stack.append(action)
                self._log.append_log(f"[重做] 重新移动 {len(entries)} 个节点到目标位置", "INFO")
            elif kind == "add_edges":
                # 重做"新增连线"：恢复连线
                edge_entries = action.get("edges") or []
                restored = 0
                for es in edge_entries:
                    if self._restore_edge_from_snapshot(es) is not None:
                        restored += 1
                self._undo_stack.append(action)
                self._log.append_log(f"[重做] 重新新增 {restored} 条连线", "INFO")
            elif kind == "change_param":
                # 重做"参数修改"：恢复 new_value
                nid = action.get("node_id")
                pname = action.get("param_name")
                new_v = action.get("new_value")
                if nid and (self._graph is None or nid not in self._graph.nodes):
                    self._redo_stack.append(action)
                    self._log.append_log(f"[重做] 节点 {nid} 已不存在，无法重做它的参数 {pname}", "WARN")
                    return
                if self._applying_undo_redo:
                    self._on_param_changed(nid, pname, new_v)
                self._undo_stack.append(action)
                self._log.append_log(
                    f"[重做] 重新修改参数：节点 {nid} 的 {pname}",
                    "INFO",
                )
            else:
                self._redo_stack.append(action)
                self._log.append_log(f"[重做] 暂不支持重做「{kind}」，后续版本会补齐。", "WARN")
                return
        finally:
            self._applying_undo_redo = False
        self._scene.graph_changed.emit()

    # ---------- Hooks implementation ----------
    def _hook_create_find_image_node(self, template_path, region_tuple4_or_None, template_b64_or_None=None):
        """切分工具/截图工具回调：在画布中央新增一个 FindImage 节点，并预填参数。

        参数：
          template_path        - 已保存到磁盘的模板 PNG 绝对路径（必填）。
          region_tuple4_or_None - (x, y, w, h) 源图上的相对矩形，用于 FindImage 的 region（可选）。
          template_b64_or_None - 模板的 base64 PNG 缩略图（可选，<=220px），用于 inspector 直接显示预览。

        返回：成功=返回 node.id（非空字符串）；失败=返回 None。
        """
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
            if isinstance(template_b64_or_None, str) and template_b64_or_None:
                node.params["template_b64"] = template_b64_or_None
            if region_tuple4_or_None is not None and len(region_tuple4_or_None) == 4:
                rx, ry, rw, rh = region_tuple4_or_None
                node.params["region"] = (int(rx), int(ry), int(rw), int(rh))
            self._scene.select_node_id(node.id)
            self._inspector.set_node(node)
            # 同样：撤销栈不能重复 push node_created
            self._scene.graph_changed.emit()
            return node.id
        except Exception as e:
            self._log.append_log(f"[Hook] create_find_image_node 失败: {e}", "ERROR")
            return None

    # ==================== 全局热键 & 游戏内无弹窗截图 ====================
    def closeEvent(self, event):
        """关闭窗口时：先检查蓝图是否有未保存修改 → 再释放 pynput 全局钩子，避免进程残留。"""
        # 1) 先检查并询问蓝图保存（用户明确要求：关程序前必须检测保存状态）
        try:
            if self._runner is not None and self._runner.isRunning():
                ret = QMessageBox.question(
                    self, "蓝图正在运行中",
                    "当前有蓝图正在执行，关闭程序会强制停止它。\n\n是否仍然关闭？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if ret != QMessageBox.Yes:
                    event.ignore()
                    return
                try:
                    self._runner.request_stop()
                    self._runner.wait(500)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if not self._confirm_discard():
                # 用户选了 Cancel 或保存后仍需保存（例如保存失败又取消丢弃）
                event.ignore()
                return
        except Exception:
            pass
        # 2) 释放全局热键钩子
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
        # 2.5) 销毁 LLM 相关窗口（悬浮球 + 聊天窗口 + 设置对话框）
        try:
            for _attr in ("_llm_settings_dialog_ref", "_floating_ball"):
                w = getattr(self, _attr, None)
                if w is None:
                    continue
                try:
                    # full_chat_dialog / quick_bubble 是子窗口挂在 floating_ball 上的，这里关父窗口就行
                    w.close()
                except Exception:
                    pass
                try:
                    w.deleteLater()
                except Exception:
                    pass
            setattr(self, "_llm_settings_dialog_ref", None)
            setattr(self, "_floating_ball", None)
        except Exception:
            pass
        # 3) 同步用户设置到磁盘（保证设置对话框里改过的热键等下次启动还在）
        try:
            save_user_settings(self._user_settings)
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
         此函数返回 {} 作为占位（_apply_shortcuts_from_settings 遍历 SHORTCUT_REGISTRY，
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
        """打开设置窗口（支持修改全部快捷键）。

        关键点：在设置界面打开期间，临时暂停 GlobalHotkeyManager 的 pynput 监听，
        防止用户录制新快捷键时，按键刚好触发已经注册的全局热键（比如用户把暂停/运行
        热键绑定到了 F5，录制时按 F5 就把蓝图停了）。设置窗口关闭后（无论点确定还是取消）
        再恢复监听。
        """
        # 用当前内存里的 user_settings 初始化（SettingsDialog会自己read_user_settings，但保持capture_target行为）
        dlg = SettingsDialog(parent=self)
        # 临时屏蔽所有 pynput 全局热键（listener 停止，但 _hotkeys map 完整保留，resume 时直接用）
        try:
            self._hotkey_mgr.pause_all()
        except Exception as e:
            print(f"[GlobalHotkeyManager] pause_all before SettingsDialog failed: {e}")

        try:
            if dlg.exec() != QDialog.Accepted:
                return
        finally:
            # 不管点「确定」还是「取消」，最后都一定要恢复全局热键监听
            try:
                self._hotkey_mgr.resume_all()
            except Exception as e:
                print(f"[GlobalHotkeyManager] resume_all after SettingsDialog failed: {e}")

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

    # ==================== LLM UI 壳子 ====================
    def _on_llm_settings_clicked(self):
        """工具栏 🤖 LLM 设置按钮点击 → 打开/激活对话框（单例，不会开多个）。"""
        try:
            from src.blueprint.llm.ui.llm_settings_panel import LLMSettingsDialog
        except Exception as e:
            QMessageBox.critical(
                self, "模块加载失败",
                f"加载 LLM 设置面板失败（UI 壳子文件缺失或语法错误）：\n{e}"
            )
            return

        if self._llm_settings_dialog_ref is None or not getattr(
            self._llm_settings_dialog_ref, "isVisible", lambda: False)():
            dlg = LLMSettingsDialog(self)
            # 保存后如果启用开关变了 → 立即显示/隐藏悬浮球（两个信号都监听，双保险）
            try:
                dlg.llm_enabled_changed.connect(self._on_llm_enabled_changed_from_dialog)
            except Exception as e:
                try: self._log.append_log(f"[LLM] 连接信号 llm_enabled_changed 失败：{e}", "WARN")
                except Exception: pass
            try:
                dlg.settings_saved.connect(self._on_llm_settings_saved_from_dialog)
            except Exception as e:
                try: self._log.append_log(f"[LLM] 连接信号 settings_saved 失败：{e}", "WARN")
                except Exception: pass
            try:
                dlg.destroyed.connect(lambda *_: setattr(self, "_llm_settings_dialog_ref", None))
            except Exception:
                pass
            self._llm_settings_dialog_ref = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        else:
            # 已打开 → 提到前台
            try:
                dlg = self._llm_settings_dialog_ref
                dlg.showNormal()
                dlg.raise_()
                dlg.activateWindow()
            except Exception:
                pass

    def _on_llm_settings_saved_from_dialog(self, data: dict):
        """设置对话框保存 → 根据 data['enable'] 同步悬浮球。"""
        try:
            en = bool(data.get("enable", True))
            self._ensure_floating_ball(show=en)
            try: self._log.append_log(f"[LLM] 设置已保存，悬浮球状态：{'显示' if en else '隐藏'}", "INFO")
            except Exception: pass
        except Exception:
            pass

    def _on_llm_enabled_changed_from_dialog(self, enabled: bool):
        """设置对话框里勾/取消「启用 LLM」 → 立即创建/销毁悬浮球（实时，不等保存）。"""
        try:
            self._ensure_floating_ball(show=bool(enabled))
            try: self._log.append_log(f"[LLM] 开关切换：悬浮球{'显示' if enabled else '隐藏'}（实时生效）", "INFO")
            except Exception: pass
        except Exception as e:
            try: self._log.append_log(f"[LLM] 创建/销毁悬浮球失败：{type(e).__name__}: {e}", "ERROR")
            except Exception: pass

    def _ensure_floating_ball(self, show: bool):
        """
        show=True  → 若没创建就创建悬浮球并显示；已存在就 raise_ 到前面
        show=False → 若存在就关闭并销毁并置 None
        """
        if show:
            if self._floating_ball is None:
                try:
                    from src.blueprint.llm.ui.floating_ball import FloatingBall
                    # ⚠ 传 parent=None —— 必须让 FloatingBall 成为独立顶层窗口：
                    #   - 主窗口最小化后悬浮球仍可见
                    #   - Z-order 不跟随主窗口（游戏全屏时能压在游戏上面）
                    ball = FloatingBall(None)
                    # （预留）用户在悬浮球发送的消息，可以接信号连到 BlueprintGenerator
                    try:
                        ball.user_message.connect(self._on_ball_user_message_stub)
                    except Exception:
                        pass
                    self._floating_ball = ball
                except Exception as e:
                    self._status_lbl.setText(f"[LLM] 创建悬浮球失败：{e}")
                    return
            try:
                self._floating_ball.show()
                self._floating_ball.raise_()
            except Exception:
                pass
        else:
            if self._floating_ball is None:
                return
            try:
                # 先把挂在它下面的气泡/完整对话框关掉
                for attr in ("_full_dlg", "_quick_bubble"):
                    sub = getattr(self._floating_ball, attr, None)
                    if sub is not None:
                        try:
                            sub.close()
                        except Exception:
                            pass
                        try:
                            sub.deleteLater()
                        except Exception:
                            pass
                    try:
                        setattr(self._floating_ball, attr, None)
                    except Exception:
                        pass
                self._floating_ball.close()
                try:
                    self._floating_ball.deleteLater()
                except Exception:
                    pass
            except Exception:
                pass
            self._floating_ball = None

    def _on_ball_user_message_stub(self, msg: str):
        """悬浮球发送消息打桩：在状态栏显示一条提示（真正接入 LLMClient 后改这里）。"""
        try:
            short = msg if len(msg) <= 40 else msg[:37] + "..."
            self._status_lbl.setText(f"[LLM 打桩] 已接收：{short}（真实发送功能待接入）")
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
    # ⚠ 【启动时也要自己切工程】
    # ProjectStartupDialog 已经改成：回调里绝不提前 open_project，以解决「切工程前必须先 confirm_discard」
    # 的严格时序要求；所以启动时自己在这里调 open_project 让 PM 状态正确。
    root = startup.result_project_root
    if not root or not pm.open_project(root):
        print(f"[BOOT] 启动失败：无法打开工程 {root!r}，已退出。")
        return 1
    if not pm.is_open():
        # 兜底：理论 open_project 成功后 is_open 必为 True
        return 1

    win = BlueprintMainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
