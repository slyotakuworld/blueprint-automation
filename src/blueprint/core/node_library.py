from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import Port, PortDirection, PortType, Node


# ============================================================
# 参数编辑器类型（Inspector 用）
# ============================================================
EDITOR_INT = "int"
EDITOR_FLOAT = "float"
EDITOR_STRING = "string"
EDITOR_BOOL = "bool"
EDITOR_PATH = "path"
EDITOR_ENUM = "enum"
EDITOR_TUPLE2 = "tuple2"
EDITOR_TUPLE4 = "tuple4"
EDITOR_TEMPLATE_PICKER = "template_picker"


ON_FAIL_OPTIONS = ["stop", "continue", "retry_then_skip"]
CLICK_MODE_OPTIONS = ["position", "region", "key"]


# ============================================================
# 节点分类
# ============================================================
CATEGORY_CONTROL = "控制流"
CATEGORY_ACTION = "动作/识别"
CATEGORY_LITERAL = "常量/输入"
CATEGORY_VARIABLE = "变量"
CATEGORY_META = "元/场景"

CATEGORIES_ORDER = [
    CATEGORY_CONTROL,
    CATEGORY_ACTION,
    CATEGORY_LITERAL,
    CATEGORY_VARIABLE,
    CATEGORY_META,
]

