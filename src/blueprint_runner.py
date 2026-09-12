# -*- coding: utf-8 -*-
"""蓝图独立运行时（导出的 EXE 的入口）。

由编辑器的「导出为独立 EXE」功能使用：把一个通用运行时模板 BlueprintRuntime.exe
复制一份、在末尾追加蓝图载荷（蓝图 JSON + 图片资源 zip），就成了一个可以脱离本
项目单独双击运行的挂机程序。

启动流程：
    1. 读取自身 exe 末尾的载荷（开发/调试时可用环境变量 BP_RUNTIME_PAYLOAD 指定文件）
    2. 解包到 %LOCALAPPDATA%/BlueprintRuntime/<载荷指纹>/，把它当成"伪工程根"，
       注册给 app_paths，使 templates/screenshots/cache/debug 等目录可用
    3. 把蓝图 JSON 里的 @@BPASSET:n@@ 记号还原成解包目录下的真实图片路径
    4. 起一个控制面板窗口 + 全局热键（F8 优雅停止 / F6 紧急退出），开始执行蓝图
"""

import os
import sys
import hashlib
import traceback

_THIS = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS))

# 开发模式：把项目根放进 sys.path，才能 import src.blueprint.*
# 打包后（frozen）依赖已经内置，无需处理。
if not getattr(sys, "frozen", False) and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


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


from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
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

_SUB_DIRS = ("templates", "screenshots", "cache", "debug", "assets")


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


def _prepare(payload: bytes):
    """解包载荷 → 注册伪工程根 → 加载蓝图。返回 (graph, manifest, work_dir, name)。"""
    work_dir = _work_dir(payload)
    os.makedirs(work_dir, exist_ok=True)
    for sub in _SUB_DIRS:
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    manifest = extract_to(payload, work_dir)

    # 伪工程根：app_paths 不认识"工程"，这里用一个固定目录冒充，保证四个子目录可用
    def _provider(key: str):
        mapping = {"screenshot": "screenshots", "template": "templates", "cache": "cache", "debug": "debug"}
        sub = mapping.get(key)
        if not sub:
            return None
        p = os.path.join(work_dir, sub)
        return p if os.path.isdir(p) else None

    app_paths.set_project_path_provider(_provider)

    bp_file = str(manifest.get("blueprint_file") or "blueprint.bp.json")
    bp_path = os.path.join(work_dir, bp_file)
    with open(bp_path, "r", encoding="utf-8") as f:
        bp_text = f.read()

    # 关键一步：把 @@BPASSET:n@@ 还原成解包目录下的真实图片路径
    data = restore_blueprint_dict(bp_text, work_dir, manifest)
    graph = BlueprintGraph.from_dict(data)

    name = str(manifest.get("blueprint_name") or data.get("metadata", {}).get("name") or "未命名蓝图")
    return graph, manifest, work_dir, name


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
    def __init__(self, graph: BlueprintGraph, manifest: dict, work_dir: str, name: str):
        super().__init__()
        self._graph = graph
        self._manifest = manifest
        self._work_dir = work_dir
        self._name = name
        self._runner = None
        self._pause_btn = None
        self._stop_btn = None
        self._status = None
        self._log = None
        self._tray = None

        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.setWindowTitle(f"{self._name} · {APP_TITLE}")
        self.setWindowIcon(_make_icon())
        self.resize(560, 360)

        root = QVBoxLayout(self)

        head = QHBoxLayout()
        self._status = QLabel("准备中…")
        self._status.setStyleSheet("font-size:13px; font-weight:600; color:#1F6FEB;")
        head.addWidget(self._status)
        head.addStretch(1)
        root.addLayout(head)

        hint = QLabel("F8 = 优雅停止（跑完当前节点后退出）　F6 = 紧急退出")
        hint.setStyleSheet("color:#8B949E; font-size:11px;")
        root.addWidget(hint)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setStyleSheet("font-family:Consolas,monospace; font-size:11px;")
        root.addWidget(self._log, 1)

        bar = QHBoxLayout()
        self._pause_btn = QPushButton("⏸ 暂停")
        self._pause_btn.clicked.connect(self._on_toggle_pause)
        self._pause_btn.setEnabled(False)
        bar.addWidget(self._pause_btn)

        self._stop_btn = QPushButton("⏹ 停止")
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        bar.addWidget(self._stop_btn)

        bar.addStretch(1)
        hide_btn = QPushButton("最小化到托盘")
        hide_btn.clicked.connect(self._hide_to_tray)
        bar.addWidget(hide_btn)
        root.addLayout(bar)

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
            act_stop = QAction("停止运行", self)
            act_stop.triggered.connect(self._on_stop)
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
                self._tray.showMessage(APP_TITLE, "已最小化到托盘，蓝图继续运行中。", QSystemTrayIcon.Information, 3000)
            except Exception:
                pass
        else:
            self.showMinimized()

    # ---------------- 运行控制 ----------------
    def autostart(self):
        QTimer.singleShot(300, self._start_run)

    def _start_run(self):
        self._append(f"▶ 开始运行蓝图：{self._name}", "INFO")
        self._append(f"  解包目录：{self._work_dir}", "INFO")
        runner = BlueprintRunner(self._graph, self, initial_hwnd=None)
        runner.log_message.connect(self._append)
        runner.progress_step.connect(
            lambda step, ntype: self._status.setText(f"运行中 · Step {step} · {ntype}")
        )
        runner.finished_ok.connect(self._on_finished_ok)
        runner.finished_err.connect(self._on_finished_err)
        runner.finished.connect(self._on_finished)
        runner.pause_changed.connect(self._on_pause_changed)
        self._runner = runner
        self._status.setText("运行中…")
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        runner.start()

        # 全局热键：F8 优雅停止 / F6 紧急退出
        self._hotkeys = HotkeyManager(on_stop=self._on_stop, log_fn=self._append)
        self._hotkeys.start()

    def _on_toggle_pause(self):
        if self._runner is None:
            return
        paused = self._runner.toggle_pause()
        self._pause_btn.setText("▶ 继续" if paused else "⏸ 暂停")

    def _on_stop(self):
        if self._runner is None:
            return
        self._append("⏹ 请求停止…", "WARN")
        try:
            self._runner.request_stop()
        except Exception:
            pass

    def _on_pause_changed(self, paused: bool):
        if self._pause_btn is not None:
            self._pause_btn.setText("▶ 继续" if paused else "⏸ 暂停")

    def _on_finished_ok(self):
        self._append("✅ 蓝图执行完成。", "SUCCESS")

    def _on_finished_err(self, err: str):
        self._append(f"❌ 蓝图执行异常：{err}", "ERROR")

    def _on_finished(self):
        self._status.setText("已停止")
        if self._pause_btn is not None:
            self._pause_btn.setEnabled(False)
        if self._stop_btn is not None:
            self._stop_btn.setEnabled(False)
        try:
            if getattr(self, "_hotkeys", None) is not None:
                self._hotkeys.stop()
        except Exception:
            pass

    def _append(self, msg: str, level: str = "INFO"):
        try:
            self._log.appendPlainText(f"[{level}] {msg}")
        except Exception:
            pass

    def _quit(self):
        try:
            if self._runner is not None and self._runner.isRunning():
                self._runner.request_stop()
                self._runner.wait(2000)
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
        graph, manifest, work_dir, name = _prepare(payload)
    except Exception as e:
        QMessageBox.critical(
            None,
            "启动失败",
            f"无法加载蓝图载荷：\n{e}\n\n{traceback.format_exc()}",
        )
        return 3

    win = RuntimeWindow(graph, manifest, work_dir, name)
    win.show()
    win.autostart()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
