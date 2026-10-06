from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
# 节点分类（四大类节点分类）
# ============================================================
CATEGORY_APP       = "应用控制"     # 第一类：打开程序/系统命令/网页/网络请求
CATEGORY_LOGIC     = "逻辑"         # 第二类：开始/结束/组件/循环/序列/并行/变量/条件判断
CATEGORY_EXECUTE   = "动作"         # 第三类：鼠标/键盘/延时/提示/剪贴板
CATEGORY_VISION    = "感知"         # 第四类：找图/检索/截图/静止/文字

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
        "WebBrowser",    # 2、打开网页（浏览器打开URL）
        "Command",       # 3、系统命令
        "HttpRequest",   # 4、HTTP请求（接口调用：GET/POST/PUT/DELETE/Header/Body/超时）
    ],
    CATEGORY_LOGIC: [
        "Start",         # 1、开始
        "End",           # 2、结束
        "Component",     # 3、组件（蓝图复用/预制体）
        "Loop",          # 4、循环
        "Sequence",      # 5、顺序执行（纯串行队列，子画布容器）
        "Parallel",      # 6、并行执行（多分支并发，子画布容器）
        "SetVariable",   # 7、变量
        "VarSwitch",     # 8、条件判断 → AND 多条件判断，每行 变量名 运算符 值，全部同时满足 → yes / 任一不满足 → no
    ],
    CATEGORY_EXECUTE: [
        "Mouse",         # 1、鼠标
        "PressKey",      # 2、键盘
        "Wait",          # 3、延时
        "Notify",        # 4、提示
        "Clipboard",     # 5、剪贴板
    ],
    CATEGORY_VISION: [
        "FindImage",     # 1、找图 → 单图Find
        "IfImageDetect", # 2、检索 → 多图AND
        "Screenshot",    # 3、截图 → 截目标窗口 ROI 输出图像
        "StillnessDetect",# 4、静止
        "OcrRecognize",  # 5、文字/数字识别 → 内置 OCR 引擎
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
    # 中文下拉框显示：{option_value: 显示文本}；不填则直接显示 options 原值
    option_labels: Optional[Dict[Any, str]] = None
    # tuple2 点属性是否附带 📍 坐标拾取按钮（仅 Mouse 节点用）
    pick_coord: bool = False
    # 输入框 placeholder 提示文本（EDITOR_STRING / EDITOR_MULTILINE 用）
    placeholder: str = ""


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
    type_short_name: str = ""
    has_dynamic_ports: bool = False
    hidden_in_palette: bool = False

    def default_params(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params if p.default is not None}


# ============================================================
# 类型名反向映射：小写短名 -> 驼峰名（序列化 from_dict 时用）
# 动态构建：NODE_LIBRARY 初始化完后，通过每个 NodeMeta.type_short_name 自动生成 SHORT_NAME_MAP 映射表
# ============================================================
def _build_short_name_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for ba_name, meta in NODE_LIBRARY.items():
        qn = getattr(meta, "type_short_name", "") or ""
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


def _target_locator_params(scope_label: str) -> List[ParamDef]:
    """「目标窗口定位三件套」（标题 / 程序名 / 类名）的公用构造。

    找图 / 鼠标 / 检测 / 静止 / 截图 / 文字 六个节点的这套参数**逐字相同**，只有
    help 里那句「仅「X=目标窗口」时生效」的 X 不一样（X 是本节点里 range_mode 的标签：
    找图范围 / 坐标基准 / 检测范围 / 截取范围 / 识别范围），所以由调用方把标签传进来。

    为什么必须共用：运行时这六个节点定位窗口走的是同一套
    `executor._resolve_target_rect`（title 子串 / exe 子串 / class 全等）。
    参数文案各写各的，改一处忘一处就会出现「文档说有、实际没有」的假文档。
    """
    return [
        ParamDef(name="target_title", editor=EDITOR_STRING, default="",
                 label="目标窗口·标题",
                 help=f"仅「{scope_label}=目标窗口」时生效。窗口标题子串匹配，支持 {{变量}} 插值。\n"
                      "三个条件（标题/程序名/类名）至少填一个。\n点上方「🎯 拾取目标窗口」按钮可自动填充。"),
        ParamDef(name="target_exe", editor=EDITOR_STRING, default="",
                 label="目标窗口·程序名",
                 help=f"仅「{scope_label}=目标窗口」时生效。程序名子串匹配（如 notepad.exe），支持 {{变量}} 插值。留空=不按程序名匹配。"),
        ParamDef(name="target_class", editor=EDITOR_STRING, default="",
                 label="目标窗口·类名",
                 help=f"仅「{scope_label}=目标窗口」时生效。窗口类名精确匹配，支持 {{变量}} 插值。留空=不按类名匹配。"),
    ]


# ============================================================
# 16 个 MVP 节点定义
# ============================================================
NODE_LIBRARY: Dict[str, NodeMeta] = {
    # ---------------- 控制流 ----------------
    "Start": NodeMeta(
        type="Start",
        category=CATEGORY_LOGIC,
        title="开始",
        description="咱是整张蓝图唯一的起点哟~每张蓝图只能放一个咱，从这里开始往下跑！\n\n作为「组件蓝图」的入口时，还能声明入参端口（param_0/param_1…），调用它的「组件」节点会按序把传入的值喂给这些端口。",
        color="#238636",
        accent="#3FB950",
        outputs=[_exec_out()],
        params=[
            ParamDef(name="num_params", editor=EDITOR_INT, default=0, min=0, max=20,
                     label="入参端口数",
                     help="本蓝图作为「组件」被调用时的入参端口数量（param_0/param_1…，数据输出端口）。\n"
                          "普通蓝图保持 0 即可；设为 N 后，调用方「组件」节点需按序传入 N 个值。"),
        ],
        type_short_name="start",
        has_dynamic_ports=True,
    ),
    "End": NodeMeta(
        type="End",
        category=CATEGORY_LOGIC,
        title="结束",
        description="和Start配对的终点~跑到咱这里就算这条分支收工啦，不会再继续往下走咯。\n\n作为「组件蓝图」的出口时，还能声明返回值端口（ret_0/ret_1…），把值交还给调用它的「组件」节点。",
        color="#0b3d91",
        accent="#1565c0",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[],
        params=[
            ParamDef(name="num_returns", editor=EDITOR_INT, default=0, min=0, max=20,
                     label="返回值端口数",
                     help="本蓝图作为「组件」被调用时的返回值端口数量（ret_0/ret_1…，数据输入端口）。\n"
                          "普通蓝图保持 0 即可；设为 N 后，连到这些端口的值会按序交还给调用方「组件」节点的输出端口。"),
        ],
        type_short_name="end",
        has_dynamic_ports=True,
    ),
    "Loop": NodeMeta(
        type="Loop",
        category=CATEGORY_LOGIC,
        title="循环",
        description="循环跑圈儿~要么到了设定的最大次数就退出，要么勾上「无限循环」就一直跑（只能暂停/停止），每轮之间还能等一小会儿再跑下一圈哟。",
        color="#8250DF",
        accent="#A371F7",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("loop_body", label="循环体"),
            _exec_out("out_exit", label="退出"),
        ],
        params=[
            ParamDef(name="loop_body", editor=EDITOR_MULTILINE, default="", label="循环体子图(只读)",
                     help="内部字段，保存循环体子画布内容。双击本节点进入子画布编辑，无需手填。"),
            ParamDef(name="infinite", editor=EDITOR_BOOL, default=False, label="无限循环",
                     help="勾选后忽略最大迭代次数，仅手动停止或条件退出才结束（慎用）"),
            ParamDef(name="max_iterations", editor=EDITOR_INT, default=5, label="最大迭代",
                     min=1, max=10000000, step=1, required=True,
                     help="硬约束：防止死循环。无限循环模式下此项不生效"),
            ParamDef(name="interval_sec", editor=EDITOR_FLOAT, default=1.0, label="每轮间隔(s)",
                     min=0.0, max=60.0, step=0.05,
                     help="每轮循环体执行完后的等待时间（秒）"),
        ],
        type_short_name="loop",
    ),
    "Continue": NodeMeta(
        type="Continue",
        category=CATEGORY_LOGIC,
        title="再次循环",
        description="只出现在循环体子画布内~跑到咱这里就立即结束本轮、开始下一轮循环，不会继续往下走后续节点。",
        color="#8250DF",
        accent="#A371F7",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[],
        params=[],
        type_short_name="continue",
        hidden_in_palette=True,
    ),
    "Break": NodeMeta(
        type="Break",
        category=CATEGORY_LOGIC,
        title="跳出循环",
        description="只出现在循环体子画布内~跑到咱这里就立即结束整个循环，跳到循环节点的「退出」出口。",
        color="#DA3633",
        accent="#FF7B72",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[],
        params=[],
        type_short_name="break",
        hidden_in_palette=True,
    ),
    "Branch": NodeMeta(
        type="Branch",
        category=CATEGORY_LOGIC,
        title="分支入口",
        description="只出现在并行体子画布内~每个咱代表一条并行分支的起点，从咱的 out_exec 往下连就是这条分支要跑的动作。运行时所有「分支入口」会同时用线程并发执行。",
        color="#0E6B5C",
        accent="#2DD4BF",
        inputs=[],
        outputs=[_exec_out("out_exec", label="▶")],
        params=[],
        type_short_name="branch",
        hidden_in_palette=True,
    ),
    "Component": NodeMeta(
        type="Component",
        category=CATEGORY_LOGIC,
        title="组件",
        description="把一个蓝图当作「预制体/组件」复用~选中工程里任意一张「组件蓝图」，跑到咱这里就跳到那张蓝图去跑一遍，跑完再回来继续。\n\n组件蓝图放在哪个目录都可以（资源树里右键蓝图 → 🧩 设为组件蓝图）。\n\n支持显式传参：左侧 in_0/in_1… 输入端口把值传给子蓝图的 Start 入参端口，子蓝图 End 的返回值会回到右侧 out_0/out_1… 输出端口。变量仍然全局共享。",
        color="#1F6FEB",
        accent="#58A6FF",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[_exec_out("out_done", label="完成")],
        params=[
            ParamDef(name="blueprint_path", editor=EDITOR_STRING, default="", label="组件蓝图",
                     placeholder="从下拉列表选择一张组件蓝图",
                     help="要复用的那张蓝图（工程里任何位置的 .bp.json，只要被标为组件蓝图）。从下拉列表选择即可，无需手填路径。"),
            ParamDef(name="num_inputs", editor=EDITOR_INT, default=0, min=0, max=20,
                     label="输入参数端口数",
                     help="传给子蓝图 Start 入参端口的参数个数（in_0/in_1… 数据输入端口）。\n"
                          "应与子蓝图 Start 的「入参端口数」一致，按序一一对应。"),
            ParamDef(name="num_outputs", editor=EDITOR_INT, default=0, min=0, max=20,
                     label="输出参数端口数",
                     help="从子蓝图 End 返回值端口接收的返回值个数（out_0/out_1… 数据输出端口）。\n"
                          "应与子蓝图 End 的「返回值端口数」一致，按序一一对应。"),
        ],
        type_short_name="component",
        has_dynamic_ports=True,
    ),
    "Wait": NodeMeta(
        type="Wait",
        category=CATEGORY_EXECUTE,
        title="等待",
        description="歇会儿再接着跑~要么固定睡几秒，要么在范围内随机小睡更像真人，上游传过来的秒数也能直接拿来用哟。",
        color="#2D333B",
        accent="#8B949E",
        inputs=[
            _exec_in(),
            _data_in("in_seconds", "float", label="秒数"),
        ],
        outputs=[_exec_out()],
        params=[
            # 模式选择（第一位）
            ParamDef(name="delay_type", editor=EDITOR_ENUM, default="fixed",
                     options=["fixed", "random"],
                     option_labels={"fixed": "固定延时", "random": "随机延时"},
                     label="延时模式",
                     help="固定延时(fixed)：总是等待 seconds 秒；\n随机延时(random)：在 min_seconds ~ max_seconds 之间随机等待，更拟人。"),
            # --- 固定延时参数 ---
            ParamDef(name="seconds", editor=EDITOR_FLOAT, default=1.0, label="秒数",
                     min=0.0, max=3600.0, step=0.1,
                     help="【固定延时】等待的秒数。"),
            # --- 随机延时参数 ---
            ParamDef(name="min_seconds", editor=EDITOR_FLOAT, default=1.0, label="最小(秒)",
                     min=0.0, max=3600.0, step=0.1,
                     help="【随机延时】随机范围下限。"),
            ParamDef(name="max_seconds", editor=EDITOR_FLOAT, default=3.0, label="最大(秒)",
                     min=0.0, max=3600.0, step=0.1,
                     help="【随机延时】随机范围上限。"),
        ],
        type_short_name="wait",
    ),
    "VarSwitch": NodeMeta(
        type="VarSwitch",
        category=CATEGORY_LOGIC,
        title="条件判断",
        description="一条条检查汝写的条件~全部都对上了就走「满足」，只要有一条不对就走「不满足」。",
        color="#57606A",
        accent="#F78166",
        inputs=[
            _exec_in(),
            _data_in("in_0", "any", label="#1 输入"),
        ],
        outputs=[
            _exec_out("yes", label="满足"),
            _exec_out("no", label="不满足"),
        ],
        params=[
            ParamDef(
                name="num_data_inputs",
                editor=EDITOR_INT,
                default=1,
                min=0,
                max=20,
                label="数据输入端口数",
                help="动态增减数据输入端口（+/- 按钮调整），每个端口对应一个输入值。\n临时变量名（所有节点统一写法，就这一种）：\n- 完整数组：__INPUTS__（list类型，可写 {__INPUTS__[0]} 索引）\n- 按索引单值：__INPUTS__0（第1个端口）、__INPUTS__1（第2个）、...、__INPUTS__19\n简写语法示例（行首直接写运算符，左值自动=__INPUTS__0）：\n  == 1             → __INPUTS__0 == 1\n  == {id}          → __INPUTS__0 == 变量 id\n  >= {min_score}   → __INPUTS__0 >= 及格线"
            ),
            ParamDef(
                name="cases",
                editor=EDITOR_MULTILINE,
                default="",
                label="条件设置",
                placeholder="hp >= 30\nhp <= 100\nscore >= 60\ngender != \"female\"\nlevel > {min_level}\n# ↓ 简写写法（连了第 1 个数据端口时用）\n== 1\n== {id}\n>= {min_score}\n# ↓ 多端口：一行写一条，逐行分开写（不要在一行里用 AND）\n__INPUTS__0 >= {min_score}\n__INPUTS__1 != {exclude_status}",
                help='每行写一个条件，行与行之间自动 AND（全部满足才走 yes）。\n'
                     '⚠ 不要在一行里写 AND / OR：本节点不支持行内 AND，写了会被判成格式错误（因为那一行会被当成「值」）。想要 OR，请串两个条件判断节点。\n\n'
                     '两种写法，推荐简写：\n'
                     '① 完整写法：「变量名 运算符 值」例：score >= 60、level > {min_level}\n'
                     '   多输入端口引用就这一种：__INPUTS__0=第1个、__INPUTS__1=第2个、...（全大写，两侧双下划线）\n'
                     '② 【简写语法】（连了第1个输入端口时用）：行首直接写运算符，省掉左变量名\n'
                     '   左值自动 = __INPUTS__0（想主动写表达式时直接写这个名）\n'
                     '   例：\n'
                     '     == 1              → 意思：第1个输入 == 数字 1\n'
                     '     == {id}           → 意思：第1个输入 == 变量 id\n'
                     '     != {exclude}      → 意思：第1个输入 != 变量 exclude\n'
                     '     >= {min_score}    → 意思：第1个输入 >= 变量 min_score\n\n'
                     '左值只能是「裸变量名」，不能写成 {变量}；只有右边才支持插值。\n'
                     '运算符：== 等于、!= 不等于、> 大于、< 小于、>= 大于等于、<= 小于等于\n'
                     '值：字符串加引号 "abc" 或 \'abc\'；数字直接写；布尔写 true/false；支持 {变量} / ${变量} / {= expr} 插值\n'
                     '空行 和 // 或 # 开头的行 作为注释忽略\n'
                     '运行时如果条件写错（语法错误、变量名拼错、插值没生效），运行日志里会有 WARN 明确指出来。'
            ),
        ],
        type_short_name="var_switch",
        has_dynamic_ports=True,
    ),
    "Sequence": NodeMeta(
        type="Sequence",
        category=CATEGORY_LOGIC,
        title="顺序执行",
        description="纯串行队列~把要顺序执行的一串动作都塞进咱的子画布里，从上到下一条条跑，不做任何条件判断。双击咱进入子画布编辑，跑完就继续往下走。",
        color="#57606A",
        accent="#58A6FF",
        inputs=[_exec_in()],
        outputs=[_exec_out("out_done", label="完成")],
        params=[
            ParamDef(name="seq_body", editor=EDITOR_MULTILINE, default="", label="顺序体子图(只读)",
                     help="内部字段，保存顺序执行的子画布内容。双击本节点进入子画布编辑，无需手填。"),
        ],
        type_short_name="seq",
    ),
    "Parallel": NodeMeta(
        type="Parallel",
        category=CATEGORY_LOGIC,
        title="并行执行",
        description="并行跑~把多条分支都塞进咱的子画布里，每条分支用线程同时跑，各跑各的互不等待。全部跑完走「完成」，任意一条出错走「失败」（但不打断其它分支）。双击咱进入子画布编辑。",
        color="#0E6B5C",
        accent="#2DD4BF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
        ],
        params=[
            ParamDef(name="par_body", editor=EDITOR_MULTILINE, default="", label="并行体子图(只读)",
                     help="内部字段，保存并行执行的子画布内容。双击本节点进入子画布编辑，无需手填。"),
        ],
        type_short_name="parallel",
        # 分支数量由子画布里「分支入口(Branch)」节点数量决定，运行时只认 Branch，
        # 端口固定（完成/失败），无需动态端口机制
        has_dynamic_ports=False,
    ),
    # ---------------- 应用控制 ----------------
    "OpenApp": NodeMeta(
        type="OpenApp",
        category=CATEGORY_APP,
        title="启动程序",
        description="帮汝打开外部程序~告诉咱程序在哪、要传啥参数，启动成功就继续，开不起来就走失败分支。",
        color="#8957E5",
        accent="#BC8CFF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out", label="成功"),
            _exec_out("fail", label="失败"),
        ],
        params=[
            ParamDef(name="path", editor=EDITOR_PATH, default="", label="程序路径",
                     help="要启动的程序 exe 路径；支持 {变量} 插值。"),
            ParamDef(name="args", editor=EDITOR_STRING, default="", label="启动参数",
                     help="传给程序的命令行参数；支持 {变量} 插值。"),
            ParamDef(name="working_dir", editor=EDITOR_PATH, default="", label="工作目录",
                     help="程序启动时的工作目录（CWD）；留空=自动用程序所在目录。支持 {变量} 插值。"),
            ParamDef(name="wait_sec_after_launch", editor=EDITOR_FLOAT, default=2.0, label="等待时间(s)", min=0.0, max=120.0, step=0.1,
                     help="程序启动后先等这么多秒再继续跑下一节点（给程序留加载时间）。"),
        ],
        type_short_name="open_app",
    ),
    # ---------------- 动作 / 识别 ----------------
    "FindImage": NodeMeta(
        type="FindImage",
        category=CATEGORY_VISION,
        title="找图",
        description="给咱一张参考图就在屏幕上找~在超时时间里反复试，找不到还能滚动鼠标页面后继续找；找到了可以顺手点一下再从「成功找到」往下走，一直没找到就走「没有找到」，「执行」出口则是找没找到都能继续；要是范围选了目标窗口却没能定位到窗口，那就只走「失败」。",
        color="#0969DA",
        accent="#58A6FF",
        inputs=[
            _exec_in(),
        ],
        # 端口顺序 = 画布上的显示顺序。按「执行 → 找到 / 没找到 → 失败 → 位置」排，
        # 让「失败（窗口没定位到这类硬错）」紧挨在数据出口前面，与描述里的语义顺序一致。
        outputs=[
            _exec_out("out_exec", label="执行"),
            _exec_out("found", label="成功找到"),
            _exec_out("not_found", label="没有找到"),
            _exec_out("err", label="失败"),
            _data_out("out_pos", "tuple2", label="位置"),
        ],
        params=[
            # 范围模式
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="找图范围",
                     help="目标窗口(window)：只在下面「目标窗口」定位到的那个窗口范围内找图，输出坐标也是相对该窗口左上角的（配合鼠标节点「目标窗口」模式使用）；\n"
                          "全屏(fullscreen)：在整块屏幕上找图，输出坐标为绝对屏幕坐标（配合鼠标节点「全屏」模式使用）。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会退化成全屏去找。"),
            # 目标窗口定位（仅 range_mode=window 时生效）——定位三件套与其余五个节点共用
            *_target_locator_params("找图范围"),
            # 匹配参数（算法 + 阈值相邻，便于一起调）
            ParamDef(
                name="match_strategy",
                editor=EDITOR_ENUM,
                default="hybrid",
                options=["hybrid", "template", "gray"],
                option_labels={
                    "hybrid": "智能混合",
                    "template": "模板匹配",
                    "gray": "灰度匹配",
                },
                label="匹配算法",
                help='各算法优缺点：\n'
                     '● 智能混合(hybrid)【推荐】：彩色模板→灰度模板，两级级联兜底。先按颜色找，找不到再用灰度找（吃下颜色变化），大多数场景首选。\n'
                     '● 模板匹配(template)：彩色像素归一化相关。速度最快，精度最高；但对缩放、旋转、颜色变化敏感。适合尺寸固定、画面清晰的UI按钮/图标。\n'
                     '● 灰度匹配(gray)：灰度像素归一化相关。容忍颜色变化（如选中态/高亮态），适合颜色可能变化但形状不变的场景。\n'
                     '三种算法都自带多尺度搜索（约 0.45~1.8），可覆盖一定程度的缩放；但都不抗旋转与局部遮挡。',
            ),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.9, label="匹配阈值", min=0.3, max=1.0, step=0.01,
                     help="相似度阈值，越高越严格。模板匹配/灰度匹配/混合模式均为相关系数。"),
            ParamDef(name="timeout_seconds", editor=EDITOR_FLOAT, default=10.0, label="超时(秒)", min=0.5, max=600.0, step=0.5,
                     help="在超时时间内会根据自动滚动设置反复重试，直到找到或超时。"),
            ParamDef(name="template", editor=EDITOR_PATH, default="", label="模板路径",
                     help="要查找的图片文件路径（PNG/JPG/BMP等），也可点上面工具栏「📸 截取目标」或「📂 导入图片」直接选择，自动写入路径+预览。"),
            ParamDef(name="template_b64", editor=EDITOR_STRING, default="", label="模板预览图(内部字段)",
                     help="内部字段：由上方「📸 截取目标」/「📂 导入图片」写入的模板缩略图(b64)，供画布节点直接预览。\n面板不渲染这一行（属机器托管数据），无需手填。"),
            # 未找到时滚动重试
            ParamDef(name="scroll_dy", editor=EDITOR_INT, default=0, label="滚动量(0=不滚动)", min=-5000, max=5000, step=1,
                     help="鼠标滚轮滚动量，单位是「滚轮刻度数」（1 刻度 ≈ 滚轮拨一格）。0=不滚动；"
                          "负值=向下翻页（内容往后滚）；正值=向上翻页。\n"
                          "【推荐值 1~10】：3~5 通常就能翻动一屏，列表类界面用 3 左右最合适；\n"
                          "填过头（例如 -300）会一次滚到底，反而跳过目标，看着像「找图失效」。\n"
                          "非0时，找图失败后会把鼠标移到搜索区域中心滚动一次再重试，直到超时。\n"
                          "上限 ±5000 只是留给极端场景的余量，日常基本用不到。"),
            # 找到后执行
            ParamDef(
                name="after_action",
                editor=EDITOR_ENUM,
                default="none",
                options=["none", "click", "double_click", "right_click"],
                option_labels={
                    "none": "不执行（仅输出位置）",
                    "click": "左键单击（推荐）",
                    "double_click": "左键双击",
                    "right_click": "右键单击",
                },
                label="找到后执行",
                help="找到目标后要自动做的动作。建议用默认「左键单击」直接点击目标；不需要鼠标操作则选「不执行」仅把位置传给下游Mouse节点。",
            ),
            ParamDef(name="offset", editor=EDITOR_TUPLE2, default=(0, 0), label="点击偏移",
                     help="找到目标后，相对模板中心的像素偏移 (X, Y)。X 正值向右，负值向左；Y 正值向下，负值向上。\n例如找到的图片中心在窗口(100,200)，偏移设(10,-5)，则实际点击位置为(110,195)。\nout_pos 输出的是「模板中心+偏移」后的最终坐标。"),
            ParamDef(name="post_delay_ms", editor=EDITOR_INT, default=50, min=0, max=5000,
                     label="点击后延迟(ms)", help="找到并执行点击动作后、继续下一个节点之前的等待时间（固定值）。\n常用于点击后等菜单弹出/场景加载完，避免下一节点立刻跑时画面还没变。设 0 = 不等待。\n仅「找到后执行」非「不执行」时生效。"),
            # ===== 特征识别辅助 =====
            ParamDef(name="use_feature_assist", editor=EDITOR_ENUM, default="off",
                     options=["off", "verify", "fallback"],
                     option_labels={
                         "off": "关闭",
                         "verify": "验证模式",
                         "fallback": "兜底模式",
                     },
                     label="特征识别辅助",
                     help="特征辅助用预存的小特征图（点上方「✂ 从模板截取特征」生成）做交叉验证：\n"
                          "● 关闭：不使用特征辅助。\n"
                          "● 验证模式：模板匹配成功后，再逐个验证特征是否还在原位；通过数量不够就判失败（更严格，抗误匹配）。\n"
                          "● 兜底模式：模板匹配失败后，全屏搜特征并交叉验证位置，反推模板中心（更宽松，抗改版/缩放）。\n"
                          "【特征为空时】没有可用特征图时：开启验证模式且「最少命中特征数」>0 → 直接判失败并打 WARN"
                          "（不静默降级）；只有把「最少命中特征数」设为 0 才放行。\n"
                          "【算法】特征小图跟随主算法，层数与主匹配保持一致："
                          "主算法选「灰度匹配」→用灰度匹配；选「智能混合」→用「彩色→灰度」两级；"
                          "选「模板匹配」→用彩色匹配。\n"
                          "【换模板】换了模板图后，旧特征会失效，程序会自动清空特征数据并提示重新截取。"),
            ParamDef(name="features_json", editor=EDITOR_MULTILINE, default="[]",
                     label="特征列表(JSON)",
                     help="特征数据JSON数组，每项包含: b64(特征图base64), rel_x/rel_y(相对模板左上角), w/h(特征宽高)。\n通常通过面板上方「✂ 从模板截取特征」按钮自动生成，无需手填。"),
            ParamDef(name="feature_tolerance", editor=EDITOR_INT, default=15, min=1, max=200, step=1,
                     label="位置容差(像素)",
                     help="两种模式下含义不同，别混用：\n"
                          "● 兜底模式（模板没找到、靠特征反推位置）：这是「位置交叉验证」的允许误差。"
                          "以第一个命中特征为基准，推算其他特征应该出现的位置，实际位置与推算位置的欧几里得距离"
                          "超过此值就丢弃该特征；偏差大的特征被丢光、剩余命中数不够，就判本次失败。值越小越严格。\n"
                          "● 验证模式（模板已找到、在命中位置附近验证特征）：不参与「通过与否」的判定。"
                          "它只用来决定搜索窗口开多大：窗口宽 = 特征宽×缩放 + 容差×2（最小 30px）。"
                          "因此这里调大 = 搜索范围更宽松、更容易验证通过，但太宽松就失去了「验证」的收紧作用。\n"
                          "建议 10-20。"),
            ParamDef(name="feature_min_hits", editor=EDITOR_INT, default=2, min=0, max=20, step=1,
                     label="最少命中特征数",
                     help="判定口径（验证模式/兜底模式一致）：**命中并通过位置验证的特征数量 ≥ 此值**才算成功，"
                          "小于它一律算失败。\n"
                          "● 2（默认，推荐）：至少两个特征互相印证，能靠位置关系排除误匹配。\n"
                          "● 1：退化为单特征匹配，失去交叉验证，误匹配概率明显升高（不推荐）。\n"
                          "● 0：不要求任何特征命中。此时连「特征列表为空」也直接放行，等效于不做特征校验"
                          "（仅用于临时放宽，比如刚开验证模式还没截特征），运行时会打 WARN 提醒。\n"
                          "若此值大于实际可用特征数，本次运行必然失败，日志会明确提示。"),
            ParamDef(name="feature_capture_width", editor=EDITOR_INT, default=0, min=0, max=99999, step=1,
                     label="截图时窗口宽",
                     help="截取特征时目标窗口的宽度，用于分辨率不匹配时的缩放回退计算。通常自动记录，无需手填。"),
            ParamDef(name="feature_template_w", editor=EDITOR_INT, default=0, min=0, max=99999, step=1,
                     label="模板总宽",
                     help="原始模板图片的总宽度（像素），用于推算模板中心和out_pos。自动记录。"),
            ParamDef(name="feature_template_h", editor=EDITOR_INT, default=0, min=0, max=99999, step=1,
                     label="模板总高",
                     help="原始模板图片的总高度（像素），用于推算模板中心和out_pos。自动记录。"),
        ],
        type_short_name="image",
    ),

    "PressKey": NodeMeta(
        type="PressKey",
        category=CATEGORY_EXECUTE,
        title="按键",
        description="键盘交给咱~要么输入一段文字（还能用剪贴板粘贴防乱码、末尾按回车），要么按组合键（ctrl+c、alt+f4这种，还能重复按好几次）。",
        color="#B62324",
        accent="#FF7B72",
        # in_text 只在「输入文本」模式存在（见 _resolve_input_ports_default），这里只是静态兜底
        inputs=[_exec_in(), _data_in("in_text", "str", label="文本内容(连线优先)")],
        outputs=[_exec_out()],
        params=[
            # 模式选择（第一位）
            ParamDef(name="kb_mode", editor=EDITOR_ENUM, default="text",
                     options=["text", "key"],
                     option_labels={"text": "输入文本", "key": "按键组合"},
                     label="输入模式",
                     help="输入文本模式(text)：输入一段文字 —— 内容可来自下方「文本内容」参数，"
                          "也可由左侧「文本内容」端口从上游传入（如文字识别、命令输出）。可选粘贴模式防乱码、可选末尾按回车；\n"
                          "按键组合模式(key)：按下组合键，如 ctrl+c、alt+f4、win+r（此模式不使用左侧「文本内容」端口）。"),
            # --- 输入文本模式参数 ---
            ParamDef(name="text", editor=EDITOR_MULTILINE, default="", label="文本内容",
                     help="【输入文本模式】手写要输入的文字，支持 {var} / ${var} / {= 表达式 } 插值。\n"
                          "若左侧「文本内容」端口连了上游且值非空 → 优先用连线值，这里写的内容被忽略；\n"
                          "没连线时用这里写的内容（空则本节点不输入任何文字）。",
                     placeholder="手写要输入的文字，支持 {var} 变量插值。\n"
                                 "例：你好，{user_name}！\n"
                                 "例：识别结果 {ocr_text}\n"
                                 "也可留空，把左侧「文本内容」端口连到上游节点，直接输入上游拿到的内容。"),
            ParamDef(name="use_paste", editor=EDITOR_BOOL, default=True, label="粘贴模式",
                     help="【输入文本模式】勾选=用剪贴板粘贴（快速、防中文乱码）；不勾=逐字符输入。"),
            ParamDef(name="press_enter", editor=EDITOR_BOOL, default=False, label="末尾按回车",
                     help="【输入文本模式】输入完文本后再按一次回车键。"),
            # --- 按键组合模式参数 ---
            ParamDef(name="key_combo", editor=EDITOR_STRING, default="[]",
                     label="按键组合",
                     help="【按键组合模式】按键数组JSON，如 [\"ctrl\",\"c\"]。通过面板下拉框选择，无需手填。"),
            ParamDef(name="count", editor=EDITOR_INT, default=1, label="次数", min=1, max=1000, step=1,
                     help="【按键组合模式】重复按几次。"),
            ParamDef(name="hold_ms", editor=EDITOR_INT, default=50, label="保持(ms)", min=1, max=5000, step=1,
                     help="【按键组合模式】每次按下保持时间。"),
            ParamDef(name="interval_ms", editor=EDITOR_INT, default=100, label="间隔(ms)", min=1, max=60000, step=1,
                     help="【按键组合模式】重复按键之间的间隔。"),
        ],
        type_short_name="keyboard",
    ),
    "Mouse": NodeMeta(
        type="Mouse",
        category=CATEGORY_EXECUTE,
        title="鼠标",
        description="鼠标交给咱啦~点击、移动、拖拽、滚轮随便选，坐标既能去屏幕上点着选，也能从上游找图节点直接连线传过来更省心哟。",
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
            # 1) 范围模式（第一位）
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="坐标基准",
                     help="目标窗口(window)：坐标基于下方「目标窗口」定位到的那个窗口左上角，拾取时自动扣除窗口偏移（配合找图节点的「目标窗口」范围使用）；\n"
                          "全屏(fullscreen)：坐标基于屏幕左上角（绝对屏幕坐标）。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会按绝对坐标硬点。"),
            # 1.5) 目标窗口定位（仅 range_mode=window 时生效）——定位三件套与其他节点共用
            *_target_locator_params("坐标基准"),
            # 2) 动作类型（第二位）
            ParamDef(name="action", editor=EDITOR_ENUM, default="click",
                     options=["click", "move", "drag", "scroll"],
                     option_labels={
                         "click": "点击",
                         "move": "移动", "drag": "拖拽", "scroll": "滚动",
                     },
                     label="动作类型",
                     help="切换动作后，下方仅显示该动作用得上的参数。"),

            # --- 目标点（click/move/scroll 用）---
            ParamDef(name="pos", editor=EDITOR_TUPLE2, default=(0, 0), label="目标点",
                     pick_coord=True,
                     help="鼠标操作的目标坐标 (X, Y)。点 📍 按钮可在屏幕上拾取坐标。"),

            # --- 拖拽起点/终点（drag 独有）---
            ParamDef(name="from_pos", editor=EDITOR_TUPLE2, default=(0, 0), label="起点",
                     pick_coord=True,
                     help="拖拽起点坐标 (X, Y)。点 📍 按钮可在屏幕上拾取坐标。"),
            ParamDef(name="to_pos", editor=EDITOR_TUPLE2, default=(0, 0), label="终点",
                     pick_coord=True,
                     help="拖拽终点坐标 (X, Y)。点 📍 按钮可在屏幕上拾取坐标。"),
            ParamDef(name="drag_duration_ms", editor=EDITOR_INT, default=300, min=0, max=10000,
                     label="拖拽时长(ms)", help='0=瞬间；越大越平滑'),

            # --- 按钮/时长相关 ---
            ParamDef(name="button", editor=EDITOR_ENUM, default="left",
                     options=["left", "right"],
                     option_labels={
                         "left": "左键", "right": "右键",
                     },
                     label="鼠标按键",
                     help="点击 / 拖拽使用的鼠标键。"),
            ParamDef(name="click_count", editor=EDITOR_INT, default=1, min=1, max=3,
                     label="点击次数", help='1=单击 2=双击 3=三击'),
            ParamDef(name="hold_ms", editor=EDITOR_INT, default=50, min=1, max=5000,
                     label="按下保持(ms)",
                     help="【点击】按下到抬起的保持时长（固定值），与 PressKey 的「保持(ms)」同口径。"),
            ParamDef(name="post_delay_ms", editor=EDITOR_INT, default=50, min=0, max=5000,
                     label="节点后延迟(ms)", help="本节点操作完成后、继续下一个节点之前的等待时间（固定值），与 FindImage 的「点击后延迟」同口径。\n常用于操作后等程序响应（弹菜单、切换场景），避免下一节点立刻跑时目标还没就绪。设 0 = 不等待。"),

            # --- scroll 独有 ---
            ParamDef(name="scroll_dy", editor=EDITOR_INT, default=400, min=-5000, max=5000,
                     label="滚动距离(像素)", help='像素估算值，正=滚轮向前(页面/视角向上滚) 负=滚轮向后(页面/视角向下滚)。按常见浏览器1格≈40像素做经验换算：400≈1页、100≈小半页、1000≈2~3页；实际多少取决于目标程序和系统"每次滚动行数"设置。'),
            ParamDef(name="scroll_steps", editor=EDITOR_INT, default=1, min=1, max=100,
                     label="重复次数",
                     help="【滚动】滚动动作重复次数，每次间隔约 20ms。"),
        ],
        type_short_name="mouse",
    ),
    # ---------------- 变量 ----------------
    "SetVariable": NodeMeta(
        type="SetVariable",
        category=CATEGORY_LOGIC,
        title="设置变量",
        description="存变量用的小盒子~每行写 名字=值，还能塞别的变量进去算一算，存好了后面所有节点都能拿来用哟。\n\n每个变量还会在右侧长出一个同名「数据出口」（○），可把值直接连给「组件」节点的入参端口，或组件蓝图里「结束」节点的返回值端口。",
        color="#2D333B",
        accent="#F778BA",
        inputs=[
            _exec_in(),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(
                name="num_data_inputs",
                editor=EDITOR_INT,
                default=0,
                min=0,
                max=20,
                label="数据输入端口数",
                help="动态增减数据输入端口（+/- 按钮调整），每个端口对应一个输入值。\n临时变量名（所有节点统一写法，就这一种）：\n- 完整数组：__INPUTS__（list类型，可写 {__INPUTS__[0]} 索引）\n- 按索引单值：__INPUTS__0（第1个端口）、__INPUTS__1（第2个）、...、__INPUTS__19\n用法示例（写在下方「批量设置」里）：\n  cmd_out   = {__INPUTS__0}     # 第1个端口接的 Command stdout 存 cmd_out\n  clip_text = {__INPUTS__1}     # 第2个端口接的 Clipboard 存 clip_text\n  total     = {= {__INPUTS__0} + {__INPUTS__1}}"
            ),
            ParamDef(name="batch_text", editor=EDITOR_MULTILINE, default="", label="批量设置",
                     placeholder="# 用法1：字面量\nuser = Alice\nport = 8080\n\n# 用法2：多输入端口数组（先在上方加端口数 N，再连 N 条线）\ncmd_out   = {__INPUTS__0}\nclip_text = {__INPUTS__1}\ntotal     = {= {__INPUTS__0} + {__INPUTS__1}}",
                     help="每行 name=value。value 支持 {var}/${var}/{= expr} 插值。\n"
                          "空行、以 # 或 // 开头的整行算注释，忽略。\n"
                          "【变量名写法】必须以字母/下划线/汉字开头，后面可以是字母/数字/下划线，允许点号（如 target.hwnd）。\n"
                          "⚠ 不能以数字开头，也不能含空格或横杠（写成这样这一行不成立、不会生成出参，画布上会标 ⚠）。\n"
                          "【右侧出参】每认下一个变量名，节点右侧就长出一个同名 ○ 数据出口；\n"
                          "写错的行不会生成出口 —— 画布上会标 ⚠，点上方「✓ 检查格式」看具体是哪一行。\n"
                          "【多输入端口用法】\n- 先调整上方「数据输入端口数」加需要的 N 个端口，连线到上游节点\n"
                          "- 每个端口按顺序存为临时变量，就这一种写法：\n  __INPUTS__0（第1个端口）、__INPUTS__1（第2个端口）、__INPUTS__2 ...\n"
                          "- 示例：\n  cmd_out   = {__INPUTS__0}      # 把第1个端口接的 Command stdout 存成变量 cmd_out\n"
                          "  clip_text = {__INPUTS__1}      # 把第2个端口接的 Clipboard 存成 clip_text\n"
                          "  total     = {= {__INPUTS__0} + {__INPUTS__1}}\n"
                          "- 也可以直接在表达式里写：{= {__INPUTS__2} * 2}"),
        ],
        type_short_name="set_var",
        has_dynamic_ports=True,
    ),
    # ---------------- 通知 ----------------
    "Notify": NodeMeta(
        type="Notify",
        category=CATEGORY_EXECUTE,
        title="通知",
        description="想提醒点啥就用咱~要么写在日志里，要么弹系统小窗口，还能放提示音，内容里塞变量也没问题哟。",
        color="#0B3A5C",
        accent="#58A6FF",
        inputs=[
            _exec_in(),
            _data_in("in_title", "any", label="标题(连线优先)"),
            _data_in("in_message", "any", label="内容(连线优先)"),
        ],
        outputs=[_exec_out()],
        params=[
            ParamDef(name="title", editor=EDITOR_STRING, default="通知", label="标题",
                     help="通知标题。【优先级】若 in_title 连线非空则优先用连线值，否则用本参数。支持 {var} 插值"),
            ParamDef(name="message", editor=EDITOR_MULTILINE, default="", label="内容",
                     help="通知正文。【优先级】若 in_message 连线非空则优先用连线值，否则用本参数。支持 {var}/${var}/{= expr} 插值；{ALL_VARS} 展开所有变量"),
            ParamDef(
                name="level",
                editor=EDITOR_ENUM,
                default="INFO",
                label="级别",
                options=["INFO", "WARN", "ERROR", "DEBUG"],
                option_labels={"INFO": "信息", "WARN": "警告", "ERROR": "错误", "DEBUG": "调试"},
                help="通知级别：写入日志面板时的级别标记；弹系统弹窗时 ERROR/WARN 会使用对应的图标。"
            ),
            ParamDef(name="duration_sec", editor=EDITOR_FLOAT, default=3.0, label="持续(s)",
                     min=0.5, max=3600.0, step=0.5,
                     help="主窗口右上角浮动提示（Toast）停留几秒后自动消失。\n"
                          "勾了「系统弹窗」时不受本参数影响 —— 那是阻塞弹窗，要点确定才关。"),
            ParamDef(name="show_popup", editor=EDITOR_BOOL, default=False, label="系统弹窗",
                     help="勾选后额外弹出系统原生阻塞弹窗（用于打断用户注意，会阻塞蓝图直到关闭）；\n"
                          "不勾选时（默认）：右上角主窗口弹非阻塞浮动Toast + 日志面板记录（不卡蓝图执行，推荐）"),
            ParamDef(name="play_sound", editor=EDITOR_BOOL, default=False, label="提示音",
                     help="勾选后播放系统提示音"),
        ],
        type_short_name="notify",
    ),
    # ---------------- 系统交互 ----------------
    "Clipboard": NodeMeta(
        type="Clipboard",
        category=CATEGORY_EXECUTE,
        title="剪贴板",
        description="管系统剪贴板的小助手~要么读出来存变量还顺带给下游数据线；要么把汝写的（或上游传进来的）内容写进去；也能一键清空剪贴板哟。",
        color="#4527a0",
        accent="#7e57c2",
        inputs=[_exec_in(), _data_in("in_text", "str", label="写入内容(连线优先)")],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
            _data_out("out_text", "str", label="剪贴板文本"),
        ],
        params=[
            ParamDef(name="mode", editor=EDITOR_ENUM, default="read",
                     options=["read", "write", "clear"],
                     option_labels={"read": "读取", "write": "写入", "clear": "清空"},
                     label="操作模式",
                     help='read：读剪贴板 → 数据出口 out_text（并同时存为变量）；\n'
                          'write：把文本写进剪贴板，内容来自左侧「写入内容」连线，或用下面的「写入内容」参数；\n'
                          'clear：清空剪贴板。\n'
                          '注意：左侧「写入内容」端口一直存在（切模式不会消失、不会断线），但只有 write 模式会用它；'
                          'read / clear 模式下连了也不会写。'),
            ParamDef(name="value", editor=EDITOR_MULTILINE, default="",
                     label="写入内容",
                     help='仅「操作模式=写入」生效：手写要写进剪贴板的文本，支持 {var} / ${var} / {= 表达式 } 插值。\n'
                          '与左侧「写入内容」端口二选一 —— 端口有连线且值非空时用连线值，否则用本参数。',
                     placeholder="手写要写进剪贴板的文本，支持 {var} 变量插值。\n"
                                 "例：{user_name}\n"
                                 "例：识别结果 {ocr_text}\n"
                                 "也可留空，把左侧「写入内容」端口连到上游节点，直接写入上游拿到的内容。"),
            ParamDef(name="output_var_name", editor=EDITOR_STRING, default="clip_text",
                     label="输出变量名",
                     help='仅 mode=read 生效：读到的文本同时存到 ctx.variables，后续节点 {clip_text} 直接用'),
        ],
        type_short_name="clipboard",
    ),
    "Command": NodeMeta(
        type="Command",
        category=CATEGORY_APP,
        title="执行命令",
        description="跑Windows命令或者外部程序用哒~命令里能塞变量，三种shell模式随便挑；不想等程序关掉就往下跑可以关了捕获；命令报错退出码非零也可以当失败处理；标准输出、错误、退出码都会捡回来给下游用哟。",
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
                     label="命令",
                     help='支持 {变量} 插值。例：dir C:\\；python script.py {arg1}；start notepad'),
            ParamDef(name="working_dir", editor=EDITOR_PATH, default="",
                     label="工作目录",
                     help='命令执行时的工作目录；留空=沿用主程序当前目录。支持 {变量} 插值和特殊占位符 {blueprint_dir}=当前蓝图所在文件夹'),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=10000, min=0, max=3600000,
                     label="超时(ms)", help='命令执行超时毫秒数。0=永不超时（慎用）。'),
            ParamDef(name="capture_streams", editor=EDITOR_BOOL, default=True,
                     label="捕获输出",
                     help='关掉后不会等待程序结束立刻out_done；适合打开notepad.exe这类GUI程序'),
            ParamDef(name="shell_mode", editor=EDITOR_ENUM, default="cmd_c",
                     options=["cmd_c", "powershell", "no_shell"],
                     label="shell模式",
                     help='cmd_c=经 cmd /c 执行（支持 dir 等内置命令）；\npowershell=经 PowerShell 执行；\nno_shell=不经 shell 直接启动 exe（参数需自己拼好）。'),
            ParamDef(name="fail_on_nonzero", editor=EDITOR_BOOL, default=False,
                     label="非零退出走err",
                     help='不勾（默认）：不论exit_code是几都走out_done，用户自己用if判断exit_code；勾了：exit_code!=0就走err分支'),
        ],
        type_short_name="cmd",
    ),
    "WebBrowser": NodeMeta(
        type="WebBrowser",
        category=CATEGORY_APP,
        title="打开网页",
        description="帮汝开浏览器访问指定网址，默认浏览器或者Chrome/Edge/Firefox随便汝选。指定了哪家就必须用哪家，找不到直接走「失败」，绝不偷偷换成默认浏览器哟。",
        color="#01579b",
        accent="#29b6f6",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
        ],
        params=[
            ParamDef(name="url", editor=EDITOR_STRING,
                     label="URL",
                     default="https://www.baidu.com/s?wd={keyword}", required=True,
                     help="要访问的网址，支持 {变量} 插值。"
                     ),
            ParamDef(name="browser", editor=EDITOR_ENUM, default="默认",
                     options=["默认", "Chrome", "Edge", "Firefox"],
                     label="浏览器",
                     help="默认(默认)：用系统默认浏览器打开；\n"
                          "指定 Chrome/Edge/Firefox：必须已安装，找不到可执行文件会直接走「失败」出口，"
                          "不会改用默认浏览器顶替（有些操作是绑定浏览器的，兜底会悄悄做错事）。"),
            ParamDef(name="browser_path", editor=EDITOR_PATH, default="",
                     label="浏览器完整路径",
                     help="留空=按「浏览器」下拉自动查找；\n"
                          "填了就只用这个 exe（绿色版、装在 D 盘等非标准位置的浏览器用这个）。\n"
                          "填了但文件不存在 → 直接走「失败」（严格模式，不偷偷换成别的浏览器）。\n"
                          "填了此项时「浏览器」下拉仅用于显示名称，实际启动以本路径为准。"),
            ParamDef(name="wait_sec_after_open", editor=EDITOR_FLOAT, default=5.0, label="打开后等待(s)",
                     min=0.0, max=120.0, step=0.1,
                     help="浏览器启动命令下发后，本节点再等这么多秒才走下一节点。\n"
                          "打开网页不是一瞬间的事（起窗口/页面加载都要时间），等一会儿更稳。\n"
                          "填 0 = 不等（立即走下一节点）。"),
        ],
        type_short_name="web_browser",
    ),
    "HttpRequest": NodeMeta(
        type="HttpRequest",
        category=CATEGORY_APP,
        title="网络请求",
        description="调网络接口用哒~GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS 七种方法随便选，URL、请求头、请求体里都能塞变量，响应字典还能自动拆成 web_ 开头的变量直接用。",
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
                     label="方法",
                     help="HTTP 请求方法。"),
            ParamDef(name="url", editor=EDITOR_STRING, default="https://example.com/api?name={name}",
                     label="URL", required=True,
                     help="要访问的网址，支持 {变量} 插值。"),
            ParamDef(name="headers_json", editor=EDITOR_MULTILINE,
                     default='{\n  "Authorization": "Bearer {token}",\n  "User-Agent": "BA/1.0"\n}',
                     label="请求头",
                     help='JSON 格式，支持 {var} 插值。空=不自定义头'),
            ParamDef(name="body_text", editor=EDITOR_MULTILINE,
                     default='{"key": "{var}", "num": 123}',
                     label="请求体",
                     help='支持 {var} 插值。GET/HEAD 自动忽略；JSON/Form/XML 都可填，原样发送；空=不发请求体'),
            ParamDef(name="content_type", editor=EDITOR_STRING, default="",
                     label="Content-Type",
                     help='空=有请求体且未自定义时自动填 application/json; charset=utf-8'),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=10000, min=500, max=300000,
                     label="超时(ms)",
                     help="请求超时毫秒数；填小于 500 的值会被自动提高到 500。"),
            ParamDef(name="save_json_to_vars", editor=EDITOR_BOOL, default=True,
                     label="JSON拆成变量",
                     help='勾了后响应JSON若是字典，每个 key 自动塞到 ctx.variables["web_"+key]'),
        ],
        type_short_name="http_request",
    ),
    # ---------------- 检测节点 ----------------
    "IfImageDetect": NodeMeta(
        type="IfImageDetect",
        category=CATEGORY_VISION,
        title="检测",
        description="当下就检查好几张图是不是都在屏幕上~完全求值全中才走「检测到」，短路缺一张就立马否决，一真即真（逻辑或）只要撞见任意一张中就喊「检测到」；只瞅当下这一眼，不会等太久哟。\n注意：未添加任何检测图片时（完全求值 / 短路 / 一真即真都一样），将直接走「未检测到」分支（空集视为未匹配）。",
        color="#ef6c00",
        accent="#ff9800",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="检测到"),
            _exec_out("no", label="未检测到"),
            _exec_out("err", label="失败"),
        ],
        params=[
            # 检测范围模式
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="检测范围",
                     help="目标窗口(window)：只在下面「目标窗口」定位到的那个窗口范围内检测，确保所有模板都在同一个窗口内找到，避免跨窗口误匹配；\n"
                          "全屏(fullscreen)：在整块屏幕上检测，可能把不同窗口的相似图案误判为同一目标。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会退化成全屏去检测。"),
            # 目标窗口定位（仅 range_mode=window 时生效）——定位三件套与其他节点共用
            *_target_locator_params("检测范围"),
            # 匹配参数（算法 + 阈值相邻，便于一起调）
            ParamDef(
                name="match_strategy",
                editor=EDITOR_ENUM,
                default="hybrid",
                options=["hybrid", "template", "gray"],
                option_labels={
                    "hybrid": "智能混合",
                    "template": "模板匹配",
                    "gray": "灰度匹配",
                },
                label="匹配算法",
                help='各算法优缺点：\n'
                     '● 智能混合(hybrid)【推荐】：彩色模板→灰度模板，两级级联兜底。先按颜色找，找不到再用灰度找（吃下颜色变化），大多数场景首选。\n'
                     '● 模板匹配(template)：彩色像素归一化相关。速度最快，精度最高；但对缩放、旋转、颜色变化敏感。适合尺寸固定、画面清晰的UI按钮/图标。\n'
                     '● 灰度匹配(gray)：灰度像素归一化相关。容忍颜色变化（如选中态/高亮态），适合颜色可能变化但形状不变的场景。\n'
                     '三种算法都自带多尺度搜索（约 0.45~1.8），可覆盖一定程度的缩放；但都不抗旋转与局部遮挡。',
            ),
            ParamDef(
                name="threshold",
                editor=EDITOR_FLOAT,
                default=0.9,
                label="匹配阈值",
                min=0.3,
                max=1.0,
                step=0.01,
                help="相似度阈值，越高越严格。模板匹配/灰度匹配/混合模式均为相关系数。",
            ),
            ParamDef(
                name="eval_mode",
                editor=EDITOR_ENUM,
                default="complete",
                options=["complete", "short_circuit", "or_true"],
                option_labels={
                    "complete": "完全求值",
                    "short_circuit": "短路模式",
                    "or_true": "一真即真",
                },
                label="求值模式",
                help="完全求值=不管中间是否失败，都会检测所有参考图并在日志输出每张分数，便于调试；全部匹配才走yes。\n"
                     "短路模式=依次检测（AND语义），只要有一张不匹配就立刻返回no，省时间。\n"
                     "一真即真(or_true)（逻辑或）= 只要有任意一张图匹配成功就立刻返回yes；全部都不匹配才走no。",
            ),
            ParamDef(
                name="images_json",
                editor=EDITOR_STRING,
                default="[]",
                label="参考图列表(JSON数组)",
                help='[{"b64":"...","w":100,"h":80},...] Inspector中提供可视化增删UI',
            ),
        ],
        type_short_name="if_img",
    ),
    "StillnessDetect": NodeMeta(
        type="StillnessDetect",
        category=CATEGORY_VISION,
        title="静止",
        description="盯着屏幕上一块区域看~画面安安静静不变就走yes，等太久还在动就超时走no，用来等加载结束最合适啦。区域要先用「🖱 框选ROI」框出来，没框就属于还没配好，直接走「失败」。",
        color="#d84315",
        accent="#ff7043",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="静止"),
            _exec_out("no", label="超时"),
            _exec_out("err", label="失败"),
        ],
        params=[
            # 检测范围模式
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="检测范围",
                     help="目标窗口(window)：ROI 坐标基于下方「目标窗口」定位到的那个窗口左上角，窗口移动 ROI 跟着移动；\n"
                          "全屏(fullscreen)：ROI 坐标基于屏幕左上角，固定不变。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会退化成全屏去检测。"),
            # 目标窗口定位（仅 range_mode=window 时生效）——定位三件套与其他节点共用
            *_target_locator_params("检测范围"),
            # ROI：与「截图 / 文字」节点同款 TUPLE4（四个自旋框），不再是手填字符串
            ParamDef(name="roi", editor=EDITOR_TUPLE4, default=(0, 0, 0, 0),
                     label="ROI区域(x,y,w,h)",
                     help="相对「检测范围」左上角的区域 (x, y, w, h)。**必须框选**：宽或高为 0（未框选）时本节点直接走「失败」出口，不会拿整个范围去判定。\n点上方「🖱 框选ROI」按钮自动填写，一般无需手填。"),
            ParamDef(name="roi_preview_b64", editor=EDITOR_STRING, default="",
                     label="ROI缩略图(只读)",
                     help="框选 ROI 时写入的缩略图，便于画布/面板预览，无需手填。"),
            ParamDef(
                name="stillness_seconds",
                editor=EDITOR_FLOAT,
                default=5.0,
                min=0.1,
                max=600.0,
                step=0.1,
                label="静止持续时间(秒)",
                help="画面需要保持静止的最小时间，默认 5 秒",
            ),
            ParamDef(
                name="timeout_seconds",
                editor=EDITOR_FLOAT,
                default=20.0,
                min=1.0,
                max=3600.0,
                step=0.5,
                label="超时(秒)",
                help="最长等待时间，超过后走「超时」出口，默认 20 秒",
            ),
            ParamDef(
                name="sample_interval_seconds",
                editor=EDITOR_FLOAT,
                default=1.0,
                min=0.03,
                max=60.0,
                step=0.1,
                label="采样间隔(秒)",
                help="多久采样一次屏幕做帧差比较，默认 1 秒",
            ),
            ParamDef(
                name="diff_threshold",
                editor=EDITOR_FLOAT,
                default=0.01,
                min=0.0,
                max=0.5,
                step=0.001,
                label="帧差阈值",
                help="两帧ROI像素差低于该值视为静止",
            ),
        ],
        type_short_name="if_static",
    ),
    "Screenshot": NodeMeta(
        type="Screenshot",
        category=CATEGORY_VISION,
        title="截图",
        description="把目标窗口（或屏幕）的某个 ROI 区域截下来，输出成图像给下游用~可以直接喂给网络请求节点，把图交给别的程序处理，也能存成文件。",
        color="#0e3a5c",
        accent="#1f8dd6",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败"),
            _data_out("image_bgr", "image", label="图像(BGR)"),
            _data_out("image_png_bytes", "bytes", label="PNG字节"),
            _data_out("image_path", "str", label="保存路径"),
        ],
        params=[
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="截取范围",
                     help="目标窗口(window)：截「目标窗口」定位到的那个窗口；\n"
                          "全屏(fullscreen)：截整个虚拟桌面。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会退化成截全屏。"),
            # 目标窗口定位三件套（与其他节点共用，见 _target_locator_params）
            *_target_locator_params("截取范围"),
            ParamDef(name="roi", editor=EDITOR_TUPLE4, default=(0, 0, 0, 0),
                     label="ROI区域(x,y,w,h)",
                     help="相对「截取范围」左上角的区域 (x, y, w, h)。全 0 = 截整个范围。"),
            ParamDef(name="roi_preview_b64", editor=EDITOR_STRING, default="",
                     label="ROI缩略图(只读)",
                     help="框选 ROI 时写入的缩略图，便于画布/面板预览，无需手填。"),
            ParamDef(name="save_path", editor=EDITOR_STRING, default="",
                     label="保存路径(可选)",
                     help="留空=只输出图像不落盘；填了就把 PNG 存到这个路径（支持 {var} 插值），并输出 image_path。"),
        ],
        type_short_name="screenshot",
    ),
    "OcrRecognize": NodeMeta(
        type="OcrRecognize",
        category=CATEGORY_VISION,
        title="文字",
        description="用内置 OCR 引擎识别屏幕上的文字和数字~按「识别范围」现场截图（目标窗口 / 全屏），输出整屏文本和逐条识别结果；还能填一段文本或正则表达式去匹配整屏文本，命中走「匹配到」、没命中走「未匹配到」；一条文字都没识别到就走「无文字」（与「失败」分开，属于正常业务结果，同时也会触发「完成」）。",
        color="#0b3d61",
        accent="#29b6f6",
        inputs=[
            _exec_in(),
        ],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("no_text", label="无文字"),
            _exec_out("matched", label="匹配到"),
            _exec_out("not_matched", label="未匹配到"),
            _exec_out("err", label="失败"),
            _data_out("text", "str", label="整屏文本"),
            _data_out("items", "list", label="识别条目(列表)"),
        ],
        params=[
            ParamDef(name="range_mode", editor=EDITOR_ENUM, default="window",
                     options=["window", "fullscreen"],
                     option_labels={"window": "目标窗口", "fullscreen": "全屏"},
                     label="识别范围",
                     help="目标窗口(window)：只在下面「目标窗口」定位到的那个窗口内截图识别，ROI 坐标相对该窗口左上角；\n"
                          "全屏(fullscreen)：在整块屏幕上截图识别，ROI 坐标相对屏幕左上角。\n"
                          "选「目标窗口」时，窗口没找到会直接走「失败」出口，不会退化成截全屏。"),
            # 目标窗口定位（仅 range_mode=window 时生效）——定位三件套与其他节点共用
            *_target_locator_params("识别范围"),
            ParamDef(name="roi", editor=EDITOR_TUPLE4, default=(0, 0, 0, 0),
                     label="ROI区域(x,y,w,h)",
                     help="相对「识别范围」左上角的区域 (x, y, w, h)。全 0 = 识别整个范围。"),
            ParamDef(name="roi_preview_b64", editor=EDITOR_STRING, default="",
                     label="ROI缩略图(只读)",
                     help="框选 ROI 时写入的缩略图，便于画布/面板预览，无需手填。"),
            ParamDef(name="lang", editor=EDITOR_ENUM, default="ch",
                     options=["ch", "en"],
                     option_labels={"ch": "中文(含数字)", "en": "英文(含数字)"},
                     label="识别语言",
                     help="中文模型同时覆盖中文+数字；英文模型更快，适合纯数字/英文场景。"),
            # 文本匹配（填了「匹配文本」才会做匹配）
            ParamDef(name="match_mode", editor=EDITOR_ENUM, default="contains",
                     options=["contains", "regex"],
                     option_labels={"contains": "包含文本", "regex": "正则表达式"},
                     label="匹配方式",
                     help="包含文本：整屏文本里出现「匹配文本」这几个字就算命中（区分大小写）；\n"
                          "正则表达式：把「匹配文本」当成正则去匹配整屏文本，例如 [Hh]ello、金币\\s*(\\d+)、error|失败。\n"
                          "两种方式都只在填了「匹配文本」时才生效。"),
            ParamDef(name="match_text", editor=EDITOR_STRING, default="",
                     label="匹配文本",
                     help="要在整屏文本里查找的内容（此处不做 {变量} 插值，避免与正则的 {} 冲突）。\n"
                          "留空 = 不做匹配：识别到文字时只走「完成」出口；\n"
                          "填了 = 命中走「匹配到」、没命中走「未匹配到」（两者都会同时触发「完成」）。"),
        ],
        type_short_name="ocr_recognize",
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
# SHORT_NAME_MAP: 小写短名 -> 驼峰名（序列化 from_dict 自动识别）
# 在 NODE_LIBRARY 初始化后构建，避免循环
# ============================================================
SHORT_NAME_MAP: Dict[str, str] = {}
for _ba_name, _meta in NODE_LIBRARY.items():
    _qn = getattr(_meta, "type_short_name", "") or ""
    if _qn:
        SHORT_NAME_MAP.setdefault(_qn, _ba_name)