# ============================================================
# 参数定义
# ============================================================
@dataclass
class ParamDef:
    name: str
    editor: str
    default: Any = None
    label: str = ""
    options: Optional[List[Any]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: float = 1.0
    required: bool = False
    help: str = ""


# ============================================================
# 节点元数据
# ============================================================
@dataclass
class NodeMeta:
    type: str
    category: str
    title: str
    description: str = ""
    color: str = "#3A3F4B"
    accent: str = "#58A6FF"
    inputs: List[Port] = field(default_factory=list)
    outputs: List[Port] = field(default_factory=list)
    params: List[ParamDef] = field(default_factory=list)

    def default_params(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params if p.default is not None}


# ============================================================
# 快捷构造函数
# ============================================================
def _exec_in(name="in_exec", label=""):
    return Port(name=name, type=PortType.EXEC, direction=PortDirection.IN, label=label or "▶")


def _exec_out(name="out_exec", label=""):
    return Port(name=name, type=PortType.EXEC, direction=PortDirection.OUT, label=label or "▶")


def _data_in(name, data_type="any", label=""):
    return Port(name=name, type=PortType.DATA, direction=PortDirection.IN, data_type=data_type, label=label or name)


def _data_out(name, data_type="any", label=""):
    return Port(name=name, type=PortType.DATA, direction=PortDirection.OUT, data_type=data_type, label=label or name)


# ============================================================
# 16 个 MVP 节点定义
# ============================================================
NODE_LIBRARY: Dict[str, NodeMeta] = {
    # ---------------- 控制流 ----------------
    "Start": NodeMeta(
        type="Start",
        category=CATEGORY_CONTROL,
        title="开始",
        description="蓝图唯一入口，每张图必须且仅能有 1 个。",
        color="#238636",
        accent="#3FB950",
        outputs=[_exec_out()],
    ),
    "Branch": NodeMeta(
        type="Branch",
        category=CATEGORY_CONTROL,
        title="条件分支",
        description="if / else 二分支，根据 cond 为真走 out_true，假走 out_false。",
        color="#9A6700",
        accent="#F0B429",
        inputs=[
            _exec_in(),
            _data_in("in_cond", "bool", label="条件"),
        ],
        outputs=[
            _exec_out("out_true", label="真"),
            _exec_out("out_false", label="假"),
        ],
    ),
    "Loop": NodeMeta(
        type="Loop",
        category=CATEGORY_CONTROL,
        title="循环",
        description="while 语义；每轮走 loop_body，超过 max_iterations 或条件不满足走 out_exit。",
        color="#8250DF",
        accent="#A371F7",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("loop_body", label="循环体"),
            _exec_out("out_exit", label="退出"),
        ],
        params=[
            ParamDef(name="max_iterations", editor=EDITOR_INT, default=9999, label="最大迭代", min=1, max=10000000, step=1, required=True, help="硬约束：防止死循环"),
            ParamDef(name="interval_sec", editor=EDITOR_FLOAT, default=0.0, label="每轮间隔(s)", min=0.0, max=60.0, step=0.05),
        ],
    ),
    "Wait": NodeMeta(
        type="Wait",
        category=CATEGORY_CONTROL,
        title="等待",
        description="睡眠一段时间。优先走 in_seconds 数据线，否则用默认参数。",
        color="#2D333B",
        accent="#8B949E",
        inputs=[
            _exec_in(),
            _data_in("in_seconds", "float", label="秒数"),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="default_sec", editor=EDITOR_FLOAT, default=1.0, label="默认秒数", min=0.0, max=3600.0, step=0.1),
        ],
    ),
    "TargetSetup": NodeMeta(
        type="TargetSetup",
        category=CATEGORY_META,
        title="场景/窗口绑定",
        description="启动目标EXE（可选）+ 绑定目标窗口 HWND、校准分辨率、激活前台，后续找图/点击均以此为基准。",
        color="#8957E5",
        accent="#BC8CFF",
        inputs=[_exec_in()],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="exe_path", editor=EDITOR_PATH, default="", label="启动EXE路径", help="可选；填写后会先启动程序，等待秒数后再绑窗口。"),
            ParamDef(name="exe_args", editor=EDITOR_STRING, default="", label="启动参数(空格分隔)"),
            ParamDef(name="working_dir", editor=EDITOR_PATH, default="", label="工作目录(可选)"),
            ParamDef(name="wait_sec_after_launch", editor=EDITOR_FLOAT, default=2.0, label="启动后等待(s)", min=0.0, max=120.0, step=0.1),
            ParamDef(name="window_title", editor=EDITOR_STRING, default="", label="窗口标题(子串)", required=True, help="用于 FindWindow；留空表示全屏（仍可配合启动EXE使用）"),
            ParamDef(name="resolution_w", editor=EDITOR_INT, default=1920, label="参考宽", min=800, max=7680, step=1),
            ParamDef(name="resolution_h", editor=EDITOR_INT, default=1080, label="参考高", min=600, max=4320, step=1),
        ],
    ),
    # ---------------- 动作 / 识别 ----------------
    "FindImage": NodeMeta(
        type="FindImage",
        category=CATEGORY_ACTION,
        title="找图",
        description="在屏幕或指定区域内用模板匹配找指定模板。找到输出 out_pos 与 out_found=True，并继续执行。",
        color="#0969DA",
        accent="#58A6FF",
        inputs=[
            _exec_in(),
            _data_in("in_template", "path", label="模板"),
            _data_in("in_region", "tuple4", label="区域"),
            _data_in("in_threshold", "float", label="阈值"),
        ],
        outputs=[
            _exec_out(),
            _data_out("out_pos", "tuple2", label="位置"),
            _data_out("out_found", "bool", label="找到"),
            _data_out("out_score", "float", label="最高得分"),
        ],
        params=[
            ParamDef(name="template", editor=EDITOR_PATH, default="", label="模板路径", required=True),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.75, label="匹配阈值", min=0.3, max=1.0, step=0.01),
            ParamDef(name="fast_mode", editor=EDITOR_BOOL, default=True, label="快速模式"),
            ParamDef(name="use_gray", editor=EDITOR_BOOL, default=True, label="灰度匹配"),
            ParamDef(name="on_fail", editor=EDITOR_ENUM, default="stop", label="失败策略", options=ON_FAIL_OPTIONS, help="stop=终止；continue=继续；retry_then_skip=重试几次后跳过"),
            ParamDef(name="retry_count", editor=EDITOR_INT, default=3, label="重试次数", min=0, max=100, step=1),
            ParamDef(name="retry_interval_ms", editor=EDITOR_FLOAT, default=500.0, label="重试间隔(ms)", min=50.0, max=60000.0, step=10.0),
        ],
    ),
    "FindAnyImage": NodeMeta(
        type="FindAnyImage",
        category=CATEGORY_ACTION,
        title="多图任找(分支)",
        description="依次尝试多张模板，命中的那张走对应的 out_found_N 分支；全部未中走 out_not_found。",
        color="#0969DA",
        accent="#79C0FF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_not_found", label="都没找到"),
        ],
        params=[
            ParamDef(name="templates", editor=EDITOR_STRING, default="", label="模板列表(逗号分隔)", required=True, help="如: btn_a.png,btn_b.png,btn_c.png"),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.75, label="阈值", min=0.3, max=1.0, step=0.01),
            ParamDef(name="fast_mode", editor=EDITOR_BOOL, default=True, label="快速模式"),
            ParamDef(name="timeout_ms", editor=EDITOR_FLOAT, default=0.0, label="总超时(ms)", min=0.0, max=3600000.0, step=100.0, help="0=只查一次不等待"),
            ParamDef(name="interval_ms", editor=EDITOR_FLOAT, default=300.0, label="轮询间隔(ms)", min=50.0, max=60000.0, step=10.0),
        ],
    ),
    "WaitAnyImage": NodeMeta(
        type="WaitAnyImage",
        category=CATEGORY_ACTION,
        title="等待多图任一",
        description="轮询多张模板直到命中或超时。命中后 out_found_index 输出从 0 起的序号，out_pos 输出位置。",
        color="#0550AE",
        accent="#54AEFF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out(),
            _data_out("out_pos", "tuple2", label="命中位置"),
            _data_out("out_found_index", "int", label="命中序号"),
            _data_out("out_found", "bool", label="是否命中"),
        ],
        params=[
            ParamDef(name="templates", editor=EDITOR_STRING, default="", label="模板列表(逗号分隔)", required=True),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.75, label="阈值", min=0.3, max=1.0, step=0.01),
            ParamDef(name="timeout_ms", editor=EDITOR_FLOAT, default=5000.0, label="超时(ms)", min=100.0, max=3600000.0, step=100.0),
            ParamDef(name="interval_ms", editor=EDITOR_FLOAT, default=300.0, label="轮询间隔(ms)", min=50.0, max=60000.0, step=10.0),
            ParamDef(name="fast_mode", editor=EDITOR_BOOL, default=True, label="快速模式"),
        ],
    ),
    "Click": NodeMeta(
        type="Click",
        category=CATEGORY_ACTION,
        title="点击/按键",
        description="鼠标点击指定位置/区域中心，或按下指定按键。支持拟人化随机偏移与保持时间。",
        color="#DA3633",
        accent="#F85149",
        inputs=[
            _exec_in(),
            _data_in("in_pos", "tuple2", label="坐标"),
            _data_in("in_region", "tuple4", label="区域"),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="mode", editor=EDITOR_ENUM, default="position", label="模式", options=CLICK_MODE_OPTIONS, help="position=坐标；region=区域中心；key=按键"),
            ParamDef(name="key", editor=EDITOR_STRING, default="", label="按键"),
            ParamDef(name="double", editor=EDITOR_BOOL, default=False, label="双击"),
            ParamDef(name="post_delay", editor=EDITOR_FLOAT, default=0.1, label="点击后延时(s)", min=0.0, max=10.0, step=0.01),
            ParamDef(name="random_offset_radius", editor=EDITOR_INT, default=8, label="随机偏移半径(px)", min=0, max=200, step=1),
            ParamDef(name="hold_ms_min", editor=EDITOR_INT, default=30, label="按下保持最小(ms)", min=1, max=5000, step=1),
            ParamDef(name="hold_ms_max", editor=EDITOR_INT, default=80, label="按下保持最大(ms)", min=1, max=5000, step=1),
            ParamDef(name="post_delay_min_ms", editor=EDITOR_INT, default=80, label="后延时最小(ms)", min=0, max=10000, step=1),
            ParamDef(name="post_delay_max_ms", editor=EDITOR_INT, default=200, label="后延时最大(ms)", min=0, max=10000, step=1),
        ],
    ),
    "PressKey": NodeMeta(
        type="PressKey",
        category=CATEGORY_ACTION,
        title="按键",
        description="按下并释放硬件级按键（scan code）。支持多次按。",
        color="#B62324",
        accent="#FF7B72",
        inputs=[_exec_in()],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="key", editor=EDITOR_STRING, default="", label="按键名", required=True, help="如: enter, space, esc, a, 1, up, f1, lctrl"),
            ParamDef(name="count", editor=EDITOR_INT, default=1, label="次数", min=1, max=1000, step=1),
            ParamDef(name="hold_ms", editor=EDITOR_INT, default=50, label="保持(ms)", min=1, max=5000, step=1),
            ParamDef(name="interval_ms", editor=EDITOR_INT, default=100, label="间隔(ms)", min=1, max=60000, step=1),
        ],
    ),
    # ---------------- 常量 / 输入 ----------------
    "RegionLiteral": NodeMeta(
        type="RegionLiteral",
        category=CATEGORY_LITERAL,
        title="区域常量",
        description="输出一个 (x,y,w,h) 常量区域，供 FindImage 的 in_region 或 Click 的 in_region 连接。",
        color="#227D51",
        accent="#3FB950",
        outputs=[_data_out("out_region", "tuple4", label="区域")],
        params=[
            ParamDef(name="x", editor=EDITOR_INT, default=0, label="X", min=0, max=10000, step=1, required=True),
            ParamDef(name="y", editor=EDITOR_INT, default=0, label="Y", min=0, max=10000, step=1, required=True),
            ParamDef(name="w", editor=EDITOR_INT, default=100, label="W", min=1, max=10000, step=1, required=True),
            ParamDef(name="h", editor=EDITOR_INT, default=100, label="H", min=1, max=10000, step=1, required=True),
        ],
    ),
    "TemplatePicker": NodeMeta(
        type="TemplatePicker",
        category=CATEGORY_LITERAL,
        title="模板(截图入库)",
        description="点击「裁图」按钮弹出全屏半透明遮罩，拖拽框选区域后自动保存到 images/ 目录。",
        color="#1F6FEB",
        accent="#79C0FF",
        outputs=[_data_out("out_template", "path", label="模板路径")],
        params=[
            ParamDef(name="template_path", editor=EDITOR_TEMPLATE_PICKER, default="", label="模板路径", required=True),
        ],
    ),
    # ---------------- 变量 ----------------
    "EdgeCounter": NodeMeta(
        type="EdgeCounter",
        category=CATEGORY_VARIABLE,
        title="边沿计数",
        description="累计 in_signal 的 false→true 与 true→false 次数；每次 in_exec 触发时输出单帧脉冲 edge_rising / edge_falling。",
        color="#2D333B",
        accent="#D29922",
        inputs=[
            _exec_in(),
            _data_in("in_signal", "bool", label="信号"),
        ],
        outputs=[
            _exec_out(),
            _data_out("rising_count", "int", label="上升沿累计"),
            _data_out("falling_count", "int", label="下降沿累计"),
            _data_out("edge_rising", "bool", label="本帧上升"),
            _data_out("edge_falling", "bool", label="本帧下降"),
        ],
    ),
    "SetVariable": NodeMeta(
        type="SetVariable",
        category=CATEGORY_VARIABLE,
        title="设置变量",
        description="写 ctx.variables[name] = in_value。支持 int/float/bool/坐标/区域/字符串。",
        color="#2D333B",
        accent="#F778BA",
        inputs=[
            _exec_in(),
            _data_in("in_value", "any", label="值"),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="name", editor=EDITOR_STRING, default="", label="变量名", required=True),
        ],
    ),
    "GetVariable": NodeMeta(
        type="GetVariable",
        category=CATEGORY_VARIABLE,
        title="读取变量",
        description="读取 ctx.variables[name]，若不存在输出默认值。",
        color="#2D333B",
        accent="#DB61A2",
        outputs=[_data_out("out_value", "any", label="值")],
        params=[
            ParamDef(name="name", editor=EDITOR_STRING, default="", label="变量名", required=True),
            ParamDef(name="default", editor=EDITOR_STRING, default="", label="默认值"),
        ],
    ),
    # ---------------- 便签（非执行） ----------------
    "Note": NodeMeta(
        type="Note",
        category=CATEGORY_META,
        title="便签",
        description="纯注释节点，不参与执行。用于分组说明。",
        color="#3A3F00",
        accent="#D4A72C",
        inputs=[],
        outputs=[],
        params=[
            ParamDef(name="text", editor=EDITOR_STRING, default="", label="内容"),
        ],
    ),
}


NODE_TYPES_ORDERED: List[str] = []
for cat in CATEGORIES_ORDER:
    for t, meta in NODE_LIBRARY.items():
        if meta.category == cat:
            NODE_TYPES_ORDERED.append(t)


# ============================================================
# 工厂：按 type 实例化一个 Node（自动装配端口 + 默认参数）
# ============================================================
import uuid as _uuid


def create_node(node_type: str, pos=(0, 0), node_id: Optional[str] = None) -> Node:
    meta = NODE_LIBRARY.get(node_type)
    if meta is None:
        raise ValueError(f"未知节点类型: {node_type}")
    nid = node_id or _uuid.uuid4().hex[:12]
    return Node(
        id=nid,
        type=node_type,
        pos=pos,
        params=meta.default_params(),
        inputs=[Port(**p.__dict__) for p in meta.inputs],
        outputs=[Port(**p.__dict__) for p in meta.outputs],
    )


def get_meta(node_type: str) -> Optional[NodeMeta]:
    return NODE_LIBRARY.get(node_type)
