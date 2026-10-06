from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# ---------------- 端口类型 ----------------
class PortType(Enum):
    EXEC = "exec"
    DATA = "data"


class PortDirection(Enum):
    IN = "in"
    OUT = "out"


DATA_TYPES = [
    "any",
    "bool",
    "int",
    "float",
    "tuple2",
    "tuple4",
    "path",
    "string",
    "str",
    "list_path",
    "list",
    "dict",
    "image",
    "bytes",
]


@dataclass
class Port:
    name: str
    type: PortType
    direction: PortDirection
    data_type: str = "any"
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.value,
            "direction": self.direction.value,
            "data_type": self.data_type,
            "label": self.label or self.name,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Port":
        return Port(
            name=d["name"],
            type=PortType(d["type"]),
            direction=PortDirection(d["direction"]),
            data_type=d.get("data_type", "any"),
            label=d.get("label", ""),
        )


# ---------------- 节点 / 连线 / 图 ----------------
@dataclass
class Node:
    id: str
    type: str
    pos: tuple = (0, 0)
    params: Dict[str, Any] = field(default_factory=dict)
    inputs: List[Port] = field(default_factory=list)
    outputs: List[Port] = field(default_factory=list)
    comment: str = ""
    breakpoint: bool = False
    # 运行时缓存（不序列化！）：PIL图、临时解析结果等
    runtime_attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化：内部对象 → 标准格式 JSON。

        输出字段：
          - type: 通过 NodeMeta.type_short_name 映射为标准短名
          - x/y: 独立 int 坐标字段
          - data: 参数字典
          - outputs: 仅动态端口节点写出（节省空间）
          - breakpoint: bool
          - comment: 字符串
        """
        from .node_library import NODE_LIBRARY

        meta = NODE_LIBRARY.get(self.type)
        # 1. 类型名转换
        q_type: str
        if meta is not None and getattr(meta, "type_short_name", ""):
            q_type = meta.type_short_name
        else:
            q_type = str(self.type)
        # 2. data：参数字典
        data = dict(self.params)
        out: Dict[str, Any] = {
            "id": self.id,
            "type": q_type,
            "x": int(self.pos[0]),
            "y": int(self.pos[1]),
            "data": data,
            "breakpoint": bool(getattr(self, "breakpoint", False)),
        }
        if self.comment:
            out["comment"] = self.comment
        # 3. 动态端口：has_dynamic_ports=True 的节点额外写 inputs/outputs（完整数组（保留用户自定义的 in_N 端口）
        has_dynamic = bool(meta is not None and getattr(meta, "has_dynamic_ports", False))
        if has_dynamic:
            if self.inputs:
                out["inputs"] = [p.to_dict() for p in self.inputs]
            if self.outputs:
                out["outputs"] = [p.to_dict() for p in self.outputs]
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Node":
        """反序列化：加载当前标准格式蓝图。

        读取字段：
          1. 类型名：查 SHORT_NAME_MAP 映射（短名 → 驼峰），不在映射中则原样
          2. 位置：x/y 字段，缺失则 (0, 0)
          3. 参数：data 字段，缺失则空字典
          4. 端口：JSON里有inputs/outputs则用，否则按NODE_LIBRARY默认补
          5. 断点：breakpoint 字段
        """
        from .node_library import NODE_LIBRARY, SHORT_NAME_MAP

        q_type = d.get("type", "")
        # 1. 类型名反向：短名 → 驼峰
        ba_type: str = q_type
        if isinstance(q_type, str) and q_type in SHORT_NAME_MAP:
            ba_type = SHORT_NAME_MAP[q_type]
        meta = NODE_LIBRARY.get(ba_type)

        # 2. 位置：只认 x/y 字段
        if "x" in d and "y" in d:
            pos_t = (int(d["x"]), int(d["y"]))
        else:
            pos_t = (0, 0)

        # 3. 参数：只认 data 字段
        if "data" in d:
            params = dict(d["data"]) if isinstance(d["data"], dict) else {}
        else:
            params = {}

        # 4. 输入输出端口：有则读，没有则按NODE_LIBRARY默认补
        if "inputs" in d and isinstance(d["inputs"], list):
            inputs = [Port.from_dict(p) for p in d["inputs"]]
        elif meta is not None:
            from .node_library import _resolve_input_ports_default
            try:
                defaults = _resolve_input_ports_default(ba_type, params)
                inputs = [Port(**p.__dict__) for p in defaults]
            except Exception:
                inputs = [Port(**p.__dict__) for p in meta.inputs]
        else:
            inputs = []
        if "outputs" in d and isinstance(d["outputs"], list):
            outputs = [Port.from_dict(p) for p in d["outputs"]]
        elif meta is not None:
            from .node_library import _resolve_output_ports_default
            try:
                defaults = _resolve_output_ports_default(ba_type, params)
                outputs = [Port(**p.__dict__) for p in defaults]
            except Exception:
                outputs = [Port(**p.__dict__) for p in meta.outputs]
        else:
            outputs = []

        # 4.5 端口迁移：非动态端口节点，若 JSON 里保存的端口与当前节点库定义不一致
        #     （多为旧版节点定义遗留，如旧版 Sequence 的 out_1/out_2/out_3 分支输出），
        #     以当前定义为准重建端口。动态端口节点（has_dynamic_ports=True，端口由参数
        #     生成）仍以 JSON 为准，避免把用户配置的端口数量弄丢。
        if meta is not None and not bool(getattr(meta, "has_dynamic_ports", False)):
            try:
                from .node_library import _resolve_input_ports_default, _resolve_output_ports_default
                want_in = [pp.name for pp in _resolve_input_ports_default(ba_type, params)]
                want_out = [pp.name for pp in _resolve_output_ports_default(ba_type, params)]
                if [pp.name for pp in inputs] != want_in:
                    inputs = [Port(**pp.__dict__) for pp in _resolve_input_ports_default(ba_type, params)]
                if [pp.name for pp in outputs] != want_out:
                    outputs = [Port(**pp.__dict__) for pp in _resolve_output_ports_default(ba_type, params)]
            except Exception:
                pass

        # 4.6 SetVariable 特例：它的数据输出端口完全由 batch_text 决定（写几个变量就几个出参），
        #     所以即使 JSON 里存的是旧端口（以前只有 out_exec），也按当前定义对齐一次 ——
        #     否则会出现「打开已有蓝图看不到出参，非要再动一下那段文本才冒出来」。
        #     端口名不变的那些连线不受影响（连线按端口名重挂）。
        if ba_type == "SetVariable":
            try:
                from .node_library import _resolve_output_ports_default
                want_out = _resolve_output_ports_default(ba_type, params)
                if [pp.name for pp in outputs] != [pp.name for pp in want_out]:
                    outputs = [Port(**pp.__dict__) for pp in want_out]
            except Exception:
                pass

        n = Node(
            id=d["id"],
            type=ba_type,
            pos=pos_t,
            params=params,
            inputs=inputs,
            outputs=outputs,
            comment=d.get("comment", ""),
        )
        n.breakpoint = bool(d.get("breakpoint", False))
        return n


@dataclass
class Edge:
    id: str
    from_node: str
    from_port: str
    to_node: str
    to_port: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "from_node": self.from_node,
            "from_port": self.from_port,
            "to_node": self.to_node,
            "to_port": self.to_port,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Edge":
        return Edge(
            id=d.get("id") or d.get("from_node", "") + "/" + d.get("from_port", "") + "->" + d.get("to_node", "") + "/" + d.get("to_port", ""),
            from_node=d["from_node"],
            from_port=d["from_port"],
            to_node=d["to_node"],
            to_port=d["to_port"],
        )


@dataclass
class BlueprintGraph:
    name: str
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)
    version: str = "2.0"
    created_at: str = ""
    updated_at: str = ""
    dev_scale_x: float = 1.0
    dev_scale_y: float = 1.0
    # 蓝图类型："blueprint"=普通蓝图 / "component"=组件蓝图（可被 Component 节点调用）。
    # ⚠ 这是「蓝图是什么」的元数据，与「蓝图放在哪个目录」无关 —— 位置由用户在资源面板决定。
    bp_type: str = "blueprint"
    # 运行时：下一个要分配的节点id（1,2,3…自增，每张蓝图独立计数，不序列化）
    _next_node_id: int = 1

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")

    def touch(self):
        """刷新 updated_at = 当前时间。
        ⚠️ 只应该在【真正落盘保存】时调用（graph_store.save），
           绝对不要在 hash 计算 / 判定 dirty / 序列化预览时用：
           如果 to_dict 每次都写 updated_at，两次 hash 计算的时间戳永远不一样，_need_save 永远 True。
        """
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        """序列化：→ 顶层标准结构（nodes+links+breakpoints+metadata）。
        ⚠️ 纯【只读】调用，不修改 self 的任何字段（保证 hash 计算稳定）。
        """
        nodes_d: Dict[str, Any] = {}
        breakpoints_list: List[str] = []
        for nid, n in sorted(self.nodes.items()):
            nd = n.to_dict()
            nodes_d[nid] = nd
            if getattr(n, "breakpoint", False):
                breakpoints_list.append(nid)
        # links 数组
        links: List[Dict[str, Any]] = []
        for eid, e in sorted(self.edges.items()):
            t = self._edge_exec_data_type(eid)
            links.append({
                "from_node": e.from_node,
                "from_port": e.from_port,
                "to_node": e.to_node,
                "to_port": e.to_port,
                "type": t,
            })
        # ★ 注意：这里不再 self.updated_at = datetime.now()！改为只在 save 时 graph.touch() 刷新
        return {
            # bp_type 刻意放顶层：save 时 sort_keys=True 让它落在文件首行，
            # 资源面板/下拉框只需读文件头 4KB 就能判出这张蓝图是普通蓝图还是组件
            "bp_type": str(self.bp_type or "blueprint"),
            "nodes": nodes_d,
            "links": links,
            "breakpoints": breakpoints_list,
            "metadata": {
                "name": self.name,
                "version": "2.0",
                "dev_scale_x": float(self.dev_scale_x),
                "dev_scale_y": float(self.dev_scale_y),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            },
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BlueprintGraph":
        """反序列化：加载标准格式蓝图。"""
        nodes_d = d.get("nodes", {}) if isinstance(d.get("nodes"), dict) else {}
        breakpoints_from_top: set = set(d.get("breakpoints", [])) if isinstance(d.get("breakpoints"), list) else set()

        # 只认 links[] 数组
        if "links" in d and isinstance(d["links"], list):
            links = d["links"]
            edges_d: Dict[str, Any] = {}
            for i, link in enumerate(links):
                if not isinstance(link, dict):
                    continue
                key_tup = (
                    str(link.get("from_node", "")),
                    str(link.get("from_port", "")),
                    str(link.get("to_node", "")),
                    str(link.get("to_port", "")),
                )
                eid = f"e_{abs(hash(key_tup)):010d}"
                edges_d[eid] = {
                    "id": eid,
                    "from_node": key_tup[0],
                    "from_port": key_tup[1],
                    "to_node": key_tup[2],
                    "to_port": key_tup[3],
                }
        else:
            edges_d = {}

        nodes: Dict[str, Node] = {nid: Node.from_dict(nd) for nid, nd in nodes_d.items()}
        edges: Dict[str, Edge] = {eid: Edge.from_dict(ed) for eid, ed in edges_d.items()}

        # 过滤悬空连线：端口在（迁移后的）节点上不存在的连线直接丢弃，
        # 避免旧版端口（如旧 Sequence 的 out_1/out_2/out_3）被清理后留下坏连线。
        valid_edges: Dict[str, Edge] = {}
        for eid, e in edges.items():
            fn = nodes.get(e.from_node)
            tn = nodes.get(e.to_node)
            if fn is None or tn is None:
                continue
            if e.from_port not in {pp.name for pp in fn.outputs}:
                continue
            if e.to_port not in {pp.name for pp in tn.inputs}:
                continue
            valid_edges[eid] = e
        edges = valid_edges

        # 合并断点：顶层 breakpoints list + 每个节点自己的 breakpoint 字段
        for nid, n in nodes.items():
            if nid in breakpoints_from_top:
                n.breakpoint = True
        meta = d.get("metadata") or {}
        name_val = meta.get("name") or d.get("name") or "Untitled"
        g = BlueprintGraph(name=str(name_val))
        g.nodes = nodes
        g.edges = edges
        g.version = str(meta.get("version") or d.get("version") or "2.0")
        g.dev_scale_x = float(meta.get("dev_scale_x", 1.0))
        g.dev_scale_y = float(meta.get("dev_scale_y", 1.0))
        g.created_at = str(meta.get("created_at") or g.created_at)
        g.updated_at = str(meta.get("updated_at") or g.updated_at)
        # 蓝图类型：顶层优先（新格式），metadata 里也认（兜底），都没有则普通蓝图
        _bt = d.get("bp_type") or meta.get("bp_type") or "blueprint"
        g.bp_type = str(_bt).strip().lower() or "blueprint"
        # 根据已加载节点的最大数字id，初始化下一个要分配的自增id（避免与现有节点冲突）
        _max = 0
        for nid in list(g.nodes.keys()):
            try:
                v = int(str(nid))
                if v > _max:
                    _max = v
            except (TypeError, ValueError):
                continue
        g._next_node_id = _max + 1
        return g

    def find_start_node(self) -> Optional[Node]:
        for n in self.nodes.values():
            if n.type == "Start":
                return n
        return None

    # -------- 内部辅助：连线 exec / data 类型判别 --------
    def _edge_exec_data_type(self, edge_id: str) -> str:
        e = self.edges.get(edge_id)
        if e is None:
            return "exec"
        # 按 from_node 的 from_port 端口类型判
        src = self.nodes.get(e.from_node)
        if src is not None:
            for p in src.outputs:
                if p.name == e.from_port:
                    return "exec" if p.type == PortType.EXEC else "data"
        return "exec"


# ---------------- 执行上下文 ----------------
@dataclass
class ExecutionContext:
    run_id: str
    matcher: Any = None
    input_actions: Any = None
    screen: Any = None
    step_counter: int = 0
    is_running: bool = True
    is_paused: bool = False
    variables: Dict[str, Any] = field(default_factory=dict)
    loop_counters: Dict[str, int] = field(default_factory=dict)
    last_outputs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    log_fn: Callable[[str, str], None] = (
        lambda msg, level="INFO": None
    )
    last_progress_ts: float = 0.0
    # 扩展 runtime_attrs：节点运行期零散状态（如 OpenApp 的已启动进程、FindImage 的 PIL 缓存等）
    runtime_attrs: Dict[str, Any] = field(default_factory=dict)
    # 单步调试：点"下一步"按钮后 True；执行完一个节点后自动置 False 并自动 is_paused=True 进入再次暂停（类似 debugger Step Over）
    step_mode: bool = False
    # 当前正在/刚执行完的节点ID（Sequence分支/主循环 两个入口统一写入）：供搜索栏「下一断点/定位执行点」按钮快速跳到画布中执行位置
    current_node_id: Optional[str] = None

    def log(self, msg: str, level: str = "INFO"):
        try:
            self.log_fn(msg, level)
        except Exception:
            pass

    def stop(self):
        self.is_running = False

    def output_for(self, node_id: str, port_name: str, default: Any = None):
        return self.last_outputs.get(node_id, {}).get(port_name, default)

    def set_outputs(self, node_id: str, outputs: Dict[str, Any]):
        self.last_outputs[node_id] = dict(outputs or {})

    def get_var(self, name: str, default: Any = None):
        return self.variables.get(name, default)

    def set_var(self, name: str, value: Any):
        self.variables[name] = value
