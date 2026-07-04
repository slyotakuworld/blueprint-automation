import json
import os
import tempfile
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

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
# Sequence 短路OR分支：语义常量
# ============================================================
# 失败语义出口：分支一旦走到这些端口之一 → 立即判定该分支失败，尝试下一条
FAIL_SEMANTIC = {"fail", "timeout", "not_found", "no"}
# 成功语义出口：走到这些端口且无下游连线 → 视为分支到达终点（成功）
# 注：中性端口（out_exec / 普通out）继续向下游走，不立即判定


def _walk_branch_until_end(
    ctx: ExecutionContext,
    graph: BlueprintGraph,
    runner: "BlueprintRunner",
    seq_node_id: str,
    branch_out_port: str,
) -> Tuple[bool, str]:
    """
    同步 walk Sequence 的一条分支直到执行流终点：
      - True 成功语义：
        * 到达执行流终点（无下游可走），且过程中从未命中 FAIL_SEMANTIC
      - False 失败语义：
        * 任何节点返回 FAIL_SEMANTIC 端口；或分支起点无下游连线
    返回 (success: bool, reason: str)
    """
    edges = _exec_out_edges(graph, seq_node_id, branch_out_port)
    if not edges:
        return False, f"路径#{branch_out_port}端口无下游连线"

    curr_id, _curr_in = edges[0]
    safety = 50000
    while safety > 0 and ctx.is_running:
        safety -= 1
        node = graph.nodes.get(curr_id)
        if node is None:
            return False, f"分支节点不存在 id={curr_id[:8]}"

        wait_if_paused(ctx)

        # UI 高亮 + 进度（复用 runner 信号
        try:
            runner.highlight_node.emit(curr_id)
            runner.progress_step.emit(ctx.step_counter + 1, node.type)
        except Exception:
            pass

        # 1) DATA 输入解析
        inputs = _resolve_data_inputs(graph, ctx, node)
        ctx.step_counter += 1

        # 2) 执行（复用 EXECUTORS）
        ctx.log(f"[Node▶] {node.type}  id={curr_id[:8]} [seq分支]", "INFO")
        fn = EXECUTORS.get(node.type)
        try:
            if fn is None:
                outputs, next_ports, _note = {}, ["out_exec"], ""
            else:
                outputs, next_ports, _note = fn(ctx, node, inputs)
        except Exception as e:
            tb = traceback.format_exc()
            ctx.log(f"[Node] seq分支内执行异常 {node.type}: {e}\n{tb}", "ERROR")
            outputs, next_ports = {}, []

        # 3) 保存输出
        ctx.set_outputs(curr_id, outputs or {})
        ctx.last_progress_ts = time.time()

        # 4) 判定 FAIL_SEMANTIC → 立即判负
        if not next_ports:
            # 无 next_ports：到达执行流终点 → 成功（流程跑完）
            return True, f"分支#{branch_out_port} 执行完成到达终点"
        pname = next_ports[0]
        if pname in FAIL_SEMANTIC:
            return False, f"节点 {node.type} 走到失败端口 {pname}"

        # 5) 找下游继续走
        outs = _exec_out_edges(graph, curr_id, pname)
        if not outs:
            # 该端口无下游 → 视为到达终点，成功
            return True, f"到达执行终点（{node.type}.{pname}无下游）"
        curr_id, _curr_in = outs[0]

    if not ctx.is_running:
        return False, "运行被用户停止"
    return False, "分支执行超过安全步数（疑似死循环）"


