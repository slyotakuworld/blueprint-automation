import os
from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QAction, QFont
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
    QMenu,
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
        btn.setMinimumWidth(180)
        btn.setStyleSheet(
            "QPushButton { background:#238636; color:white; border-radius:6px; font-weight:600; padding: 4px 16px; }"
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
        # ⚠ 【关键：auto_open=False】只创建磁盘结构/meta/初始蓝图，不切 ProjectManager 状态。
        # 真正的工程切换（close旧+open新）必须在主程序里先 confirm_discard 保存完旧工程修改之后再做，
        # 否则 set_last_opened_blueprint 会把旧工程的蓝图路径写到新工程的 meta 里，造成「串资源」。
        created = self._pm.create_project(parent, name, auto_open=False)
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
            "所有连线、节点、截图模板一个都不会少，连线都乖乖连在原来的位置哦！<br>"
            "<br>"
            "开工小贴士（贤狼の智慧）：<b style='color:#58A6FF'>Ctrl+S/Z/Y/C/V</b> 都给汝设好了，不用找按钮～<br>"
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
        btn.setMinimumWidth(180)
        btn.setStyleSheet(
            "QPushButton { background:#0969DA; color:white; border-radius:6px; font-weight:600; padding: 4px 16px; }"
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
        # ⚠ 【不在这里 open_project】只验证合法性+返回目标路径。
        # 真正的工程切换（close旧+open新）必须在主程序里先 confirm_discard 保存完旧工程修改之后再做，
        # 否则 set_last_opened_blueprint 会把旧工程的蓝图路径写到新工程的 meta 里，造成「串资源」。
        self.result_project_root = os.path.abspath(p)
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
        self._recent_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._recent_list.customContextMenuRequested.connect(self._on_recent_context_menu)
        v.addWidget(self._recent_list, 1)

        empty_hint = QLabel("（暂无最近工程，先点【🆕 新建工程】创建第一个）")
        empty_hint.setStyleSheet("color:#8B949E;")
        v.addWidget(empty_hint)

        self._fill_recent(empty_hint)

        btn_bar = QHBoxLayout()
        btn_bar.addStretch(1)

        btn_del_sel = QPushButton("🗑 删除工程")
        btn_del_sel.setMinimumHeight(36)
        btn_del_sel.setMinimumWidth(140)
        btn_del_sel.setStyleSheet(
            "QPushButton { background:#DA3633; color:white; border-radius:6px; font-weight:600; padding: 4px 16px; }"
            " QPushButton:hover { background:#F85149; }"
        )
        btn_del_sel.setToolTip("将工程目录整个移动到回收站（可恢复），并从最近工程列表里移除")
        btn_del_sel.clicked.connect(lambda _=False: self._on_delete_selected_project(double_click=False))
        btn_bar.addWidget(btn_del_sel)

        btn_open_sel = QPushButton("🗂 打开工程")
        btn_open_sel.setMinimumHeight(36)
        btn_open_sel.setMinimumWidth(140)
        btn_open_sel.setStyleSheet(
            "QPushButton { background:#0969DA; color:white; border-radius:6px; font-weight:600; padding: 4px 16px; }"
            " QPushButton:hover { background:#1F6FEB; }"
        )
        btn_open_sel.clicked.connect(lambda _=False: self._on_open_recent(double_click=False))
        btn_bar.addWidget(btn_open_sel)
        v.addLayout(btn_bar)

        self._recent_list.itemDoubleClicked.connect(
            lambda _it: self._on_open_recent(double_click=True)
        )
        self._recent_empty_hint = empty_hint
        return w

    def _selected_recent_path(self, show_warning: bool = False) -> Optional[str]:
        it = self._recent_list.currentItem()
        if not it:
            if show_warning:
                QMessageBox.information(self, "请先选中", "请先在列表里选一个最近工程。")
            return None
        p = it.data(Qt.UserRole)
        return p if isinstance(p, str) and p else None

    def _on_recent_context_menu(self, pos: QPoint):
        it = self._recent_list.itemAt(pos)
        if not it:
            return
        self._recent_list.setCurrentItem(it)
        p = it.data(Qt.UserRole)
        if not isinstance(p, str) or not p:
            return

        menu = QMenu(self)
        act_open = QAction("🗂 打开此工程", self)
        act_open.triggered.connect(lambda _=False: self._on_open_recent(double_click=False))
        menu.addAction(act_open)

        menu.addSeparator()

        act_delete = QAction("🗑 从磁盘删除此工程…", self)
        act_delete.triggered.connect(lambda _=False: self._on_delete_selected_project(double_click=False))
        menu.addAction(act_delete)

        menu.exec(self._recent_list.viewport().mapToGlobal(pos))

    def _on_remove_selected_from_list(self):
        p = self._selected_recent_path(show_warning=True)
        if not p:
            return
        name = os.path.basename(p.rstrip("\\/"))
        ret = QMessageBox.question(
            self, "仅从列表移除",
            f"确认把『{name}』从最近工程列表里移除此记录？\n\n"
            f"  · 磁盘上的工程目录不会被删除\n"
            f"  · 下次手动打开此工程时会重新出现在列表里\n\n"
            f"  路径：{p}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if ret != QMessageBox.Yes:
            return
        ok = self._pm.remove_recent_project(p)
        if not ok:
            QMessageBox.warning(self, "移除失败", "写回用户配置失败，请稍后重试。")
            return
        self._fill_recent(self._recent_empty_hint)

    def _on_delete_selected_project(self, double_click: bool = False):
        p = self._selected_recent_path(show_warning=not double_click)
        if not p:
            return

        if not os.path.isdir(p):
            ret = QMessageBox.question(
                self, "目录不存在",
                f"该工程目录在磁盘上已不存在：\n  {p}\n\n是否仅从最近工程列表里移除对应记录？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if ret == QMessageBox.Yes:
                self._pm.remove_recent_project(p)
                self._fill_recent(self._recent_empty_hint)
            return

        if not ProjectManager.is_valid_project_dir(p):
            ret = QMessageBox.warning(
                self, "工程已损坏",
                f"{p}\n不再是合法工程（缺少 project_meta.json 或 project_name）。\n\n"
                f"是否仍要删除整个目录？\n\n⚠ 这不会经过 send2trash，只能彻底删除（需手动确认永久删除二次弹框）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return
            permanent_ret = QMessageBox.warning(
                self, "⚠ 永久删除",
                f"因无法识别为合法工程，只能直接彻底删除（不会进回收站）。\n\n确认删除整个目录？\n  {p}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if permanent_ret != QMessageBox.Yes:
                return
            import shutil as _sh
            try:
                _sh.rmtree(p)
            except Exception as e:
                QMessageBox.critical(self, "删除失败", f"彻底删除失败：\n{e}")
                return
            self._pm.remove_recent_project(p)
            self._fill_recent(self._recent_empty_hint)
            return

        # 正常流程：合法工程 → 走 ProjectManager 的安全删除
        name = os.path.basename(p.rstrip("\\/"))
        # 读工程 meta 名，更友好
        meta_path = os.path.join(p, "project_meta.json")
        try:
            import json as _j
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_d = _j.load(f) or {}
            pn = str(meta_d.get("project_name") or "").strip()
            if pn:
                name = pn
        except Exception:
            pass

        ret = QMessageBox.warning(
            self, "⚠ 删除工程",
            f"确定要删除这个工程吗？\n\n"
            f"  · 工程名称：{name}\n"
            f"  · 路径：{p}\n\n"
            f"✅ 优先移动到【回收站】（删除后可从回收站恢复）。\n"
            f"⚠ 回收站不可用时，会询问是否改为永久删除（不可恢复）。\n\n"
            f"工程内的截图、模板、蓝图、运行日志会一起被删除。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return

        ok, reason = self._pm.delete_project(p, permanent=False)
        if ok:
            QMessageBox.information(self, "删除成功", reason)
            self._fill_recent(self._recent_empty_hint)
            return
        # 回收站路径失败 → 询问是否升级为 permanent
        if isinstance(reason, str) and reason.startswith("TRASH_UNAVAILABLE"):
            cleaned = reason.replace("TRASH_UNAVAILABLE:", "").strip()
            r2 = QMessageBox.warning(
                self, "回收站不可用",
                f"{cleaned}\n\n是否改为【永久删除】（不可恢复，不会进回收站）？\n\n  路径：{p}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if r2 != QMessageBox.Yes:
                return
            ok2, reason2 = self._pm.delete_project(p, permanent=True)
            if ok2:
                QMessageBox.information(self, "已永久删除", reason2)
                self._fill_recent(self._recent_empty_hint)
                return
            QMessageBox.critical(self, "删除失败", reason2)
            return
        # 其他拒绝/失败：直接弹原因（如"正在使用中"、"不是合法工程"）
        QMessageBox.critical(self, "无法删除", reason)

    def _fill_recent(self, empty_hint: QLabel):
        self._recent_list.clear()
        recents = self._pm.recent_projects()
        if not recents:
            empty_hint.setText("（暂无最近工程，先点【🆕 新建工程】创建第一个）")
            return
        empty_hint.setText(f"（共 {len(recents)} 个最近工程，双击可直接打开；右键菜单可删除/移除）")
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
        # ⚠ 【不在这里 open_project】只验证合法性+返回目标路径。
        # 真正的工程切换（close旧+open新）必须在主程序里先 confirm_discard 保存完旧工程修改之后再做，
        # 否则 set_last_opened_blueprint 会把旧工程的蓝图路径写到新工程的 meta 里，造成「串资源」。
        self.result_project_root = os.path.abspath(p)
        self.accept()
