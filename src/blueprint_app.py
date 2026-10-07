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


from PySide6.QtCore import Qt, QPointF, QSize, QTimer
from PySide6.QtGui import QAction, QKeySequence, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
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

# OCR（可选）：库（paddle 等）装在 .venv 里（start.cmd ocr-runtime 安装），
# 开发模式直接 import 即可；模型在 ocr_runtime/models，由 ocr_engine 解析。

import uuid

from src.blueprint.core.graph_store import BlueprintStore
from src.blueprint.core import asset_store as asset_store_mod
from src.blueprint.core import bp_kind as bp_kind_mod
from src.blueprint.core.models import BlueprintGraph, Edge, Port
from src.blueprint.core.node_library import NODE_LIBRARY, create_node
from src.blueprint.project_manager import ProjectManager
from src.blueprint.runtime.executor import BlueprintRunner
from src.blueprint.runtime.validator import (
    validate_graph,
    has_errors,
    format_issues,
)
from src.blueprint.ui.asset_panel import AssetPanel
from src.blueprint.ui.graphics_scene import NodeScene, NodeView
from src.blueprint.ui.inspector_panel import InspectorPanel
from src.blueprint.ui.log_panel import LogPanel
from src.blueprint.ui.node_palette import NodePalette
from src.blueprint.ui.project_startup_dialog import ProjectStartupDialog
from src.blueprint.ui.settings_dialog import SettingsDialog
# 全局热键 + 用户设置
from src.tools.hotkey_manager import GlobalHotkeyManager
from src.config.user_settings import (
    get_shortcut,
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

# ============================================================
# 「孤儿 runner」池：停止请求发出后 3 秒内没停干净的 QThread（当前节点是长等待/
# 找图/网络时，要等它跑完手上这个节点才真正退出）。
# 必须脱离父对象 + 持有引用，否则关闭窗口/切换工程时会连带析构一个还在跑的
# QThread —— Qt 直接 abort，表现就是「调试中停止/关窗偶发闪退」。
# 线程真正跑完后由 finished→deleteLater 安全析构（编辑器还活着时）；
# 若编辑器已在退出，残留至进程结束，无害。
# ============================================================
_ORPHAN_RUNNERS = []


def _park_orphan_runner(runner):
    try:
        runner.setParent(None)
    except Exception:
        pass
    try:
        runner.finished.connect(runner.deleteLater)
    except Exception:
        pass
    alive = []
    for _r in _ORPHAN_RUNNERS:
        try:
            if _r.isRunning():
                alive.append(_r)
        except RuntimeError:
            pass  # C++ 对象已被 deleteLater，无需再持引用
    alive.append(runner)
    _ORPHAN_RUNNERS[:] = alive


# ============================================================
# 通知节点：主窗口右上角非阻塞 Toast 浮层
#   - 完全不阻塞蓝图执行（QTimer.singleShot 到时自动销毁）
#   - 支持堆叠：多张 toast 自动从上向下铺
#   - INFO 蓝 / WARN 黄 / ERROR 红，三种配色 + 三种图标字符
# ============================================================
class _NotifyToastCard(QFrame):
    """单张 Notify Toast，附着在主窗口上，position 由父窗口 _relayout_toasts 统一管理。"""

    def __init__(
        self,
        parent: QMainWindow,
        title: str,
        body: str,
        level: str,
        duration_sec: float,
        on_destroyed_cb,
    ):
        super().__init__(parent)
        self._on_destroyed_cb = on_destroyed_cb
        level = (level or "INFO").upper()
        if level not in ("INFO", "WARN", "ERROR", "DEBUG"):
            level = "INFO"
        # ---- 配色 ----
        if level == "ERROR":
            bg, bd, ic, tx = "#3B1314", "#DA3633", "#F85149", "#FFE3E4"
            icon_txt = "✕"
        elif level == "WARN":
            bg, bd, ic, tx = "#3B2A00", "#9A6700", "#F0B429", "#FFF3CD"
            icon_txt = "!"
        else:
            bg, bd, ic, tx = "#0B3A5C", "#1F6FEB", "#58A6FF", "#D9ECFF"
            icon_txt = "ℹ" if level == "INFO" else "ⓓ"

        self.setObjectName("NotifyToast")
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self.setStyleSheet(
            f"QFrame#NotifyToast {{ background:{bg}; color:{tx};"
            f"  border:1px solid {bd}; border-radius:8px; padding:10px; }}"
            f"QLabel {{ background:transparent; color:{tx}; }}"
            f"QToolButton {{ background:transparent; color:{ic}; border:none;"
            f"  font-size:14px; font-weight:700; padding:0px 4px; }}"
            f"QToolButton:hover {{ color:#FFFFFF; }}"
        )
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 8, 10)
        outer.setSpacing(10)

        # 左侧图标（色块 + 字符）
        icon_lbl = QLabel(icon_txt)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet(
            f"QLabel {{ background:{ic}; color:#1C2128; border-radius:12px;"
            f"  font-size:14px; font-weight:800; min-width:24px; min-height:24px; max-width:24px; max-height:24px; }}"
        )
        outer.addWidget(icon_lbl, 0, Qt.AlignTop)

        # 中间：标题 + 内容
        mid = QVBoxLayout()
        mid.setSpacing(4)
        title_s = title.strip() if isinstance(title, str) else ""
        body_s = body.strip() if isinstance(body, str) else ""
        if len(body_s) > 600:
            body_s = body_s[:600] + "\n…（截断，完整内容看日志面板）"

        if title_s:
            t_lbl = QLabel(title_s)
            tf = t_lbl.font()
            tf.setBold(True)
            tf.setPointSize(tf.pointSize() + 1 if tf.pointSize() > 0 else 10)
            t_lbl.setFont(tf)
            t_lbl.setWordWrap(True)
            t_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            mid.addWidget(t_lbl)
        if body_s:
            b_lbl = QLabel(body_s)
            b_lbl.setWordWrap(True)
            b_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            mid.addWidget(b_lbl)
        outer.addLayout(mid, 1)

        # 右侧：手动关闭按钮
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setToolTip("关闭提示")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        outer.addWidget(close_btn, 0, Qt.AlignTop | Qt.AlignRight)

        # 定时自动关闭
        dur_ms = max(500, int(float(duration_sec or 3.0) * 1000))
        QTimer.singleShot(dur_ms, self.close)

    def closeEvent(self, e):
        cb = self._on_destroyed_cb
        super().closeEvent(e)
        try:
            if cb is not None:
                cb(self)
        except Exception:
            pass


