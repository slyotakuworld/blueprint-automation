# -*- coding: utf-8 -*-
"""文档攻略查看器（清爽文档阅读器风格）。

参考：GitBook / MkDocs Material / GitHub Docs / ReadTheDocs
  ┌─────────────────────────────────────────────────────────────────┐
  │ 顶部标题条：手册名称  +  搜索框（白底、圆角 8px、focus 蓝边）   │
  ├──────────────────────┬──────────────────────────────────────────┤
  │ 左侧导航 (260px 固定)│ 右侧 Markdown 内容区                     │
  │ - 灰底极浅的侧栏      │ - 白底                                   │
  │ - 悬停：浅灰底        │ - 文字 深灰 #1F2328                      │
  │ - 选中：左蓝条+浅蓝底│ - 标题 黑色 #111827 带底部分隔线         │
  │   +蓝文字            │ - 表格 灰头白身(GitHub风)                 │
  │                      │ - 代码块 深色 Monokai / 浅色 GitHub 风   │
  │                      │ - 引用块 4px蓝边+浅灰底                  │
  └──────────────────────┴──────────────────────────────────────────┘

文档配置目录：config/doc/
  - doc_index.json：目录结构定义
  - *.md：各章节 Markdown 内容

【模块归属】：蓝图模块通用 UI 组件（blueprint/ui/）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 仅在需要小图标时借用 LLM 子模块的 icon 加载器（纯视觉资产，不耦合归属）
from src.blueprint.llm.ui._icon_loader import icon, pixmap


# =====================================================================
# 文档专属配色（赫萝风格 —— 粉色系 · 暖米白 · 玫瑰粉强调 · 狼麦香）
# =====================================================================
_DOC_THEME: Dict[str, str] = {
    # ---- 容器背景（暖米白 + 奶油粉） ----
    "bg_root":        "#FFFBF8",  # 主背景：极淡粉米白（暖感）
    "bg_page":        "#FFFBF8",  # 内容区：同暖米白
    "bg_sidebar":     "#FFF2F0",  # 侧栏：奶油桃粉（赫萝萌系浅粉）
    "bg_topbar":      "#FDEDE7",  # 顶栏：粉桃麦香（和麦香呼应，赫萝爱麦子）
    "bg_hover":       "#FCE4EC",  # hover：浅桃粉
    "bg_active":      "#FBD5DC",  # 选中：玫瑰粉淡底
    "bg_input":       "#FFFFFF",  # 搜索框：白

    # ---- 主色 / 强调（赫萝玫瑰粉 / 莓果红）----
    "primary":        "#D64B6E",       # 主色：莓果玫瑰粉（醒目但不刺眼）
    "primary_hover":  "#B93A5B",
    "primary_soft":   "#FBD5DC",       # 最浅粉（选中第二档）

    # ---- 文字（暖棕系，代替冷灰黑）----
    "text_title":     "#4A2B33",  # 标题：深莓棕（柔一点暖棕红）
    "text_h2":        "#5C353D",
    "text_main":      "#5A3A42",  # 正文：暖棕粉灰（不冷硬）
    "text_sub":       "#8A5A67",  # 辅助文字：灰粉棕
    "text_muted":     "#A87885",  # 最淡粉棕

    # ---- 边框 / 分割（桃粉系）----
    "border":         "#F0C8D0",  # 常规边框：浅桃粉
    "border_heavy":   "#E0A4B0",  # 强一点的粉棕
    "divider":        "#F3D9DE",  # 细分割线：极淡桃粉

    # ---- 代码 ----
    "code_bg":        "#FFF0F3",  # 行内代码：奶油粉
    "code_bg_block":  "#FFF0F3",  # 代码块：奶油粉
    "code_fg":        "#5A3A42",  # 同正文暖棕
    "code_border":    "#F0C8D0",

    # ---- 引用 ----
    "quote_border":   "#E87A90",  # 左侧边：玫瑰粉（赫萝粉）
    "quote_bg":       "#FFF5F7",
    "quote_fg":       "#8A5A67",

    # ---- 表格 ----
    "table_head_bg":  "#FDEDEE",   # 表头：极浅粉
    "table_border":   "#F0C8D0",

    # ---- 滚动条 ----
    "scroll_track":   "transparent",
    "scroll_handle":  "#E8B9C4",   # 粉把手
    "scroll_hover":   "#D64B6E",   # hover 变莓果粉

    # ---- 侧栏选中条 ----
    "sidebar_active_bar": "#D64B6E",  # 左边 3px 莓果粉条
}


# =====================================================================
# 文档专属字体栈（Windows 下清晰阅读字体 —— 不要"站酷庆科黄油体"那种卡通风）
# =====================================================================
_DOC_FONT_STACK = ",".join([
    '"PingFang SC"',          # 苹果/新 Mac 可选（只是以防万一）
    '"Microsoft YaHei UI"',   # Windows 10+ 默认：清晰的 UI 黑体
    '"微软雅黑"',
    '"Source Han Sans CN"',   # 思源黑体
    '"Segoe UI"',             # 英文正文
    '"Helvetica Neue"',
    'Arial',
    'sans-serif',
])

_DOC_FONT_MONO = ",".join([
    '"JetBrains Mono"',
    '"Fira Code"',
    '"Consolas"',
    '"Microsoft YaHei Mono"',
    '"Courier New"',
    'monospace',
])


# =====================================================================
# 文档查看器专属 QSS（强作用域隔离：所有样式只在 DocViewerPage 内部生效，
# 不污染主窗口的 Holo 奶茶主题）
# =====================================================================
_DOC_QSS = f"""
/* ---------- 最外层容器：纯白背景 ---------- */
QFrame#docViewerRoot {{
    background: {_DOC_THEME['bg_root']};
    border: none;
    border-radius: 0px;
}}

