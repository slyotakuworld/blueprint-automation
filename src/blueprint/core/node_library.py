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
EDITOR_MULTILINE = "multiline"


ON_FAIL_OPTIONS = ["stop", "continue", "retry_then_skip"]
CLICK_MODE_OPTIONS = ["position", "region", "key"]


# ============================================================
# 节点分类（对齐《借鉴Qflow-main蓝图节点.txt》四大类 + 废弃分类）
# ============================================================
CATEGORY_APP       = "应用控制"     # 第一类：打开程序/绑定窗口/系统命令/网页
CATEGORY_LOGIC     = "逻辑组件"     # 第二类：开始/结束/循环/序列/变量/分流/剪贴板/提示
CATEGORY_EXECUTE   = "执行动作"     # 第三类：鼠标/键盘/延时
CATEGORY_VISION    = "视觉、感知"   # 第四类：找图/检索/静止/声音

CATEGORIES_ORDER = [
    CATEGORY_APP,
    CATEGORY_LOGIC,
    CATEGORY_EXECUTE,
    CATEGORY_VISION,
]

# 每个分类内部的节点顺序（严格按照txt小节的序号排列）
# 缺失的节点（如 End、条件分支、多图任一）后续补齐时按序插入
CATEGORY_NODE_ORDER: Dict[str, List[str]] = {
    CATEGORY_APP: [
        "OpenApp",       # 1、打开程序
        "BindWindow",    # 2、绑定窗口
        "WebBrowser",    # 3、网页（浏览器操作：开URL/刷新/回退/前进）
        "Command",       # 4、系统命令
        "HttpRequest",   # 5、HTTP请求（接口调用：GET/POST/PUT/DELETE/Header/Body/超时）
    ],
    CATEGORY_LOGIC: [
        "Start",         # 1、开始
        "End",           # 2、结束
        "Loop",          # 3、循环
        "Sequence",      # 4、序列
        "SetVariable",   # 5、变量
        "VarSwitch",     # 6、分流 → VarSwitch(多分支)：支持布尔 yes/no（等价于被废弃的Branch）+ 多值枚举 N选一
        "Clipboard",     # 7、剪贴板
        "Notify",        # 8、提示
    ],
    CATEGORY_EXECUTE: [
        "Mouse",         # 1、鼠标
        "PressKey",      # 2、键盘
        "Wait",          # 3、延时
    ],
    CATEGORY_VISION: [
        "FindImage",     # 1、找图 → 单图Find
        "FindAnyImage",  # 1、找图 → 多图任找（Qflow增强）
        "IfImageDetect", # 2、检索 → 多图AND
        "StillnessDetect",# 3、静止
        "SoundDetect",   # 4、声音
    ],
}

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
    qflow_name: str = ""
    has_dynamic_ports: bool = False
    deprecated: bool = False
    hidden_in_palette: bool = False

    def default_params(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params if p.default is not None}


# ============================================================
# 类型名反向映射：Qflow小写短名 -> BA驼峰名（序列化 from_dict 时用）
# 动态构建：NODE_LIBRARY 初始化完后，通过每个 NodeMeta.qflow_name 自动生成 QFLOW2BA
# ============================================================
def _build_qflow2ba_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for ba_name, meta in NODE_LIBRARY.items():
        qn = getattr(meta, "qflow_name", "") or ""
        if qn:
            mapping[qn] = ba_name
    return mapping


