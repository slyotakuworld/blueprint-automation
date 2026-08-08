"""
Prompt Builder - System Prompt 组装
从 llm_defaults.json 读人设，从 node_library 读节点清单，组装各 Tab 的系统提示词。
"""
import json
import os
from typing import Dict, List, Optional


def _load_llm_defaults() -> Dict:
    """读取 config/llm_defaults.json（带缓存）。"""
    import sys as _sys

    # 打包模式：优先从 _MEIPASS 读取
    internal = getattr(_sys, "_MEIPASS", None)
    if internal:
        path = os.path.join(internal, "config", "llm_defaults.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "persona" in data:
                return data

    # 开发模式：相对路径
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "config", "llm_defaults.json"
    )
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"LLM 配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not (isinstance(data, dict) and "ui" in data and "blueprint" in data and "persona" in data):
        raise ValueError(f"LLM 配置文件结构不完整：{path}")
    return data


def get_persona_prompt(preset_idx: int = 0) -> str:
    """获取指定预设的系统提示词。"""
    data = _load_llm_defaults()
    presets = data["persona"]["presets"]
    if not (0 <= preset_idx < len(presets)):
        preset_idx = 0
    return presets[preset_idx]["system_prompt"]


def get_greeting(preset_idx: int = 0) -> str:
    """获取指定预设的问候语。"""
    data = _load_llm_defaults()
    presets = data["persona"]["presets"]
    if not (0 <= preset_idx < len(presets)):
        preset_idx = 0
    return presets[preset_idx].get("greeting", "")


def get_forced_names() -> tuple:
    """返回 (forced_self, forced_you)。"""
    data = _load_llm_defaults()
    p = data["persona"]
    return str(p["forced_self"]), str(p["forced_you"])


def _build_profile_context() -> str:
    """从 user_settings 读取用户档案，构建附加上下文。"""
    try:
        from src.config.user_settings import get_llm_profile
        profile = get_llm_profile()
    except Exception:
        return ""

    if not profile:
        return ""

    parts = []
    if profile.get("name"):
        parts.append(f"用户名：{profile['name']}")
    if profile.get("job"):
        parts.append(f"职业：{profile['job']}")
    if profile.get("skill"):
        parts.append(f"技能：{profile['skill']}")
    if profile.get("games"):
        parts.append(f"常玩游戏：{profile['games']}")
    if profile.get("extra"):
        parts.append(f"额外信息：{profile['extra']}")

    if not parts:
        return ""
    return "\n\n--- 用户档案（请在对话中适当参考）---\n" + "\n".join(parts)


def build_chat_prompt(preset_idx: int = 0, user_query: str = "") -> str:
    """
    自由聊天 Tab 的系统提示词。
    纯人设 + 用户档案 + RAG 文档检索（仅在用户提问涉及工具功能时）。
    """
    base = get_persona_prompt(preset_idx)
    forced_self, forced_you = get_forced_names()
    profile_ctx = _build_profile_context()

    # RAG 检索：仅在用户提问涉及工具功能时注入相关文档
    doc_ctx = ""
    if user_query:
        try:
            from src.blueprint.llm.doc_retriever import build_chat_doc_context
            doc_ctx = build_chat_doc_context(user_query)
        except Exception:
            pass

    return base + (
        f"\n\n注意：\n"
        f"· 自称必须用「{forced_self}」，称呼用户必须用「{forced_you}」\n"
        f"· 回答使用中文\n"
        f"· 不要使用 Markdown 标题语法（#），可以用普通文本和换行\n"
        f"· 代码块用 ``` 包裹"
    ) + profile_ctx + doc_ctx


