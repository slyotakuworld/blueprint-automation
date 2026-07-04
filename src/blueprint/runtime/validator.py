from typing import List, Tuple

from ..core.models import BlueprintGraph, Node, PortType


class ValidationIssue:
    def __init__(self, level: str, code: str, message: str, node_id: str = "", edge_id: str = ""):
        self.level = level    # ERROR | WARN
        self.code = code      # 代码，方便程序判断
        self.message = message
        self.node_id = node_id
        self.edge_id = edge_id

    def __repr__(self):
        return f"[{self.level}] ({self.code}) {self.message}"


def validate_graph(graph: BlueprintGraph) -> List[ValidationIssue]:
    """运行前静态校验 6 条硬约束。"""
    issues: List[ValidationIssue] = []
    nodes = list(graph.nodes.values())

    # 1. 唯一 Start
    starts = [n for n in nodes if n.type == "Start"]
    if len(starts) == 0:
        issues.append(ValidationIssue("ERROR", "NO_START", "缺少 Start 节点：蓝图必须有一个 Start 入口。"))
    elif len(starts) > 1:
        for s in starts[1:]:
            issues.append(ValidationIssue("ERROR", "MULTI_START", f"存在多余 Start 节点（总共 {len(starts)} 个）。", s.id))

    # 2. Start 必须有出边
    if starts:
        s = starts[0]
        has_out = any(
            e.from_node == s.id and e.from_port == "out_exec"
            for e in graph.edges.values()
        )
        if not has_out:
            issues.append(ValidationIssue("WARN", "START_NO_OUT", "Start 节点没有 out_exec 出边，蓝图不会执行任何动作。", s.id))

    # 3. 非 Start 节点 in_exec 必须有上游
    for n in nodes:
        if n.type == "Start":
            continue
        need_exec_in = any(p for p in n.inputs if p.type == PortType.EXEC)
        if not need_exec_in:
            continue
        has_in = any(
            e.to_node == n.id and any(p.name == e.to_port and p.type == PortType.EXEC for p in n.inputs)
            for e in graph.edges.values()
        )
        if not has_in:
            issues.append(ValidationIssue("WARN", "EXEC_UNREACHABLE", f"节点「{n.type}」的 in_exec 没有连接，不会被执行。", n.id))

    # 4. 必填参数
    for n in nodes:
        _check_required_params(n, issues)

    # 5. Loop 自环合法性（简化：只检查 Loop.max_iterations > 0）
    for n in nodes:
        if n.type == "Loop":
            max_it = int(n.params.get("max_iterations", 0) or 0)
            if max_it <= 0:
                issues.append(ValidationIssue("ERROR", "LOOP_NO_LIMIT", f"Loop 节点 max_iterations 必须 > 0（当前={max_it}）。", n.id))

    # 6. DATA 连线类型兼容
    nodes_by_id = graph.nodes
    for e in graph.edges.values():
        fn, tn = nodes_by_id.get(e.from_node), nodes_by_id.get(e.to_node)
        if not (fn and tn):
            issues.append(ValidationIssue("ERROR", "EDGE_DANGLING", f"连线 {e.id} 端点节点不存在。", "", e.id))
            continue
        from_port = next((p for p in fn.outputs if p.name == e.from_port), None)
        to_port = next((p for p in tn.inputs if p.name == e.to_port), None)
        if not (from_port and to_port):
            issues.append(ValidationIssue("ERROR", "EDGE_PORT_MISSING", f"连线 {e.id} 的端点端口不存在。", "", e.id))
            continue
        if from_port.type != to_port.type:
            issues.append(ValidationIssue("ERROR", "EDGE_PORT_TYPE", f"连线 {e.id}：EXEC 与 DATA 端口不能互接。", "", e.id))
            continue
        if from_port.type == PortType.DATA:
            a, b = from_port.data_type, to_port.data_type
            if a != b and a != "any" and b != "any":
                issues.append(
                    ValidationIssue(
                        "WARN", "DATA_TYPE_MISMATCH",
                        f"连线 {e.id} 数据类型不兼容：{a} -> {b}（其中一方应为 any）。",
                        "", e.id,
                    )
                )
    return issues


def _check_required_params(n: Node, issues: List[ValidationIssue]):
    def _not_empty(v) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        return True

    t = n.type
    if t == "FindImage":
        if not _not_empty(n.params.get("template")):
            issues.append(ValidationIssue("ERROR", "FIND_NO_TEMPLATE", "FindImage.template 不能为空（需要一个模板路径）。", n.id))
    elif t == "PressKey":
        kb_mode = str(n.params.get("kb_mode", "text") or "text")
        if kb_mode == "text":
            if not _not_empty(n.params.get("text")):
                issues.append(ValidationIssue("WARN", "PRESS_NO_TEXT", "PressKey.text 为空（输入文本模式）。", n.id))
        else:
            if not _not_empty(n.params.get("key_name")):
                issues.append(ValidationIssue("ERROR", "PRESS_NO_KEY", "PressKey.key_name 不能为空（按键组合模式）。", n.id))
    elif t == "SetVariable":
        has_batch = bool(str(n.params.get("batch_text", "") or "").strip())
        if not has_batch:
            issues.append(ValidationIssue("WARN", "SET_EMPTY", "SetVariable 未设置任何变量：batch_text 为空。", n.id))
    elif t == "VarSwitch":
        from ..core.node_library import parse_varswitch_conditions
        raw = n.params.get("cases", "") or ""
        if isinstance(raw, str) and raw.strip():
            conds = parse_varswitch_conditions(raw)
            valid = [c for c in conds if not c.get("error")]
            errors = [c for c in conds if c.get("error")]
            if not valid and errors:
                head = "; ".join([f"第{c['line_no']}行:{c['error']}" for c in errors[:3]])
                issues.append(ValidationIssue("ERROR", "VSWITCH_SYNTAX",
                    f"VarSwitch 条件语法错误（{len(errors)}处）：{head}", n.id))
            elif not valid:
                issues.append(ValidationIssue("WARN", "VSWITCH_EMPTY",
                    "VarSwitch 未识别到任何合法条件分支，运行时会直接走 else。", n.id))
        elif not (isinstance(raw, list) and raw):
            issues.append(ValidationIssue("WARN", "VSWITCH_EMPTY",
                "VarSwitch 未设置任何条件，运行时会直接走 else。", n.id))


def has_errors(issues: List[ValidationIssue]) -> bool:
    return any(i.level == "ERROR" for i in issues)


def format_issues(issues: List[ValidationIssue]) -> str:
    if not issues:
        return "（无问题）"
    lines = []
    for it in issues:
        lines.append(f"[{it.level}] {it.message}")
    return "\n".join(lines)