def get_meta(node_type: str) -> Optional[NodeMeta]:
    return NODE_LIBRARY.get(node_type)


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
        category=CATEGORY_LOGIC,
        title="开始",
        description="蓝图唯一入口，每张图必须且仅能有 1 个。",
        color="#238636",
        accent="#3FB950",
        outputs=[_exec_out()],
        qflow_name="start",
    ),
    "End": NodeMeta(
        type="End",
        category=CATEGORY_LOGIC,
        title="结束",
        description="【对齐Qflow】蓝图执行终点（Start配对），到达此处视为该分支结束，不再向下流转。",
        color="#0b3d91",
        accent="#1565c0",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[],
        params=[],
        qflow_name="end",
    ),
    "Loop": NodeMeta(
        type="Loop",
        category=CATEGORY_LOGIC,
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
        qflow_name="loop",
    ),
    "Wait": NodeMeta(
        type="Wait",
        category=CATEGORY_EXECUTE,
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
        qflow_name="wait",
    ),
    "VarSwitch": NodeMeta(
        type="VarSwitch",
        category=CATEGORY_LOGIC,
        title="变量分流",
        description="二模式：简单模式（var_name非空，输出yes/no）；多分支模式（cases数组，输出case.id...+else）。均支持DATA连线 in_value 或从变量引擎读取。",
        color="#57606A",
        accent="#F78166",
        inputs=[
            _exec_in(),
            _data_in("in_value", "any", label="比较值（可选）"),
        ],
        outputs=[
            # 默认 2 输出（简单模式yes/no），实际动态由 params 决定，序列化时 outputs 会写全分支
            _exec_out("yes", label="是"),
            _exec_out("no", label="否"),
        ],
        params=[
            ParamDef(name="var_name", editor=EDITOR_STRING, default="", label="简单模式-变量名（留空=多分支模式）"),
            ParamDef(name="var_expr", editor=EDITOR_STRING, default="", label="简单模式-表达式（可选，写{var}，留空则变量真值判断）"),
            ParamDef(
                name="cases",
                editor=EDITOR_MULTILINE,
                default="",
                label="多分支模式 cases（每行：case_id = 值 或 直接写JSON数组 [{id,value}]）",
                help='示例：\ncase_a = 100\ncase_b = hello\n或 JSON: [{"id":"case_a","value":"100"}, {"id":"case_b","value":"hello"}]'
            ),
        ],
        qflow_name="var_switch",
        has_dynamic_ports=True,
    ),
    "Sequence": NodeMeta(
        type="Sequence",
        category=CATEGORY_LOGIC,
        title="顺序分支(短路OR)",
        description="短路OR分支：按顺序尝试 #1→#2→…→#N 路径，任一分支走到成功语义出口(yes/success/out/found)则立即走 out_done 完成；若分支走到失败语义出口(no/fail/timeout/not_found)则自动尝试下一条；全部失败走 else。",
        color="#57606A",
        accent="#58A6FF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("1", label="#1"),
            _exec_out("2", label="#2"),
            _exec_out("3", label="#3"),
            _exec_out("else", label="全失败"),
            _exec_out("out_done", label="完成"),
        ],
        params=[
            ParamDef(name="num_paths", editor=EDITOR_INT, default=3, label="路径数N", min=1, max=50, step=1, required=True),
            ParamDef(name="interval_sec", editor=EDITOR_FLOAT, default=0.0, label="每条路径间间隔(s)", min=0.0, max=3600.0, step=0.05),
        ],
        qflow_name="seq",
        has_dynamic_ports=True,
    ),
    # ---------------- 场景/窗口绑定（Qflow对齐：拆分后OpenApp + BindWindow） ----------------
    "OpenApp": NodeMeta(
        type="OpenApp",
        category=CATEGORY_APP,
        title="启动程序",
        description="启动外部程序：path + args（支持空格分隔或shlex）；working_dir为工作目录，wait_sec为启动后等待秒数。成功走out端口，失败走fail端口。",
        color="#8957E5",
        accent="#BC8CFF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out", label="成功"),
            _exec_out("fail", label="失败"),
        ],
        params=[
            ParamDef(name="path", editor=EDITOR_PATH, default="", label="程序路径(EXE)", required=True),
            ParamDef(name="args", editor=EDITOR_STRING, default="", label="启动参数(空格分隔)"),
            ParamDef(name="working_dir", editor=EDITOR_PATH, default="", label="工作目录(可选)", help="不填默认用EXE所在目录"),
            ParamDef(name="wait_sec_after_launch", editor=EDITOR_FLOAT, default=2.0, label="启动后等待(s)", min=0.0, max=120.0, step=0.1),
        ],
        qflow_name="open_app",
    ),
    "BindWindow": NodeMeta(
        type="BindWindow",
        category=CATEGORY_APP,
        title="绑定窗口",
        description="绑定目标窗口HWND到Screen/Matcher/InputActions：三维搜索(title子串/exe文件名/class_name)，对应3个开关；全开全不开时退化兜底强制开启title；分辨率参考用于坐标缩放。成功走success，失败走fail。",
        color="#8957E5",
        accent="#A371F7",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("success", label="绑定成功"),
            _exec_out("fail", label="绑定失败"),
        ],
        params=[
            ParamDef(name="title", editor=EDITOR_STRING, default="", label="窗口标题(子串)"),
            ParamDef(name="use_title", editor=EDITOR_BOOL, default=True, label="启用标题搜索"),
            ParamDef(name="exe_name", editor=EDITOR_STRING, default="", label="程序名(ex: notepad.exe)"),
            ParamDef(name="use_exe_name", editor=EDITOR_BOOL, default=True, label="启用程序名搜索"),
            ParamDef(name="class_name", editor=EDITOR_STRING, default="", label="窗口类名"),
            ParamDef(name="use_class_name", editor=EDITOR_BOOL, default=False, label="启用类名搜索"),
            ParamDef(name="resolution_w", editor=EDITOR_INT, default=1920, label="参考宽", min=800, max=7680, step=1),
            ParamDef(name="resolution_h", editor=EDITOR_INT, default=1080, label="参考高", min=600, max=4320, step=1),
        ],
        qflow_name="bind_win",
    ),
    # ---------------- 动作 / 识别 ----------------
    "FindImage": NodeMeta(
        type="FindImage",
        category=CATEGORY_VISION,
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
        qflow_name="image",
    ),
    "FindAnyImage": NodeMeta(
        type="FindAnyImage",
        category=CATEGORY_VISION,
        title="多图任找(分支)",
        description="依次尝试多张模板，命中的那张走对应的 out_found_N 分支；全部未中走 out_not_found。同时输出命中序号/位置/是否命中的数据线。",
        color="#0969DA",
        accent="#79C0FF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_not_found", label="都没找到"),
            _data_out("out_found_index", "int", label="命中序号"),
            _data_out("out_pos", "tuple2", label="命中位置"),
            _data_out("out_found", "bool", label="是否命中"),
        ],
        params=[
            ParamDef(name="templates", editor=EDITOR_STRING, default="", label="模板列表(逗号分隔)", required=True, help="如: btn_a.png,btn_b.png,btn_c.png"),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.75, label="阈值", min=0.3, max=1.0, step=0.01),
            ParamDef(name="fast_mode", editor=EDITOR_BOOL, default=True, label="快速模式"),
            ParamDef(name="timeout_ms", editor=EDITOR_FLOAT, default=0.0, label="总超时(ms)", min=0.0, max=3600000.0, step=100.0, help="0=只查一次不等待"),
            ParamDef(name="interval_ms", editor=EDITOR_FLOAT, default=300.0, label="轮询间隔(ms)", min=50.0, max=60000.0, step=10.0),
        ],
        qflow_name="find_any_image",
        has_dynamic_ports=True,
    ),
    "PressKey": NodeMeta(
        type="PressKey",
        category=CATEGORY_EXECUTE,
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
        qflow_name="keyboard",
    ),
    "Mouse": NodeMeta(
        type="Mouse",
        category=CATEGORY_EXECUTE,
        title="鼠标操作",
        description="【对齐Qflow】统一鼠标操作节点（点击/按住/释放/移动/拖拽/滚动），用 action 选择类型。",
        color="#ef6c00",
        accent="#ff9800",
        inputs=[
            _exec_in(),
            _data_in("in_pos", "tuple2", label="坐标(来自FindImage)"),
        ],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
        ],
        params=[
            ParamDef(name="action", editor=EDITOR_ENUM, default="click",
                     options=["click", "press", "release", "move", "drag", "scroll"],
                     label="【动作类型】",
                     help='Inspector根据本值动态显示对应分组参数'),
            ParamDef(name="bind_mode", editor=EDITOR_ENUM, default="current_win",
                     options=["current_win", "screen_abs", "foreground_win"],
                     label="坐标参考系",
                     help='current_win=绑定窗口(默认)；screen_abs=屏幕绝对；foreground_win=前景窗口'),

            ParamDef(name="pos_x", editor=EDITOR_INT, default=0, label="目标点 X"),
            ParamDef(name="pos_y", editor=EDITOR_INT, default=0, label="目标点 Y"),
            ParamDef(name="click_rel", editor=EDITOR_STRING, default="[0.5, 0.5]",
                     label="相对点击位置[rx,ry]"),
            ParamDef(name="offset_x", editor=EDITOR_INT, default=0, min=-500, max=500, label="X 偏移像素"),
            ParamDef(name="offset_y", editor=EDITOR_INT, default=0, min=-500, max=500, label="Y 偏移像素"),

            ParamDef(name="from_x", editor=EDITOR_INT, default=0, label="起点 X"),
            ParamDef(name="from_y", editor=EDITOR_INT, default=0, label="起点 Y"),
            ParamDef(name="to_x", editor=EDITOR_INT, default=0, label="终点 X"),
            ParamDef(name="to_y", editor=EDITOR_INT, default=0, label="终点 Y"),
            ParamDef(name="drag_duration_ms", editor=EDITOR_INT, default=300, min=0, max=10000,
                     label="拖拽时长(ms)", help='0=瞬间；越大越平滑'),

            ParamDef(name="button", editor=EDITOR_ENUM, default="left",
                     options=["left", "middle", "right", "x1", "x2"],
                     label="鼠标按键"),
            ParamDef(name="click_count", editor=EDITOR_INT, default=1, min=1, max=3,
                     label="点击次数", help='1=单击 2=双击 3=三击'),
            ParamDef(name="hold_ms_min", editor=EDITOR_INT, default=30, min=1, max=5000,
                     label="按下保持最小(ms)"),
            ParamDef(name="hold_ms_max", editor=EDITOR_INT, default=80, min=1, max=5000,
                     label="按下保持最大(ms)"),
            ParamDef(name="move_speed_ms", editor=EDITOR_INT, default=0, min=0, max=20000,
                     label="移动时长(ms)", help='0=瞬移'),

            ParamDef(name="scroll_dy", editor=EDITOR_INT, default=3, min=-100, max=100,
                     label="垂直滚动(格)", help='正=上 负=下'),
            ParamDef(name="scroll_dx", editor=EDITOR_INT, default=0, min=-100, max=100,
                     label="水平滚动(格)", help='正=右 负=左'),
            ParamDef(name="scroll_steps", editor=EDITOR_INT, default=1, min=1, max=100,
                     label="重复次数"),
        ],
        qflow_name="mouse",
    ),
    # ---------------- 变量 ----------------
    "SetVariable": NodeMeta(
        type="SetVariable",
        category=CATEGORY_LOGIC,
        title="设置变量",
        description="批量设置变量：每行 name=value。value 支持 {var}/${var}/{= expr} 插值；兼容旧蓝图的 name + in_value 单变量模式。",
        color="#2D333B",
        accent="#F778BA",
        inputs=[
            _exec_in(),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="batch_text", editor=EDITOR_MULTILINE, default="", label="批量设置（每行 name=value）"),
            ParamDef(name="name", editor=EDITOR_STRING, default="", label="变量名（兼容旧单变量模式）"),
        ],
        qflow_name="set_var",
    ),
    # ---------------- 通知 ----------------
    "Notify": NodeMeta(
        type="Notify",
        category=CATEGORY_LOGIC,
        title="通知",
        description="日志/弹窗通知：title+message 支持 {var}/${var}/{= expr} 插值；{ALL_VARS} 仅在 message 最后一步单独替换（避免与其他变量占位提前冲突）。",
        color="#0B3A5C",
        accent="#58A6FF",
        inputs=[_exec_in()],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="title", editor=EDITOR_STRING, default="通知", label="标题"),
            ParamDef(name="message", editor=EDITOR_MULTILINE, default="", label="内容（{ALL_VARS} 展开所有变量）"),
            ParamDef(
                name="level",
                editor=EDITOR_ENUM,
                default="INFO",
                label="级别",
                options=["INFO", "WARN", "ERROR", "DEBUG"],
            ),
            ParamDef(name="show_popup", editor=EDITOR_BOOL, default=False, label="系统弹窗"),
        ],
        qflow_name="notify",
    ),
    # ---------------- 系统交互 ----------------
    "Clipboard": NodeMeta(
        type="Clipboard",
        category=CATEGORY_LOGIC,
        title="剪贴板",
        description="读取系统剪贴板文本到变量；或把变量值写入剪贴板；或清空剪贴板。",
        color="#4527a0",
        accent="#7e57c2",
        inputs=[_exec_in(), _data_in("in_text", "str", label="写入文本")],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
            _data_out("out_text", "str", label="剪贴板文本"),
        ],
        params=[
            ParamDef(name="mode", editor=EDITOR_ENUM, default="read",
                     options=["read", "write", "clear"],
                     label="操作模式",
                     help='read=读剪贴板→out_text；write=写剪贴板→in_text或下面value；clear=清空剪贴板'),
            ParamDef(name="value", editor=EDITOR_MULTILINE, default="",
                     label="写入内容（支持{var}插值）",
                     help='仅mode=write生效；若in_text有线则用连线值优先，空则用本参数值'),
            ParamDef(name="output_var_name", editor=EDITOR_STRING, default="clip_text",
                     label="读到的内容存到变量名（仅read）",
                     help='mode=read时，读到的文本同时塞进ctx.variables，后续节点{clip_text}直接用'),
        ],
        qflow_name="clipboard",
    ),
    "Command": NodeMeta(
        type="Command",
        category=CATEGORY_APP,
        title="执行命令",
        description="执行Windows shell命令或外部程序，支持 {变量} 插值。支持 stdout/stderr/exit_code 3数据线输出。",
        color="#bf360c",
        accent="#f4511e",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败/超时"),
            _data_out("stdout", "str", label="标准输出"),
            _data_out("stderr", "str", label="标准错误"),
            _data_out("exit_code", "int", label="退出码"),
        ],
        params=[
            ParamDef(name="command", editor=EDITOR_MULTILINE, default="echo hello {name}",
                     label="命令（支持{变量}）", help='例：dir C:\\；python script.py {arg1}；start notepad'),
            ParamDef(name="working_dir", editor=EDITOR_STRING, default="",
                     label="工作目录（可空）",
                     help='空=当前项目目录；支持特殊占位符 {blueprint_dir} = 当前蓝图所在文件夹'),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=10000, min=0, max=3600000,
                     label="超时(ms)", help='0=永不超时（慎用）'),
            ParamDef(name="capture_streams", editor=EDITOR_BOOL, default=True,
                     label="捕获stdout/stderr",
                     help='关掉后不会等待程序结束立刻out_done；适合打开notepad.exe这类GUI程序'),
            ParamDef(name="shell_mode", editor=EDITOR_ENUM, default="cmd_c",
                     options=["cmd_c", "powershell", "no_shell"],
                     label="shell模式"),
            ParamDef(name="fail_on_nonzero", editor=EDITOR_BOOL, default=False,
                     label="非零退出码走err",
                     help='不勾（默认）：不论exit_code是几都走out_done，用户自己用if判断exit_code；勾了：exit_code!=0就走err分支'),
        ],
        qflow_name="cmd",
    ),
    "WebBrowser": NodeMeta(
        type="WebBrowser",
        category=CATEGORY_APP,
        title="网页",
        description="【应用控制#4】浏览器操作：用系统默认浏览器（或选Chrome/Edge/Firefox）打开URL/刷新/回退/前进。仅负责用户界面操作，不返回响应内容（需要接口数据请用 HTTP 请求节点）。",
        color="#01579b",
        accent="#29b6f6",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败/超时"),
        ],
        params=[
            ParamDef(name="action", editor=EDITOR_ENUM, default="open",
                     options=["open", "refresh", "back", "forward"],
                     label="动作",
                     help='open=新打开URL；refresh/back/forward=浏览器刷新/回退/前进（若系统无法控制当前浏览器，则降级为重新open该URL）'),
            ParamDef(name="url", editor=EDITOR_STRING, default="https://www.baidu.com/s?wd={keyword}",
                     label="URL（支持{var}）",
                     help='open 时必填；refresh/back/forward 可为空（此时用上次打开过的URL兜底重开）'),
            ParamDef(name="browser", editor=EDITOR_ENUM, default="default",
                     options=["default", "chrome", "edge", "firefox"],
                     label="使用浏览器",
                     help='default=操作系统默认浏览器；chrome/edge/firefox=指定对应可执行程序（找不到则自动fallback到default）'),
        ],
        qflow_name="web_browser",
    ),
    "HttpRequest": NodeMeta(
        type="HttpRequest",
        category=CATEGORY_APP,
        title="HTTP请求",
        description="【应用控制#5】网络接口调用：发送 GET/POST/PUT/DELETE/PATCH 请求到服务器，返回响应文本、JSON和状态码。URL/请求头/请求体均支持 {var} 插值；响应 JSON 自动拆成 web_ 前缀变量（可选）。",
        color="#1a237e",
        accent="#5c6bc0",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败/超时"),
            _data_out("response_text", "str", label="响应原文"),
            _data_out("response_json", "dict", label="响应JSON(字典)"),
            _data_out("status_code", "int", label="HTTP状态码"),
        ],
        params=[
            ParamDef(name="method", editor=EDITOR_ENUM, default="GET",
                     options=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                     label="请求方法"),
            ParamDef(name="url", editor=EDITOR_STRING, default="https://example.com/api?name={name}",
                     label="URL（支持{var}）", required=True),
            ParamDef(name="headers_json", editor=EDITOR_MULTILINE,
                     default='{\n  "Authorization": "Bearer {token}",\n  "User-Agent": "BA/1.0"\n}',
                     label="请求头(JSON，支持{var})",
                     help='空=不自定义头；key=header名，value=header值（字符串）'),
            ParamDef(name="body_text", editor=EDITOR_MULTILINE,
                     default='{"key": "{var}", "num": 123}',
                     label="请求体原文（支持{var}）",
                     help='GET/HEAD 自动忽略；JSON/Form/XML 都可填，原样发送；空=不发请求体'),
            ParamDef(name="content_type", editor=EDITOR_STRING, default="",
                     label="Content-Type（空则自动）",
                     help='空=有请求体且未自定义Content-Type时自动填 application/json; charset=utf-8；填入任意字符串会覆盖为该值'),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=10000, min=500, max=300000,
                     label="超时(ms)"),
            ParamDef(name="save_json_to_vars", editor=EDITOR_BOOL, default=True,
                     label="JSON自动拆成 web_ 变量",
                     help='勾了后响应JSON若是字典，每个 key 自动塞到 ctx.variables["web_"+key]，后续节点直接 {web_user_id} 用'),
        ],
        qflow_name="http_request",
    ),
    # ---------------- 检测节点 ----------------
    "IfImageDetect": NodeMeta(
        type="IfImageDetect",
        category=CATEGORY_VISION,
        title="检测（多图AND）",
        description="检测一组参考图是否全部同时出现在当前屏幕/窗口中。全部存在走yes，缺任何一张走no。",
        color="#ef6c00",
        accent="#ff9800",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="是"),
            _exec_out("no", label="否"),
        ],
        params=[
            ParamDef(
                name="images_json",
                editor=EDITOR_STRING,
                default="[]",
                label="参考图列表(JSON数组)",
                help='[{"b64":"...","w":100,"h":80},...] Inspector中提供可视化增删UI',
            ),
            ParamDef(
                name="threshold",
                editor=EDITOR_FLOAT,
                default=0.9,
                label="相似度阈值",
                min=0.3,
                max=1.0,
                step=0.01,
            ),
            ParamDef(
                name="match_strategy",
                editor=EDITOR_ENUM,
                default="hybrid",
                options=["hybrid", "template", "feature"],
                label="匹配算法",
            ),
        ],
        qflow_name="if_img",
    ),
    "StillnessDetect": NodeMeta(
        type="StillnessDetect",
        category=CATEGORY_VISION,
        title="静止检测",
        description="截取一个屏幕矩形ROI，等待该区域内画面静止（帧差<阈值）后走yes，超时走no。",
        color="#d84315",
        accent="#ff7043",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="静止"),
            _exec_out("no", label="超时"),
        ],
        params=[
            ParamDef(
                name="roi",
                editor=EDITOR_STRING,
                default="[0,0,0,0]",
                label="ROI矩形 [x,y,w,h]（点右侧按钮框选）",
            ),
            ParamDef(
                name="roi_preview_b64",
                editor=EDITOR_STRING,
                default="",
                label="ROI缩略图（只读）",
                help="Inspector里点框选时写入b64",
            ),
            ParamDef(
                name="diff_threshold",
                editor=EDITOR_FLOAT,
                default=0.01,
                min=0.0,
                max=0.5,
                step=0.001,
                label="帧差阈值（越小越严格）",
                help="两帧ROI像素差低于该值视为静止",
            ),
            ParamDef(
                name="stable_frames",
                editor=EDITOR_INT,
                default=3,
                min=1,
                max=60,
                label="连续多少帧都满足才算静止",
            ),
            ParamDef(
                name="sample_interval_ms",
                editor=EDITOR_INT,
                default=200,
                min=30,
                max=5000,
                label="采样间隔(ms)",
            ),
            ParamDef(
                name="timeout_ms",
                editor=EDITOR_INT,
                default=5000,
                min=0,
                max=300000,
                label="超时(ms)",
            ),
        ],
        qflow_name="if_static",
    ),
    "SoundDetect": NodeMeta(
        type="SoundDetect",
        category=CATEGORY_VISION,
        title="声音检测",
        description="监听系统声音（默认扬声器环回），采集指定毫秒时长判断振幅/频谱是否超阈值。是则走yes，超时走no。",
        color="#00838f",
        accent="#00acc1",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="有声"),
            _exec_out("no", label="无声/超时"),
        ],
        params=[
            ParamDef(name="device", editor=EDITOR_STRING, default="default_loopback",
                     label="音频设备ID",
                     help='default_loopback=当前默认扬声器（录系统自己播放的声音，对齐Qflow默认）；可点"🎧 枚举设备"按钮选择，或直接填麦克风设备名/ID'),
            ParamDef(name="sample_ms", editor=EDITOR_INT, default=500, min=50, max=10000,
                     label="单次采样时长(ms)", help="越长越准，但单次检测越慢"),
            ParamDef(name="threshold_db", editor=EDITOR_FLOAT, default=-30.0, min=-90.0, max=0.0, step=0.5,
                     label="振幅阈值(dBFS)", help="默认 -30dB≈人耳清晰可闻；调大(如-10)必须很响才算；调小(如-60)轻微响声也触发"),
            ParamDef(name="judge_mode", editor=EDITOR_ENUM, default="amplitude_max",
                     options=["amplitude_max", "amplitude_mean", "spectral_band"],
                     label="判断模式",
                     help="amplitude_max=采样期间最高振幅超阈值即算(最常用)；amplitude_mean=平均振幅超阈值(抗瞬时噪声)；spectral_band=指定频段能量超阈值(只检测某频率的提示音)"),
            ParamDef(name="spectral_band_low_hz", editor=EDITOR_INT, default=500, min=20, max=20000,
                     label="频段下限(Hz)"),
            ParamDef(name="spectral_band_high_hz", editor=EDITOR_INT, default=2000, min=20, max=20000,
                     label="频段上限(Hz)"),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=5000, min=0, max=300000,
                     label="超时(ms)", help="0=永不超时(慎用)；默认5秒内没检测到超阈值就走no"),
            ParamDef(name="debug_visual", editor=EDITOR_BOOL, default=False,
                     label="调试可视化(Log面板ASCII波形)",
                     help="仅debug时勾；会在日志用ASCII字符画绘制采样波形+阈值线"),
        ],
        qflow_name="if_sound",
    ),
}