def build_blueprint_prompt(preset_idx: int = 0, user_query: str = "") -> str:
    """
    蓝图生成 Tab 的系统提示词。
    人设 + 蓝图节点库说明 + JSON 格式约束 + RAG 文档检索。
    """
    base = get_persona_prompt(preset_idx)
    forced_self, forced_you = get_forced_names()
    node_docs = _build_node_library_doc()

    profile_ctx = _build_profile_context()

    # RAG 检索：根据用户需求检索相关攻略文档
    doc_ctx = ""
    if user_query:
        try:
            from src.blueprint.llm.doc_retriever import build_blueprint_doc_context
            doc_ctx = build_blueprint_doc_context(user_query)
        except Exception:
            pass

    return base + (
        f"\n\n注意：\n"
        f"· 自称必须用「{forced_self}」，称呼用户必须用「{forced_you}」\n"
        f"· 回答使用中文\n\n"
        f"--- 蓝图生成任务 ---\n"
        f"你需要根据用户的自然语言描述，生成 BlueprintAutomation 蓝图 JSON。\n\n"
        f"可用节点清单：\n{node_docs}\n\n"
        f"JSON 格式规范：\n"
        f'```json\n'
        f'{{\n'
        f'  "nodes": {{\n'
        f'    "node_1": {{"id": "node_1", "type": "Start", "x": 100, "y": 100, "data": {{}}}},\n'
        f'    "node_2": {{"id": "node_2", "type": "OpenApp", "x": 300, "y": 100, "data": {{"path": "notepad.exe"}}}}\n'
        f'  }},\n'
        f'  "links": [\n'
        f'    {{"from_node": "node_1", "from_port": "out_exec", "to_node": "node_2", "to_port": "in_exec", "type": "exec"}}\n'
        f'  ],\n'
        f'  "breakpoints": [],\n'
        f'  "metadata": {{"name": "生成的蓝图", "version": "2.0"}}\n'
        f'}}\n'
        f'```\n\n'
        f"规则：\n"
        f"1. 必须有且仅有一个 Start 节点\n"
        f"2. 节点用 node_1, node_2... 作为 id\n"
        f"3. 连线 type 只有两种：exec（执行端口）和 data（数据端口）\n"
        f"4. 执行端口名：输入 in_exec，输出 out_exec\n"
        f"5. 数据端口名：输入 in_data_0, in_data_1...，输出 out_data_0...\n"
        f"6. data 字段存放节点参数，参数名和默认值参考上方节点清单\n"
        f"7. 只输出 JSON，不要附加解释文字\n"
    ) + doc_ctx + profile_ctx


def build_code_prompt(preset_idx: int = 0) -> str:
    """
    代码助手 Tab 的系统提示词。
    人设 + 代码排查指令。
    """
    base = get_persona_prompt(preset_idx)
    forced_self, forced_you = get_forced_names()

    return base + (
        f"\n\n注意：\n"
        f"· 自称必须用「{forced_self}」，称呼用户必须用「{forced_you}」\n"
        f"· 回答使用中文\n\n"
        f"--- 代码助手任务 ---\n"
        f"你是 BlueprintAutomation 的代码助手，帮助用户排查代码问题。\n"
        f"· 用户会粘贴代码或描述问题，你需要给出诊断和修复建议\n"
        f"· 代码块用 ``` 包裹\n"
        f"· 解释要简洁，先说结论再说原因\n"
        f"· 不涉及蓝图生成功能"
    )


def _build_node_library_doc() -> str:
    """从 node_library 构建节点清单文档，供 LLM 参考。"""
    try:
        from src.blueprint.core.node_library import NODE_TYPES_ORDERED, get_meta
    except Exception:
        return "（节点库加载失败）"

    lines: List[str] = []
    current_cat = ""

    for nt in NODE_TYPES_ORDERED:
        m = get_meta(nt)
        if m is None or m.hidden_in_palette:
            continue

        if m.category != current_cat:
            current_cat = m.category
            lines.append(f"\n## {current_cat}")

        # 节点名 + 标题
        line = f"- {m.type}（{m.title}）"
        if m.description:
            line += f"：{m.description}"

        # 参数
        param_parts = []
        for p in m.params:
            if p.name.startswith("_"):
                continue
            val = p.default if p.default is not None else ""
            if p.help:
                param_parts.append(f"    {p.name}={val}  # {p.help}")
            else:
                param_parts.append(f"    {p.name}={val}")
        if param_parts:
            line += "\n  参数：\n" + "\n".join(param_parts)

        # 端口
        in_ports = [p.name for p in m.inputs if not p.name.startswith("_")]
        out_ports = [p.name for p in m.outputs if not p.name.startswith("_")]
        if in_ports:
            line += f"\n  输入端口：{', '.join(in_ports)}"
        if out_ports:
            line += f"\n  输出端口：{', '.join(out_ports)}"

        lines.append(line)

    return "\n".join(lines)
