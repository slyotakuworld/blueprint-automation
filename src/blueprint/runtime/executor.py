import json
import os
import tempfile
import time
import traceback
import contextlib
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
from .input_actions import InputActions
from .screen_capture import ScreenCapture
from .template_matcher import TemplateMatcher
from ...config.app_paths import debug_dir


# ============================================================
# 辅助：目标窗口(HWND) 应用到运行时所有服务
# ============================================================
def _apply_target_hwnd(ctx: ExecutionContext, hwnd, res_w=1920, res_h=1080):
    """
    把一个 HWND（目标窗口程序）应用到运行时所有下游服务和变量。
    这就是「设置目标程序」的统一逻辑：可在蓝图启动时调用（初始下拉框选中的目标），
    也可在 BindWindow 节点执行时调用（中途切换目标程序）。

    参数:
      hwnd   - 窗口句柄(int) 或 None（None=回退为全屏模式）
      res_w / res_h - 分辨率参考，用于坐标归一化（0=使用窗口实际尺寸）
    """
    import win32gui  # type: ignore

    # 1. 激活到前台（仅当 hwnd 有效时）
    if hwnd:
        try:
            from src.tools.window_utils import bring_window_to_front

            bring_window_to_front(hwnd, log_fn=ctx.log)
        except Exception:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass

    # 2. 读实际窗口尺寸 + 标题
    win_rect = None
    win_title = ""
    if hwnd:
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            win_rect = (l, t, r - l, b - t)
            win_title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            win_rect = None
            win_title = ""
        # 分辨率参考：0=自动用窗口实际尺寸
        try:
            if int(res_w or 0) <= 0 and win_rect:
                res_w = win_rect[2]
            if int(res_h or 0) <= 0 and win_rect:
                res_h = win_rect[3]
        except Exception:
            pass

    # 3. 应用到 ScreenCapture（目标句柄）
    if ctx.screen is not None:
        try:
            ctx.screen.hwnd = hwnd
        except Exception:
            pass

    # 4. 应用到 TemplateMatcher（搜索 ROI + 分辨率参考）
    if ctx.matcher is not None:
        try:
            ctx.matcher.config["resolution_ref"] = (int(res_w or 1920), int(res_h or 1080))
        except Exception:
            pass
        if win_rect is not None:
            try:
                ctx.matcher.set_fullscreen_region(win_rect)
            except Exception:
                pass
        else:
            # None=全屏：删除「全界面」区域，让 matcher 回退全屏
            try:
                if "全界面" in getattr(ctx.matcher, "regions", {}):
                    ctx.matcher.regions.pop("全界面", None)
            except Exception:
                pass

    # 5. 应用到 InputActions（安全点击区域）
    if ctx.input_actions is not None:
        try:
            ctx.input_actions.set_safe_region(win_rect)
        except Exception:
            pass

    # 6. 保存到 ctx 上的全局变量（VarEngine / 下游逻辑直接用）
    target_info = {
        "hwnd": hwnd,
        "title": win_title,
        "rect": win_rect,
        "resolution_ref": (int(res_w or 1920), int(res_h or 1080)),
    }
    try:
        ctx.set_var("target", target_info)
        ctx.set_var("target_hwnd", hwnd)
        ctx.set_var("target_title", win_title)
        if win_rect:
            ctx.set_var("target_x", win_rect[0])
            ctx.set_var("target_y", win_rect[1])
            ctx.set_var("target_w", win_rect[2])
            ctx.set_var("target_h", win_rect[3])
        else:
            for k in ("target_x", "target_y", "target_w", "target_h"):
                try:
                    ctx.variables.pop(k, None)
                except Exception:
                    pass
    except Exception:
        pass

    return win_rect, win_title


# ============================================================
# 辅助：端口/连线 解析
# ============================================================
def _resolve_data_inputs(graph: BlueprintGraph, ctx: ExecutionContext, node: Node) -> Dict[str, Any]:
    """
    解析节点所有 DATA 输入端口的值：
      - 若端口被连线：取上游 last_outputs（注意：同一INPUT端口多根线只会取遍历顺序第一条，其余静默忽略并打WARN）
      - 否则：取 node.params 同名值
    """
    resolved: Dict[str, Any] = {}
    for p in node.inputs:
        if p.type != PortType.DATA:
            continue
        # 收集所有指向该输入端口的边（如果>=2条，说明有冲突）
        all_edges: List[Any] = []
        for e in graph.edges.values():
            if e.to_node == node.id and e.to_port == p.name:
                all_edges.append(e)
        if all_edges:
            if len(all_edges) > 1:
                ctx.log(
                    f"[WARN] 输入端口「{node.type}.{p.name}」连了 {len(all_edges)} 根线！"
                    f"INPUT 端口只允许一根线，运行时只会保留边「{all_edges[0].id}」生效，"
                    f"其余 {len(all_edges)-1} 根（{', '.join(e.id for e in all_edges[1:])}）会被静默丢弃。"
                    f"请手动断开多余的线。",
                    "WARN",
                )
            found_edge = all_edges[0]
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
FAIL_SEMANTIC = {"fail", "timeout", "not_found", "no", "err", "abort", "skipped", "cancelled", "else"}
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

        # UI 高亮 + 进度（复用 runner 信号） + 记录当前执行节点ID + 发 current_node_changed
        try:
            runner.highlight_node.emit(curr_id)
            runner.progress_step.emit(ctx.step_counter + 1, node.type)
        except Exception:
            pass
        try:
            ctx.current_node_id = curr_id
            runner.current_node_changed.emit(curr_id, node.type)
        except Exception:
            pass

        # 1) DATA 输入解析
        inputs = _resolve_data_inputs(graph, ctx, node)
        ctx.step_counter += 1

        # 2) 执行（复用 EXECUTORS）
        ctx.log(f"[Node▶] {node.type}  id={curr_id[:8]} [seq分支]", "INFO")
        # --- 断点命中检查（Sequence 分支入口）---
        _hit_node_breakpoint(ctx, node, runner)
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

        if not ctx.is_running:
            return False, "运行被用户停止"

        # 3) 保存输出
        ctx.set_outputs(curr_id, outputs or {})
        ctx.last_progress_ts = time.time()

        # 4) 下一步判定：先找下游，再根据「是否有下游 + 是否 FAIL_SEMANTIC」判断
        if not next_ports:
            # 无 next_ports：到达执行流终点 → 成功（流程跑完）
            return True, f"分支#{branch_out_port} 执行完成到达终点"
        pname = next_ports[0]

        # 5) 找下游连线（先找，再根据有没有下游决定成败）
        outs = _exec_out_edges(graph, curr_id, pname)
        if not outs:
            # 该端口无下游 → 到达执行流终点：根据端口名判定
            if pname in FAIL_SEMANTIC:
                return False, f"节点 {node.type} 终结于失败端口 {pname}（无下游，分支终止于失败状态）"
            else:
                return True, f"到达执行终点（{node.type}.{pname}无下游）"
        # 有下游连线：
        if pname in FAIL_SEMANTIC:
            # 关键修复：虽然当前端口是 FAIL_SEMANTIC，但用户明确接了下游
            # （典型场景：cmd 故意触发 err/timeout，后面接「断言成功 Notify」）
            # → 不立刻判负，继续沿下游 walk 直到真正的终点，最后按终点状态判定
            ctx.log(
                f"[Sequence-Walk] {node.type}.{pname}（FAIL_SEMANTIC端口）有下游连线，继续遍历不提前判负",
                "DEBUG",
            )
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


def execute_End(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """蓝图终点：到达此处视为分支结束，不再向下流转出节点。"""
    return {}, [], "结束"


def execute_Loop(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    infinite = bool(node.params.get("infinite", False))
    max_it = int(node.params.get("max_iterations", 5) or 5)
    interval = float(node.params.get("interval_sec", 1.0) or 1.0)
    counter = ctx.loop_counters.get(node.id, 0)
    if max_it <= 0:
        max_it = 5
    if infinite:
        # 无限循环：不受 max_iterations 限制
        ctx.loop_counters[node.id] = counter + 1
        if interval > 0 and ctx.is_running:
            t0 = time.time()
            while ctx.is_running and (time.time() - t0) < interval:
                wait_if_paused(ctx)
                time.sleep(0.05)
        wait_if_paused(ctx)
        note = f"Loop ∞ i={counter+1} -> loop_body"
        annotate_step(ctx, node, "loop", note=note)
        ctx.log(f"[Loop] {node.id[:6]}  ∞ i={counter+1} -> loop_body", "DEBUG")
        return (
            {"out_iteration": counter + 1, "out_max": -1},
            ["loop_body"],
            "",
        )
    elif counter < max_it:
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
    """双模式延时：固定延时 / 随机延时。优先走 in_seconds 数据线。"""
    import random
    p = dict(node.params or {})
    delay_type = str(p.get("delay_type", "fixed") or "fixed")

    # 优先用 in_seconds 连线值
    in_sec = inputs.get("in_seconds")
    if in_sec is not None:
        sec = max(0.0, min(float(in_sec), 3600.0))
        mode_txt = "连线"
    elif delay_type == "random":
        min_s = max(0.0, float(p.get("min_seconds", 1.0) or 1.0))
        max_s = max(min_s, float(p.get("max_seconds", 3.0) or 3.0))
        sec = random.uniform(min_s, max_s)
        mode_txt = f"随机[{min_s:g}~{max_s:g}]"
    else:
        sec = max(0.0, min(float(p.get("seconds", 1.0) or 1.0), 3600.0))
        mode_txt = "固定"

    note = f"{mode_txt} 等待 {sec:.3f}s"
    ctx.log(f"[Wait] {mode_txt} sleep {sec:.3f}s", "INFO")
    t0 = time.time()
    while ctx.is_running and (time.time() - t0) < sec:
        wait_if_paused(ctx)
        time.sleep(0.05)
    wait_if_paused(ctx)
    annotate_step(ctx, node, "wait", note=note)
    return {"out_waited_sec": round(sec, 4)}, ["out_exec"], ""


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
    exe_name: str,
    class_name: str,
    prefer_visible: bool = True,
) -> Optional[int]:
    """三维搜索：title子串 / exe文件名(小写含) / class_name 精确等于（不区分大小写）。
    三个维度全部启用：仅当对应字段非空时才参与匹配评分。
    """
    title_s = str(title or "").strip()
    exe_s = str(exe_name or "").strip().lower()
    cls_s = str(class_name or "").strip()
    has_any_keyword = bool(title_s) or bool(exe_s) or bool(cls_s)
    if not has_any_keyword:
        return None

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
        # title（非空才参与匹配）
        if title_s:
            dim_checked += 1
            if title_s.lower() in (w_title or "").lower():
                score += 10
                dim_ok += 1
            else:
                score -= 1000
        # exe_name（非空才参与匹配）
        if exe_s:
            dim_checked += 1
            w_exe = _pid_to_exe_name(w_pid) if w_pid else ""
            if w_exe and (exe_s in w_exe or w_exe.endswith(exe_s)):
                score += 10
                dim_ok += 1
            else:
                score -= 1000
        # class_name（非空才参与匹配）
        if cls_s:
            dim_checked += 1
            if (w_cls or "").lower() == cls_s.lower():
                score += 10
                dim_ok += 1
            else:
                score -= 1000
        if dim_checked == 0:
            continue
        # 维度必须全满足（只要用户填了对应值的维度必须命中）
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
    exe_name = str(node.params.get("exe_name", "") or "")
    class_name = str(node.params.get("class_name", "") or "")
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
        title=title,
        exe_name=exe_name,
        class_name=class_name,
    )
    if hwnd is None:
        msg = (
            f"[BindWindow] 未找到匹配窗口。"
            f"title='{title}'  exe='{exe_name}'  class='{class_name}'"
        )
        ctx.log(msg, "WARN")
        annotate_step(ctx, node, "bind_win", note="FAIL 未找到窗口")
        return {}, ["fail"], msg

    # ---------- 用统一函数应用目标窗口：切换目标程序 ----------
    win_rect, win_title = _apply_target_hwnd(ctx, hwnd, res_w, res_h)

    eff_w = (res_w if int(res_w or 0) > 0 else (win_rect[2] if win_rect else 1920))
    eff_h = (res_h if int(res_h or 0) > 0 else (win_rect[3] if win_rect else 1080))
    note_parts = [f"HWND={hwnd}", f"res_ref={eff_w}x{eff_h}"]
    if win_title:
        note_parts.append(f"title='{win_title[:24]}'")
    if win_rect:
        note_parts.append(f"rect={win_rect}")
    note = "BindWindow (切换目标程序) " + "  ".join(note_parts)
    ctx.log(f"[BindWindow] ✅ {note}  后续截图/找图/点击均自动对准此窗口。", "SUCCESS")
    annotate_step(ctx, node, "bind_win", note=note)
    return {}, ["success"], ""


