# -*- coding: utf-8 -*-
"""蓝图独立运行时（导出的 EXE 的入口）。

由编辑器的「导出为独立 EXE」功能使用：把一个通用运行时模板 BlueprintRuntime.exe
复制一份、在末尾追加蓝图载荷（蓝图 JSON + 图片资源 zip），就成了一个可以脱离本
项目单独双击运行的挂机程序。

启动流程：
    1. 读取自身 exe 末尾的载荷（开发/调试时可用环境变量 BP_RUNTIME_PAYLOAD 指定文件）
    2. 解包到 %LOCALAPPDATA%/BlueprintRuntime/<载荷指纹>/，把它当成"伪工程根"，
       注册给 app_paths，使 resource/cache/debug 等目录可用
    3. 把蓝图 JSON 里的 @@BPASSET:n@@ 记号还原成解包目录下的真实图片路径
    4. 弹出控制面板窗口：**默认不自动运行**，由用户点「▶ 开始」或按 F5 启动；
       全局热键 F5 开始 / F6 暂停继续 / F7 停止 / F12 紧急退出。
"""

import os
import sys
import json
import hashlib
import traceback

_THIS = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS))

# 开发模式：把项目根放进 sys.path，才能 import src.blueprint.*
if not getattr(sys, "frozen", False) and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# OCR（可选）：库（paddle 等）已随模板打包进 EXE（PyInstaller 收集自 .venv）；
# 模型仍外挂在 exe 同级的 ocr_runtime/models（蓝图含「文字」节点时由导出流程携带）。
# 模型路径由 src/blueprint/runtime/ocr_engine.get_runtime_dir() 解析（含中文路径的
# ASCII 视图规避，见 app_paths.ocr_runtime_view_dir）。


# ============================================================
# DPI 感知：必须早于 PySide6 / pynput / pyautogui 初始化
# ============================================================
if os.name == "nt":
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QCheckBox,
    QMessageBox,
    QSystemTrayIcon,
    QMenu,
)

from src.blueprint.export.overlay import read_payload
from src.blueprint.export.payload import extract_to, restore_blueprint_dict
from src.blueprint.core.models import BlueprintGraph
from src.blueprint.runtime.executor import BlueprintRunner
from src.blueprint.runtime.hotkey_manager import HotkeyManager
from src.config import app_paths

APP_TITLE = "蓝图独立运行"
ENV_PAYLOAD = "BP_RUNTIME_PAYLOAD"
PREFS_FILE = "runtime_prefs.json"

_SUB_DIRS = ("resource", "cache", "debug", "assets")

# 停止超时的孤儿 runner（防「窗口销毁连带析构在跑的 QThread → abort 闪退」，见 _quit）
_ORPHAN_RUNNERS = []

# 状态色（浅色主题）
_C_READY = "#57606A"
_C_RUNNING = "#1F883D"
_C_PAUSED = "#9A6700"
_C_ERROR = "#CF222E"