class BlueprintMainWindow(QMainWindow):
    """蓝图编辑器主窗口：左节点面板 / 中画布 / 右Inspector / 底日志。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("蓝图自动化工具")
        # 初始尺寸 = 「还原」时的窗口大小（正常几何）。启动时默认走 showMaximized()
        # 直接最大化铺满屏幕（见 main()）；用户点「还原」就回到这个尺寸，
        # 所以这里必须保留一个合理的默认值，删掉的话还原会变成一个很小的裸窗口。
        self.resize(1560, 920)
        self._toasts: List[_NotifyToastCard] = []
        self._current_path: Optional[str] = None
        self._graph: Optional[BlueprintGraph] = None
        self._last_saved_graph_hash: Optional[str] = None  # 上一次保存/加载时的蓝图哈希，None=从未落盘
        # 「设置变量」节点的数据输出端口（= batch_text 里的变量名）防抖重建：
        # batch_text 是**逐字符** textChanged 提交的，若每敲一个字就重建端口，
        # 画布会疯狂重建/重连边 —— 所以这里只登记待处理节点，停手后再统一对齐一次。
        self._setvar_port_pending: set = set()
        # 上次已提示过的「写错的行」指纹（node_id -> 错误摘要），同一种错误只提醒一次，
        # 避免打字过程中每停 400ms 就往日志里刷一条 WARN。
        self._setvar_err_reported: Dict[str, str] = {}
        self._setvar_port_timer = QTimer(self)
        self._setvar_port_timer.setSingleShot(True)
        self._setvar_port_timer.setInterval(400)
        self._setvar_port_timer.timeout.connect(self._apply_setvar_output_ports)
        self._runner: Optional[BlueprintRunner] = None
        # 工程系统
        self._pm: ProjectManager = ProjectManager.instance()
        # 热键相关
        self._user_settings: Dict[str, Any] = read_user_settings()
        self._hotkey_mgr = GlobalHotkeyManager(qt_parent=self)
        # 剪贴板 + 撤销/重做栈（硬编码标准快捷键：Ctrl+C/V/Z/Y，不在设置界面暴露）
        self._clipboard_nodes: list = []
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._applying_undo_redo: bool = False
        self._paste_batch_collecting: bool = False
        self._paste_batch_node_ids: list = []
        # ---- 内嵌文档对话框（UI 壳子） ----
        self._doc_dialog_ref = None            # 文档攻略对话框（单例）

        # ---- 子图导航栈（循环体/顺序体/并行体子画布） ----
        # 进入 Loop/Sequence/Parallel 的子画布时把父上下文压栈；返回时弹栈。
        # 每项 = {"kind": "loop"|"seq"|"parallel", "body_key": 宿主节点参数键,
        #         "parent_graph": BlueprintGraph, "node_id": str, "sub_graph": BlueprintGraph}
        # sub_graph 是本层子图对象，退出/回写时以它为准（不能依赖 self._graph，它可能被保存逻辑临时改动）。
        self._subgraph_stack: list = []
        self._breadcrumb_bar = None            # 面包屑导航条（进入子图时显示）

        self._build_central()
        self._build_toolbar()
        self._build_statusbar()
        self._wire_signals()
        # 绑定：全局热键 ID -> 回调（统一 QTimer.singleShot 立即到主线程，避免重入）
        for hid, cb in self._global_hotkey_id_to_callback().items():
            self._hotkey_mgr.set_callback(hid, lambda c=cb: QTimer.singleShot(0, c))

        # 启动：如果当前已经有打开的工程（main里弹过ProjectStartupDialog设置过），
        # 就加载该工程下的 <工程名>.bp.json；否则创建一张未保存的空白图（理论不会走到这，main会强制先选工程）
        self._load_or_new_graph_for_current_project()
        # 启动时把左侧「🗂 工程资源」树按当前工程填好（这条启动路径不走 _finalize_project_switch）
        # reveal_path=默认蓝图：自动展开 blueprints/ 并选中默认蓝图，避免它藏在折叠目录里看不见
        self._refresh_assets(reveal_path=self._current_path)

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
        所有「写资源」的操作（截图、运行蓝图、保存模板）前必须调用：
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
        # 3) 刷新最近工程菜单 + 标题/状态栏 + 左侧资源树（换工程后必须重建）
        try:
            self._refresh_recent_projects_menu()
        except Exception:
            pass
        try:
            # reveal_path=当前蓝图：自动展开其所在目录（如 blueprints/）并选中，
            # 确保「创建/打开工程」后默认蓝图立刻在资源树里可见，而不是藏在折叠目录中
            self._refresh_assets(reveal_path=self._current_path)
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
            self._stop_and_detach_runner()
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
        # 全局图片路径缓存在 _finalize_project_switch 中由 clear_all_project_caches 清理

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
                f"当前工程内暂未创建任何蓝图文件，请在左侧『🗂 工程资源』面板点「＋蓝图」新建（或 Ctrl+S 保存为 .bp.json）。",
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
    @staticmethod
    def _menu_qss() -> str:
        """深色主题的 QMenu 样式。

        默认 QMenu 在深色底上「选中态」几乎不可见，用户反馈"鼠标放上去没效果、
        不知道当前选中哪一项"；这里统一给菜单/菜单项加 hover 高亮与分隔线样式。
        """
        return (
            "QMenu { background:#161B22; color:#C9D1D9; border:1px solid #30363D; padding:4px; }"
            "QMenu::item { padding:6px 22px 6px 14px; border-radius:3px; }"
            "QMenu::item:selected { background:#1F6FEB; color:#FFFFFF; }"
            "QMenu::item:disabled { color:#6E7681; }"
            "QMenu::separator { height:1px; background:#30363D; margin:4px 6px; }"
        )

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
        self._scene.edge_priority_changed.connect(self._on_scene_edge_priority_changed)
        self._scene.nodes_dragged.connect(self._on_scene_nodes_dragged)

        self._view = NodeView(self._scene)
        center_wrap = QWidget()
        cl = QVBoxLayout(center_wrap)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        # 面包屑导航条（进入循环体子画布 / 组件子图时显示，返回父图时隐藏）
        self._breadcrumb_bar = self._build_breadcrumb_bar()
        self._breadcrumb_bar.setVisible(False)
        cl.addWidget(self._breadcrumb_bar)
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

        # 左：🗂 工程资源（资源管理器，位于节点库左边）
        self._assets = AssetPanel()
        dock_assets = QDockWidget("🗂 工程资源", self)
        dock_assets.setObjectName("dockAssets")
        dock_assets.setWidget(self._assets)
        dock_assets.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock_assets.setFeatures(QDockWidget.DockWidgetMovable)
        dock_assets.setMinimumWidth(230)
        dock_assets.setMaximumWidth(340)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock_assets)
        dock_assets.setStyleSheet(_dock_stylesheet)
        dock_assets.show()
        self._dock_assets = dock_assets

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
        # 【关键】把资源面板挪到节点库左边（Cocos 式布局：资源 | 节点库 | 画布 | 属性）
        self.splitDockWidget(dock_assets, dock_left, Qt.Horizontal)

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
        # 蓝图级操作中只剩「保存」留在菜单里（其余新建/打开/另存/删除/重命名都在左侧资源面板）
        self._act_save = mk_act("#0969DA", "保存蓝图", self._on_save)

        # 工程级 2 个操作
        self._act_new_project = mk_act("#7C3AED", "新建工程…", self._on_new_project)
        self._act_open_project = mk_act("#7C3AED", "打开工程…", self._on_open_project)

        # === 构建：工程 ▾ 顶层菜单 ===
        # 【2026-09-13 定稿】蓝图级操作（新建/保存/另存/加载/删除）全部移出本菜单，
        # 改由左侧「🗂 工程资源」资源管理器承担（Cocos 式资源面板）。
        # 本菜单只保留工程级操作：新建工程 / 打开工程 / 最近工程。
        self._menu_proj_saveas = QMenu(self)
        # 【关键】菜单 hover 高亮：默认 QMenu 在深色主题下选中态几乎不可见，
        # 用户"鼠标放上去连效果都没有、不知道选中谁"。统一套用深色高亮样式。
        self._menu_proj_saveas.setStyleSheet(self._menu_qss())
        self._menu_proj_saveas.setToolTipsVisible(True)

        # ---------- 第 1 组：📦 工程级（打开工程 与 最近工程 同级，最近工程放在打开工程下面） ----------
        self._menu_proj_saveas.addAction(self._act_new_project)
        self._menu_proj_saveas.addAction(self._act_open_project)
        # 🗂  最近工程：直接作为顶层子菜单挂在 工程▾ 下，与「打开工程…」同级
        self._menu_recent_projects = QMenu("最近工程", self)
        self._menu_recent_projects.setToolTipsVisible(True)
        self._menu_recent_projects.setStyleSheet(self._menu_qss())
        self._menu_proj_saveas.addMenu(self._menu_recent_projects)

        # ---------- 第 2 组：🗂 蓝图级（已精简为仅「保存蓝图」） ----------
        # 保存仍留一个入口（Ctrl+S 也能用）；新建/另存/加载/删除请看左侧资源面板
        self._menu_proj_saveas.addSeparator()
        self._menu_proj_saveas.addAction(self._act_save)

        # === 把构建好的菜单挂到 ToolButton ===
        tb.addSeparator()
        self._btn_proj_menu = QToolButton()
        self._btn_proj_menu.setText("工程 ▾")
        self._btn_proj_menu.setToolTip("新建/打开工程、切换最近工程\n（蓝图的 新建/打开/重命名/删除 请看左侧「🗂 工程资源」面板）")
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

        # --------------- 缩放 / 视图工具组 ---------------
        self._act_fit = mk_act("#8B949E", "缩放适应", self._view.zoom_fit)
        self._act_zoom_1 = mk_act("#8B949E", "重置100%", self._view.reset_zoom)
        # 【2026-07-03 用户明确：只保留4个标准快捷键 Ctrl+S/Z/C/V；其他缩放快捷键移除，按钮仍可点击】
        self._act_fit.setShortcut(QKeySequence())
        self._act_zoom_1.setShortcut(QKeySequence())
        add_toolbtn(self._act_fit)
        add_toolbtn(self._act_zoom_1)

        # --------------- 右侧: 导出 EXE + 文档攻略 ---------------
        tb.addSeparator()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # ---- 📦 导出为独立 EXE（放在文档攻略左侧） ----
        btn_export = QPushButton("📦 导出 EXE")
        btn_export.setCursor(Qt.PointingHandCursor)
        btn_export.setStyleSheet(
            "QPushButton { background:#238636; color:#FFFFFF; border:1px solid #1A7F37; border-radius:4px; padding:3px 12px; font-weight:600; }"
            "QPushButton:hover { background:#1A7F37; border-color:#3FB950; }"
            "QPushButton:pressed { background:#146C2E; }"
        )
        btn_export.setToolTip("把当前蓝图打包成一个可脱离本项目、单独双击运行的 EXE（自动带上蓝图引用的图片资源）")
        btn_export.clicked.connect(self._on_export_clicked)
        tb.addWidget(btn_export)
        # 视觉分隔：和文档攻略之间留点小空隙
        sep_exp = QLabel("  ")
        sep_exp.setStyleSheet("color:#30363D;")
        tb.addWidget(sep_exp)

        # ---- 📚 文档攻略按钮 ----
        btn_doc = QPushButton("📚 文档攻略")
        btn_doc.setCursor(Qt.PointingHandCursor)
        btn_doc.setStyleSheet(
            "QPushButton { background:#B62324; color:#FFFFFF; border:1px solid #8B1B1C; border-radius:4px; padding:3px 12px; font-weight:600; }"
            "QPushButton:hover { background:#8B1B1C; border-color:#FF7B72; }"
            "QPushButton:pressed { background:#6E1516; }"
        )
        btn_doc.setToolTip("打开蓝图自动化使用手册（软件怎么用、功能说明、所有节点的属性与参数详解）")
        btn_doc.clicked.connect(self._on_doc_guide_clicked)
        tb.addWidget(btn_doc)

        # 【构建完菜单后】先填一次"最近工程"子菜单内容
        self._refresh_recent_projects_menu()

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
    # ==================== 子图导航（循环体子画布 / 组件子图） ====================
    def _build_breadcrumb_bar(self):
        """顶部面包屑导航条：进入子图时显示「返回」按钮 + 当前层级路径。"""
        bar = QWidget()
        bar.setStyleSheet(
            "background: #161B22; border-bottom: 1px solid #30363D;"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)
        btn_back = QPushButton("← 返回父图")
        btn_back.setStyleSheet(
            "QPushButton { background: #21262D; color: #58A6FF; border: 1px solid #30363D;"
            " border-radius: 4px; padding: 3px 10px; font-size: 11px; }"
            "QPushButton:hover { background: #30363D; }"
        )
        btn_back.clicked.connect(self._exit_subgraph)
        lay.addWidget(btn_back)
        self._breadcrumb_label = QLabel("")
        self._breadcrumb_label.setStyleSheet("color: #C9D1D9; font-size: 11px;")
        lay.addWidget(self._breadcrumb_label, 1)
        return bar

    def _refresh_breadcrumb(self):
        """刷新面包屑路径文案与可见性。"""
        if self._breadcrumb_bar is None:
            return
        if not self._subgraph_stack:
            self._breadcrumb_bar.setVisible(False)
            return
        # 从栈底到栈顶拼接路径
        parts = []
        for item in self._subgraph_stack:
            node = None
            pg = item.get("parent_graph")
            nid = item.get("node_id", "")
            if pg is not None:
                node = pg.nodes.get(nid)
            title = (node.type if node else "子图")
            kind = item.get("kind")
            kind_label = {"loop": "循环体", "seq": "顺序体", "parallel": "并行体"}.get(kind, "")
            label = f"{title} {kind_label}" if kind_label else title
            parts.append(label)
        self._breadcrumb_label.setText("  ▸  ".join(parts))
        self._breadcrumb_bar.setVisible(True)

    def _on_node_double_clicked(self, node_id: str):
        """双击节点：Loop → 循环体子画布；Sequence → 顺序体子画布；Parallel → 并行体子画布。"""
        if self._graph is None or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]
        # 三种子画布容器节点：节点类型 -> (回写参数键名, 子画布 kind)
        if node.type in self._CONTAINER_BODY_KEYS:
            body_key, kind = self._CONTAINER_BODY_KEYS[node.type]
            body = node.params.get(body_key)
            if not (isinstance(body, dict) and body.get("nodes")):
                # 还没初始化过 → 先在内存里造一份空子画布（刻意不写进 params，见 _create_empty_subgraph）
                body = self._create_empty_subgraph(node, body_key, kind)
            self._enter_subgraph(node, body_key, kind, body)
            return
        # 组件节点：双击 → 跳到它调用的那张组件蓝图（跳转前问是否保存当前修改）
        if node.type == "Component":
            self._jump_to_component_blueprint(node)
            return

    def _jump_to_component_blueprint(self, node):
        """双击 Component 节点 → 打开它引用的组件蓝图（相对工程根路径）。"""
        ref = str((node.params or {}).get("blueprint_path", "") or "").strip()
        if not ref:
            QMessageBox.information(
                self, "未选择组件",
                "这个组件节点还没有选择要调用的蓝图。\n\n请在右侧属性面板的「组件蓝图」下拉里选一张组件蓝图；"
                "若还没有组件，可在左侧「🗂 工程资源」面板点「＋组件」新建（放哪个目录都行），"
                "或右键任意蓝图 → 🧩 设为组件蓝图。",
            )
            return
        self._open_component_ref(ref, node=node)

    def _on_inspector_open_component(self, ref: str):
        """属性面板「↗ 打开这个组件」按钮 → 跳转到组件蓝图。"""
        self._open_component_ref(str(ref or "").strip(), node=None)

    def _on_scene_open_component(self, node_id: str):
        """画布上组件节点右键「🧩 打开引用的组件」。"""
        if self._graph is None or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]
        self._jump_to_component_blueprint(node)

    def _open_component_ref(self, ref: str, node=None):
        """公共入口：把「组件相对路径」解析成绝对路径并跳转过去编辑。"""
        if not ref:
            return
        root = self._pm.project_root if (self._pm and self._pm.is_open()) else None
        abs_path = os.path.normpath(os.path.join(root, ref)) if (root and not os.path.isabs(ref)) else os.path.normpath(ref)
        if not os.path.isfile(abs_path):
            r = QMessageBox.warning(
                self, "组件不存在",
                f"引用的组件文件不存在：\n\n  {ref}\n（解析为：{abs_path}）\n\n"
                f"可能已被删除或重命名。是否清空该节点的组件引用？"
                if node is not None else
                f"引用的组件文件不存在：\n\n  {ref}\n（解析为：{abs_path}）\n\n可能已被删除或重命名。",
                (QMessageBox.Yes | QMessageBox.No) if node is not None else QMessageBox.Ok,
                QMessageBox.No if node is not None else QMessageBox.Ok,
            )
            if node is not None and r == QMessageBox.Yes:
                # 走统一参数修改入口（进撤销栈 + 刷新画布/属性面板）
                try:
                    self._on_param_changed(node.id, "blueprint_path", "")
                except Exception:
                    node.params["blueprint_path"] = ""
                    self._scene.update()
            return
        # 跳到组件前先处理未保存修改（与切换蓝图一致）
        if not self._confirm_discard():
            return
        self._open_blueprint_path(abs_path)
        self._log.append_log(f"[Component] 已跳转到组件蓝图：{os.path.basename(abs_path)}", "SUCCESS")

    # ---------------- 子画布 body 的「统一口径」（读 / 写 / 算哈希三处必须完全一致）----------------
    @staticmethod
    def _sub_canonical(sub) -> Optional[dict]:
        """把子画布图对象规范化成「只含 nodes + links」的纯 dict。

        为什么不能直接用 `sub.to_dict()`：
          ① to_dict 会带上 bp_type / breakpoints / metadata(name, created_at…)，而 metadata.created_at
             在构造 BlueprintGraph 时被填成「当前时间」→「进子画布看一眼再退出」就会把 body 改一次，
             文档哈希随之变化 → 标题栏误报未保存 *，而且每进出一次文件内容就变一次。
          ② links 顺序是按边 id 排的，而边 id 在加载时由 hash() 生成、跨进程不稳定 →
             同一个 body 在不同次启动下顺序不同 → 又是假脏。
        所以统一口径：只留 nodes + links，且 links 按「端口四元组」稳定排序。
        """
        if sub is None:
            return None
        try:
            d = sub.to_dict()
        except Exception:
            return None
        links = d.get("links")
        if not isinstance(links, list):
            links = []

        def _k(l):
            if not isinstance(l, dict):
                return ("", "", "", "")
            return (
                str(l.get("from_node", "")), str(l.get("from_port", "")),
                str(l.get("to_node", "")), str(l.get("to_port", "")),
            )

        return {
            "nodes": d.get("nodes") if isinstance(d.get("nodes"), dict) else {},
            "links": sorted(links, key=_k),
        }

    @classmethod
    def _sub_hash(cls, sub) -> Optional[str]:
        """子画布内容的指纹（用统一口径算，跨进程稳定）。"""
        d = cls._sub_canonical(sub)
        if d is None:
            return None
        try:
            canonical = json.dumps(d, sort_keys=True, ensure_ascii=False, indent=2)
            return hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _sub_changed(self, item: dict, sub) -> bool:
        """相对「进入子画布那一刻」有没有真实改动。算不出指纹时保守返回 True。"""
        if sub is None:
            return True
        cur = self._sub_hash(sub)
        if cur is None:
            return True
        return cur != item.get("entry_hash")

    def _create_empty_subgraph(self, host_node, body_key: str, kind: str) -> dict:
        """构造一个空的子图 body（只含一个起始节点）并返回，**不写进 host_node.params**。

        起始节点按容器类型区分：并行体用「分支入口 Branch」（运行时只认 Branch，
        开始节点在并行体里不参与执行），循环体/顺序体用「开始 Start」。

        **不预置「结束 End」节点**：三个容器都不要求接「结束」（执行流跑到最后一个节点、
        或者末尾出口悬空，都算正常收尾），预置了反而让用户以为非接不可。

        ⚠ 只「造」不「写」是刻意的：双击进去看一眼不算修改。body 只有在用户真的在
        子画布里有改动时，才会在「退出子画布 / 保存」时写回宿主节点的 params。
        """
        from src.blueprint.core.node_library import create_node as _cn
        start_type = "Branch" if kind == "parallel" else "Start"
        start = _cn(start_type, pos=(0, 0), node_id="start")
        body = {
            "nodes": {start.id: start.to_dict()},
            "links": [],
        }
        title = {"loop": "循环", "seq": "顺序执行", "parallel": "并行执行"}.get(kind, "子画布")
        self._log.append_log(
            f"「{title}」节点的子画布还是空的，已自动放入一个"
            f"「{'分支入口' if kind == 'parallel' else '开始'}」节点。"
            f"在里面编辑后退出/保存才会写回蓝图。",
            "INFO",
        )
        return body

    # 三种子画布容器节点：节点类型 → (回写参数键名, 子画布 kind)
    _CONTAINER_BODY_KEYS = {
        "Loop": ("loop_body", "loop"),
        "Sequence": ("seq_body", "seq"),
        "Parallel": ("par_body", "parallel"),
    }

    # 子画布专用上下文节点：在哪一层的容器里，左侧节点库就额外给出对应节点
    _CONTEXT_NODES = {
        "loop": ["Continue", "Break"],          # 再次循环 / 跳出循环
        "seq": ["ExitSequence"],                # 退出串行
        "parallel": ["Branch", "ExitParallel"], # 分支入口 / 退出并行
    }

    def _apply_context_nodes(self, kind):
        """按「当前所在层的容器类型」刷新左侧节点库的上下文节点。

        loop → Continue/Break；seq → ExitSequence；parallel → Branch/ExitParallel；
        其余（根图）→ 无。

        ⚠ 退出子画布时也必须按「新的当前层」重新套用 —— 否则
        「进循环体（有 Continue/Break）→ 再进并行体 → 退回循环体」时，
        这几个节点会被上一次的 set_context_nodes([]) 清掉，看起来就是「节点不见了」。
        """
        palette = getattr(self, "_palette", None)
        if palette is None:
            return
        try:
            palette.set_context_nodes(list(self._CONTEXT_NODES.get(kind, [])))
        except Exception:
            pass

    def _enter_subgraph(self, host_node, body_key: str, kind: str, body=None, quiet: bool = False):
        """进入子画布容器节点（Loop/Sequence/Parallel）的子画布：父图压栈，切换渲染。

        quiet=True 用于「运行时自动跟随」：照常切换画布，但不往日志面板刷「进入子画布」这类噪音。
        """
        from src.blueprint.core.models import BlueprintGraph
        if not isinstance(body, dict):
            body = host_node.params.get(body_key)
        if not (isinstance(body, dict) and body.get("nodes")):
            body = self._create_empty_subgraph(host_node, body_key, kind)

        # ---- 进画布前的体检 ----
        # 历史 bug 的形态：中途保存会把「父图内容」误写进 loop_body，再进循环体看到的就是整张父图；
        # 若父图里又有本容器节点，就变成自我嵌套、每保存一次膨胀一层。
        # ⚠ 注意：节点 id 是「每张图各自编号」的，"1" 在不同层级重复出现是完全正常的
        #   （父图有 1，子画布里第一个节点也叫 1）。所以下面【只告警、绝不改内容】——
        #   宁可提示一句，也绝不能因为误判把用户辛苦搭好的子画布清空。
        _MAX_SUBGRAPH_DEPTH = 32
        if len(self._subgraph_stack) >= _MAX_SUBGRAPH_DEPTH:
            self._log.append_log(
                f"[子画布] 嵌套层级已达上限 {_MAX_SUBGRAPH_DEPTH} 层，为防递归失控已阻止继续进入。"
                f"请检查是不是哪里套得太深。",
                "ERROR",
            )
            return
        bnodes = body.get("nodes") or {}
        parent = self._graph
        # 体检只在「用户手动进来」时提示：运行时自动跟随会反复进出同一层，
        # 每次都刷一遍 WARN 会把运行日志冲掉（要看去手动双击一次即可）。
        if (not quiet) and parent is not None and bnodes:
            # 泄漏判据必须够严：只看「id 集合相同」会误报 —— 两张小图恰好都只有 start 和 1 是很常见的。
            # 所以要求「id 完全一致 且 每个 id 的节点类型也一模一样」，才认定是「父图被整体写进子画布」。
            _bid = set(bnodes.keys())
            _pid = set(parent.nodes.keys())
            if _bid == _pid:
                _types_match = True
                for _k in _bid:
                    _bnd = bnodes.get(_k) or {}
                    _pnd = parent.nodes.get(_k)
                    _ptype = getattr(_pnd, "type", None)
                    try:
                        from src.blueprint.core.node_library import NODE_LIBRARY as _NL2
                        _pshort = getattr(_NL2.get(_ptype), "type_short_name", _ptype)
                    except Exception:
                        _pshort = _ptype
                    if str(_bnd.get("type", "")) != str(_pshort):
                        _types_match = False
                        break
                if _types_match:
                    self._log.append_log(
                        f"[子画布] 「{host_node.type} {host_node.id}」的 {body_key} 内容和父图一模一样"
                        f"（id 与类型全部对上），疑似父图被整体误写进了子画布（历史 bug 形态）。"
                        f"请先确认内容是否正确，再决定是否保存。",
                        "WARN",
                    )
            _nested = bnodes.get(host_node.id)
            if isinstance(_nested, dict):
                try:
                    from src.blueprint.core.node_library import NODE_LIBRARY as _NL
                    _short = getattr(_NL.get(host_node.type), "type_short_name", host_node.type)
                except Exception:
                    _short = host_node.type
                if str(_nested.get("type", "")) == str(_short):
                    self._log.append_log(
                        f"[子画布] 「{host_node.type} {host_node.id}」的 {body_key} 里出现了同类型同 id 的容器节点，"
                        f"疑似自我嵌套（越保存越大）。如果这是有意套的可以忽略本条，否则建议删掉多余层级。",
                        "WARN",
                    )

        # 读取口径与写回口径一致：只喂 nodes + links，不注入 metadata
        # （否则子图会带上「构造时刻」的 created_at，一往返就变）
        sub = BlueprintGraph.from_dict({
            "nodes": body.get("nodes", {}),
            "links": body.get("links", []),
        })
        sub._host_node_id = host_node.id
        self._subgraph_stack.append({
            "kind": kind,            # "loop" | "seq" | "parallel"
            "body_key": body_key,    # 回写参数键名（loop_body / seq_body / par_body）
            "parent_graph": self._graph,
            "node_id": host_node.id,
            "sub_graph": sub,        # 本层子图对象：退出/回写时以此为准，不能依赖 self._graph
            "entry_hash": self._sub_hash(sub),  # 进入时的内容指纹：判断「有没有真改过」
        })
        self._scene.set_graph(sub)
        self._graph = sub
        self._inspector.set_node(None)
        # 上下文节点：循环体注入 Continue/Break；顺序体注入 ExitSequence；并行体注入 Branch/ExitParallel
        self._apply_context_nodes(kind)
        self._refresh_breadcrumb()
        if not quiet:
            title = {"loop": "循环", "seq": "顺序执行", "parallel": "并行执行"}.get(kind, "子画布")
            self._log.append_log(
                f"进入「{title}」节点 {host_node.id[:8]} 的子画布。双击节点内容可编辑；点「← 返回父图」退出。",
                "INFO",
            )

    def _exit_subgraph(self, quiet: bool = False):
        """退出当前子画布，返回父图。

        quiet=True 用于「运行时自动跟随」：照常回写/切换画布，但不往日志面板刷噪音。
        """
        if not self._subgraph_stack:
            return
        item = self._subgraph_stack.pop()
        parent_graph = item.get("parent_graph")
        if parent_graph is None:
            return
        # 回写子图内容到宿主节点：必须用栈里记录的本层子图对象，不能用 self._graph。
        # 否则一旦中途保存过（保存时 self._graph 可能被临时指向父图），
        # 这里就会把「父图内容」写进 loop_body —— 再进循环体看到的就变成整张父图（历史 bug）。
        # 另外：只有「相对进入时真的改过」才写回 —— 双击进去看一眼不该把蓝图变脏。
        body_key = item.get("body_key")
        sub_graph = item.get("sub_graph") or self._graph
        if body_key and sub_graph is not None:
            node_id = item.get("node_id")
            host = parent_graph.nodes.get(node_id)
            if host is not None and self._sub_changed(item, sub_graph):
                host.params[body_key] = self._sub_canonical(sub_graph)
            if host is not None:
                # 无论本次写没写，都以当前内容刷新指纹：
                # 未改 → 指纹不变；已改 → 写回后继续按新内容比对，避免反复进出造成假脏。
                item["entry_hash"] = self._sub_hash(sub_graph)
        self._scene.set_graph(parent_graph)
        self._graph = parent_graph
        self._inspector.set_node(None)
        # ★ 必须按「退回去之后所在的层」重新套用上下文节点，不能无条件清空：
        #   循环体里再进并行体时，退回循环体后 Continue/Break 要重新出现（否则看起来「节点不见了」）。
        self._apply_context_nodes(
            self._subgraph_stack[-1].get("kind") if self._subgraph_stack else None
        )
        self._refresh_breadcrumb()
        self._update_title()  # 退出后刷新标题栏的「未保存 *」（未改动时不该出现星号）
        if not quiet:
            self._log.append_log("已返回父图。", "INFO")

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
        self._scene.node_double_clicked.connect(self._on_node_double_clicked)
        self._scene.node_find_in_log.connect(self._on_node_find_in_log)
        self._scene.node_open_component.connect(self._on_scene_open_component)
        self._scene.asset_dropped.connect(self._on_asset_dropped)
        self._inspector.param_changed.connect(self._on_param_changed)
        self._inspector.request_pick_template.connect(self._on_pick_template)
        self._inspector.request_snip_for_node.connect(self._on_snip_for_node)
        self._inspector.request_pick_coord.connect(self._on_pick_coord)
        self._inspector.request_open_component.connect(self._on_inspector_open_component)
        self._inspector.component_chosen.connect(self._on_component_chosen)
        self._palette.info_message.connect(lambda m, l: self._log.append_log(m, l))
        # 📋 日志面板：双击日志行 → 定位到画布节点（日志 ↔ 节点 双向定位）
        self._log.node_activated.connect(self._on_log_line_activated)
        # 🗂 工程资源面板：所有文件写操作都回调到主窗口执行（面板只发意图）
        self._assets.open_blueprint.connect(self._on_asset_open_blueprint)
        self._assets.request_new.connect(self._on_asset_new)
        self._assets.request_rename.connect(self._on_asset_rename)
        self._assets.request_delete.connect(self._on_asset_delete)
        self._assets.request_set_bp_type.connect(self._on_asset_set_bp_type)
        self._assets.request_cleanup_assets.connect(self._on_asset_cleanup_assets)
        self._assets.save_current.connect(self._on_save)
        self._assets.save_as_component.connect(self._on_save_as_component)
        self._assets.open_from_disk.connect(self._on_open)
        self._assets.reveal_path.connect(self._on_asset_reveal)
        self._assets.request_reverse_deps.connect(self._on_asset_reverse_deps)
        # 保险：任何「工程打开」事件都强制刷新资源树（即便某条开工程路径忘了调 _refresh_assets，
        # 也能靠信号兜底，避免出现「开了工程但面板空白」）。
        try:
            self._pm.project_opened.connect(lambda *_a: self._refresh_assets())
        except Exception:
            pass
        # 注意：全局热键已由 GlobalHotkeyManager.set_callback 直接绑定到主线程 QTimer.singleShot，
        # 无需额外 bridge.connect

    # ==================== 基础动作 ====================
    def _bind_new_graph(self, graph: BlueprintGraph, zoom_fit: bool = True):
        """把新蓝图 graph 绑定到画布和所有关联面板，统一清理残留状态。
        作用：避免切换蓝图后 Inspector 仍显示旧节点参数、画布选中高亮残留等。
        """
        # 0) 停止正在运行的 runner（防止旧 runner 继续执行旧图、高亮串到新图节点）
        self._stop_and_detach_runner()
        # 0.4) 丢掉上一张蓝图遗留的「设置变量」端口对齐任务（节点 id 是每张图各自编号的，
        #      留着可能在切换后打到新蓝图里恰好同 id 的节点上）
        try:
            self._setvar_port_pending.clear()
            self._setvar_err_reported.clear()
            self._setvar_port_timer.stop()
        except Exception:
            pass
        # 0.5) 重置子图导航栈 + palette 上下文（切换蓝图后不再停留在旧的子画布）
        self._subgraph_stack.clear()
        self._apply_context_nodes(None)  # 回到根图：左侧不再有子画布专用节点
        if self._breadcrumb_bar is not None:
            try:
                self._breadcrumb_bar.setVisible(False)
            except Exception:
                pass
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
        #    优先恢复「这张蓝图上次的缩放/位置」（离上次编辑不远，接着看很顺手）；
        #    没有记录时再按 zoom_fit 参数走默认行为。
        _restored = False
        try:
            if self._current_path:
                _restored = self._restore_view_state(self._current_path)
        except Exception:
            _restored = False
        if (not _restored) and zoom_fit and self._view is not None:
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
        # 5) 左侧资源树：高亮当前蓝图（打开/新建/启动装载都会走到这里）
        try:
            if getattr(self, "_assets", None) is not None:
                self._assets.set_current_path(self._current_path)
        except Exception:
            pass

    # ==================== 编辑器视图记忆（下次启动接着看） ====================
    @staticmethod
    def _editor_prefs_path() -> str:
        try:
            from src.config.app_paths import get_app_dir
            return os.path.join(get_app_dir(), "editor_prefs.json")
        except Exception:
            return ""

    def _read_editor_prefs(self) -> dict:
        p = self._editor_prefs_path()
        if not p or not os.path.isfile(p):
            return {}
        try:
            d = json.load(open(p, encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _persist_view_state(self):
        """关闭窗口时记录：视图缩放/中心（按蓝图分别记），供下次打开接着看。"""
        p = self._editor_prefs_path()
        if not p:
            return
        data = self._read_editor_prefs()
        try:
            view = self._view
            if view is not None and self._current_path:
                t = view.transform()
                c = view.mapToScene(view.viewport().rect().center())
                key = "view::" + os.path.normcase(os.path.abspath(self._current_path))
                data[key] = {"scale": float(t.m11()), "cx": float(c.x()), "cy": float(c.y())}
        except Exception:
            pass
        # 裁剪：只保留最近 20 个视图记录，防止文件无限膨胀
        try:
            vkeys = [k for k in data if k.startswith("view::")]
            if len(vkeys) > 20:
                for k in vkeys[:-20]:
                    data.pop(k, None)
        except Exception:
            pass
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _restore_view_state(self, path: str) -> bool:
        """恢复某张蓝图上次的视图缩放/中心。没有记录或恢复失败返回 False。"""
        data = self._read_editor_prefs()
        if not data:
            return False
        key = "view::" + os.path.normcase(os.path.abspath(path))
        rec = data.get(key)
        if not isinstance(rec, dict):
            return False
        try:
            view = self._view
            if view is None:
                return False
            scale = float(rec.get("scale", 1.0) or 1.0)
            if scale <= 0.01 or scale > 20:
                scale = 1.0
            view.resetTransform()
            view.scale(scale, scale)
            view.centerOn(QPointF(float(rec.get("cx", 0.0)), float(rec.get("cy", 0.0))))
            return True
        except Exception:
            return False

    def _new_graph(self):
        g = BlueprintStore.new_graph("Untitled")
        self._current_path = None
        self._last_saved_graph_hash = None  # 新建=从未保存过，hash置空
        self._bind_new_graph(g, zoom_fit=False)  # 新空图不用缩放
        self._log.append_log("新建空白蓝图（未保存）。", "SUCCESS")
        self._update_title()
        self._refresh_assets()

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
        self._log_component_identity(path)
        self._update_title()
        self._refresh_assets()

    def _log_component_identity(self, path: str):
        """打开的若是「组件蓝图」，在日志里点明身份 + 当前被多少处引用。

        组件蓝图与普通蓝图在编辑器里外观完全相同（标题栏 [组件] 之外没有其它线索），
        而「改一张组件 = 同时改所有引用它的蓝图」，所以打开时就要说清楚。
        """
        try:
            if not bp_kind_mod.is_component(path, self._proj_root()):
                return
        except Exception:
            return
        n_ref = 0
        detail = ""
        try:
            refs = self._collect_component_refs(path)
            n_ref = len(refs)
            if refs:
                shown = "、".join(f"{os.path.basename(r['bp'])} · {r['where']}" for r in refs[:5])
                detail = f"（{shown}{' 等' if n_ref > 5 else ''}）"
            else:
                detail = "（当前没有任何「调用组件」节点引用它）"
        except Exception:
            pass
        self._log.append_log(
            "🧩 这是一张「组件蓝图」：改它会同时影响所有引用它的蓝图。"
            f"当前工程里有 {n_ref} 处引用{detail}。"
            "查看全部引用：资源树里右键本文件 → 🔗 谁引用了它",
            "INFO",
        )
        # 工程下的蓝图列表可能因为"最近修改时间"重排，下次打开工程下拉时自动刷新（aboutToShow 已经连了刷新）

    def _save_target_graph(self):
        """「保存」应写入磁盘的那张图：处于子画布时是根图（先把子图回写进宿主），否则是当前图。

        ⚠ 绝不改动导航状态（self._graph / scene 仍停留在子画布）：
        否则退出子画布时会把父图内容误写回 loop_body（历史 bug：再进循环体看到整张父图）。
        """
        if self._subgraph_stack:
            self._flush_subgraph_to_host()
            root = self._subgraph_stack[0].get("parent_graph")
            if root is not None:
                return root
        return self._graph

    def _on_save(self):
        if self._graph is None:
            return
        # 处于子画布（循环体/顺序体/并行体）时，先把子图回写进宿主节点，再保存「根图」；
        # 但不改动当前导航状态（画布仍停在子画布，可继续编辑）。
        target = self._save_target_graph()
        if target is None:
            return
        if not self._current_path:
            self._on_save_as()
            return
        ok = BlueprintStore.save(target, self._current_path)
        if ok:
            self._mark_graph_clean()  # 保存成功，哈希更新=干净了
            try:
                self._pm.set_last_opened_blueprint(self._current_path)
            except Exception:
                pass
            self._log.append_log(f"已保存: {self._current_path}", "SUCCESS")
            self._update_title()
            self._refresh_assets()
        else:
            QMessageBox.warning(self, "保存失败", "写入文件失败，请检查路径与权限。")

    def _flush_subgraph_to_host(self):
        """把栈里**每一层**子画布的内容逐层回写到各自宿主节点的 params（loop_body/seq_body/par_body），
        不改变导航栈、不切画布。

        必须从最深一层往浅层回写：深层的宿主节点属于「上一层子图对象」，只有先写深层，
        上一层回写时才会带上最新内容。多层嵌套（如 循环体 → 并行体）时只回写一层，
        会让更外层拿到旧内容 → 存盘后重开丢内层修改，标题栏还会一直显示未保存 *。

        只有「相对进入时真的改过」才写（查看 ≠ 修改）。写回用统一口径（只含 nodes+links），
        并刷新指纹，保证「在子画布里按保存」不会把文档搞成假脏。
        """
        if not self._subgraph_stack:
            return
        depth = len(self._subgraph_stack)
        for idx in range(depth - 1, -1, -1):
            item = self._subgraph_stack[idx]
            body_key = item.get("body_key")
            if not body_key:
                continue
            parent_graph = item.get("parent_graph")
            node_id = item.get("node_id")
            if parent_graph is None or node_id is None:
                continue
            sub_graph = item.get("sub_graph")
            if sub_graph is None and idx == depth - 1:
                sub_graph = self._graph  # 最顶层退而取当前图（正常情况下 sub_graph 一定有值）
            if sub_graph is None:
                continue
            host = parent_graph.nodes.get(node_id)
            if host is not None:
                if self._sub_changed(item, sub_graph):
                    host.params[body_key] = self._sub_canonical(sub_graph)
                item["entry_hash"] = self._sub_hash(sub_graph)

    def _on_save_as(self):
        if self._graph is None:
            return
        # 处于子画布时，「另存为」保存的仍是整张根图（先回写子图内容），与 _on_save 口径一致
        target = self._save_target_graph()
        if target is None:
            return
        default_dir = self._pm.project_root or os.getcwd()
        default = self._current_path or BlueprintStore.default_path(target, default_dir)
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
        if stem and stem != getattr(target, "name", None):
            target.name = stem
        ok = BlueprintStore.save(target, path)
        if ok:
            self._current_path = path
            self._mark_graph_clean()  # 另存成功，哈希更新=干净了
            try:
                self._pm.set_last_opened_blueprint(path)
            except Exception:
                pass
            self._log.append_log(f"另存为: {path}", "SUCCESS")
            self._update_title()
            self._refresh_assets()
        else:
            QMessageBox.warning(self, "保存失败", "写入文件失败。")

    # ==================== 蓝图 / 组件 创建（统一流程） ====================
    def _default_component_dir(self) -> str:
        """「另存为组件」的默认落点：当前蓝图所在目录（没有则工程根）。

        ⚠ 组件不再强制放在 components/ —— 放哪儿由用户在资源面板决定，
          「是不是组件」写在蓝图自己的 bp_type 里，与目录无关。
        """
        root = (self._pm.project_root if (self._pm and self._pm.is_open()) else "") or ""
        cur = self._current_path
        if cur:
            try:
                d = os.path.dirname(os.path.abspath(cur))
                if d and os.path.isdir(d):
                    return d
            except Exception:
                pass
        return root

    @staticmethod
    def _sanitize_stem(raw: str) -> str:
        """清洗用户输入的文件名主干：去空白、去 .bp.json 后缀、替换 Windows 非法字符。"""
        s = (raw or "").strip()
        if s.lower().endswith(BlueprintStore.EXT.lower()):
            s = s[: -len(BlueprintStore.EXT)]
        invalid = '<>:"/\\|?*'
        return "".join("_" if ch in invalid else ch for ch in s).strip()

    def _ask_blueprint_stem(self, target_dir: str, *, title: str, prompt: str,
                            default_stem: str) -> Optional[str]:
        """循环询问文件名，返回最终 stem（用户取消返回 None）。
        重名覆盖确认统一在这里处理，保证「新建蓝图/新建组件/另存为组件」三条路径行为一致。"""
        while True:
            user_stem, ok = QInputDialog.getText(self, title, prompt, text=default_stem)
            if not ok:
                return None
            stem = self._sanitize_stem(user_stem)
            if not stem:
                QMessageBox.warning(self, "名称非法", "名称不能为空，且不能只包含特殊字符。")
                continue
            final_path = os.path.join(target_dir, stem + BlueprintStore.EXT)
            if os.path.exists(final_path):
                r = QMessageBox.question(
                    self, "名称已存在",
                    f"目标目录下已存在同名文件：\n{final_path}\n\n是否覆盖？（覆盖后原文件会进备份，不会永久丢失。）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    default_stem = stem
                    continue
            return stem

    def _suggest_stem(self, target_dir: str, base: str) -> str:
        """给出不重名的建议名：base / base 1 / base 2 ..."""
        i = 0
        while True:
            stem = base if i == 0 else f"{base} {i}"
            if not os.path.exists(os.path.join(target_dir, stem + BlueprintStore.EXT)):
                return stem
            i += 1

    def _create_blueprint_at(self, target_dir: Optional[str], *, as_component: bool,
                             use_current_graph: bool = False):
        """统一创建入口（新建蓝图 / 新建组件 / 另存为组件 共用）：
          - as_component=False          新建空白蓝图
          - as_component=True           新建空白组件（落到 components/）
          - use_current_graph=True      把当前蓝图另存一份到目标目录（一键转组件）
        """
        if not target_dir:
            return
        if not self._ensure_project_open("新建组件" if as_component else "新建蓝图"):
            return
        if not use_current_graph and not self._confirm_discard():
            return
        os.makedirs(target_dir, exist_ok=True)
        kind_cn = "组件" if as_component else "蓝图"
        src_graph = None
        if use_current_graph:
            if self._graph is None:
                return
            # 正在子画布编辑 → 先回写宿主，另存的是完整根图；但不改动当前导航状态
            src_graph = self._save_target_graph()
            if src_graph is None:
                return
            default_stem = getattr(src_graph, "name", None) or f"新{kind_cn}"
        else:
            default_stem = self._suggest_stem(target_dir, f"新{kind_cn}")
        stem = self._ask_blueprint_stem(
            target_dir,
            title=(f"另存为{kind_cn}" if use_current_graph else f"新建{kind_cn}"),
            prompt=f"请输入{kind_cn}名称（保存到 {target_dir}，无需写后缀）：",
            default_stem=default_stem,
        )
        if stem is None:
            return
        final_path = os.path.join(target_dir, stem + BlueprintStore.EXT)
        want_type = bp_kind_mod.BP_TYPE_COMPONENT if as_component else bp_kind_mod.BP_TYPE_BLUEPRINT
        if use_current_graph:
            src_graph.name = stem      # 文件名同步为蓝图名，重开标题栏显示正确
            src_graph.bp_type = want_type
            g = src_graph
            need_bind = False
        else:
            g = BlueprintStore.new_graph(stem, bp_type=want_type)
            g.name = stem
            need_bind = True
        ok = BlueprintStore.save(g, final_path)
        if not ok:
            QMessageBox.warning(self, f"创建{kind_cn}失败", f"无法写入文件，请检查目录权限：\n{final_path}")
            if need_bind:                # 落盘失败也把空白图绑到画布，用户不至于白等
                self._current_path = None
                self._last_saved_graph_hash = None
                self._bind_new_graph(g, zoom_fit=False)
                self._update_title()
            return
        self._current_path = final_path
        if need_bind:
            self._bind_new_graph(g, zoom_fit=False)
        self._mark_graph_clean(g)
        try:
            self._pm.set_last_opened_blueprint(final_path)
        except Exception:
            pass
        verb = "已另存为" if use_current_graph else "已创建"
        suffix = "（Component 节点下拉列表可直接选用）" if as_component else ""
        self._log.append_log(f"✅ {verb}{kind_cn}：{final_path}{suffix}", "SUCCESS")
        self._update_title()
        self._refresh_assets(reveal_path=final_path)

    def _on_new(self):
        """新建空白蓝图（落在工程根目录）。"""
        if not self._ensure_project_open("新建蓝图"):
            return
        self._create_blueprint_at(self._pm.project_root, as_component=False)

    def _on_save_as_component(self):
        """把当前蓝图另存一份（默认落在当前蓝图所在目录）并标记为组件蓝图。

        「组件」是蓝图自己的类型元数据，跟放在哪个目录无关；
        想让某个目录里的蓝图变成组件，直接在资源树里右键「设为组件蓝图」即可。
        """
        if not self._ensure_project_open("另存为组件"):
            return
        self._create_blueprint_at(self._default_component_dir(), as_component=True,
                                  use_current_graph=True)

    # ==================== 🗂 资源面板回调（面板只发意图，写文件在这里） ====================
    def _refresh_assets(self, reveal_path: Optional[str] = None):
        """刷新资源树 + 当前蓝图高亮（新建/保存/删除/切工程后调用）。

        reveal_path: 新建/保存后让该文件自动展开并选中（避免落在子目录里被折叠藏住）。
        """
        try:
            if getattr(self, "_assets", None) is None:
                return
            pm = self._pm
            root = pm.project_root if (pm and pm.is_open()) else None
            name = pm.project_name if pm else ""
            # 只有「工程根/名真变了」才走 set_project（它会重置展开状态）；
            # 普通的保存/新建只 refresh()，保留用户已展开的目录，避免每次都折叠回只剩根。
            if root == self._assets._last_root and name == self._assets._last_name:
                self._assets.refresh()
            else:
                self._assets.set_project(root, name)
            self._assets.set_current_path(self._current_path)
            if reveal_path:
                self._assets.reveal(reveal_path)
        except Exception as e:
            try:
                self._log.append_log(f"[Assets] 刷新资源树失败: {e}", "WARN")
            except Exception:
                pass

    def _on_asset_open_blueprint(self, path: str):
        """资源面板双击/右键「打开蓝图」。"""
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "打开失败", f"文件不存在（可能已被外部删除）：\n{path}")
            self._refresh_assets()
            return
        if self._current_path:
            try:
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self._current_path)):
                    self._log.append_log(f"该蓝图已是当前蓝图：{os.path.basename(path)}", "INFO")
                    return
            except Exception:
                pass
        if not self._confirm_discard():
            return
        self._open_blueprint_path(path)
        # reveal_path=当前蓝图：自动展开其所在目录并选中，双击打开后在资源树里定位到它
        self._refresh_assets(reveal_path=self._current_path)

    def _on_asset_new(self, kind: str, target_dir: str):
        """资源面板「新建蓝图/组件/文件夹」：落在面板当前选中的目录下（不再固定 components/）。"""
        if kind == "folder":
            self._on_asset_new_folder(target_dir)
            return
        if not self._ensure_project_open("新建资源"):
            return
        as_component = (kind == "component")
        if not target_dir or not os.path.isdir(target_dir):
            target_dir = self._pm.project_root
        self._create_blueprint_at(target_dir, as_component=as_component)

    def _on_asset_new_folder(self, parent_dir: str):
        if not self._ensure_project_open("新建文件夹"):
            return
        if not parent_dir or not os.path.isdir(parent_dir):
            QMessageBox.warning(self, "无法创建", "目标目录不存在，请先刷新资源树。")
            return
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称：", text="新建文件夹")
        if not ok:
            return
        name = self._sanitize_stem(name)   # 复用清洗逻辑（去掉路径非法字符）
        if not name:
            QMessageBox.warning(self, "名称非法", "文件夹名称不能为空。")
            return
        new_dir = os.path.join(parent_dir, name)
        if os.path.exists(new_dir):
            QMessageBox.warning(self, "已存在", f"同名文件/文件夹已存在：\n{new_dir}")
            return
        try:
            os.makedirs(new_dir, exist_ok=False)
        except Exception as e:
            QMessageBox.critical(self, "创建失败", f"无法创建文件夹：\n{e}")
            return
        self._log.append_log(f"📁 已创建文件夹：{new_dir}", "SUCCESS")
        self._refresh_assets()

    # ==================== 蓝图类型切换 / 资源清理 ====================
    def _on_asset_set_bp_type(self, path: str, bp_type: str):
        """资源面板右键：把某张蓝图设为「普通蓝图」或「组件蓝图」。

        类型是蓝图自己的元数据（JSON 顶层 bp_type），与所在目录无关；
        只改这一个字段、不刷新 updated_at，画布上的未保存修改不受影响。
        """
        if not self._ensure_project_open("切换蓝图类型"):
            return
        if bp_type not in bp_kind_mod.VALID_TYPES:
            return
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "切换失败", f"文件不存在（可能已被外部删除）：\n{path}")
            self._refresh_assets()
            return
        old_type = bp_kind_mod.read_bp_type(path)
        if old_type == bp_type:
            return
        if not bp_kind_mod.write_bp_type(path, bp_type):
            QMessageBox.critical(self, "切换失败", f"无法写入蓝图文件：\n{path}")
            return
        # 正在编辑的就是这张 → 内存里的图也要同步，否则一保存又把类型写回去
        try:
            if self._current_path and os.path.normcase(os.path.abspath(self._current_path)) == \
                    os.path.normcase(os.path.abspath(path)) and self._graph is not None:
                self._graph.bp_type = bp_type
                self._mark_graph_clean(self._graph)
        except Exception:
            pass
        to_cn = "组件蓝图" if bp_type == bp_kind_mod.BP_TYPE_COMPONENT else "普通蓝图"
        extra = ""
        if old_type == bp_kind_mod.BP_TYPE_COMPONENT and bp_type == bp_kind_mod.BP_TYPE_BLUEPRINT:
            refs = self._count_component_refs(path)
            if refs:
                extra = (f"\n⚠ 仍有 {refs} 处「调用组件」节点引用它，"
                         "改成普通蓝图后这些调用不会失效，但语义上就不再是组件了。")
        self._log.append_log(f"🔁 已把 {os.path.basename(path)} 设为{to_cn}{extra}", "SUCCESS")
        if extra:
            QMessageBox.information(self, "已切换类型", f"{os.path.basename(path)} → {to_cn}{extra}")
        self._refresh_assets()

    def _on_asset_cleanup_assets(self):
        """🧹 清理未使用资源：删掉没有任何蓝图引用的图片，避免打包进无意义资源。"""
        if not self._ensure_project_open("清理未使用资源"):
            return
        root = self._proj_root()
        if not root:
            return
        try:
            unused, total = asset_store_mod.find_unused_assets(root)
        except Exception as e:
            QMessageBox.critical(self, "扫描失败", f"扫描工程资源时出错：\n{e}")
            return
        scope_cn = "、".join(asset_store_mod.SCAN_DIRS)
        if total == 0:
            QMessageBox.information(
                self, "🧹 清理未使用资源",
                f"扫描范围（{scope_cn}/）里还没有任何图片资源。\n\n"
                "截图 / 导入的图片会自动存进 resource/<蓝图所在目录>/，与蓝图一一对应。",
            )
            return
        if not unused:
            QMessageBox.information(
                self, "🧹 清理未使用资源",
                f"扫描了 {total} 个图片资源，全部都被蓝图引用着，没有可清理的内容。",
            )
            return
        try:
            size = sum(os.path.getsize(p) for p in unused)
            size_cn = f"{size / 1048576:.2f} MB" if size >= 1048576 else f"{size / 1024:.1f} KB"
        except Exception:
            size_cn = "未知大小"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("🧹 清理未使用资源")
        box.setText(
            f"扫描范围：{scope_cn}/（只扫资源目录，不动其它目录）\n"
            f"共 {total} 个图片资源，其中 {len(unused)} 个没有被任何蓝图引用，合计 {size_cn}。"
        )
        box.setInformativeText("确认后将「移入回收站」（可从系统回收站恢复），列表见下方详情。")
        box.setDetailedText("\n".join(unused))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            self._log.append_log(f"🧹 已取消清理（{len(unused)} 个候选资源未处理）", "INFO")
            return
        deleted, failed = [], []
        for p in unused:
            err = self._trash_one(p)
            (failed.append((p, err)) if err else deleted.append(p))
        # 清理后顺带收掉空目录，避免 res/ 里留下一堆空壳
        self._prune_empty_dirs([os.path.join(root, d) for d in asset_store_mod.SCAN_DIRS])
        self._log.append_log(
            f"🧹 已清理 {len(deleted)} 个未使用资源" + (f"，{len(failed)} 个失败" if failed else ""),
            "SUCCESS" if deleted else "WARN",
        )
        if failed:
            QMessageBox.warning(self, "部分清理失败",
                                "\n".join(f"{p}\n  → {e}" for p, e in failed[:5]))
        self._refresh_assets()

    @staticmethod
    def _prune_empty_dirs(roots):
        """自底向上删掉空目录（只在自己给定的根目录内，且不删根本身）。"""
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                if os.path.normcase(dirpath) == os.path.normcase(root):
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass

    def _save_node_asset(self, node, image, prefix: str, ext: str = ".png") -> str:
        """把节点用到的图片存进「该蓝图对应的 resource 目录」，返回相对工程根的路径。

        口径：<工程根>/resource/<当前蓝图所在目录>/<prefix>_<时间戳>_<随机>.png
              （蓝图未保存到工程内时落在 <工程根>/resource/ 根下）
        """
        root = self._proj_root()
        if not root:
            self._log.append_log("[资源] 尚未打开工程，图片资源无处安放。", "WARN")
            return ""
        try:
            rel = asset_store_mod.save_image(root, self._current_path, image, prefix=prefix, ext=ext)
        except Exception as e:
            self._log.append_log(f"[资源] 保存图片失败: {e}", "ERROR")
            return ""
        if not rel:
            self._log.append_log("[资源] 保存图片失败（写入被拒绝）。", "ERROR")
        return rel

    def _on_asset_rename(self, path: str):
        """重命名文件/目录；若被其它蓝图以组件形式引用，同步更新引用路径。"""
        if not path or not os.path.exists(path):
            self._refresh_assets()
            return
        is_dir = os.path.isdir(path)
        old_name = os.path.basename(path)
        default_name = old_name
        if (not is_dir) and old_name.lower().endswith(BlueprintStore.EXT.lower()):
            default_name = old_name[: -len(BlueprintStore.EXT)]
        new_name, ok = QInputDialog.getText(
            self, "重命名", f"新名称（{'文件夹' if is_dir else '文件'}）：", text=default_name
        )
        if not ok:
            return
        new_name = self._sanitize_stem(new_name)
        if not new_name:
            QMessageBox.warning(self, "名称非法", "名称不能为空。")
            return
        if (not is_dir) and (not new_name.lower().endswith(BlueprintStore.EXT.lower())):
            new_name = new_name + BlueprintStore.EXT
        if new_name == old_name:
            return
        new_path = os.path.join(os.path.dirname(path), new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "已存在", f"同名文件/文件夹已存在：\n{new_path}")
            return
        try:
            os.rename(path, new_path)
        except Exception as e:
            QMessageBox.critical(self, "重命名失败", f"无法重命名：\n{e}")
            return
        self._log.append_log(f"✏ 已重命名：{old_name} → {new_name}", "SUCCESS")
        # 当前编辑的正是被重命名的文件/其子文件 → 同步 _current_path，避免后续保存写回旧路径
        self._retarget_current_path_after_move(path, new_path, is_dir)
        # 同步更新其它蓝图里对旧路径的组件引用
        if old_name.lower().endswith(BlueprintStore.EXT.lower()) or is_dir:
            self._fix_component_refs_after_rename(path, new_path, is_dir)
        self._refresh_assets()

    def _retarget_current_path_after_move(self, old_path: str, new_path: str, is_dir: bool):
        """移动/重命名后，若当前蓝图路径受影响则同步更新。"""
        cur = self._current_path
        if not cur:
            return
        try:
            cur_abs = os.path.abspath(cur)
            old_abs = os.path.abspath(old_path)
            new_abs = os.path.abspath(new_path)
        except Exception:
            return
        changed = False
        if os.path.normcase(cur_abs) == os.path.normcase(old_abs):
            self._current_path = new_path
            changed = True
        elif is_dir:
            try:
                prefix = os.path.normcase(old_abs) + os.sep
                if os.path.normcase(cur_abs).startswith(prefix):
                    rel = os.path.relpath(cur_abs, old_abs)
                    self._current_path = os.path.join(new_abs, rel)
                    changed = True
            except Exception:
                pass
        if changed:
            try:
                self._pm.set_last_opened_blueprint(self._current_path)
            except Exception:
                pass
            try:
                self._graph.name = os.path.splitext(os.path.basename(self._current_path))[0].replace(".bp", "")
            except Exception:
                pass
            self._update_title()
            self._log.append_log(f"[Assets] 当前蓝图路径已同步为：{self._current_path}", "INFO")

    def _fix_component_refs_after_rename(self, old_path: str, new_path: str, is_dir: bool):
        """扫描工程内所有蓝图，把 Component 节点里指向旧路径的引用改写为新路径。

        组件引用存的是「相对工程根」的路径（如 components/登录.bp.json），
        重命名组件后若不改引用，所有调用处都会静默变成空操作 —— 这里一次性修好。
        """
        root = self._pm.project_root if (self._pm and self._pm.is_open()) else None
        if not root:
            return
        try:
            old_rel = os.path.relpath(os.path.abspath(old_path), os.path.abspath(root)).replace("\\", "/")
            new_rel = os.path.relpath(os.path.abspath(new_path), os.path.abspath(root)).replace("\\", "/")
        except Exception:
            return
        patched_files = 0
        patched_nodes = 0
        # 收集受影响的蓝图文件（含子目录，跳过隐藏/运行产物目录）
        targets = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                           ("cache", "debug", "log", "logs", "__pycache__", "dist", "build")]
            for fn in filenames:
                if fn.lower().endswith(".bp.json") and ".backup_" not in fn:
                    targets.append(os.path.join(dirpath, fn))
        for bp_path in targets:
            try:
                bp_path_abs = os.path.abspath(bp_path)
                if os.path.normcase(bp_path_abs) == os.path.normcase(os.path.abspath(new_path)):
                    continue          # 自己不用改
                g = BlueprintStore.load(bp_path)
                if g is None:
                    continue
                hits = self._rewrite_component_refs_in_graph(g, old_rel, new_rel, is_dir)
                hits += self._rewrite_component_refs_in_subgraphs(g, old_rel, new_rel, is_dir)
                if hits:
                    BlueprintStore.save(g, bp_path)
                    patched_files += 1
                    patched_nodes += hits
            except Exception as e:
                try:
                    self._log.append_log(f"[Assets] 修正组件引用失败 {os.path.basename(bp_path)}: {e}", "WARN")
                except Exception:
                    pass
        if patched_nodes:
            self._log.append_log(
                f"🔗 已在 {patched_files} 个蓝图里同步 {patched_nodes} 处组件引用：{old_rel} → {new_rel}",
                "SUCCESS",
            )
            # 当前打开的蓝图若是被改写的文件之一，需要重新加载以反映新引用
            try:
                cur = self._current_path
                if cur and os.path.isfile(cur):
                    g2 = BlueprintStore.load(cur)
                    if g2 is not None:
                        self._bind_new_graph(g2, zoom_fit=False)
                        self._mark_graph_clean(g2)
            except Exception:
                pass

    @staticmethod
    def _rewrite_component_refs_in_graph(graph, old_rel: str, new_rel: str, is_dir: bool,
                                        count_only: bool = False) -> int:
        """改写一张蓝图里的 Component 节点引用。返回改写处数。

        注意：节点参数存在 node.params（序列化时可能落在 data 键，由 Node.from_dict 统一读入），
        组件路径参数名为 blueprint_path，存的是相对工程根路径。
        """
        hits = 0
        try:
            nodes = getattr(graph, "nodes", {}) or {}
            for node in nodes.values():
                if getattr(node, "type", "") != "Component":
                    continue
                p = getattr(node, "params", None)
                if not isinstance(p, dict):
                    continue
                ref = p.get("blueprint_path")
                if not isinstance(ref, str) or not ref:
                    continue
                ref_norm = ref.replace("\\", "/").lstrip("./")
                if ref_norm == old_rel:
                    if not count_only:
                        p["blueprint_path"] = new_rel
                    hits += 1
                elif is_dir and ref_norm.startswith(old_rel.rstrip("/") + "/"):
                    if not count_only:
                        p["blueprint_path"] = new_rel.rstrip("/") + "/" + ref_norm[len(old_rel.rstrip("/")) + 1:]
                    hits += 1
        except Exception:
            pass
        return hits

    @classmethod
    def _rewrite_component_refs_in_subgraphs(cls, graph, old_rel: str, new_rel: str, is_dir: bool,
                                             count_only: bool = False) -> int:
        """递归改写子画布（loop_body / seq_body / par_body）里的 Component 引用。"""
        hits = 0
        try:
            nodes = getattr(graph, "nodes", {}) or {}
            for node in nodes.values():
                p = getattr(node, "params", None)
                if not isinstance(p, dict):
                    continue
                for body_key in ("loop_body", "seq_body", "par_body"):
                    body = p.get(body_key)
                    if not (isinstance(body, dict) and isinstance(body.get("nodes"), dict)):
                        continue
                    sub_nodes = body.get("nodes") or {}
                    for sn in sub_nodes.values():
                        if not isinstance(sn, dict):
                            continue
                        if str(sn.get("type", "")).lower() != "component":
                            continue
                        sp = sn.get("data") if isinstance(sn.get("data"), dict) else sn.get("params")
                        if not isinstance(sp, dict):
                            continue
                        ref = sp.get("blueprint_path")
                        if not isinstance(ref, str) or not ref:
                            continue
                        ref_norm = ref.replace("\\", "/").lstrip("./")
                        if ref_norm == old_rel:
                            if not count_only:
                                sp["blueprint_path"] = new_rel
                            hits += 1
                        elif is_dir and ref_norm.startswith(old_rel.rstrip("/") + "/"):
                            if not count_only:
                                sp["blueprint_path"] = new_rel.rstrip("/") + "/" + ref_norm[len(old_rel.rstrip("/")) + 1:]
                            hits += 1
        except Exception:
            pass
        return hits

    def _count_component_refs(self, old_path: str) -> int:
        """统计工程内其它蓝图对本文件（组件）的引用处数。"""
        root = self._pm.project_root if (self._pm and self._pm.is_open()) else None
        if not root:
            return 0
        try:
            old_rel = os.path.relpath(os.path.abspath(old_path), os.path.abspath(root)).replace("\\", "/")
        except Exception:
            return 0
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                           ("cache", "debug", "log", "logs", "__pycache__", "dist", "build")]
            for fn in filenames:
                if not fn.lower().endswith(".bp.json") or ".backup_" in fn:
                    continue
                bp = os.path.join(dirpath, fn)
                try:
                    if os.path.normcase(os.path.abspath(bp)) == os.path.normcase(os.path.abspath(old_path)):
                        continue
                    g = BlueprintStore.load(bp)
                    if g is None:
                        continue
                    total += self._rewrite_component_refs_in_graph(g, old_rel, "", False, count_only=True)
                    total += self._rewrite_component_refs_in_subgraphs(g, old_rel, "", False, count_only=True)
                except Exception as e:
                    try:
                        self._log.append_log(f"[Assets] 统计引用失败 {fn}: {e}", "WARN")
                    except Exception:
                        pass
        return total

    # ============================================================
    # 组件：引用收集 / 反向依赖 / 端口数核对（只提示）/ 循环引用检测
    # ============================================================
    def _proj_root(self) -> Optional[str]:
        """当前工程根目录（未打开工程返回 None）。"""
        try:
            if self._pm and self._pm.is_open():
                return self._pm.project_root
        except Exception:
            pass
        return None

    def _iter_project_blueprints(self):
        """遍历工程内所有蓝图文件（跳过隐藏目录、运行产物与备份文件）。"""
        root = self._proj_root()
        if not root:
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                           ("cache", "debug", "log", "logs", "__pycache__", "dist", "build")]
            for fn in filenames:
                if fn.lower().endswith(".bp.json") and ".backup_" not in fn:
                    yield os.path.join(dirpath, fn)

    @staticmethod
    def _norm_rel(rel: str) -> str:
        """组件引用路径归一化：统一斜杠、去掉开头的 ./ 与空白。"""
        return (rel or "").replace("\\", "/").lstrip("./").strip()

    @classmethod
    def _collect_refs_in_graph(cls, graph, target_rel: str, bp_path: str) -> List[dict]:
        """收集一张蓝图主画布组件的引用位置。"""
        out: List[dict] = []
        tr = cls._norm_rel(target_rel)
        for node in (getattr(graph, "nodes", {}) or {}).values():
            if getattr(node, "type", "") != "Component":
                continue
            ref = (getattr(node, "params", None) or {}).get("blueprint_path")
            if isinstance(ref, str) and cls._norm_rel(ref) == tr:
                out.append({"bp": bp_path, "node_id": str(getattr(node, "id", "")), "where": "主画布"})
        return out

    @classmethod
    def _collect_refs_in_subgraphs(cls, graph, target_rel: str, bp_path: str) -> List[dict]:
        """收集一张蓝图各子画布（循环体/顺序体/并行体）内的组件引用位置。"""
        out: List[dict] = []
        tr = cls._norm_rel(target_rel)
        for node in (getattr(graph, "nodes", {}) or {}).values():
            p = getattr(node, "params", None)
            if not isinstance(p, dict):
                continue
            for body_key, label in (("loop_body", "循环体"), ("seq_body", "顺序体"), ("par_body", "并行体")):
                body = p.get(body_key)
                if not (isinstance(body, dict) and isinstance(body.get("nodes"), dict)):
                    continue
                for sn in (body.get("nodes") or {}).values():
                    if not isinstance(sn, dict):
                        continue
                    if str(sn.get("type", "")).lower() != "component":
                        continue
                    sp = sn.get("data") if isinstance(sn.get("data"), dict) else sn.get("params")
                    if not isinstance(sp, dict):
                        continue
                    ref = sp.get("blueprint_path")
                    if isinstance(ref, str) and cls._norm_rel(ref) == tr:
                        out.append({
                            "bp": bp_path,
                            "node_id": str(sn.get("id", "")),
                            "where": f"{label}（宿主节点 {getattr(node, 'id', '')}）",
                        })
        return out

    def _collect_component_refs(self, target_path: str) -> List[dict]:
        """扫描整个工程，找出所有引用了 target_path（组件蓝图）的位置。"""
        root = self._proj_root()
        if not root:
            return []
        try:
            target_rel = os.path.relpath(os.path.abspath(target_path), os.path.abspath(root))
        except Exception:
            return []
        results: List[dict] = []
        for bp in self._iter_project_blueprints():
            try:
                if os.path.normcase(os.path.abspath(bp)) == os.path.normcase(os.path.abspath(target_path)):
                    continue
                g = BlueprintStore.load(bp)
                if g is None:
                    continue
                results += self._collect_refs_in_graph(g, target_rel, bp)
                results += self._collect_refs_in_subgraphs(g, target_rel, bp)
            except Exception:
                continue
        return results

    def _on_asset_reverse_deps(self, path: str):
        """🔗 谁引用了它：列出所有引用该组件的蓝图/节点，双击跳转。"""
        if not self._ensure_project_open("查看组件引用"):
            return
        name = os.path.basename(path)
        refs = self._collect_component_refs(path)
        root = self._proj_root() or ""
        if not refs:
            QMessageBox.information(
                self, "谁引用了它",
                f"当前工程里没有任何「调用组件」节点引用 {name}。\n\n"
                "（可以放心删除或改名，不会影响其它蓝图。）",
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"🔗 谁引用了 {name}")
        dlg.resize(620, 380)
        lay = QVBoxLayout(dlg)
        head = QLabel(f"共 {len(refs)} 处引用（双击条目可跳转到对应蓝图）：")
        head.setStyleSheet("color:#C9D1D9; font-size:11px;")
        lay.addWidget(head)
        lst = QListWidget()
        lst.setStyleSheet(
            "QListWidget { background:#0D1117; color:#C9D1D9; border:1px solid #30363D; font-size:11px; }"
            "QListWidget::item { padding:4px 6px; }"
            "QListWidget::item:selected { background:#1F6FEB; color:#FFFFFF; }"
        )
        for r in refs:
            try:
                shown = os.path.relpath(r["bp"], root) if root else r["bp"]
            except Exception:
                shown = r["bp"]
            it = QListWidgetItem(f"🗺 {shown}   ·   {r['where']}   ·   节点 {r['node_id']}")
            it.setData(Qt.UserRole, r["bp"])
            lst.addItem(it)
        lay.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.reject)
        row.addWidget(btn_close)
        lay.addLayout(row)
        picked = {"bp": ""}

        def _open_item(item):
            picked["bp"] = str(item.data(Qt.UserRole) or "")
            dlg.accept()

        lst.itemDoubleClicked.connect(_open_item)
        dlg.exec()
        if picked["bp"] and os.path.isfile(picked["bp"]):
            if self._confirm_discard():
                self._open_blueprint_path(picked["bp"])
                self._log.append_log(f"[Component] 已跳转到引用方：{os.path.basename(picked['bp'])}", "SUCCESS")

    # ---------------- 组件端口数核对（只提示，绝不自动改） ----------------
    @staticmethod
    def _component_port_counts(sub_graph) -> tuple:
        """读子蓝图定义，返回 (入参端口数, 返回值端口数)；某一端不存在返回 None。"""
        n_in = None
        n_out = None
        try:
            for node in (getattr(sub_graph, "nodes", {}) or {}).values():
                t = getattr(node, "type", "")
                p = getattr(node, "params", None) or {}
                if t == "Start" and n_in is None:
                    try:
                        n_in = max(0, min(20, int(p.get("num_params", 0) or 0)))
                    except Exception:
                        n_in = 0
                elif t == "End" and n_out is None:
                    try:
                        n_out = max(0, min(20, int(p.get("num_returns", 0) or 0)))
                    except Exception:
                        n_out = 0
        except Exception:
            pass
        return n_in, n_out

    def _check_component_port_match(self, node, abs_path: str, rel_path: str) -> bool:
        """核对「调用方端口数」与「组件 Start / End 声明的端口数」是否一致。

        只写日志，**绝不改参数**：改 num_inputs / num_outputs 会让节点重建动态端口，
        已经连好的线会断。所以这里只负责把不一致说出来，改不改、怎么改由用户决定。

        返回 True = 一致（或组件没声明端口，无从核对）。
        """
        try:
            sub = BlueprintStore.load(abs_path)
        except Exception as e:
            self._log.append_log(f"[Component] 读取组件失败：{e}", "WARN", node_id=node.id)
            return True
        if sub is None:
            return True
        n_in, n_out = self._component_port_counts(sub)
        if n_in is None and n_out is None:
            self._log.append_log(
                f"提示：组件 {os.path.basename(rel_path)} 里没有 Start / End 节点，"
                f"无法核对端口数（该组件也不会通过端口传参）。",
                "INFO", node_id=node.id,
            )
            return True
        cur_in = int(node.params.get("num_inputs", 0) or 0)
        cur_out = int(node.params.get("num_outputs", 0) or 0)
        diffs = []
        if n_in is not None and n_in != cur_in:
            diffs.append(f"入参：组件 Start 声明 {n_in} 个，本节点设了 {cur_in} 个")
        if n_out is not None and n_out != cur_out:
            diffs.append(f"返回值：组件 End 声明 {n_out} 个，本节点设了 {cur_out} 个")
        if not diffs:
            return True
        self._log.append_log(
            f"⚠ 组件端口数不一致（{os.path.basename(rel_path)}）：{'；'.join(diffs)}。"
            f"两边数量必须相等，运行时才能按序对上 —— 请手动把本节点改成与组件一致。"
            f"（不会自动改：改端口数会重建端口、断开已连好的线）",
            "WARN", node_id=node.id,
        )
        return False

    def _on_component_chosen(self, node_id: str, rel_path: str):
        """选完组件蓝图后：① 核对端口数（**只提示，绝不自动改**）② 循环引用检测。

        为什么不自动对齐：`num_inputs` / `num_outputs` 一改，节点会重建动态端口，
        已经连好的线会跟着断（用户明确要求：不要自动改，只提示未对齐）。
        """
        if self._graph is None or node_id not in self._graph.nodes:
            return
        rel_path = (rel_path or "").strip()
        if not rel_path:
            return
        root = self._proj_root()
        if not root:
            return
        abs_path = os.path.normpath(os.path.join(root, rel_path))
        if not os.path.isfile(abs_path):
            self._log.append_log(f"⚠ 组件文件不存在，无法核对端口数：{rel_path}", "WARN")
            return
        node = self._graph.nodes[node_id]

        # ---- ① 端口数一致性核对（只打日志，不改任何参数）----
        self._check_component_port_match(node, abs_path, rel_path)

        # ---- ② 循环引用检测 ----
        try:
            cycle = self._find_component_cycle(rel_path)
            if cycle:
                chain = " → ".join(cycle)
                self._log.append_log(f"⛔ 检测到组件循环引用：{chain}", "ERROR")
                QMessageBox.warning(
                    self, "组件循环引用",
                    "检测到循环引用，运行时会无限递归、必须修掉：\n\n"
                    f"  {chain}\n\n"
                    "说明：链路上的蓝图互相「调用组件」绕回了自己。请断开其中一环。",
                )
        except Exception:
            pass

    def _find_component_cycle(self, start_rel: str, max_depth: int = 12) -> Optional[List[str]]:
        """从 start_rel（组件相对路径）出发，检测是否绕回当前正在编辑的蓝图。

        返回循环链路（如 ['components/A.bp.json', 'components/B.bp.json', 'main.bp.json']）；
        无循环返回 None。
        """
        root = self._proj_root()
        cur = self._current_path
        if not root or not cur:
            return None
        try:
            cur_rel = self._norm_rel(os.path.relpath(os.path.abspath(cur), os.path.abspath(root)))
        except Exception:
            return None

        def _refs_of(rel: str) -> List[str]:
            abs_p = os.path.normpath(os.path.join(root, rel))
            if not os.path.isfile(abs_p):
                return []
            g = BlueprintStore.load(abs_p)
            if g is None:
                return []
            refs: List[str] = []
            for node in (getattr(g, "nodes", {}) or {}).values():
                if getattr(node, "type", "") == "Component":
                    r = (getattr(node, "params", None) or {}).get("blueprint_path")
                    if isinstance(r, str) and r.strip():
                        refs.append(self._norm_rel(r))
                p = getattr(node, "params", None)
                if isinstance(p, dict):
                    for k in ("loop_body", "seq_body", "par_body"):
                        body = p.get(k)
                        if not (isinstance(body, dict) and isinstance(body.get("nodes"), dict)):
                            continue
                        for sn in (body.get("nodes") or {}).values():
                            if not isinstance(sn, dict) or str(sn.get("type", "")).lower() != "component":
                                continue
                            sp = sn.get("data") if isinstance(sn.get("data"), dict) else sn.get("params")
                            if isinstance(sp, dict):
                                r = sp.get("blueprint_path")
                                if isinstance(r, str) and r.strip():
                                    refs.append(self._norm_rel(r))
            return refs

        start = self._norm_rel(start_rel)
        if start == cur_rel:
            return [start, cur_rel]

        def _dfs(rel: str, chain: List[str]):
            if len(chain) > max_depth:
                return None
            for nxt in _refs_of(rel):
                if nxt == cur_rel:
                    return chain + [nxt]
                if nxt in chain:
                    continue          # 链上已有的环（与当前蓝图无关）不重复展开
                got = _dfs(nxt, chain + [nxt])
                if got:
                    return got
            return None

        return _dfs(start, [start])

    # ---------------- 资源面板拖蓝图到画布 ----------------
    def _on_asset_dropped(self, abs_path: str, scene_pos):
        """把资源面板里的蓝图拖到画布：命中「调用组件」节点则填引用，否则新建一个组件节点。"""
        if self._graph is None or self._scene is None:
            return
        if not isinstance(abs_path, str) or not os.path.isfile(abs_path):
            return
        if not abs_path.lower().endswith(".bp.json"):
            self._log.append_log("只有蓝图（.bp.json）可以拖到画布上。", "WARN")
            return
        root = self._proj_root()
        if not root:
            self._log.append_log("请先打开工程，再拖蓝图到画布。", "WARN")
            return
        # 若拖到的是当前正在编辑的蓝图本身 → 直接阻止（会自我递归）
        try:
            if os.path.normcase(os.path.abspath(abs_path)) == os.path.normcase(os.path.abspath(self._current_path or "")):
                QMessageBox.information(self, "不能这么拖", "不能把蓝图拖到它自己身上（会变成自我递归调用）。")
                return
        except Exception:
            pass
        rel = self._norm_rel(os.path.relpath(abs_path, root))
        if not bp_kind_mod.is_component(abs_path, root):
            self._log.append_log(
                f"ℹ {os.path.basename(abs_path)} 目前是「普通蓝图」，这里按「调用组件」引用了它。"
                "若要让它算作组件（资源树显示为 🧩），右键 → 设为组件蓝图。", "INFO")

        # 命中已有「调用组件」节点？（拖到节点上 = 给它填引用）
        target = None
        try:
            for nid, ni in self._scene._node_items.items():
                nd = self._graph.nodes.get(nid)
                if nd is not None and nd.type == "Component" and ni.sceneBoundingRect().contains(scene_pos):
                    target = nd
                    break
        except Exception:
            target = None

        if target is not None:
            self._on_param_changed(target.id, "blueprint_path", rel)
            self._on_component_chosen(target.id, rel)
            self._scene.select_node_id(target.id)
            self._log.append_log(f"🧩 已把 {os.path.basename(abs_path)} 填入选中的组件节点", "SUCCESS")
            return

        node = self._scene.add_node_by_type("Component", scene_pos)
        if node is None:
            return
        self._on_param_changed(node.id, "blueprint_path", rel)
        self._on_component_chosen(node.id, rel)
        self._scene.select_node_id(node.id)
        self._log.append_log(f"🧩 已从资源面板创建「调用组件」节点 → {os.path.basename(abs_path)}", "SUCCESS")

    # ---------------- 日志 ↔ 画布节点 定位 ----------------
    def _on_log_line_activated(self, node_id: str, line_text: str):
        """双击日志行 → 定位到画布上对应的节点。"""
        nid = (node_id or "").strip()
        if not nid and line_text:
            import re as _re
            m = _re.search(r"(?:节点|node)[\s:：=]*([A-Za-z0-9_\-]+)", line_text)
            if m:
                nid = m.group(1)
        if not nid:
            self._log.append_log("这一行日志没有关联的节点信息（只有运行日志才会带节点定位）。", "INFO")
            return
        self._locate_node_in_canvas(nid)

    def _locate_node_in_canvas(self, node_id: str):
        """把画布跳到并选中指定节点（复用搜索栏的定位能力）。"""
        if self._graph is None or node_id not in self._graph.nodes:
            self._log.append_log(f"节点 {node_id} 不在当前蓝图（可能在别的蓝图/子画布里）。", "WARN")
            return
        ok = False
        try:
            if self._view is not None:
                ok = self._view.jump_to_node_by_id(node_id, flash_ms=2600)
        except Exception:
            ok = False
        if not ok:
            try:
                self._scene.select_node_id(node_id)
            except Exception:
                pass
        ntype = self._graph.nodes[node_id].type
        self._log.append_log(f"📍 已定位到节点 {ntype} id={node_id[:8]}", "INFO")

    def _on_node_find_in_log(self, node_id: str):
        """节点右键「在日志中查找」→ 把日志滚动到该节点最近的一条。"""
        ok = False
        try:
            ok = self._log.highlight_node(node_id)
        except Exception:
            ok = False
        if ok:
            self._log.append_log(f"🔍 日志已定位到节点 {node_id[:8]} 的记录", "INFO")
        else:
            self._log.append_log(f"日志里还没有节点 {node_id[:8]} 的记录（先运行一次蓝图试试）。", "INFO")

    def _on_asset_delete(self, paths):
        """资源面板删除（走回收站，尽量可恢复）。"""
        if not self._ensure_project_open("删除资源"):
            return
        if isinstance(paths, str):
            paths = [paths]
        targets = [p for p in (paths or []) if p and os.path.exists(p)]
        if not targets:
            self._refresh_assets()
            return
        display = "\n".join("  · " + p for p in targets[:10])
        more = f"\n  …… 共 {len(targets)} 项" if len(targets) > 10 else ""
        # 被组件引用的文件先提示引用数量，避免「删了组件，一堆蓝图静默失效」
        ref_total = 0
        for p in targets:
            if os.path.isfile(p) and p.lower().endswith(BlueprintStore.EXT.lower()):
                ref_total += self._count_component_refs(p)
        ref_warn = f"\n\n⚠ 检测到 {ref_total} 处组件引用指向它，删除后这些调用会变成空操作。" if ref_total else ""
        ret = QMessageBox.warning(
            self, "⚠ 删除资源",
            f"确定要删除以下内容吗？（会移入回收站）\n\n{display}{more}{ref_warn}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        deleted, failed = [], []
        for p in targets:
            err = self._trash_one(p)
            (failed.append((p, err)) if err else deleted.append(p))
        if deleted:
            self._log.append_log(f"🗑 已删除 {len(deleted)} 项：{', '.join(os.path.basename(d) for d in deleted[:5])}", "WARN")
        if failed:
            QMessageBox.critical(self, "部分删除失败", "\n".join(f"{p}\n  → {e}" for p, e in failed[:5]))
        # 当前蓝图被删 → 清成空白蓝图，避免继续往已删文件里写
        try:
            cur = self._current_path
            if cur and any(os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(d)) for d in deleted):
                self._log.append_log("[Assets] 当前蓝图已被删除，已切换为空白蓝图。", "WARN")
                self._current_path = None
                self._new_graph()
        except Exception:
            pass
        self._refresh_assets()

    def _trash_one(self, path: str) -> str:
        """删除单个文件/目录：优先回收站，失败回退彻底删除。返回空串=成功，否则返回错误信息。"""
        try:
            import send2trash  # type: ignore
        except Exception:
            send2trash = None
        if send2trash is not None:
            try:
                send2trash.send2trash(path)
                return ""
            except Exception as e:
                fallback_err = f"回收站失败：{e}"
        else:
            fallback_err = "未安装 send2trash"
        try:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)
            return ""
        except Exception as e:
            return f"{fallback_err}；彻底删除也失败：{e}"

    def _on_asset_reveal(self, path: str):
        """在系统资源管理器中定位文件/打开目录。"""
        try:
            if os.name == "nt":
                if os.path.isdir(path):
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    import subprocess
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])
        except Exception as e:
            self._log.append_log(f"[Assets] 打开资源管理器失败: {e}", "WARN")

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

    def _root_graph(self):
        """当前正在编辑的「文档」根图：处于子画布时是栈底的父图，否则就是 self._graph。"""
        if self._subgraph_stack:
            root = self._subgraph_stack[0].get("parent_graph")
            if root is not None:
                return root
        return self._graph

    def _doc_dict(self):
        """当前文档（根图）的序列化 dict；处于子画布时把每层实时子图内容覆盖进宿主节点。

        为什么要这样：dirty 判定 / 哈希比较必须始终以「整份蓝图」为准。
        若直接对 self._graph 算 hash，进入子画布后 self._graph 变成子图，
        退出又变回父图，哈希基准来回跳 → 会出现「刚保存就显示未保存 *」的假脏。
        """
        root = self._root_graph()
        if root is None:
            return None
        try:
            data = root.to_dict()
        except Exception:
            return None
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            return data
        depth = len(self._subgraph_stack)
        # 先看每一层相对「进入时」有没有真实改动：只有「改动层 → 更深层」才需要覆盖进文档，
        # 没改动的层保持磁盘原样 —— 否则「进子画布看一眼」就会被算成「有未保存修改」。
        changed = []
        subs = []
        for idx, item in enumerate(self._subgraph_stack):
            sub = self._graph if idx == depth - 1 else item.get("sub_graph")
            subs.append(sub)
            try:
                changed.append(self._sub_changed(item, sub))
            except Exception:
                changed.append(True)  # 保守：算不出来就当作改过，宁可提示保存也不要漏
        first_changed = next((i for i, c in enumerate(changed) if c), None)
        if first_changed is None:
            return data
        for idx in range(first_changed, depth):
            item = self._subgraph_stack[idx]
            body_key = item.get("body_key")
            nid = item.get("node_id")
            host = nodes.get(nid) if isinstance(nid, str) else None
            if not isinstance(host, dict) or not body_key:
                break
            sub = subs[idx]
            if sub is None:
                break
            sub_data = self._sub_canonical(sub)
            if sub_data is None:
                break
            params = host.get("data")
            if not isinstance(params, dict):
                params = {}
                host["data"] = params
            # ★ 与写回同口径（只含 nodes+links）：保证「保存后算出的哈希」与「这里覆盖出的哈希」一致
            params[body_key] = sub_data
            nodes = sub_data.get("nodes") if isinstance(sub_data.get("nodes"), dict) else {}
        return data

    def _current_doc_hash(self) -> Optional[str]:
        """当前文档（根图 + 实时子画布内容）的 SHA1 哈希，口径与 _hash_graph 完全一致。"""
        data = self._doc_dict()
        if data is None:
            return None
        try:
            canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2)
            return hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _mark_graph_clean(self, graph=None):
        """把当前图标记为『干净状态』（未改动）——在加载成功/保存成功后调用。

        参数 graph：
          - 若显式传入（推荐在"绑定新 graph 之前"直接用新加载的 g），
            直接用传入对象算 hash，**不依赖 `self._graph` 此时的赋值状态**。
          - 若不传（保存场景），用「当前文档」哈希（根图 + 实时子画布），
            这样在子画布里保存后不会误报未保存。

        为什么要支持显式传参？
          因为 `_bind_new_graph(g)` 里才做 `self._graph = g`，
          如果在 bind 之前就调 `_mark_graph_clean()` 而不传参，算 hash 用的是
          旧的 self._graph（或 None），就会出现「刚加载完永远是未保存状态」的 bug。
        """
        self._last_saved_graph_hash = (
            self._hash_graph(graph) if graph is not None else self._current_doc_hash()
        )

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
        current_hash = self._current_doc_hash()
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
        comp_mark = ""  # 组件蓝图标识（普通蓝图为空）
        if self._graph is not None:
            # 文档名必须取「根图」的名字：子画布那张图的 name 是 "Untitled"，
            # 直接用 self._graph 会让「进循环体/并行体后标题栏丢掉蓝图名」。
            _doc = self._root_graph() or self._graph
            blueprint_name = str(_doc.name or "未命名蓝图").strip() or "未命名蓝图"
            # 组件蓝图与普通蓝图在编辑器里长得一模一样，标题栏必须把身份标出来，
            # 否则用户分不清「我改的是被 N 处引用的公共组件」还是「一张独立蓝图」。
            if str(getattr(_doc, "bp_type", "") or "") == bp_kind_mod.BP_TYPE_COMPONENT:
                comp_mark = "  [组件]"
            if self._need_save():
                saved_mark = " *"  # 未保存 / 有改动 → 标题栏加星号
        # 3. 拼接标题逻辑：有工程就放最前面；/号只在「同时有工程和蓝图」时显示，避免多余符号
        if project_part and blueprint_name:
            title = f"{project_part}  /  {blueprint_name}{comp_mark}{saved_mark} — {app_name}"
        elif project_part:
            title = f"{project_part} — {app_name}"
        elif blueprint_name:
            title = f"{blueprint_name}{comp_mark}{saved_mark} — {app_name}"
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
        # 走延迟入口：面板重建（50~250ms）不再卡在鼠标事件那一帧，
        # 节点选中高亮 / 连线联动先画出来，面板随后刷新（详见 set_node_deferred）。
        if self._graph is None or node_id not in self._graph.nodes:
            self._inspector.set_node_deferred(None)
            return
        self._inspector.set_node_deferred(self._graph.nodes[node_id])

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
        # 改参数**之前**先把节点当前（旧的）整块区域标记为待重绘：
        # 摘要行数/预览图变化会让节点变高变矮，变矮时旧区域必须重绘才不会留残影。
        # 必须在改之前——update() 的矩形会被裁剪到当时的 boundingRect，
        # 等 params 写进去后 boundingRect 已经变小，旧区域就再也标不上了。
        ni = self._scene.node_item(node_id)
        _old_scene_rect = None
        if ni is not None:
            try:
                _old_scene_rect = ni.mark_geometry_dirty()
            except Exception:
                _old_scene_rect = None
        node.params[param_name] = value
        # ---- 并行体「主线程分支」唯一性（用户定案：有且只能有一个）----
        # 在某个分支入口上勾了「主线程分支」→ 同一张并行体画布里的其它分支入口自动取消勾选。
        # （处在并行体子画布时 self._graph 就是那张子图，兄弟分支入口都在里面。）
        if node.type == "Branch" and param_name == "is_main" and bool(value):
            for other in self._graph.nodes.values():
                if other.id != node.id and other.type == "Branch" and other.params.get("is_main"):
                    other.params["is_main"] = False
                    oi = self._scene.node_item(other.id)
                    if oi is not None:
                        try:
                            oi.update()
                        except Exception:
                            pass
                    self._log.append_log(f"[Parallel] 主线程分支唯一 → 已取消节点 {other.id} 的「主线程分支」勾选", "INFO")
        # ---- 换模板 → 级联清空「特征识别辅助」里与旧模板绑定的数据 ----
        # 特征的 rel_x/rel_y、特征图、模板总宽高都是按「截取时那张模板」记录的：
        # 换了模板，旧特征既不可能命中（验证模式 100% 失败），还会让兜底模式推算的模板原点整体错位。
        # 这里清空，并把清空动作并入刚压入的那条撤销记录（Ctrl+Z 可一起还原）。
        if param_name == "template" and not applying_undo:
            try:
                self._cascade_clear_feature_assist(node, cur)
            except Exception as e:
                print(f"[WARN] 换模板时清空特征数据失败: {e}")
        if ni is not None:
            # 动态端口节点：关键参数变化时，重建端口集（输入/输出都可能变）
            # 注意：每次端口变化后，必须延迟刷新 inspector，退出当前信号栈再重建 UI，防止 sender 控件被中途删除导致 0xC0000005 ACCESS_VIOLATION
            try:
                need_refresh_ports = False
                # ---- Component：num_inputs / num_outputs 变 → 输入/输出端口数变 ----
                if node.type == "Component" and param_name in ("num_inputs", "num_outputs"):
                    from src.blueprint.core.node_library import _resolve_input_ports_default, _resolve_output_ports_default
                    p = dict(node.params or {})
                    new_in = _resolve_input_ports_default(node.type, p)
                    new_out = _resolve_output_ports_default(node.type, p)
                    cur_in_names = [pp.name for pp in node.inputs]
                    cur_out_names = [pp.name for pp in node.outputs]
                    new_in_names = [pp.name for pp in new_in]
                    new_out_names = [pp.name for pp in new_out]
                    if cur_in_names != new_in_names or cur_out_names != new_out_names:
                        removed_in_set = set(cur_in_names) - set(new_in_names)
                        removed_out_set = set(cur_out_names) - set(new_out_names)
                        if removed_in_set or removed_out_set:
                            edges_to_remove = []
                            for edge in list(self._graph.edges.values()):
                                if (edge.to_node == node_id and edge.to_port in removed_in_set) or \
                                   (edge.from_node == node_id and edge.from_port in removed_out_set):
                                    edges_to_remove.append(edge.id)
                            for eid in edges_to_remove:
                                if self._scene is not None:
                                    self._scene.remove_edge(eid, _emit_will=False)
                        node.inputs = list(new_in)
                        node.outputs = list(new_out)
                        need_refresh_ports = True
                # ---- Start：num_params 变 → 输出端口数变（组件蓝图入参）----
                if node.type == "Start" and param_name == "num_params":
                    from src.blueprint.core.node_library import _resolve_output_ports_default
                    p = dict(node.params or {})
                    new_out = _resolve_output_ports_default(node.type, p)
                    cur_out_names = [pp.name for pp in node.outputs]
                    new_out_names = [pp.name for pp in new_out]
                    if cur_out_names != new_out_names:
                        removed_out_set = set(cur_out_names) - set(new_out_names)
                        removed_in_set = set()
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
                        need_refresh_ports = True
                # ---- End：num_returns 变 → 输入端口数变（组件蓝图返回值）----
                if node.type == "End" and param_name == "num_returns":
                    from src.blueprint.core.node_library import _resolve_input_ports_default
                    p = dict(node.params or {})
                    new_in = _resolve_input_ports_default(node.type, p)
                    cur_in_names = [pp.name for pp in node.inputs]
                    new_in_names = [pp.name for pp in new_in]
                    if cur_in_names != new_in_names:
                        removed_in_set = set(cur_in_names) - set(new_in_names)
                        removed_out_set = set()
                        if removed_in_set or removed_out_set:
                            edges_to_remove = []
                            for edge in list(self._graph.edges.values()):
                                if (edge.to_node == node_id and edge.to_port in removed_in_set) or \
                                   (edge.from_node == node_id and edge.from_port in removed_out_set):
                                    edges_to_remove.append(edge.id)
                            for eid in edges_to_remove:
                                if self._scene is not None:
                                    self._scene.remove_edge(eid, _emit_will=False)
                        node.inputs = list(new_in)
                        need_refresh_ports = True
                # ---- VarSwitch / SetVariable：num_data_inputs 变 → 输入端口数变 ----
                if node.type in ("VarSwitch", "SetVariable") and param_name == "num_data_inputs":
                    from src.blueprint.core.node_library import _resolve_input_ports_default
                    p = dict(node.params or {})
                    new_in = _resolve_input_ports_default(node.type, p)
                    cur_in_names = [pp.name for pp in node.inputs]
                    new_in_names = [pp.name for pp in new_in]
                    if cur_in_names != new_in_names:
                        # 清理被移除输入端口上的关联连线
                        removed_in_set = set(cur_in_names) - set(new_in_names)
                        removed_out_set = set()
                        if removed_in_set or removed_out_set:
                            edges_to_remove = []
                            for edge in list(self._graph.edges.values()):
                                if (edge.to_node == node_id and edge.to_port in removed_in_set) or \
                                   (edge.from_node == node_id and edge.from_port in removed_out_set):
                                    edges_to_remove.append(edge.id)
                            for eid in edges_to_remove:
                                if self._scene is not None:
                                    self._scene.remove_edge(eid, _emit_will=False)
                        node.inputs = list(new_in)
                        need_refresh_ports = True
                # ---- SetVariable：batch_text 里的变量名 → 数据输出端口 ----
                # 变量的增删/改名都会改变端口集，但 batch_text 是**逐字符**提交的：
                # 这里只登记，由 400ms 防抖定时器统一重建（打字过程中既不重建端口，
                # 也不刷新属性面板，否则会把用户正在输入的文本框刷掉）。
                if node.type == "SetVariable" and param_name == "batch_text":
                    self._setvar_queue_ports(node.id)

                if need_refresh_ports:
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
            # 任何参数变化 → 刷新节点几何 + 重绘（标题三段式 + 摘要文本）
            # 传入改参数前采样的旧几何：节点变矮时显式让视口重绘旧区域，否则留残影
            ni.refresh_geometry(_old_scene_rect)
        self._scene.graph_changed.emit()

    # ---------------- SetVariable：变量 → 数据输出端口（防抖重建） ----------------
    def _setvar_queue_ports(self, node_id: str):
        """登记一个待对齐输出端口的「设置变量」节点，并（重）启防抖定时器。"""
        try:
            self._setvar_port_pending.add(node_id)
            self._setvar_port_timer.start()
        except Exception:
            pass

    def _apply_setvar_output_ports(self):
        """把「设置变量」节点的数据输出端口对齐到 batch_text 里当前设置的变量名。

        端口名/标签 = 变量名，所以「写几个变量就长出几个出参」，可直接连到
        「组件」节点的入参端口，或组件蓝图里「结束」节点的返回值端口。

        只有端口名集合真的变了才重建；重建口径与 Component / Start / End 完全一致
        （先删掉「消失端口」上的连线 → 换端口 → 重建 PortItem → 按端口名重挂其余边）。
        ⚠ 刻意**不刷新属性面板**：batch_text 是逐字符提交的，刷新会打断用户正在输入的文本。
        """
        pending = set(getattr(self, "_setvar_port_pending", set()) or set())
        try:
            self._setvar_port_pending = set()
        except Exception:
            pass
        if not pending or self._graph is None:
            return
        from src.blueprint.core.node_library import _resolve_output_ports_default
        for node_id in sorted(pending):
            node = self._graph.nodes.get(node_id)
            if node is None or node.type != "SetVariable":
                continue
            try:
                # 写错的行（认不下来的行）不会变成变量 → 也就不会有对应出参。
                # 这里必须点出来，否则用户看到的是「明明写了变量，右侧却没有出参」而毫无线索。
                # 单独 try 包住：报告失败绝不能影响端口重建本身。
                try:
                    self._report_setvar_parse_errors(node)
                except Exception:
                    pass
                new_out = _resolve_output_ports_default("SetVariable", dict(node.params or {}))
                cur_out_names = [p.name for p in node.outputs]
                new_out_names = [p.name for p in new_out]
                if cur_out_names == new_out_names:
                    continue
                removed = set(cur_out_names) - set(new_out_names)
                if removed and self._scene is not None:
                    for eid in [e.id for e in list(self._graph.edges.values())
                                if e.from_node == node_id and e.from_port in removed]:
                        self._scene.remove_edge(eid, _emit_will=False)
                node.outputs = list(new_out)
                if self._scene is not None:
                    ni = self._scene.node_item(node_id)
                    if ni is not None:
                        ni.sync_from_node()   # 内部已含 refresh_geometry()
                        self._scene.reconnect_node_edges(node_id)
                try:
                    self._log.append_log(
                        "「设置变量」节点的数据出口已按变量名更新："
                        + ("、".join(new_out_names[1:]) if len(new_out_names) > 1 else "（暂无变量）"),
                        "DEBUG",
                        node_id=node_id,
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"[WARN] SetVariable 输出端口重建异常: {e}")
        if self._scene is not None:
            self._scene.graph_changed.emit()

    def _report_setvar_parse_errors(self, node):
        """把「设置变量」里认不下来的行打到日志（同一种错误只报一次）。

        `batch_text` 里每一行都必须是 `变量名=值`，变量名须以字母/下划线/汉字开头
        （不能数字开头、不能含空格或横杠）。写错的行**不会**变成变量，
        因此也不会生成右侧数据出口 —— 这就是「写了变量却没有出参」的唯一原因，
        必须明确告知，而不是静默丢掉。
        """
        try:
            from src.blueprint.core.node_library import _setvar_parse_issues
            _names, errs = _setvar_parse_issues((node.params or {}).get("batch_text"))
        except Exception:
            return
        reported = getattr(self, "_setvar_err_reported", None)
        if not isinstance(reported, dict):     # 防御：属性缺失也不能把端口重建带崩
            reported = {}
            try:
                self._setvar_err_reported = reported
            except Exception:
                pass
        sig = "\n".join(errs)
        if reported.get(node.id) == sig:
            return          # 与上次提示内容完全相同 → 不重复刷日志
        reported[node.id] = sig
        if not errs:
            return
        head = "；".join(errs[:2])
        if len(errs) > 2:
            head += f" …等共 {len(errs)} 行"
        try:
            self._log.append_log(
                f"「设置变量」有 {len(errs)} 行写法不正确，这些行不会生成右侧出参（"
                f"变量名须字母/下划线/汉字开头，数字开头或含空格、横杠都不行）：{head}",
                "WARN",
                node_id=node.id,
            )
        except Exception:
            pass

    # 与「模板图」绑定的特征辅助参数：模板一换，这些值全部失效
    _FEATURE_BOUND_PARAMS = (
        "features_json",          # 特征图 + 相对模板左上角的 rel_x/rel_y
        "feature_template_w",     # 旧模板总宽
        "feature_template_h",     # 旧模板总高
        "feature_capture_width",  # 截取特征时的窗口宽（缩放回退用）
    )

    def _cascade_clear_feature_assist(self, node, old_template):
        """换模板时清空与旧模板绑定的特征数据，并把本次清空并入同一条撤销记录。

        old_template 是改参数之前的旧值（用于「路径没真变」的短路）。
        """
        import copy as _copy
        try:
            if str(node.params.get("template", "") or "") == str(old_template or ""):
                return
            old_vals = {}
            new_vals = {}
            changed = False
            for k in self._FEATURE_BOUND_PARAMS:
                old_v = _copy.deepcopy(node.params.get(k, None))
                new_v = "[]" if k == "features_json" else 0
                if old_v != new_v:
                    changed = True
                old_vals[k] = old_v
                new_vals[k] = new_v
                node.params[k] = new_v
            if not changed:
                return
            # 并入刚压入的那条 change_param 记录，保证 Ctrl+Z 一次性还原「模板+特征」
            if self._undo_stack:
                top = self._undo_stack[-1]
                if (isinstance(top, dict) and top.get("kind") == "change_param"
                        and top.get("node_id") == node.id and top.get("param_name") == "template"):
                    top["cascade"] = {
                        "node_id": node.id,
                        "old_values": old_vals,
                        "new_values": new_vals,
                    }
            try:
                self._log.append_log(
                    "[找图] 模板已更换 → 原特征数据已自动清空"
                    "（特征图与相对位置都是相对旧模板记录的，留着必然误判）；"
                    "若仍需特征辅助，请重新点「✂ 从模板截取特征」。",
                    "WARN",
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[WARN] _cascade_clear_feature_assist 异常: {e}")

    def _apply_cascade_params(self, action, which: str):
        """撤销/重做 change_param 时，把附带记录的级联参数（如特征数据）一并应用。

        which = "old_values"（撤销）/ "new_values"（重做）。
        调用时 _applying_undo_redo=True，所以不会再次压栈、也不会再次触发清空逻辑。
        """
        try:
            casc = action.get("cascade")
            if not isinstance(casc, dict):
                return
            nid = casc.get("node_id") or action.get("node_id")
            vals = casc.get(which)
            if not nid or not isinstance(vals, dict):
                return
            for k, v in vals.items():
                self._on_param_changed(nid, k, v)
        except Exception as e:
            print(f"[WARN] 应用级联参数失败: {e}")

    def _on_pick_template(self, node_id: str, param_name: str):
        if not self._ensure_project_open("裁剪模板保存到工程resource/"):
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
            # ✅ 存到「该蓝图对应的 resource 目录」：<工程根>/resource/<蓝图所在目录>/tpl_*.png
            #    （资源跟着蓝图走，不再全工程塞一个目录；清理时也能逐个蓝图对账）
            rel = self._save_node_asset(None, img_bgr, "tpl")
            if not rel:
                self._log.append_log("[资源] 模板保存失败，未回填路径。", "ERROR")
                return
            full = os.path.join(self._proj_root() or "", rel.replace("/", os.sep))
            self._on_param_changed(node_id, param_name, rel)
            self._log.append_log(f"模板已裁到: {full}", "SUCCESS")
            # 重新 set_node 刷新表单
            if self._graph and node_id in self._graph.nodes:
                self._inspector.set_node(self._graph.nodes[node_id])
        except Exception as e:
            self._log.append_log(f"裁图失败: {e}", "ERROR")
            traceback.print_exc()

    # ---------- 目标窗口解析（Inspector 框选范围 / Mouse 坐标拾取 共用） ----------
    def _find_node_target_window(self, node):
        """按节点「目标窗口」参数找窗口，返回 (window_dict, status)。

        status = "none"      未配置目标窗口定位条件
               = "ok"        已找到（window_dict 含 hwnd/title/exe_name/class_name/rect）
               = "notfound"  配置了但当前找不到

        匹配一律走运行时同款 _find_window_3d（title 子串 / exe 子串 / class 全等，
        填了就必须命中；不过滤最小化窗口）。
        「编辑器 / 运行时口径一致」原则：可见窗口枚举 enumerate_valid_windows
        只允许用于「📋 从列表选择」这个配置入口，禁止参与任何匹配 / 运行路径。
        改这里必须同步 executor._find_window_3d。
        """
        params = getattr(node, "params", None) or {}
        title = str(params.get("target_title", "") or "").strip()
        exe = str(params.get("target_exe", "") or "").strip()
        cls = str(params.get("target_class", "") or "").strip()
        if not (title or exe or cls):
            return None, "none"
        try:
            from src.blueprint.runtime.executor import _find_window_3d
            hwnd = _find_window_3d(title, exe, cls)
        except Exception as e:
            self._log.append_log(f"[Target] 目标窗口匹配失败: {e}", "WARN")
            return None, "notfound"
        if not hwnd:
            return None, "notfound"
        try:
            from src.blueprint.ui.window_picker_overlay import _get_window_info
            best = _get_window_info(hwnd)
        except Exception as e:
            self._log.append_log(f"[Target] 窗口信息读取失败: {e}", "WARN")
            best = None
        if not best:
            return None, "notfound"
        return best, "ok"

    def _resolve_snip_limit(self, node):
        """解析节点的「目标窗口」，用于把框选范围限定在该窗口内。

        返回 (rect, label, status)：
          status = "none"     未配置目标窗口 → 按原逻辑全屏框选
                 = "ok"       已找到窗口，rect=(x, y, w, h) 为该窗口屏幕矩形
                 = "notfound" 配置了但当前找不到该窗口 → 回退全屏并告警
        """
        best, status = self._find_node_target_window(node)
        if status != "ok" or best is None:
            return None, "", status
        # 把临时目标窗口提到最前，避免截到盖在它上面的其它窗口
        # （bring_window_to_front 会自动还原最小化窗口；还原后矩形会变，需重取）
        hwnd = int(best.get("hwnd") or 0) or None
        if hwnd:
            try:
                from src.tools.window_utils import bring_window_to_front
                bring_window_to_front(hwnd)
            except Exception:
                pass
            try:
                from src.blueprint.ui.window_picker_overlay import _get_window_info
                fresh = _get_window_info(hwnd)
                if fresh:
                    best = fresh
            except Exception:
                pass
        # 矩形：DWM 可见边界（与运行时 _resolve_target_rect 同口径），
        # _get_window_info 返回的 rect 本就是 DWM 口径 (x,y,w,h)
        try:
            lx, ly, lw, lh = (int(v) for v in best.get("rect"))
        except Exception:
            return None, "", "notfound"
        if lw < 5 or lh < 5:
            return None, "", "notfound"
        params = getattr(node, "params", None) or {}
        exe = str(params.get("target_exe", "") or "").strip()
        cls = str(params.get("target_class", "") or "").strip()
        name = str(best.get("title", "") or "") or exe or cls
        return (lx, ly, lw, lh), f"目标窗口：{name}", "ok"

    # ---------- IfImageDetect / StillnessDetect / FindImage 全屏 SnipOverlay 处理 ----------
    def _on_snip_for_node(self, node_id: str, mode: str):
        """处理 Inspector 发来的截图请求：
        mode ∈ {
            "if_img_add",           # IfImageDetect 追加一张
            "if_img_replace_<idx>", # IfImageDetect 替换第 idx 张
            "if_static_roi",        # StillnessDetect 框选 ROI
            "findimg_target",       # FindImage 截取目标（保存PNG到templates）
        }
        若该节点配置了临时目标窗口，框选范围会被限定在该窗口内。
        """
        if not self._graph or node_id not in self._graph.nodes:
            return
        # findimg_target / if_img_add / if_img_replace 都需要 templates 目录保存文件，
        # 工程未打开时禁止操作（避免 fallback 到全局 APP_DIR 污染 src/）
        if not self._ensure_project_open("裁剪模板保存到工程resource/"):
            return
        try:
            from src.blueprint.ui.snip_overlay import SnipOverlay
        except Exception as e:
            self._log.append_log(f"SnipOverlay 加载失败: {e}（降级为 region_selector）", "WARN")
            return

        # ---- 有了临时目标窗口就以它为框选范围；否则维持全屏 ----
        # 限定区（临时目标窗口）**故意先不解析**：解析过程要把目标窗口提到最前，
        # 若发生在遮罩上屏之前，用户"点完按钮又点了一下屏幕"就会点到那个窗口上。
        # 这里只声明变量，稍后在遮罩已接管输入之后才填值；_on_result 闭包按名引用，
        # 调用时读到的就是填好的值。
        limit_rect, limit_label, limit_status = (None, "", "none")

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
                ox1, oy1, ox2, oy2 = (int(v) for v in bbox)
                x1, y1, x2, y2 = ox1, oy1, ox2, oy2
                # 兜底：再夹一次到临时目标窗口范围（遮罩已夹过，防坐标口径漂移）
                if limit_rect:
                    try:
                        lx, ly, lw, lh = (int(v) for v in limit_rect)
                        x1 = max(x1, lx)
                        y1 = max(y1, ly)
                        x2 = min(x2, lx + lw)
                        y2 = min(y2, ly + lh)
                    except Exception:
                        x1, y1, x2, y2 = ox1, oy1, ox2, oy2
                w = max(0, x2 - x1)
                h = max(0, y2 - y1)
                if w < 5 or h < 5:
                    self._log.append_log("[Snip] 选区太小 (宽<5 或 高<5)，忽略", "WARN")
                    return
                # 夹过之后图片要跟着裁，否则图片与坐标对不上
                if (x1, y1, x2, y2) != (ox1, oy1, ox2, oy2):
                    try:
                        pil = pil.crop((x1 - ox1, y1 - oy1, x2 - ox1, y2 - oy1))
                    except Exception:
                        x1, y1, x2, y2 = ox1, oy1, ox2, oy2
                        w = max(0, x2 - x1)
                        h = max(0, y2 - y1)
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
                # 静止 / 截图 / 文字：roi 一律是 tuple4 (x,y,w,h)（三者同款参数样式）
                if m in ("if_static_roi", "screenshot_roi", "ocr_roi"):
                    # ★ roi 坐标系必须与运行时一致（节点文档：「window 模式相对窗口左上角」）：
                    #   · 框选被限定在目标窗口内（limit_rect 非空）→ 存**相对窗口左上角**；
                    #   · 未限定（fullscreen 模式 / 目标窗口没找到）→ 存**屏幕绝对坐标**。
                    # 运行时 _crop_roi 是对「窗口局部图」裁剪、if_static 回退抓屏还会加窗口偏移，
                    # 所以 window 模式存绝对坐标会让裁剪整体右/下偏移（甚至只剩一条窄边），
                    # 表现就是「窗口里明明有字，测试识别/运行却说没识别到」（2026-10-05 定位）。
                    rx, ry = int(x1), int(y1)
                    if limit_rect:
                        try:
                            rx = int(x1) - int(limit_rect[0])
                            ry = int(y1) - int(limit_rect[1])
                        except Exception:
                            rx, ry = int(x1), int(y1)
                    elif limit_status == "notfound":
                        self._log.append_log(
                            "[Snip] 目标窗口当前没找到，本次 ROI 只能按「屏幕绝对坐标」记录；"
                            "该节点是「目标窗口」模式，等窗口就绪后这份 ROI 会对不上，"
                            "建议窗口打开时重新框选一次 ROI。", "WARN")
                    self._on_param_changed(nid, "roi", (rx, ry, int(w), int(h)))
                    self._on_param_changed(nid, "roi_preview_b64", b64)
                    tag = {"if_static_roi": "[if_static]", "screenshot_roi": "[screenshot]",
                           "ocr_roi": "[ocr]"}.get(m, "[roi]")
                    origin = "窗口相对" if limit_rect else "屏幕绝对"
                    self._log.append_log(
                        f"{tag} ROI框选完成: {origin}({rx},{ry}) {w}×{h}", "SUCCESS"
                    )
                    self._inspector.set_node(node)
                    return
                # FindImage：findimg_target - 保存PNG到该蓝图的 res/ 镜像目录，回填 template 路径 + b64预览
                if m == "findimg_target":
                    try:
                        rel = self._save_node_asset(node, pil, "findimg")
                        if not rel:
                            self._log.append_log("[FindImage] 模板保存失败（未打开工程或写入被拒）", "ERROR")
                            return
                        # 同时写入PIL runtime_attrs缓存
                        try:
                            if not hasattr(node, "runtime_attrs") or node.runtime_attrs is None:
                                node.runtime_attrs = {}
                            node.runtime_attrs["_pil_template"] = pil.copy()
                        except Exception:
                            pass
                        self._on_param_changed(nid, "template", rel)
                        self._on_param_changed(nid, "template_b64", b64)
                        self._log.append_log(
                            f"[FindImage] 截取目标完成: {w}×{h} 已保存到 {rel}", "SUCCESS"
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
                # 参考图同时落盘一份到该蓝图的 res/ 镜像目录：
                #   1) 资源面板里能看到、能清理；2) 导出的 EXE 用全分辨率原图匹配，比 b64 缩略图更准
                rel_img = self._save_node_asset(node, pil, "ifimg")
                if rel_img:
                    new_item["path"] = rel_img
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
                # 刷新 _pil_images 缓存，使运行时编辑后立即生效
                try:
                    from PIL import Image as _PILImage
                    import io as _io
                    new_cache = []
                    for item in arr:
                        b64_str = item.get("b64", "") if isinstance(item, dict) else ""
                        path = item.get("path", "") if isinstance(item, dict) else ""
                        pil_img = None
                        if b64_str:
                            try:
                                pil_img = _PILImage.open(_io.BytesIO(base64.b64decode(b64_str))).convert("RGBA")
                            except Exception:
                                pil_img = None
                        elif path:
                            try:
                                from src.config.app_paths import get_img_path
                                real = get_img_path(path)
                            except Exception:
                                real = path
                            if os.path.exists(real):
                                try:
                                    pil_img = _PILImage.open(real).convert("RGBA")
                                except Exception:
                                    pil_img = None
                        new_cache.append(pil_img)
                    node.runtime_attrs["_pil_images"] = new_cache
                    self._log.append_log(f"[if_img] 已刷新图片缓存({len(new_cache)}张)", "DEBUG")
                except Exception as e3:
                    self._log.append_log(f"[if_img] 缓存刷新失败: {e3}", "WARN")
                self._inspector.set_node(node)
            except Exception as e2:
                self._log.append_log(f"[Snip] 结果处理失败: {e2}", "ERROR")
                traceback.print_exc()

        try:
            # ★ 顺序至关重要（2026-10-04 实测确认的事件穿透）：
            #   ① 先 hide 主窗 + **同步** show 遮罩（delay_ms=0，中间不留任何等待）。
            #      只要存在"遮罩还没上屏"的窗口期，用户点完 📸 又随手点一下屏幕的那次
            #      按下就会落到当前前台窗口上 —— 而截图流程紧接着正好要把「临时目标窗口」
            #      提到最前，于是那一下被目标窗口接走，Windows 连鼠标捕获一起给它，
            #      整段拖拽都变成在目标窗口里框选 / 选中文本。
            overlay = SnipOverlay.prepare_and_run(self, delay_ms=0)
            overlay.finished.connect(_on_result)
            #   ② 遮罩已在屏幕上接管命中测试，现在才去解析目标窗口：提窗、找 hwnd
            #      这些慢动作期间再怎么卡，用户点到的也只是我们的遮罩。
            try:
                node0 = self._graph.nodes.get(node_id)
                if node0 is not None:
                    limit_rect, limit_label, limit_status = self._resolve_snip_limit(node0)
            except Exception as e:
                self._log.append_log(f"[Snip] 解析临时目标窗口失败: {e}", "WARN")
            #   ③ 把限定区回填给遮罩（set_limit 内部会重新置顶一次，抵掉提窗造成的 z 序变化）
            if limit_rect:
                overlay.set_limit(limit_rect, limit_label)
                self._log.append_log(
                    f"[Snip] {limit_label}，框选范围已限定在该窗口内 {limit_rect}", "INFO"
                )
                self._log.append_log(
                    f"[Snip] 请在临时目标窗口内框选矩形（范围 {limit_rect}，ESC取消）...", "INFO"
                )
            else:
                if limit_status == "notfound":
                    self._log.append_log(
                        "[Snip] 节点已配置临时目标窗口，但当前找不到该窗口，本次回退为全屏框选", "WARN"
                    )
                self._log.append_log("[Snip] 请在屏幕上框选矩形（ESC取消）...", "INFO")
        except Exception as e:
            self._log.append_log(f"[Snip] 启动失败: {e}", "ERROR")
            traceback.print_exc()

    # ---------- Mouse 点属性 📍 坐标拾取 ----------
    def _on_pick_coord(self, node_id: str, param_name: str):
        """启动 CoordOverlay 全屏遮罩拾取坐标，根据节点 range_mode 决定保存窗口相对/绝对坐标。"""
        if not self._graph or node_id not in self._graph.nodes:
            return
        node = self._graph.nodes[node_id]

        # FindImage 的 offset 参数：走视觉偏移拾取（以模板中心为基准），不走 CoordOverlay
        if node.type == "FindImage" and param_name == "offset":
            self._start_findimg_offset_pick(node_id)
            return

        range_mode = str(node.params.get("range_mode", "window") or "window")

        # 相对坐标的基准窗口 = 节点自身的「目标窗口」定位条件，
        # 与运行时 _resolve_target_rect 同口径（title子串 / exe子串 / class全等），
        # 解析不到就退绝对坐标模式。
        hwnd = None
        window_rect = None
        exe_name = ""
        if range_mode == "window":
            best, status = self._find_node_target_window(node)
            lx = ly = lw = lh = 0
            if status == "ok" and best is not None:
                try:
                    lx, ly, lw, lh = (int(v) for v in best.get("rect"))
                except Exception:
                    lx = ly = lw = lh = 0
                if lw >= 5 and lh >= 5:
                    window_rect = (lx, ly, lw, lh)
                    hwnd = int(best.get("hwnd") or 0) or None
                    exe_name = str(best.get("exe_name", "") or "")
                    # 把基准窗口提到最前，否则全屏遮罩盖着它，用户点不到
                    if hwnd:
                        try:
                            from src.tools.window_utils import bring_window_to_front
                            bring_window_to_front(hwnd)
                        except Exception:
                            pass
                else:
                    self._log.append_log(
                        f"[📍] 目标窗口尺寸异常({lw}x{lh})，本次按绝对坐标拾取", "WARN"
                    )
            elif status == "notfound":
                self._log.append_log(
                    "[📍] 节点已配置目标窗口，但当前找不到该窗口 → 本次按绝对坐标拾取", "WARN"
                )
            else:
                self._log.append_log(
                    "[📍] 节点未配置「目标窗口」→ 本次按绝对屏幕坐标拾取", "INFO"
                )

        try:
            from src.blueprint.ui.coord_overlay import CoordOverlay
        except Exception as e:
            self._log.append_log(f"CoordOverlay 加载失败: {e}", "ERROR")
            return

        self._log.append_log(
            f"[📍] 坐标拾取启动（{ '窗口相对坐标' if hwnd else '绝对屏幕坐标'}模式），请在屏幕上点击目标位置...",
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
                lambda result, nid=node_id, pn=param_name, cm=range_mode: self._on_coord_picked(nid, pn, cm, result)
            )
        except Exception as e:
            self._log.append_log(f"[📍] CoordOverlay 启动失败: {e}", "ERROR")
            traceback.print_exc()

    def _on_coord_picked(self, node_id: str, param_name: str, range_mode: str, result):
        """CoordOverlay 拾取完成回调：根据 range_mode 保存窗口相对 或 绝对坐标。"""
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
        # range_mode=window 且拿到了基准窗口 → 保存 rel；否则保存 abs
        if range_mode == "window" and result.get("mode") == "bound":
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
        if tpl_pil is None and tpl_path:
            try:
                from src.config.app_paths import get_img_path

                _real = get_img_path(tpl_path)
                if _real and os.path.exists(_real):
                    from PIL import Image as _PILImg
                    tpl_pil = _PILImg.open(_real)
            except Exception:
                tpl_pil = None
        if tpl_pil is None:
            QMessageBox.information(self, "提示", "还没有模板图片，请先点「📸 截取目标」或「📂 导入图片」设置模板。")
            return

        # 2) 隐藏主窗口 → 截全屏
        self._log.append_log("[📍偏移拾取] 正在截取屏幕并匹配模板...", "INFO")
        try:
            from src.tools.crash_blackbox import log as _bb_log
            _bb_log("[hide] 偏移拾取流程隐藏主窗口（若此后遮罩未恢复，窗口会凭空消失）")
        except Exception:
            pass
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

        try:
            overlay = VisualOffsetOverlay.prepare_and_run(
                self,
                screenshot=screenshot_pix,
                template_rect=tpl_qrect,
                delay_ms=100,
            )
        except Exception as e:
            # prepare_and_run 在构造失败时已恢复主窗（见其注释），这里只负责提示；
            # 不包这一层的话异常会抛进 Qt 槽函数，属于未处理异常。
            QMessageBox.warning(self, "偏移拾取启动失败", f"无法启动偏移拾取：\n{e}")
            return
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
    def _on_runner_log(self, msg: str, level: str = "INFO"):
        """运行时日志 → 日志面板。

        顺带把「当前正在执行的节点 id」挂在日志行上，
        这样在日志面板里**双击任意一行**即可把画布定位到对应节点（日志 ↔ 节点 双向定位）。
        """
        nid = ""
        try:
            if self._runner is not None:
                nid = str(getattr(self._runner, "current_node_id", "") or "")
        except Exception:
            nid = ""
        try:
            self._log.append_log(msg, level, node_id=nid or None)
        except TypeError:
            self._log.append_log(msg, level)

    def _stop_and_detach_runner(self):
        """停止正在运行的 runner 并断开所有信号连接。
        统一清理逻辑，确保切换蓝图/工程时不会因旧 runner 信号触发已销毁的对象而崩溃。
        """
        runner = self._runner
        if runner is None:
            return
        self._runner = None  # 先置 None，防止信号回调中再次访问
        # 1) 断开所有信号连接，防止 runner 线程在结束过程中发射信号到已失效的 UI
        try:
            runner.log_message.disconnect()
            runner.highlight_node.disconnect()
            runner.highlight_clear.disconnect()
            runner.progress_step.disconnect()
            runner.finished_ok.disconnect()
            runner.finished_err.disconnect()
            runner.finished.disconnect()
            runner.pause_changed.disconnect()
            runner.notify_toast.disconnect()
            runner.current_node_changed.disconnect()
        except Exception:
            pass
        # 2) 请求停止
        try:
            runner.request_stop()
        except Exception:
            pass
        # 3) 等待线程结束（最多 3 秒，防止 UI 卡死）
        if hasattr(runner, "isRunning") and runner.isRunning():
            try:
                runner.wait(3000)
            except Exception:
                pass
            # 3 秒没停干净（当前节点可能是长等待 / 找图 / 网络，请求停止后要等它跑完手上
            # 这个节点）→ **必须孤儿化**：脱离父对象 + 全局持引用，等线程真正跑完再析构。
            # 否则关闭窗口时会连带析构一个还在跑的 QThread —— Qt 直接 abort，
            # 表现就是「调试中停止/关窗偶发闪退」。
            try:
                if runner.isRunning():
                    _park_orphan_runner(runner)
            except Exception:
                pass

    def _on_run_clicked(self):
        if self._graph is None:
            QMessageBox.warning(self, "无法运行", "当前没有蓝图。")
            return
        if not self._ensure_project_open("运行蓝图（会在工程目录下生成debug缓存"):
            return
        # 把还挂在防抖里的「设置变量」数据出口（变量名 → 端口）立刻对齐一次，
        # 免得「刚敲完变量名就按 F5」时画布/文件里的端口还是旧的。
        try:
            if getattr(self, "_setvar_port_pending", None):
                self._setvar_port_timer.stop()
                self._apply_setvar_output_ports()
        except Exception:
            pass
        if self._runner is not None and self._runner.isRunning():
            self._log.append_log("已有运行中的蓝图，先停止。", "WARN")
            return
        # ★ 运行的是「整张根蓝图」，不是当前打开的那一层子画布（self._graph 在子画布里是子图）。
        #   若正停在循环体/顺序体/并行体里编辑，就先逐层退回根图 —— 退回过程会把每层子图内容
        #   回写进宿主节点，这样运行的图 == 保存出来的图，画布也和实际执行的内容对得上。
        #   不这样做的话：循环体会被当成一张独立蓝图单独跑一遍；并行体里只有 Branch 入口、
        #   没有开始节点，会直接报「没有开始节点」。
        if self._subgraph_stack:
            while self._subgraph_stack:
                self._exit_subgraph()
            self._log.append_log("运行前已自动返回根图：本次运行的是整张蓝图。", "INFO")
        # 1. 静态校验（§九）
        # 传 project_root：组件节点要据此把 blueprint_path 解析成文件去核对端口数
        issues = validate_graph(self._graph, project_root=self._proj_root())
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
        # 1.5 组件蓝图独立运行：没有调用方 → 没有人写 __arg_i → Start 的 param 端口会全空。
        #     先讲清楚，并允许临时填测试值（只注入本次运行，绝不写进蓝图文件）。
        preset_vars = self._collect_component_run_args()
        if preset_vars is None:
            self._log.append_log("已取消运行（组件入参未确认）。", "INFO")
            return
        # 2. 启动 runner（QThread 硬约束 §4）
        # ⚠ 运行期没有「全局目标窗口」：
        #   每个节点的操作范围 100% 由它自己的「范围模式」（目标窗口 / 全屏）+ 目标定位条件决定，
        #   这样「编辑器内点▶运行」与「导出后的挂机 EXE」行为完全一致，
        #   避免出现「编辑器里能跑、导出后找不到图」的口径偏差。
        runner = BlueprintRunner(self._graph, self, initial_variables=preset_vars)
        self._hl_collision_notified = False   # 每次运行重置「撞名高亮已忽略」诊断（只提示一次）
        runner.log_message.connect(self._on_runner_log, Qt.QueuedConnection)
        runner.highlight_node.connect(self._on_runner_highlight, Qt.QueuedConnection)
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
        runner.notify_toast.connect(self._show_notify_toast, Qt.QueuedConnection)
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

    # ---------------- 组件蓝图「独立运行」的入参确认 ----------------
    def _collect_component_run_args(self) -> Optional[dict]:
        """组件蓝图在编辑器里点运行前的入参确认。

        「组件」本质就是一张完整蓝图，所以它自己也能当根图跑。但没有调用方时
        没人往 ctx.variables 写 __arg_i → Start 的 param_0…param_N 全部为空，
        组件内部直接拿这些端口当输入时会静默拿到空值。这里先把话说清楚。

        返回：
          None   → 用户取消，不要运行
          {}     → 继续运行，入参留空
          {...}  → 用临时测试值运行（键 __arg_i，只在本次运行注入，不写回蓝图）
        """
        doc = self._root_graph() or self._graph
        if doc is None:
            return {}
        if str(getattr(doc, "bp_type", "") or "") != bp_kind_mod.BP_TYPE_COMPONENT:
            return {}  # 普通蓝图，不打扰
        start = None
        for nd in (getattr(doc, "nodes", {}) or {}).values():
            if getattr(nd, "type", "") == "Start":
                start = nd
                break
        if start is None:
            return {}
        try:
            n_in = max(0, min(20, int((getattr(start, "params", None) or {}).get("num_params", 0) or 0)))
        except Exception:
            n_in = 0
        if n_in <= 0:
            return {}  # 组件没声明入参，没什么可确认的

        box = QMessageBox(self)
        box.setWindowTitle("组件蓝图 · 独立运行")
        box.setIcon(QMessageBox.Question)
        box.setText(f"这是一张「组件蓝图」，它的 Start 声明了 {n_in} 个入参。")
        box.setInformativeText(
            f"独立运行时没有任何调用方传参，Start 的 param_0 … param_{n_in - 1} 会全部为空；\n"
            "组件内部若直接使用这些端口，拿到的就是空值。\n\n"
            "· 直接运行：入参留空\n"
            "· 填入测试值：临时给本次运行喂几个值，方便单独调试（不写进蓝图文件）"
        )
        btn_test = box.addButton("填入测试值…", QMessageBox.YesRole)
        btn_direct = box.addButton("直接运行（入参为空）", QMessageBox.NoRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_direct)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_test:
            return self._ask_component_arg_values(n_in)
        if clicked is btn_direct:
            return {}
        return None

    def _ask_component_arg_values(self, n_in: int) -> Optional[dict]:
        """弹窗临时填 n_in 个组件入参值。返回 {__arg_i: 文本}；取消返回 None。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("组件入参 · 测试值")
        dlg.resize(460, 0)
        lay = QVBoxLayout(dlg)
        tip = QLabel(
            f"为本次运行临时填入 {n_in} 个入参（留空 = 该入参为空）。\n"
            "这些值不会写进蓝图文件，只在这一次运行里生效。"
        )
        tip.setStyleSheet("color:#8B949E; font-size:11px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        form = QFormLayout()
        edits: List[QLineEdit] = []
        for i in range(n_in):
            ed = QLineEdit()
            ed.setPlaceholderText(f"组件 Start 的 param_{i}")
            form.addRow(f"入参 {i + 1}", ed)
            edits.append(ed)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("开始运行")
        btn_ok.setDefault(True)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)
        result: Dict[str, Any] = {"v": None}

        def _ok():
            result["v"] = {f"__arg_{i}": ed.text() for i, ed in enumerate(edits)}
            dlg.accept()

        btn_ok.clicked.connect(_ok)
        btn_cancel.clicked.connect(dlg.reject)
        if edits:
            edits[0].setFocus()
        dlg.exec()
        return result["v"]

    def _on_runner_highlight(self, node_id, path=None):
        """执行高亮（runner 信号）：先把画布「跟随」到执行点所在的子画布层，再高亮该节点。

        path = 执行点的「容器链」（由 executor 带上）：
          []                  → 根图
          [LoopId]            → 循环体
          [LoopId, ParId]     → 循环体里的并行体
          None                → 不可导航（组件子蓝图内部）→ 不动画布、也**不按裸 id 高亮**，
                                保持上一个高亮（进入组件前调用方已把组件节点点亮）
        为什么要带上 path：节点 id 是「每张图各自编号」的，父图的 1 和循环体里的 1 是同名不同人，
        只给一个裸 id 时画布只能停在当前层 —— 表现就是「高亮不跟着进循环/并行体」。
        """
        try:
            self._follow_exec_path(path)
        except Exception:
            pass
        if path is None:
            # ⚠ 组件子蓝图内部：绝不能按裸 id 高亮。组件是另一张图，它的节点 id 会与
            # 当前画布上的节点同名（比如组件里的 id=4 撞上循环体里的串行节点 id=4），
            # 按 id 点亮就会「明明该走组件、却把串行节点点亮」—— 用户实测踩过的坑。
            # 组件节点本身在进入组件前已被调用方画布点亮，这里保持住即可。
            try:
                items = getattr(self._scene, "_node_items", {}) or {}
                if node_id in items and not getattr(self, "_hl_collision_notified", False):
                    self._hl_collision_notified = True
                    self._log.append_log(
                        f"[调试] 组件内部节点 id={node_id[:8]} 与当前画布节点同名，"
                        f"其高亮已忽略（防串层误亮）。看到本行 = 高亮修复已生效。",
                        "INFO",
                    )
            except Exception:
                pass
            return
        try:
            self._scene.set_running_node(node_id)
        except Exception:
            pass

    def _follow_exec_path(self, path):
        """把画布导航到 path 指定的子画布层（只导航，不改任何蓝图内容）。

        做法：先退到与目标路径的公共前缀，再逐层进入。已经在目标层时什么都不做
        （避免每次执行节点都重建一遍画布）。路径里出现「不是容器」的 id（蓝图被改过）
        就放弃跟随、保持当前层，绝不乱跳。
        """
        if path is None:
            return  # 不可导航（组件子蓝图内部）
        target = [str(x) for x in path]
        # 1) 退到公共前缀：栈本身是 target 的前缀时就停
        guard = 0
        while self._subgraph_stack and guard < 64:
            guard += 1
            cur = [str(it.get("node_id")) for it in self._subgraph_stack]
            if target[: len(cur)] == cur:
                break
            self._exit_subgraph(quiet=True)
        # 2) 逐层进入（只认三种容器节点）
        guard = 0
        while len(self._subgraph_stack) < len(target) and guard < 64:
            guard += 1
            nid = target[len(self._subgraph_stack)]
            node = self._graph.nodes.get(nid) if self._graph is not None else None
            if node is None or node.type not in self._CONTAINER_BODY_KEYS:
                return  # 结构对不上（节点被删/不是容器）→ 放弃跟随，保持当前层
            body_key, kind = self._CONTAINER_BODY_KEYS[node.type]
            body = node.params.get(body_key)
            if not (isinstance(body, dict) and body.get("nodes")):
                body = self._create_empty_subgraph(node, body_key, kind)
            self._enter_subgraph(node, body_key, kind, body, quiet=True)

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
                        curr_path = getattr(self._runner, "current_node_path", None)
                        if isinstance(curr_id, str) and len(curr_id) > 0:
                            if curr_path is None:
                                # 暂停点在「组件子蓝图」内部：绝不能按裸 id 跳画布 ——
                                # 组件是另一张图，它的节点 id 会与当前画布上的节点同名
                                # （比如组件里的 id=4 撞上循环体里的串行节点 id=4），
                                # 跳过去就是「明明停在组件里、画布却定位到串行节点」。
                                # 画布保持在调用方（组件节点本身已被运行高亮点亮）。
                                self._log.append_log(
                                    "📍暂停点在组件内部（画布不跳转防串层误定位）——"
                                    "组件节点亮着，组件内部执行细节看日志 [Node▶] … [子图] 行",
                                    "INFO",
                                )
                            else:
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

    # ================ 通知节点 Toast 浮层 ================
    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._relayout_toasts()
        except Exception:
            pass

    def _relayout_toasts(self):
        """把所有活跃 toast 堆在主窗口右上角，从上往下依次排。"""
        toasts = getattr(self, "_toasts", None)
        if not toasts:
            return
        try:
            geo = self.geometry()
            margin_x = 20
            margin_y = 24
            spacing = 10
            # 从窗口右上角向下铺
            cur_y = geo.y() + margin_y + self.frameGeometry().height() - self.geometry().height() + 16
            # 更简单：用 self.rect() 映射到全局坐标
            top_right_local = self.rect().topRight()
            global_pt = self.mapToGlobal(top_right_local)
            base_x = global_pt.x() - margin_x
            base_y = global_pt.y() + margin_y
            cur_y = base_y
            for t in list(toasts):
                try:
                    # 布局所有 toast（含尚未 show 的，避免遗漏）
                    # 先调大小，让 layout 把高度算出来
                    t.adjustSize()
                    if t.width() < 320:
                        t.resize(320, t.height())
                    w = t.width()
                    h = t.height()
                    t.move(base_x - w, cur_y)
                    cur_y += h + spacing
                except Exception:
                    continue
        except Exception:
            pass

    def _show_notify_toast(self, title: str, body: str, level: str, duration_sec: float):
        """接收 BlueprintRunner 的 notify_toast Signal → 在主窗口右上角弹非阻塞浮层。
        即使蓝图在后台跑 / 用户切到别的窗口，只要主窗口没最小化，也能看得到提示。"""
        try:
            card = _NotifyToastCard(
                self,
                title or "",
                body or "",
                level or "INFO",
                duration_sec if duration_sec and duration_sec > 0 else 3.0,
                on_destroyed_cb=self._on_toast_destroyed,
            )
            self._toasts.append(card)
            # 先计算位置再 show，避免先在默认位置闪现再跳到右上角
            self._relayout_toasts()
            card.show()
            # show 后再修正一次（adjustSize 可能改变高度）
            QTimer.singleShot(0, self._relayout_toasts)
            # 也在日志面板打一行，让最小化窗口时仍可在日志里看到
            try:
                lv = (level or "INFO").upper()
                safe_body = (body or "")
                if len(safe_body) > 120:
                    safe_body = safe_body[:120] + "…"
                self._log.append_log(f"[通知][{lv}] {title or ''} {safe_body}", lv if lv in ("INFO","WARN","ERROR","DEBUG","SUCCESS") else "INFO")
            except Exception:
                pass
        except Exception:
            pass

    def _on_toast_destroyed(self, card):
        try:
            ts = getattr(self, "_toasts", None)
            if ts is not None and card in ts:
                ts.remove(card)
        except Exception:
            pass
        try:
            self._relayout_toasts()
        except Exception:
            pass

    def _on_stop_clicked(self):
        if self._runner is not None and self._runner.isRunning():
            stop_qt = pynput_to_qkeysequence(get_shortcut("stop_blueprint", self._user_settings))
            self._log.append_log(f"■ 请求优雅停止 ({stop_qt})，等待已入队节点完成...", "WARN")
            self._runner.request_stop()
            # 立即禁用停止/暂停/下一步按钮，防止用户在 runner 线程结束前重复点击
            self._act_stop.setEnabled(False)
            self._act_pause.setEnabled(False)
            if hasattr(self, "_act_step"):
                self._act_step.setEnabled(False)
            self._status_run.setText("状态: 正在停止...")
            self._status_run.setStyleSheet("color: #F85149; font-size: 10px; padding: 0 8px;")
        else:
            self._log.append_log("当前没有运行中的蓝图。", "INFO")
            # 兜底：确保按钮状态恢复
            self._act_run.setEnabled(True)
            self._act_pause.setEnabled(False)
            self._act_stop.setEnabled(False)

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
            priority=int(snap.get("priority", 1) or 1),
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

    def _on_scene_edge_priority_changed(self, edge_id: str, old_w: int, new_w: int):
        """右键连线改「优先级权重」：记日志 + 压撤销栈（Ctrl+Z 可还原旧权重）。"""
        if self._applying_undo_redo:
            return
        self._push_undo({
            "kind": "edge_priority",
            "edge_id": edge_id,
            "old_value": int(old_w),
            "new_value": int(new_w),
        })
        self._log.append_log(
            f"⛓ 连线优先级权重：{old_w} → {new_w}"
            f"（同端口 fan-out 时权重大的先执行；权重相同按连线先后）",
            "INFO",
        )

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
                # 级联参数（换模板时自动清掉的特征数据）跟着一起还原
                self._apply_cascade_params(action, "old_values")
                self._redo_stack.append(action)
                self._log.append_log(
                    f"[撤销] 恢复参数：节点 {nid} 的 {pname}",
                    "INFO",
                )
            elif kind == "edge_priority":
                # 撤销"改连线权重"：恢复 old_value
                eid = action.get("edge_id")
                old_v = action.get("old_value")
                edge = self._scene.graph.edges.get(eid) if self._scene and self._scene.graph else None
                if edge is None:
                    self._undo_stack.append(action)
                    self._log.append_log(f"[撤销] 连线 {eid} 已不存在，无法还原它的权重", "WARN")
                    return
                edge.priority = int(old_v)
                ei = self._scene._edge_items.get(eid)
                if ei is not None:
                    ei.update()
                self._redo_stack.append(action)
                self._log.append_log(f"[撤销] 连线权重恢复为 {old_v}（Ctrl+Y 重做）", "INFO")
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
                # 级联参数跟着一起重做
                self._apply_cascade_params(action, "new_values")
                self._undo_stack.append(action)
                self._log.append_log(
                    f"[重做] 重新修改参数：节点 {nid} 的 {pname}",
                    "INFO",
                )
            elif kind == "edge_priority":
                # 重做"改连线权重"：恢复 new_value
                eid = action.get("edge_id")
                new_v = action.get("new_value")
                edge = self._scene.graph.edges.get(eid) if self._scene and self._scene.graph else None
                if edge is None:
                    self._redo_stack.append(action)
                    self._log.append_log(f"[重做] 连线 {eid} 已不存在，无法重做它的权重", "WARN")
                    return
                edge.priority = int(new_v)
                ei = self._scene._edge_items.get(eid)
                if ei is not None:
                    ei.update()
                self._undo_stack.append(action)
                self._log.append_log(f"[重做] 连线权重恢复为 {new_v}（Ctrl+Z 撤销）", "INFO")
            else:
                self._redo_stack.append(action)
                self._log.append_log(f"[重做] 暂不支持重做「{kind}」，后续版本会补齐。", "WARN")
                return
        finally:
            self._applying_undo_redo = False
        self._scene.graph_changed.emit()

    # ==================== 全局热键 ====================
    def closeEvent(self, event):
        """关闭窗口时：先检查蓝图是否有未保存修改 → 再释放 pynput 全局钩子，避免进程残留。"""
        # 黑匣子：记录「窗口被关闭」——若用户没点 X 却出现这行，说明有代码在程序性关窗
        try:
            from src.tools.crash_blackbox import log as _bb_log
            _bb_log("[closeEvent] 主窗口关闭流程触发（用户点X / Alt+F4 / 程序性 close 都会走到这里）")
        except Exception:
            pass
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
                self._stop_and_detach_runner()
        except Exception:
            pass
        try:
            if not self._confirm_discard():
                # 用户选了 Cancel 或保存后仍需保存（例如保存失败又取消丢弃）
                event.ignore()
                return
        except Exception:
            pass
        # 2.5) 记录视图状态（下次打开同一张蓝图接着看）
        try:
            self._persist_view_state()
        except Exception:
            pass
        # 2) 释放全局热键钩子
        try:
            self._hotkey_mgr.unregister()
        except Exception:
            pass
        # 3) 同步用户设置到磁盘（保证设置对话框里改过的热键等下次启动还在）
        # 只把 hotkeys/ui 等主窗口管理的字段合并到磁盘最新内容上，避免覆盖磁盘上的其他设置。
        try:
            from src.config.user_settings import read_user_settings as _rus
            disk = _rus()
            # 只用内存快照中的 hotkeys / ui 字段（这些是 SettingsDialog 管理的）
            if "hotkeys" in self._user_settings:
                disk["hotkeys"] = self._user_settings["hotkeys"]
            if "ui" in self._user_settings:
                disk["ui"] = self._user_settings["ui"]
            save_user_settings(disk)
        except Exception:
            pass
        # 3.5) 孤儿 runner 兜底：停止请求已发出但节点是长等待/找图/网络时，3 秒内停不干净。
        #   再等最多 10 秒；仍没停干净就直接结束进程 —— 退出阶段绝不能让「还在跑的 QThread」
        #   被窗口析构连带销毁（Qt 直接 abort = 关窗闪退）。用户走到这里已确认放弃运行，
        #   运行日志每次 write 都已 flush，无数据可丢。
        try:
            deadline = time.time() + 10.0
            while _ORPHAN_RUNNERS and time.time() < deadline:
                if not any(r.isRunning() for r in list(_ORPHAN_RUNNERS)):
                    break
                time.sleep(0.1)
            if any(r.isRunning() for r in list(_ORPHAN_RUNNERS)):
                os._exit(0)
        except Exception:
            pass
        super().closeEvent(event)

    def _global_hotkey_id_to_callback(self) -> Dict[str, Any]:
        """全局热键ID -> 回调（1-param 0参函数）。对应 user_settings SHORTCUT_REGISTRY 里 scope=global 的项。"""
        return {
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
        """刷新状态栏：按固定顺序显示所有全局热键的 Qt 格式。"""
        parts = []
        gmap = self._global_hotkey_id_to_callback()
        order = {"run_blueprint": 0, "pause_blueprint": 1, "stop_blueprint": 2}
        for key_id in sorted(gmap.keys(), key=lambda k: (order.get(k, 9), k)):
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

    # ==================== 导出为独立 EXE ====================
    def _on_export_clicked(self):
        """工具栏 📦 导出 EXE：把当前蓝图打包成一个可脱离本项目单独运行的 EXE。

        导出逻辑见 src/blueprint/export/（载荷打包）与 src/blueprint/ui/export_dialog.py（界面）。
        产物 = 运行时模板 BlueprintRuntime.exe 的副本 + 尾部追加的「蓝图 + 图片资源」载荷。
        """
        if self._graph is None:
            QMessageBox.warning(self, "无法导出", "当前没有打开任何蓝图。")
            return
        # ★ 导出的必须是「整张根蓝图」：若正处在子画布（循环体/顺序体/并行体）里，
        #   先把各层内容回写进宿主节点、再取根图，否则会把那层子画布当成蓝图导出。
        #   这里只回写、不切换画布 —— 导出不该把用户从子画布弹出去。
        src_graph = self._save_target_graph() or self._graph
        # 取一个友好的蓝图名：优先 graph.name，其次当前文件名
        name = str(getattr(src_graph, "name", "") or "").strip()
        if (not name or name == "Untitled") and self._current_path:
            try:
                _, stem, _ = BlueprintStore._split_bp_path(self._current_path)
                if stem:
                    name = stem
            except Exception:
                pass
        if not name:
            name = "未命名蓝图"
        try:
            project_root = self._pm.project_root
        except Exception:
            project_root = None
        try:
            from src.blueprint.ui.export_dialog import ExportDialog
        except Exception as e:
            QMessageBox.critical(self, "模块加载失败", f"加载导出对话框失败：\n{e}")
            return
        dlg = ExportDialog(self, src_graph, name, project_root=project_root)
        if dlg.exec():
            out_path = getattr(dlg, "_result_path", "")
            if out_path:
                self._log.append_log(f"📦 已导出独立 EXE: {out_path}", "SUCCESS")

    # ==================== 内嵌文档攻略 UI 壳子 ====================
    def _clear_doc_dialog_ref(self):
        try:
            self._doc_dialog_ref = None
        except Exception:
            pass

    def _on_doc_guide_clicked(self):
        """工具栏 📚 文档攻略按钮点击 → 打开/激活内嵌手册对话框（单例，不会开多个）。

        对话框内容 = DocViewerPage Widget（之前做的左树+右Markdown查看器），
        与 FullChatDialog 里复用同一个类，保证样式/文档一致。
        """
        if self._doc_dialog_ref is None or not getattr(
            self._doc_dialog_ref, "isVisible", lambda: False)():
            try:
                from PySide6.QtWidgets import QDialog, QVBoxLayout
                # 文档查看器：蓝图模块通用 UI 组件
                from src.blueprint.ui.doc_viewer import DocViewerPage
            except Exception as e:
                QMessageBox.critical(
                    self, "模块加载失败",
                    f"加载文档攻略查看器失败（UI壳子文件缺失或语法错误）：\n{e}"
                )
                return

            dlg = QDialog(self, Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowMinMaxButtonsHint)
            dlg.setObjectName("DocGuideDialog")
            dlg.setWindowTitle("📚 赫萝蓝图自动化 · 使用手册")
            dlg.resize(1080, 720)
            dlg.setMinimumSize(780, 520)

            # DocViewerPage 自己的样式：清爽文档阅读风（独立 QSS 作用域，不影响主窗口 Holo 主题）
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(18, 18, 18, 18)   # 对话框四周边距：18px，别贴着窗口边框
            lay.setSpacing(0)                         # 内容贴 DocViewerPage 自己的分割线
            viewer = DocViewerPage(dlg)
            viewer.setObjectName("docViewerRoot")
            lay.addWidget(viewer, 1)

            # 关窗自动清引用（防止下一次开又找不到）
            try:
                dlg.destroyed.connect(lambda *_: self._clear_doc_dialog_ref())
            except Exception:
                pass
            self._doc_dialog_ref = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        else:
            # 已经打开了 → 提到前台（含最小化还原）
            try:
                dlg = self._doc_dialog_ref
                dlg.showNormal()
                dlg.raise_()
                dlg.activateWindow()
            except Exception:
                pass


def main() -> int:
    # ============================================================
    # OCR 测试识别 worker 模式：frozen（发布版编辑器 exe）下
    # BlueprintAutomation.exe --bp-ocr-worker <img> <lang> <result>
    # 不启动 GUI，直接跑 OCR 并把 JSON 结果写入 result 文件后退出。
    # 开发模式则直接跑 ocr_test_worker.py 脚本，不走这里。
    # ============================================================
    if "--bp-ocr-worker" in sys.argv:
        try:
            i = sys.argv.index("--bp-ocr-worker")
            from src.blueprint.runtime.ocr_test_worker import main as _worker_main
            _worker_main(sys.argv[i + 1:])
        except Exception:
            pass
        return 0

    # ============================================================
    # 打包 --windowed 模式下 sys.stdout/stderr 为 None，print() 会抛异常
    # 安装一个空的 writer 兜底（开发模式有控制台不受影响）
    # ============================================================
    if sys.platform.startswith("win") and getattr(sys, "frozen", False):
        import io as _io
        if sys.stdout is None:
            sys.stdout = _io.StringIO()
        if sys.stderr is None:
            sys.stderr = _io.StringIO()

    # Windows 控制台默认编码是 GBK/CP936，print 中文时可能乱码（尤其关闭时 flush 缓冲区）
    # 强制 stdout/stderr 使用 UTF-8（打包 --windowed 模式下 sys.stdout 可能为 None，需检查）
    try:
        if sys.platform.startswith("win"):
            if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ============================================================
    # 打包后首次启动：确保 exe 同级（APP_DIR）有 config.json（外置用户配置）
    #   - 必须在任何 read_user_settings / ProjectManager / save_user_settings 之前调用
    # ============================================================
    try:
        from src.config.app_paths import ensure_user_config_file, USER_CONFIG_FILE
        ok = ensure_user_config_file()
        if ok:
            print(f"[BOOT] ✅ 用户配置文件就绪：{USER_CONFIG_FILE}")
        else:
            print(f"[BOOT] ⚠️  无法创建用户配置文件：{USER_CONFIG_FILE}（后续读写可能失败）")
    except Exception as e:
        print(f"[BOOT] ⚠️  ensure_user_config_file 异常：{type(e).__name__}: {e}")

    # ============================================================
    # 崩溃黑匣子（2026-10-07）：历次闪退在 Windows 事件日志里零记录，死因无法回溯。
    # ① faulthandler 把原生崩溃的线程栈写进日志；② 记录退出轨迹，区分
    #    「原生崩溃 / 窗口被关闭 / 进程被外部终止（杀软）」三种死法。
    # ============================================================
    try:
        from src.tools.crash_blackbox import enable as _bb_enable, log as _bb_log, \
            log_exception as _bb_exc
        _bb_enable()
    except Exception:
        _bb_log = _bb_exc = None

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
    # 加载 icon.png：开发模式在项目根目录；打包后优先从 INTERNAL_DIR（_MEIPASS）找
    try:
        def _resolve_icon_png() -> Optional[str]:
            try:
                from src.config.app_paths import INTERNAL_DIR
                internal_png = os.path.join(INTERNAL_DIR, "icon.png")
                if os.path.isfile(internal_png):
                    return internal_png
            except Exception:
                pass
            project_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dev_png = os.path.join(project_root_dir, "icon.png")
            return dev_png if os.path.isfile(dev_png) else None
        icon_png = _resolve_icon_png()
        if icon_png and os.path.isfile(icon_png):
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
    # ============================================================
    # 启动窗口形态：默认自动最大化铺满屏幕（保留标题栏 + 任务栏，用户可随时「还原」。
    # 用最大化而不是 showFullScreen()——真全屏会盖住任务栏且无标题栏，编辑器类工具需要
    # 频繁切窗口/看运行结果，全屏反而碍事）。
    # 关掉方式：config.json → ui.start_maximized = false
    # ============================================================
    try:
        _ui_prefs = (win._user_settings or {}).get("ui") or {}
        _start_maximized = bool(_ui_prefs.get("start_maximized", True))
    except Exception:
        _start_maximized = True  # 读设置失败也要最大化，保持行为一致
    if _start_maximized:
        win.showMaximized()
    else:
        win.show()
    try:
        rc = app.exec()
        if _bb_log is not None:
            _bb_log(f"[EXIT] 事件循环返回 rc={rc!r}（走正常 exec 退出路径）")
        return rc
    except BaseException as e:
        if _bb_exc is not None:
            _bb_exc("main()/app.exec() 抛出异常", e)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
