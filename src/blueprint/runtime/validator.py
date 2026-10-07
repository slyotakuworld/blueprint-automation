import os
from typing import List, Optional

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


def validate_graph(graph: BlueprintGraph, project_root: Optional[str] = None) -> List[ValidationIssue]:
    """运行前静态校验。

    参数 project_root：当前工程根目录。只有传了它才会做「组件端口数一致性」检查
    （要据此把组件节点的 blueprint_path 解析成绝对路径去读那张组件蓝图）。
    不传 = 跳过该项，其余检查照常。
    """
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
        _check_required_params(n, issues, graph)

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

    # 7. 坐标基准配对：FindImage.out_pos → Mouse.in_pos 两边 range_mode 必须一致
    #
    # FindImage 的 out_pos 是「按自己 range_mode 换算过的坐标」：
    #   · fullscreen → 绝对屏幕坐标
    #   · window     → 相对目标窗口左上角的坐标
    # Mouse 拿到 in_pos 后又会按**自己的** range_mode 再换算一次，所以两边基准不一致时
    # 会重复叠加（或漏掉）一次窗口偏移 —— 点是能点上，但位置系统性偏掉，且运行时不报错。
    # 这类"静默做错事"必须在跑之前就说出来，所以放在静态校验里。
    _BASE_CN = {"window": "目标窗口", "fullscreen": "全屏"}
    for e in graph.edges.values():
        fn, tn = nodes_by_id.get(e.from_node), nodes_by_id.get(e.to_node)
        if not (fn and tn):
            continue
        if (fn.type == "FindImage" and tn.type == "Mouse"
                and e.from_port == "out_pos" and e.to_port == "in_pos"):
            fmode = str(fn.params.get("range_mode", "window") or "window")
            tmode = str(tn.params.get("range_mode", "window") or "window")
            if fmode != tmode:
                issues.append(
                    ValidationIssue(
                        "WARN", "POS_BASE_MISMATCH",
                        f"坐标基准不一致：FindImage「找图范围」= {_BASE_CN.get(fmode, fmode)}，"
                        f"Mouse「坐标基准」= {_BASE_CN.get(tmode, tmode)}。"
                        f"找图输出的坐标会按 Mouse 的基准再换算一次，点击位置会整体偏掉。"
                        f"请把两边的基准改成一致。",
                        tn.id, e.id,
                    )
                )

    # 8. Component 组件的端口数 vs 被调用蓝图 Start/End 的声明
    #
    # 调用方的 num_inputs / num_outputs 与被调用方 Start.num_params / End.num_returns
    # 是两套各自独立的数字，运行期靠「按序对应」传参：
    #     调用方 in_2 → 变量 __arg_2 → 组件 Start 的 param_2
    # 数量不一致时两边都用 .get() 取值，**不报错、不警告**，只会静默错位：
    # 多传的端口被丢弃，少传的端口读到 None。属于典型「静默做错事」，必须在跑之前说出来。
    if project_root:
        for n in nodes:
            if n.type == "Component":
                _check_component_ports(n, project_root, issues)

    # 9. 加载时被丢弃的悬空连线
    #
    # BlueprintGraph.from_dict 对「端点节点不存在 / 端口已不存在」的连线是**丢弃**处理
    # （为了兼容旧版被清理掉的端口，如旧 Sequence 的 out_1/out_2/out_3）。
    # 以前是纯静默的：子画布里断了一根线，加载后就无声消失，排查时看不到任何痕迹。
    # 现在 from_dict 把丢掉的连线记在 graph.dropped_links 上，这里报出来。
    for link in (getattr(graph, "dropped_links", None) or []):
        issues.append(ValidationIssue(
            "WARN", "DANGLING_LINK",
            f"有一条连线被丢弃（{link.get('reason', '端口或节点不存在')}）："
            f"{link.get('from_node', '?')}.{link.get('from_port', '?')}"
            f" → {link.get('to_node', '?')}.{link.get('to_port', '?')}。它不会参与执行。",
        ))

    # 注：曾经有过第 10 条「容器子画布里必须有「结束(End)」节点」的校验（CONTAINER_NO_END），
    #     随「必须走到 End 才算完成」这条口径一起**作废**：
    #     现在三个容器（Loop / Sequence / Parallel）都**不要求接「结束」节点**，
    #     判定口径是「只有节点执行异常 / 死循环才失败」，对缺 End 的蓝图报警属于误报。
    #     （2026-10-06 用户定案）

    # 11. 「执行命令」节点安全审查（2026-10-07 免责/防误跑）
    #
    # 蓝图是可导入、可分发的 JSON，而 Command 节点会以**当前用户权限**原样执行
    # 命令文本——「别人发来的蓝图夹带破坏性命令」是真实风险面。这类「静默做危险
    # 事」必须在跑之前亮出来：① 有 Command 节点 → WARN 清点；② 命中高危特征
    # → 额外发 DANGEROUS_CMD，编辑器运行前会**弹窗二次确认**（不直接禁止，
    # 因为删文件/关机等也可能是合法的自动化需求，拦不拦由用户临场判断）。
    cmd_nodes = scan_command_nodes(graph)
    if cmd_nodes:
        issues.append(ValidationIssue(
            "WARN", "CMD_NODES",
            f"本蓝图包含 {len(cmd_nodes)} 个「执行命令」节点：运行时会以当前用户权限"
            f"原样执行其中的命令（命令支持 {{变量}} 插值，实际内容可能随变量变化）。"
            f"若蓝图来自他人，请先逐个审查命令内容。",
        ))
        for nid, cmd in cmd_nodes:
            low = cmd.lower()
            hits = [p for p in DANGEROUS_CMD_PATTERNS if p in low]
            if hits:
                preview = cmd[:80] + ("…" if len(cmd) > 80 else "")
                issues.append(ValidationIssue(
                    "WARN", "DANGEROUS_CMD",
                    f"高危命令特征（{'、'.join(hits[:3])}）：节点 {nid} 的命令为：{preview}。"
                    f"请确认这是你有意写入的命令。",
                    str(nid),
                ))

    return issues