def parse_varswitch_conditions(raw: str) -> List[Dict[str, Any]]:
    """解析 VarSwitch（条件判断）AND 批量条件文本：
    每行格式：变量名 运算符 值（空格可选，与 SetVariable 的 gg=1 一样紧凑写也合法）
    支持运算符：==  !=  >  <  >=  <=
    空行、以 # 或 // 开头的整行视为注释，跳过。

    ★ 支持【简写语法】：如果行首直接是运算符（>= 60 / == 42 等），自动补 __INPUTS__0 前缀。
       这样用户把数据连到 in_0 端口后，条件里不用重复写 __INPUTS__0。

    返回：[{"label": str(该条件可读文本), "var": str, "op": str, "value": Any, "raw_line": str, "line_no": int, "error": str}]
    解析失败的行 error 非空。
    """
    import re
    results: List[Dict[str, Any]] = []
    if not raw or not isinstance(raw, str):
        return results

    # ---- 预处理：简写语法（行首直接运算符 == 42 / >= 60）补 __INPUTS__0 前缀 ----
    _op_prefix = re.compile(r'^\s*(==|!=|>=|<=|>|<)\s*(.+)')
    _normalized_lines = []
    if isinstance(raw, str):
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                _normalized_lines.append(line)
                continue
            m = _op_prefix.match(line)
            if m:
                _normalized_lines.append(f"__INPUTS__0 {m.group(1)} {m.group(2)}")
            else:
                _normalized_lines.append(line)
    raw_normalized = "\n".join(_normalized_lines)

    # 整行正则：^\s* (合法变量名) \s* (双字符运算符优先 == != >= <= | 单字符 > <) \s* (值) \s*$
    # 注意：运算符顺序必须双字符在前，否则 "==" 会被错拆成 "=" + "="
    # 变量名字符集与 SetVariable 的 batch_text **完全一致**（变量名允许汉字/点号，
    # 见 variable_engine._RE_VAR_NAME）：读侧 {血量} 早就支持，写侧不能只认 ASCII。
    _var_name = r"[^\W\d]\w*(?:\.[^\W\d]\w*)*"
    line_re = re.compile(
        rf'^\s*({_var_name})\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$',
        re.DOTALL,
    )
    for idx, line in enumerate(raw_normalized.split("\n"), 1):
        ln = line.rstrip()
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = line_re.match(ln)
        if not m:
            # 给更精确的错误提示：判断是哪一部分缺失
            head_re = re.compile(rf'^\s*({_var_name})')
            hm = head_re.match(ln)
            if not hm:
                results.append({
                    "error": "条件开头不合法：变量名须字母/下划线/汉字开头（数字开头、含空格或横杠都不行）",
                    "raw_line": ln, "line_no": idx,
                })
                continue
            var_part = hm.group(1)
            tail = ln[hm.end():]
            # 检查运算符
            op_check = re.match(r'^\s*(==|!=|>=|<=|>|<)', tail)
            if not op_check:
                results.append({
                    "error": f"缺少合法运算符（支持 == != > < >= <=），变量 '{var_part}' 后面内容为: '{tail.strip()[:20]}'",
                    "raw_line": ln, "line_no": idx,
                })
                continue
            op = op_check.group(1)
            after_op = tail[op_check.end():]
            if not after_op.strip():
                results.append({
                    "error": f"运算符 '{op}' 后面缺少比较值",
                    "raw_line": ln, "line_no": idx,
                })
                continue
            results.append({
                "error": f"值部分不合法（字符串请加引号，数字直接写）: '{after_op.strip()[:30]}'",
                "raw_line": ln, "line_no": idx,
            })
            continue
        var_name = m.group(1)
        op = m.group(2)
        val_raw = m.group(3)
        if not val_raw:
            results.append({
                "error": f"运算符 '{op}' 后面缺少比较值",
                "raw_line": ln, "line_no": idx,
            })
            continue
        # 一行里塞 AND / OR 是「语法看似合法、其实永远判错」的典型陷阱：
        # 它会被当成「值」的一部分（例如值变成 '1 AND y != 2'），既不可能相等、也不报错。
        # 本节点是「每行写一条条件、多行之间自动 AND」，所以这里直接判为格式错误。
        _v = val_raw.strip()
        _quoted = (_v.startswith('"') and _v.endswith('"') and len(_v) >= 2) or \
                  (_v.startswith("'") and _v.endswith("'") and len(_v) >= 2)
        if not _quoted:
            _m_and = re.search(r"(?<![A-Za-z0-9_])(AND|OR|and|or|&&|\|\|)(?![A-Za-z0-9_])", _v)
            if _m_and:
                results.append({
                    "error": f"一行里出现了 '{_m_and.group(1)}'：本节点是「每行写一条条件、多行之间自动 AND」，"
                             f"请把 '{_m_and.group(1)}' 两侧拆成两行（想要 OR 可以串两个条件判断节点）",
                    "raw_line": ln, "line_no": idx,
                })
                continue
        parsed_val, val_err = _parse_condition_value(val_raw)
        if val_err:
            results.append({"error": val_err, "raw_line": ln, "line_no": idx})
            continue
        # label 用于显示：直接是完整的条件字符串（加空格美化）
        label = f"{var_name} {op} {val_raw}"
        results.append({
            "label": label,
            "var": var_name,
            "op": op,
            "value": parsed_val,
            "value_raw": val_raw,
            "raw_line": ln,
            "line_no": idx,
            "error": "",
        })
    return results