/* ---------- 顶部标题条（浅灰底 + 下分割线） ---------- */
QFrame#docTopBar {{
    background: {_DOC_THEME['bg_topbar']};
    border: none;
    border-bottom: 1px solid {_DOC_THEME['divider']};
    border-radius: 0px;
}}
QLabel#docMainTitle {{
    color: {_DOC_THEME['text_title']};
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 0.5px;
    background: transparent;
}}
QLabel#docSubTitle {{
    color: {_DOC_THEME['text_sub']};
    font-size: 12px;
    background: transparent;
}}

/* ---------- 搜索框 ---------- */
QLineEdit#docSearch {{
    background: {_DOC_THEME['bg_input']};
    color: {_DOC_THEME['text_main']};
    border: 1px solid {_DOC_THEME['border']};
    border-radius: 8px;
    padding: 6px 12px 6px 12px;
    selection-background-color: {_DOC_THEME['primary']};
    selection-color: #FFFFFF;
    font-size: 13px;
    min-width: 220px;
}}
QLineEdit#docSearch:focus {{
    border: 1px solid {_DOC_THEME['primary']};
}}
QLineEdit#docSearch::placeholder {{
    color: {_DOC_THEME['text_muted']};
}}

/* ---------- 左侧导航面板 ---------- */
QFrame#docSidePanel {{
    background: {_DOC_THEME['bg_sidebar']};
    border: none;
    border-right: 1px solid {_DOC_THEME['divider']};
    border-radius: 00px;
}}
QLabel#docNavTitle {{
    color: {_DOC_THEME['text_sub']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    padding-left: 8px;
    background: transparent;
}}
QLabel#docVersion {{
    color: {_DOC_THEME['text_muted']};
    font-size: 11px;
}}