# ============================================================
# 「执行命令」节点安全审查（2026-10-07）
# ============================================================

# 高危命令特征：小写子串匹配。宁可多报（多一道人工确认）不可漏报；
# 不直接禁止——删除/关机也可能是挂机脚本的合法收尾，拦截权留给用户临场判断。
DANGEROUS_CMD_PATTERNS = (
    "format ", "format/q",                 # 格式化磁盘
    "del /s", "del /f", "erase /s",        # 递归/强制删除
    "rd /s", "rmdir /s",                   # 递归删目录
    "diskpart", "cipher /w", "vssadmin",   # 磁盘操作 / 抹除空闲空间 / 删卷影副本
    "bcdedit",                             # 改启动配置
    "reg delete", "reg add hkey",          # 改注册表
    "remove-item -recurse", "rm -rf",      # PowerShell / 跨平台递归删除
    "dd if=",                              # 跨平台裸写磁盘
    "shutdown",                            # 关机/重启
    "taskkill /f",                         # 强杀进程
    "powershell -enc", "invoke-expression",# 混淆/动态执行代码
)

# 容器节点的子画布 body 参数键（与编辑器 _CONTAINER_BODY_KEYS / 执行器读取处一致）
_CONTAINER_BODY_PARAM_KEYS = ("loop_body", "seq_body", "par_body")


def scan_command_nodes(graph) -> list:
    """递归收集一张图里的全部「执行命令」节点 → [(节点id, 命令文本)]。

    递归范围：容器节点（Loop/Sequence/Parallel）params 里的子画布 body
    （loop_body / seq_body / par_body），以及 body 里再嵌套的容器。
    body 是编辑器 `_sub_canonical` / 加载落盘的 canonical dict：
    nodes 为 {id: {"type": 短名, "data": {参数...}, ...}}（Node.to_dict 格式）。

    供两处使用：validate_graph 的运行前校验、编辑器打开蓝图时的提示。
    """
    from ..core.node_library import NODE_LIBRARY

    cmd_short = ""
    meta = NODE_LIBRARY.get("Command")
    if meta is not None:
        cmd_short = getattr(meta, "type_short_name", "") or ""

    out = []

    def _grab(nid, ntype, params):
        # 根图的 Node.type 是驼峰全名；body 里的 "type" 是短名（cmd）——两个都认
        if ntype == "Command" or (cmd_short and ntype == cmd_short):
            out.append((str(nid), str(params.get("command", "") or "")))
        for key in _CONTAINER_BODY_PARAM_KEYS:
            body = params.get(key)
            if isinstance(body, dict) and isinstance(body.get("nodes"), dict) and body["nodes"]:
                for cid, cd in body["nodes"].items():
                    if not isinstance(cd, dict):
                        continue
                    cdata = cd.get("data")
                    _grab(cid, str(cd.get("type", "") or ""),
                          cdata if isinstance(cdata, dict) else {})

    try:
        nodes_iter = list(graph.nodes.values())
    except Exception:
        return out
    for n in nodes_iter:
        _grab(n.id, n.type, n.params)
    return out


