from dataclasses import dataclass, field
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
    "list_path",
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "pos": list(self.pos),
            "params": self.params,
            "inputs": [p.to_dict() for p in self.inputs],
            "outputs": [p.to_dict() for p in self.outputs],
            "comment": self.comment,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Node":
        pos = d.get("pos", [0, 0])
        pos_t = (int(pos[0]), int(pos[1])) if len(pos) >= 2 else (0, 0)
        return Node(
            id=d["id"],
            type=d["type"],
            pos=pos_t,
            params=d.get("params", {}),
            inputs=[Port.from_dict(p) for p in d.get("inputs", [])],
            outputs=[Port.from_dict(p) for p in d.get("outputs", [])],
            comment=d.get("comment", ""),
        )


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
            id=d["id"],
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
    version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "nodes": {nid: n.to_dict() for nid, n in sorted(self.nodes.items())},
            "edges": {eid: e.to_dict() for eid, e in sorted(self.edges.items())},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BlueprintGraph":
        g = BlueprintGraph(
            name=d.get("name", "Untitled"),
            version=d.get("version", "1.0"),
        )
        for nid, nd in d.get("nodes", {}).items():
            g.nodes[nid] = Node.from_dict(nd)
        for eid, ed in d.get("edges", {}).items():
            g.edges[eid] = Edge.from_dict(ed)
        return g

    def find_start_node(self) -> Optional[Node]:
        for n in self.nodes.values():
            if n.type == "Start":
                return n
        return None


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