def _parse_condition_value(s: str) -> Tuple[Any, str]:
    """解析条件右边的值：字符串""或'' / 数字 / 布尔true/false / None / 含{}插值表达式（原样返回字符串，执行时再算）。"""
    # 含 {} 插值或 ${} → 原样返回字符串，执行时再算
    if "{" in s or "${" in s:
        return s, ""
    s2 = s.strip()
    if (s2.startswith('"') and s2.endswith('"')) or (s2.startswith("'") and s2.endswith("'")):
        if len(s2) < 2:
            return None, "字符串引号不闭合"
        return s2[1:-1], ""
    if s2.lower() in ("true", "false"):
        return s2.lower() == "true", ""
    if s2.lower() in ("none", "null"):
        return None, ""
    # int
    try:
        if s2.startswith(("+", "-")) and s2[1:].isdigit():
            return int(s2), ""
        if s2.isdigit():
            return int(s2), ""
        if s2.lower().startswith("0x"):
            return int(s2, 16), ""
    except Exception:
        pass
    try:
        if ("." in s2) or ("e" in s2.lower()):
            return float(s2), ""
    except Exception:
        pass
    return None, f"无法识别的值格式（字符串需加引号）：'{s2[:30]}'"


def _setvar_parse_issues(batch_text: Any) -> Tuple[List[str], List[str]]:
    """解析 SetVariable 的 batch_text，返回 `(变量名列表, 逐行错误说明列表)`。

    唯一解析口径：内部调 `variable_engine.parse_batch_var_lines`（与运行时 `execute_SetVariable`
    完全同一个函数），所以「画布上看到的出参」「检查格式按钮的结论」「运行时真正生效的变量」
    三者必然一致。

    ⚠ 只有**被认下来的行**才会成为变量、才会生成数据出口。解析失败的行必须由调用方
    显示出来（画布摘要 / 检查格式 / 编辑器日志），不能静默丢掉 ——
    否则用户会看到「批量设置里明明写了变量，画布右侧却没有出参」而毫无线索。

    返回的变量名按书写顺序、已去重、最多 20 个；与固定执行出口 `out_exec` 重名的会被跳过。
    """
    if not isinstance(batch_text, str) or not batch_text.strip():
        return [], []
    try:
        from .variable_engine import parse_batch_var_lines
        kv, err = parse_batch_var_lines(batch_text)
    except Exception as e:
        return [], [f"解析异常：{e}"]
    names: List[str] = []
    for k in kv.keys():
        if not k or str(k) == "out_exec":
            continue
        names.append(str(k))
        if len(names) >= 20:
            break
    errs = [ln.strip() for ln in str(err or "").splitlines() if ln.strip()]
    return names, errs