_STYLE = """
QWidget#RuntimeRoot { background: #F6F8FA; }
QLabel { color: #24292F; font-size: 12px; }
QLabel#NameLabel { color: #57606A; font-size: 12px; }
QLabel#StatusLabel { font-size: 13px; font-weight: 500; }
QLabel#HintLabel { color: #6E7781; font-size: 11px; }
QPlainTextEdit {
    background: #FFFFFF; color: #24292F;
    border: 1px solid #D0D7DE; border-radius: 6px;
    font-family: Consolas, "Courier New", monospace; font-size: 11px;
    padding: 6px;
}
QCheckBox { color: #24292F; font-size: 12px; spacing: 6px; }
QPushButton {
    background: #FFFFFF; color: #24292F;
    border: 1px solid #D0D7DE; border-radius: 6px;
    padding: 6px 14px; font-size: 13px;
}
QPushButton:hover:enabled { background: #F3F4F6; border-color: #B6BEC7; }
QPushButton:pressed:enabled { background: #E0E5EA; border-color: #9AA4AE; }
QPushButton:disabled { background: #F0F2F4; color: #9BA3AC; border-color: #E4E8EC; }
QPushButton[kind="primary"] {
    background: #1F883D; color: #FFFFFF; border-color: #1A7F37;
}
QPushButton[kind="primary"]:hover:enabled { background: #1A7F37; border-color: #166B2E; }
QPushButton[kind="primary"]:pressed:enabled { background: #166B2E; border-color: #125C27; }
QPushButton[kind="primary"]:disabled {
    background: #94D3A2; color: #F0FBF3; border-color: #94D3A2;
}
QPushButton[kind="danger"] {
    background: #CF222E; color: #FFFFFF; border-color: #B81E29;
}
QPushButton[kind="danger"]:hover:enabled { background: #B81E29; border-color: #A11A24; }
QPushButton[kind="danger"]:pressed:enabled { background: #A11A24; border-color: #8C1620; }
QPushButton[kind="danger"]:disabled {
    background: #E9A2A8; color: #FDF2F3; border-color: #E9A2A8;
}
"""


# ============================================================
# 载荷定位与解包
# ============================================================
def _read_payload() -> bytes:
    """按优先级找载荷：环境变量 → 自身 exe 尾部 → 同目录 .bppayload。"""
    env_path = os.environ.get(ENV_PAYLOAD)
    if env_path and os.path.isfile(env_path):
        with open(env_path, "rb") as f:
            return f.read()

    exe_path = sys.executable if getattr(sys, "frozen", False) else _THIS
    data = read_payload(exe_path)
    if data:
        return data

    # 开发/调试便利：允许把载荷放在同目录的 <同名>.bppayload
    side = os.path.join(
        os.path.dirname(exe_path),
        os.path.splitext(os.path.basename(exe_path))[0] + ".bppayload",
    )
    if os.path.isfile(side):
        with open(side, "rb") as f:
            return f.read()

    return b""


def _work_dir(payload: bytes) -> str:
    """解包目录：%LOCALAPPDATA%/BlueprintRuntime/<指纹>/（同载荷复用同一目录）。"""
    digest = hashlib.sha1(payload).hexdigest()[:16]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "BlueprintRuntime", digest)


def _make_graph_loader(work_dir: str, manifest: dict, bp_path: str):
    """返回一个「重新构造蓝图对象」的工厂。

    每次「开始 / 重新开始」都从磁盘重新加载一次，保证拿到的是**全新的图**，
    不会带上上一轮运行残留的节点内部状态。
    """

    def load() -> BlueprintGraph:
        with open(bp_path, "r", encoding="utf-8") as f:
            bp_text = f.read()
        data = restore_blueprint_dict(bp_text, work_dir, manifest)
        return BlueprintGraph.from_dict(data)

    return load


def _prepare(payload: bytes):
    """解包载荷 → 注册伪工程根 → 返回 (loader, manifest, work_dir, name)。"""
    work_dir = _work_dir(payload)
    os.makedirs(work_dir, exist_ok=True)
    for sub in _SUB_DIRS:
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    manifest = extract_to(payload, work_dir)

    # 伪工程根：app_paths 不认识"工程"，这里用一个固定目录冒充，保证四个子目录可用
    def _provider(key: str):
        if key == "root":
            # 蓝图里的资源路径可能是「相对工程根」的（新式 resource/... 写法）。
            # 导出的载荷会把它们替换成解包后的绝对路径，这里只是兜底解析。
            return work_dir
        mapping = {
            "assets": "resource",
            "cache": "cache",
            "debug": "debug",
        }
        sub = mapping.get(key)
        if not sub:
            return None
        p = os.path.join(work_dir, sub)
        return p if os.path.isdir(p) else None

    app_paths.set_project_path_provider(_provider)

    bp_file = str(manifest.get("blueprint_file") or "blueprint.bp.json")
    bp_path = os.path.join(work_dir, bp_file)

    loader = _make_graph_loader(work_dir, manifest, bp_path)
    graph = loader()  # 启动即校验一次，蓝图有问题在这里就报出来

    name = str(
        manifest.get("blueprint_name")
        or getattr(graph, "name", "")
        or "未命名蓝图"
    )
    return loader, manifest, work_dir, name