/* ---------- 树形目录（赫萝风·贴左） ---------- */
QTreeWidget#docTree {{
    background: transparent;
    border: none;
    color: {_DOC_THEME['text_main']};
    font-size: 13px;
    outline: 0;
    padding: 2px 0 2px 0;
}}
QTreeWidget#docTree::item {{
    height: 30px;
    border-left: 3px solid transparent;
    padding-left: 5px;
    padding-right: 6px;
}}
QTreeWidget#docTree::item:hover {{
    background: {_DOC_THEME['bg_hover']};
    color: {_DOC_THEME['text_title']};
}}
QTreeWidget#docTree::item:selected {{
    background: {_DOC_THEME['bg_active']};
    color: {_DOC_THEME['primary']};
    border-left: 3px solid {_DOC_THEME['sidebar_active_bar']};
    font-weight: 600;
}}
/* 父节点：略大、略深、不要太卡通 */
QTreeWidget#docTree::branch:has-children:closed,
QTreeWidget#docTree::branch:has-children:open {{
    border-image: none;
    image: none;
    color: {_DOC_THEME['text_sub']};
    width: 10px;
}}

/* ---------- 右侧内容面板 ---------- */
QFrame#docContentPanel {{
    background: {_DOC_THEME['bg_page']};
    border: none;
    border-radius: 0px;
}}
QLabel#docChapterTitle {{
    background: {_DOC_THEME['bg_page']};
    color: {_DOC_THEME['text_title']};
    font-size: 14px;
    font-weight: 700;
    padding: 18px 80px 14px 80px;   /* 章节标题四边加大边距，左右 80px 与正文 QLabel 级 padding+HTML padding 对齐 */
    border-bottom: 1px solid {_DOC_THEME['divider']};
}}
/* 滚动条：文档灰（不是奶茶色） */
QScrollArea#docScrollArea, QScrollArea#docScrollArea > QWidget > QWidget {{
    background: {_DOC_THEME['bg_page']};
    border: none;
}}
QScrollArea#docScrollArea QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollArea#docScrollArea QScrollBar::handle:vertical {{
    background: {_DOC_THEME['scroll_handle']};
    border-radius: 5px;
    min-height: 40px;
    margin: 0 3px 0 3px;
}}
QScrollArea#docScrollArea QScrollBar::handle:vertical:hover {{
    background: {_DOC_THEME['scroll_hover']};
}}
QScrollArea#docScrollArea QScrollBar::add-line:vertical,
QScrollArea#docScrollArea QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollArea#docScrollArea QScrollBar::add-page:vertical,
QScrollArea#docScrollArea QScrollBar::sub-page:vertical {{
    background: none;
}}
"""


# =====================================================================
# Markdown -> 清爽文档风 HTML（GitHub Docs 风格，与上面配色严格一致）
# =====================================================================
class SimpleMarkdownRenderer:
    """极简 Markdown 渲染器：
    支持：标题(#~######)、加粗(**x**)、斜体(*x*)、代码(`x`)、
         列表(- / 1.)、表格、引用(> )、分割线(---)、链接。
    所有 inline style 都和 _DOC_THEME 对齐，保证视觉一致性。
    """

    T = _DOC_THEME
    FONT_MONO = _DOC_FONT_MONO
    FONT_BODY = _DOC_FONT_STACK

    @staticmethod
    def render(markdown_text: str) -> str:
        if not markdown_text:
            return ""
        lines = markdown_text.replace("\r\n", "\n").split("\n")
        html_parts: List[str] = []
        # 最外层 padding 模拟 ReadTheDocs 正文页边距
        html_parts.append(
            f"<div style='line-height:1.88; padding:24px 40px 56px 40px;"
            f"font-family:{SimpleMarkdownRenderer.FONT_BODY}; color:{SimpleMarkdownRenderer.T['text_main']};"
            f"font-size:14.5px;'>"
        )

        i = 0
        in_code_block = False
        code_lines: List[str] = []
        in_table = False
        table_rows: List[List[str]] = []
        in_list_ul = False
        in_list_ol = False

        def close_lists():
            nonlocal in_list_ul, in_list_ol
            if in_list_ul:
                html_parts.append("</ul>")
                in_list_ul = False
            if in_list_ol:
                html_parts.append("</ol>")
                in_list_ol = False

        def close_table():
            nonlocal in_table, table_rows
            if in_table and table_rows:
                t = SimpleMarkdownRenderer.T
                html_parts.append(
                    "<table style='border-collapse:collapse; margin:18px 0; "
                    f"width:100%; font-size:13.5px; border:1px solid {t['table_border']};'>"
                )
                for ri, row in enumerate(table_rows):
                    tag = "th" if ri == 0 else "td"
                    is_head = (ri == 0)
                    bg = t['table_head_bg'] if is_head else '#FFFFFF'
                    fg = t['text_title'] if is_head else t['text_main']
                    weight = '700' if is_head else '400'
                    html_parts.append("<tr>")
                    for cell in row:
                        style = (
                            f"border:1px solid {t['table_border']};"
                            f"padding:8px 14px;"
                            f"background:{bg};"
                            f"color:{fg};"
                            f"font-weight:{weight};"
                            "text-align:left;"
                            "vertical-align:top;"
                        )
                        html_parts.append(
                            f"<{tag} style='{style}'>{SimpleMarkdownRenderer._inline(cell)}</{tag}>"
                        )
                    html_parts.append("</tr>")
                html_parts.append("</table>")
            in_table = False
            table_rows = []

        while i < len(lines):
            line = lines[i]

            # 代码块 ```
            if line.strip().startswith("```"):
                close_lists()
                close_table()
                if not in_code_block:
                    in_code_block = True
                    code_lines = []
                else:
                    t = SimpleMarkdownRenderer.T
                    fm = SimpleMarkdownRenderer.FONT_MONO
                    html_parts.append(
                        f"<pre style='background:{t['code_bg_block']}; color:{t['code_fg']}; "
                        f"padding:14px 18px; border-radius:8px; overflow-x:auto; "
                        f"font-size:13px; line-height:1.6; margin:16px 0;"
                        f"border:1px solid {t['code_border']};"
                        f"font-family:{fm};'>"
                    )
                    html_parts.append("<code>" + "<br>".join(
                        SimpleMarkdownRenderer._escape(c) for c in code_lines
                    ) + "</code></pre>")
                    in_code_block = False
                i += 1
                continue
            if in_code_block:
                code_lines.append(line)
                i += 1
                continue

            # 空行
            if not line.strip():
                close_lists()
                close_table()
                i += 1
                continue

            # 分割线
            if line.strip() in ("---", "***", "___"):
                close_lists()
                close_table()
                html_parts.append(
                    f"<hr style='border:none; border-top:1px solid {SimpleMarkdownRenderer.T['divider']}; "
                    "margin:26px 0;' />"
                )
                i += 1
                continue

            # 表格行（含 | ）
            if "|" in line and line.strip().startswith("|"):
                close_lists()
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if all(c.replace("-", "").replace(":", "") == "" for c in cells):
                    i += 1
                    continue
                if not in_table:
                    in_table = True
                    table_rows = []
                table_rows.append(cells)
                i += 1
                continue
            else:
                close_table()

            # 引用
            if line.startswith(">"):
                close_lists()
                content = line[1:].strip()
                blockquote_lines = [content]
                j = i + 1
                while j < len(lines) and lines[j].startswith(">"):
                    blockquote_lines.append(lines[j][1:].strip())
                    j += 1
                t = SimpleMarkdownRenderer.T
                html_parts.append(
                    f"<div style='border-left:4px solid {t['quote_border']}; "
                    f"background:{t['quote_bg']}; padding:4px 16px; margin:16px 0; "
                    f"color:{t['quote_fg']}; font-size:13.5px;'>"
                    f"{SimpleMarkdownRenderer._inline('<br>'.join(blockquote_lines))}</div>"
                )
                i = j
                continue

            # 标题
            stripped = line.lstrip("#")
            level = len(line) - len(stripped)
            if 1 <= level <= 6 and stripped.startswith(" "):
                close_lists()
                title_text = stripped.strip()
                t = SimpleMarkdownRenderer.T
                if level == 1:
                    style = (
                        f"font-size:26px; font-weight:800; color:{t['text_title']}; "
                        f"padding-bottom:10px; margin:32px 0 18px 0;"
                        f"border-bottom:1px solid {t['divider']};"
                        f"font-family:{SimpleMarkdownRenderer.FONT_BODY};"
                    )
                elif level == 2:
                    style = (
                        f"font-size:20px; font-weight:700; color:{t['text_h2']}; "
                        f"padding-bottom:6px; margin:28px 0 14px 0;"
                        f"border-bottom:1px solid {t['divider']};"
                    )
                elif level == 3:
                    style = (
                        f"font-size:17px; font-weight:700; color:{t['text_title']}; "
                        f"margin:24px 0 10px 0;"
                    )
                else:
                    style = (
                        f"font-size:15px; font-weight:600; color:{t['text_title']}; "
                        f"margin:20px 0 8px 0;"
                    )
                html_parts.append(
                    f"<h{level} style='{style}'>{SimpleMarkdownRenderer._inline(title_text)}</h{level}>"
                )
                i += 1
                continue

            # 无序列表
            if line.lstrip().startswith(("- ", "* ")):
                content = line.lstrip()[2:].strip()
                if not in_list_ul:
                    close_lists()
                    t = SimpleMarkdownRenderer.T
                    html_parts.append(
                        f"<ul style='margin:10px 0 10px 0; padding-left:26px; color:{t['text_main']}; "
                        f"line-height:1.9;'>"
                    )
                    in_list_ul = True
                html_parts.append(
                    f"<li style='margin:4px 0; list-style-type:disc;'>"
                    f"{SimpleMarkdownRenderer._inline(content)}</li>"
                )
                i += 1
                continue

            # 有序列表
            import re
            if re.match(r"^\s*\d+\.\s+", line):
                m = re.match(r"^(\s*\d+\.\s+)(.*)$", line)
                content = m.group(2).strip() if m else ""
                if not in_list_ol:
                    close_lists()
                    t = SimpleMarkdownRenderer.T
                    html_parts.append(
                        f"<ol style='margin:10px 0 10px 0; padding-left:26px; color:{t['text_main']}; "
                        f"line-height:1.9;'>"
                    )
                    in_list_ol = True
                html_parts.append(
                    f"<li style='margin:4px 0;'>"
                    f"{SimpleMarkdownRenderer._inline(content)}</li>"
                )
                i += 1
                continue

            # 普通段落
            close_lists()
            para_lines = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if (not nxt.strip()) or nxt.startswith("#") or nxt.startswith(">") \
                        or nxt.lstrip().startswith(("- ", "* ")) \
                        or (nxt.strip().startswith("|") and "|" in nxt) \
                        or re.match(r"^\s*\d+\.\s+", nxt) or nxt.strip() in ("---", "***", "___"):
                    break
                para_lines.append(nxt)
                j += 1
            t = SimpleMarkdownRenderer.T
            html_parts.append(
                f"<p style='margin:10px 0; color:{t['text_main']}; font-size:14px; "
                f"line-height:1.85; word-wrap:break-word;'>"
                f"{SimpleMarkdownRenderer._inline(' '.join(l.strip() for l in para_lines))}</p>"
            )
            i = j

        close_lists()
        close_table()
        html_parts.append("</div>")
        return "\n".join(html_parts)

    @staticmethod
    def _escape(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    @staticmethod
    def _inline(text: str) -> str:
        if not text:
            return ""
        import re
        s = text
        t = SimpleMarkdownRenderer.T
        fm = SimpleMarkdownRenderer.FONT_MONO

        def _code(m):
            content = m.group(1)
            return (
                f"<code style='background:{t['code_bg']}; color:{t['code_fg']}; "
                f"padding:2px 8px; border-radius:6px; font-size:12.5px; "
                f"border:1px solid {t['code_border']}; "
                f"font-family:{fm};'>{SimpleMarkdownRenderer._escape(content)}</code>"
            )
        s = re.sub(r"`([^`]+)`", _code, s)

        # 加粗 **xxx**
        s = re.sub(
            r"\*\*([^*]+)\*\*",
            lambda m: f"<b style='color:{t['text_title']}; font-weight:700;'>{m.group(1)}</b>",
            s,
        )

        # 斜体 *xxx*
        s = re.sub(
            r"(?<!\*)\*([^*\n]+)\*(?!\*)",
            lambda m: f"<i style='color:{t['text_main']};'>{m.group(1)}</i>",
            s,
        )

        # 链接 [文字](URL)
        s = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: (
                f"<a href='{m.group(2)}' style='color:{t['primary']}; "
                f"text-decoration:none; border-bottom:1px solid {t['primary_soft']};'>{m.group(1)}</a>"
            ),
            s,
        )

        return s


# =====================================================================
# 文档查看器主 Widget（嵌入 QDialog）
# =====================================================================
class DocViewerPage(QFrame):
    """文档攻略查看器：左侧树 + 右侧内容（清爽文档风）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("docViewerRoot")
        self.setStyleSheet(_DOC_QSS)  # 文档专属 QSS，作用域隔离
        self._doc_dir = self._resolve_doc_dir()
        self._index: Dict[str, Any] = {}
        self._flat_items: Dict[str, QTreeWidgetItem] = {}
        self._build_ui()
        self._load_index()

    # -----------------------------------------------------------------
    # 路径解析
    # -----------------------------------------------------------------
    @staticmethod
    def _resolve_doc_dir() -> str:
        from src.config.app_paths import INTERNAL_DIR, APP_DIR

        candidates = [
            os.path.normpath(os.path.join(INTERNAL_DIR, "config", "doc")),
            os.path.normpath(os.path.join(APP_DIR, "config", "doc")),
        ]
        try:
            here = os.path.abspath(os.path.dirname(__file__))
            candidates.append(
                os.path.normpath(
                    os.path.join(here, "..", "..", "..", "config", "doc")
                )
            )
        except Exception:
            pass
        candidates.append(os.path.normpath(os.path.join(os.getcwd(), "config", "doc")))

        for c in candidates:
            if os.path.isdir(c):
                return c
        return candidates[-1]

    # -----------------------------------------------------------------
    # UI（严格按文档风配色）
    # -----------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)   # 文档页整体四边留空，避免贴着对话框边框
        root.setSpacing(10)  # 顶栏和主体之间留小空隙

        # ===== 顶部标题条 =====
        top = QFrame(); top.setObjectName("docTopBar")
        th = QHBoxLayout(top); th.setContentsMargins(24, 14, 24, 14); th.setSpacing(18)

        title_col = QVBoxLayout(); title_col.setSpacing(2)
        t1 = QLabel("📚  赫萝蓝图自动化 · 攻略手册"); t1.setObjectName("docMainTitle")
        t2 = QLabel("🐺 软件说明 · 节点参数 · 进阶技巧 · 常见问题"); t2.setObjectName("docSubTitle")
        title_col.addWidget(t1); title_col.addWidget(t2)
        th.addLayout(title_col, 1)

        # 搜索框
        self.ed_search = QLineEdit(); self.ed_search.setObjectName("docSearch")
        self.ed_search.setPlaceholderText("🔍  搜索章节标题…（如 找图、变量、FAQ）")
        self.ed_search.setFixedWidth(300)
        self.ed_search.textChanged.connect(self._on_search)
        th.addWidget(self.ed_search, 0, Qt.AlignVCenter)
        root.addWidget(top, 0)

        # ===== 主体：左右分栏 =====
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(1)
        split.setStyleSheet(f"QSplitter::handle {{ background: {_DOC_THEME['divider']}; }}")
        root.addWidget(split, 1)

        # -------- 左侧：导航面板 --------
        left = QFrame(); left.setObjectName("docSidePanel")
        lv = QVBoxLayout(left); lv.setContentsMargins(0, 4, 2, 6); lv.setSpacing(6)

        nav_title = QLabel("章节目录"); nav_title.setObjectName("docNavTitle")
        lv.addWidget(nav_title)

        self.tree = QTreeWidget(); self.tree.setObjectName("docTree")
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(11)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        lv.addWidget(self.tree, 1)

        self.lbl_ver = QLabel(""); self.lbl_ver.setObjectName("docVersion")
        lv.addWidget(self.lbl_ver)

        left.setMinimumWidth(180); left.setMaximumWidth(270)
        split.addWidget(left)

        # -------- 右侧：内容 --------
        right = QFrame(); right.setObjectName("docContentPanel")
        rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(0)

        self.lbl_chapter = QLabel("欢迎使用赫萝蓝图自动化手册"); self.lbl_chapter.setObjectName("docChapterTitle")
        rv.addWidget(self.lbl_chapter, 0)

        scroll = QScrollArea(); scroll.setObjectName("docScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.lbl_content = QLabel()
        self.lbl_content.setWordWrap(True)
        self.lbl_content.setTextFormat(Qt.RichText)
        self.lbl_content.setOpenExternalLinks(True)
        self.lbl_content.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
        )
        # 【关键】直接在 QLabel widget 层加左右 padding，保证文字绝不可能贴边框
        # （QSS 级 padding > HTML 内层 padding，哪怕富文本渲染忽略内部 div padding 也稳）
        self.lbl_content.setStyleSheet(
            f"QLabel {{ background: {_DOC_THEME['bg_page']};"
            f" padding: 0 40px 32px 40px; }}"
        )
        self.lbl_content.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self.lbl_content)
        rv.addWidget(scroll, 1)

        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([195, 885])

        self._show_welcome()

    # -----------------------------------------------------------------
    # 加载索引 & 树
    # -----------------------------------------------------------------
    def _load_index(self):
        idx_path = os.path.join(self._doc_dir, "doc_index.json")
        try:
            if not os.path.isfile(idx_path):
                self.lbl_content.setText(
                    f"<div style='padding:30px; color:{_DOC_THEME['primary']};'>"
                    f"❌ 找不到文档索引文件：<br><code>{idx_path}</code></div>"
                )
                return
            with open(idx_path, "r", encoding="utf-8") as f:
                self._index = json.load(f)
        except Exception as e:
            self.lbl_content.setText(
                f"<div style='padding:30px; color:{_DOC_THEME['primary']};'>"
                f"❌ 文档索引加载失败：{e}</div>"
            )
            return

        ver = self._index.get("version", "1.0.0")
        self.lbl_ver.setText(f"v{ver}")

        self.tree.clear()
        self._flat_items.clear()
        chapters = self._index.get("chapters", []) or []
        _EMOJI_LEAD = re.compile(
            r"^[\U0001F300-\U0001FAFF\U0001F000-\U0001F02F\u2600-\u27BF"
            r"\u2B05-\u2B07\u2190-\u2199\u23F8-\u23FA\u2934\u2935\u25AA\u25AB"
            r"\u25B6\u25C0\u2665\u2663\u2660\u2666\u2702\u2705\u2708-\u270D"
            r"\u2728\u2763\u2764\u2795-\u2797\u27A1\u2934\u2935\u3030\u303D"
            r"\u3297\u3299\u00A9\u00AE\u203C\u2049\u2122\u2139\u2190-\u2199"
            r"\u21A9\u21AA\u231A\u231B\u2328\u2388\u23CF\u23E9-\u23F3\u23F8-\u23FA"
            r"\u24C2\u25AA-\u25C0\u25FB-\u25FE\u2600-\u27BF\s]+"
        )
        for ch in chapters:
            parent = QTreeWidgetItem(self.tree)
            raw_title = ch.get("title") or "未命名章节"
            parent.setText(0, _EMOJI_LEAD.sub("", raw_title) or raw_title)
            parent.setFont(0, QFont(_DOC_FONT_STACK, 13, QFont.DemiBold))
            parent.setForeground(0, QColor(_DOC_THEME["text_title"]))
            icon_fn = ch.get("icon")
            if icon_fn:
                pm = pixmap(icon_fn, 20)
                if not pm.isNull():
                    parent.setIcon(0, QIcon(pm))
            parent.setExpanded(True)
            parent.setData(0, Qt.UserRole, None)
            for sub in (ch.get("children") or []):
                child = QTreeWidgetItem(parent)
                raw_sub = sub.get("title") or "未命名小节"
                child.setText(0, _EMOJI_LEAD.sub("", raw_sub) or raw_sub)
                child.setFont(0, QFont(_DOC_FONT_STACK, 13))
                child.setData(0, Qt.UserRole, sub)
                cid = sub.get("id") or ""
                if cid:
                    self._flat_items[cid] = child

    # -----------------------------------------------------------------
    # 交互
    # -----------------------------------------------------------------
    def _on_search(self, text: str):
        text = (text or "").strip().lower()
        if not text:
            for i in range(self.tree.topLevelItemCount()):
                p = self.tree.topLevelItem(i)
                p.setHidden(False); p.setExpanded(False)
            for it in self._flat_items.values():
                it.setHidden(False)
            return
        for cid, it in self._flat_items.items():
            match = text in it.text(0).lower()
            it.setHidden(not match)
        for i in range(self.tree.topLevelItemCount()):
            p = self.tree.topLevelItem(i)
            any_vis = any(not p.child(j).isHidden() for j in range(p.childCount()))
            p.setHidden(not any_vis)
            if any_vis:
                p.setExpanded(True)

    def _on_tree_clicked(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        chapter_title = data.get("title") or "未命名"
        md_file = data.get("file") or ""
        self.lbl_chapter.setText("📄  " + chapter_title)
        md_path = os.path.join(self._doc_dir, md_file) if md_file else ""
        if not md_file or not os.path.isfile(md_path):
            self.lbl_content.setText(
                f"<div style='padding:30px; color:{_DOC_THEME['text_sub']};'>"
                f"（该小节文件不存在：<code>{md_file}</code>）</div>"
            )
            return
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                md_text = f.read()
            html = SimpleMarkdownRenderer.render(md_text)
            self.lbl_content.setText(html)
        except Exception as e:
            self.lbl_content.setText(
                f"<div style='padding:30px; color:{_DOC_THEME['primary']};'>"
                f"❌ 文档读取失败：{e}</div>"
            )

    def _show_welcome(self):
        welcome_md = (
            "# 🍎 赫萝的苹果摊 · 使用手册\n\n"
            "唔姆~苹果商人，欢迎来到咱的小窝。所有的使用说明、节点参数、进阶技巧、常见问题都整理好啦，"
            "咱可是花了不少心思写的，读完了记得留一颗苹果哦 🐺🍎\n\n"
            "## 🧭 怎么用这本手册？\n\n"
            "- **左侧目录**：点一下章节或小节，右侧就会显示对应内容\n"
            "- **右上角搜索框**：输入关键词快速搜章节标题（比如 `找图`、`VarSwitch`、`FAQ`）\n"
            "- 所有文档是纯文本 Markdown，保存在 `config/doc/` 目录，随时可以手动补充\n\n"
            "## 📚 推荐阅读顺序\n\n"
            "1. **🍎 快速上手** → 6 步跑通第一张蓝图，先和咱混个脸熟\n"
            "2. **📖 基础操作** → 项目、画布、节点、连线、变量、运行、截图，基础打牢\n"
            "3. 按需查阅 **🚀应用控制 / 🧠逻辑组件 / 🎯执行动作 / 👁视觉感知** 的节点详解\n"
            "4. 碰到麻烦先查 **❓ 常见问题**，再翻 **⚡ 进阶技巧** 提升效率\n\n"
            "> 🐺 咱的提示：快捷键是 **Ctrl+F9** 运行、**Ctrl+F10** 暂停、**Ctrl+F11** 停止，记住这三个就够日常用啦~\n"
        )
        self.lbl_content.setText(SimpleMarkdownRenderer.render(welcome_md))
