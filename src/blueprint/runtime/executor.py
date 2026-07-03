import os
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from ..core.models import (
    BlueprintGraph,
    ExecutionContext,
    Node,
    PortDirection,
    PortType,
)
from ..core.node_library import NODE_LIBRARY
from .debug_annotator import annotate_step
from .hotkey_manager import HotkeyManager
from .input_actions import InputActions
from .screen_capture import ScreenCapture
from .template_matcher import TemplateMatcher


# ============================================================
# 辅助：端口/连线 解析
# ============================================================
def _resolve_data_inputs(graph: BlueprintGraph, ctx: ExecutionContext, node: Node) -> Dict[str, Any]:
    """
    解析节点所有 DATA 输入端口的值：
      - 若端口被连线：取上游 last_outputs
      - 否则：取 node.params 同名值
    """
    resolved: Dict[str, Any] = {}
    for p in node.inputs:
        if p.type != PortType.DATA:
            continue
        found_edge = None
        for e in graph.edges.values():
            if e.to_node == node.id and e.to_port == p.name:
                found_edge = e
                break
        if found_edge:
            upstream = ctx.last_outputs.get(found_edge.from_node, {})
            resolved[p.name] = upstream.get(found_edge.from_port)
        else:
            resolved[p.name] = node.params.get(p.name)
    return resolved


def _exec_out_edges(graph: BlueprintGraph, node_id: str, out_port: str) -> List[Tuple[str, str]]:
    """返回 (next_node_id, next_in_exec_port) 列表。"""
    result = []
    for e in graph.edges.values():
        if e.from_node == node_id and e.from_port == out_port:
            result.append((e.to_node, e.to_port))
    return result


