"""
文档 RAG 检索模块。
从 config/doc/ 目录加载攻略文档，根据用户查询关键词检索相关文档片段注入 LLM 提示词。

策略：轻量级关键词匹配（无需向量模型），按节点名/中文标题/功能关键词匹配。
- 蓝图生成模式：始终注入连线+变量基础文档 + 匹配到的节点文档
- 自由聊天模式：仅在用户提问涉及工具功能时注入相关文档
"""
import os
import re
from typing import Dict, List, Optional, Tuple

# ============================================================
# 文档目录定位（复用 doc_viewer 的 4 级优先级回退）
# ============================================================
_DOC_CACHE: Optional[Dict[str, str]] = None  # filename -> content
_DOC_META_CACHE: Optional[List[dict]] = None  # [{filename, title, node_type, keywords, content}]


def _resolve_doc_dir() -> str:
    """定位 config/doc 目录（4 级优先级回退）。"""
    import sys as _sys
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


# ============================================================
# 文档加载 + 索引构建
# ============================================================

# 节点文件名 → (节点类型名, 中文标题) 映射
_NODE_DOC_MAP: Dict[str, Tuple[str, str]] = {
    "node_Start.md":          ("Start",           "开始"),
    "node_End.md":            ("End",             "结束"),
    "node_OpenApp.md":        ("OpenApp",         "启动程序 打开应用"),
    "node_BindWindow.md":     ("BindWindow",      "绑定窗口 绑定程序"),
    "node_WebBrowser.md":     ("WebBrowser",      "打开网页 浏览器 网页"),
    "node_Command.md":        ("Command",         "执行命令 命令行 CMD"),
    "node_HttpRequest.md":    ("HttpRequest",     "网络请求 HTTP 请求 API"),
    "node_Mouse.md":          ("Mouse",           "鼠标 点击 移动 拖拽 滚轮"),
    "node_PressKey.md":       ("PressKey",        "按键 键盘 输入文字"),
    "node_Wait.md":           ("Wait",            "等待 延时 暂停"),
    "node_FindImage.md":      ("FindImage",       "找图 模板匹配 图片搜索"),
    "node_IfImageDetect.md":  ("IfImageDetect",   "多图检测 图片判断"),
    "node_StillnessDetect.md":("StillnessDetect", "静止检测 画面不动"),
    "node_SoundDetect.md":    ("SoundDetect",     "声音检测 音频 音量"),
    "node_Loop.md":           ("Loop",            "循环 重复 遍历"),
    "node_Sequence.md":       ("Sequence",        "顺序执行 串行"),
    "node_SetVariable.md":    ("SetVariable",     "设置变量 赋值 变量"),
    "node_VarSwitch.md":      ("VarSwitch",       "条件判断 分支 switch"),
    "node_Clipboard.md":      ("Clipboard",       "剪贴板 复制 粘贴"),
    "node_Notify.md":         ("Notify",          "通知 提示 弹窗"),
}

# 基础文档 → 关键词映射
_BASIC_DOC_MAP: Dict[str, str] = {
    "14_basic_connection.md": "连线 连接 端口 线 数据线 执行线",
    "15_basic_variable.md":   "变量 局部变量 全局变量 变量引用",
    "12_basic_canvas.md":     "画布 缩放 平移 拖动",
    "13_basic_node.md":       "节点 添加 删除 复制 粘贴 属性",
    "16_basic_run.md":        "运行 调试 断点 启动 停止 暂停",
    "17_basic_screenshot.md": "截图 截屏 截取 捕获",
    "11_basic_project.md":    "项目 工程 新建 保存 打开",
}

# 进阶文档 → 关键词映射
_ADV_DOC_MAP: Dict[str, str] = {
    "31_adv_template.md":     "模板匹配 匹配算法 特征匹配 灰度 hybrid",
    "32_adv_variables.md":    "变量进阶 VarSwitch 条件 流程控制",
    "33_adv_anti_detect.md":  "反检测 拟人化 防封 随机 人类行为",
    "34_adv_optimize.md":     "性能优化 调试 效率 速度",
}

# FAQ 文档 → 关键词
_FAQ_DOC_MAP: Dict[str, str] = {
    "41_faq_basic.md":        "常见问题 FAQ 报错 不行 无法",
    "42_faq_debug.md":        "排错 调试 问题 崩溃 卡住",
    "43_faq_advanced.md":     "高级问题 进阶问题",
}