def _make_icon() -> QIcon:
    """代码画一个图标，避免依赖外部图片资源。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QBrush(QColor("#1F6FEB")))
        p.setPen(Qt.NoPen)
        p.drawEllipse(6, 6, 52, 52)
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(22, 22, 20, 20)
    finally:
        p.end()
    return QIcon(pm)


# ============================================================
# 控制面板窗口
# ============================================================
class RuntimeWindow(QWidget):
    # 跨线程信号：pynput 监听线程 → Qt 主线程
    sig_log = Signal(str, str)
    sig_hotkey = Signal(str)

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"

    def __init__(self, loader, manifest: dict, work_dir: str, name: str):
        super().__init__()
        self._loader = loader
        self._manifest = manifest
        self._work_dir = work_dir
        self._name = name

        self._runner = None
        self._run_seq = 0
        self._state = self.IDLE
        self._pending_restart = False

        self._hotkeys = HotkeyManager(
            dispatch=lambda hid: self.sig_hotkey.emit(hid),
            log_fn=lambda m, l="INFO": self.sig_log.emit(m, l),
        )

        self._prefs = self._load_prefs()

        self._build_ui()
        self._apply_state()

        self.sig_log.connect(self._append, Qt.QueuedConnection)
        self.sig_hotkey.connect(self._on_hotkey, Qt.QueuedConnection)

        # 热键在窗口建好后立刻注册：这样还没点「开始」时 F5 也能启动
        self._hotkeys.start()

    # ---------------- 偏好（记住"启动后自动运行"） ----------------
    def _prefs_path(self) -> str:
        return os.path.join(self._work_dir, PREFS_FILE)

    def _load_prefs(self) -> dict:
        try:
            with open(self._prefs_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_prefs(self):
        try:
            with open(self._prefs_path(), "w", encoding="utf-8") as f:
                json.dump(
                    {"auto_start": bool(self._chk_auto.isChecked())},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

    # ---------------- UI ----------------
    def _mk_btn(self, text: str, slot, kind: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(34)
        b.setMinimumWidth(96)
        if kind:
            b.setProperty("kind", kind)
        b.clicked.connect(slot)
        return b

    def _build_ui(self):
        self.setObjectName("RuntimeRoot")
        self.setWindowTitle(f"{self._name} · {APP_TITLE}")
        self.setWindowIcon(_make_icon())
        self.resize(640, 430)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._status = QLabel("就绪")
        self._status.setObjectName("StatusLabel")
        head.addWidget(self._status)
        head.addStretch(1)
        name_lbl = QLabel(self._name)
        name_lbl.setObjectName("NameLabel")
        head.addWidget(name_lbl)
        root.addLayout(head)

        hint = QLabel("快捷键：" + "　·　".join(self._hotkeys.labels))
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        root.addWidget(self._log, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._btn_start = self._mk_btn("▶ 开始", self._on_start_clicked, "primary")
        self._btn_pause = self._mk_btn("⏸ 暂停", self._on_toggle_pause)
        self._btn_stop = self._mk_btn("⏹ 停止", self._on_stop_clicked, "danger")
        self._btn_restart = self._mk_btn("🔄 重新开始", self._on_restart_clicked)
        for b in (self._btn_start, self._btn_pause, self._btn_stop, self._btn_restart):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        bar2.setSpacing(8)
        self._chk_auto = QCheckBox("启动后自动运行")
        self._chk_auto.setChecked(bool(self._prefs.get("auto_start")))
        self._chk_auto.setToolTip(
            "勾选后，双击这个程序会直接开始跑（适合挂机）。\n"
            "取消勾选则停在就绪状态，等你点「▶ 开始」或按 F5。"
        )
        self._chk_auto.toggled.connect(lambda _=False: self._save_prefs())
        bar2.addWidget(self._chk_auto)
        bar2.addStretch(1)
        bar2.addWidget(self._mk_btn("最小化到托盘", self._hide_to_tray))
        root.addLayout(bar2)

        # 样式最后设，确保 [kind="..."] 属性选择器在 polish 时可见
        self.setStyleSheet(_STYLE)

        self._setup_tray()

    def _setup_tray(self):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = QSystemTrayIcon(_make_icon(), self)
            menu = QMenu()
            act_show = QAction("显示控制面板", self)
            act_show.triggered.connect(self._show_from_tray)
            menu.addAction(act_show)
            menu.addSeparator()
            act_start = QAction("▶ 开始", self)
            act_start.triggered.connect(self._on_start_clicked)
            menu.addAction(act_start)
            act_pause = QAction("⏸ 暂停 / ▶ 继续", self)
            act_pause.triggered.connect(self._on_toggle_pause)
            menu.addAction(act_pause)
            act_stop = QAction("⏹ 停止", self)
            act_stop.triggered.connect(self._on_stop_clicked)
            menu.addAction(act_stop)
            menu.addSeparator()
            act_quit = QAction("退出程序", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_quit)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(self._on_tray_activated)
            self._tray.setToolTip(f"{self._name} · {APP_TITLE}")
            self._tray.show()
        except Exception:
            self._tray = None

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray(self):
        if self._tray is not None:
            self.hide()
            try:
                self._tray.showMessage(
                    APP_TITLE,
                    "已最小化到托盘，蓝图继续运行中。",
                    QSystemTrayIcon.Information,
                    3000,
                )
            except Exception:
                pass
        else:
            self.showMinimized()

    # ---------------- 状态机 ----------------
    def _apply_state(self):
        running = self._state in (self.RUNNING, self.PAUSED)
        self._btn_start.setEnabled(self._state in (self.IDLE, self.FINISHED))
        self._btn_pause.setEnabled(running)
        self._btn_pause.setText("▶ 继续" if self._state == self.PAUSED else "⏸ 暂停")
        self._btn_stop.setEnabled(running)
        self._btn_restart.setEnabled(self._state in (self.RUNNING, self.PAUSED, self.FINISHED))

    def _set_status(self, text: str, color: str):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color}; font-size:13px; font-weight:500;")

    # ---------------- 启动后自动运行 ----------------
    def maybe_autostart(self):
        if self._chk_auto.isChecked():
            QTimer.singleShot(400, self._on_start_clicked)

    # ---------------- 运行控制 ----------------
    def _start_run(self):
        if self._runner is not None and self._runner.isRunning():
            self._append("已有蓝图在运行，忽略本次「开始」。", "WARN")
            return

        try:
            graph = self._loader()
        except Exception as e:
            self._append(f"❌ 加载蓝图失败：{e}", "ERROR")
            self._set_status("加载失败", _C_ERROR)
            QMessageBox.critical(self, APP_TITLE, f"无法加载蓝图：\n{e}")
            return

        self._run_seq += 1
        token = self._run_seq
        self._pending_restart = False

        runner = BlueprintRunner(graph, self)
        runner.log_message.connect(self._append)
        runner.progress_step.connect(lambda step, ntype, t=token: self._on_progress(t, step, ntype))
        runner.finished_ok.connect(lambda t=token: self._on_finished_ok(t))
        runner.finished_err.connect(lambda err, t=token: self._on_finished_err(t, err))
        runner.finished.connect(lambda t=token: self._on_runner_finished(t))
        runner.pause_changed.connect(lambda paused, t=token: self._on_pause_changed(t, paused))
        self._runner = runner

        self._state = self.RUNNING
        self._apply_state()
        self._set_status("运行中 · 启动…", _C_RUNNING)

        self._append(f"▶ 开始运行蓝图：{self._name}", "INFO")
        self._append(f"  工作目录：{self._work_dir}", "INFO")
        runner.start()

    def _on_start_clicked(self):
        if self._runner is not None and self._runner.isRunning():
            self._append("已有蓝图在运行，忽略本次「开始」。", "WARN")
            return
        self._start_run()

    def _on_toggle_pause(self):
        r = self._runner
        if r is None or not r.isRunning():
            self._append("当前没有运行中的蓝图，无法暂停 / 继续。", "WARN")
            return
        paused = r.toggle_pause()
        self._state = self.PAUSED if paused else self.RUNNING
        self._apply_state()
        if paused:
            self._set_status("已暂停 · 按 F6 继续", _C_PAUSED)
            self._append("⏸ 已暂停（不丢状态）。", "WARN")
        else:
            self._set_status("运行中…", _C_RUNNING)
            self._append("▶ 已继续。", "INFO")

    def _on_stop_clicked(self):
        r = self._runner
        if r is None or not r.isRunning():
            self._append("当前没有运行中的蓝图。", "INFO")
            return
        self._append("⏹ 请求停止，等待当前节点跑完…", "WARN")
        self._set_status("正在停止…", _C_PAUSED)
        self._btn_stop.setEnabled(False)
        self._btn_pause.setEnabled(False)
        self._btn_restart.setEnabled(False)
        try:
            r.request_stop()
        except Exception:
            pass

    def _on_restart_clicked(self):
        r = self._runner
        if r is not None and r.isRunning():
            self._pending_restart = True
            self._append("🔄 重新开始：先停止当前运行，随后从头开始…", "WARN")
            self._set_status("正在停止（随后重新开始）…", _C_PAUSED)
            self._btn_restart.setEnabled(False)
            try:
                r.request_stop()
            except Exception:
                pass
            QTimer.singleShot(6000, self._restart_guard)
            return
        self._start_run()

    def _restart_guard(self):
        """重新开始等太久时的兜底提示（不强制杀线程，只告诉用户怎么办）。"""
        if self._pending_restart and self._runner is not None and self._runner.isRunning():
            self._append("⚠ 当前节点迟迟没有结束，重新开始被挂起；可点「⏹ 停止」或按 F12 紧急退出。", "WARN")

    def _on_runner_finished(self, token: int):
        if token != self._run_seq:
            return
        old = self._runner
        self._runner = None
        if old is not None:
            try:
                old.deleteLater()
            except Exception:
                pass

        if self._pending_restart:
            self._pending_restart = False
            self._state = self.IDLE
            self._apply_state()
            self._append("🔄 已停止，即将重新开始…", "INFO")
            QTimer.singleShot(300, self._start_run)
            return

        self._state = self.FINISHED
        self._apply_state()
        self._set_status("已结束 · 可点「▶ 开始」或按 F5 再跑一次", _C_READY)

    # ---------------- 信号回调 ----------------
    def _on_hotkey(self, hotkey_id: str):
        if hotkey_id == "start":
            self._on_start_clicked()
        elif hotkey_id == "pause":
            self._on_toggle_pause()
        elif hotkey_id == "stop":
            self._on_stop_clicked()

    def _on_progress(self, token: int, step: int, ntype: str):
        if token != self._run_seq:
            return
        if self._state == self.PAUSED:
            return
        self._set_status(f"运行中 · Step {step} · {ntype}", _C_RUNNING)

    def _on_pause_changed(self, token: int, paused: bool):
        if token != self._run_seq:
            return
        self._state = self.PAUSED if paused else self.RUNNING
        self._apply_state()
        if paused:
            self._set_status("已暂停 · 按 F6 继续", _C_PAUSED)
        else:
            self._set_status("运行中…", _C_RUNNING)

    def _on_finished_ok(self, token: int):
        if token != self._run_seq:
            return
        self._append("✅ 蓝图执行完成。", "SUCCESS")

    def _on_finished_err(self, token: int, err: str):
        if token != self._run_seq:
            return
        self._append(f"❌ 蓝图执行异常：{err}", "ERROR")
        self._set_status("执行异常", _C_ERROR)

    def _append(self, msg: str, level: str = "INFO"):
        try:
            self._log.appendPlainText(f"[{level}] {msg}")
        except Exception:
            pass

    def _quit(self):
        try:
            self._hotkeys.stop()
        except Exception:
            pass
        r = self._runner
        try:
            if r is not None and r.isRunning():
                r.request_stop()
                r.wait(3000)
                # 3 秒还没停干净（当前节点是长等待/找图/网络，要等它跑完手上这个节点）
                # → 孤儿化脱离父对象，绝不能让窗口销毁连带析构还在跑的 QThread
                #（Qt 直接 abort = 退出时闪退）；然后立即结束进程，避免僵尸残留。
                # 走到这里说明用户已确认「停止并退出」，运行日志每次 write 都已 flush，无数据可丢。
                if r.isRunning():
                    try:
                        r.setParent(None)
                        r.finished.connect(r.deleteLater)
                        _ORPHAN_RUNNERS.append(r)
                    except Exception:
                        pass
                    os._exit(0)
        except Exception:
            pass
        QApplication.quit()

    # ---------------- 生命周期 ----------------
    def closeEvent(self, event):
        running = self._runner is not None and self._runner.isRunning()
        if running:
            ret = QMessageBox.question(
                self,
                APP_TITLE,
                "蓝图仍在运行，确定要停止并退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        self._quit()
        event.accept()


# ============================================================
def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BlueprintRuntime")
    app.setQuitOnLastWindowClosed(False)
    try:
        app.setStyle("Fusion")  # 统一控件外观，保证按钮 hover/pressed 反馈一致
    except Exception:
        pass

    payload = _read_payload()
    if not payload:
        QMessageBox.information(
            None,
            APP_TITLE,
            "这是一个「蓝图运行时」模板程序，本身没有携带蓝图。\n\n"
            "请在「赫萝蓝图自动化」编辑器里打开蓝图后，使用「📦 导出为独立 EXE」生成可直接运行的程序。",
        )
        return 2

    try:
        loader, manifest, work_dir, name = _prepare(payload)
    except Exception as e:
        QMessageBox.critical(
            None,
            "启动失败",
            f"无法加载蓝图载荷：\n{e}\n\n{traceback.format_exc()}",
        )
        return 3

    # 安全提示（2026-10-07 免责）：导出的 EXE 会被分发给他人运行，含「执行命令」
    # 节点的蓝图会以**运行者本人的 Windows 权限**原样执行命令。启动时检测到就
    # 明确告知一次——⚠ 接收者手里没有编辑器，没法「去编辑器里审查」，所以把
    # 将要执行的命令原文直接列进弹窗详细信息里，让他当场看、当场决定跑不跑。
    try:
        from src.blueprint.runtime.validator import scan_command_nodes
        _probe_graph = loader()
        _cmds = scan_command_nodes(_probe_graph)
        if _cmds:
            _detail = "\n".join(
                f"[{i + 1}] 节点 {nid}：\n{cmd}\n"
                for i, (nid, cmd) in enumerate(_cmds)
            )
            _box = QMessageBox(QMessageBox.Warning, "安全提示", "")
            _box.setText(
                f"该蓝图包含 {len(_cmds)} 个「执行命令」节点，运行时将以你的 Windows "
                f"用户权限原样执行其中的命令（命令支持变量插值，实际内容可能随变量变化）。\n\n"
                f"请确认本程序来自你信任的人。点开下方「显示详细信息(Show Details)」"
                f"可查看将要执行的命令原文；来源不明或内容可疑的，请勿运行。"
            )
            _box.setDetailedText(_detail)
            _box.exec()
    except Exception:
        pass

    win = RuntimeWindow(loader, manifest, work_dir, name)
    win.show()
    win.maybe_autostart()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