NODE_TYPES_ORDERED: List[str] = []
_seen_nodes: set = set()
for cat in CATEGORIES_ORDER:
    order_list = CATEGORY_NODE_ORDER.get(cat, [])
    for tname in order_list:
        meta = NODE_LIBRARY.get(tname)
        if meta is None:
            continue
        if meta.category != cat:
            continue
        if tname in _seen_nodes:
            continue
        NODE_TYPES_ORDERED.append(tname)
        _seen_nodes.add(tname)
    # 兜底：该分类下其他未在 ORDER 表中列出的节点，按注册顺序追加到末尾
    for t, meta in NODE_LIBRARY.items():
        if meta.category == cat and t not in _seen_nodes:
            NODE_TYPES_ORDERED.append(t)
            _seen_nodes.add(t)


# ============================================================
# QFLOW2BA: Qflow小写短名 -> BA驼峰名（序列化 from_dict 自动识别）
# 在 NODE_LIBRARY 初始化后构建，避免循环
# ============================================================
QFLOW2BA: Dict[str, str] = {}
for _ba_name, _meta in NODE_LIBRARY.items():
    _qn = getattr(_meta, "qflow_name", "") or ""
    if _qn:
        QFLOW2BA.setdefault(_qn, _ba_name)


def _resolve_output_ports_default(node_type: str, params: Optional[Dict[str, Any]] = None) -> List[Port]:
    """返回给定节点类型的默认输出端口列表（VarSwitch/Sequence后续动态扩展，当前先返回 NodeMeta.outputs 静态定义）。

    供 Node.from_dict 反序列化时自动补默认端口，避免丢失连线。
    """
    meta = NODE_LIBRARY.get(node_type)
    if meta is None:
        return []
    params = params or {}
    # -------- 动态端口节点的默认输出（后续P0-1-3/P1-4实现完会精细化）--------
    if node_type == "VarSwitch":
        var_name = str(params.get("var_name", "") or "").strip()
        if var_name:
            names = ["yes", "no"]
        else:
            cases = params.get("cases", []) or []
            case_ids = [str(c.get("id", "")).strip() for c in cases if str(c.get("id", "")).strip()]
            names = case_ids + ["else"]
        return [Port(name=n, type=PortType.EXEC, direction=PortDirection.OUT, label=n) for n in names]
    if node_type == "Sequence":
        n = max(1, int(params.get("num_paths", 3) or 3))
        names = [str(i) for i in range(1, n + 1)] + ["else", "out_done"]
        return [Port(name=x, type=PortType.EXEC, direction=PortDirection.OUT, label=x) for x in names]
    # FindAnyImage 动态端口：out_found_0/1/... + out_not_found + 数据线输出（根据 templates 数量推算）
    if node_type == "FindAnyImage":
        raw = str(params.get("templates", "") or "")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        count = max(1, len(parts))
        outs: List[Port] = []
        for i in range(count):
            outs.append(_exec_out(f"out_found_{i}", label=f"#{i+1}"))
        outs.append(_exec_out("out_not_found", label="都没找到"))
        outs.append(_data_out("out_found_index", "int", label="命中序号"))
        outs.append(_data_out("out_pos", "tuple2", label="命中位置"))
        outs.append(_data_out("out_found", "bool", label="是否命中"))
        return outs
    # -------- 默认：返回 NodeMeta 静态 outputs --------
    return list(meta.outputs)


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
