# -*- coding: utf-8 -*-
"""「导出为独立 EXE」对话框。

列出当前蓝图会被打包的图片资源、提示缺失项，选择输出路径，
然后调用 src.blueprint.export.exporter 完成导出。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from PySide6.QtCore import Qt, QStandardPaths
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from src.blueprint.export import exporter


def _fmt_size(n: Any) -> str:
    try:
        v = float(n)
    except Exception:
        return "-"
    if v < 1024:
        return f"{int(v)} B"
    for unit in ("KB", "MB", "GB"):
        v /= 1024.0
        if v < 1024 or unit == "GB":
            return f"{v:.1f} {unit}"
    return f"{v:.1f} GB"


def _safe_filename(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "-_ ") else "_" for ch in (name or "")).strip()
    return out or "blueprint"


class ExportDialog(QDialog):
    def __init__(self, parent, graph, blueprint_name: str, project_root: Optional[str] = None):
        super().__init__(parent)
        self._graph = graph
        self._name = blueprint_name or getattr(graph, "name", "") or "未命名蓝图"
        self._project_root = project_root
        self._result_path = ""
        self._tpl = exporter.template_info()

        self.setWindowTitle("📦 导出为独立 EXE")
        self.setMinimumWidth(640)
        self._build_ui()
        self._scan_resources()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("把当前蓝图打包成一个可以脱离本项目、单独双击运行的 EXE")
        title.setStyleSheet("font-size:13px; font-weight:600; color:#1F6FEB;")
        root.addWidget(title)

        sub = QLabel(
            "导出的程序会自动带上蓝图引用到的图片资源，运行时不再依赖本项目的工程目录。"
        )
        sub.setStyleSheet("color:#8B949E; font-size:11px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("蓝图名称："))
        self._name_lbl = QLabel(self._name)
        self._name_lbl.setStyleSheet("font-weight:600;")
        name_row.addWidget(self._name_lbl)
        name_row.addStretch(1)
        root.addLayout(name_row)

        root.addWidget(QLabel("打包的图片资源："))
        self._assets_view = QPlainTextEdit()
        self._assets_view.setReadOnly(True)
        self._assets_view.setMinimumHeight(150)
        self._assets_view.setStyleSheet("font-family:Consolas,monospace; font-size:11px;")
        root.addWidget(self._assets_view, 1)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("导出到："))
        self._path_edit = QLineEdit(self._default_out_path())
        path_row.addWidget(self._path_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        tpl_color = "#3FB950" if self._tpl["exists"] else "#F85149"
        tpl_txt = (
            f"运行时模板：已就绪（{_fmt_size(self._tpl['size'])}）"
            if self._tpl["exists"]
            else f"运行时模板缺失：{self._tpl['path']}　→　请先运行 build_runtime.cmd"
        )
        self._tpl_lbl = QLabel(tpl_txt)
        self._tpl_lbl.setStyleSheet(f"color:{tpl_color}; font-size:11px;")
        self._tpl_lbl.setWordWrap(True)
        root.addWidget(self._tpl_lbl)

        # OCR 依赖提示（动态打包）：用了 OCR 节点才提示（含组件递归检测）
        self._uses_ocr = exporter.uses_ocr(self._graph, project_root=project_root)
        if self._uses_ocr:
            ocr_lbl = QLabel(
                "⚠ 本蓝图包含「文字识别」节点，运行时需要 OCR 引擎（ocr_runtime/ 目录）。\n"
                "   导出后请把项目里的 ocr_runtime/ 目录放到生成的 EXE 同级目录下，否则识别节点会报「OCR 未安装」。"
            )
            ocr_lbl.setStyleSheet("color:#D29922; font-size:11px; background:#2A2414; padding:6px 8px; border-radius:4px;")
            ocr_lbl.setWordWrap(True)
            root.addWidget(ocr_lbl)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self._ok_btn = QPushButton("开始导出")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._do_export)
        self._ok_btn.setEnabled(bool(self._tpl["exists"]))
        btns.addWidget(self._ok_btn)
        root.addLayout(btns)

    def _default_out_path(self) -> str:
        base = ""
        try:
            base = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation) or ""
        except Exception:
            base = ""
        if not base or not os.path.isdir(base):
            base = self._project_root or os.path.expanduser("~")
        return os.path.join(base, _safe_filename(self._name) + ".exe")

    # ---------------- 资源扫描 ----------------
    def _scan_resources(self):
        try:
            assets, missing = exporter.preview_assets(self._graph)
        except Exception as e:
            self._assets_view.setPlainText(f"扫描资源失败：{e}")
            return
        lines = []
        if not assets and not missing:
            lines.append("（本蓝图没有引用任何图片资源）")
        total = 0
        for a in assets:
            total += int(a.get("size") or 0)
            lines.append(f"  ✔ {os.path.basename(a['orig'])}    {_fmt_size(a.get('size'))}    {a['orig']}")
        if assets:
            lines.append("")
            lines.append(f"共 {len(assets)} 个图片，合计 {_fmt_size(total)}")
        if missing:
            lines.append("")
            lines.append(f"⚠ 以下 {len(missing)} 个引用找不到文件（不会被包含，运行时该节点会失败）：")
            for m in missing:
                lines.append(f"  ✘ {m}")
        self._assets_view.setPlainText("\n".join(lines))

    # ---------------- 交互 ----------------
    def _on_browse(self):
        cur = self._path_edit.text().strip()
        start = os.path.dirname(cur) if cur else ""
        path, _ = QFileDialog.getSaveFileName(self, "导出为", cur or start, "可执行文件 (*.exe)")
        if path:
            if not path.lower().endswith(".exe"):
                path += ".exe"
            self._path_edit.setText(path)

    def _do_export(self):
        out = self._path_edit.text().strip()
        if not out:
            QMessageBox.warning(self, "导出", "请先选择导出路径。")
            return
        if not self._tpl["exists"]:
            QMessageBox.critical(
                self,
                "缺少运行时模板",
                "未找到 BlueprintRuntime.exe，无法导出。\n\n"
                f"预期位置：{self._tpl['path']}\n\n"
                "请先运行项目根目录下的 build_runtime.cmd 生成运行时模板。",
            )
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._ok_btn.setEnabled(False)
        try:
            res = exporter.export_blueprint(
                self._graph, self._name, out, source_project=self._project_root
            )
        finally:
            QApplication.restoreOverrideCursor()
            self._ok_btn.setEnabled(True)

        if not res.ok:
            QMessageBox.critical(self, "导出失败", res.message)
            return

        self._result_path = res.out_path
        msg = (
            f"导出成功！\n\n"
            f"文件：{res.out_path}\n"
            f"大小：{_fmt_size(res.total_bytes)}\n"
            f"打包图片：{res.asset_count} 个"
        )
        if self._uses_ocr:
            msg += (
                "\n\n⚠ 本蓝图包含「文字识别」节点：请把 ocr_runtime/ 目录放到"
                " EXE 同级目录，否则识别节点无法运行。"
            )
        if res.missing:
            msg += "\n\n⚠ 以下引用的图片未找到，未被打包：\n" + "\n".join(res.missing[:8])
            if len(res.missing) > 8:
                msg += f"\n… 等共 {len(res.missing)} 个"

        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setIcon(QMessageBox.Information)
        box.setText(msg)
        open_btn = box.addButton("打开所在文件夹", QMessageBox.ActionRole)
        box.addButton("确定", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_folder(os.path.dirname(res.out_path))
        self.accept()

    @staticmethod
    def _open_folder(path: str):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass
