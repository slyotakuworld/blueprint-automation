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
CATEGORY_APP       = "应用控制"     # 第一类：打开程序/绑定窗口/系统命令/网页
CATEGORY_LOGIC     = "逻辑组件"     # 第二类：开始/结束/循环/序列/变量/条件判断/剪贴板/提示
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
        "WebBrowser",    # 3、打开网页（浏览器打开URL）
        "Command",       # 4、系统命令
        "HttpRequest",   # 5、HTTP请求（接口调用：GET/POST/PUT/DELETE/Header/Body/超时）
    ],
    CATEGORY_LOGIC: [
        "Start",         # 1、开始
        "End",           # 2、结束
        "Loop",          # 3、循环
        "Sequence",      # 4、序列
        "SetVariable",   # 5、变量
        "VarSwitch",     # 6、条件判断 → AND 多条件判断，每行 变量名 运算符 值，全部同时满足 → yes / 任一不满足 → no
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


# ============================================================
# 16 个 MVP 节点定义
# ============================================================
NODE_LIBRARY: Dict[str, NodeMeta] = {
    # ---------------- 控制流 ----------------
    "Start": NodeMeta(
        type="Start",
        category=CATEGORY_LOGIC,
        title="开始",
        description="咱是整张蓝图唯一的起点哟~每张蓝图只能放一个咱，从这里开始往下跑！",
        color="#238636",
        accent="#3FB950",
        outputs=[_exec_out()],
        type_short_name="start",
    ),
    "End": NodeMeta(
        type="End",
        category=CATEGORY_LOGIC,
        title="结束",
        description="和Start配对的终点~跑到咱这里就算这条分支收工啦，不会再继续往下走咯。",
        color="#0b3d91",
        accent="#1565c0",
        inputs=[_exec_in("in_exec", label="▶")],
        outputs=[],
        params=[],
        type_short_name="end",
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
    "Wait": NodeMeta(
        type="Wait",
        category=CATEGORY_EXECUTE,
        title="等待",
        description="歇会儿再接着跑~要么固定睡几秒，要么在范围内随机小睡更像真人，上游传过来秒数也能用，实际等了多久还能输出给下游用哟。",
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
                     label="模式",
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
                label="批量设置",
                placeholder="hp >= 30\nhp <= 100\nscore >= 60\ngender != \"female\"\nlevel > {min_level}\n# ↓ 多端口用法：__INPUTS__0=第1个、__INPUTS__1=第2个端口值\n== 1\n== {id}\n>= {min_score}\n# 复杂表达式：\n__INPUTS__0 >= {min_score} AND __INPUTS__1 != {exclude_status}",
                help='每行写一个条件（全部 AND 逻辑，同时满足才走 yes）\n\n两种写法，推荐简写：\n① 完整写法：「变量名 运算符 值」例：score >= 60、level > {min_level}\n   多输入端口引用就这一种：__INPUTS__0=第1个、__INPUTS__1=第2个、...\n② 【简写语法】（连了第1个输入端口时用）：行首直接写运算符，省掉左变量名\n   左值自动 = __INPUTS__0（想主动写表达式时直接写这个名）\n   例：\n     == 1              → 意思：第1个输入 == 数字 1\n     == {id}           → 意思：第1个输入 == 变量 id\n     != {exclude}      → 意思：第1个输入 != 变量 exclude\n     >= {min_score}    → 意思：第1个输入 >= 变量 min_score\n\n运算符：== 等于、!= 不等于、> 大于、< 小于、>= 大于等于、<= 小于等于\n值：字符串加引号 "abc" 或 \'abc\'；数字直接写；布尔写 true/false；支持 {变量} / ${变量} / {= expr} 插值\n空行 和 // 或 # 开头的行 作为注释忽略'
            ),
        ],
        type_short_name="var_switch",
        has_dynamic_ports=True,
    ),
    "Sequence": NodeMeta(
        type="Sequence",
        category=CATEGORY_LOGIC,
        title="顺序执行",
        description="按顺序一条条试路径~短路和一真即真只要一条跑成功就收工走完成；完全模式得条条都成功才过；全败了就走失败口，每两条之间还能歇一会儿哟。",
        color="#57606A",
        accent="#58A6FF",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("1", label="#1"),
            _exec_out("2", label="#2"),
            _exec_out("3", label="#3"),
            _exec_out("else", label="失败"),
            _exec_out("out_done", label="完成"),
        ],
        params=[
            ParamDef(name="mode", editor=EDITOR_ENUM, default="short_circuit",
                     options=["short_circuit", "complete", "or_true"],
                     option_labels={"short_circuit": "短路模式", "complete": "完全求值", "or_true": "一真即真"},
                     label="模式",
                     help="短路模式(short_circuit)：任一分支走到成功语义出口(yes/success/out/found)即立即走 out_done 完成；失败语义出口(no/fail/timeout/not_found)则自动尝试下一条；全部失败走 else。\n完全模式(complete)：所有分支都必须走到成功语义出口才算通过，走 out_done；任一分支失败则走 else。\n一真即真(or_true)（逻辑或）：按顺序尝试各条分支，只要有任意一条分支返回成功语义就立即结束并走 out_done；全部失败才走 else。"),
            ParamDef(name="num_paths", editor=EDITOR_INT, default=3, label="路径数",
                     min=1, max=50, step=1, required=True,
                     help="分支路径数量（1~50）。建议用属性面板的 +/- 按钮调整，避免误操作"),
            ParamDef(name="interval_sec", editor=EDITOR_FLOAT, default=1.0, label="路径间隔(s)",
                     min=0.0, max=3600.0, step=0.05,
                     help="每条路径尝试之间的等待时间（秒）"),
        ],
        type_short_name="seq",
        has_dynamic_ports=True,
    ),
    # ---------------- 场景/窗口绑定（拆分后OpenApp + BindWindow） ----------------
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
            ParamDef(name="path", editor=EDITOR_PATH, default="", label="程序路径"),
            ParamDef(name="args", editor=EDITOR_STRING, default="", label="启动参数"),
            ParamDef(name="working_dir", editor=EDITOR_PATH, default="", label="工作目录",
                     help="程序启动时的 CWD；留空=自动用程序所在目录"),
            ParamDef(name="wait_sec_after_launch", editor=EDITOR_FLOAT, default=2.0, label="等待时间(s)", min=0.0, max=120.0, step=0.1),
        ],
        type_short_name="open_app",
    ),
    "BindWindow": NodeMeta(
        type="BindWindow",
        category=CATEGORY_APP,
        title="绑定窗口",
        description="告诉咱要操作哪个窗口~标题、程序名、类名这仨条件填了就得全对上才算配对成功，绑好之后后面的截图、点击、找图都会自动对准这个窗口的坐标哒。",
        color="#8957E5",
        accent="#A371F7",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("success", label="成功"),
            _exec_out("fail", label="失败"),
        ],
        params=[
            ParamDef(name="title", editor=EDITOR_STRING, default="", label="窗口标题"),
            ParamDef(name="exe_name", editor=EDITOR_STRING, default="", label="程序名"),
            ParamDef(name="class_name", editor=EDITOR_STRING, default="", label="窗口类名"),
            ParamDef(name="resolution_w", editor=EDITOR_INT, default=0, label="分辨率宽",
                     help="0=自动探测窗口实际宽；填了就按这个像素做屏幕坐标归一化"),
            ParamDef(name="resolution_h", editor=EDITOR_INT, default=0, label="分辨率高",
                     help="0=自动探测窗口实际高；填了就按这个像素做屏幕坐标归一化"),
        ],
        type_short_name="bind_win",
    ),
    # ---------------- 动作 / 识别 ----------------
    "FindImage": NodeMeta(
        type="FindImage",
        category=CATEGORY_VISION,
        title="找图",
        description="给咱一张参考图就在屏幕上找~在超时时间里反复试，找不到还能滚动鼠标页面后继续找；找到了可以顺手点一下，就算最后没找到，也会继续往下跑，下游靠「找到」信号自己判断就行啦。",
        color="#0969DA",
        accent="#58A6FF",
        inputs=[
            _exec_in(),
            _data_in("in_template", "path", label="模板"),
            _data_in("in_threshold", "float", label="阈值"),
        ],
        outputs=[
            _exec_out("out_exec", label="执行"),
            _data_out("out_pos", "tuple2", label="位置"),
            _data_out("out_found", "bool", label="找到"),
            _data_out("out_score", "float", label="最高得分"),
        ],
        params=[
            ParamDef(
                name="match_strategy",
                editor=EDITOR_ENUM,
                default="hybrid",
                options=["hybrid", "template", "feature"],
                option_labels={
                    "hybrid": "智能混合",
                    "template": "模板匹配",
                    "feature": "特征匹配",
                },
                label="匹配算法",
                help='各算法优缺点：\n'
                     '● 智能混合(hybrid)【推荐】：先用灰度模板匹配快速初筛，不通过再试特征点。速度较快，兼顾准确性和抗形变能力，大多数场景首选。\n'
                     '● 模板匹配(template)：直接做像素归一化相关。速度最快，像素级精度最高；但参考图和屏幕只要有缩放、旋转、轻微形变或压缩失真就会匹配失败。适合尺寸固定、画面清晰的UI按钮/图标。\n'
                     '● 特征匹配(feature)：提取SIFT/ORB局部特征点做匹配。抗缩放、旋转、部分遮挡和光照变化；但对于大面积纯色、缺少纹理的图片（如纯色方块）会找不到特征点而失败，速度也稍慢。',
            ),
            ParamDef(name="threshold", editor=EDITOR_FLOAT, default=0.75, label="匹配阈值", min=0.3, max=1.0, step=0.01,
                     help="相似度阈值，越高越严格。模板匹配/混合模式=相关系数；特征匹配=内点比例。"),
            ParamDef(name="timeout_seconds", editor=EDITOR_FLOAT, default=10.0, label="超时(秒)", min=0.5, max=600.0, step=0.5,
                     help="在超时时间内会根据自动滚动设置反复重试，直到找到或超时。"),
            ParamDef(name="template", editor=EDITOR_PATH, default="", label="模板路径",
                     help="要查找的图片文件路径（PNG/JPG/BMP等），也可点上面工具栏「📸 截取目标」或「📂 导入图片」直接选择，自动写入路径+预览。"),
            ParamDef(name="template_b64", editor=EDITOR_STRING, default="", label="模板预览图(只读)",
                     help="通过「📸 截取目标」/「📂 导入图片」时会写入b64缩略图，便于画布节点直接预览。"),
            # 未找到时尝试滚动
            ParamDef(name="auto_scroll", editor=EDITOR_BOOL, default=False,
                     label="未找到时尝试滚动",
                     help="勾选=找图失败后自动把鼠标移到屏幕中心并滚动一次，然后继续重试，直到超时。"),
            ParamDef(name="scroll_dy", editor=EDITOR_INT, default=-500, label="垂直滚动量(负=向下)", min=-5000, max=5000, step=10,
                     help="pyautogui.scroll 的垂直 clicks 值，典型值：向下翻页=-500；向上翻页=500。仅勾选「未找到时尝试滚动」才生效。"),
            ParamDef(name="scroll_dx", editor=EDITOR_INT, default=0, label="水平滚动量(正=向右)", min=-5000, max=5000, step=10,
                     help="pyautogui.hscroll 的水平 clicks 值（Windows下部分程序有效）：向右=正数，向左=负数。0=不做水平滚动。仅勾选「未找到时尝试滚动」才生效。"),
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
                     pick_coord=True,
                     help="找到目标后，相对目标中心的像素偏移 (X, Y)。X 正值向右，负值向左；Y 正值向下，负值向上。点右侧 📍 按钮可在屏幕上拾取坐标（取绝对坐标；如需相对模板中心可手动微调）。"),
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
        inputs=[_exec_in()],
        outputs=[_exec_out()],
        params=[
            # 模式选择（第一位）
            ParamDef(name="kb_mode", editor=EDITOR_ENUM, default="text",
                     options=["text", "key"],
                     option_labels={"text": "输入文本", "key": "按键组合"},
                     label="模式",
                     help="输入文本模式(text)：输入一段文字，可选粘贴模式防乱码、可选末尾按回车；\n按键组合模式(key)：按下组合键，如 ctrl+c、alt+f4、win+r。"),
            # --- 输入文本模式参数 ---
            ParamDef(name="text", editor=EDITOR_MULTILINE, default="", label="文本内容",
                     help="【输入文本模式】要输入的文本内容，支持 {var} 变量插值。"),
            ParamDef(name="use_paste", editor=EDITOR_BOOL, default=True, label="粘贴模式",
                     help="【输入文本模式】勾选=用剪贴板粘贴（快速、防中文乱码）；不勾=逐字符输入。"),
            ParamDef(name="press_enter", editor=EDITOR_BOOL, default=False, label="末尾按回车",
                     help="【输入文本模式】输入完文本后再按一次回车键。"),
            # --- 按键组合模式参数 ---
            ParamDef(name="key_name", editor=EDITOR_STRING, default="", label="组合键",
                     required=False,
                     placeholder="如: enter, ctrl+c, alt+f4, win+r, shift+1",
                     help="【按键组合模式】按键名，多键用 + 连接。如: enter, ctrl+c, alt+f4, win+r, shift+1。\n单键: enter, space, esc, a, 1, up, f1 等。"),
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
        title="鼠标操作",
        description="鼠标交给咱啦~点击、按住、释放、移动、拖拽、滚轮随便选，坐标既能去屏幕上点着选，也能从上游找图节点直接连线传过来更省心哟。",
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
            # 1) 坐标参考系（第一位）
            ParamDef(name="coord_mode", editor=EDITOR_ENUM, default="relative",
                     options=["relative", "absolute"],
                     option_labels={"relative": "相对绑定窗口", "absolute": "绝对屏幕坐标"},
                     label="坐标参考系",
                     help="相对绑定窗口(relative)：坐标基于绑定窗口左上角，拾取时自动扣除窗口偏移；\n绝对屏幕坐标(absolute)：坐标基于屏幕左上角。"),
            # 2) 动作类型（第二位）
            ParamDef(name="action", editor=EDITOR_ENUM, default="click",
                     options=["click", "press", "release", "move", "drag", "scroll"],
                     option_labels={
                         "click": "点击", "press": "按住", "release": "释放",
                         "move": "移动", "drag": "拖拽", "scroll": "滚动",
                     },
                     label="动作类型",
                     help="切换动作后，下方仅显示该动作用得上的参数。"),

            # --- 目标点（click/press/move/scroll 用）---
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
            # --- 偏移量（click/press/move/scroll/drag 用）---
            ParamDef(name="offset", editor=EDITOR_TUPLE2, default=(0, 0), label="偏移量",
                     help="对目标点的像素偏移 (X, Y)。X 负数=向左，Y 负数=向下。"),
            ParamDef(name="drag_duration_ms", editor=EDITOR_INT, default=300, min=0, max=10000,
                     label="拖拽时长(ms)", help='0=瞬间；越大越平滑'),

            # --- 按钮/时长相关 ---
            ParamDef(name="button", editor=EDITOR_ENUM, default="left",
                     options=["left", "middle", "right", "x1", "x2"],
                     option_labels={
                         "left": "左键", "middle": "中键", "right": "右键",
                         "x1": "侧键1", "x2": "侧键2",
                     },
                     label="鼠标按键"),
            ParamDef(name="click_count", editor=EDITOR_INT, default=1, min=1, max=3,
                     label="点击次数", help='1=单击 2=双击 3=三击'),
            ParamDef(name="hold_ms_min", editor=EDITOR_INT, default=30, min=1, max=5000,
                     label="按下保持最小(ms)"),
            ParamDef(name="hold_ms_max", editor=EDITOR_INT, default=80, min=1, max=5000,
                     label="按下保持最大(ms)"),
            ParamDef(name="post_delay_min_ms", editor=EDITOR_INT, default=30, min=0, max=5000,
                     label="操作后最小延迟(ms)", help="鼠标操作完成后的额外等待时间下限。上下限一致=固定等待；否则在两者间随机。"),
            ParamDef(name="post_delay_max_ms", editor=EDITOR_INT, default=80, min=0, max=5000,
                     label="操作后最大延迟(ms)", help="鼠标操作完成后的额外等待时间上限"),
            ParamDef(name="move_speed_ms", editor=EDITOR_INT, default=0, min=0, max=20000,
                     label="移动时长(ms)", help='0=瞬移'),

            # --- scroll 独有 ---
            ParamDef(name="scroll_dy", editor=EDITOR_INT, default=3, min=-100, max=100,
                     label="垂直滚动(格)", help='正=上 负=下'),
            ParamDef(name="scroll_dx", editor=EDITOR_INT, default=0, min=-100, max=100,
                     label="水平滚动(格)", help='正=右 负=左'),
            ParamDef(name="scroll_steps", editor=EDITOR_INT, default=1, min=1, max=100,
                     label="重复次数"),
        ],
        type_short_name="mouse",
    ),
    # ---------------- 变量 ----------------
    "SetVariable": NodeMeta(
        type="SetVariable",
        category=CATEGORY_LOGIC,
        title="设置变量",
        description="存变量用的小盒子~每行写 名字=值，还能塞别的变量进去算一算，存好了后面所有节点都能拿来用哟。",
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
                     help="每行 name=value。value 支持 {var}/${var}/{= expr} 插值。\n【多输入端口用法】\n- 先调整上方「数据输入端口数」加需要的 N 个端口，连线到上游节点\n- 每个端口按顺序存为临时变量，就这一种写法：\n  __INPUTS__0（第1个端口）、__INPUTS__1（第2个端口）、__INPUTS__2 ...\n- 示例：\n  cmd_out   = {__INPUTS__0}      # 把第1个端口接的 Command stdout 存成变量 cmd_out\n  clip_text = {__INPUTS__1}      # 把第2个端口接的 Clipboard 存成 clip_text\n  total     = {= {__INPUTS__0} + {__INPUTS__1}}\n- 也可以直接在表达式里写：{= {__INPUTS__2} * 2}"),
        ],
        type_short_name="set_var",
        has_dynamic_ports=True,
    ),
    # ---------------- 通知 ----------------
    "Notify": NodeMeta(
        type="Notify",
        category=CATEGORY_LOGIC,
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
            ),
            ParamDef(name="duration_sec", editor=EDITOR_FLOAT, default=3.0, label="持续(s)",
                     min=0.5, max=3600.0, step=0.5,
                     help="系统弹窗/通知显示持续时间（秒）"),
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
        category=CATEGORY_LOGIC,
        title="剪贴板",
        description="管系统剪贴板的小助手~要么读出来存变量还顺带给下游数据线；要么把汝写的（或上游传进来的）内容写进去；也能一键清空剪贴板哟。",
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
                     option_labels={"read": "读取", "write": "写入", "clear": "清空"},
                     label="模式",
                     help='read=读剪贴板→out_text；write=写剪贴板→in_text或下面value；clear=清空剪贴板'),
            ParamDef(name="value", editor=EDITOR_MULTILINE, default="",
                     label="写入内容",
                     help='仅 mode=write 生效；支持 {var} 插值。若 in_text 有连线则用连线值优先，空则用本参数值'),
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
                     help='空=当前项目目录；支持特殊占位符 {blueprint_dir} = 当前蓝图所在文件夹'),
            ParamDef(name="timeout_ms", editor=EDITOR_INT, default=10000, min=0, max=3600000,
                     label="超时(ms)", help='0=永不超时（慎用）'),
            ParamDef(name="capture_streams", editor=EDITOR_BOOL, default=True,
                     label="捕获输出",
                     help='关掉后不会等待程序结束立刻out_done；适合打开notepad.exe这类GUI程序'),
            ParamDef(name="shell_mode", editor=EDITOR_ENUM, default="cmd_c",
                     options=["cmd_c", "powershell", "no_shell"],
                     label="shell模式"),
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
        description="帮汝开浏览器访问指定网址，默认浏览器或者Chrome/Edge/Firefox随便汝选。",
        color="#01579b",
        accent="#29b6f6",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("out_done", label="完成"),
            _exec_out("err", label="失败/超时"),
        ],
        params=[
            ParamDef(name="url", editor=EDITOR_STRING,
                     label="URL",
                     default="https://www.baidu.com/s?wd={keyword}", required=True
                     ),
            ParamDef(name="browser", editor=EDITOR_ENUM, default="默认",
                     options=["默认", "Chrome", "Edge", "Firefox"],
                     label="浏览器"
                     ),
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
                     label="方法"),
            ParamDef(name="url", editor=EDITOR_STRING, default="https://example.com/api?name={name}",
                     label="URL", required=True,
                     help="支持 {var} 插值"),
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
                     label="超时(ms)"),
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
        description="当下就检查好几张图是不是都在屏幕上~完全求值全中才走是，短路缺一张就立马否决，一真即真（逻辑或）只要撞见任意一张中就喊是；只瞅当下这一眼，不会等太久哟。",
        color="#ef6c00",
        accent="#ff9800",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="是"),
            _exec_out("no", label="否"),
        ],
        params=[
            ParamDef(
                name="match_strategy",
                editor=EDITOR_ENUM,
                default="hybrid",
                options=["hybrid", "template", "feature"],
                option_labels={
                    "hybrid": "混合匹配",
                    "template": "模板匹配",
                    "feature": "特征点匹配",
                },
                label="匹配算法",
                help='各算法优缺点：\n'
                     '● 混合匹配(hybrid)【推荐】：先用模板匹配快速初筛，不通过再试特征点。速度较快，兼顾准确性和抗形变能力，适合大多数场景。\n'
                     '● 模板匹配(template)：直接做像素归一化相关。速度最快，像素级精度最高；但参考图和屏幕只要有缩放、旋转、轻微形变或压缩失真就会匹配失败。适合尺寸固定、画面清晰的UI按钮/图标。\n'
                     '● 特征点匹配(feature)：提取SIFT/ORB局部特征点做匹配。抗缩放、旋转、部分遮挡和光照变化；但对于大面积纯色、缺少纹理的图片（如纯色方块）会找不到特征点而失败，速度也稍慢。',
            ),
            ParamDef(
                name="threshold",
                editor=EDITOR_FLOAT,
                default=0.9,
                label="相似度阈值",
                min=0.3,
                max=1.0,
                step=0.01,
                help="模板匹配/混合下的相关系数阈值；特征匹配下为内点比例阈值。越大越严格，越小越宽松。",
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
                     "短路模式=从左到右依次检测（AND语义），只要有一张不匹配就立刻返回no，省时间。\n"
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
        title="静止检测",
        description="盯着屏幕上一块区域看~画面安安静静不变就走yes，等太久还在动就超时走no，用来等加载结束最合适啦。",
        color="#d84315",
        accent="#ff7043",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="静止"),
            _exec_out("no", label="超时"),
        ],
        params=[
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
                label="最大超时(秒)",
                help="最长等待时间，超过后走no分支，默认 20 秒",
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
                name="roi",
                editor=EDITOR_STRING,
                default="[0,0,0,0]",
                label="静止检测ROI",
            ),
            ParamDef(
                name="roi_preview_b64",
                editor=EDITOR_STRING,
                default="",
                label="ROI缩略图",
                help="Inspector里点框选时写入b64",
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
    "SoundDetect": NodeMeta(
        type="SoundDetect",
        category=CATEGORY_VISION,
        title="声音检测",
        description="竖起耳朵听声音~要么等提示音响起来就满足，要么等环境彻底安静下来才算数，等太久就超时走no哟。",
        color="#00838f",
        accent="#00acc1",
        inputs=[_exec_in()],
        outputs=[
            _exec_out("yes", label="满足"),
            _exec_out("no", label="超时/不满足"),
        ],
        params=[
            ParamDef(name="device", editor=EDITOR_STRING, default="default_loopback",
                     label="音频设备ID",
                     help='default_loopback=当前默认扬声器（录制系统播放的声音）；可从下拉框直接选择扬声器环回或麦克风设备'),
            ParamDef(
                name="judge_mode", editor=EDITOR_ENUM, default="detect_sound",
                options=["detect_sound", "detect_silence"],
                option_labels={
                    "detect_sound": "检测声音",
                    "detect_silence": "检测静音",
                },
                label="判断模式",
                help='检测声音：只要采样期间出现一次超阈值声音就判定满足（适合等提示音）；\n'
                     '检测静音：采样期间全部低于阈值才判定满足（适合等环境安静下来）。',
            ),
            ParamDef(name="sample_ms", editor=EDITOR_INT, default=500, min=50, max=10000,
                     label="单次采样时长(ms)", help="越长越准，但单次检测越慢；「检测静音」建议设 500~2000ms"),
            ParamDef(name="threshold_db", editor=EDITOR_FLOAT, default=-30.0, min=-90.0, max=0.0, step=0.5,
                     label="振幅阈值(dBFS)", help="默认 -30dB≈人耳清晰可闻；调大(如-10)必须很响才算；调小(如-60)轻微响声也触发"),
            ParamDef(name="timeout_seconds", editor=EDITOR_FLOAT, default=5.0, min=0.0, max=600.0, step=0.5,
                     label="超时(秒)", help="0=永不超时(慎用)；默认5秒内一直不满足条件就走 no"),
            ParamDef(name="debug_visual", editor=EDITOR_BOOL, default=False,
                     label="调试可视化",
                     help="仅debug时勾；会在日志用ASCII字符画绘制采样波形+阈值线"),
        ],
        type_short_name="if_sound",
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
    返回：[{"label": str(该条件可读文本), "var": str, "op": str, "value": Any, "raw_line": str, "line_no": int, "error": str}]
    解析失败的行 error 非空。
    """
    import re
    results: List[Dict[str, Any]] = []
    if not raw or not isinstance(raw, str):
        return results
    # 整行正则：^\s* (合法变量名) \s* (双字符运算符优先 == != >= <= | 单字符 > <) \s* (值) \s*$
    # 注意：运算符顺序必须双字符在前，否则 "==" 会被错拆成 "=" + "="
    line_re = re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$',
        re.DOTALL,
    )
    for idx, line in enumerate(raw.split("\n"), 1):
        ln = line.rstrip()
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = line_re.match(ln)
        if not m:
            # 给更精确的错误提示：判断是哪一部分缺失
            head_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)')
            hm = head_re.match(ln)
            if not hm:
                results.append({
                    "error": "条件开头不合法：变量名必须是字母/下划线开头，后接字母数字下划线",
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


def _resolve_output_ports_default(node_type: str, params: Optional[Dict[str, Any]] = None) -> List[Port]:
    """返回给定节点类型的默认输出端口列表（VarSwitch/Sequence后续动态扩展，当前先返回 NodeMeta.outputs 静态定义）。

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
    if node_type == "Sequence":
        n = max(1, int(params.get("num_paths", 3) or 3))
        outs: List[Port] = []
        for i in range(1, n + 1):
            outs.append(Port(name=str(i), type=PortType.EXEC, direction=PortDirection.OUT, label=f"#{i}"))
        outs.append(Port(name="else", type=PortType.EXEC, direction=PortDirection.OUT, label="失败"))
        outs.append(Port(name="out_done", type=PortType.EXEC, direction=PortDirection.OUT, label="完成"))
        return outs
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
    if node_type == "Sequence":
        # Sequence 输入固定，只有 in_exec
        return [
            Port(name="in_exec", type=PortType.EXEC, direction=PortDirection.IN, label="▶"),
        ]
    # -------- 默认：返回 NodeMeta 静态 inputs --------
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