# ============================================================
# execute_*: 每类节点的执行函数
# 返回 (outputs_dict, next_out_ports_list, extra_note_for_annotation)
# ============================================================
def _auto_coerce(value: Any) -> Any:
    """字符串自动数字/布尔解析，否则原样返回；None/非字符串保持不变。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s == "":
        return ""
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() in ("none", "null"):
        return None
    # int（允许负号、十六进制）
    try:
        if s.startswith(("+", "-")) and s[1:].isdigit():
            return int(s)
        if s.isdigit():
            return int(s)
        if s.lower().startswith("0x"):
            return int(s, 16)
    except Exception:
        pass
    # float
    try:
        f = float(s)
        # 简单判断：如果字符串真的像个 float（含.或e），就返回 f；否则避免把 "123."之类奇怪格式误判（仍保留原串）
        if ("." in s) or ("e" in s.lower()) or ("+" in s and ("e" in s.lower())) or ("-" in s and ("e" in s.lower())):
            return f
    except Exception:
        pass
    return value


def execute_Start(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    return {}, ["out_exec"], "开始"


def execute_Branch(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    cond = bool(inputs.get("in_cond", False))
    next_ports = ["out_true"] if cond else ["out_false"]
    annotate_step(ctx, node, "branch", note=f"cond={cond} -> {next_ports[0]}")
    return {"out_cond": cond}, next_ports, ""


def execute_End(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """【对齐Qflow】蓝图终点：到达此处视为分支结束，不再向下流转出节点。"""
    return {}, [], "结束"


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
    """⚠️ 已废弃：TargetSetup 单节点已按Qflow拆分为 OpenApp（启动程序）+ BindWindow（绑定窗口）。

    保留此函数仅作兜底：旧蓝图运行到此节点时给出WARN并走 fail 分支，不让蓝图直接崩溃。请用户手动删除该节点后，重建为 OpenApp → BindWindow 的串联。
    """
    msg = (
        "[TargetSetup(已废弃)] 该单节点已删除，按Qflow设计拆分为：OpenApp（启动程序）→ BindWindow（绑定窗口）。"
        "请手动删除本节点并重建为新的两节点串联；当前执行跳过并走fail分支。"
    )
    ctx.log(msg, "WARN")
    annotate_step(ctx, node, "target(废弃)", note="废弃TargetSetup(→OpenApp+BindWindow)", extra={"warning": True})
    # 返回 None 输出，走 fail（如果下游有 out_exec → fail 的节点连线，我们尽量不输出 out_exec 让执行中断）
    return {"out_hwnd": None, "out_resolution": None}, [], msg


# ============================================================
# Qflow 对齐：OpenApp（启动程序） + BindWindow（绑定窗口）拆分节点
# ============================================================
def execute_OpenApp(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    path_raw = str(node.params.get("path", "") or "").strip()
    args_raw = str(node.params.get("args", "") or "")
    working_dir_raw = str(node.params.get("working_dir", "") or "").strip()
    wait_sec = float(node.params.get("wait_sec_after_launch", 2.0) or 2.0)
    if wait_sec < 0:
        wait_sec = 0.0

    if not path_raw:
        msg = "[OpenApp] path 为空，未启动任何程序。"
        ctx.log(msg, "WARN")
        annotate_step(ctx, node, "open_app", note="FAIL(path空)")
        return {"out_pid": None, "out_path": path_raw}, ["fail"], msg
    # 路径插值（允许 {game_dir}/xx.exe 这种）
    try:
        from src.blueprint.core.variable_engine import VariableEngine

        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
        if engine.has_any_marker(path_raw):
            path_raw = str(engine.interpolate(path_raw))
        if engine.has_any_marker(working_dir_raw):
            working_dir_raw = str(engine.interpolate(working_dir_raw))
        if engine.has_any_marker(args_raw):
            args_raw = str(engine.interpolate(args_raw))
    except Exception:
        pass
    # ---- 优先复用现有 src.tools.window_utils.start_exe（它含 shlex + sleep + log 封装）----
    try:
        import shlex

        try:
            args_list = shlex.split(args_raw, posix=False) if args_raw else []
        except Exception:
            args_list = [s for s in args_raw.split(" ") if s]
        from src.tools.window_utils import start_exe

        ok, pid, msg = start_exe(
            path_raw,
            args=args_list,
            working_dir=working_dir_raw or None,
            wait_sec_after_launch=wait_sec,
            log_fn=ctx.log,
        )
        if ok:
            note = f"OpenApp PID={pid}  wait={wait_sec}s"
            ctx.log(f"[OpenApp] {note}", "SUCCESS")
            annotate_step(ctx, node, "open_app", note=note)
            return {"out_pid": pid, "out_path": path_raw}, ["out"], ""
        else:
            ctx.log(f"[OpenApp] FAIL: {msg}", "WARN")
            annotate_step(ctx, node, "open_app", note=f"FAIL: {str(msg)[:60]}")
            return {"out_pid": None, "out_path": path_raw}, ["fail"], str(msg or "启动失败")
    except Exception as e:
        tb = traceback.format_exc()
        ctx.log(f"[OpenApp] 异常: {e}\n{tb}", "ERROR")
        annotate_step(ctx, node, "open_app", note=f"Exception: {type(e).__name__}")
        return {"out_pid": None, "out_path": path_raw}, ["fail"], str(e)


def _enum_windows_win32():
    """兜底：如果 pywin32 装了，用 EnumWindows 返回 [(hwnd, title, classname, pid)]。"""
    results = []
    try:
        import win32gui  # type: ignore

        def _cb(hwnd, _):
            try:
                title = win32gui.GetWindowText(hwnd) or ""
                cls = win32gui.GetClassName(hwnd) or ""
                try:
                    import win32process  # type: ignore

                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    pid = 0
                results.append((hwnd, title, cls, int(pid or 0)))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return results


def _pid_to_exe_name(pid: int) -> str:
    """给定 pid 取进程 exe 文件名（小写），失败返回空串。psutil 优先，无则 ctypes CreateToolhelp32Snapshot 兜底。"""
    pid = int(pid or 0)
    if pid <= 0:
        return ""
    try:
        import psutil  # type: ignore

        p = psutil.Process(pid)
        return (p.name() or "").lower()
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]
        snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            return ""
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(pe)
            ok = ctypes.windll.kernel32.Process32First(snap, ctypes.byref(pe))
            while ok:
                if int(pe.th32ProcessID) == pid:
                    try:
                        return pe.szExeFile.decode("mbcs", errors="ignore").lower()
                    except Exception:
                        return ""
                ok = ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(pe))
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(snap)
    except Exception:
        return ""


def _find_window_3d(
    title: str,
    use_title: bool,
    exe_name: str,
    use_exe: bool,
    class_name: str,
    use_class: bool,
    prefer_visible: bool = True,
) -> Optional[int]:
    """三维搜索：title子串 / exe文件名(小写含) / class_name 精确等于（不区分大小写）。

    退化兜底：如果 3 个开关全为 False → 强制开启 use_title 搜索；
             如果 3 个开关全开但 3 个关键字段全空 → 同样退化强制用 title（空title也允许，返回None）。
    """
    title_s = str(title or "").strip()
    exe_s = str(exe_name or "").strip().lower()
    cls_s = str(class_name or "").strip()
    has_any_keyword = bool(title_s) or bool(exe_s) or bool(cls_s)
    if not has_any_keyword:
        return None
    all_off = (not use_title) and (not use_exe) and (not use_class)
    all_on = use_title and use_exe and use_class
    if all_off or (all_on and not has_any_keyword):
        # 退化兜底：强制开启 title
        use_title = True
        use_exe = bool(exe_s)
        use_class = bool(cls_s)

    wins = _enum_windows_win32()
    if not wins:
        return None
    # 先计算每个窗口匹配了哪些维度，维度越多分越高；同分下可见窗口优先
    scored = []
    import win32gui as _wg

    for hwnd, w_title, w_cls, w_pid in wins:
        # 可见性（优先）
        try:
            vis = _wg.IsWindowVisible(hwnd) if prefer_visible else True
        except Exception:
            vis = False
        score = 0
        dim_ok = 0
        dim_checked = 0
        # title
        if use_title:
            dim_checked += 1
            if title_s and (title_s.lower() in (w_title or "").lower()):
                score += 10
                dim_ok += 1
            elif not title_s:
                # 没填title的维度不算"不合格"，不影响但不加分
                pass
            else:
                score -= 1000
        # exe_name
        if use_exe:
            dim_checked += 1
            w_exe = _pid_to_exe_name(w_pid) if w_pid else ""
            if exe_s:
                if w_exe and (exe_s in w_exe or w_exe.endswith(exe_s)):
                    score += 10
                    dim_ok += 1
                else:
                    score -= 1000
            else:
                # 关键字段空：维度跳过
                pass
        # class_name
        if use_class:
            dim_checked += 1
            if cls_s:
                if (w_cls or "").lower() == cls_s.lower():
                    score += 10
                    dim_ok += 1
                else:
                    score -= 1000
            else:
                pass
        if dim_checked == 0:
            continue
        # 维度必须全满足（只要用户填了对应值的维度必须命中；维度没填值的不要求）
        if score < 0:
            continue
        # 可见加分
        if vis:
            score += 1
        # 匹配维度数优先
        score += dim_ok * 100
        scored.append((score, dim_ok, -w_pid, hwnd))
    if not scored:
        return None
    scored.sort(reverse=True)
    return int(scored[0][3])


def execute_BindWindow(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    title = str(node.params.get("title", "") or "")
    use_title = bool(node.params.get("use_title", True))
    exe_name = str(node.params.get("exe_name", "") or "")
    use_exe = bool(node.params.get("use_exe_name", True))
    class_name = str(node.params.get("class_name", "") or "")
    use_class = bool(node.params.get("use_class_name", False))
    res_w = int(node.params.get("resolution_w", 1920) or 1920)
    res_h = int(node.params.get("resolution_h", 1080) or 1080)

    # ---------- 变量插值（如 title="{game_title}"）----------
    try:
        from src.blueprint.core.variable_engine import VariableEngine

        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
        if engine.has_any_marker(title):
            title = str(engine.interpolate(title))
        if engine.has_any_marker(exe_name):
            exe_name = str(engine.interpolate(exe_name))
        if engine.has_any_marker(class_name):
            class_name = str(engine.interpolate(class_name))
    except Exception:
        pass

    hwnd = _find_window_3d(
        title=title, use_title=use_title,
        exe_name=exe_name, use_exe=use_exe,
        class_name=class_name, use_class=use_class,
    )
    if hwnd is None:
        msg = (
            f"[BindWindow] 未找到匹配窗口。"
            f"title(use={use_title})='{title}'  "
            f"exe(use={use_exe})='{exe_name}'  "
            f"class(use={use_class})='{class_name}'"
        )
        ctx.log(msg, "WARN")
        annotate_step(ctx, node, "bind_win", note="FAIL 未找到窗口")
        return {"out_hwnd": None, "out_resolution": (res_w, res_h)}, ["fail"], msg

    # ---------- 拿到 HWND：先激活前台 ----------
    import win32gui  # type: ignore
    try:
        from src.tools.window_utils import bring_window_to_front

        bring_window_to_front(hwnd, log_fn=ctx.log)
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    # 读实际窗口尺寸（用于下游坐标缩放 & safe_region）
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        win_rect = (l, t, r - l, b - t)
        win_title = win32gui.GetWindowText(hwnd) or ""
    except Exception:
        win_rect = None
        win_title = ""

    # ---------- 推给下游核心服务：screen.hwnd / matcher.set_fullscreen_region / input_actions.set_safe_region ----------
    if ctx.screen is not None:
        try:
            ctx.screen.hwnd = hwnd
        except Exception:
            pass
    if ctx.matcher is not None:
        try:
            ctx.matcher.config["resolution_ref"] = (res_w, res_h)
        except Exception:
            pass
        if win_rect is not None:
            try:
                ctx.matcher.set_fullscreen_region(win_rect)
            except Exception:
                pass
    if ctx.input_actions is not None and win_rect is not None:
        try:
            ctx.input_actions.set_safe_region(win_rect)
        except Exception:
            pass
    # 保存到 ctx 上全局变量（VarEngine / 下游逻辑直接用）
    target_info = {
        "hwnd": hwnd,
        "title": win_title,
        "rect": win_rect,
        "resolution_ref": (res_w, res_h),
    }
    ctx.set_var("target", target_info)
    ctx.set_var("target_hwnd", hwnd)
    ctx.set_var("target_title", win_title)
    if win_rect:
        ctx.set_var("target_x", win_rect[0])
        ctx.set_var("target_y", win_rect[1])
        ctx.set_var("target_w", win_rect[2])
        ctx.set_var("target_h", win_rect[3])
    note_parts = [f"HWND={hwnd}", f"res_ref={res_w}x{res_h}"]
    if win_title:
        note_parts.append(f"title='{win_title[:24]}'")
    if win_rect:
        note_parts.append(f"rect={win_rect}")
    note = "BindWindow " + "  ".join(note_parts)
    ctx.log(f"[BindWindow] {note}", "SUCCESS")
    annotate_step(ctx, node, "bind_win", note=note)
    return {
        "out_hwnd": hwnd,
        "out_resolution": (res_w, res_h),
        "out_rect": win_rect,
        "out_title": win_title,
    }, ["success"], ""


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


def _mouse_final_pos(ctx, params: Dict[str, Any], inputs: Dict[str, Any]):
    """按 bind_mode 把 pos_x/y 转换为屏幕绝对坐标；若上游连了 in_pos 则优先用连线值。"""
    in_pos = inputs.get("in_pos")
    if in_pos is not None and isinstance(in_pos, (tuple, list)) and len(in_pos) == 2:
        x, y = int(in_pos[0]), int(in_pos[1])
        ox = int(params.get("offset_x", 0) or 0)
        oy = int(params.get("offset_y", 0) or 0)
        return (x + ox, y + oy)
    mode = str(params.get("bind_mode", "current_win") or "current_win")
    x = int(params.get("pos_x", 0) or 0)
    y = int(params.get("pos_y", 0) or 0)
    ox = int(params.get("offset_x", 0) or 0)
    oy = int(params.get("offset_y", 0) or 0)
    if mode == "screen_abs":
        return (x + ox, y + oy)
    elif mode == "current_win":
        try:
            target = getattr(ctx, "target", None)
            if target is not None:
                rect = getattr(target, "window_rect", lambda: None)()
                if rect is not None:
                    rx = int(getattr(rect, "x", 0) or 0)
                    ry = int(getattr(rect, "y", 0) or 0)
                    return (rx + x + ox, ry + y + oy)
        except Exception:
            pass
    return (x + ox, y + oy)


def execute_Mouse(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """统一 mouse 执行：根据 action 分派（click/press/release/move/drag/scroll）。"""
    import random
    p = dict(node.params or {})
    action = str(p.get("action", "click") or "click")
    btn = str(p.get("button", "left") or "left")
    ia = getattr(ctx, "input_actions", None)

    try:
        pos = _mouse_final_pos(ctx, p, inputs)
        wait_if_paused(ctx)

        if action == "click":
            clicks = max(1, int(p.get("click_count", 1) or 1))
            hold_min = max(1, int(p.get("hold_ms_min", 30) or 30))
            hold_max = max(1, int(p.get("hold_ms_max", 80) or 80))
            hold_range = (min(hold_min, hold_max), max(hold_min, hold_max))
            post_min = max(0, int(p.get("post_delay_min_ms", 80) or 80))
            post_max = max(0, int(p.get("post_delay_max_ms", 200) or 200))
            post_range = (min(post_min, post_max), max(post_min, post_max))
            double = clicks >= 2
            if ia is not None:
                ia.game_click(pos, double=double, random_offset_radius=0,
                              hold_ms_range=hold_range, post_delay_range=post_range,
                              repeat=clicks if clicks >= 3 else 1)
            annotate_step(ctx, node, "click", pos=pos,
                          note=f"click@{pos} btn={btn} count={clicks}")
            ctx.log(f"[Mouse] click @{pos} btn={btn} x{clicks}", "SUCCESS")

        elif action == "press":
            hold_ms = max(0, int(p.get("press_ms", 0) or 0))
            if ia is not None:
                ia.game_click(pos, double=False, random_offset_radius=0,
                              hold_ms_range=(hold_ms, hold_ms) if hold_ms > 0 else (30, 80),
                              post_delay_range=(0, 0))
            annotate_step(ctx, node, "press", pos=pos, note=f"press@{pos} btn={btn} hold={hold_ms}ms")
            ctx.log(f"[Mouse] press @{pos} btn={btn} hold={hold_ms}ms", "SUCCESS")

        elif action == "release":
            annotate_step(ctx, node, "release", pos=pos, note=f"release btn={btn}")
            ctx.log(f"[Mouse] release btn={btn}", "SUCCESS")

        elif action == "move":
            speed = max(0, int(p.get("move_speed_ms", 0) or 0))
            # 简化实现：直接移动，后续可加入分帧
            if ia is not None:
                try:
                    ia._ensure_dc()
                    if hasattr(ia, "_mouse_move"):
                        ia._mouse_move(pos[0], pos[1])
                    else:
                        ia.game_click(pos, double=False, random_offset_radius=0,
                                      hold_ms_range=(0, 0), post_delay_range=(0, 0))
                except Exception:
                    pass
            annotate_step(ctx, node, "move", pos=pos, note=f"move→{pos} speed={speed}ms")
            ctx.log(f"[Mouse] move→{pos} speed={speed}ms", "SUCCESS")

        elif action == "drag":
            from_x = int(p.get("from_x", 0) or 0)
            from_y = int(p.get("from_y", 0) or 0)
            to_x = int(p.get("to_x", 0) or 0)
            to_y = int(p.get("to_y", 0) or 0)
            dur = max(0, int(p.get("drag_duration_ms", 300) or 300))
            from_pt = (from_x, from_y)
            to_pt = (to_x, to_y)
            if ia is not None:
                try:
                    steps = max(2, dur // 20)
                    ia.game_click(from_pt, double=False, random_offset_radius=0,
                                  hold_ms_range=(20, 20), post_delay_range=(0, 0))
                    for i in range(1, steps + 1):
                        ratio = i / steps
                        px = int(from_x + (to_x - from_x) * ratio)
                        py = int(from_y + (to_y - from_y) * ratio)
                        time.sleep((dur / steps) / 1000.0)
                        wait_if_paused(ctx)
                    ia.game_click(to_pt, double=False, random_offset_radius=0,
                                  hold_ms_range=(20, 20), post_delay_range=(0, 0))
                except Exception:
                    pass
            annotate_step(ctx, node, "drag", pos=to_pt,
                          note=f"drag {from_pt}→{to_pt} dur={dur}ms")
            ctx.log(f"[Mouse] drag {from_pt}→{to_pt} dur={dur}ms", "SUCCESS")

        elif action == "scroll":
            dy = int(p.get("scroll_dy", 0) or 0)
            dx = int(p.get("scroll_dx", 0) or 0)
            steps = max(1, int(p.get("scroll_steps", 1) or 1))
            if ia is not None:
                for i in range(steps):
                    try:
                        if hasattr(ia, "scroll"):
                            ia.scroll(pos, dy=dy, dx=dx)
                        else:
                            ia._ensure_dc()
                    except Exception:
                        pass
                    time.sleep(0.02)
                    wait_if_paused(ctx)
            annotate_step(ctx, node, "scroll", pos=pos,
                          note=f"scroll@{pos} dy={dy} dx={dx} x{steps}")
            ctx.log(f"[Mouse] scroll @{pos} dy={dy} dx={dx} repeat={steps}", "SUCCESS")

        else:
            ctx.log(f"[Mouse] 未知 action={action}", "ERROR")
            annotate_step(ctx, node, "mouse", note=f"ERROR: unknown action={action}")
            return {}, ["err"], ""

    except Exception as e:
        ctx.log(f"[Mouse] 执行失败: {type(e).__name__}: {e}", "ERROR")
        annotate_step(ctx, node, "mouse", note=f"ERROR: {e}")
        return {}, ["err"], ""

    return {}, ["out_done"], ""


# ---------------- 系统交互节点：Clipboard / Command / Web ----------------
def execute_Clipboard(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """剪贴板：读/写/清空"""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QClipboard

    params = dict(node.params or {})
    mode = str(params.get("mode", "read") or "read")
    value_template = str(params.get("value", "") or "")
    output_var = str(params.get("output_var_name", "clip_text") or "clip_text")

    try:
        clip: QClipboard = QApplication.clipboard()
    except Exception:
        clip = None

    try:
        wait_if_paused(ctx)
        if mode == "read":
            text = ""
            if clip is not None:
                text = clip.text() or ""
            if output_var:
                ctx.variables[output_var] = text
            annotate_step(ctx, node, "clip_read", note=f"len={len(text)}")
            ctx.log(f"[Clipboard] read OK len={len(text)} -> var[{output_var}]", "SUCCESS")
            return {"out_text": text}, ["out_done"], ""

        elif mode == "write":
            in_text = inputs.get("in_text") if inputs else None
            if in_text is None or (isinstance(in_text, str) and not in_text):
                # 用 value 参数，支持变量插值
                from .variable_engine import VariableEngine
                text_to_write = VariableEngine.render(value_template, ctx.variables)
            else:
                text_to_write = str(in_text)
            if clip is not None:
                clip.setText(text_to_write)
                # 确保同步到系统剪贴板
                from PySide6.QtCore import QEventLoop, QTimer
                try:
                    loop = QEventLoop()
                    QTimer.singleShot(50, loop.quit)
                    loop.exec()
                except Exception:
                    pass
            annotate_step(ctx, node, "clip_write", note=f"len={len(text_to_write)}")
            ctx.log(f"[Clipboard] write OK len={len(text_to_write)}", "SUCCESS")
            return {"out_text": text_to_write}, ["out_done"], ""

        elif mode == "clear":
            if clip is not None:
                clip.clear()
            annotate_step(ctx, node, "clip_clear")
            ctx.log("[Clipboard] clear OK", "SUCCESS")
            return {"out_text": ""}, ["out_done"], ""

        else:
            ctx.log(f"[Clipboard] 未知 mode={mode}", "ERROR")
            return {"out_text": ""}, ["err"], f"unknown mode: {mode}"

    except Exception as e:
        ctx.log(f"[Clipboard] 失败: {type(e).__name__}: {e}", "ERROR")
        annotate_step(ctx, node, "clip_err", note=str(e))
        return {"out_text": ""}, ["err"], str(e)


def execute_Command(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """执行shell命令/外部程序"""
    import subprocess

    params = dict(node.params or {})
    cmd_template = str(params.get("command", "") or "")
    wd_template = str(params.get("working_dir", "") or "")
    timeout_ms = max(0, int(params.get("timeout_ms", 10000) or 10000))
    capture = bool(params.get("capture_streams", True))
    shell_mode = str(params.get("shell_mode", "cmd_c") or "cmd_c")
    fail_nonzero = bool(params.get("fail_on_nonzero", False))

    try:
        wait_if_paused(ctx)
        from .variable_engine import VariableEngine
        # 变量插值 + 特殊占位符 {blueprint_dir}
        ctx_vars = dict(getattr(ctx, "variables", {}) or {})
        blueprint_dir = ctx_vars.get("blueprint_dir", getattr(ctx, "blueprint_dir", ""))
        ctx_vars["blueprint_dir"] = blueprint_dir
        command = VariableEngine.render(cmd_template, ctx_vars)
        working_dir = VariableEngine.render(wd_template, ctx_vars) if wd_template else ""

        # 解析 shell 模式
        if shell_mode == "no_shell":
            use_shell = False
            args = command
        elif shell_mode == "powershell":
            use_shell = True
            # 包装成 powershell -Command
            args = ["powershell", "-NoProfile", "-Command", command]
            use_shell = False
        else:  # cmd_c 默认
            use_shell = False
            args = ["cmd", "/c", command]

        cwd = working_dir if working_dir and os.path.isdir(working_dir) else None
        stdout_val, stderr_val, exit_code = "", "", -1

        if not capture:
            # 不捕获：启动后立刻 out_done，不等待结束
            try:
                subprocess.Popen(
                    args, shell=use_shell, cwd=cwd,
                    stdin=None, stdout=None, stderr=None,
                    close_fds=True if os.name != "nt" else False,
                )
                ctx.log(f"[Command] 启动(不等待): {command[:80]}", "SUCCESS")
                annotate_step(ctx, node, "cmd_start", note=f"no_wait: {command[:60]}")
                return {"stdout": "", "stderr": "", "exit_code": 0}, ["out_done"], ""
            except Exception as e:
                ctx.log(f"[Command] 启动失败: {e}", "ERROR")
                return {"stdout": "", "stderr": str(e), "exit_code": -1}, ["err"], str(e)

        # 捕获模式：等待执行完
        timeout_s = (timeout_ms / 1000.0) if timeout_ms > 0 else None
        try:
            result = subprocess.run(
                args, shell=use_shell, cwd=cwd,
                capture_output=True, text=True, timeout=timeout_s,
            )
            stdout_val = result.stdout or ""
            stderr_val = result.stderr or ""
            exit_code = int(result.returncode)
        except subprocess.TimeoutExpired as te:
            stdout_val = te.stdout or "" if isinstance(te.stdout, str) else ""
            stderr_val = f"(TIMEOUT after {timeout_ms}ms) " + (te.stderr or "" if isinstance(te.stderr, str) else "")
            exit_code = -999
            ctx.log(f"[Command] 超时 {timeout_ms}ms cmd={command[:60]}", "ERROR")
            annotate_step(ctx, node, "cmd_timeout", note=f"timeout {timeout_ms}ms")
            return {"stdout": stdout_val, "stderr": stderr_val, "exit_code": exit_code}, ["err"], "timeout"

        # 判定出口：fail_nonzero + 非0退出码 → err；否则 out_done
        if fail_nonzero and exit_code != 0:
            ctx.log(f"[Command] exit={exit_code} (fail_on_nonzero=ON) → err", "WARN")
            annotate_step(ctx, node, "cmd_fail", note=f"exit={exit_code}")
            return {"stdout": stdout_val, "stderr": stderr_val, "exit_code": exit_code}, ["err"], ""
        ctx.log(f"[Command] OK exit={exit_code} stdout_len={len(stdout_val)} cmd={command[:60]}", "SUCCESS")
        annotate_step(ctx, node, "cmd_done", note=f"exit={exit_code}")
        return {"stdout": stdout_val, "stderr": stderr_val, "exit_code": exit_code}, ["out_done"], ""

    except Exception as e:
        ctx.log(f"[Command] 异常: {type(e).__name__}: {e}", "ERROR")
        annotate_step(ctx, node, "cmd_err", note=str(e))
        return {"stdout": "", "stderr": str(e), "exit_code": -1}, ["err"], str(e)


def execute_Web(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """网页/HTTP请求：browser开URL；http_get/http_post拿响应"""
    import json as _json
    import urllib.request
    import urllib.error
    import webbrowser

    params = dict(node.params or {})
    mode = str(params.get("mode", "browser") or "browser")
    url_template = str(params.get("url", "") or "")
    body_template = str(params.get("body_json", "") or "")
    headers_template = str(params.get("headers_json", "") or "")
    timeout_ms = max(500, int(params.get("timeout_ms", 10000) or 10000))
    save_to_vars = bool(params.get("save_json_to_vars", True))

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
        if engine.has_any_marker(url_template):
            url_template = str(engine.interpolate(url_template))
        url = url_template

        if mode == "browser":
            try:
                webbrowser.open(url, new=2)
            except Exception:
                # 兜底：Windows下 os.startfile
                if os.name == "nt":
                    try: os.startfile(url)
                    except Exception: pass
            annotate_step(ctx, node, "web_browser", note=url[:80])
            ctx.log(f"[Web] browser open → {url[:80]}", "SUCCESS")
            return {"response_text": "", "response_json": {}, "status_code": 0}, ["out_done"], ""

        # http_get / http_post：构造请求
        timeout_s = timeout_ms / 1000.0
        # 解析 headers JSON（先插值再parse）
        headers = {}
        try:
            if headers_template and engine.has_any_marker(headers_template):
                headers_template = str(engine.interpolate(headers_template))
            h_text = headers_template if headers_template else ""
            if h_text.strip():
                parsed = _json.loads(h_text)
                if isinstance(parsed, dict):
                    headers = {str(k): str(v) for k, v in parsed.items()}
        except Exception as he:
            ctx.log(f"[Web] headers JSON解析失败: {he}", "WARN")

        data_bytes = None
        method = "GET"
        if mode == "http_post":
            method = "POST"
            try:
                if body_template and engine.has_any_marker(body_template):
                    body_template = str(engine.interpolate(body_template))
                b_text = body_template if body_template else ""
                if b_text.strip():
                    data_bytes = b_text.encode("utf-8")
                    if "Content-Type" not in headers and "content-type" not in [k.lower() for k in headers.keys()]:
                        headers["Content-Type"] = "application/json; charset=utf-8"
            except Exception as be:
                ctx.log(f"[Web] body处理失败: {be}", "WARN")

        req = urllib.request.Request(url, data=data_bytes, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status_code = int(getattr(resp, "status", 200) or getattr(resp, "code", 200))
                try: resp_bytes = resp.read()
                except Exception: resp_bytes = b""
                try: resp_text = resp_bytes.decode("utf-8", errors="replace")
                except Exception: resp_text = str(resp_bytes)
        except urllib.error.HTTPError as he:
            status_code = int(getattr(he, "code", 0) or 0)
            try: resp_bytes = he.read()
            except Exception: resp_bytes = b""
            try: resp_text = resp_bytes.decode("utf-8", errors="replace")
            except Exception: resp_text = str(resp_bytes)
            ctx.log(f"[Web] HTTP {status_code}: {url[:60]}", "WARN")
        except Exception as e:
            ctx.log(f"[Web] 请求异常: {type(e).__name__}: {e}", "ERROR")
            annotate_step(ctx, node, "web_err", note=str(e))
            return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], str(e)

        # 解析 JSON 响应
        response_json: Any = {}
        try:
            if resp_text.strip():
                response_json = _json.loads(resp_text)
        except Exception:
            response_json = {}

        # 自动把 JSON 拆到 ctx.variables web_ 前缀
        if save_to_vars and isinstance(response_json, dict):
            for k, v in response_json.items():
                try:
                    ctx.variables[f"web_{k}"] = v
                except Exception:
                    pass
            ctx.log(f"[Web] 拆入变量 web_xxx keys={list(response_json.keys())[:8]}", "INFO")

        annotate_step(ctx, node, f"web_{mode}", note=f"HTTP {status_code} len={len(resp_text)}")
        ctx.log(f"[Web] {mode} OK status={status_code} len={len(resp_text)}", "SUCCESS")
        return {"response_text": resp_text, "response_json": response_json, "status_code": status_code}, ["out_done"], ""

    except Exception as e:
        ctx.log(f"[Web] 顶层异常: {type(e).__name__}: {e}", "ERROR")
        annotate_step(ctx, node, "web_err", note=str(e))
        return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], str(e)


def execute_WebBrowser(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """【网页】浏览器操作：打开URL/刷新/回退/前进。支持 {var} 插值 + 选择 default/chrome/edge/firefox 浏览器。"""
    import webbrowser
    import shutil as _shutil

    p = dict(node.params or {})
    action = str(p.get("action", "open") or "open").strip().lower()
    url_template = str(p.get("url", "") or "")
    browser_name = str(p.get("browser", "default") or "default").strip().lower()

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(getattr(ctx, "variables", {}) or {})
        if engine.has_any_marker(url_template):
            url_template = str(engine.interpolate(url_template))
        url = url_template if url_template else ""

        # refresh/back/forward：如果用户没填URL，不报错但降级为重开上次URL
        if action in ("refresh", "back", "forward"):
            if not url:
                note_extra = f"{action} 未填URL，无法控制已有浏览器窗口，降级为跳过（请改用 open 模式主动打开URL以保证可控）。"
                annotate_step(ctx, node, f"web_browser_{action}", note=note_extra)
                ctx.log(f"[WebBrowser] {action} no-op（未填URL，无法驱动已打开的窗口）→ ok", "WARN")
                return {}, ["out_done"], ""

        if not url:
            err = "URL 为空（open 模式必须填 URL）"
            annotate_step(ctx, node, "web_browser_err", note=err)
            ctx.log(f"[WebBrowser] {err}", "ERROR")
            return {}, ["err"], err

        # 按 browser_name 找对应可执行程序 → 注册到 webbrowser；找不到就 fallback 到 default
        controller = None
        if browser_name in ("chrome", "edge", "firefox"):
            exe_candidates = {
                "chrome": [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                    "google-chrome", "chrome", "chromium",
                ],
                "edge": [
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                    "msedge", "edge",
                ],
                "firefox": [
                    r"C:\Program Files\Mozilla Firefox\firefox.exe",
                    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
                    "firefox", "mozilla",
                ],
            }.get(browser_name, [])
            for cand in exe_candidates:
                try:
                    if os.path.exists(cand) or _shutil.which(cand):
                        try:
                            controller = webbrowser.get(f'"{cand}" %s')
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
            if controller is None:
                ctx.log(f"[WebBrowser] 指定浏览器 {browser_name} 未找到可执行文件，fallback 到系统默认浏览器", "WARN")

        opened = False
        last_err = None
        try:
            if controller is not None:
                opened = bool(controller.open(url, new=2, autoraise=True))
            else:
                opened = bool(webbrowser.open(url, new=2, autoraise=True))
        except Exception as e:
            last_err = e
            opened = False

        if not opened and os.name == "nt":
            # Windows 兜底：os.startfile（最鲁棒）
            try:
                os.startfile(url)
                opened = True
            except Exception as e2:
                last_err = last_err or e2

        if not opened:
            err_msg = f"打开浏览器失败: {last_err}" if last_err else "打开浏览器失败（无可用浏览器）"
            annotate_step(ctx, node, "web_browser_err", note=err_msg)
            ctx.log(f"[WebBrowser] {err_msg}", "ERROR")
            return {}, ["err"], err_msg

        note = f"[{browser_name}] {action} → {url[:100]}"
        annotate_step(ctx, node, f"web_browser_{action}", note=note)
        ctx.log(f"[WebBrowser] {note}", "SUCCESS")
        return {}, ["out_done"], ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        annotate_step(ctx, node, "web_browser_err", note=err)
        ctx.log(f"[WebBrowser] 顶层异常 {err}", "ERROR")
        return {}, ["err"], err


def execute_HttpRequest(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """【HTTP请求】网络接口调用：method=GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS，返回response_text/json/status_code。"""
    import json as _json
    import urllib.request
    import urllib.error

    p = dict(node.params or {})
    method = str(p.get("method", "GET") or "GET").strip().upper() or "GET"
    url_template = str(p.get("url", "") or "")
    headers_template = str(p.get("headers_json", "") or "")
    body_template = str(p.get("body_text", "") or "")
    content_type_override = str(p.get("content_type", "") or "").strip()
    timeout_ms = max(500, int(p.get("timeout_ms", 10000) or 10000))
    save_to_vars = bool(p.get("save_json_to_vars", True))

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(getattr(ctx, "variables", {}) or {})
        if engine.has_any_marker(url_template):
            url_template = str(engine.interpolate(url_template))
        url = url_template
        if not url.strip():
            err = "URL 为空"
            annotate_step(ctx, node, "http_err", note=err)
            return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], err

        # headers
        headers: Dict[str, str] = {}
        try:
            if headers_template and engine.has_any_marker(headers_template):
                headers_template = str(engine.interpolate(headers_template))
            h_text = headers_template if headers_template else ""
            if h_text.strip():
                parsed = _json.loads(h_text)
                if isinstance(parsed, dict):
                    headers = {str(k): str(v) for k, v in parsed.items()}
        except Exception as he:
            ctx.log(f"[HttpRequest] headers JSON 解析失败: {he}", "WARN")

        # body（GET/HEAD 忽略）
        data_bytes = None
        has_body = False
        if method not in ("GET", "HEAD") and body_template.strip():
            try:
                if body_template and engine.has_any_marker(body_template):
                    body_template = str(engine.interpolate(body_template))
                b_text = body_template if body_template else ""
                if b_text.strip():
                    data_bytes = b_text.encode("utf-8")
                    has_body = True
                    # Content-Type：优先用户 override → 用户手动写过某个 content-type 头 → 自动 application/json
                    def _has_ct(d: Dict[str, str]) -> bool:
                        return any(k.lower() == "content-type" for k in d.keys())
                    if content_type_override:
                        # 用户写了 content_type 参数 → 覆盖/插入 头
                        # 先删除旧的同名(含大小写)头，确保只剩一个
                        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                        headers["Content-Type"] = content_type_override
                    elif not _has_ct(headers):
                        headers["Content-Type"] = "application/json; charset=utf-8"
            except Exception as be:
                ctx.log(f"[HttpRequest] body 处理失败: {be}", "WARN")

        timeout_s = timeout_ms / 1000.0
        req = urllib.request.Request(url, data=data_bytes, headers=headers or {}, method=method)
        status_code = 0
        resp_text = ""
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status_code = int(getattr(resp, "status", 200) or getattr(resp, "code", 200) or 200)
                try: resp_bytes = resp.read()
                except Exception: resp_bytes = b""
                try: resp_text = resp_bytes.decode("utf-8", errors="replace")
                except Exception: resp_text = str(resp_bytes)
        except urllib.error.HTTPError as he:
            status_code = int(getattr(he, "code", 0) or 0)
            try: resp_bytes = he.read()
            except Exception: resp_bytes = b""
            try: resp_text = resp_bytes.decode("utf-8", errors="replace")
            except Exception: resp_text = str(resp_bytes)
            ctx.log(f"[HttpRequest] HTTP {status_code}: {method} {url[:80]}", "WARN")
        except Exception as e:
            ctx.log(f"[HttpRequest] 请求异常: {type(e).__name__}: {e}", "ERROR")
            annotate_step(ctx, node, "http_err", note=f"{type(e).__name__}: {e}")
            return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], f"{type(e).__name__}: {e}"

        # JSON 解析
        response_json: Any = {}
        try:
            if resp_text.strip():
                response_json = _json.loads(resp_text)
        except Exception:
            response_json = {}

        # 拆变量 web_ 前缀
        if save_to_vars and isinstance(response_json, dict):
            n_set = 0
            for k, v in response_json.items():
                try:
                    ctx.variables[f"web_{k}"] = v
                    n_set += 1
                except Exception:
                    pass
            if n_set:
                keys_sample = list(response_json.keys())[:8]
                ctx.log(f"[HttpRequest] 拆入 web_ 变量 {n_set} 个 keys={keys_sample}", "INFO")

        note = f"{method} {status_code} len={len(resp_text)}"
        annotate_step(ctx, node, f"http_{method.lower()}", note=note)
        ctx.log(f"[HttpRequest] {note} url={url[:80]}", "SUCCESS")
        return {"response_text": resp_text, "response_json": response_json, "status_code": status_code}, ["out_done"], ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        ctx.log(f"[HttpRequest] 顶层异常: {err}", "ERROR")
        annotate_step(ctx, node, "http_err", note=err)
        return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], err


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
    from src.blueprint.core.variable_engine import parse_batch_var_lines, VariableEngine

    engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
    set_count = 0
    first_err = ""
    note_parts: list = []

    # ---- 1) 新批量模式：batch_text 多行 name=value ----
    batch_raw = node.params.get("batch_text") or ""
    if isinstance(batch_raw, str) and batch_raw.strip():
        kv_map, err_msg = parse_batch_var_lines(batch_raw)
        if err_msg:
            first_err = err_msg.splitlines()[0]
            ctx.log(f"[Var] SetVariable 解析警告: {err_msg}", "WARN")
        # 按解析顺序 set，后写的可以引用前面同名变量（engine 内每一步更新 ctx.variables）
        for k, v in kv_map.items():
            # 插值：先过 VariableEngine，支持 {a} / ${a} / {= 1+2 }
            if isinstance(v, str) and engine.has_any_marker(v):
                interpolated = engine.interpolate(v)
            else:
                interpolated = v
            # 尝试自动数字解析（对 8080 → int，1.5 → float），避免都是字符串
            final_val = _auto_coerce(interpolated)
            ctx.set_var(k, final_val)
            engine.set(k, final_val)
            set_count += 1
            if len(note_parts) < 6:
                note_parts.append(f"{k}={repr(final_val)[:40]}")

    # ---- 2) 旧兼容模式：name + in_value 单变量（旧蓝图连线到 in_value 的情况）----
    legacy_name = str(node.params.get("name", "") or "").strip()
    legacy_value = inputs.get("in_value") if "in_value" in inputs else None
    if legacy_name and legacy_value is not None:
        ctx.set_var(legacy_name, legacy_value)
        set_count += 1
        note_parts.append(f"{legacy_name}={repr(legacy_value)[:40]}")

    if not set_count and legacy_name and "in_value" not in inputs and not batch_raw.strip():
        first_err = first_err or "SetVariable 未设置任何变量：batch_text 为空且 name 无 in_value 连线"

    if set_count:
        note = "SetVar(" + str(set_count) + ") " + "; ".join(note_parts)
        ctx.log(f"[Var] {note}", "DEBUG")
    else:
        note = "SetVar(0)"
        ctx.log(f"[Var] {note} {first_err}".rstrip(), "WARN" if first_err else "DEBUG")
    annotate_step(ctx, node, "var", note=note)
    return {}, ["out_exec"], first_err or ""


def execute_GetVariable(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    name = str(node.params.get("name", "") or "")
    default = node.params.get("default")
    value = ctx.get_var(name, default)
    return {"out_value": value}, [], ""


def _parse_varswitch_cases(raw: str) -> list:
    """解析 VarSwitch.cases 文本：优先多行 id=value；若以 [/{ 开头则 JSON 数组。返回 [{id,value}] 列表。"""
    raw = raw or ""
    if not raw.strip():
        return []
    # JSON 数组？
    head = raw.strip()[:2]
    if head in ("[{", "[\"", "['") or raw.strip().startswith("["):
        try:
            import json as _json

            arr = _json.loads(raw)
            if isinstance(arr, list):
                out = []
                for item in arr:
                    if isinstance(item, dict):
                        cid = str(item.get("id") or "").strip()
                        val = item.get("value")
                        if cid:
                            out.append({"id": cid, "value": val})
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        out.append({"id": str(item[0]).strip(), "value": item[1]})
                return out
        except Exception:
            pass
    # 行式 id=value
    from src.blueprint.core.variable_engine import parse_batch_var_lines

    kv, _err = parse_batch_var_lines(raw)
    return [{"id": k, "value": v} for k, v in kv.items()]


def _varswitch_match(a, b) -> bool:
    """宽松等于：数字自动 str↔int/float 比较，否则直接 ==。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        if a == b:
            return True
    except Exception:
        pass
    # 数字互转兜底
    try:
        af = float(a)
        bf = float(b)
        if abs(af - bf) < 1e-9:
            return True
    except Exception:
        pass
    return str(a).strip() == str(b).strip()


def execute_VarSwitch(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    from src.blueprint.core.variable_engine import VariableEngine

    engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)

    var_name = str(node.params.get("var_name", "") or "").strip()
    in_value = inputs.get("in_value")
    cases_raw = node.params.get("cases", "") or ""

    has_in_value = "in_value" in inputs  # True 表示上游连了 DATA 线（即使 None 也是上游明确给的 None）

    # ---------------- 简单模式：var_name 非空（或上游 in_value 连线 + var_expr 表达式） ----------------
    if var_name or has_in_value:
        # 比较对象：优先 in_value 连线；否则 engine 取 var_name；都没有 → 走 no / else
        compare_value = in_value if has_in_value else engine.get(var_name)
        # var_expr：可选，若填了就把 compare_value 代入 {var} 表达式再算布尔（写 {in_value} 或 {变量名}都行）
        expr = str(node.params.get("var_expr", "") or "")
        if expr.strip():
            # 临时把 in_value 塞进去便于表达式写 {in_value}
            tmp_inject = {"in_value": compare_value}
            for k, v in tmp_inject.items():
                engine.set(k, v)
            try:
                result_val = engine.interpolate(expr)
            finally:
                pass
            # result_val → bool
            cond = bool(result_val) if not isinstance(result_val, str) else (result_val.strip().lower() not in ("", "0", "false", "none", "null", "no"))
            port = "yes" if cond else "no"
            note = f"VarSwitch expr={cond} -> {port}"
            annotate_step(ctx, node, "switch", note=note)
            return {"out_value": compare_value}, [port], ""
        # 无表达式：按 compare_value 真值判断
        if isinstance(compare_value, str):
            cond = compare_value.strip().lower() not in ("", "0", "false", "none", "null", "no")
        else:
            cond = bool(compare_value)
        port = "yes" if cond else "no"
        note = f"VarSwitch {var_name or 'in_value'}={repr(compare_value)[:40]} -> {port}"
        ctx.log(f"[VarSwitch] {note}", "DEBUG")
        annotate_step(ctx, node, "switch", note=note)
        return {"out_value": compare_value}, [port], ""

    # ---------------- 多分支模式：cases 数组 ----------------
    cases = _parse_varswitch_cases(cases_raw if isinstance(cases_raw, str) else "")
    # 多分支下 in_value 必须有（或者 cases.value 已经是表达式？这里保守：in_value 连线有就用，没连线要求用户通过 var_name 传——但多分支模式 var_name 空，所以只能用 in_value 或 cases.value 里直接写插值表达式（引擎算完再比）
    base = in_value if has_in_value else None
    # 如果没 in_value，但 cases.value 里有 {var} 插值表达式：也能按每个case自己算
    matched_id = None
    for case in cases:
        cid = case.get("id")
        cval = case.get("value")
        # cval 允许写 {some_var}，这样每个 case 可以匹配不同变量（进阶用法）
        if isinstance(cval, str) and engine.has_any_marker(cval):
            cval = engine.interpolate(cval)
        cval_coerced = _auto_coerce(cval)
        # base 有值 vs cval 比较；base 没值时，判断 cval 真/假（类似 VarSwitch 多分支写"布尔匹配"：case_true=true, case_false=false）
        if has_in_value or base is not None:
            if _varswitch_match(base, cval_coerced):
                matched_id = cid
                break
        else:
            # 没基准值：把 cval 当布尔，真的第一个命中
            if isinstance(cval_coerced, str):
                truth = cval_coerced.strip().lower() not in ("", "0", "false", "none", "null", "no")
            else:
                truth = bool(cval_coerced)
            if truth:
                matched_id = cid
                break
    port = matched_id if matched_id else "else"
    note = f"VarSwitch multi -> {port}"
    ctx.log(f"[VarSwitch] {note}  base={repr(base)[:40]}", "DEBUG")
    annotate_step(ctx, node, "switch", note=note)
    return {"out_value": base}, [port], ""


def execute_Sequence(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    num_paths = max(1, int(node.params.get("num_paths", 3) or 3))
    interval = float(node.params.get("interval_sec", 0.0) or 0.0)

    graph = getattr(ctx, "_graph_ref", None)
    runner = getattr(ctx, "_runner_ref", None)
    if graph is None or runner is None:
        msg = "[Sequence] ctx 缺少 graph/runner 引用，无法同步walk分支（请检查BlueprintRunner入口是否注入）。"
        ctx.log(msg, "ERROR")
        annotate_step(ctx, node, "seq", note="FAIL: 缺少graph_ref/runner_ref")
        return {"out_done_path": None, "out_num_paths": num_paths}, ["else"], msg

    path_ok: Optional[int] = None
    last_reason = ""
    for path_i in range(1, num_paths + 1):
        if not ctx.is_running:
            break
        # 路径间间隔（第一条路径前不sleep）
        if path_i > 1 and interval > 0 and ctx.is_running:
            t0 = time.time()
            while ctx.is_running and (time.time() - t0) < interval:
                wait_if_paused(ctx)
                time.sleep(0.05)

        port_name = str(path_i)
        ctx.log(f"[Sequence] {node.id[:6]} 尝试路径 #{path_i}/{num_paths}  (出口端口={port_name})", "DEBUG")
        annotate_step(ctx, node, "seq", note=f"try #{path_i}/{num_paths} -> {port_name}")

        success, reason = _walk_branch_until_end(ctx, graph, runner, node.id, port_name)
        last_reason = reason
        if success:
            path_ok = path_i
            break
        else:
            ctx.log(f"[Sequence] 路径#{path_i} 失败：{reason}，继续尝试下一条。", "DEBUG")

    if path_ok is not None:
        note = f"短路成功：路径#{path_ok}/{num_paths} 命中 → out_done；原因：{last_reason}"
        ctx.log(f"[Sequence] {node.id[:6]} {note}", "INFO")
        annotate_step(ctx, node, "seq", note=f"✓短路#{path_ok}→out_done")
        return {"out_done_path": path_ok, "out_num_paths": num_paths}, ["out_done"], ""
    else:
        note = f"全部失败：{num_paths} 条路径无命中 → else；最后原因：{last_reason}"
        ctx.log(f"[Sequence] {node.id[:6]} {note}", "WARN")
        annotate_step(ctx, node, "seq", note=f"✗全失败→else；末因：{last_reason[:40]}")
        return {"out_done_path": None, "out_num_paths": num_paths}, ["else"], ""


def _win32_popup(title: str, message: str, level: str) -> int:
    """后台线程安全的 Windows 原生 MessageBox（不依赖 Qt UI 线程，蓝图运行线程可直接调）。"""
    try:
        import ctypes

        MB_OK = 0x00000000
        MB_ICONINFORMATION = 0x00000040
        MB_ICONWARNING = 0x00000030
        MB_ICONERROR = 0x00000010
        level = (level or "INFO").upper()
        icon = MB_ICONINFORMATION
        if level in ("WARN", "WARNING"):
            icon = MB_ICONWARNING
        elif level == "ERROR":
            icon = MB_ICONERROR
        return ctypes.windll.user32.MessageBoxW(0, str(message or ""), str(title or ""), MB_OK | icon)
    except Exception:
        return 0


def execute_Notify(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    from src.blueprint.core.variable_engine import VariableEngine

    engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)

    title_raw = str(node.params.get("title", "") or "")
    msg_raw = str(node.params.get("message", "") or "")
    level_raw = str(node.params.get("level", "INFO") or "INFO").upper()
    show_popup = bool(node.params.get("show_popup", False))

    # --- 第一阶段：普通插值（title/message 都走，但 message 先 disable ALL_VARS 留着最后一步替换） ---
    title = engine.interpolate(title_raw) if engine.has_any_marker(title_raw) else title_raw
    # message 先做插值，但 {ALL_VARS} 不替换
    msg_without_allvars = (
        engine.interpolate(msg_raw, disable_all_vars=True)
        if engine.has_any_marker(msg_raw)
        else msg_raw
    )
    # --- 第二阶段：message 单独替换 {ALL_VARS} / ${ALL_VARS}（最后一步，避免与其他变量占位冲突） ---
    all_vars_rendered = engine._render_all_vars() if isinstance(engine, VariableEngine) else VariableEngine(ctx.variables)._render_all_vars()

    def _replace_all_vars(text: str) -> str:
        if not isinstance(text, str):
            return text
        t = text.replace("${ALL_VARS}", all_vars_rendered)
        t = t.replace("{ALL_VARS}", all_vars_rendered)
        return t

    msg_final = _replace_all_vars(msg_without_allvars)
    title_final = _replace_all_vars(str(title) if not isinstance(title, str) else title)

    # 级别归一：INFO/WARN/ERROR/DEBUG（未知默认 INFO）
    if level_raw not in ("INFO", "WARN", "ERROR", "DEBUG"):
        level_final = "INFO"
    else:
        level_final = level_raw

    # 日志输出（truncate 过大的 msg，防止主窗口日志卡）
    log_body = str(msg_final)
    if len(log_body) > 4000:
        log_body = log_body[:4000] + "\n...（截断，完整内容看系统弹窗）"
    ctx.log(f"[{level_final}] {title_final}\n{log_body}" if log_body else f"[{level_final}] {title_final}", level_final)

    # 系统弹窗：show_popup=True 才弹；ERROR/WARN 级别默认强制弹？— 文档默认4参数里 show_popup 只是开关，不额外强制
    if show_popup:
        try:
            _win32_popup(str(title_final), str(msg_final), level_final)
        except Exception as e:
            ctx.log(f"[Notify] 弹窗失败: {e}", "WARN")

    annotate_step(ctx, node, "notify", note=f"Notify {level_final}: {str(title_final)[:60]}")
    return {}, ["out_exec"], ""


def execute_Note(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    return {}, [], ""


# ---------------- 检测：多图AND（if_img） ----------------
def execute_IfImageDetect(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    对齐 Qflow if_img：检查**所有**模板图是否**同时**出现在屏幕上。
    - 无 timeout（只查一次，与 FindImage 区分）
    - 全部命中 -> yes；缺任何一张 -> no
    - images_json 每项：{path, b64}，优先用 runtime_attrs["_pil_images"] 缓存
    """
    wait_if_paused(ctx)
    data = node.params
    raw = data.get("images_json", "[]")
    if isinstance(raw, str) and raw:
        try:
            imgs = json.loads(raw)
        except Exception:
            imgs = []
    elif isinstance(raw, list):
        imgs = raw
    else:
        imgs = []
    threshold = float(data.get("threshold", 0.9) or 0.9)
    strategy = str(data.get("match_strategy", "hybrid") or "hybrid")

    if not imgs:
        ctx.log("[IfImg] 没有配置参考图 -> no", "WARN")
        annotate_step(ctx, node, "find", note="ERROR: images 为空")
        return {}, ["no"], ""

    cached_pils: List[Optional[Image.Image]] = list(
        getattr(node, "runtime_attrs", {}).get("_pil_images", []) or []
    )
    while len(cached_pils) < len(imgs):
        cached_pils.append(None)

    tmp_paths: List[str] = []
    miss_name: Optional[str] = None
    hit_all = True

    try:
        for i, item in enumerate(imgs):
            wait_if_paused(ctx)
            if not ctx.is_running:
                break
            if not isinstance(item, dict):
                miss_name = f"[{i}]无效项"
                hit_all = False
                break
            path = item.get("path", "") or ""
            pil = cached_pils[i] if i < len(cached_pils) else None

            use_path = ""
            if isinstance(path, str) and path and os.path.exists(path):
                use_path = path
            elif pil is not None:
                fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ba_ifimg_")
                os.close(fd)
                try:
                    pil.save(tmp, format="PNG")
                    tmp_paths.append(tmp)
                    use_path = tmp
                except Exception:
                    pass

            if not use_path:
                miss_name = f"[{i}]无路径/无图"
                hit_all = False
                break

            pos = None
            try:
                if ctx.matcher is not None:
                    if strategy == "template":
                        pos = ctx.matcher.find_image(use_path, threshold=threshold, fast_mode=True)
                    elif strategy == "feature":
                        pos = ctx.matcher.find_image(use_path, threshold=threshold, fast_mode=False)
                    else:  # hybrid
                        pos = ctx.matcher.find_image_gray(use_path, threshold=threshold, fast_mode=True)
                        if pos is None:
                            pos = ctx.matcher.find_image(use_path, threshold=threshold, fast_mode=False)
            except Exception as e:
                ctx.log(f"[IfImg] 匹配异常[{i}]: {e}", "WARN")

            if pos is None:
                display = os.path.basename(str(path)) if path else f"image[{i}]"
                miss_name = display
                ctx.log(f"[IfImg] 缺少: {display} -> no", "DEBUG")
                hit_all = False
                break

        if hit_all and ctx.is_running:
            ctx.log(f"[IfImg] 全部{len(imgs)}张都存在 -> yes", "DEBUG")
            annotate_step(ctx, node, "find", note=f"ALL PRESENT x{len(imgs)}")
            return {}, ["yes"], ""
        else:
            note = f"MISS: {miss_name}" if miss_name else f"仅命中 {i}/{len(imgs)}"
            annotate_step(ctx, node, "find", note=note)
            return {}, ["no"], ""
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass


# ---------------- 检测：静止（if_static） ----------------
def execute_StillnessDetect(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    对齐 Qflow if_static：指定 ROI 内连续 N 帧像素差 < 阈值即判定静止。
    - roi: [x, y, w, h]
    - 超时 ms 内不满足 -> no；满足 -> yes
    - 暂停检查：循环开头 + sleep 内
    """
    wait_if_paused(ctx)
    p = node.params
    roi_raw = p.get("roi", "[0,0,0,0]")
    try:
        if isinstance(roi_raw, str) and roi_raw:
            roi = [int(v) for v in json.loads(roi_raw)]
        elif isinstance(roi_raw, (list, tuple)) and len(roi_raw) >= 4:
            roi = [int(v) for v in roi_raw[:4]]
        else:
            roi = [0, 0, 0, 0]
    except Exception:
        roi = [0, 0, 0, 0]
    if len(roi) < 4:
        roi += [0] * (4 - len(roi))
    x, y, w, h = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])

    dt = float(p.get("diff_threshold", 0.01) or 0.01)
    need_stable = max(1, int(p.get("stable_frames", 3) or 3))
    interval_ms = max(30, int(p.get("sample_interval_ms", 200) or 200))
    timeout_ms = max(0, int(p.get("timeout_ms", 5000) or 5000))

    if w <= 0 or h <= 0:
        ctx.log("[if_static] 未设置ROI，先点右侧按钮框选区域 -> no", "WARN")
        annotate_step(ctx, node, "static", note="ERROR: ROI empty")
        return {}, ["no"], ""

    prev_roi: Optional[np.ndarray] = None
    stable_cnt = 0
    t0 = time.time()
    timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else float("inf")
    interval_s = interval_ms / 1000.0
    last_diff = 0.0

    while (time.time() - t0) < timeout_s and ctx.is_running:
        wait_if_paused(ctx)
        if not ctx.is_running:
            break
        shot_bgr = None
        try:
            if ctx.screen is not None:
                shot_bgr = ctx.screen.capture_region_bgr((x, y, w, h))
        except Exception:
            shot_bgr = None
        if shot_bgr is None:
            break

        if prev_roi is not None:
            diff = float(
                np.mean(np.abs(shot_bgr.astype(np.int16) - prev_roi.astype(np.int16)) / 255.0)
            )
            last_diff = diff
            if diff < dt:
                stable_cnt += 1
                if stable_cnt >= need_stable:
                    ctx.log(
                        f"[if_static] 达到静止 stable_cnt={stable_cnt}, diff={diff:.4f} < {dt:.4f} -> yes",
                        "DEBUG",
                    )
                    annotate_step(
                        ctx, node, "static",
                        note=f"OK stable={stable_cnt} diff={diff:.3f}",
                        region=(x, y, w, h),
                    )
                    return {}, ["yes"], ""
            else:
                stable_cnt = 0
                annotate_step(
                    ctx, node, "moving",
                    note=f"diff={diff:.4f} >= {dt:.4f}",
                    region=(x, y, w, h),
                )
        prev_roi = shot_bgr

        t_sleep = time.time()
        while (time.time() - t_sleep) < interval_s and ctx.is_running:
            wait_if_paused(ctx)
            time.sleep(min(0.05, max(0.005, interval_s - (time.time() - t_sleep))))

    ctx.log(
        f"[if_static] 超时 {timeout_ms}ms 仍未静止 (last_diff={last_diff:.4f}, stable={stable_cnt}/{need_stable}) -> no",
        "DEBUG",
    )
    annotate_step(
        ctx, node, "static",
        note=f"TIMEOUT last_diff={last_diff:.3f} stable={stable_cnt}",
        region=(x, y, w, h),
    )
    return {}, ["no"], ""


# ---------------- 检测：声音 if_sound ----------------
def _capture_sound_blocking(device: str, duration_sec: float, sr: int = 44100):
    """采集一段系统音频，失败返回 None。
    首选 soundcard（支持 default_loopback 录系统扬声器自己播放的声音），
    不可用回退 PyAudio（麦克风为主）。
    """
    import numpy as np
    # 1) soundcard 首选（WASAPI loopback 稳定）
    try:
        import soundcard as sc
        if device == "default_loopback":
            spk = sc.default_speaker()
            loopback = spk.loopback
        else:
            # 按ID或名查找，包含 loopback 设备
            loopback = sc.get_microphone(id=device, include_loopback=True)
        with loopback.recorder(samplerate=sr) as rec:
            data = rec.record(numframes=int(sr * max(0.01, duration_sec)))
        if data.ndim == 2:
            data = data.mean(axis=1)
        return np.asarray(data, dtype=np.float32)
    except Exception:
        pass
    # 2) PyAudio 兜底
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        stream = p.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=sr,
            input=True,
            frames_per_buffer=1024,
        )
        frames: List[Any] = []
        total = int(sr * max(0.01, duration_sec))
        chunk = 1024
        while len(frames) * chunk < total:
            try:
                raw = stream.read(chunk, exception_on_overflow=False)
                frames.append(np.frombuffer(raw, dtype=np.float32))
            except Exception:
                break
        try:
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception:
            pass
        if frames:
            return np.concatenate(frames)
    except Exception:
        return None
    return None


def _ascii_waveform(data, peak_marker_db: float = -30.0, width: int = 60, height: int = 7) -> str:
    """用ASCII字符画绘制音频波形，便于debug_visual日志输出。"""
    import numpy as np
    try:
        arr = np.asarray(data, dtype=np.float32)
        if arr.size == 0:
            return "[waveform] (空)"
        # 降采样到 width 个点
        step = max(1, arr.size // width)
        ds = arr[: step * width].reshape(-1, step)
        env_max = np.max(np.abs(ds), axis=1)
        env_max_db = 20 * np.log10(env_max + 1e-8)
        # 刻度：从 height-1（顶部=0dB）到 0（底部=-90dB）
        thr_line = int(np.clip(height - 1 - (peak_marker_db / (-90.0)) * (height - 1), 0, height - 1))
        lines: List[str] = []
        for row in range(height - 1, -1, -1):
            db_at_row = -((height - 1 - row) / max(1, height - 1)) * 90.0
            prefix = f"{db_at_row:>5.0f}dB "
            chars = []
            for v in env_max_db:
                row_has = (height - 1 - int(np.clip((-v / 90.0) * (height - 1), 0, height - 1))) <= row
                mark = "│"
                if row == thr_line:
                    mark = "·" if not row_has else "┿"
                if row_has:
                    chars.append("█" if mark != "┿" else "▣")
                else:
                    chars.append(mark)
            lines.append(prefix + "".join(chars))
        thr_annot = " " * 6 + "-" * thr_line + f" ▲阈值({peak_marker_db:.0f}dB)"
        return "\n".join(lines + [thr_annot])
    except Exception as e:
        return f"[waveform render err: {e}]"


def execute_SoundDetect(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    对齐 Qflow if_sound：系统播放/麦克风声音超阈值判定。
    - 三种模式：amplitude_max / amplitude_mean / spectral_band
    - 失败兜底：soundcard → PyAudio，都不可用 WARN 走 no
    """
    import numpy as np

    wait_if_paused(ctx)
    p = node.params
    mode = str(p.get("judge_mode", "amplitude_max") or "amplitude_max")
    thr_db = float(p.get("threshold_db", -30.0) or -30.0)
    sample_ms = max(50, int(p.get("sample_ms", 500) or 500))
    timeout_ms = max(0, int(p.get("timeout_ms", 5000) or 5000))
    device = str(p.get("device", "default_loopback") or "default_loopback")
    band_low = int(p.get("spectral_band_low_hz", 500) or 500)
    band_high = int(p.get("spectral_band_high_hz", 2000) or 2000)
    debug_viz = bool(p.get("debug_visual", False))
    sr = 44100

    t0 = time.time()
    timeout_cap_s = (timeout_ms / 1000.0) if timeout_ms > 0 else 86400.0 * 365
    sample_s = sample_ms / 1000.0
    note = ""
    while (time.time() - t0) < timeout_cap_s and ctx.is_running:
        wait_if_paused(ctx)
        chunk = _capture_sound_blocking(device, sample_s, sr)
        if chunk is None:
            ctx.log(
                "[if_sound] 音频采集库未安装或设备不可用，自动走 no。请 pip install soundcard PyAudio。"
                " (PyAudio 安装失败可尝试：pip install pipwin && pipwin install pyaudio)",
                "WARN",
            )
            annotate_step(ctx, node, "sound", note="采集库不可用→no")
            return {"out_level_db": None}, ["no"], ""

        yes = False
        if mode == "amplitude_max":
            peak = float(np.max(np.abs(chunk)) + 1e-8)
            peak_db = 20.0 * np.log10(peak)
            yes = peak_db >= thr_db
            note = f"peak={peak_db:.1f}dB thr={thr_db:.0f}dB"
        elif mode == "amplitude_mean":
            mean = float(np.mean(np.abs(chunk)) + 1e-8)
            mean_db = 20.0 * np.log10(mean)
            yes = mean_db >= thr_db
            note = f"mean={mean_db:.1f}dB thr={thr_db:.0f}dB"
        else:  # spectral_band
            n = len(chunk)
            if n < 4:
                yes = False
                note = "chunk过短→no"
            else:
                win = np.hanning(n).astype(np.float32)
                spec = np.abs(np.fft.rfft(chunk * win))
                freqs = np.fft.rfftfreq(n, 1.0 / sr)
                low, high = sorted([band_low, band_high])
                mask = (freqs >= low) & (freqs <= high)
                if not np.any(mask):
                    band_energy = 0.0
                else:
                    band_energy = float(np.mean(spec[mask]) + 1e-8)
                # thr_amp：阈值dB转振幅基准；经验公式：band_energy >= 10^(thr_db/20) * k
                thr_amp = (10.0 ** (thr_db / 20.0)) * 0.05
                yes = band_energy >= thr_amp
                note = f"band[{low}-{high}Hz]energy={band_energy:.4f}(thr={thr_amp:.4f})"
        if debug_viz:
            wave = _ascii_waveform(chunk, peak_marker_db=thr_db)
            ctx.log(f"[if_sound waveform]\n{wave}\n判定={note} yes={yes}", "DEBUG")
        if yes:
            ctx.log(f"[if_sound] ✅ 检测到声音：{note}", "DEBUG")
            annotate_step(ctx, node, "sound", note=f"YES {note}")
            out_db = float(thr_db)  # 简化：输出阈值供下游参考
            try:
                if mode == "amplitude_max":
                    out_db = float(20.0 * np.log10(float(np.max(np.abs(chunk)) + 1e-8)))
                elif mode == "amplitude_mean":
                    out_db = float(20.0 * np.log10(float(np.mean(np.abs(chunk)) + 1e-8)))
            except Exception:
                pass
            return {"out_level_db": round(out_db, 2)}, ["yes"], ""
        # 轮询间隔
        t_slp = time.time()
        while (time.time() - t_slp) < 0.05 and ctx.is_running:
            wait_if_paused(ctx)
            time.sleep(0.01)

    # 超时
    ctx.log(f"[if_sound] ⏱ 超时 {timeout_ms}ms，未检测到超阈值声音 → no（last={note}）", "DEBUG")
    annotate_step(ctx, node, "sound", note=f"TIMEOUT({timeout_ms}ms) last={note[:50]}")
    return {"out_level_db": None}, ["no"], ""


# ============================================================
# 节点类型 -> 执行函数 映射
# ============================================================
EXECUTORS = {
    "Start": execute_Start,
    "End": execute_End,
    "Branch": execute_Branch,
    "Loop": execute_Loop,
    "Wait": execute_Wait,
    "VarSwitch": execute_VarSwitch,
    "Sequence": execute_Sequence,
    "Notify": execute_Notify,
    "OpenApp": execute_OpenApp,
    "BindWindow": execute_BindWindow,
    "FindImage": execute_FindImage,
    "FindAnyImage": execute_FindAnyImage,
    "WaitAnyImage": execute_WaitAnyImage,
    "Click": execute_Click,
    "Mouse": execute_Mouse,
    "PressKey": execute_PressKey,
    "RegionLiteral": execute_RegionLiteral,
    "TemplatePicker": execute_TemplatePicker,
    "EdgeCounter": execute_EdgeCounter,
    "SetVariable": execute_SetVariable,
    "GetVariable": execute_GetVariable,
    "Note": execute_Note,
    "IfImageDetect": execute_IfImageDetect,
    "StillnessDetect": execute_StillnessDetect,
    "SoundDetect": execute_SoundDetect,
    "Clipboard": execute_Clipboard,
    "Command": execute_Command,
    "Web": execute_Web,
    "WebBrowser": execute_WebBrowser,
    "HttpRequest": execute_HttpRequest,
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
        # Sequence 短路OR分支需要：在 ctx 中注入 graph/runner 引用，供 execute_Sequence 同步walk子分支
        ctx._graph_ref = graph
        ctx._runner_ref = self
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