def _check_component_ports(n: Node, project_root: str, issues: List[ValidationIssue]):
    """核对组件节点端口数与组件定义是否一致（只产生 issue，不改任何东西）。"""
    raw = str(n.params.get("blueprint_path", "") or "").strip()
    if not raw:
        issues.append(ValidationIssue(
            "WARN", "COMP_NO_PATH", "组件节点没有选择要调用的蓝图，运行时会被跳过。", n.id))
        return
    path = raw if os.path.isabs(raw) else os.path.join(project_root, raw)
    if not os.path.isfile(path):
        issues.append(ValidationIssue(
            "ERROR", "COMP_PATH_MISSING",
            f"组件蓝图文件不存在：{raw}（解析为 {path}）。运行时该组件会被当作空操作跳过。", n.id))
        return
    try:
        from ..core.graph_store import BlueprintStore
        sub = BlueprintStore.load(path)
    except Exception as e:
        issues.append(ValidationIssue("WARN", "COMP_LOAD_FAIL", f"组件蓝图加载失败：{e}", n.id))
        return
    if sub is None:
        issues.append(ValidationIssue("WARN", "COMP_LOAD_FAIL", f"组件蓝图读取为空：{raw}", n.id))
        return

    n_in = None
    n_out = None
    for sn in (getattr(sub, "nodes", {}) or {}).values():
        t = getattr(sn, "type", "")
        p = getattr(sn, "params", None) or {}
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

    try:
        cur_in = int(n.params.get("num_inputs", 0) or 0)
    except Exception:
        cur_in = 0
    try:
        cur_out = int(n.params.get("num_outputs", 0) or 0)
    except Exception:
        cur_out = 0

    diffs = []
    if n_in is not None and n_in != cur_in:
        diffs.append(f"入参：组件 Start 声明 {n_in} 个，本节点设了 {cur_in} 个")
    if n_out is not None and n_out != cur_out:
        diffs.append(f"返回值：组件 End 声明 {n_out} 个，本节点设了 {cur_out} 个")
    if diffs:
        issues.append(ValidationIssue(
            "WARN", "COMP_PORT_MISMATCH",
            f"组件端口数不一致（{os.path.basename(raw)}）：{'；'.join(diffs)}。"
            f"两边数量必须相等才能按序对上，否则多传的值被丢弃、少传的端口读到空值。"
            f"请手动把本节点端口数改成与组件一致（编辑器不会自动改，避免重建端口导致断线）。",
            n.id,
        ))


def _check_required_params(n: Node, issues: List[ValidationIssue], graph: BlueprintGraph = None):
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
            # 文本可能来自参数 text，也可能来自左侧「文本内容」端口（接了上游就有值），
            # 两者都没有才是真·空。
            port_linked = bool(graph) and any(
                e.to_node == n.id and e.to_port == "in_text" for e in graph.edges.values()
            )
            if not port_linked and not _not_empty(n.params.get("text")):
                issues.append(ValidationIssue(
                    "WARN", "PRESS_NO_TEXT",
                    "PressKey.text 为空（输入文本模式），且左侧「文本内容」端口没有连线。", n.id,
                ))
        else:
            # 按键组合模式：检查 key_combo（JSON数组）是否为空
            key_combo_raw = str(n.params.get("key_combo", "[]") or "[]")
            has_keys = False
            try:
                import json as _json
                kc = _json.loads(key_combo_raw) if isinstance(key_combo_raw, str) else key_combo_raw
                if isinstance(kc, list) and any(k.strip() for k in kc if isinstance(k, str)):
                    has_keys = True
            except Exception:
                pass
            if not has_keys:
                issues.append(ValidationIssue("ERROR", "PRESS_NO_KEY", "PressKey 按键组合为空（请至少选择一个按键）。", n.id))
    elif t == "SetVariable":
        has_batch = bool(str(n.params.get("batch_text", "") or "").strip())
        if not has_batch:
            issues.append(ValidationIssue("WARN", "SET_EMPTY", "SetVariable 未设置任何变量：batch_text 为空。", n.id))
        else:
            # 写法不对的行认不下来 → 既不会变成变量，也不会生成右侧数据出口。
            # 运行前先明确点出来，别让用户对着「写了变量却没有出参」猜。
            from ..core.node_library import _setvar_parse_issues
            names, errs = _setvar_parse_issues(n.params.get("batch_text"))
            if errs:
                head = "; ".join(errs[:3])
                if len(errs) > 3:
                    head += f" …等{len(errs)}行"
                issues.append(ValidationIssue("WARN", "SET_SYNTAX",
                    f"SetVariable 有 {len(errs)} 行写法不正确、不会生效（已认下 {len(names)} 个变量）：{head}",
                    n.id))
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