# ---------------- 特征识别辅助 ----------------
def _findimage_feature_match(ctx, params: Dict[str, Any], region,
                             timeout_s: float, auto_scroll: bool, scroll_dy: int,
                             base_threshold: float, offset_x: int, offset_y: int,
                             strategy: str = "template", template_pos: dict = None):
    """特征识别辅助匹配。

    验证模式（template_pos 不为 None）：模板已找到，在模板命中位置附近的小区域内
    逐一验证特征是否存在。避免全屏搜索小图导致的误匹配。

    兜底模式（template_pos 为 None）：模板未找到，全屏搜索特征并交叉验证位置，
    尝试通过特征反推模板位置。

    返回 (abs_cx, abs_cy, avg_score) 或 None。
    """
    import json as _json
    import math as _math
    import base64 as _b64
    import tempfile as _tf

    raw = params.get("features_json", "[]")
    try:
        features = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        features = []
    if not features or len(features) < 1:
        ctx.log("[FindImage·特征] 特征列表为空，跳过特征匹配", "WARN")
        return None

    tolerance = max(1, int(params.get("feature_tolerance", 15) or 15))
    min_hits = max(1, int(params.get("feature_min_hits", 2) or 2))
    template_w = int(params.get("feature_template_w", 0) or 0)
    template_h = int(params.get("feature_template_h", 0) or 0)

    # 解码特征图到临时文件
    feat_paths = []
    feat_info = []  # [(rel_x, rel_y, w, h)]
    cleanup_paths = []
    try:
        for feat in features:
            b64 = feat.get("b64", "")
            rx = int(feat.get("rel_x", 0) or 0)
            ry = int(feat.get("rel_y", 0) or 0)
            fw = int(feat.get("w", 0) or 0)
            fh = int(feat.get("h", 0) or 0)
            if not b64:
                feat_paths.append(None)
                feat_info.append((rx, ry, fw, fh))
                continue
            try:
                img_data = _b64.b64decode(b64)
                fd, fpath = _tf.mkstemp(suffix=".png", prefix="ba_feat_")
                os.close(fd)
                with open(fpath, "wb") as f:
                    f.write(img_data)
                feat_paths.append(fpath)
                cleanup_paths.append(fpath)
                feat_info.append((rx, ry, fw, fh))
            except Exception:
                feat_paths.append(None)
                feat_info.append((rx, ry, fw, fh))
    except Exception as e:
        ctx.log(f"[FindImage·特征] 解析特征列表失败: {e}", "WARN")
        return None

    valid_count = sum(1 for p in feat_paths if p is not None)
    ctx.log(f"[FindImage·特征] 共{len(features)}个特征，有效{valid_count}个，容差={tolerance}px，最少命中={min_hits}", "DEBUG")

    # 阈值计算：验证模式用与主策略相同的阈值（不降低），兜底模式适当降低
    if strategy == "gray" and ctx.matcher is not None and hasattr(ctx.matcher, "get_calibrated_gray_threshold"):
        calibrated = ctx.matcher.get_calibrated_gray_threshold(base_threshold)
        if template_pos is not None:
            # 验证模式：小窗口搜索，不需要降低阈值
            feat_threshold = max(0.55, min(0.97, float(calibrated)))
        else:
            # 兜底模式：全屏搜索小图，适当降低阈值
            feat_threshold = max(0.55, min(0.97, float(calibrated) - 0.1))
    else:
        if template_pos is not None:
            feat_threshold = max(0.5, float(base_threshold))
        else:
            feat_threshold = max(0.5, float(base_threshold) - 0.1)

    def _cleanup():
        for cp in cleanup_paths:
            try:
                os.unlink(cp)
            except Exception:
                pass

    def _match_one(fpath, search_region):
        """在指定区域内匹配单个特征，返回 pos dict 或 None。"""
        try:
            if strategy == "gray":
                return ctx.matcher.find_image_gray(fpath, region=search_region, threshold=feat_threshold, fast_mode=True)
            else:
                return ctx.matcher.find_image(fpath, region=search_region, threshold=feat_threshold, fast_mode=True)
        except Exception:
            return None

    # ================================================================
    # 验证模式：模板已找到，在模板命中位置附近验证特征
    # ================================================================
    if template_pos is not None and isinstance(template_pos, dict):
        tpl_left = int(template_pos.get("left", 0))
        tpl_top = int(template_pos.get("top", 0))
        tpl_w = int(template_pos.get("width", 0))
        tpl_h = int(template_pos.get("height", 0))

        # 从模板匹配结果推算实际缩放比例
        if template_w > 0 and tpl_w > 0:
            scale = tpl_w / template_w
        else:
            scale = 1.0

        ctx.log(f"[FindImage·特征] 验证模式：模板位置=({tpl_left},{tpl_top},{tpl_w},{tpl_h}) scale={scale:.3f}", "DEBUG")

        verified = 0
        total = 0
        scores = []
        for i, fpath in enumerate(feat_paths):
            if fpath is None:
                continue
            total += 1
            rx, ry, fw, fh = feat_info[i]
            # 特征在屏幕上的期望位置（考虑缩放）
            exp_x = tpl_left + int(rx * scale)
            exp_y = tpl_top + int(ry * scale)
            # 在期望位置附近开一个小窗口搜索（容差的2倍，最少30px）
            search_pad = max(tolerance * 2, 30)
            search_w = int(fw * scale) + search_pad * 2
            search_h = int(fh * scale) + search_pad * 2
            search_region = (exp_x - search_pad, exp_y - search_pad, search_w, search_h)

            fpos = _match_one(fpath, search_region)
            if fpos is not None:
                verified += 1
                s = float(fpos.get("score", feat_threshold)) if isinstance(fpos, dict) else feat_threshold
                scores.append(s)
                ctx.log(f"[FindImage·特征] 特征{i}验证通过 (期望=({exp_x},{exp_y}) score={s:.3f})", "DEBUG")
            else:
                ctx.log(f"[FindImage·特征] 特征{i}验证失败 (期望=({exp_x},{exp_y}) 区域={search_region})", "DEBUG")

        if verified >= min_hits:
            # 验证通过，返回模板中心（已知位置）
            abs_cx = tpl_left + tpl_w // 2 + offset_x
            abs_cy = tpl_top + tpl_h // 2 + offset_y
            avg_score = sum(scores) / len(scores) if scores else 1.0
            ctx.log(f"[FindImage·特征] 验证成功！{verified}/{total}个特征通过，模板中心=({abs_cx},{abs_cy})", "SUCCESS")
            _cleanup()
            return (abs_cx, abs_cy, avg_score)
        else:
            ctx.log(f"[FindImage·特征] 验证失败：{verified}/{total}个特征通过 < 最低{min_hits}", "DEBUG")
            _cleanup()
            return None

    # ================================================================
    # 兜底模式：模板未找到，全屏搜索特征 + 交叉验证
    # ================================================================
    t0 = time.time()
    while (time.time() - t0) < timeout_s and ctx.is_running:
        wait_if_paused(ctx)
        if not ctx.is_running:
            break

        # 1) 对每个特征在全屏（或指定 region）内独立匹配
        hits = []  # [(feat_idx, screen_cx, screen_cy, score)]
        for i, fpath in enumerate(feat_paths):
            if fpath is None:
                continue
            fpos = _match_one(fpath, region)
            if fpos is not None:
                if isinstance(fpos, dict):
                    fl = int(fpos.get("left", 0)); ft = int(fpos.get("top", 0))
                    fwi = int(fpos.get("width", fw)); fhi = int(fpos.get("height", fh))
                    raw_s = fpos.get("score")
                    fscore = float(raw_s) if isinstance(raw_s, (int, float)) else float(feat_threshold)
                elif isinstance(fpos, (list, tuple)) and len(fpos) >= 4:
                    fl, ft, fwi, fhi = int(fpos[0]), int(fpos[1]), int(fpos[2]), int(fpos[3])
                    fscore = float(feat_threshold)
                elif isinstance(fpos, (list, tuple)) and len(fpos) >= 2:
                    fl, ft, fwi, fhi = int(fpos[0]), int(fpos[1]), fw, fh
                    fscore = float(feat_threshold)
                else:
                    continue
                scx = fl + fwi // 2
                scy = ft + fhi // 2
                hits.append((i, scx, scy, fscore))

        if len(hits) < min(2, min_hits):
            if auto_scroll and ctx.is_running and (time.time() - t0) < timeout_s:
                try:
                    import pyautogui
                    if region is not None:
                        pyautogui.moveTo(int(region[0]) + int(region[2]) // 2, int(region[1]) + int(region[3]) // 2)
                    if scroll_dy != 0:
                        pyautogui.scroll(scroll_dy)
                except Exception:
                    pass
                t_wait = time.time()
                while (time.time() - t_wait) < 0.5 and ctx.is_running:
                    wait_if_paused(ctx)
                    time.sleep(0.05)
                continue
            time.sleep(0.15)
            continue

        # 2) 计算缩放比例（自校准）
        scales = []
        for a in range(len(hits)):
            for b in range(a + 1, len(hits)):
                idx_a, sxa, sya, _ = hits[a]
                idx_b, sxb, syb, _ = hits[b]
                rxa, rya, _, _ = feat_info[idx_a]
                rxb, ryb, _, _ = feat_info[idx_b]
                dist_t = _math.sqrt((rxb - rxa) ** 2 + (ryb - rya) ** 2)
                dist_s = _math.sqrt((sxb - sxa) ** 2 + (syb - sya) ** 2)
                if dist_t > 5:
                    scales.append(dist_s / dist_t)

        if scales:
            scale = sum(scales) / len(scales)
        else:
            cap_w = int(params.get("feature_capture_width", 0) or 0)
            cur_w = 0
            if region is not None and len(region) >= 3:
                cur_w = int(region[2])
            if cur_w <= 0:
                try:
                    target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
                    if isinstance(target, dict):
                        r = target.get("rect")
                        if isinstance(r, (tuple, list)) and len(r) >= 3:
                            cur_w = int(r[2])
                except Exception:
                    pass
            if cap_w > 0 and cur_w > 0:
                scale = cur_w / cap_w
            else:
                scale = 1.0

        ctx.log(f"[FindImage·特征] 命中{len(hits)}个特征，scale={scale:.3f}（{len(scales)}对校准）", "DEBUG")

        # 3) 位置验证：以第一个命中为基准，验证其他特征
        base_idx, base_sx, base_sy, _ = hits[0]
        base_rx, base_ry, _, _ = feat_info[base_idx]
        valid_hits = [hits[0]]
        for hit in hits[1:]:
            idx, sx, sy, sc = hit
            rx, ry, _, _ = feat_info[idx]
            exp_x = base_sx + (rx - base_rx) * scale
            exp_y = base_sy + (ry - base_ry) * scale
            err = _math.sqrt((sx - exp_x) ** 2 + (sy - exp_y) ** 2)
            if err <= tolerance:
                valid_hits.append(hit)
            else:
                ctx.log(f"[FindImage·特征] 特征{idx}位置偏差{err:.0f}px > {tolerance}px，丢弃", "DEBUG")

        if len(valid_hits) < min_hits:
            ctx.log(f"[FindImage·特征] 有效命中{len(valid_hits)} < 最低{min_hits}，继续重试", "DEBUG")
            if auto_scroll and ctx.is_running and (time.time() - t0) < timeout_s:
                try:
                    import pyautogui
                    if region is not None:
                        pyautogui.moveTo(int(region[0]) + int(region[2]) // 2, int(region[1]) + int(region[3]) // 2)
                    if scroll_dy != 0:
                        pyautogui.scroll(scroll_dy)
                except Exception:
                    pass
                time.sleep(0.3)
                continue
            time.sleep(0.15)
            continue

        # 4) 推算模板左上角（取所有有效命中的平均值）
        origins_x = []
        origins_y = []
        for hit in valid_hits:
            idx, sx, sy, _ = hit
            rx, ry, _, _ = feat_info[idx]
            origins_x.append(sx - rx * scale)
            origins_y.append(sy - ry * scale)
        origin_x = sum(origins_x) / len(origins_x)
        origin_y = sum(origins_y) / len(origins_y)

        if template_w > 0 and template_h > 0:
            abs_cx = int(origin_x + template_w / 2) + offset_x
            abs_cy = int(origin_y + template_h / 2) + offset_y
        else:
            abs_cx = valid_hits[0][1] + offset_x
            abs_cy = valid_hits[0][2] + offset_y

        avg_score = sum(h[3] for h in valid_hits) / len(valid_hits)
        ctx.log(f"[FindImage·特征] 匹配成功！{len(valid_hits)}/{len(feat_paths)}个特征通过，模板原点=({int(origin_x)},{int(origin_y)}) 中心=({abs_cx},{abs_cy}) score={avg_score:.2f}", "SUCCESS")
        _cleanup()
        return (abs_cx, abs_cy, avg_score)

    _cleanup()
    return None


# ---------------- 动作/识别节点 ----------------
def execute_FindImage(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    找图节点：
      - 匹配策略 match_strategy: hybrid(智能混合) / template(模板匹配) / feature(特征匹配)
      - 超时 timeout_seconds：在此时间内循环重试
      - 滚动重试 scroll_dy：非0时，找图失败后将鼠标移到搜索区域中心滚动一次再重试
      - 找到后执行 after_action: none / click / double_click / right_click，带 offset (X,Y)
      - 命中：out_pos(中心坐标) / out_found=True；未命中：out_found=False 但照常走 out_exec
    """
    wait_if_paused(ctx)
    # 1) 读参数
    template = inputs.get("in_template") or node.params.get("template", "")
    # 支持临时PIL/缓存：优先 runtime_attrs["_pil_template"]
    pil_tpl: Optional[Image.Image] = None
    try:
        pil_tpl = getattr(node, "runtime_attrs", {}).get("_pil_template")  # type: ignore[union-attr]
    except Exception:
        pil_tpl = None
    # 找图范围模式：relative=相对目标程序窗口截图，absolute=全屏截图
    search_mode = str(node.params.get("search_mode", "relative") or "relative")
    threshold = inputs.get("in_threshold")
    if threshold is None:
        threshold = float(node.params.get("threshold", 0.9) or 0.9)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.9
    akaze_thr = max(0.01, min(1.0, float(threshold)))
    strategy = str(node.params.get("match_strategy", "hybrid") or "hybrid")
    timeout_s = max(0.1, float(node.params.get("timeout_seconds", 10.0) or 10.0))
    auto_scroll = (int(node.params.get("scroll_dy", 0) or 0) != 0)
    scroll_dy = int(node.params.get("scroll_dy", 0) or 0)
    after_action = str(node.params.get("after_action", "none") or "none")
    _tpl_off = node.params.get("offset", (0, 0))
    if isinstance(_tpl_off, (list, tuple)) and len(_tpl_off) >= 2:
        try:
            offset_x = int(_tpl_off[0])
            offset_y = int(_tpl_off[1])
        except Exception:
            offset_x, offset_y = 0, 0
    else:
        offset_x, offset_y = 0, 0

    feature_assist_mode = str(node.params.get("use_feature_assist", "off") or "off")

    # 2) 模板有效性检查
    use_path: Optional[str] = None
    tmp_path = ""
    if isinstance(template, str) and template and os.path.exists(template):
        use_path = template
    elif pil_tpl is not None:
        # 写临时文件给 matcher
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="ba_findimg_tpl_")
        os.close(fd)
        try:
            pil_tpl.save(tmp_path, format="PNG")
            use_path = tmp_path
        except Exception:
            use_path = None
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    if not use_path:
        ctx.log("[FindImage] 模板无效（路径不存在 & 无PIL缓存） -> out_found=False", "WARN")
        annotate_step(ctx, node, "find", note="ERROR: invalid template")
        return (
            {"out_pos": None, "out_found": False},
            ["out_exec"], "",
        )

    # 3) 超时空转 + 滚动重试 循环
    t0 = time.time()
    pos = None
    last_err = ""

    with _temp_window_foreground(ctx, node.params):
        try:
            # region 在窗口前置之后计算，避免最小化窗口恢复后 rect 失效
            region = None  # None=全屏；(x,y,w,h)=窗口区域
            win_origin = (0, 0)
            if search_mode == "relative":
                temp = _resolve_temp_target_rect(ctx, node.params)
                if temp is not None:
                    rect = temp[1]  # (left, top, right, bottom)
                    region = (int(rect[0]), int(rect[1]), int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1]))
                    win_origin = (int(rect[0]), int(rect[1]))
                    ctx.log(f"[FindImage] 相对目标窗口截图 region={region} (临时目标)", "DEBUG")
                else:
                    try:
                        target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
                        if isinstance(target, dict):
                            r = target.get("rect")
                            if isinstance(r, (tuple, list)) and len(r) >= 4:
                                region = (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
                                win_origin = (int(r[0]), int(r[1]))
                                ctx.log(f"[FindImage] 相对目标窗口截图 region={region} (全局目标)", "DEBUG")
                    except Exception:
                        pass

            while (time.time() - t0) < timeout_s and ctx.is_running:
                wait_if_paused(ctx)
                if not ctx.is_running:
                    break
                # 尝试匹配
                try:
                    if ctx.matcher is not None:
                        if strategy == "template":
                            pos = ctx.matcher.find_image(
                                str(use_path), region=region, threshold=threshold, fast_mode=True
                            )
                        elif strategy == "gray":
                            pos = ctx.matcher.find_image_gray(
                                str(use_path), region=region, threshold=threshold, fast_mode=True
                            )
                        elif strategy == "feature":
                            pos = ctx.matcher.find_image_akaze(
                                str(use_path), region=region, threshold=akaze_thr
                            )
                        else:  # hybrid：彩色 → 灰度 → AKAZE 三层级联
                            pos = ctx.matcher.find_image(
                                str(use_path), region=region, threshold=threshold, fast_mode=True
                            )
                            if pos is None:
                                pos = ctx.matcher.find_image_gray(
                                    str(use_path), region=region, threshold=threshold, fast_mode=True
                                )
                            if pos is None:
                                pos = ctx.matcher.find_image_akaze(
                                    str(use_path), region=region, threshold=akaze_thr
                                )
                        if pos is not None:
                            pass  # pos 已就绪，score 不再单独提取
                except Exception as e:
                    last_err = str(e)
                    ctx.log(f"[FindImage] 匹配异常: {e}", "WARN")

                if pos is not None:
                    # 验证模式：模板匹配成功后，用特征辅助额外验证
                    if feature_assist_mode == "verify" and ctx.is_running:
                        feat_result = _findimage_feature_match(
                            ctx, node.params, region,
                            timeout_s - (time.time() - t0),
                            auto_scroll, scroll_dy,
                            threshold, offset_x, offset_y,
                            strategy,
                            template_pos=pos
                        )
                        if feat_result is None:
                            ctx.log("[FindImage] 模板匹配成功，但特征验证失败 -> 判定未命中", "WARN")
                            pos = None
                            # 验证失败继续重试（不break）
                    if pos is not None:
                        break

                # 未命中：兜底模式（模板匹配失败后用特征辅助尝试）
                if pos is None and feature_assist_mode == "fallback" and ctx.is_running:
                    feat_result = _findimage_feature_match(
                        ctx, node.params, region,
                        timeout_s - (time.time() - t0),
                        auto_scroll, scroll_dy,
                        threshold, offset_x, offset_y,
                        strategy
                    )
                    if feat_result is not None:
                        abs_cx_f, abs_cy_f, _ = feat_result
                        pos = {
                            "left": abs_cx_f - offset_x,
                            "top": abs_cy_f - offset_y,
                            "width": 1,
                            "height": 1,
                            "_feature_assist": True,
                        }
                        break

                # 未命中：是否需要滚动
                if auto_scroll and ctx.is_running and (time.time() - t0) < timeout_s:
                    try:
                        import pyautogui
                        # 将鼠标移到搜索区域中心再滚动，确保滚动作用于目标窗口
                        if region is not None:
                            scroll_cx = int(region[0]) + int(region[2]) // 2
                            scroll_cy = int(region[1]) + int(region[3]) // 2
                        else:
                            # 全屏模式：移到屏幕中心
                            import ctypes
                            scroll_cx = ctypes.windll.user32.GetSystemMetrics(0) // 2
                            scroll_cy = ctypes.windll.user32.GetSystemMetrics(1) // 2
                        pyautogui.moveTo(scroll_cx, scroll_cy)
                        time.sleep(0.05)
                        if scroll_dy != 0:
                            pyautogui.scroll(scroll_dy)
                    except Exception as e2:
                        ctx.log(f"[FindImage] 自动滚动失败: {e2}", "WARN")
                    # 滚动后稍等
                    t_wait = time.time()
                    while (time.time() - t_wait) < 0.5 and ctx.is_running:
                        wait_if_paused(ctx)
                        time.sleep(0.05)
                    continue

                # 普通短间隔避免CPU满载
                time.sleep(0.15)

            # 4) 找到后执行 动作
            if pos is not None:
                # 特征识别辅助的结果：坐标已经是模板中心+偏移，直接用
                if isinstance(pos, dict) and pos.get("_feature_assist"):
                    abs_cx = int(pos.get("left", 0)) + offset_x
                    abs_cy = int(pos.get("top", 0)) + offset_y
                    left, top, w, h = abs_cx, abs_cy, 0, 0
                else:
                    # 解析 pos：兼容 (left, top, w, h) / 字典 / tuple5
                    # 注意：matcher 返回的是绝对屏幕坐标（region 模式下已加 region offset）
                    left, top, w, h = 0, 0, 0, 0
                    if isinstance(pos, dict):
                        left = int(pos.get("left", 0)); top = int(pos.get("top", 0))
                        w = int(pos.get("width", 0)); h = int(pos.get("height", 0))
                    elif isinstance(pos, (list, tuple)):
                        if len(pos) >= 4:
                            left, top, w, h = int(pos[0]), int(pos[1]), int(pos[2]), int(pos[3])
                        elif len(pos) >= 2:
                            left, top, w, h = int(pos[0]), int(pos[1]), 0, 0
                    # 模板中心 + 偏移 = 实际点击位置（绝对屏幕坐标）
                    abs_cx = left + (w // 2) + offset_x
                    abs_cy = top + (h // 2) + offset_y
                # out_pos：relative 模式输出相对窗口坐标，absolute 模式输出绝对坐标
                if search_mode == "relative" and win_origin != (0, 0):
                    out_pos = (abs_cx - win_origin[0], abs_cy - win_origin[1])
                else:
                    out_pos = (abs_cx, abs_cy)
                # 动作：始终用绝对坐标点击
                if after_action != "none":
                    try:
                        import pyautogui
                        pyautogui.moveTo(abs_cx, abs_cy)
                        if after_action == "click":
                            pyautogui.click()
                        elif after_action == "double_click":
                            pyautogui.doubleClick()
                        elif after_action == "right_click":
                            pyautogui.rightClick()
                    except Exception as ea:
                        ctx.log(f"[FindImage] 找到后执行动作失败: {ea}", "WARN")
                # 日志
                display = os.path.basename(str(use_path))
                act_note = {
                    "none": "", "click": "|动作=单击", "double_click": "|动作=双击",
                    "right_click": "|动作=右键",
                }.get(after_action, "")
                mode_note = "相对窗口" if search_mode == "relative" else "全屏"
                ctx.log(f"[FindImage] 命中 {display} 命中矩形=({left},{top},{w},{h}) 绝对中心=({abs_cx},{abs_cy}) out_pos={out_pos}({mode_note}) {act_note}", "SUCCESS")
                annotate_step(
                    ctx, node, "find",
                    pos=(left, top, w, h), region=region,
                    note=f"OK: {display} @({left},{top}) out={out_pos}({mode_note}) {act_note}",
                )
                return (
                    {"out_pos": out_pos, "out_found": True},
                    ["out_exec"], "",
                )
            else:
                msg = f"未找到 {os.path.basename(str(use_path))} threshold={threshold} timeout={timeout_s}s"
                if last_err:
                    msg += f" err={last_err}"
                scroll_note = f"（滚动量={scroll_dy}）" if scroll_dy != 0 else ""
                ctx.log(f"[FindImage] {msg}{scroll_note} -> out_found=False", "WARN")
                annotate_step(
                    ctx, node, "find", region=region,
                    note=f"FAIL: {msg}{scroll_note}",
                )
                return (
                    {"out_pos": None, "out_found": False},
                    ["out_exec"], "",
                )
        finally:
            # 清理临时文件
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


def _find_fail(ctx, node, on_fail: str, reason: str, region=None):
    """
    FindImage 已不再使用失败策略逻辑（on_fail已从节点参数中删除）。
    """
    annotate_step(
        ctx, node, "find",
        region=region, note=f"FAIL: {reason} | on_fail={on_fail}",
    )
    outputs = {"out_pos": None, "out_found": False}
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


def execute_PressKey(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """双模式键盘节点：
    - text 模式：输入文本（粘贴或逐字符），可选末尾按回车
    - key 模式：按下组合键（如 ctrl+c），支持多次重复
    """
    p = dict(node.params or {})
    kb_mode = str(p.get("kb_mode", "text") or "text")

    if kb_mode == "text":
        # ---------- 输入文本模式 ----------
        text = str(p.get("text", "") or "")
        use_paste = bool(p.get("use_paste", True))
        press_enter = bool(p.get("press_enter", False))

        # 变量插值
        try:
            from src.blueprint.core.variable_engine import VariableEngine
            engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
            if engine.has_any_marker(text):
                text = str(engine.interpolate(text))
        except Exception:
            pass

        if not text:
            ctx.log("[PressKey] text 模式但文本为空，跳过。", "WARN")
            annotate_step(ctx, node, "keypress", note="text mode: empty text")
            return {}, ["out_exec"], ""

        ctx.log(f"[PressKey] text 模式 len={len(text)} paste={use_paste} enter={press_enter}", "INFO")
        try:
            if use_paste:
                # 粘贴模式：写剪贴板 + Ctrl+V（防中文乱码）
                import pyperclip
                pyperclip.copy(text)
                time.sleep(0.05)
                if ctx.input_actions is not None:
                    # 用 SendInput 模拟 Ctrl+V（正确的组合键：按下Ctrl→按下V→释放V→释放Ctrl）
                    ia = ctx.input_actions
                    try:
                        ia.key_down("lctrl")
                        time.sleep(0.02)
                        ia.key_down("v")
                        time.sleep(0.03)
                        ia.key_up("v")
                        time.sleep(0.01)
                        ia.key_up("lctrl")
                    except Exception:
                        # 回退到 pyautogui
                        import pyautogui
                        pyautogui.hotkey("ctrl", "v")
                else:
                    import pyautogui
                    pyautogui.hotkey("ctrl", "v")
                time.sleep(0.05)
            else:
                # 逐字符输入
                if ctx.input_actions is not None:
                    for ch in text:
                        ctx.input_actions.hw_press(ch.lower() if ch.isalpha() else ch, hold_ms=20)
                else:
                    import pyautogui
                    pyautogui.write(text)

            if press_enter:
                if ctx.input_actions is not None:
                    ctx.input_actions.hw_press("enter", hold_ms=30)
                else:
                    import pyautogui
                    pyautogui.press("enter")
        except Exception as e:
            ctx.log(f"[PressKey] text 模式异常: {e}", "ERROR")
            annotate_step(ctx, node, "keypress", note=f"text ERROR: {e}")
            return {}, ["out_exec"], ""

        preview = text[:30] + ("…" if len(text) > 30 else "")
        annotate_step(ctx, node, "keypress", note=f"text{'(paste)' if use_paste else ''}: {preview}{' +Enter' if press_enter else ''}")
        return {}, ["out_exec"], ""

    else:
        # ---------- 按键组合模式 ----------
        count = max(1, int(p.get("count", 1) or 1))
        hold_ms = max(1, int(p.get("hold_ms", 50) or 50))
        interval_ms = max(1, int(p.get("interval_ms", 100) or 100))

        # 读取 key_combo JSON 数组
        keys = []
        key_combo_raw = p.get("key_combo", "[]")
        if isinstance(key_combo_raw, str):
            try:
                keys = json.loads(key_combo_raw) if key_combo_raw.strip() else []
            except Exception:
                keys = []
        elif isinstance(key_combo_raw, (list, tuple)):
            keys = list(key_combo_raw)

        # 规范化 + 校验
        keys = [str(k).strip().lower() for k in keys if str(k).strip()]
        if not keys:
            ctx.log("[PressKey] key 模式但 key_combo 为空，跳过。", "WARN")
            annotate_step(ctx, node, "keypress", note="key mode: empty key_combo")
            return {}, ["out_exec"], ""

        # 校验每个键是否合法（别名转换后检查 DIK_CODES）
        try:
            from src.blueprint.runtime.input_actions import DIK_CODES, KEY_ALIASES
            invalid_keys = []
            for k in keys:
                resolved = KEY_ALIASES.get(k, k)
                if resolved not in DIK_CODES:
                    invalid_keys.append(k)
            if invalid_keys:
                msg = f"未知按键: {invalid_keys}，已跳过执行"
                ctx.log(f"[PressKey] {msg}", "ERROR")
                annotate_step(ctx, node, "keypress", note=f"ERROR: {msg}")
                return {}, ["out_exec"], ""
        except Exception:
            pass  # 校验失败不阻塞执行

        ctx.log(f"[PressKey] key 模式 {'+'.join(keys)} x{count} hold={hold_ms}ms", "INFO")
        is_combo = len(keys) > 1

        if is_combo:
            # 组合键：用 hotkey 方式（按下所有键 → 释放）
            for i in range(count):
                if not ctx.is_running:
                    break
                if ctx.input_actions is not None:
                    # 依次按下所有键
                    for k in keys:
                        ctx.input_actions.key_down(k)
                    time.sleep(hold_ms / 1000.0)
                    # 反序释放
                    for k in reversed(keys):
                        ctx.input_actions.key_up(k)
                else:
                    import pyautogui
                    pyautogui.hotkey(*keys)
                if i < count - 1:
                    time.sleep(interval_ms / 1000.0)
                    wait_if_paused(ctx)
            annotate_step(ctx, node, "keypress", note=f"combo {'+'.join(keys)} x{count}")
        else:
            # 单键：复用原有 press_with_pause
            single = keys[0]
            if ctx.input_actions is not None:
                ctx.input_actions.press_with_pause(
                    single, count=count, hold_ms=hold_ms, interval_ms=interval_ms,
                )
            else:
                import pyautogui
                for i in range(count):
                    pyautogui.press(single)
                    if i < count - 1:
                        time.sleep(interval_ms / 1000.0)
            annotate_step(ctx, node, "keypress", note=f"{single} x{count}")

        return {}, ["out_exec"], ""


def _resolve_temp_target_rect(ctx, params: Dict[str, Any]):
    """解析临时目标程序窗口。如果 temp_target 参数非空，查找窗口并返回 (hwnd, rect) 或 None。"""
    from src.blueprint.core.variable_engine import VariableEngine
    title_raw = str(params.get("temp_target_title", "") or "")
    exe_name = str(params.get("temp_target_exe", "") or "").strip()
    class_name = str(params.get("temp_target_class", "") or "").strip()
    if not (title_raw.strip() or exe_name or class_name):
        return None
    try:
        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
        title = str(engine.interpolate(title_raw)) if engine.has_any_marker(title_raw) else title_raw
        # 对 exe_name 和 class_name 也做变量插值
        if engine.has_any_marker(exe_name):
            exe_name = str(engine.interpolate(exe_name))
        if engine.has_any_marker(class_name):
            class_name = str(engine.interpolate(class_name))
    except Exception:
        title = title_raw
    try:
        hwnd = _find_window_3d(title, exe_name, class_name)
        if not hwnd:
            ctx.log(f"[Mouse] 临时目标窗口未找到: title={title!r} exe={exe_name!r} class={class_name!r}", "WARN")
            return None
        import win32gui as _wg
        rect = _wg.GetWindowRect(hwnd)  # (left, top, right, bottom)
        ctx.log(f"[Mouse] 使用临时目标窗口 hwnd={hwnd} rect={rect} title={title!r}", "DEBUG")
        return (hwnd, rect)
    except Exception as e:
        ctx.log(f"[Mouse] 解析临时目标窗口失败: {type(e).__name__}: {e}", "WARN")
        return None


def _bring_window_to_front_safe(ctx, hwnd):
    """安全地把窗口前置到最前面，失败不抛异常。"""
    if not hwnd:
        return
    try:
        import win32gui as _wg
        try:
            from src.tools.window_utils import bring_window_to_front
            bring_window_to_front(hwnd, log_fn=ctx.log)
        except Exception:
            _wg.SetForegroundWindow(hwnd)
    except Exception:
        pass


@contextlib.contextmanager
def _temp_window_foreground(ctx, params: Dict[str, Any]):
    """临时目标窗口前置上下文管理器。

    如果节点配置了临时目标窗口（temp_target_*），在操作前把临时窗口前置到最前面，
    操作完成后（无论成功或异常）把全局目标窗口还原到最前面。

    用法：
        with _temp_window_foreground(ctx, node.params):
            # 执行操作
    """
    temp = _resolve_temp_target_rect(ctx, params)
    if temp is not None:
        temp_hwnd = temp[0]
        # 获取全局目标窗口句柄（用于操作后还原）
        global_hwnd = None
        try:
            target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
            if isinstance(target, dict):
                global_hwnd = target.get("hwnd")
        except Exception:
            pass
        # 前置临时窗口
        _bring_window_to_front_safe(ctx, temp_hwnd)
        time.sleep(0.15)  # 等待窗口切换完成
        try:
            yield
        finally:
            # 还原全局目标窗口到最前面
            if global_hwnd:
                _bring_window_to_front_safe(ctx, global_hwnd)
                time.sleep(0.1)
    else:
        yield


def _mouse_final_pos(ctx, params: Dict[str, Any], inputs: Dict[str, Any]):
    """按 coord_mode 把 pos(tuple2) 或 in_pos 转换为屏幕绝对坐标。
    in_pos 的坐标系取决于 Mouse 节点的 coord_mode：
      - absolute：in_pos 直接当绝对坐标
      - relative：in_pos 当相对目标窗口坐标，加上窗口偏移
    """
    in_pos = inputs.get("in_pos")
    if in_pos is not None and isinstance(in_pos, (tuple, list)) and len(in_pos) == 2:
        x, y = int(in_pos[0]), int(in_pos[1])
        mode = str(params.get("coord_mode", "relative") or "relative")
        if mode == "absolute":
            return (x, y)
        elif mode == "relative":
            # in_pos 是相对目标窗口的坐标，需要加上窗口偏移
            temp = _resolve_temp_target_rect(ctx, params)
            if temp is not None:
                rect = temp[1]
                return (int(rect[0]) + x, int(rect[1]) + y)
            try:
                target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
                if isinstance(target, dict):
                    rect = target.get("rect")
                    if isinstance(rect, (tuple, list)) and len(rect) >= 2:
                        return (int(rect[0]) + x, int(rect[1]) + y)
            except Exception:
                pass
        return (x, y)
    mode = str(params.get("coord_mode", "relative") or "relative")
    x, y = _tuple2(params.get("pos", (0, 0)))
    if mode == "absolute":
        return (x, y)
    elif mode == "relative":
        # 优先使用临时目标窗口
        temp = _resolve_temp_target_rect(ctx, params)
        if temp is not None:
            rect = temp[1]
            return (int(rect[0]) + x, int(rect[1]) + y)
        try:
            target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
            if isinstance(target, dict):
                rect = target.get("rect")
                if isinstance(rect, (tuple, list)) and len(rect) >= 2:
                    rx = int(rect[0])
                    ry = int(rect[1])
                    return (rx + x, ry + y)
        except Exception:
            pass
    return (x, y)


def _tuple2(val) -> Tuple[int, int]:
    """安全把 val 转成 (int, int)；val 可以是 tuple/list/None。"""
    if val is None:
        return (0, 0)
    if isinstance(val, (tuple, list)):
        try:
            return (int(val[0]) if len(val) > 0 else 0, int(val[1]) if len(val) > 1 else 0)
        except Exception:
            return (0, 0)
    return (0, 0)


def _mouse_final_pos_for(ctx, params: Dict[str, Any], point_param_name: str) -> Tuple[int, int]:
    """按 coord_mode 把指定点参数（from_pos / to_pos）转换为屏幕绝对坐标。"""
    mode = str(params.get("coord_mode", "relative") or "relative")
    x, y = _tuple2(params.get(point_param_name, (0, 0)))
    if mode == "absolute":
        return (x, y)
    elif mode == "relative":
        # 优先使用临时目标窗口
        temp = _resolve_temp_target_rect(ctx, params)
        if temp is not None:
            rect = temp[1]
            return (int(rect[0]) + x, int(rect[1]) + y)
        try:
            target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
            if isinstance(target, dict):
                rect = target.get("rect")
                if isinstance(rect, (tuple, list)) and len(rect) >= 2:
                    rx = int(rect[0])
                    ry = int(rect[1])
                    return (rx + x, ry + y)
        except Exception:
            pass
    return (x, y)


def execute_Mouse(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """统一 mouse 执行：根据 action 分派（click/move/drag/scroll）。"""
    import random
    p = dict(node.params or {})
    action = str(p.get("action", "click") or "click")
    btn = str(p.get("button", "left") or "left")
    ia = getattr(ctx, "input_actions", None)

    with _temp_window_foreground(ctx, p):
        try:
            pos = _mouse_final_pos(ctx, p, inputs)
            wait_if_paused(ctx)

            if action == "click":
                clicks = max(1, int(p.get("click_count", 1) or 1))
                hold_min = max(1, int(p.get("hold_ms_min", 30) or 30))
                hold_max = max(1, int(p.get("hold_ms_max", 80) or 80))
                hold_range = (min(hold_min, hold_max), max(hold_min, hold_max))
                post_min = max(0, int(p.get("post_delay_min_ms", 30) or 30))
                post_max = max(0, int(p.get("post_delay_max_ms", 80) or 80))
                post_range = (min(post_min, post_max), max(post_min, post_max))
                # clicks=1→单击1次; clicks=2→双击(1轮2击); clicks=3→三击(1轮3击,非翻倍)
                double = (clicks == 2)
                repeat = 1 if clicks <= 2 else clicks
                if ia is not None:
                    ia.game_click(pos, double=double, random_offset_radius=0,
                                  hold_ms_range=hold_range, post_delay_range=post_range,
                                  repeat=repeat, button=btn)
                annotate_step(ctx, node, "click", pos=pos,
                              note=f"click@{pos} btn={btn} count={clicks}")
                ctx.log(f"[Mouse] click @{pos} btn={btn} x{clicks}", "SUCCESS")

            elif action == "move":
                if ia is not None:
                    try:
                        ia._mouse_move(pos[0], pos[1])
                    except Exception as e:
                        ctx.log(f"[Mouse] move error: {e}", "WARN")
                annotate_step(ctx, node, "move", pos=pos, note=f"move→{pos}")
                ctx.log(f"[Mouse] move→{pos}", "SUCCESS")

            elif action == "drag":
                from_pt = _mouse_final_pos_for(ctx, p, "from_pos")
                to_pt = _mouse_final_pos_for(ctx, p, "to_pos")
                dur = max(0, int(p.get("drag_duration_ms", 300) or 300))
                from_x, from_y = from_pt
                to_x, to_y = to_pt
                _drag_btn_held = False
                if ia is not None:
                    try:
                        steps = max(2, dur // 20)
                        # 1. 移动到起点
                        ia._hw_mouse_move(from_x, from_y)
                        time.sleep(0.05)
                        # 2. 按下鼠标
                        ia._hw_mouse_down(btn)
                        _drag_btn_held = True
                        time.sleep(0.05)
                        # 3. 逐步移动到终点（真正移动鼠标）
                        for i in range(1, steps + 1):
                            if not getattr(ctx, "is_running", True):
                                break
                            ratio = i / steps
                            px = int(from_x + (to_x - from_x) * ratio)
                            py = int(from_y + (to_y - from_y) * ratio)
                            ia._mouse_move(px, py)
                            time.sleep((dur / steps) / 1000.0)
                            wait_if_paused(ctx)
                    except Exception as e:
                        ctx.log(f"[Mouse] drag error: {e}", "ERROR")
                    finally:
                        # 4. 无论成功还是异常，都必须释放鼠标按键，避免卡死
                        if _drag_btn_held:
                            try:
                                time.sleep(0.05)
                                ia._hw_mouse_up(btn)
                            except Exception:
                                pass
                annotate_step(ctx, node, "drag", pos=to_pt,
                              note=f"drag {from_pt}→{to_pt} dur={dur}ms btn={btn}")
                ctx.log(f"[Mouse] drag {from_pt}→{to_pt} dur={dur}ms btn={btn}", "SUCCESS")

            elif action == "scroll":
                dy = int(p.get("scroll_dy", 0) or 0)
                steps = max(1, int(p.get("scroll_steps", 1) or 1))
                if ia is not None:
                    for i in range(steps):
                        try:
                            ia.scroll(pos, dy_px=dy)
                        except Exception as e:
                            ctx.log(f"[Mouse] scroll error: {e}", "WARN")
                        time.sleep(0.02)
                        wait_if_paused(ctx)
                annotate_step(ctx, node, "scroll", pos=pos,
                              note=f"scroll@{pos} {dy:+}px x{steps}")
                ctx.log(f"[Mouse] scroll @{pos} Δy={dy}px repeat={steps}", "SUCCESS")

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
    """剪贴板：读/写/清空

    依赖 pywin32（pythoncom / win32clipboard / win32con）。
    每次调用都会在当前线程配对 CoInitialize/CoUninitialize，保证线程 OLE 公寓模型正确。
    """

    params = dict(node.params or {})
    mode = str(params.get("mode", "read") or "read")
    value_template = str(params.get("value", "") or "")
    output_var = str(params.get("output_var_name", "clip_text") or "clip_text")

    # --- 唯一后端：Windows 原生剪贴板 API ---
    _py_win32clipboard = None
    _py_win32con = None
    _py_pythoncom = None
    _com_refs = 0
    _import_err = None
    try:
        import pythoncom as _pcom
        import win32clipboard as _w32cb
        import win32con as _w32cn
        _py_pythoncom = _pcom
        _py_win32clipboard = _w32cb
        _py_win32con = _w32cn
    except Exception as _e:
        _import_err = f"缺少依赖 pywin32，请先安装：pip install pywin32 （原因: {type(_e).__name__}: {_e}）"

    # --- 公共工具：CoInitialize / CoUninitialize（引用计数语义） ---
    def _com_init():
        nonlocal _com_refs, _py_pythoncom
        if _py_pythoncom is not None:
            try:
                _py_pythoncom.CoInitialize()
                _com_refs += 1
            except Exception:
                pass

    def _com_uninit():
        nonlocal _com_refs, _py_pythoncom
        if _py_pythoncom is not None and _com_refs > 0:
            try:
                _py_pythoncom.CoUninitialize()
                _com_refs -= 1
            except Exception:
                _com_refs = 0

    # --- 公共工具：win32 剪贴板读/写/清空（封装 Open/Close） ---
    def _read_text() -> str:
        CB = _py_win32clipboard
        try:
            CB.OpenClipboard(0)
        except Exception:
            return ""
        try:
            if CB.IsClipboardFormatAvailable(_py_win32con.CF_UNICODETEXT):
                data = CB.GetClipboardData(_py_win32con.CF_UNICODETEXT) or ""
                return str(data)
            return ""
        except Exception:
            return ""
        finally:
            try:
                CB.CloseClipboard()
            except Exception:
                pass

    def _write_text(text: str) -> bool:
        CB = _py_win32clipboard
        try:
            CB.OpenClipboard(0)
        except Exception:
            return False
        ok = False
        try:
            CB.EmptyClipboard()
            CB.SetClipboardData(_py_win32con.CF_UNICODETEXT, str(text))
            ok = True
        except Exception:
            ok = False
        finally:
            try:
                CB.CloseClipboard()
            except Exception:
                pass
        return ok

    def _clear() -> bool:
        CB = _py_win32clipboard
        try:
            CB.OpenClipboard(0)
        except Exception:
            return False
        ok = False
        try:
            CB.EmptyClipboard()
            ok = True
        except Exception:
            ok = False
        finally:
            try:
                CB.CloseClipboard()
            except Exception:
                pass
        return ok

    try:
        wait_if_paused(ctx)

        if _import_err is not None:
            ctx.log(f"[Clipboard] 失败: {_import_err}", "ERROR")
            annotate_step(ctx, node, "clip_err", note=_import_err)
            return {"out_text": ""}, ["err"], _import_err

        # ★ 当前线程先完成 COM 初始化（配对 2 次 = 下面 finally 配对 2 次 uninit）
        _com_init()
        _com_init()

        if mode == "read":
            text = _read_text()
            if output_var:
                ctx.variables[output_var] = text
            annotate_step(ctx, node, "clip_read", note=f"len={len(text)}")
            ctx.log(f"[Clipboard] read OK len={len(text)} -> var[{output_var}]", "SUCCESS")
            return {"out_text": text}, ["out_done"], ""

        elif mode == "write":
            in_text = inputs.get("in_text") if inputs else None
            if in_text is None or (isinstance(in_text, str) and not in_text):
                from src.blueprint.core.variable_engine import VariableEngine
                engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
                if engine.has_any_marker(value_template):
                    text_to_write = str(engine.interpolate(value_template))
                else:
                    text_to_write = str(value_template)
            else:
                text_to_write = str(in_text)

            # 重试 2 次（防其他进程占用剪贴板）
            write_ok = False
            for _ in range(2):
                if _write_text(text_to_write):
                    write_ok = True
                    break
                import time
                try:
                    time.sleep(0.02)
                except Exception:
                    pass

            # 二次校验：立即读回确认
            try:
                verify = _read_text() or ""
                if verify != text_to_write:
                    ctx.log(
                        f"[Clipboard] ⚠️ write 二次校验不一致：写入len={len(text_to_write)} "
                        f"实际读回len={len(verify)}，首次内容前60字：{verify[:60]!r}",
                        "WARN",
                    )
                    write_ok = False
            except Exception:
                pass

            annotate_step(ctx, node, "clip_write", note=f"len={len(text_to_write)} ok={write_ok}")
            ctx.log(
                f"[Clipboard] write{' ' if write_ok else ' ⚠ '}len={len(text_to_write)}",
                "SUCCESS" if write_ok else "WARN",
            )
            return {"out_text": text_to_write}, ["out_done"], ""

        elif mode == "clear":
            # 重试 2 次（防其他进程占用剪贴板）
            clear_ok = False
            for _ in range(2):
                if _clear():
                    clear_ok = True
                    break
                import time
                try:
                    time.sleep(0.02)
                except Exception:
                    pass

            # 二次校验：是否真的空了
            try:
                verify = _read_text() or ""
                if verify:
                    ctx.log(
                        f"[Clipboard] ⚠️ clear 二次校验未空：实际读回len={len(verify)}，"
                        f"前60字：{verify[:60]!r}",
                        "WARN",
                    )
                    clear_ok = False
            except Exception:
                pass

            annotate_step(ctx, node, "clip_clear", note=f"ok={clear_ok}")
            ctx.log(
                f"[Clipboard] clear{' OK' if clear_ok else ' ⚠ 未清空'}",
                "SUCCESS" if clear_ok else "WARN",
            )
            return {"out_text": ""}, ["out_done"], ""

        else:
            ctx.log(f"[Clipboard] 未知 mode={mode}", "ERROR")
            return {"out_text": ""}, ["err"], f"unknown mode: {mode}"

    except Exception as e:
        ctx.log(f"[Clipboard] 失败: {type(e).__name__}: {e}", "ERROR")
        annotate_step(ctx, node, "clip_err", note=str(e))
        return {"out_text": ""}, ["err"], str(e)
    finally:
        # ★ 必须和 _com_init 调用次数严格配对（此处 init 了 2 次）
        _com_uninit()
        _com_uninit()


def _decode_cmd_output(data: bytes) -> str:
    """安全解码子进程输出：按 UTF-8 → GBK → UTF-8(replace) 顺序 fallback，避免 UnicodeDecodeError"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        try:
            return str(data)
        except Exception:
            return ""
    raw = bytes(data)
    if len(raw) == 0:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        try:
            return raw.decode("gbk", errors="replace")
        except Exception:
            return ""


def execute_Command(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """执行shell命令/外部程序"""
    import subprocess
    import shlex

    params = dict(node.params or {})
    cmd_template = str(params.get("command", "") or "")
    wd_template = str(params.get("working_dir", "") or "")
    timeout_ms = max(0, int(params.get("timeout_ms", 10000) or 10000))
    capture = bool(params.get("capture_streams", True))
    shell_mode = str(params.get("shell_mode", "cmd_c") or "cmd_c")
    fail_nonzero = bool(params.get("fail_on_nonzero", False))

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        # 变量插值 + 特殊占位符 {blueprint_dir}
        engine = getattr(ctx, "var_engine", None) or VariableEngine(getattr(ctx, "variables", {}) or {})
        blueprint_dir = engine.get("blueprint_dir") or getattr(ctx, "blueprint_dir", "")
        if not engine.has("blueprint_dir"):
            engine.set("blueprint_dir", blueprint_dir)
        command = str(engine.interpolate(cmd_template)) if engine.has_any_marker(cmd_template) else str(cmd_template)
        working_dir = ""
        if wd_template:
            working_dir = str(engine.interpolate(wd_template)) if engine.has_any_marker(wd_template) else str(wd_template)

        # 解析 shell 模式
        if shell_mode == "no_shell":
            use_shell = False
            # no_shell 下 args 必须是 list（程序+参数），不能是 str，
            # 否则整个字符串会被当作可执行文件名导致 FileNotFoundError
            try:
                args = shlex.split(command, posix=(os.name != "nt"))
            except Exception:
                # shlex 解析失败时 fallback 到空白字符拆分
                args = command.split()
        elif shell_mode == "powershell":
            use_shell = False
            # 包装成 powershell -Command
            args = ["powershell", "-NoProfile", "-Command", command]
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
        # 注意：不用 text=True，改用 bytes 捕获后手动解码，避免后台线程抛 UnicodeDecodeError
        timeout_s = (timeout_ms / 1000.0) if timeout_ms > 0 else None
        try:
            result = subprocess.run(
                args, shell=use_shell, cwd=cwd,
                capture_output=True, timeout=timeout_s,
            )
            stdout_val = _decode_cmd_output(result.stdout)
            stderr_val = _decode_cmd_output(result.stderr)
            exit_code = int(result.returncode)
        except subprocess.TimeoutExpired as te:
            stdout_val = _decode_cmd_output(te.stdout)
            stderr_val = f"(TIMEOUT after {timeout_ms}ms) " + _decode_cmd_output(te.stderr)
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


def execute_WebBrowser(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """【打开网页】用浏览器打开指定 URL。支持 {var} 插值 + 选择 default/chrome/edge/firefox 浏览器。"""
    import webbrowser
    import shutil as _shutil

    p = dict(node.params or {})
    _BROWSER_MAP = {
        "默认": "default", "default": "default",
        "chrome": "chrome",
        "edge": "edge",
        "firefox": "firefox",
    }
    url_template = str(p.get("url", "") or "")
    browser_raw = str(p.get("browser", "default") or "default").strip().lower()
    browser_name = _BROWSER_MAP.get(browser_raw, "default")

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(getattr(ctx, "variables", {}) or {})
        if engine.has_any_marker(url_template):
            url_template = str(engine.interpolate(url_template))
        url = url_template if url_template else ""

        if not url:
            err = "URL 为空（打开网页必须填 URL）"
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

        note = f"[{browser_name}] 打开 → {url[:100]}"
        annotate_step(ctx, node, "web_browser_open", note=note)
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
    import urllib.parse

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
        raw_url = url_template
        if not raw_url.strip():
            err = "URL 为空"
            annotate_step(ctx, node, "http_err", note=err)
            return {"response_text": "", "response_json": {}, "status_code": 0}, ["err"], err

        # URL 规范化：对 path 和 query 中的非 ASCII 字符做 %xx 编码（urllib.request 要求 URL 纯 ASCII）
        # scheme/netloc 保持原样；path/query/fragment 用 quote 编码非 ASCII 字符，保留分隔符
        try:
            _splitted = urllib.parse.urlsplit(raw_url)
            _enc_path = urllib.parse.quote(_splitted.path, safe="/:@!$&'()*+,;=-._~%") if _splitted.path else ""
            _enc_query = urllib.parse.quote(_splitted.query, safe=":&=+$,/?#[]-._~%") if _splitted.query else ""
            _enc_frag = urllib.parse.quote(_splitted.fragment, safe="-._~%") if _splitted.fragment else ""
            url = urllib.parse.urlunsplit((_splitted.scheme, _splitted.netloc, _enc_path, _enc_query, _enc_frag))
        except Exception:
            # 兜底：拆分失败就退化为 quote 整URL（保留所有特殊字符）
            url = urllib.parse.quote(raw_url, safe=":/?#[]@!$&'()*+,;=-._~%")

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


# ---------------- 变量节点 ----------------
def _collect_inputs_array(inputs: Optional[Dict[str, Any]]) -> List[Any]:
    """从 inputs 字典里收集所有 `in_数字` 命名的 DATA 端口值，按数字排序返回列表（双存策略用）。
    兼容老蓝图命名：如果有旧名 `in_left` 也把它塞在 inputs_list[0]（仅当 in_0 不存在时）。"""
    if not inputs:
        return []
    import re as _re
    _num_re = _re.compile(r"^in_(\d+)$")
    indexed: Dict[int, Any] = {}
    max_idx = -1
    for k, v in inputs.items():
        m = _num_re.match(k)
        if m:
            idx = int(m.group(1))
            indexed[idx] = v
            if idx > max_idx:
                max_idx = idx
    if max_idx < 0:
        # 没找到任何 in_N → 兼容老的 in_left 端口名（VarSwitch 旧蓝图用）
        if "in_left" in inputs:
            return [inputs["in_left"]]
        return []
    # 按 0..max_idx 顺序填充（中间缺的填 None 保持索引正确）
    return [indexed.get(i) for i in range(max_idx + 1)]


def _inject_inputs_temp_vars(engine, inputs_list: List[Any]):
    """注入临时变量：__INPUTS__（完整数组） + __INPUTS__0 / __INPUTS__1 ...（按索引单值）。
    只保留统一通用名，不再用别名前缀（__VS_LEFT__ / __SET_IN__）降低理解成本。"""
    inputs_list = list(inputs_list or [])
    engine.set("__INPUTS__", list(inputs_list))
    for i, v in enumerate(inputs_list):
        engine.set(f"__INPUTS__{i}", v)


def execute_SetVariable(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    from src.blueprint.core.variable_engine import parse_batch_var_lines, VariableEngine

    # ① 先注入多端口临时变量（batch_text 里写 {__INPUTS__0} / {__INPUTS__1} 即可引用）
    engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
    inputs_list = _collect_inputs_array(inputs)
    _inject_inputs_temp_vars(engine, inputs_list)

    set_count = 0
    first_err = ""
    note_parts: list = []

    batch_raw = node.params.get("batch_text") or ""
    if isinstance(batch_raw, str) and batch_raw.strip():
        kv_map, err_msg = parse_batch_var_lines(batch_raw)
        if err_msg:
            first_err = err_msg.splitlines()[0]
            ctx.log(f"[Var] SetVariable 解析警告: {err_msg}", "WARN")
        for k, v in kv_map.items():
            if isinstance(v, str) and engine.has_any_marker(v):
                interpolated = engine.interpolate(v)
            else:
                interpolated = v
            final_val = _auto_coerce(interpolated)
            ctx.set_var(k, final_val)
            engine.set(k, final_val)
            set_count += 1
            if len(note_parts) < 6:
                note_parts.append(f"{k}={repr(final_val)[:40]}")

    if not set_count and not batch_raw.strip() and not inputs_list:
        first_err = first_err or "SetVariable 未设置任何变量：batch_text 为空且没有连数据输入端口"

    if set_count:
        note = "SetVar(" + str(set_count) + ") " + "; ".join(note_parts)
        if inputs_list:
            note += f" [端口={len(inputs_list)} 个]"
        ctx.log(f"[Var] {note}", "DEBUG")
    else:
        note = "SetVar(0)"
        if inputs_list:
            note += f" [端口={len(inputs_list)} 个]"
        ctx.log(f"[Var] {note} {first_err}".rstrip(), "WARN" if first_err else "DEBUG")
    annotate_step(ctx, node, "var", note=note)
    return {}, ["out_exec"], first_err or ""


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


def _varswitch_eval_condition(cond: Dict[str, Any], engine) -> Tuple[bool, str]:
    """对解析后的单条条件做求值。返回 (命中, 可读说明)。"""
    label = cond.get("label", "")
    var = cond.get("var", "")
    op = cond.get("op", "==")
    target = cond.get("value")
    # 先取值：变量不存在 → None（比较时当作 falsy）
    try:
        actual = engine.get(var)
    except Exception:
        actual = None
    # target 如果含 {var} 插值 → 再算一遍
    if isinstance(target, str) and ("{" in target or "${" in target):
        try:
            target = engine.interpolate(target)
        except Exception:
            pass
    # 自动类型转换（两边都转数字试试）
    a = actual
    b = _auto_coerce(target)
    def _num(x):
        try:
            return float(x), True
        except Exception:
            if isinstance(x, bool):
                return float(x), True
            return None, False
    an, a_ok = _num(a)
    bn, b_ok = _num(b)
    try:
        if op == "==":
            # 宽松等于
            hit = False
            if a == b:
                hit = True
            elif a_ok and b_ok and abs(an - bn) < 1e-9:
                hit = True
            elif str(a).strip() == str(b).strip():
                hit = True
            return hit, f"{var}={a!r} == {b!r} → {hit}"
        if op == "!=":
            hit_eq, _ = _varswitch_eval_condition({**cond, "op": "=="}, engine)
            return (not hit_eq), f"{var}={a!r} != {b!r} → {not hit_eq}"
        if op in (">", "<", ">=", "<="):
            # 必须双方都能转数字才合法，否则 False
            if not a_ok or not b_ok:
                return False, f"{var}={a!r} {op} {b!r} → 非数字无法比较"
            if op == ">":
                hit = an > bn
            elif op == "<":
                hit = an < bn
            elif op == ">=":
                hit = an >= bn
            else:  # <=
                hit = an <= bn
            return hit, f"{var}={an} {op} {bn} → {hit}"
    except Exception as e:
        return False, f"求值异常 {e}"
    return False, f"未知运算符 {op}"


def execute_VarSwitch(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    from src.blueprint.core.variable_engine import VariableEngine
    from src.blueprint.core.node_library import parse_varswitch_conditions
    import re as _re

    # ① 先收集多端口输入 + 注入临时变量（统一通用名 __INPUTS__N）
    engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
    inputs_list = _collect_inputs_array(inputs)
    _inject_inputs_temp_vars(engine, inputs_list)
    in_left_val = inputs_list[0] if inputs_list else None

    raw = node.params.get("cases", "") or ""

    # ---- 预处理：简写语法（行首直接运算符 >= 60）补 __INPUTS__0 前缀 ----
    _op_prefix = _re.compile(r'^\s*(==|!=|>=|<=|>|<)\s*(.+)')
    processed_lines = []
    shorthand_count = 0
    if isinstance(raw, str):
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                processed_lines.append(line)
                continue
            m = _op_prefix.match(line)
            if m:
                shorthand_count += 1
                processed_lines.append(f"__INPUTS__0 {m.group(1)} {m.group(2)}")
            else:
                processed_lines.append(line)
    processed_raw = "\n".join(processed_lines)

    if shorthand_count and (in_left_val is None and not inputs_list):
        ctx.log(
            f"[VarSwitch] {shorthand_count} 条简写条件需要连线 in_0 端口提供左值（当前未连线，简写条件将视为 '__INPUTS__0'=None 比较）",
            "WARN",
        )

    conditions = parse_varswitch_conditions(processed_raw if isinstance(processed_raw, str) else "")
    valid_conditions = [c for c in conditions if not c.get("error")]
    notes = []
    all_true = True
    for c in valid_conditions:
        hit, info = _varswitch_eval_condition(c, engine)
        notes.append(info)
        if not hit:
            all_true = False
    if valid_conditions:
        port = "yes" if all_true else "no"
    else:
        port = "no"
    note = f"VarSwitch → {port} ({len(inputs_list)}入; {'; '.join(notes[:3])}{' …' if len(notes) > 3 else ''})"
    ctx.log(f"[VarSwitch] {note}", "DEBUG")
    annotate_step(ctx, node, "condition", note=f"→ {port} ({len(valid_conditions)} 条件 AND, {len(inputs_list)} 输入)")
    return {}, [port], ""


def execute_Sequence(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    num_paths = max(1, int(node.params.get("num_paths", 3) or 3))
    interval = float(node.params.get("interval_sec", 1.0) or 1.0)
    mode = str(node.params.get("mode", "short_circuit") or "short_circuit")

    graph = getattr(ctx, "_graph_ref", None)
    runner = getattr(ctx, "_runner_ref", None)
    if graph is None or runner is None:
        msg = "[Sequence] ctx 缺少 graph/runner 引用，无法同步walk分支（请检查BlueprintRunner入口是否注入）。"
        ctx.log(msg, "ERROR")
        annotate_step(ctx, node, "seq", note="FAIL: 缺少graph_ref/runner_ref")
        return {"out_done_path": None, "out_num_paths": num_paths}, ["else"], msg

    if mode == "complete":
        # 完全求值模式：所有分支都跑完，全部成功才走 out_done，任一失败走 else
        all_success = True
        last_reason = ""
        for path_i in range(1, num_paths + 1):
            if not ctx.is_running:
                break
            if path_i > 1 and interval > 0 and ctx.is_running:
                t0 = time.time()
                while ctx.is_running and (time.time() - t0) < interval:
                    wait_if_paused(ctx)
                    time.sleep(0.05)
            port_name = str(path_i)
            ctx.log(f"[Sequence] {node.id[:6]} 完全模式 尝试路径 #{path_i}/{num_paths}", "DEBUG")
            annotate_step(ctx, node, "seq", note=f"complete #{path_i}/{num_paths}")
            success, reason = _walk_branch_until_end(ctx, graph, runner, node.id, port_name)
            last_reason = reason
            if not success:
                all_success = False
                ctx.log(f"[Sequence] 路径#{path_i} 失败：{reason}，完全模式继续跑剩余分支", "DEBUG")
            else:
                ctx.log(f"[Sequence] 路径#{path_i} 成功：{reason}，完全模式继续跑剩余分支", "DEBUG")
        if all_success and ctx.is_running:
            note = f"完全模式：全部 {num_paths} 条路径成功 → out_done"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "INFO")
            annotate_step(ctx, node, "seq", note=f"✓完全{num_paths}→out_done")
            return {"out_done_path": num_paths, "out_num_paths": num_paths}, ["out_done"], ""
        else:
            note = f"完全模式：路径失败 → else；原因：{last_reason}"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "WARN")
            annotate_step(ctx, node, "seq", note=f"✗完全失败→else；{last_reason[:40]}")
            return {"out_done_path": None, "out_num_paths": num_paths}, ["else"], ""
    elif mode == "or_true":
        # 一真即真（逻辑或）模式：任一分支成功即立即结束并返回成功
        path_ok: Optional[int] = None
        last_reason = ""
        for path_i in range(1, num_paths + 1):
            if not ctx.is_running:
                break
            if path_i > 1 and interval > 0 and ctx.is_running:
                t0 = time.time()
                while ctx.is_running and (time.time() - t0) < interval:
                    wait_if_paused(ctx)
                    time.sleep(0.05)
            port_name = str(path_i)
            ctx.log(f"[Sequence] {node.id[:6]} 一真即真(OR) 尝试路径 #{path_i}/{num_paths}", "DEBUG")
            annotate_step(ctx, node, "seq", note=f"OR #{path_i}/{num_paths}")
            success, reason = _walk_branch_until_end(ctx, graph, runner, node.id, port_name)
            last_reason = reason
            if success:
                path_ok = path_i
                ctx.log(f"[Sequence] 一真即真：路径#{path_i} 成功：{reason} → 立即结束→out_done", "DEBUG")
                break
            else:
                ctx.log(f"[Sequence] 路径#{path_i} 失败：{reason}，继续尝试下一条。", "DEBUG")
        if path_ok is not None:
            note = f"一真即真(OR)：路径#{path_ok}/{num_paths} 命中 → out_done；原因：{last_reason}"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "INFO")
            annotate_step(ctx, node, "seq", note=f"✓OR#{path_ok}→out_done")
            return {"out_done_path": path_ok, "out_num_paths": num_paths}, ["out_done"], ""
        else:
            note = f"一真即真(OR)：全部{num_paths}条路径无命中 → else；最后原因：{last_reason}"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "WARN")
            annotate_step(ctx, node, "seq", note=f"✗OR全失败→else；{last_reason[:40]}")
            return {"out_done_path": None, "out_num_paths": num_paths}, ["else"], ""
    else:
        # 短路模式（AND短路）：任一分支失败即立即退出，全部成功才走 out_done
        path_fail: Optional[int] = None
        last_reason = ""
        for path_i in range(1, num_paths + 1):
            if not ctx.is_running:
                break
            if path_i > 1 and interval > 0 and ctx.is_running:
                t0 = time.time()
                while ctx.is_running and (time.time() - t0) < interval:
                    wait_if_paused(ctx)
                    time.sleep(0.05)
            port_name = str(path_i)
            ctx.log(f"[Sequence] {node.id[:6]} 短路(AND) 尝试路径 #{path_i}/{num_paths}  (出口端口={port_name})", "DEBUG")
            annotate_step(ctx, node, "seq", note=f"短路AND #{path_i}/{num_paths} -> {port_name}")
            success, reason = _walk_branch_until_end(ctx, graph, runner, node.id, port_name)
            last_reason = reason
            if success:
                ctx.log(f"[Sequence] 短路(AND)：路径#{path_i} 成功，继续下一条。", "DEBUG")
            else:
                path_fail = path_i
                ctx.log(f"[Sequence] 短路(AND)：路径#{path_i} 失败：{reason} → 立即退出→else", "DEBUG")
                break
        if path_fail is None:
            note = f"短路(AND)成功：全部{num_paths}条路径通过 → out_done；末因：{last_reason}"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "INFO")
            annotate_step(ctx, node, "seq", note=f"✓短路AND全{num_paths}→out_done")
            return {"out_done_path": num_paths, "out_num_paths": num_paths}, ["out_done"], ""
        else:
            note = f"短路(AND)：路径#{path_fail} 失败 → else；原因：{last_reason}"
            ctx.log(f"[Sequence] {node.id[:6]} {note}", "WARN")
            annotate_step(ctx, node, "seq", note=f"✗短路AND#{path_fail}→else；{last_reason[:40]}")
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

    # --- 端口优先：in_title / in_message 非空则用连线值，否则回落 params ---
    def _take(key_in, fallback):
        v = inputs.get(key_in) if inputs else None
        if v is None:
            return fallback
        if isinstance(v, str) and v == "":
            return fallback
        return v

    title_raw = _take("in_title", str(node.params.get("title", "") or ""))
    msg_raw = _take("in_message", str(node.params.get("message", "") or ""))
    title_raw = str(title_raw) if not isinstance(title_raw, str) else title_raw
    msg_raw = str(msg_raw) if not isinstance(msg_raw, str) else msg_raw

    level_raw = str(node.params.get("level", "INFO") or "INFO").upper()
    show_popup = bool(node.params.get("show_popup", False))
    play_sound = bool(node.params.get("play_sound", False))
    duration_sec = float(node.params.get("duration_sec", 3.0) or 3.0)

    # --- 第一阶段：普通插值（title/message 都走，但 message 先 disable ALL_VARS 留着最后一步替换） ---
    title = engine.interpolate(title_raw) if engine.has_any_marker(title_raw) else title_raw
    msg_without_allvars = (
        engine.interpolate(msg_raw, disable_all_vars=True)
        if engine.has_any_marker(msg_raw)
        else msg_raw
    )
    # --- 第二阶段：message 单独替换 {ALL_VARS} / ${ALL_VARS} ---
    all_vars_rendered = engine._render_all_vars() if isinstance(engine, VariableEngine) else VariableEngine(ctx.variables)._render_all_vars()

    def _replace_all_vars(text: str) -> str:
        if not isinstance(text, str):
            return text
        t = text.replace("${ALL_VARS}", all_vars_rendered)
        t = t.replace("{ALL_VARS}", all_vars_rendered)
        return t

    msg_final = _replace_all_vars(msg_without_allvars)
    title_final = _replace_all_vars(str(title) if not isinstance(title, str) else title)

    if level_raw not in ("INFO", "WARN", "ERROR", "DEBUG"):
        level_final = "INFO"
    else:
        level_final = level_raw

    # 日志输出
    log_body = str(msg_final)
    if len(log_body) > 4000:
        log_body = log_body[:4000] + "\n...（截断，完整内容看系统弹窗）"
    ctx.log(f"[{level_final}] {title_final}\n{log_body}" if log_body else f"[{level_final}] {title_final}", level_final)

    # 提示音
    if play_sound:
        try:
            import ctypes
            MB_ICONINFORMATION = 0x00000040
            MB_ICONWARNING = 0x00000030
            MB_ICONERROR = 0x00000010
            icon = MB_ICONERROR if level_final == "ERROR" else (MB_ICONWARNING if level_final == "WARN" else MB_ICONINFORMATION)
            # MessageBeep 只播放声音不弹窗
            ctypes.windll.user32.MessageBeep(icon)
        except Exception as e:
            ctx.log(f"[Notify] 提示音失败: {e}", "DEBUG")

    # 系统弹窗
    if show_popup:
        try:
            _win32_popup(str(title_final), str(msg_final), level_final)
        except Exception as e:
            ctx.log(f"[Notify] 弹窗失败: {e}", "WARN")

    # 应用内非阻塞 Toast（不管 show_popup 是否勾选都会发）：
    #   关掉系统弹窗时，用户仍能在主窗口右上角看到浮动提示，避免完全无提示
    #   开了系统弹窗时也发，双保险
    try:
        runner_ref = getattr(ctx, "_runner_ref", None)
        if runner_ref is not None and hasattr(runner_ref, "notify_toast"):
            runner_ref.notify_toast.emit(
                str(title_final),
                str(msg_final),
                str(level_final),
                float(duration_sec),
            )
    except Exception:
        pass

    note = f"Notify {level_final} {duration_sec:g}s: {str(title_final)[:50]}"
    annotate_step(ctx, node, "notify", note=note)
    return {}, ["out_exec"], ""


# ---------------- 检测：多图AND（if_img） ----------------
def execute_IfImageDetect(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    if_img 节点：检查多张参考图是否满足条件。
    - 无 timeout（只查一次，与 FindImage 区分）
    - eval_mode:
      * complete（完全求值 AND）：全部命中 -> yes；缺任何一张 -> no；每张都测并输出分数，便于调试
      * short_circuit（短路 AND）：依次检测只要有一张不匹配就立刻返回 no
      * or_true（一真即真 OR）：只要有一张匹配就立刻返回 yes；全部不匹配才返回 no
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
    elif isinstance(raw, (list, tuple)):
        imgs = list(raw)
    else:
        imgs = []
    if not isinstance(imgs, list):
        imgs = []
    try:
        threshold = float(data.get("threshold", 0.9) or 0.9)
    except (TypeError, ValueError):
        threshold = 0.9
    akaze_thr = max(0.01, min(1.0, float(threshold)))
    strategy = str(data.get("match_strategy", "hybrid") or "hybrid")
    eval_mode = str(data.get("eval_mode", "complete") or "complete")
    search_mode = str(data.get("search_mode", "relative") or "relative")

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
    hit_name: Optional[str] = None
    if eval_mode == "or_true":
        any_hit = False
    else:
        hit_all = True
    scores: List[str] = []

    with _temp_window_foreground(ctx, data):
        try:
            # region 在窗口前置之后计算，避免最小化窗口恢复后 rect 失效
            region = None
            if search_mode == "relative":
                temp = _resolve_temp_target_rect(ctx, data)
                if temp is not None:
                    rect = temp[1]
                    region = (int(rect[0]), int(rect[1]), int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1]))
                    ctx.log(f"[IfImg] 相对目标窗口检测 region={region} (临时目标)", "DEBUG")
                else:
                    try:
                        target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
                        if isinstance(target, dict):
                            r = target.get("rect")
                            if isinstance(r, (tuple, list)) and len(r) >= 4:
                                region = (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
                                ctx.log(f"[IfImg] 相对目标窗口检测 region={region} (全局目标)", "DEBUG")
                    except Exception:
                        pass

            for i, item in enumerate(imgs):
                wait_if_paused(ctx)
                if not ctx.is_running:
                    break
                if not isinstance(item, dict):
                    miss_name = f"[{i}]无效项"
                    scores.append(f"[{i}] 无效=0.00")
                    if eval_mode == "or_true":
                        # OR模式：无效算未命中，继续下一张
                        continue
                    else:
                        hit_all = False
                        if eval_mode == "short_circuit":
                            break
                        else:
                            continue
                path = item.get("path", "") or ""
                pil = cached_pils[i] if i < len(cached_pils) else None

                use_path = ""
                if isinstance(path, str) and path and os.path.exists(path):
                    use_path = path
                elif pil is not None:
                    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ba_ifimg_")
                    os.close(fd)
                    tmp_paths.append(tmp)  # 先登记，save 失败也能清理
                    try:
                        pil.save(tmp, format="PNG")
                        use_path = tmp
                    except Exception:
                        use_path = None

                if not use_path:
                    miss_name = f"[{i}]无路径/无图"
                    scores.append(f"[{i}] 无图=0.00")
                    if eval_mode == "or_true":
                        continue
                    else:
                        hit_all = False
                        if eval_mode == "short_circuit":
                            break
                        else:
                            continue

                pos = None
                score_val = 0.0
                try:
                    if ctx.matcher is not None:
                        if strategy == "template":
                            pos = ctx.matcher.find_image(use_path, region=region, threshold=threshold, fast_mode=True)
                        elif strategy == "gray":
                            pos = ctx.matcher.find_image_gray(use_path, region=region, threshold=threshold, fast_mode=True)
                        elif strategy == "feature":
                            pos = ctx.matcher.find_image_akaze(use_path, region=region, threshold=akaze_thr)
                        else:  # hybrid：彩色 → 灰度 → AKAZE
                            pos = ctx.matcher.find_image(use_path, region=region, threshold=threshold, fast_mode=True)
                            if pos is None:
                                pos = ctx.matcher.find_image_gray(use_path, region=region, threshold=threshold, fast_mode=True)
                            if pos is None:
                                pos = ctx.matcher.find_image_akaze(use_path, region=region, threshold=akaze_thr)
                except Exception as e:
                    ctx.log(f"[IfImg] 匹配异常[{i}]: {e}", "WARN")
                try:
                    if pos is not None and isinstance(pos, (tuple, list)) and len(pos) >= 5:
                        score_val = float(pos[4])
                    elif pos is not None and isinstance(pos, dict):
                        s = pos.get("score")
                        if isinstance(s, (int, float)):
                            score_val = float(s)
                        else:
                            s2 = pos.get("similarity")
                            score_val = float(s2) if isinstance(s2, (int, float)) else 1.0
                    else:
                        score_val = 1.0 if pos is not None else 0.0
                except Exception:
                    score_val = 1.0 if pos is not None else 0.0

                display = os.path.basename(str(path)) if path else f"image[{i}]"
                scores.append(f"[{i}] {display} 分数={score_val:.2f} {'✓' if pos is not None else '✗'}")

                if pos is None:
                    miss_name = display
                    ctx.log(f"[IfImg] 缺少: {display} (score={score_val:.2f})", "DEBUG")
                    if eval_mode == "or_true":
                        continue
                    else:
                        hit_all = False
                        if eval_mode == "short_circuit":
                            break
                else:
                    hit_name = display
                    ctx.log(f"[IfImg] 命中: {display} score={score_val:.2f}", "DEBUG")
                    if eval_mode == "or_true":
                        any_hit = True
                        ctx.log(f"[IfImg] 一真即真(OR)：{display} 命中 → 立即返回 yes", "DEBUG")
                        break

            if eval_mode == "complete" and scores:
                ctx.log("[IfImg] 匹配结果汇总(完全求值):\n  " + "\n  ".join(scores), "DEBUG")
            elif eval_mode == "or_true" and scores:
                ctx.log("[IfImg] 匹配结果汇总(一真即真 OR):\n  " + "\n  ".join(scores), "DEBUG")

            if eval_mode == "or_true":
                if any_hit and ctx.is_running:
                    ctx.log(f"[IfImg] 一真即真(OR)：命中 [{hit_name}] -> yes", "DEBUG")
                    hit_cnt = sum(1 for s in scores if "✓" in s)
                    annotate_step(ctx, node, "find", note=f"OR HIT: {hit_name} ({hit_cnt}/{len(imgs)})")
                    return {}, ["yes"], ""
                else:
                    note_parts = [f"0/{len(imgs)}"]
                    if miss_name:
                        note_parts.append(f"ALL MISS, last: {miss_name}")
                    note_parts.append(f"mode=or_true")
                    annotate_step(ctx, node, "find", note=" | ".join(note_parts))
                    return {}, ["no"], ""
            else:
                if hit_all and ctx.is_running:
                    ctx.log(f"[IfImg] 全部{len(imgs)}张都存在 -> yes", "DEBUG")
                    annotate_step(ctx, node, "find", note=f"ALL PRESENT x{len(imgs)} [{eval_mode}]")
                    return {}, ["yes"], ""
                else:
                    note_parts = []
                    if miss_name:
                        note_parts.append(f"MISS: {miss_name}")
                    note_parts.append(f"mode={eval_mode}")
                    if eval_mode == "complete":
                        hit_cnt = sum(1 for s in scores if "✓" in s)
                        note_parts.insert(0, f"{hit_cnt}/{len(imgs)}")
                    annotate_step(ctx, node, "find", note=" | ".join(note_parts))
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
    if_static 节点：指定 ROI 内，在「静止持续时间」内像素差 < 阈值即判定静止。
    - roi: [x, y, w, h]
    - 超过「最大超时」仍不满足 -> no；满足 -> yes
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
    stillness_seconds = max(0.1, float(p.get("stillness_seconds", 5.0) or 5.0))
    sample_interval_seconds = max(0.03, float(p.get("sample_interval_seconds", 1.0) or 1.0))
    timeout_seconds = max(1.0, float(p.get("timeout_seconds", 20.0) or 20.0))
    search_mode = str(p.get("search_mode", "relative") or "relative")

    if w <= 0 or h <= 0:
        ctx.log("[if_static] 未设置ROI，先点右侧按钮框选区域 -> no", "WARN")
        annotate_step(ctx, node, "static", note="ERROR: ROI empty")
        return {}, ["no"], ""

    # relative 模式：获取窗口偏移（缓存，不每帧枚举窗口）
    _cached_win_offset = None
    _cache_ts = 0.0
    _CACHE_TTL = 2.0  # 缓存有效期2秒

    def _get_win_offset():
        nonlocal _cached_win_offset, _cache_ts
        if search_mode != "relative":
            return (0, 0)
        # 快路径：优先从 ctx 缓存的全局目标读取（不枚举窗口）
        try:
            target = ctx.get_var("target") if hasattr(ctx, "get_var") else None
            if isinstance(target, dict):
                r = target.get("rect")
                if isinstance(r, (tuple, list)) and len(r) >= 2:
                    return (int(r[0]), int(r[1]))
        except Exception:
            pass
        # 慢路径：临时目标窗口，使用缓存避免每帧枚举
        now = time.monotonic()
        if _cached_win_offset is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cached_win_offset
        temp = _resolve_temp_target_rect(ctx, p)
        if temp is not None:
            rect = temp[1]
            _cached_win_offset = (int(rect[0]), int(rect[1]))
            _cache_ts = now
            return _cached_win_offset
        return (0, 0)

    prev_roi: Optional[np.ndarray] = None
    stable_since_ts: Optional[float] = None
    t0 = time.monotonic()
    timeout_s = timeout_seconds
    interval_s = sample_interval_seconds
    last_diff = None  # None=未采样过，float=最近一次帧差

    with _temp_window_foreground(ctx, p):
        while (time.monotonic() - t0) < timeout_s and getattr(ctx, "is_running", True):
            wait_if_paused(ctx)
            if not getattr(ctx, "is_running", True):
                break
            shot_bgr = None
            try:
                if ctx.screen is not None:
                    ox, oy = _get_win_offset()
                    shot_bgr = ctx.screen.capture_region_bgr((x + ox, y + oy, w, h))
            except Exception:
                shot_bgr = None
            if shot_bgr is None:
                # 截图失败不立即终止，等待下一帧重试
                time.sleep(min(0.5, interval_s))
                continue

            if prev_roi is not None:
                diff = float(
                    np.mean(np.abs(shot_bgr.astype(np.int16) - prev_roi.astype(np.int16)) / 255.0)
                )
                last_diff = diff
                if diff < dt:
                    if stable_since_ts is None:
                        stable_since_ts = time.monotonic()
                    elapsed_s = time.monotonic() - stable_since_ts
                    if elapsed_s >= stillness_seconds:
                        ctx.log(
                            f"[if_static] 达到静止 持续={elapsed_s:.1f}s (要求>={stillness_seconds:.1f}s), diff={diff:.4f} < {dt:.4f} -> yes",
                            "DEBUG",
                        )
                        annotate_step(
                            ctx, node, "static",
                            note=f"OK stable={elapsed_s:.1f}s diff={diff:.3f}",
                            region=(x, y, w, h),
                        )
                        return {}, ["yes"], ""
                else:
                    stable_since_ts = None
                    annotate_step(
                        ctx, node, "moving",
                        note=f"diff={diff:.4f} >= {dt:.4f}",
                        region=(x, y, w, h),
                    )
            prev_roi = shot_bgr

            t_sleep = time.monotonic()
            while (time.monotonic() - t_sleep) < interval_s and getattr(ctx, "is_running", True):
                wait_if_paused(ctx)
                time.sleep(min(0.05, max(0.005, interval_s - (time.monotonic() - t_sleep))))

    _diff_str = f"{last_diff:.4f}" if last_diff is not None else "未采样"
    ctx.log(
        f"[if_static] 超时 {timeout_seconds:.1f}s 仍未静止 (last_diff={_diff_str}, stable_since={stable_since_ts}) -> no",
        "DEBUG",
    )
    annotate_step(
        ctx, node, "static",
        note=f"TIMEOUT last_diff={_diff_str}",
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
    if_sound 节点：系统播放/麦克风声音检测。
    - judge_mode: detect_sound(检测声音) / detect_silence(检测静音)
    - 失败兜底：soundcard → PyAudio，都不可用 WARN 走 no
    """
    import numpy as np

    wait_if_paused(ctx)
    p = node.params
    mode_raw = str(p.get("judge_mode", "detect_sound") or "detect_sound")
    if mode_raw in ("detect_silence", "is_silent", "silence", "detect_mute"):
        mode = "detect_silence"
    else:
        mode = "detect_sound"
    thr_db = float(p.get("threshold_db", -30.0) or -30.0)
    sample_ms = max(50, int(p.get("sample_ms", 500) or 500))
    timeout_s = max(0.0, float(p.get("timeout_seconds", 5.0) or 0.0))
    device = str(p.get("device", "default_loopback") or "default_loopback")
    debug_viz = bool(p.get("debug_visual", False))
    sr = 44100

    t0 = time.time()
    timeout_cap_s = timeout_s if timeout_s > 0 else (86400.0 * 365)
    sample_s = sample_ms / 1000.0
    note = ""
    # 检测静音：需要累计稳定静音时长，这里简化为「单次采样期间全程静音」即判定；否则一直轮询
    silence_consec_s = 0.0  # detect_silence 专用累计
    last_sample_end = t0

    while (time.time() - t0) < timeout_cap_s and ctx.is_running:
        wait_if_paused(ctx)
        chunk = _capture_sound_blocking(device, sample_s, sr)
        if chunk is None:
            ctx.log(
                "[if_sound] 音频采集库未安装或设备不可用，自动走 no。请先执行："
                " pip install soundcard pyaudio "
                "（PyAudio 安装失败可尝试： pip install pipwin && pipwin install pyaudio ）",
                "WARN",
            )
            annotate_step(ctx, node, "sound", note="采集库不可用→no")
            return {"out_level_db": None}, ["no"], ""

        # 计算峰值dB和均值dB
        peak = float(np.max(np.abs(chunk)) + 1e-8)
        mean = float(np.mean(np.abs(chunk)) + 1e-8)
        peak_db = float(20.0 * np.log10(peak))
        mean_db = float(20.0 * np.log10(mean))
        out_db = peak_db

        yes = False
        if mode == "detect_sound":
            # 检测声音：只要出现一次峰值/均值超过阈值就算满足
            yes = (peak_db >= thr_db) or (mean_db >= thr_db)
            note = f"[{mode}] peak={peak_db:.1f}dB mean={mean_db:.1f}dB thr={thr_db:.0f}dB"
        else:  # detect_silence
            # 检测静音：整个采样期间 peak 必须低于阈值
            sample_is_silent = (peak_db < thr_db) and (mean_db < thr_db)
            now = time.time()
            dt_last = max(0.0, now - last_sample_end)
            last_sample_end = now
            if sample_is_silent:
                silence_consec_s += sample_s
            else:
                silence_consec_s = 0.0
            # 只要累计静音时长超过一次采样窗口就判定一次
            yes = sample_is_silent and silence_consec_s >= sample_s
            note = (
                f"[{mode}] peak={peak_db:.1f}dB mean={mean_db:.1f}dB thr={thr_db:.0f}dB "
                f"consec_silence={silence_consec_s:.2f}s"
            )

        if debug_viz:
            wave = _ascii_waveform(chunk, peak_marker_db=thr_db)
            ctx.log(f"[if_sound waveform]\n{wave}\n判定={note} yes={yes}", "DEBUG")

        if yes:
            mode_cn = "检测声音" if mode == "detect_sound" else "检测静音"
            ctx.log(f"[if_sound] ✅ {mode_cn} 满足条件：{note}", "DEBUG")
            annotate_step(ctx, node, "sound", note=f"YES[{mode_cn}] peak={peak_db:.1f}dB")
            return {"out_level_db": round(out_db, 2)}, ["yes"], ""

        # 轮询间隔（避免死循环占CPU）
        t_slp = time.time()
        while (time.time() - t_slp) < 0.05 and ctx.is_running:
            wait_if_paused(ctx)
            time.sleep(0.01)

    # 超时
    t_used_ms = int((time.time() - t0) * 1000)
    mode_cn = "检测声音" if mode == "detect_sound" else "检测静音"
    ctx.log(
        f"[if_sound] ⏱ 超时 实际={t_used_ms}ms(要求≤{int(timeout_s*1000)}ms)，"
        f"模式=[{mode_cn}]，条件未满足 → no（last={note}）",
        "DEBUG",
    )
    annotate_step(ctx, node, "sound", note=f"TIMEOUT[{mode_cn}] last={note[:60]}")
    return {"out_level_db": None}, ["no"], ""


# ============================================================
# 节点类型 -> 执行函数 映射
# ============================================================
EXECUTORS = {
    "Start": execute_Start,
    "End": execute_End,
    "Loop": execute_Loop,
    "Wait": execute_Wait,
    "VarSwitch": execute_VarSwitch,
    "Sequence": execute_Sequence,
    "Notify": execute_Notify,
    "OpenApp": execute_OpenApp,
    "BindWindow": execute_BindWindow,
    "FindImage": execute_FindImage,
    "Mouse": execute_Mouse,
    "PressKey": execute_PressKey,
    "SetVariable": execute_SetVariable,
    "IfImageDetect": execute_IfImageDetect,
    "StillnessDetect": execute_StillnessDetect,
    "SoundDetect": execute_SoundDetect,
    "Clipboard": execute_Clipboard,
    "Command": execute_Command,
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
# 辅助：断点命中（Step 模式 OR 手动 breakpoint=True）→ 进入节点开头立即暂停
# ============================================================
def _hit_node_breakpoint(ctx: ExecutionContext, node: Node, runner: "BlueprintRunner"):
    """
    统一在 EXECUTORS 执行前调用（进入节点开头先停住，和 IDE 调试一致）。
    有两种停住的情况（前者优先级高，都不生效则不停，直接返回）：

    1️⃣ Step 模式：用户上次停住时，不是点▶继续，而是点了⏭下一步 → ctx.step_mode=True
       → 进入**任何节点**（哪怕该节点完全没设置 breakpoint）都自动暂停，实现「点下一步松开断点后，
         后续节点全部自动带断点效果」。
       → 暂停后，如果用户再次点⏭下一步：step_mode 仍保持 True → 取消暂停继续跑 → 下一个节点
         进入时又被这个分支 catch 住 → 再停，直到用户点▶继续（toggle_pause 会把 step_mode=False）
       → 暂停后，如果用户点▶继续：toggle_pause 把 step_mode=False → 退出 Step 模式，之后走情况2。

    2️⃣ 手动断点：用户通过节点右键切为 breakpoint=True 的节点 → 进入即停。
       → step_mode=False 时才走这里（否则用户点⏭下一步松开断点后还是会停在当前节点，不合理）
       → 命中一次不清空，用户右键取消才消失（循环重复场景持续生效）。

    任何情况停住都会打 WARN 级日志 + annotate_step 保存调试截图/证据 + is_paused=True + 发
    pause_changed(True) Signal 给 UI，让暂停按钮文字切为「继续」、下一步按钮变可点。
    """
    # ---------- 情况 1：Step 模式 → 什么节点都停 ----------
    if bool(getattr(ctx, "step_mode", False)):
        try:
            annotate_step(ctx, node, "step_mode", note="Step模式: 进入每个节点自动暂停")
        except Exception:
            pass
        ctx.log(
            f"[Step] ⏭ Step模式：进入节点 id={node.id[:8]} type={node.type} → 自动暂停（点⏭下一步→步过到下节点 / 点▶继续→退出Step模式，后续只手动断点=True才停）",
            "WARN",
        )
        ctx.is_paused = True
        try:
            runner.pause_changed.emit(True)
        except Exception:
            pass
        # wait_if_paused 内部循环检查 is_paused / is_running
        #  - 用户点⏭下一步：step_mode 保持 True，is_paused=False → wait 返回 → 执行完当前节点 → 下节点开头又回到这里 Step 分支
        #  - 用户点▶继续：toggle_pause 把 step_mode=False + is_paused=False → wait 返回 → 后面只走情况2
        wait_if_paused(ctx)
        # 如果 wait 返回后用户在暂停时点了停止（is_running=False）→ 上层立即结束，不再继续
        return

    # ---------- 情况 2：手动断点（node.breakpoint=True）----------
    if not bool(getattr(node, "breakpoint", False)):
        return
    try:
        annotate_step(ctx, node, "breakpoint", note="命中断点")
    except Exception:
        pass
    ctx.log(
        f"[Breakpoint] 📍 命中断点 id={node.id[:8]} type={node.type} → 已进入暂停（按 Ctrl+F10 继续 / Ctrl+F11 停止 / 工具栏⏭下一步→进入Step模式每节点自动停）",
        "WARN",
    )
    ctx.is_paused = True
    try:
        runner.pause_changed.emit(True)
    except Exception:
        pass
    wait_if_paused(ctx)


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
    # 通知节点：非阻塞式应用内 Toast 提示（title, body, level, duration_sec）
    # 即使 show_popup=False（系统弹窗关了）也会发，保证关掉系统弹窗时用户至少能看到主窗口的浮动提示
    notify_toast = Signal(str, str, str, float)
    # 当前执行节点变更（每次进入一个新节点前发射，参数: node_id, node_type）
    #  供搜索栏「📍下一断点/定位执行点」按钮标签实时显示 📍执行中: TypeX id=xx
    #  以及命中断点/暂停后快速跳画布
    current_node_changed = Signal(str, str)

    def __init__(self, graph: BlueprintGraph, parent=None, initial_hwnd=None):
        super().__init__(parent)
        self._graph = graph
        self._ctx: Optional[ExecutionContext] = None
        self._initial_hwnd = initial_hwnd

    def request_stop(self):
        if self._ctx is not None:
            self._ctx.is_running = False
            # 清除暂停状态，确保 wait_if_paused 循环能立即退出
            self._ctx.is_paused = False

    def request_pause(self, pause: bool):
        if self._ctx is not None:
            self._ctx.is_paused = bool(pause)
            # 手动置暂停/继续时，如果是从暂停→继续（pause=False），且当前在 Step 模式 → 也同步清掉 Step 模式
            if (not pause) and bool(getattr(self._ctx, "step_mode", False)):
                self._ctx.step_mode = False
                try:
                    self._ctx.log("[Step] ▶ 手动取消暂停 → 已退出 Step 模式（后续只认手动 breakpoint=True 的节点才会停）", "INFO")
                except Exception:
                    pass
            self.pause_changed.emit(self._ctx.is_paused)

    def toggle_pause(self) -> bool:
        """暂停/继续切换（对应工具栏⏸暂停/▶继续按钮 或 Ctrl+F10 快捷键）。
        【和 Step 模式联动】：如果当前处于 Step 模式（之前点过⏭下一步）且用户现在点的是「▶继续」
        （即 paused=True → False），把 step_mode=False 清掉，退出 Step 模式，
        后续不再「进入每节点自动停」，只认用户手动设置的 breakpoint=True 节点才会停。"""
        if self._ctx is None:
            return False
        # 停止中或已停止时不允许切换暂停状态
        if not bool(getattr(self._ctx, "is_running", True)):
            return False
        cur = not bool(self._ctx.is_paused)
        # 暂停 → 继续：退出 Step 模式（后续不再每节点自动停）
        if (not cur) and bool(getattr(self._ctx, "step_mode", False)):
            self._ctx.step_mode = False
            try:
                self._ctx.log("[Step] ▶ 点继续恢复执行 → 已退出 Step 模式（后续只认手动 breakpoint=True 的节点才会停）", "INFO")
            except Exception:
                pass
        self._ctx.is_paused = cur
        self.pause_changed.emit(cur)
        return cur

    def request_step(self) -> bool:
        """『下一步』调试：语义为【打开/保持 Step 模式】，后续进入每个节点都自动暂停。
        只在「暂停状态」下有效（命中断点暂停 / 手动⏸暂停 / Step 模式自动停 都算）：
        1. 置 step_mode=True（打开/保持 Step 模式：后面进入每一个节点，不管它自己设没设 breakpoint，
           在 _hit_node_breakpoint 的 Step 分支都会自动 is_paused=True 停住，直到用户点「▶继续」清 step_mode）
        2. 临时置 is_paused=False → wait_if_paused 返回，执行当前节点 → 下一个节点开头又 Step 自动停
        返回 True=请求成功；False=当前 ctx 不存在 / 没在运行。"""
        if self._ctx is None:
            return False
        if not bool(getattr(self._ctx, "is_running", True)):
            return False
        self._ctx.step_mode = True
        self._ctx.is_paused = False
        # UI 更新：暂停按钮从▶继续 → 切回⏸暂停，下一步⏭灰禁用（下一次 Step 停住才再次亮）
        try:
            self.pause_changed.emit(False)
        except Exception:
            pass
        self._ctx.log(
            f"[Step] ⏭ 请求下一步 → 进入/保持 Step 模式：后续每个节点进入时都会自动暂停。点▶继续可随时退出 Step 模式",
            "WARN",
        )
        return True

    @property
    def is_paused(self) -> bool:
        return bool(self._ctx.is_paused) if self._ctx is not None else False

    @property
    def current_node_id(self) -> Optional[str]:
        """当前正在/刚执行完的节点 ID：None 表示还没开始执行或已结束。"""
        if self._ctx is None:
            return None
        val = getattr(self._ctx, "current_node_id", None)
        return val if isinstance(val, str) and len(val) > 0 else None

    @property
    def current_node_type(self) -> Optional[str]:
        """拿到当前执行节点的 type（若在 graph 中找到），供 UI 标签显示节点类型。"""
        nid = self.current_node_id
        if nid is None:
            return None
        if self._graph is None or self._graph.nodes is None:
            return None
        nd = self._graph.nodes.get(nid)
        return nd.type if nd is not None else None

    # ---------------- 主循环 ----------------
    def run(self):
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # ---------- ★ 线程级 COM 初始化（必须在所有工作线程一进来第一时间做！
        # Windows OLE 剪贴板（OleSetClipboard / OleGetClipboard）、
        # 屏幕取色、UIA 自动化、输入法等都依赖 COM。后台 QThread
        # 默认不会初始化 COM，会导致 Clipboard 节点报 COM error 0x800401f0
        # （CO_E_NOTINITIALIZED = CoInitialize has not been called）。
        _com_inited = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _com_inited = True
        except Exception:
            # 极少数情况（如 pythoncom 不可用）失败不影响主流程，
            # 只是剪切板等 COM 依赖功能不可用，日志会在具体节点提示。
            _com_inited = False

        # ---------- 运行日志落盘：debug/runs/{id}/run_{id}.log ----------
        run_dir = os.path.join(debug_dir(), "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        log_path = os.path.join(run_dir, f"run_{run_id}.log")
        log_fp = None
        try:
            log_fp = open(log_path, "a", encoding="utf-8")

            def _write_log(msg: str, level: str = "INFO"):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                line = f"[{ts}] [{level}] {msg}\n"
                if log_fp is not None:
                    try:
                        log_fp.write(line)
                        log_fp.flush()
                    except Exception:
                        pass
                self.log_message.emit(msg, level)

            _write_log(f"[Runtime] 开始 run_id={run_id}", "INFO")
        except Exception as _e:
            _write_log = lambda m, l="INFO": self.log_message.emit(m, l)
            self.log_message.emit(f"[Runtime] 创建运行日志文件失败: {_e}", "WARN")

        ctx = ExecutionContext(run_id=run_id)
        ctx.log_fn = _write_log
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

        # 初始化目标程序（右上角下拉框选中的 HWND；None=全屏）
        init_hwnd = getattr(self, "_initial_hwnd", None)
        try:
            if init_hwnd:
                win_rect, win_title = _apply_target_hwnd(ctx, int(init_hwnd), 0, 0)
                hwnd_hex = f"0x{int(init_hwnd):08X}" if int(init_hwnd) else "None"
                _write_log(
                    f"[Runtime] 初始目标程序来自主界面下拉框：HWND={hwnd_hex}  "
                    f"title='{(win_title or '')[:32]}'  rect={win_rect}",
                    "INFO",
                )
            else:
                _apply_target_hwnd(ctx, None, 0, 0)
                _write_log("[Runtime] 初始目标程序：未指定（全屏模式），请用 BindWindow 节点或主界面下拉框设置。", "INFO")
        except Exception as _ie:
            _write_log(f"[Runtime] 应用初始目标程序失败（不影响后续）: {_ie}", "WARN")

        try:
            self._run_graph(ctx)
            _write_log(
                f"[Runtime] 完成：步骤 {ctx.step_counter} 个 | run_id={run_id}",
                "SUCCESS" if ctx.is_running else "WARN",
            )
            self.highlight_clear.emit()
            self.finished_ok.emit()
        except Exception as e:
            tb = traceback.format_exc()
            _write_log(f"[Runtime] 执行异常: {e}\n{tb}", "ERROR")
            self.highlight_clear.emit()
            self.finished_err.emit(str(e))
        finally:
            # ---------- ★ 对应 run() 开头的 CoInitialize：必须配对调用 ----------
            if _com_inited:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            try:
                if log_fp is not None:
                    log_fp.close()
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
        # UI 高亮（硬约束 §4 Signal 回传） + 记录当前执行节点ID + 发 current_node_changed
        self.highlight_node.emit(node.id)
        self.progress_step.emit(ctx.step_counter + 1, node.type)
        try:
            ctx.current_node_id = node.id
            self.current_node_changed.emit(node.id, node.type)
        except Exception:
            pass

        # 1. 解析 DATA 输入
        inputs = _resolve_data_inputs(graph, ctx, node)

        # 2. 日志
        ctx.log(f"[Node▶] {node.type}  id={node.id[:8]}", "INFO")

        # 3. 断点命中检查（主执行入口）
        _hit_node_breakpoint(ctx, node, self)

        # 4. 执行
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

        if not ctx.is_running:
            return

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