def _setvar_output_var_names(batch_text: Any) -> List[str]:
    """解析 SetVariable 的 batch_text，返回「按书写顺序、已去重」的变量名列表（最多 20 个）。

    这些名字**就是本节点数据输出端口的名字与标签** —— 用户写几个变量，节点就长出几个出参，
    可直接连到「组件」节点的入参端口，或组件蓝图里「结束」节点的返回值端口。
    执行时每个端口输出「本次刚设置的那个值」。

    ⚠ 与固定执行出口 `out_exec` 重名的变量会被跳过（避免同名端口打架）。
    ⚠ 变量名允许含点号（如 target.hwnd），端口名照样用它，不做改名。
    """
    return _setvar_parse_issues(batch_text)[0]


def _resolve_output_ports_default(node_type: str, params: Optional[Dict[str, Any]] = None) -> List[Port]:
    """返回给定节点类型的默认输出端口列表（动态端口节点按参数生成）。

    供 Node.from_dict 反序列化时自动补默认端口，避免丢失连线。
    """
    meta = NODE_LIBRARY.get(node_type)
    if meta is None:
        return []
    params = params or {}
    # -------- 动态端口节点的默认输出 --------
    if node_type == "VarSwitch":
        # 条件判断：AND 语义，固定输出 yes（全满足）/ no（任一不满足）
        return [
            Port(name="yes", type=PortType.EXEC, direction=PortDirection.OUT, label="满足"),
            Port(name="no", type=PortType.EXEC, direction=PortDirection.OUT, label="不满足"),
        ]
    if node_type == "Start":
        # 组件蓝图入参：out_exec 之后跟 num_params 个 param_0 / param_1 ... 数据输出
        n = max(0, min(20, int(params.get("num_params", 0) or 0)))
        outs: List[Port] = [Port(name="out_exec", type=PortType.EXEC, direction=PortDirection.OUT, label="▶")]
        for i in range(n):
            outs.append(Port(name=f"param_{i}", type=PortType.DATA, direction=PortDirection.OUT,
                             data_type="any", label=f"入参{i+1}"))
        return outs
    if node_type == "Component":
        # 调用组件：out_done 执行出口 + num_outputs 个 out_0 / out_1 ... 数据输出（接收子蓝图返回值）
        n = max(0, min(20, int(params.get("num_outputs", 0) or 0)))
        outs: List[Port] = [Port(name="out_done", type=PortType.EXEC, direction=PortDirection.OUT, label="完成")]
        for i in range(n):
            outs.append(Port(name=f"out_{i}", type=PortType.DATA, direction=PortDirection.OUT,
                             data_type="any", label=f"返回{i+1}"))
        return outs
    if node_type == "SetVariable":
        # 数据出口 = batch_text 里设置的变量（一个变量一个出参，端口名/标签就是变量名）
        outs = [Port(name="out_exec", type=PortType.EXEC, direction=PortDirection.OUT, label="▶")]
        for nm in _setvar_output_var_names(params.get("batch_text")):
            outs.append(Port(name=nm, type=PortType.DATA, direction=PortDirection.OUT,
                             data_type="any", label=nm))
        return outs
    if node_type == "Parallel":
        # 并行：完成/失败 两个执行出口（分支在子画布里，不体现在端口上）
        return [
            Port(name="out_done", type=PortType.EXEC, direction=PortDirection.OUT, label="完成"),
            Port(name="err", type=PortType.EXEC, direction=PortDirection.OUT, label="失败"),
        ]
    # -------- 默认：返回 NodeMeta 静态 outputs --------
    return list(meta.outputs)