# ============================================================
# execute_*: 每类节点的执行函数
# 返回 (outputs_dict, next_out_ports_list, extra_note_for_annotation)
# ============================================================
def execute_Start(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    return {}, ["out_exec"], "开始"


def execute_Branch(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    cond = bool(inputs.get("in_cond", False))
    next_ports = ["out_true"] if cond else ["out_false"]
    annotate_step(ctx, node, "branch", note=f"cond={cond} -> {next_ports[0]}")
    return {"out_cond": cond}, next_ports, ""


def execute_Loop(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    max_it = int(node.params.get("max_iterations", 9999) or 9999)
    interval = float(node.params.get("interval_sec", 0.0) or 0.0)
    counter = ctx.loop_counters.get(node.id, 0)
    if max_it <= 0:
        max_it = 9999
    if counter < max_it:
        ctx.loop_counters[node.id] = counter + 1
        if interval > 0 and ctx.is_running:
            t0 = time.time()
            while ctx.is_running and (time.time() - t0) < interval:
                wait_if_paused(ctx)
                time.sleep(0.05)
        wait_if_paused(ctx)
        note = f"Loop i={counter+1}/{max_it} -> loop_body"
        annotate_step(ctx, node, "loop", note=note)
        ctx.log(f"[Loop] {node.id[:6]}  i={counter+1}/{max_it} -> loop_body", "DEBUG")
        return (
            {"out_iteration": counter + 1, "out_max": max_it},
            ["loop_body"],
            "",
        )
    else:
        ctx.log(
            f"[Loop] {node.id[:6]} 达到 max_iterations={max_it}，退出循环。",
            "WARN",
        )
        annotate_step(ctx, node, "loop", note=f"EXIT: hit max={max_it}")
        ctx.loop_counters.pop(node.id, None)
        return (
            {"out_iteration": counter, "out_max": max_it},
            ["out_exit"],
            "",
        )


def execute_Wait(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    sec = inputs.get("in_seconds")
    if sec is None:
        sec = float(node.params.get("default_sec", 1.0) or 1.0)
    sec = max(0.0, min(float(sec), 3600.0))
    note = f"等待 {sec:.3f}s"
    ctx.log(f"[Wait] sleep {sec:.3f}s", "INFO")
    t0 = time.time()
    while ctx.is_running and (time.time() - t0) < sec:
        wait_if_paused(ctx)
        time.sleep(0.05)
    wait_if_paused(ctx)
    annotate_step(ctx, node, "wait", note=note)
    return {"out_waited_sec": round(sec, 4)}, ["out_exec"], ""


def execute_TargetSetup(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    exe_path = str(node.params.get("exe_path", "") or "")
    exe_args_raw = str(node.params.get("exe_args", "") or "")
    working_dir = str(node.params.get("working_dir", "") or "")
    wait_sec = float(node.params.get("wait_sec_after_launch", 2.0) or 2.0)
    title = str(node.params.get("window_title", "") or "")
    w = int(node.params.get("resolution_w", 1920) or 1920)
    h = int(node.params.get("resolution_h", 1080) or 1080)
    note_parts = [f"res_ref={w}x{h}"]
    hwnd = None
    launched_pid = None

    # 1. 启动 EXE（有路径就启动）
    if exe_path:
        import shlex
        try:
            args_list = shlex.split(exe_args_raw, posix=False) if exe_args_raw else []
        except Exception:
            args_list = [s for s in exe_args_raw.split(" ") if s]
        from src.tools.window_utils import start_exe, find_hwnd_by_title_substring, bring_window_to_front
        ok, pid, msg = start_exe(
            exe_path, args=args_list, working_dir=working_dir or None,
            wait_sec_after_launch=wait_sec, log_fn=ctx.log,
        )
        if not ok:
            ctx.log(f"[TargetSetup] 启动失败: {msg}", "WARN")
            note_parts.append(f"启动失败:{msg[:40]}")
        else:
            launched_pid = pid
            note_parts.append(f"PID={pid}")
    else:
        from src.tools.window_utils import find_hwnd_by_title_substring, bring_window_to_front

    # 2. 绑定 HWND（launched_pid + title 双重过滤，经验教训 1043761 句柄优先防误选）
    if title or launched_pid is not None:
        hwnd = find_hwnd_by_title_substring(
            title, pid=launched_pid, log_fn=ctx.log,
        )
        if hwnd is None and title:
            # 退化为仅标题
            hwnd = find_hwnd_by_title_substring(title, pid=None, log_fn=ctx.log)
        if hwnd is not None:
            import win32gui
            try:
                wt = win32gui.GetWindowText(hwnd) or ""
                note_parts.append(f"HWND={hwnd} title='{wt[:30]}'")
            except Exception:
                note_parts.append(f"HWND={hwnd}")
            bring_window_to_front(hwnd, log_fn=ctx.log)
        else:
            note_parts.append("窗口未找到(使用全屏)")

    # 3. 把 HWND/Rect 推给下游核心服务
    if hwnd is not None and ctx.screen is not None:
        ctx.screen.hwnd = hwnd
    if ctx.matcher is not None:
        ctx.matcher.config["resolution_ref"] = (w, h)
        if hwnd is not None:
            try:
                import win32gui
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                ctx.matcher.set_fullscreen_region((l, t, r - l, b - t))
            except Exception:
                pass
    if ctx.input_actions is not None:
        if hwnd is not None:
            try:
                import win32gui
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                ctx.input_actions.set_safe_region((l, t, r - l, b - t))
            except Exception:
                pass
    note = " | ".join(note_parts)
    ctx.log(f"[TargetSetup] {note}", "SUCCESS")
    annotate_step(ctx, node, "target", note=note)
    return {"out_hwnd": hwnd, "out_resolution": (w, h)}, ["out_exec"], ""


# ---------------- 动作/识别节点 ----------------
def execute_FindImage(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    找图节点：
      - 失败策略 on_fail: stop | continue | retry_then_skip
      - 重试 retry_count + retry_interval_ms
      - 命中：out_pos / out_found=True / out_score
      - 未命中：根据 on_fail 决定是否终止
    """
    template = inputs.get("in_template") or node.params.get("template", "")
    region = inputs.get("in_region") or node.params.get("region")
    threshold = inputs.get("in_threshold")
    if threshold is None:
        threshold = float(node.params.get("threshold", 0.75) or 0.75)
    fast_mode = bool(node.params.get("fast_mode", True))
    use_gray = bool(node.params.get("use_gray", True))
    on_fail = str(node.params.get("on_fail", "stop") or "stop")
    retry_count = max(0, int(node.params.get("retry_count", 3) or 0))
    retry_interval_ms = max(10.0, float(node.params.get("retry_interval_ms", 500.0) or 500.0))

    if not template:
        ctx.log("[FindImage] template 为空，失败。", "ERROR")
        annotate_step(ctx, node, "find", note="ERROR: template empty")
        return _find_fail(ctx, node, on_fail, "NO_TEMPLATE")

    pos = None
    score = 0.0
    attempts = retry_count + 1
    last_err = ""
    for i in range(attempts):
        if not ctx.is_running:
            break
        wait_if_paused(ctx)
        try:
            if ctx.matcher is not None:
                if use_gray:
                    pos = ctx.matcher.find_image_gray(
                        str(template), region=region,
                        threshold=threshold, fast_mode=fast_mode,
                    )
                else:
                    pos = ctx.matcher.find_image(
                        str(template), region=region,
                        threshold=threshold, fast_mode=fast_mode,
                    )
                if pos is not None:
                    score = 1.0
            else:
                pos = None
        except Exception as e:
            last_err = str(e)
            ctx.log(f"[FindImage] 异常: {e}", "WARN")
        if pos is not None:
            break
        if i < attempts - 1 and ctx.is_running:
            t0 = time.time()
            dt_s = retry_interval_ms / 1000.0
            while ctx.is_running and (time.time() - t0) < dt_s:
                wait_if_paused(ctx)
                time.sleep(0.05)

    if pos is not None:
        ctx.log(f"[FindImage] 命中 {template}  pos={pos}", "SUCCESS")
        annotate_step(
            ctx, node, "find",
            pos=pos, region=region,
            note=f"OK: {os.path.basename(str(template))}@{pos}",
        )
        return (
            {"out_pos": tuple(pos), "out_found": True, "out_score": float(score)},
            ["out_exec"], "",
        )
    else:
        msg = f"未找到: {os.path.basename(str(template))} threshold={threshold}"
        if last_err:
            msg += f" err={last_err}"
        ctx.log(f"[FindImage] {msg} (策略={on_fail})", "WARN")
        return _find_fail(ctx, node, on_fail, msg, region=region)


def _find_fail(ctx, node, on_fail: str, reason: str, region=None):
    annotate_step(
        ctx, node, "find",
        region=region, note=f"FAIL: {reason} | on_fail={on_fail}",
    )
    outputs = {"out_pos": None, "out_found": False, "out_score": 0.0}
    if on_fail == "stop":
        ctx.log("[FindImage] 策略=stop: 终止执行。", "ERROR")
        ctx.is_running = False
        return outputs, [], ""
    elif on_fail == "continue":
        return outputs, ["out_exec"], ""
    elif on_fail.startswith("retry"):
        return outputs, ["out_exec"], ""
    else:
        ctx.is_running = False
        return outputs, [], ""


def execute_FindAnyImage(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    templates_raw = str(node.params.get("templates", "") or "")
    templates = [t.strip() for t in templates_raw.split(",") if t.strip()]
    threshold = float(node.params.get("threshold", 0.75) or 0.75)
    fast_mode = bool(node.params.get("fast_mode", True))
    timeout_ms = max(0.0, float(node.params.get("timeout_ms", 0.0) or 0.0))
    interval_ms = max(50.0, float(node.params.get("interval_ms", 300.0) or 300.0))

    if not templates:
        ctx.log("[FindAnyImage] templates 为空", "ERROR")
        annotate_step(ctx, node, "find", note="ERROR: templates empty")
        return {"out_found_index": -1, "out_found": False, "out_pos": None}, ["out_not_found"], ""

    t0 = time.time()
    hit_idx = -1
    hit_pos = None
    while ctx.is_running:
        wait_if_paused(ctx)
        for i, tpl in enumerate(templates):
            if not ctx.is_running:
                break
            try:
                if ctx.matcher is not None:
                    p = ctx.matcher.find_image(tpl, threshold=threshold, fast_mode=fast_mode)
                else:
                    p = None
            except Exception:
                p = None
            if p is not None:
                hit_idx, hit_pos = i, p
                break
        if hit_idx >= 0:
            break
        if timeout_ms <= 0:
            break
        if (time.time() - t0) * 1000.0 >= timeout_ms:
            break
        time.sleep(interval_ms / 1000.0)

    if hit_idx >= 0 and hit_pos is not None:
        out_port = f"out_found_{hit_idx}"
        ctx.log(f"[FindAnyImage] 命中 #{hit_idx}: {templates[hit_idx]} @ {hit_pos}", "SUCCESS")
        annotate_step(ctx, node, "find", pos=hit_pos, note=f"hit #{hit_idx}: {os.path.basename(templates[hit_idx])}")
        return (
            {"out_found_index": hit_idx, "out_found": True, "out_pos": tuple(hit_pos)},
            [out_port], "",
        )
    else:
        ctx.log(f"[FindAnyImage] 全部未命中({len(templates)}张) -> out_not_found", "WARN")
        annotate_step(ctx, node, "find", note=f"ALL MISS ({len(templates)}张)")
        return {"out_found_index": -1, "out_found": False, "out_pos": None}, ["out_not_found"], ""


def execute_WaitAnyImage(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    templates_raw = str(node.params.get("templates", "") or "")
    templates = [t.strip() for t in templates_raw.split(",") if t.strip()]
    threshold = float(node.params.get("threshold", 0.75) or 0.75)
    timeout_ms = max(100.0, float(node.params.get("timeout_ms", 5000.0) or 5000.0))
    interval_ms = max(50.0, float(node.params.get("interval_ms", 300.0) or 300.0))
    fast_mode = bool(node.params.get("fast_mode", True))

    if not templates:
        ctx.log("[WaitAnyImage] templates 为空", "ERROR")
        annotate_step(ctx, node, "find", note="ERROR: templates empty")
        return {"out_found_index": -1, "out_found": False, "out_pos": None}, ["out_exec"], ""

    t0 = time.time()
    hit_idx = -1
    hit_pos = None
    while ctx.is_running and (time.time() - t0) * 1000.0 < timeout_ms:
        wait_if_paused(ctx)
        for i, tpl in enumerate(templates):
            if not ctx.is_running:
                break
            try:
                if ctx.matcher is not None:
                    p = ctx.matcher.find_image(tpl, threshold=threshold, fast_mode=fast_mode)
                else:
                    p = None
            except Exception:
                p = None
            if p is not None:
                hit_idx, hit_pos = i, p
                break
        if hit_idx >= 0:
            break
        time.sleep(interval_ms / 1000.0)

    found = hit_idx >= 0 and hit_pos is not None
    if found:
        ctx.log(f"[WaitAnyImage] 命中 #{hit_idx}: {templates[hit_idx]} @ {hit_pos}", "SUCCESS")
        annotate_step(ctx, node, "find", pos=hit_pos, note=f"WAIT HIT #{hit_idx}")
    else:
        ctx.log(f"[WaitAnyImage] 超时未命中 -> 继续", "WARN")
        annotate_step(ctx, node, "find", note="WAIT TIMEOUT")
    return (
        {
            "out_pos": tuple(hit_pos) if found else None,
            "out_found_index": hit_idx,
            "out_found": found,
        },
        ["out_exec"], "",
    )


def execute_Click(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    mode = str(node.params.get("mode", "position") or "position")
    key = str(node.params.get("key", "") or "")
    double = bool(node.params.get("double", False))
    post_delay = float(node.params.get("post_delay", 0.1) or 0.1)
    ro_radius = int(node.params.get("random_offset_radius", 8) or 8)
    hold_min = int(node.params.get("hold_ms_min", 30) or 30)
    hold_max = int(node.params.get("hold_ms_max", 80) or 80)
    post_min = int(node.params.get("post_delay_min_ms", 80) or 80)
    post_max = int(node.params.get("post_delay_max_ms", 200) or 200)
    hold_range = (min(hold_min, hold_max), max(hold_min, hold_max))
    post_range = (min(post_min, post_max), max(post_min, post_max))

    in_pos = inputs.get("in_pos")
    in_region = inputs.get("in_region")
    actual_pos = None
    note = ""

    if mode == "key":
        if not key:
            ctx.log("[Click] key模式但key为空，跳过。", "WARN")
            annotate_step(ctx, node, "click", note="ERROR: key empty")
            return {}, ["out_exec"], ""
        ctx.log(f"[Click] 按键 {key}", "INFO")
        if ctx.input_actions is not None:
            ctx.input_actions.hw_press(key, hold_ms_range=hold_range)
        annotate_step(ctx, node, "keypress", note=f"key={key}")
    else:
        pos = None
        if mode == "region" and in_region and len(in_region) == 4:
            x, y, w, h = in_region
            pos = (int(x) + int(w) // 2, int(y) + int(h) // 2)
            annotate_step(ctx, node, "click", region=in_region)
        elif in_pos and len(in_pos) == 2:
            pos = (int(in_pos[0]), int(in_pos[1]))
        if pos is None:
            ctx.log("[Click] 没有可用的坐标/区域，跳过。", "WARN")
            annotate_step(ctx, node, "click", note="WARN: no pos/region")
            return {}, ["out_exec"], ""
        ctx.log(f"[Click] 点击 {pos} double={double}", "INFO")
        if ctx.input_actions is not None:
            actual_pos = ctx.input_actions.game_click(
                pos, double=double,
                random_offset_radius=ro_radius,
                hold_ms_range=hold_range,
                post_delay_range=post_range,
            )
        else:
            actual_pos = pos
        annotate_step(
            ctx, node, "click",
            pos=actual_pos, region=in_region if mode == "region" else None,
            note=f"pos={actual_pos} double={double}",
        )

    if post_delay > 0 and ctx.is_running:
        time.sleep(min(post_delay, 10.0))
    return {"out_pos": actual_pos}, ["out_exec"], ""


def execute_PressKey(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    key = str(node.params.get("key", "") or "")
    count = max(1, int(node.params.get("count", 1) or 1))
    hold_ms = max(1, int(node.params.get("hold_ms", 50) or 50))
    interval_ms = max(1, int(node.params.get("interval_ms", 100) or 100))
    if not key:
        ctx.log("[PressKey] key 为空，跳过。", "WARN")
        annotate_step(ctx, node, "keypress", note="ERROR: key empty")
        return {}, ["out_exec"], ""
    ctx.log(f"[PressKey] {key} x{count} hold={hold_ms}ms", "INFO")
    if ctx.input_actions is not None:
        ctx.input_actions.press_with_pause(
            key, count=count, hold_ms=hold_ms, interval_ms=interval_ms,
        )
    annotate_step(ctx, node, "keypress", note=f"{key} x{count}")
    return {}, ["out_exec"], ""


# ---------------- 常量/输入节点 ----------------
def execute_RegionLiteral(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    x = int(node.params.get("x", 0) or 0)
    y = int(node.params.get("y", 0) or 0)
    w = max(1, int(node.params.get("w", 100) or 1))
    h = max(1, int(node.params.get("h", 100) or 1))
    return {"out_region": (x, y, w, h)}, [], ""


def execute_TemplatePicker(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    path = str(node.params.get("template_path", "") or "")
    return {"out_template": path}, [], ""


# ---------------- 变量节点 ----------------
def execute_EdgeCounter(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    signal = bool(inputs.get("in_signal", False))
    key_r = node.id + "__rise"
    key_f = node.id + "__fall"
    key_prev = node.id + "__prev"
    prev = ctx.variables.get(key_prev)
    rising_count = int(ctx.variables.get(key_r, 0) or 0)
    falling_count = int(ctx.variables.get(key_f, 0) or 0)
    edge_rising = False
    edge_falling = False
    if prev is None:
        pass
    elif (not prev) and signal:
        rising_count += 1
        edge_rising = True
    elif prev and (not signal):
        falling_count += 1
        edge_falling = True
    ctx.variables[key_prev] = signal
    ctx.variables[key_r] = rising_count
    ctx.variables[key_f] = falling_count
    note = f"rising={rising_count} falling={falling_count}"
    if edge_rising or edge_falling:
        annotate_step(ctx, node, "edge", note=note)
    return (
        {
            "rising_count": rising_count,
            "falling_count": falling_count,
            "edge_rising": edge_rising,
            "edge_falling": edge_falling,
        },
        ["out_exec"], "",
    )


def execute_SetVariable(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    name = str(node.params.get("name", "") or "")
    value = inputs.get("in_value")
    if name:
        ctx.set_var(name, value)
    note = f"SetVar {name} = {repr(value)[:60]}"
    ctx.log(f"[Var] {note}", "DEBUG")
    annotate_step(ctx, node, "var", note=note)
    return {}, ["out_exec"], ""


def execute_GetVariable(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    name = str(node.params.get("name", "") or "")
    default = node.params.get("default")
    value = ctx.get_var(name, default)
    return {"out_value": value}, [], ""


def execute_Note(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    return {}, [], ""


# ============================================================
# 节点类型 -> 执行函数 映射
# ============================================================
EXECUTORS = {
    "Start": execute_Start,
    "Branch": execute_Branch,
    "Loop": execute_Loop,
    "Wait": execute_Wait,
    "TargetSetup": execute_TargetSetup,
    "FindImage": execute_FindImage,
    "FindAnyImage": execute_FindAnyImage,
    "WaitAnyImage": execute_WaitAnyImage,
    "Click": execute_Click,
    "PressKey": execute_PressKey,
    "RegionLiteral": execute_RegionLiteral,
    "TemplatePicker": execute_TemplatePicker,
    "EdgeCounter": execute_EdgeCounter,
    "SetVariable": execute_SetVariable,
    "GetVariable": execute_GetVariable,
    "Note": execute_Note,
}


# ============================================================
# 辅助：暂停等待（is_paused=True 时循环 sleep，直到恢复或被停止）
# ============================================================
def wait_if_paused(ctx: ExecutionContext, tick_s: float = 0.05):
    """若 ctx.is_paused，每 tick_s 秒检查一次，直到暂停解除或 ctx.is_running=False（停止）。"""
    while bool(getattr(ctx, "is_paused", False)) and bool(getattr(ctx, "is_running", True)):
        time.sleep(tick_s)


# ============================================================
# BlueprintRunner（QThread，硬约束 §4 线程隔离）
# ============================================================
class BlueprintRunner(QThread):
    """
    后台执行蓝图：
      - Signal: log_message(str, str) / highlight_node(str) / finished_ok / finished_err(str)
      - 严格不直接操作 Qt 控件
    """

    log_message = Signal(str, str)
    highlight_node = Signal(str)
    highlight_clear = Signal()
    finished_ok = Signal()
    finished_err = Signal(str)
    progress_step = Signal(int, str)
    pause_changed = Signal(bool)

    def __init__(self, graph: BlueprintGraph, parent=None):
        super().__init__(parent)
        self._graph = graph
        self._ctx: Optional[ExecutionContext] = None
        self._hotkeys: Optional[HotkeyManager] = None

    def request_stop(self):
        if self._ctx is not None:
            self._ctx.is_running = False

    def request_pause(self, pause: bool):
        if self._ctx is not None:
            self._ctx.is_paused = bool(pause)
            self.pause_changed.emit(self._ctx.is_paused)

    def toggle_pause(self) -> bool:
        if self._ctx is None:
            return False
        cur = not bool(self._ctx.is_paused)
        self._ctx.is_paused = cur
        self.pause_changed.emit(cur)
        return cur

    @property
    def is_paused(self) -> bool:
        return bool(self._ctx.is_paused) if self._ctx is not None else False

    # ---------------- 主循环 ----------------
    def run(self):
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_message.emit(f"[Runtime] 开始 run_id={run_id}", "INFO")

        ctx = ExecutionContext(run_id=run_id)
        ctx.log_fn = lambda m, l="INFO": self.log_message.emit(m, l)
        try:
            ctx.matcher = TemplateMatcher(
                log_fn=ctx.log,
                is_running_fn=lambda: getattr(ctx, "is_running", True),
            )
            ctx.input_actions = InputActions(
                log_fn=ctx.log,
                is_running_fn=lambda: getattr(ctx, "is_running", True),
            )
            ctx.screen = ScreenCapture()
        except Exception as e:
            ctx.log(f"[Runtime] 核心服务初始化异常: {e}", "WARN")

        self._ctx = ctx
        ctx.last_progress_ts = time.time()

        # 热键注册（硬约束 §3）
        self._hotkeys = HotkeyManager(
            on_stop=self.request_stop,
            log_fn=lambda m, l="INFO": self.log_message.emit(m, l),
        )
        self._hotkeys.start()

        try:
            self._run_graph(ctx)
            self.log_message.emit(
                f"[Runtime] 完成：步骤 {ctx.step_counter} 个 | run_id={run_id}",
                "SUCCESS" if ctx.is_running else "WARN",
            )
            self.highlight_clear.emit()
            self.finished_ok.emit()
        except Exception as e:
            tb = traceback.format_exc()
            self.log_message.emit(f"[Runtime] 执行异常: {e}\n{tb}", "ERROR")
            self.highlight_clear.emit()
            self.finished_err.emit(str(e))
        finally:
            try:
                if self._hotkeys is not None:
                    self._hotkeys.stop()
            except Exception:
                pass

    # ---------------- 执行算法（文档 §八.2） ----------------
    def _run_graph(self, ctx: ExecutionContext):
        graph = self._graph
        start_node = graph.find_start_node()
        if start_node is None:
            raise RuntimeError("蓝图没有 Start 节点（在校验阶段已报错，但仍被执行）")

        queue: Deque[Tuple[str, str]] = deque()
        # Start 无 in_exec，直接执行它并把 out_exec 入队
        self._execute_node(ctx, graph, start_node, queue)

        max_queue_safety = 10_000_000
        iter_count = 0

        while queue and ctx.is_running:
            wait_if_paused(ctx)
            iter_count += 1
            if iter_count > max_queue_safety:
                ctx.log("[Runtime] 执行队列超过安全上限，强制退出（疑似死循环）。", "ERROR")
                break
            curr_id, curr_out_port = queue.popleft()
            # 找到下游 in_exec
            for next_id, next_in_port in _exec_out_edges(graph, curr_id, curr_out_port):
                if not ctx.is_running:
                    break
                wait_if_paused(ctx)
                next_node = graph.nodes.get(next_id)
                if next_node is None:
                    continue
                self._execute_node(ctx, graph, next_node, queue)
            # 刷新进度看门狗
            ctx.last_progress_ts = time.time()

    def _execute_node(
        self,
        ctx: ExecutionContext,
        graph: BlueprintGraph,
        node: Node,
        queue: Deque[Tuple[str, str]],
    ):
        if not ctx.is_running:
            return
        wait_if_paused(ctx)
        # UI 高亮（硬约束 §4 Signal 回传）
        self.highlight_node.emit(node.id)
        self.progress_step.emit(ctx.step_counter + 1, node.type)

        # 1. 解析 DATA 输入
        inputs = _resolve_data_inputs(graph, ctx, node)

        # 2. 日志
        ctx.log(f"[Node▶] {node.type}  id={node.id[:8]}", "INFO")

        # 3. 执行
        fn = EXECUTORS.get(node.type)
        try:
            if fn is None:
                ctx.log(f"[Node] 未知类型 {node.type}，跳过。", "WARN")
                outputs, next_ports, _ = {}, ["out_exec"], ""
            else:
                outputs, next_ports, _note = fn(ctx, node, inputs)
        except Exception as e:
            tb = traceback.format_exc()
            ctx.log(f"[Node] 执行异常 {node.type}: {e}\n{tb}", "ERROR")
            outputs, next_ports = {}, []

        # 4. 保存输出
        ctx.set_outputs(node.id, outputs or {})

        # 5. 根据 next_ports 入队下游 exec
        for pname in next_ports:
            if not ctx.is_running:
                break
            # 如果节点没出边但有 out_exec（纯数据流节点可能返回空），跳过
            outs = _exec_out_edges(graph, node.id, pname)
            if not outs:
                # 若是执行流终点，记录日志
                if pname.startswith("out"):
                    ctx.log(f"[Node] {node.type}.{pname} 无下游，终止该分支。", "DEBUG")
                continue
            for nid, np in outs:
                queue.append((node.id, pname))
                # 这里注意：executor 主循环会再次用 _exec_out_edges 解析
                # 所以我们在 queue 中存的是 (node.id, out_port_x)，循环会解析为下一跳
                # 但当前我们的实现是：主循环出队 (curr_id, out_port)，然后找下游并执行
                # 因此这里应当 append (node.id, pname)，保持一致即可
                # 为避免重复查找，保持该语义：主循环会用 (curr_id, out_port) 查找下游 in_exec
                # ——无需调整。