def _load_all_docs() -> Dict[str, str]:
    """加载所有文档内容（带缓存）。"""
    global _DOC_CACHE
    if _DOC_CACHE is not None:
        return _DOC_CACHE

    _DOC_CACHE = {}
    doc_dir = _resolve_doc_dir()
    try:
        for fname in os.listdir(doc_dir):
            if fname.endswith(".md"):
                path = os.path.join(doc_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        _DOC_CACHE[fname] = f.read()
                except Exception:
                    pass
    except Exception:
        pass
    return _DOC_CACHE


def _build_doc_index() -> List[dict]:
    """构建文档索引：每篇文档的 filename, title, keywords, content。"""
    global _DOC_META_CACHE
    if _DOC_META_CACHE is not None:
        return _DOC_META_CACHE

    docs = _load_all_docs()
    _DOC_META_CACHE = []

    for fname, content in docs.items():
        # 提取标题（第一个 # 行）
        title = ""
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                break

        # 收集关键词
        keywords = []
        if fname in _NODE_DOC_MAP:
            node_type, cn_keywords = _NODE_DOC_MAP[fname]
            keywords.append(node_type.lower())
            keywords.extend(cn_keywords.split())
        elif fname in _BASIC_DOC_MAP:
            keywords.extend(_BASIC_DOC_MAP[fname].split())
        elif fname in _ADV_DOC_MAP:
            keywords.extend(_ADV_DOC_MAP[fname].split())
        elif fname in _FAQ_DOC_MAP:
            keywords.extend(_FAQ_DOC_MAP[fname].split())

        _DOC_META_CACHE.append({
            "filename": fname,
            "title": title,
            "keywords": keywords,
            "content": content,
            "is_node_doc": fname.startswith("node_"),
        })

    return _DOC_META_CACHE


# ============================================================
# 关键词检索
# ============================================================

def _score_doc(doc: dict, query: str) -> int:
    """计算文档与查询的相关性得分（越高越相关）。"""
    query_lower = query.lower()
    score = 0

    # 1. 关键词精确匹配
    for kw in doc["keywords"]:
        if kw.lower() in query_lower:
            score += 10
            # 节点类型名精确匹配加分
            if kw.lower() == query_lower.strip():
                score += 20

    # 2. 文档标题匹配
    if doc["title"]:
        title_lower = doc["title"].lower()
        for word in query_lower.split():
            if len(word) >= 2 and word in title_lower:
                score += 5

    # 3. 内容关键词匹配（取正文前 500 字符做快速匹配）
    content_snippet = doc["content"][:500].lower()
    for word in query_lower.split():
        if len(word) >= 2 and word in content_snippet:
            score += 2

    return score


def retrieve_relevant_docs(
    query: str,
    max_docs: int = 8,
    max_chars: int = 30000,
    always_include_basics: bool = False,
) -> str:
    """根据用户查询检索相关文档，返回格式化的文档上下文字符串。

    Args:
        query: 用户查询文本
        max_docs: 最多返回的文档数
        max_chars: 返回内容最大字符数
        always_include_basics: 是否始终包含连线+变量基础文档

    Returns:
        格式化的文档片段字符串，用于注入 LLM 系统提示词
    """
    index = _build_doc_index()
    if not index:
        return ""

    # 评分排序
    scored = [(doc, _score_doc(doc, query)) for doc in index]
    scored.sort(key=lambda x: x[1], reverse=True)

    # 始终包含的基础文档
    basic_filenames = set()
    if always_include_basics:
        basic_filenames = {"14_basic_connection.md", "15_basic_variable.md"}

    # 收集选中的文档
    selected: List[dict] = []
    used_filenames: set = set()

    # 先加入基础文档
    for doc in index:
        if doc["filename"] in basic_filenames:
            selected.append(doc)
            used_filenames.add(doc["filename"])

    # 再加入按得分排序的文档（跳过已选的）
    for doc, score in scored:
        if score <= 0:
            break
        if doc["filename"] not in used_filenames:
            selected.append(doc)
            used_filenames.add(doc["filename"])
        if len(selected) >= max_docs:
            break

    if not selected:
        return ""

    # 拼接文档内容，控制总长度
    parts: List[str] = []
    total = 0
    for doc in selected:
        content = doc["content"]
        # 如果总长度超限，截断当前文档
        if total + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > 500:  # 至少留 500 字符才有意义
                content = content[:remaining] + "\n\n...(文档截断)"
            else:
                break

        parts.append(f"### 📄 {doc['title']}（{doc['filename']}）\n\n{content}")
        total += len(content)

    if not parts:
        return ""

    return (
        "\n\n--- 参考文档（RAG 检索）---\n"
        "以下是与用户需求相关的攻略文档片段，生成蓝图时请参考其中的参数用法和注意事项：\n\n"
        + "\n\n---\n\n".join(parts)
    )


def build_blueprint_doc_context(user_query: str) -> str:
    """蓝图生成模式的文档上下文：始终包含连线+变量基础 + 匹配到的节点文档。"""
    return retrieve_relevant_docs(
        user_query,
        max_docs=8,
        max_chars=30000,
        always_include_basics=True,
    )


def build_chat_doc_context(user_query: str) -> str:
    """自由聊天模式的文档上下文：仅在用户提问涉及工具功能时注入。"""
    # 先检查是否涉及工具功能
    tool_keywords = [
        "节点", "蓝图", "连线", "变量", "找图", "鼠标", "按键", "等待",
        "循环", "截图", "窗口", "命令", "剪贴板", "通知", "声音", "检测",
        "FindImage", "Mouse", "PressKey", "Wait", "Loop", "BindWindow",
        "OpenApp", "Command", "VarSwitch", "SetVariable", "Clipboard",
        "Notify", "SoundDetect", "StillnessDetect", "IfImageDetect",
        "WebBrowser", "HttpRequest", "Sequence", "Start", "End",
        "怎么用", "怎么设置", "参数", "报错", "不行", "无法",
    ]
    query_lower = user_query.lower()
    if not any(kw.lower() in query_lower for kw in tool_keywords):
        return ""

    return retrieve_relevant_docs(
        user_query,
        max_docs=5,
        max_chars=15000,
        always_include_basics=False,
    )
