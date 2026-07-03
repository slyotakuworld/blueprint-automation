import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..project_manager import ProjectManager


class ProjectStartupDialog(QDialog):
    """
    启动应用强制先选工程：
      Tab1 新建工程 → 填工程名 + 选父目录（默认 projects/）
      Tab2 打开工程 → 选工程根目录
      Tab3 最近工程 → 列表里点一个直接打开
    结果：
      - accept() → 调用者拿 self.result_project_root（绝对路径）
      - reject() → 用户点× → 主程序应当直接退出（因为用户不先选工程不让进）
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("第一步：选择工程")
        self.setModal(True)
        self.resize(560, 480)

        self.result_project_root: Optional[str] = None
        self._pm = ProjectManager.instance()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("请先新建或打开一个工程")
        f = QFont()
        f.setBold(True)
        f.setPointSize(13)
        title.setFont(f)
        title.setStyleSheet("color:#C9D1D9;")
        root.addWidget(title)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_new_tab(), "🆕 新建工程")
        tabs.addTab(self._build_open_tab(), "📁 打开工程")
        tabs.addTab(self._build_recent_tab(), "🕒 最近工程")
        root.addWidget(tabs, 1)

        # 底部按钮
        bar = QHBoxLayout()
        bar.addStretch(1)
        btn_quit = QPushButton("退出程序")
        btn_quit.clicked.connect(self.reject)
        bar.addWidget(btn_quit)
        root.addLayout(bar)

    # ============================================================
    # Tab1：新建工程
    # ============================================================
    def _build_new_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        box = QGroupBox("工程信息")
        form = QFormLayout(box)
        form.setSpacing(10)

        self._new_name = QLineEdit()
        self._new_name.setPlaceholderText("例如：我的手游日常脚本、某游戏自动战斗")
        form.addRow("工程名称：", self._new_name)

        self._new_parent_row = QHBoxLayout()
        self._new_parent = QLineEdit(ProjectManager.default_projects_root())
        self._new_parent.setReadOnly(True)
        self._new_parent_row.addWidget(self._new_parent, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._on_new_browse_parent)
        self._new_parent_row.addWidget(btn_browse)
        wrap = QWidget()
        wrap.setLayout(self._new_parent_row)
        form.addRow("父目录：", wrap)

        v.addWidget(box)

        holo_intro_new = QLabel(
            "<b style='color:#F0B429;'>🐺 贤狼赫萝の小唠叨</b><br>"
            "汝好呀～咱是约伊兹的贤狼赫萝，也是这个小工具的吉祥物哦～<br>"
            "汝现在要创建一个新工程对吧？把每天要重复做的那些截图、找图、点按钮的活儿，<br>"
            "一张一张画成<b style='color:#58A6FF'>蓝图</b>，咱就会按照汝定的规矩，<br>"
            "一遍一遍乖乖帮汝跑，绝不会偷懒打瞌睡～<br>"
            "不同工程的截图、模板、运行日志会全部分开存，汝尽管折腾就好！"
        )
        holo_intro_new.setTextFormat(Qt.RichText)
        holo_intro_new.setStyleSheet(
            "QLabel { color:#D2992A; background:#1E1810; padding:10px 12px; border-radius:6px;"
            " border:1px solid #3A2E18; }"
        )
        holo_intro_new.setWordWrap(True)
        v.addWidget(holo_intro_new)

        v.addStretch(1)
        btn = QPushButton("✅ 创建并打开该工程")
        btn.setMinimumHeight(36)
        btn.setStyleSheet(
            "QPushButton { background:#238636; color:white; border-radius:6px; font-weight:600; }"
            " QPushButton:hover { background:#2EA043; }"
        )
        btn.clicked.connect(self._on_create)
        v.addWidget(btn)

        return w

    def _on_new_browse_parent(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择新建工程所在的父目录",
            self._new_parent.text() or ProjectManager.default_projects_root(),
        )
        if d:
            self._new_parent.setText(d)

    def _on_create(self):
        name = (self._new_name.text() or "").strip()
        if not name:
            QMessageBox.warning(self, "请填写工程名称", "必须给新工程起个名字。")
            return
        parent = (self._new_parent.text() or "").strip()
        if not parent or not os.path.isdir(parent):
            QMessageBox.warning(self, "父目录无效", "父目录不存在，请先选择一个合法的父目录。")
            return
        created = self._pm.create_project(parent, name)
        if not created:
            QMessageBox.warning(self, "创建失败", "创建工程失败（可能是权限问题）。请换个父目录或换个工程名试试。")
            return
        self.result_project_root = created
        self.accept()

    # ============================================================
    # Tab2：打开工程
    # ============================================================
    def _build_open_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        row = QHBoxLayout()
        self._open_path = QLineEdit()
        self._open_path.setPlaceholderText("点击右侧『浏览…』选择工程文件夹（里面要有 project_meta.json）")
        row.addWidget(self._open_path, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._on_open_browse)
        row.addWidget(btn_browse)
        v.addLayout(row)

        holo_intro_open = QLabel(
            "<b style='color:#F0B429;'>🐺 贤狼赫萝の碎碎念</b><br>"
            "哟，汝回来啦～咱可是一直乖乖坐在这儿等汝呢，尾巴都晃了八百多圈咯～<br>"
            "上次蓝图画到一半？没关系，咱帮汝把进度好好收在工程目录里了，<br>"
            "所有连线、节点、截图模板、切图缓存一个都不会少，连线都乖乖连在原来的位置哦！<br>"
            "<br>"
            "开工小贴士（贤狼の智慧）：<b style='color:#58A6FF'>F5</b> 直接运行当前蓝图，<b style='color:#58A6FF'>F7</b> 随时喊停，<br>"
            "<b style='color:#58A6FF'>F8</b> 一键截图切分，<b style='color:#58A6FF'>Ctrl+S/Z/C/V</b> 都给汝设好了，不用找按钮～<br>"
            "要是跑蓝图出了小状况？别慌，debug 文件夹里有完整运行日志，<br>"
            "汝尽管折腾出花样来，咱帮汝兜着底！🍎（要不要干完这单，咱一起去吃苹果呀？）"
        )
        holo_intro_open.setTextFormat(Qt.RichText)
        holo_intro_open.setStyleSheet(
            "QLabel { color:#D2992A; background:#1E1810; padding:10px 12px; border-radius:6px;"
            " border:1px solid #3A2E18; }"
        )
        holo_intro_open.setWordWrap(True)
        v.addWidget(holo_intro_open)

        v.addStretch(1)
        btn = QPushButton("📂 打开选中的工程")
        btn.setMinimumHeight(36)
        btn.setStyleSheet(
            "QPushButton { background:#0969DA; color:white; border-radius:6px; font-weight:600; }"
            " QPushButton:hover { background:#1F6FEB; }"
        )
        btn.clicked.connect(self._on_open)
        v.addWidget(btn)

        return w

    def _on_open_browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择工程根目录",
            self._open_path.text() or ProjectManager.default_projects_root(),
        )
        if d:
            self._open_path.setText(d)

    def _on_open(self):
        p = (self._open_path.text() or "").strip()
        if not p or not os.path.isdir(p):
            QMessageBox.warning(self, "请选择目录", "请先选择一个合法的工程根目录。")
            return
        if not ProjectManager.is_valid_project_dir(p):
            QMessageBox.warning(
                self, "不是合法工程",
                f"这个目录里没有 {os.path.join(p, 'project_meta.json')}，不是合法的工程。\n"
                "请先确认目录是否正确，或者点【新建工程】创建一个。"
            )
            return
        ok = self._pm.open_project(p)
        if not ok:
            QMessageBox.warning(self, "打开失败", "打开工程失败（读取 project_meta.json 失败）。")
            return
        self.result_project_root = self._pm.project_root
        self.accept()

    # ============================================================
    # Tab3：最近工程
    # ============================================================
    def _build_recent_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        self._recent_list = QListWidget()
        self._recent_list.setStyleSheet(
            "QListWidget { background:#1C2128; color:#C9D1D9; border:1px solid #2D333B; border-radius:6px; }"
            " QListWidget::item { height:28px; padding:4px 8px; }"
            " QListWidget::item:hover { background:#2D333B; }"
            " QListWidget::item:selected { background:#0969DA; color:white; }"
        )
        v.addWidget(self._recent_list, 1)

        empty_hint = QLabel("（暂无最近工程，先点【🆕 新建工程】创建第一个）")
        empty_hint.setStyleSheet("color:#8B949E;")
        v.addWidget(empty_hint)

        self._fill_recent(empty_hint)

        btn_bar = QHBoxLayout()
        btn_bar.addStretch(1)
        btn_open_sel = QPushButton("🗂 打开选中的最近工程")
        btn_open_sel.setMinimumHeight(36)
        btn_open_sel.clicked.connect(lambda _=False: self._on_open_recent(double_click=False))
        btn_bar.addWidget(btn_open_sel)
        v.addLayout(btn_bar)

        self._recent_list.itemDoubleClicked.connect(
            lambda _it: self._on_open_recent(double_click=True)
        )
        return w

    def _fill_recent(self, empty_hint: QLabel):
        self._recent_list.clear()
        recents = self._pm.recent_projects()
        if not recents:
            empty_hint.setText("（暂无最近工程，先点【🆕 新建工程】创建第一个）")
            return
        empty_hint.setText(f"（共 {len(recents)} 个最近工程，双击可直接打开）")
        for p in recents:
            meta = os.path.join(p, "project_meta.json")
            name = os.path.basename(p.rstrip("\\/"))
            try:
                import json as _j
                with open(meta, "r", encoding="utf-8") as f:
                    d = _j.load(f)
                opened = d.get("opened_at") or d.get("created_at") or ""
                if opened:
                    pn = str(d.get("project_name") or "").strip() or os.path.basename(p.rstrip("\\/"))
                    name = f"{pn}  ·  {opened}"
            except Exception:
                pass
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, p)
            it.setToolTip(p)
            self._recent_list.addItem(it)

    def _on_open_recent(self, double_click: bool = False):
        it = self._recent_list.currentItem()
        if not it:
            if not double_click:
                QMessageBox.information(self, "请先选中", "请先在列表里选一个最近工程再打开。")
            return
        p = it.data(Qt.UserRole)
        if not p or not os.path.isdir(p):
            QMessageBox.warning(self, "目录不存在", f"目录 {p} 不存在，可能已经被删除/移动。")
            return
        if not ProjectManager.is_valid_project_dir(p):
            QMessageBox.warning(self, "工程已损坏", f"{p} 不再是合法工程（缺少 project_meta.json）。")
            return
        ok = self._pm.open_project(p)
        if not ok:
            QMessageBox.warning(self, "打开失败", "打开工程失败。")
            return
        self.result_project_root = self._pm.project_root
        self.accept()