def _resolve_input_ports_default(node_type: str, params: Optional[Dict[str, Any]] = None) -> List[Port]:
    """返回给定节点类型的默认输入端口列表（动态端口节点按参数生成 in_N 数据输入端口）。

    供 Node.from_dict / create_node 自动生成端口，以及参数变化时重建端口集。
    """
    meta = NODE_LIBRARY.get(node_type)
    if meta is None:
        return []
    params = params or {}
    # -------- 动态端口节点的默认输入 --------
    if node_type == "VarSwitch":
        # 固定有一个执行输入 in_exec，后面跟 num_data_inputs 个 in_0 / in_1 ... 数据输入
        n = max(0, min(20, int(params.get("num_data_inputs", 1) or 1)))
        ins: List[Port] = [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
        for i in range(n):
            ins.append(Port(name=f"in_{i}", type=PortType.DATA, direction=PortDirection.IN,
                            data_type="any", label=f"#{i+1} 输入"))
        return ins
    if node_type == "SetVariable":
        # 固定有一个执行输入 in_exec，后面跟 num_data_inputs 个 in_0 / in_1 ... 数据输入
        n = max(0, min(20, int(params.get("num_data_inputs", 0) or 0)))
        ins: List[Port] = [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
        for i in range(n):
            ins.append(Port(name=f"in_{i}", type=PortType.DATA, direction=PortDirection.IN,
                            data_type="any", label=f"#{i+1} 输入"))
        return ins
    if node_type == "End":
        # 组件蓝图返回值：in_exec 之后跟 num_returns 个 ret_0 / ret_1 ... 数据输入
        n = max(0, min(20, int(params.get("num_returns", 0) or 0)))
        ins: List[Port] = [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
        for i in range(n):
            ins.append(Port(name=f"ret_{i}", type=PortType.DATA, direction=PortDirection.IN,
                            data_type="any", label=f"返回{i+1}"))
        return ins
    if node_type == "Component":
        # 调用组件：in_exec 之后跟 num_inputs 个 in_0 / in_1 ... 数据输入（传给子蓝图入参）
        n = max(0, min(20, int(params.get("num_inputs", 0) or 0)))
        ins: List[Port] = [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
        for i in range(n):
            ins.append(Port(name=f"in_{i}", type=PortType.DATA, direction=PortDirection.IN,
                            data_type="any", label=f"入参{i+1}"))
        return ins
    if node_type == "Parallel":
        # Parallel 输入固定，只有 in_exec
        return [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
    # -------- 默认：返回 NodeMeta 静态 inputs --------
    # 注：PressKey 的「文本内容」/ Clipboard 的「写入内容」端口**固定存在，不随模式增减**
    #     （用户明确要求：切模式不能让端口消失、连带把连线断掉）。只在对应模式下会被执行器使用。
    return list(meta.inputs)


# ============================================================
# 工厂：按 type 实例化一个 Node（自动装配端口 + 默认参数）
# ============================================================
import uuid as _uuid


def create_node(node_type: str, pos=(0, 0), node_id: Optional[str] = None) -> Node:
    meta = NODE_LIBRARY.get(node_type)
    if meta is None:
        raise ValueError(f"未知节点类型: {node_type}")
    nid = node_id or _uuid.uuid4().hex[:12]
    params = meta.default_params()
    has_dynamic = bool(getattr(meta, "has_dynamic_ports", False))
    if has_dynamic:
        inputs = [Port(**p.__dict__) for p in _resolve_input_ports_default(node_type, params)]
        outputs = [Port(**p.__dict__) for p in _resolve_output_ports_default(node_type, params)]
    else:
        inputs = [Port(**p.__dict__) for p in meta.inputs]
        outputs = [Port(**p.__dict__) for p in meta.outputs]
    return Node(
        id=nid,
        type=node_type,
        pos=pos,
        params=params,
        inputs=inputs,
        outputs=outputs,
    )


def get_meta(node_type: str) -> Optional[NodeMeta]:
    return NODE_LIBRARY.get(node_type)
