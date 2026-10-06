import json
import os
import tempfile
import threading
import time
import traceback
import contextlib
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from PySide6.QtCore import QThread, Signal

from ..core.models import (
    BlueprintGraph,
    ExecutionContext,
    Node,
    PortType,
)
from .debug_annotator import annotate_step, bump_step
from .input_actions import InputActions
from .screen_capture import ScreenCapture
from .template_matcher import TemplateMatcher
from ...config.app_paths import debug_dir


# ============================================================
# 组件递归保护：组件是文件引用（A 可调 B、B 可调 A），没有上限就会无限递归
# 直接把整个运行时撑爆。超过这个层数一律判为「疑似循环引用」并中止该分支。
# 32 层对正常业务足够深（组件嵌套很少超过 5 层），又远小于 Python 递归上限。
# ============================================================
MAX_COMPONENT_DEPTH = 32


# ============================================================
# 剪贴板互斥锁：剪贴板是「系统级单例」资源，同一时刻只能有一个写入方。
# 并行分支若同时「写剪贴板 → Ctrl+V」，后写入者会覆盖前者，表现为多条分支
# 粘贴出同一份内容（实测：3 条分支全部粘出第 3 条分支的文本）。
# 所以所有碰剪贴板的操作（按键节点粘贴、剪贴板节点读/写/清空）都要持有这把锁，
# 把「写剪贴板 → 使用/读回」收敛成一段不可分割的临界区。
# 锁只保证互斥，不改变各分支自己的文本内容，也不改变分支之间的先后（谁先拿到锁谁先粘）。
# ============================================================
_CLIPBOARD_LOCK = threading.Lock()


# ============================================================
# 执行路径（供编辑器把画布「跟随」进正在执行的子画布）
# ------------------------------------------------------------
# 子画布里的节点 id 是「每张图各自编号」的（父图有 1，循环体里第一个节点也叫 1），
# 编辑器只看一个裸 id 无法知道它属于哪一层 —— 不带上路径，画布就只能停在当前层高亮，
# 钻不进循环体 / 并行体。所以运行时把「容器链」一起告诉编辑器：
#     根图 [] → 循环体 [LoopId] → 循环体里的并行体 [LoopId, ParId]
# 用 thread-local 维护，并行分支各线程互不干扰（线程池里的线程由 walker 重新 seed）。
# ============================================================
_exec_path_tls = threading.local()


def _exec_path_get():
    """本线程当前的容器链（list）；None = 不可导航（如组件子蓝图内部）。"""
    st = getattr(_exec_path_tls, "stack", None)
    if not st:
        return []
    if any(x is None for x in st):
        return None
    return list(st)


def _exec_path_set(path):
    """walker 进入某张图时 seed 本线程的容器链（path=None → 标记为不可导航）。返回旧值。"""
    prev = getattr(_exec_path_tls, "stack", None)
    _exec_path_tls.stack = [None] if path is None else list(path)
    return prev


def _exec_path_restore(prev):
    """还原 seed 前的容器链（与 _exec_path_set 配对使用）。"""
    _exec_path_tls.stack = prev


def _exec_path_of(graph):
    """某张（子）图的容器链；未标记（组件子蓝图等）→ None（编辑器不做跟随）。"""
    p = getattr(graph, "_runtime_path", None)
    return list(p) if isinstance(p, list) else None


def _exec_child_path(node):
    """以本线程当前容器链为父，算出一个子画布容器的容器链；父级不可导航 → None。"""
    cur = _exec_path_get()
    if cur is None:
        return None
    return cur + [str(node.id)]


# ============================================================
# 辅助：端口/连线 解析
# ============================================================
def _resolve_data_inputs(graph: BlueprintGraph, ctx: ExecutionContext, node: Node, id_prefix: str = "") -> Dict[str, Any]:
    """
    解析节点所有 DATA 输入端口的值：
      - 若端口被连线：取上游 last_outputs（注意：同一INPUT端口多根线只会取遍历顺序第一条，其余静默忽略并打WARN）
      - 否则：取 node.params 同名值

    id_prefix：子图内执行时传入子图输出前缀（"{sub_prefix}::"），使子图内 DATA 连线能取到上游值；
    主图执行时不传（默认空），用原始节点 id 查找。
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
            upstream = ctx.last_outputs.get(id_prefix + found_edge.from_node, {})
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
            runner.highlight_node.emit(curr_id, _exec_path_of(graph))
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
        bump_step(ctx)

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
    """蓝图起点。若本蓝图是「组件」被调用（有 num_params 入参端口），
    从 ctx 变量 __arg_0... 读取调用方传入的值，输出到 param_0... 端口供下游连线取用。"""
    outputs: Dict[str, Any] = {}
    n = int(node.params.get("num_params", 0) or 0)
    for i in range(n):
        outputs[f"param_{i}"] = ctx.variables.get(f"__arg_{i}")
    return outputs, ["out_exec"], "开始"


def execute_End(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """蓝图终点。若本蓝图是「组件」（有 num_returns 返回值端口），
    把连到 ret_0... 端口的输入值写入 ctx 变量 __ret_0...，供调用方 Component 节点读回。"""
    n = int(node.params.get("num_returns", 0) or 0)
    for i in range(n):
        ctx.variables[f"__ret_{i}"] = inputs.get(f"ret_{i}")
    return {}, [], "结束"


def _subgraph_from_dict(sub: Dict[str, Any], name: str = "__subgraph__") -> "BlueprintGraph":
    """把一个「子图 dict」包成临时 BlueprintGraph，复用 _resolve_data_inputs / _exec_out_edges / _walk_branch_until_end。

    子图 dict 结构对齐 BlueprintGraph.to_dict()：{"nodes": {...}, "links": [...]}
    """
    d = {"nodes": sub.get("nodes", {}), "links": sub.get("links", []), "metadata": {"name": name}}
    return BlueprintGraph.from_dict(d)


def _run_subgraph_until_control(
    ctx: ExecutionContext,
    sub: "BlueprintGraph",
    runner,
    entry_node_id: str,
) -> str:
    """同步执行一张子图，从 entry_node_id 出发走完整条执行流，直到：
      - 遇到 Continue 节点 → 返回 "continue"
      - 遇到 Break 节点 → 返回 "break"
      - 执行流自然走到终点（无下游）→ 返回 "end"
      - 运行被停止 → 返回 "stop"

    返回的控制信号供 execute_Loop 决定「继续下一轮 / 跳出循环 / 正常结束本轮」。

    子图内节点 id 是子图内部命名空间（与父图隔离）；输出写进 ctx.last_outputs 时
    加前缀 {sub_prefix}::{node_id} 避免与父图节点 id 撞车（子图节点 id 可能是纯数字，与父图同值）。
    """
    sub_prefix = getattr(sub, "_runtime_prefix", "__sub__")
    # 注入 graph/runner 引用，供子图内 Sequence 等需要递归 walk 的节点使用
    saved_graph = getattr(ctx, "_graph_ref", None)
    saved_runner = getattr(ctx, "_runner_ref", None)
    # seed 本线程容器链 = 这张子图的路径：子图内如果再套容器，它的路径要接在这一层后面
    saved_path = _exec_path_set(_exec_path_of(sub))
    ctx._graph_ref = sub
    ctx._runner_ref = runner
    try:
        curr_id = entry_node_id
        safety = 50000
        while safety > 0 and ctx.is_running:
            safety -= 1
            node = sub.nodes.get(curr_id)
            if node is None:
                return "end"

            wait_if_paused(ctx)
            # UI 高亮（子图节点）——尽量复用 runner 信号，失败则静默
            try:
                runner.highlight_node.emit(curr_id, _exec_path_of(sub))
                runner.progress_step.emit(ctx.step_counter + 1, node.type)
            except Exception:
                pass
            try:
                ctx.current_node_id = curr_id
                runner.current_node_changed.emit(curr_id, node.type)
            except Exception:
                pass

            inputs = _resolve_data_inputs(sub, ctx, node, id_prefix=f"{sub_prefix}::")
            bump_step(ctx)
            ctx.log(f"[Node▶] {node.type}  id={curr_id[:8]} [子图]", "INFO")
            fn = EXECUTORS.get(node.type)
            try:
                if fn is None:
                    outputs, next_ports, _note = {}, ["out_exec"], ""
                else:
                    outputs, next_ports, _note = fn(ctx, node, inputs)
            except Exception as e:
                tb = traceback.format_exc()
                ctx.log(f"[Node] 子图内执行异常 {node.type}: {e}\n{tb}", "ERROR")
                outputs, next_ports = {}, []

            # 控制节点（Continue/Break）在 execute_* 里直接 return 控制信号——这里用 next_ports 空列表 + 特殊标记识别
            # 更稳的方式：execute_Continue/Break 通过 ctx.runtime_attrs 传递信号
            ctrl = ctx.runtime_attrs.pop(f"_sub_control::{sub_prefix}", None)
            if ctrl is not None:
                return ctrl

            # 保存输出（带前缀，避免与父图撞车）
            ctx.set_outputs(f"{sub_prefix}::{curr_id}", outputs or {})

            if not ctx.is_running:
                return "stop"

            if not next_ports:
                return "end"

            pname = next_ports[0]
            outs = _exec_out_edges(sub, curr_id, pname)
            if not outs:
                return "end"
            curr_id, _curr_in = outs[0]

        return "stop" if not ctx.is_running else "end"
    finally:
        # 恢复父图引用（若期间没有被其它递归层改写）
        if saved_graph is not None:
            ctx._graph_ref = saved_graph
        else:
            ctx._graph_ref = None
        if saved_runner is not None:
            ctx._runner_ref = saved_runner
        else:
            ctx._runner_ref = None
        _exec_path_restore(saved_path)


def execute_Loop(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    infinite = bool(node.params.get("infinite", False))
    max_it = int(node.params.get("max_iterations", 5) or 5)
    interval = param_num(node.params.get("interval_sec"), 1.0, float, lo=0.0)
    if max_it <= 0:
        max_it = 5
    counter = ctx.loop_counters.get(node.id, 0)

    # ---------- 新模式：loop_body 子图容器（不靠拉线） ----------
    loop_body = node.params.get("loop_body")
    if isinstance(loop_body, dict) and loop_body.get("nodes"):
        return _execute_loop_body_container(ctx, node, loop_body, infinite, max_it, interval, counter)

    # ---------- 旧模式：loop_body 端口拉线（向后兼容，蓝图未用子图容器时） ----------
    if infinite:
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
        return {}, ["loop_body"], ""
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
        return {}, ["loop_body"], ""
    else:
        ctx.log(
            f"[Loop] {node.id[:6]} 达到 max_iterations={max_it}，退出循环。",
            "WARN",
        )
        annotate_step(ctx, node, "loop", note=f"EXIT: hit max={max_it}")
        ctx.loop_counters.pop(node.id, None)
        return {}, ["out_exit"], ""


def _execute_loop_body_container(
    ctx: ExecutionContext,
    node: Node,
    loop_body: Dict[str, Any],
    infinite: bool,
    max_it: int,
    interval: float,
    counter: int,
):
    """循环体子图容器：递归执行 loop_body 子图，直到 Break / 达到次数 / 自然结束。

    - 每次进入子图前，把 ctx._graph_ref 临时切到子图（Sequence 等依赖它），执行完恢复。
    - 子图内 Continue → 立即进入下一轮（不跑后续节点）。
    - 子图内 Break → 立即退出整个循环，走 out_exit。
    - 子图自然走到终点 → 计一次迭代，进入下一轮，直到 max_iterations。
    """
    graph = getattr(ctx, "_graph_ref", None)
    runner = getattr(ctx, "_runner_ref", None)
    if graph is None or runner is None:
        msg = "[Loop] ctx 缺少 graph/runner 引用，无法执行循环体子图。"
        ctx.log(msg, "ERROR")
        annotate_step(ctx, node, "loop", note="FAIL: 缺少graph_ref/runner_ref")
        return {}, ["out_exit"], msg

    sub = _subgraph_from_dict(loop_body, name=f"{node.id}::loop_body")
    sub._runtime_prefix = node.id
    # 容器链：编辑器靠它把画布钻到正在执行的这一层（见文件头「执行路径」说明）
    sub._runtime_path = _exec_child_path(node)

    # 找子图里的起始节点：优先 Start，否则第一个节点
    entry_id = None
    for nid, n in sub.nodes.items():
        if n.type == "Start":
            entry_id = nid
            break
    if entry_id is None and sub.nodes:
        entry_id = next(iter(sub.nodes.keys()))
    if entry_id is None:
        ctx.log(f"[Loop] {node.id[:6]} 循环体子图为空，直接退出。", "WARN")
        return {}, ["out_exit"], ""

    def _interval_wait():
        if interval > 0 and ctx.is_running:
            t0 = time.time()
            while ctx.is_running and (time.time() - t0) < interval:
                wait_if_paused(ctx)
                time.sleep(0.05)

    # 执行子图 Start 节点本身（Start 无输入，产出 out_exec）
    start_node = sub.nodes.get(entry_id)
    if start_node is not None and start_node.type == "Start":
        # 直接找到 Start 的下游作为真正入口
        outs = _exec_out_edges(sub, entry_id, "out_exec")
        if outs:
            entry_id, _ = outs[0]
        else:
            # Start 无下游，循环体只有 Start，直接判空结束
            ctx.log(f"[Loop] {node.id[:6]} 循环体只有 Start 无内容。", "WARN")
            return {}, ["out_exit"], ""

    iteration = counter
    while ctx.is_running:
        if infinite:
            ctx.loop_counters[node.id] = iteration + 1
        else:
            if iteration >= max_it:
                ctx.log(f"[Loop] {node.id[:6]} 达到 max_iterations={max_it}，退出循环。", "WARN")
                annotate_step(ctx, node, "loop", note=f"EXIT: hit max={max_it}")
                ctx.loop_counters.pop(node.id, None)
                return {}, ["out_exit"], ""
            ctx.loop_counters[node.id] = iteration + 1

        _interval_wait()
        wait_if_paused(ctx)
        annotate_step(ctx, node, "loop", note=f"container i={iteration+1}" + ("/∞" if infinite else f"/{max_it}"))
        ctx.log(f"[Loop] {node.id[:6]} 循环体子图 i={iteration+1} 开始", "DEBUG")

        ctrl = _run_subgraph_until_control(ctx, sub, runner, entry_id)
        if ctrl == "break":
            ctx.log(f"[Loop] {node.id[:6]} 循环体内 Break → 跳出循环。", "INFO")
            annotate_step(ctx, node, "loop", note="Break → out_exit")
            ctx.loop_counters.pop(node.id, None)
            return {}, ["out_exit"], ""
        if ctrl == "stop":
            return {}, [], "运行被停止"
        # ctrl == "continue" 或 "end"：进入下一轮
        iteration += 1

    return {}, [], ""


def execute_Continue(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """循环体子图专用：立即结束本轮、开始下一轮。"""
    sub_prefix = getattr(getattr(ctx, "_graph_ref", None), "_runtime_prefix", "__sub__")
    ctx.runtime_attrs[f"_sub_control::{sub_prefix}"] = "continue"
    ctx.log(f"[Continue] {node.id[:6]} → 下一轮循环", "DEBUG")
    return {}, [], ""


def execute_Break(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """循环体子图专用：立即结束整个循环。"""
    sub_prefix = getattr(getattr(ctx, "_graph_ref", None), "_runtime_prefix", "__sub__")
    ctx.runtime_attrs[f"_sub_control::{sub_prefix}"] = "break"
    ctx.log(f"[Break] {node.id[:6]} → 跳出循环", "DEBUG")
    return {}, [], ""


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
        min_s = param_num(p.get("min_seconds"), 1.0, float, lo=0.0, hi=3600.0)
        max_s = max(min_s, param_num(p.get("max_seconds"), 3.0, float, lo=0.0, hi=3600.0))
        sec = random.uniform(min_s, max_s)
        mode_txt = f"随机[{min_s:g}~{max_s:g}]"
    else:
        sec = param_num(p.get("seconds"), 1.0, float, lo=0.0, hi=3600.0)
        mode_txt = "固定"

    note = f"{mode_txt} 等待 {sec:.3f}s"
    ctx.log(f"[Wait] {mode_txt} sleep {sec:.3f}s", "INFO")
    t0 = time.time()
    while ctx.is_running and (time.time() - t0) < sec:
        wait_if_paused(ctx)
        time.sleep(0.05)
    wait_if_paused(ctx)
    annotate_step(ctx, node, "wait", note=note)
    return {}, ["out_exec"], ""


# ============================================================
def execute_OpenApp(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    path_raw = str(node.params.get("path", "") or "").strip()
    args_raw = str(node.params.get("args", "") or "")
    working_dir_raw = str(node.params.get("working_dir", "") or "").strip()
    wait_sec = param_num(node.params.get("wait_sec_after_launch"), 2.0, float, lo=0.0)

    if not path_raw:
        msg = "[OpenApp] path 为空，未启动任何程序。"
        ctx.log(msg, "WARN")
        annotate_step(ctx, node, "open_app", note="FAIL(path空)")
        return {}, ["fail"], msg
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
            return {}, ["out"], ""
        else:
            ctx.log(f"[OpenApp] FAIL: {msg}", "WARN")
            annotate_step(ctx, node, "open_app", note=f"FAIL: {str(msg)[:60]}")
            return {}, ["fail"], str(msg or "启动失败")
    except Exception as e:
        tb = traceback.format_exc()
        ctx.log(f"[OpenApp] 异常: {e}\n{tb}", "ERROR")
        annotate_step(ctx, node, "open_app", note=f"Exception: {type(e).__name__}")
        return {}, ["fail"], str(e)


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
    return_all: bool = False,
):
    """三维搜索：title子串 / exe文件名(小写含) / class_name 精确等于（不区分大小写）。
    三个维度全部启用：仅当对应字段非空时才参与匹配评分。

    return_all=False（默认）返回评分最高的单个 hwnd；
    return_all=True 返回按评分降序的全部 hwnd 列表（供 FindImage 多候选轮询消歧用）。
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
        # 真实可见性：IsWindowVisible + 非 DWM cloaked + rect 不在屏幕外。
        # 只查 IsWindowVisible 会把最小化(-32000 垃圾位)/零尺寸/cloaked 的同名辅助窗口
        # 也当成"可见"（浏览器有大量这种窗口），导致 exe/class 匹配选中它们 -> region 无效。
        try:
            _r = _wg.GetWindowRect(hwnd)
        except Exception:
            _r = None
        # 零尺寸窗口（IME/crashpad 等系统辅助窗口，从未渲染）直接排除——
        # 它们的 exe/cls 常与主窗口相同，留下只会靠 tie-break 随机抢占
        if _r is None or _r[2] - _r[0] <= 0 or _r[3] - _r[1] <= 0:
            continue
        try:
            vis = bool(_wg.IsWindowVisible(hwnd)) if prefer_visible else True
        except Exception:
            vis = False
        if vis:
            try:
                import ctypes as _ct
                _cloaked = _ct.c_int(0)
                _ct.windll.dwmapi.DwmGetWindowAttribute(
                    hwnd, 14, _ct.byref(_cloaked), _ct.sizeof(_cloaked))  # DWMWA_CLOAKED
                if _cloaked.value != 0:
                    vis = False
            except Exception:
                pass
            # 屏幕外最小化位（-32000 一族）：IsWindowVisible 可能为 True 但不可见
            if _r[0] <= -30000 or _r[1] <= -30000:
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
        # 可见加权（+50）：确保真实可见的主窗口稳赢同名隐藏/屏幕外辅助窗口
        # （原来只 +1，同分时按 pid/hwnd tie-break 会随机选中错误窗口）
        if vis:
            score += 50
        # 有标题加权（+10）：压过 IME/crashpad 等无标题系统辅助窗口
        # （浏览器进程下这些窗口与主窗口 exe/cls 相同，无标题时原先会同分随机选中）
        if (w_title or "").strip():
            score += 10
        # 匹配维度数优先
        score += dim_ok * 100
        scored.append((score, dim_ok, -w_pid, hwnd))
    if not scored:
        return [] if return_all else None
    scored.sort(reverse=True)
    if return_all:
        return [int(s[3]) for s in scored]
    return int(scored[0][3])


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
        features = []

    tolerance = max(1, int(params.get("feature_tolerance", 15) or 15))
    # 最少命中数允许为 0：0 表示「不要求任何特征命中」，此时特征列表为空也放行
    try:
        min_hits = int(params.get("feature_min_hits", 2))
    except (TypeError, ValueError):
        min_hits = 2
    min_hits = max(0, min_hits)
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

    # 阈值计算：只保留「验证模式不降 / 兜底模式降 0.1」这一层。
    # 灰度策略的「标定偏移」由 matcher 内部统一施加（find_image_gray 自己会调
    # get_calibrated_gray_threshold），这里**不能再标定一次** —— 否则 offset 被叠加两次，
    # 特征小图的阈值会被推到上限，出现「模板明明找对了、特征却验不过」的假失败。
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

    # ---------------------------------------------------------------
    # 没有任何可用特征图（列表为空 / 全部解码失败）
    #   判定口径与「特征命中数」保持一致：命中数(这里固定为 0) >= 最少命中数 才算通过。
    #   → 只有「验证模式 + 最少命中数=0」才放行（相当于明确表示不需要特征校验）；
    #     其余情况一律判失败，并给出可操作的提示，不静默降级。
    # ---------------------------------------------------------------
    if valid_count == 0:
        if (template_pos is not None and isinstance(template_pos, dict) and min_hits <= 0):
            tpl_left0 = int(template_pos.get("left", 0))
            tpl_top0 = int(template_pos.get("top", 0))
            tpl_w0 = int(template_pos.get("width", 0))
            tpl_h0 = int(template_pos.get("height", 0))
            ctx.log("[FindImage·特征] 验证模式：没有可用特征图，但「最少命中特征数」=0 → 视为无需特征校验，放行", "WARN")
            _cleanup()
            return (tpl_left0 + tpl_w0 // 2 + offset_x, tpl_top0 + tpl_h0 // 2 + offset_y, 1.0)
        ctx.log(
            f"[FindImage·特征] 没有可用特征图（列表为空或特征图解码失败）→ 判定失败。"
            f"请重新「✂ 从模板截取特征」；确实不需要特征校验时，把「最少命中特征数」设为 0，"
            f"或把「特征识别辅助」改为关闭。",
            "WARN",
        )
        _cleanup()
        return None

    if min_hits > valid_count:
        ctx.log(
            f"[FindImage·特征] 可用特征只有{valid_count}个 < 最少命中{min_hits}个 → 本次必然失败；"
            f"请调低「最少命中特征数」或补足特征。",
            "WARN",
        )

    def _match_one(fpath, search_region):
        """在指定区域内匹配单个特征，返回 pos dict 或 None。

        跟随主算法，且与主匹配的级联层数保持一致（不再出现「主算法两级、特征辅助只有一层」）：
          - template → 彩色单层
          - gray     → 灰度单层
          - hybrid   → 彩色 → 灰度 两级
        """
        try:
            if strategy == "gray":
                return ctx.matcher.find_image_gray(fpath, region=search_region, threshold=feat_threshold, fast_mode=True)
            pos = ctx.matcher.find_image(fpath, region=search_region, threshold=feat_threshold, fast_mode=True)
            if pos is None and strategy == "hybrid":
                pos = ctx.matcher.find_image_gray(fpath, region=search_region, threshold=feat_threshold, fast_mode=True)
            return pos
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
        if min_hits <= 0:
            ctx.log("[FindImage·特征] 「最少命中特征数」=0 → 验证模式不要求任何特征命中，本次校验直接放行（建议改回 ≥1）", "WARN")

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
    def _center_of(hit):
        """取命中项的屏幕中心（hit = (idx, left, top, w, h, score)）。"""
        return (hit[1] + hit[3] / 2.0, hit[2] + hit[4] / 2.0)

    def _tpl_center_of(feat_idx):
        """取特征在**模板坐标系**里的中心（feat_info 存的是左上角偏移）。"""
        rx_, ry_, fw_, fh_ = feat_info[feat_idx]
        return (rx_ + fw_ / 2.0, ry_ + fh_ / 2.0)

    t0 = time.time()
    while (time.time() - t0) < timeout_s and ctx.is_running:
        wait_if_paused(ctx)
        if not ctx.is_running:
            break

        # 1) 对每个特征在全屏（或指定 region）内独立匹配
        #    matcher 返回的是**绝对屏幕坐标**下的「左上角 + 宽高」（region 模式下已加过 region 偏移），
        #    因此这里保留左上角原始值，中心需要时现算——后面推算模板原点必须用左上角对左上角。
        hits = []  # [(feat_idx, left, top, w, h, score)]
        for i, fpath in enumerate(feat_paths):
            if fpath is None:
                continue
            fw_i, fh_i = feat_info[i][2], feat_info[i][3]
            fpos = _match_one(fpath, region)
            if fpos is not None:
                if isinstance(fpos, dict):
                    fl = int(fpos.get("left", 0)); ft = int(fpos.get("top", 0))
                    fwi = int(fpos.get("width", fw_i) or fw_i); fhi = int(fpos.get("height", fh_i) or fh_i)
                    raw_s = fpos.get("score")
                    fscore = float(raw_s) if isinstance(raw_s, (int, float)) else float(feat_threshold)
                elif isinstance(fpos, (list, tuple)) and len(fpos) >= 4:
                    fl, ft, fwi, fhi = int(fpos[0]), int(fpos[1]), int(fpos[2]), int(fpos[3])
                    fscore = float(feat_threshold)
                elif isinstance(fpos, (list, tuple)) and len(fpos) >= 2:
                    fl, ft, fwi, fhi = int(fpos[0]), int(fpos[1]), fw_i, fh_i
                    fscore = float(feat_threshold)
                else:
                    continue
                hits.append((i, fl, ft, fwi, fhi, fscore))

        # 至少要 1 个命中才能推算位置；需要交叉验证时至少 2 个（min(2, ...) 保持原语义：
        # 少于 2 个命中时无法做两两校准，直接等下一轮）
        if len(hits) < min(2, max(1, min_hits)):
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

        # 2) 计算缩放比例（自校准）：两边都用「中心到中心」的距离比对，
        #    这样两个特征尺寸不一样时也不会算歪（用左上角相减会差半个尺寸）
        scales = []
        for a in range(len(hits)):
            for b in range(a + 1, len(hits)):
                idx_a = hits[a][0]
                idx_b = hits[b][0]
                tax, tay = _tpl_center_of(idx_a)
                tbx, tby = _tpl_center_of(idx_b)
                sxa, sya = _center_of(hits[a])
                sxb, syb = _center_of(hits[b])
                dist_t = _math.sqrt((tbx - tax) ** 2 + (tby - tay) ** 2)
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
            if cap_w > 0 and cur_w > 0:
                scale = cur_w / cap_w
            else:
                scale = 1.0

        ctx.log(f"[FindImage·特征] 命中{len(hits)}个特征，scale={scale:.3f}（{len(scales)}对校准）", "DEBUG")

        # 3) 位置验证：以第一个命中为基准，验证其他特征
        #    容差比对的是「屏幕上的实际中心」与「按基准推算出的期望中心」之间的欧氏距离
        base_tcx, base_tcy = _tpl_center_of(hits[0][0])
        base_scx, base_scy = _center_of(hits[0])
        valid_hits = [hits[0]]
        for hit in hits[1:]:
            idx = hit[0]
            tcx, tcy = _tpl_center_of(idx)
            exp_x = base_scx + (tcx - base_tcx) * scale
            exp_y = base_scy + (tcy - base_tcy) * scale
            scx, scy = _center_of(hit)
            err = _math.sqrt((scx - exp_x) ** 2 + (scy - exp_y) ** 2)
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
        #    关系式：屏幕上特征的**左上角** = 模板左上角 + 特征在模板中的**左上角** × scale
        #    （左边必须用左上角，右边也必须用左上角；拿中心去减左上角偏移会整体偏「半个特征尺寸」）
        origins_x = []
        origins_y = []
        for hit in valid_hits:
            idx = hit[0]
            rx, ry, _, _ = feat_info[idx]
            origins_x.append(hit[1] - rx * scale)
            origins_y.append(hit[2] - ry * scale)
        origin_x = sum(origins_x) / len(origins_x)
        origin_y = sum(origins_y) / len(origins_y)

        if template_w > 0 and template_h > 0:
            # 模板中心 = 模板左上角 + 半个模板尺寸 × scale
            # （漏乘 scale 时，只要显示比例≠1，中心就会整体偏 (1-scale)/2 个模板尺寸）
            abs_cx = int(round(origin_x + template_w * scale / 2.0)) + offset_x
            abs_cy = int(round(origin_y + template_h * scale / 2.0)) + offset_y
        else:
            _scx, _scy = _center_of(valid_hits[0])
            abs_cx = int(round(_scx)) + offset_x
            abs_cy = int(round(_scy)) + offset_y

        avg_score = sum(h[5] for h in valid_hits) / len(valid_hits)
        ctx.log(f"[FindImage·特征] 匹配成功！{len(valid_hits)}/{len(feat_paths)}个特征通过，模板原点=({int(origin_x)},{int(origin_y)}) 中心=({abs_cx},{abs_cy}) score={avg_score:.2f}", "SUCCESS")
        _cleanup()
        return (abs_cx, abs_cy, avg_score)

    _cleanup()
    return None


# ---------------- 动作/识别节点 ----------------
def execute_FindImage(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """
    找图节点：
      - 匹配策略 match_strategy: hybrid(智能混合) / template(模板匹配) / gray(灰度匹配)
      - 超时 timeout_seconds：在此时间内循环重试
      - 滚动重试 scroll_dy：非0时，找图失败后将鼠标移到搜索区域中心滚动一次再重试
      - 找到后执行 after_action: none / click / double_click / right_click，带 offset (X,Y)
      - 出口：out_exec(执行，找没找到都走) + found(成功找到) / not_found(没有找到)；
              out_pos 输出命中中心坐标（未命中为 None）
      - 模板无效 -> 走 out_exec + not_found
      - range_mode=window 时目标窗口没定位到 -> 只走 err（不退化为全屏，也不走 out_exec）
    """
    wait_if_paused(ctx)
    # 1) 读参数
    template = node.params.get("template", "")
    # 支持临时PIL/缓存：优先 runtime_attrs["_pil_template"]
    pil_tpl: Optional[Image.Image] = None
    try:
        pil_tpl = getattr(node, "runtime_attrs", {}).get("_pil_template")  # type: ignore[union-attr]
    except Exception:
        pil_tpl = None
    # 找图范围模式：window=只在目标窗口内找图，fullscreen=在整屏上找图
    range_mode = str(node.params.get("range_mode", "window") or "window")
    threshold = float(node.params.get("threshold", 0.9) or 0.9)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.9
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
        ctx.log("[FindImage] 模板无效（路径不存在 & 无PIL缓存） -> 没有找到", "WARN")
        annotate_step(ctx, node, "find", note="ERROR: invalid template")
        return (
            {"out_pos": None},
            ["out_exec", "not_found"], "",
        )

    # 3) 目标窗口解析：window 模式必须先拿到窗口，找不到直接 err（不退化为全屏）
    targets = []  # [(hwnd, rect), ...] 评分降序的全部候选窗口
    if range_mode == "window":
        targets = _resolve_target_rects(ctx, node.params, tag="FindImage")
        if not targets:
            err = _target_err_msg(node.params, "FindImage")
            ctx.log(f"[FindImage] {err}", "ERROR")
            annotate_step(ctx, node, "find", note="ERROR: target window not found")
            return ({"out_pos": None}, ["err"], err)
    target = targets[0] if targets else None

    # 4) 超时空转 + 滚动重试 循环
    t0 = time.time()
    pos = None
    last_err = ""

    # 多候选窗口（exe/class 命中多个真实可见窗口）：每轮轮询一个候选，
    # 前置后在它的区域内找图，模板命中谁就算谁；单候选保持原行为（循环前一次性前置）
    multi = len(targets) > 1

    with _target_window_foreground(ctx, None if multi else target):
        try:
            # region 在窗口前置之后计算，避免最小化窗口恢复后 rect 失效
            region = None  # None=全屏；(x,y,w,h)=窗口区域
            win_origin = (0, 0)
            if target is not None:
                rect = target[1]  # (left, top, right, bottom)
                region = (int(rect[0]), int(rect[1]), int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1]))
                win_origin = (int(rect[0]), int(rect[1]))
                ctx.log(f"[FindImage] 目标窗口内找图 region={region}", "DEBUG")

            cand_i = 0  # 多候选窗口轮询指针（单候选时不起作用）
            while (time.time() - t0) < timeout_s and ctx.is_running:
                wait_if_paused(ctx)
                if not ctx.is_running:
                    break
                # 多候选：本轮轮到下一个候选窗口——前置后更新 region / win_origin
                if multi:
                    hwnd_c, rect_c = targets[cand_i % len(targets)]
                    cand_i += 1
                    _bring_window_to_front_safe(ctx, hwnd_c)
                    time.sleep(0.1)  # 等窗口切换完成再截图
                    region = (int(rect_c[0]), int(rect_c[1]),
                              int(rect_c[2]) - int(rect_c[0]), int(rect_c[3]) - int(rect_c[1]))
                    win_origin = (int(rect_c[0]), int(rect_c[1]))
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
                        else:  # hybrid：彩色 → 灰度，两级级联
                            pos = ctx.matcher.find_image(
                                str(use_path), region=region, threshold=threshold, fast_mode=True
                            )
                            if pos is None:
                                pos = ctx.matcher.find_image_gray(
                                    str(use_path), region=region, threshold=threshold, fast_mode=True
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
                # out_pos：window 模式输出相对目标窗口的坐标，fullscreen 模式输出绝对坐标
                if target is not None:
                    out_pos = (abs_cx - win_origin[0], abs_cy - win_origin[1])
                else:
                    out_pos = (abs_cx, abs_cy)
                # 动作：始终用绝对坐标点击
                if after_action != "none":
                    try:
                        import pyautogui
                        import random
                        pyautogui.moveTo(abs_cx, abs_cy)
                        if after_action == "click":
                            pyautogui.click()
                        elif after_action == "double_click":
                            pyautogui.doubleClick()
                        elif after_action == "right_click":
                            pyautogui.rightClick()
                        # 点击后延迟（固定值）
                        post_delay_ms = param_num(node.params.get("post_delay_ms"), 50, int, lo=0)
                        if post_delay_ms > 0:
                            ctx.log(f"[FindImage] 点击后延迟 {post_delay_ms}ms 再执行下一节点", "DEBUG")
                            wait_if_paused(ctx)
                            time.sleep(post_delay_ms / 1000.0)
                    except Exception as ea:
                        ctx.log(f"[FindImage] 找到后执行动作失败: {ea}", "WARN")
                # 日志
                display = os.path.basename(str(use_path))
                act_note = {
                    "none": "", "click": "|动作=单击", "double_click": "|动作=双击",
                    "right_click": "|动作=右键",
                }.get(after_action, "")
                mode_note = "目标窗口内" if target is not None else "全屏"
                ctx.log(f"[FindImage] 命中 {display} 命中矩形=({left},{top},{w},{h}) 绝对中心=({abs_cx},{abs_cy}) out_pos={out_pos}({mode_note}) {act_note}", "SUCCESS")
                annotate_step(
                    ctx, node, "find",
                    pos=(left, top, w, h), region=region,
                    note=f"OK: {display} @({left},{top}) out={out_pos}({mode_note}) {act_note}",
                )
                return (
                    {"out_pos": out_pos},
                    ["out_exec", "found"], "",
                )
            else:
                msg = f"未找到 {os.path.basename(str(use_path))} threshold={threshold} timeout={timeout_s}s"
                if last_err:
                    msg += f" err={last_err}"
                scroll_note = f"（滚动量={scroll_dy}）" if scroll_dy != 0 else ""
                ctx.log(f"[FindImage] {msg}{scroll_note} -> 没有找到", "WARN")
                annotate_step(
                    ctx, node, "find", region=region,
                    note=f"FAIL: {msg}{scroll_note}",
                )
                return (
                    {"out_pos": None},
                    ["out_exec", "not_found"], "",
                )
        finally:
            # 清理临时文件
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


def execute_PressKey(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """双模式键盘节点：
    - text 模式：输入文本（粘贴或逐字符），可选末尾按回车
    - key 模式：按下组合键（如 ctrl+c），支持多次重复
    """
    p = dict(node.params or {})
    kb_mode = str(p.get("kb_mode", "text") or "text")

    if kb_mode == "text":
        # ---------- 输入文本模式 ----------
        # 内容二选一（与 Clipboard.write 同一套约定）：
        #   ① 左侧「文本内容」端口连了上游且值非空 → 用连线值（上游实时值，不再做插值）
        #   ② 否则用参数 text（支持 {var} 插值）
        port_text = inputs.get("in_text") if inputs else None
        use_port = port_text is not None and str(port_text) != ""
        if use_port:
            text = str(port_text)
        else:
            text = str(p.get("text", "") or "")
            # 变量插值
            try:
                from src.blueprint.core.variable_engine import VariableEngine
                engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
                if engine.has_any_marker(text):
                    text = str(engine.interpolate(text))
            except Exception:
                pass

        use_paste = bool(p.get("use_paste", True))
        press_enter = bool(p.get("press_enter", False))

        if not text:
            ctx.log("[PressKey] text 模式但文本为空（参数为空，且「文本内容」端口无连线或值为空），跳过。", "WARN")
            annotate_step(ctx, node, "keypress", note="text mode: empty text")
            return {}, ["out_exec"], ""

        ctx.log(f"[PressKey] text 模式 len={len(text)} 来源={'端口' if use_port else '参数'} paste={use_paste} enter={press_enter}", "INFO")
        try:
            if use_paste:
                # 粘贴模式：写剪贴板 + Ctrl+V（防中文乱码）
                # ★ 整段「写剪贴板 → Ctrl+V → 等目标程序取走」必须持剪贴板锁串行执行。
                #   并行分支里若不串行，两条分支会各自写剪贴板，后者覆盖前者，
                #   于是所有分支粘出来的都是同一份（最后写入的那份）内容。
                import pyperclip
                with _CLIPBOARD_LOCK:
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
                    # 锁内多留一拍：让目标程序把剪贴板内容真正读走，下一分支才能覆盖
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


def _target_configured(params: Dict[str, Any]) -> bool:
    """「目标窗口」定位条件是否至少填了一个（标题/程序名/类名）。"""
    return any(str(params.get(k, "") or "").strip() for k in ("target_title", "target_exe", "target_class"))


def param_num(value, default, cast=float, lo=None, hi=None):
    """从节点参数里安全取数：只把「真·缺失」当默认值，**合法的 0 原样保留**。

    为什么需要它：`int(p.get("post_delay_ms", 50) or 50)` 这种写法里
    `0 or 50 == 50`，用户把「延迟 / 阈值 / 时长 / 超时」填 0 会被静默改成默认值。
    凡是参数 min 允许 0 的（“0 = 不等待 / 0 = 永不超时 / 0 = 瞬间”这类），
    读取处都必须走这里，不能写 `x or 默认值`。

    value 为 None / 空串 / 转不动的非法值 → 用 default；其余按 cast 转换，
    再按 lo/hi 收敛。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        value = default
    try:
        out = cast(value)
    except (TypeError, ValueError):
        out = cast(default)
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _target_err_msg(params: Dict[str, Any], tag: str) -> str:
    """range_mode=window 但拿不到目标窗口时的统一错误文案（区分「没配」和「没找到」）。"""
    if _target_configured(params):
        return (f"{tag}：目标窗口未找到（标题/程序名/类名都没匹配到）——"
                f"不会退化为全屏，请确认目标窗口已打开，或修正定位条件")
    return (f"{tag}：范围选了「目标窗口」，但没配置目标窗口（标题/程序名/类名）——"
            f"请补上定位条件，或把范围改为「全屏」")


def _resolve_target_rect(ctx, params: Dict[str, Any], tag: str = "目标窗口"):
    """解析「目标窗口」窗口：按 target_title / target_exe / target_class 查找，
    返回评分最高的 (hwnd, rect)。未配置定位条件、或条件没匹配到窗口，都返回 None
    （不再有任何全局目标窗口回落）。

    rect 与编辑器框选 ROI / 拾取窗口同口径（DWM 可见边界），返回的是裸的
    (left, top, right, bottom)，调用方按需换算成 (x, y, w, h)。

    多候选场景（同一条件命中多个可见窗口）请用 _resolve_target_rects。
    """
    rects = _resolve_target_rects(ctx, params, tag=tag)
    return rects[0] if rects else None


def _resolve_target_rects(ctx, params: Dict[str, Any], tag: str = "目标窗口") -> list:
    """解析「目标窗口」的全部候选窗口，返回按匹配评分降序的 [(hwnd, rect), ...]。

    定位条件与 _resolve_target_rect 完全一致；差别是条件命中多个真实可见窗口时
    不再只挑一个，而是全部返回，由调用方（FindImage）逐个轮询尝试，用模板图本身消歧。

    仅一个候选时行为与旧版完全一致（含最小化窗口自动还原）；
    多候选时跳过最小化窗口（避免批量把窗口弹出来），若因此没有可用候选则返回 []。
    """
    from src.blueprint.core.variable_engine import VariableEngine
    title_raw = str(params.get("target_title", "") or "")
    exe_name = str(params.get("target_exe", "") or "").strip()
    class_name = str(params.get("target_class", "") or "").strip()
    if not (title_raw.strip() or exe_name or class_name):
        return []
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
        hwnds = _find_window_3d(title, exe_name, class_name, return_all=True)
        if not hwnds:
            ctx.log(f"[{tag}] 目标窗口未找到: title={title!r} exe={exe_name!r} class={class_name!r}", "WARN")
            return []
        out = []
        import win32gui as _wg2
        for hwnd in hwnds:
            try:
                if _wg2.IsIconic(hwnd):
                    if len(hwnds) > 1:
                        continue  # 多候选：跳过最小化窗口，不批量还原
                    _wg2.ShowWindow(hwnd, 9)  # SW_RESTORE
                    time.sleep(0.15)
            except Exception:
                pass
            # 窗口矩形用 DWM 可见边界（与编辑器框选 ROI / 拾取窗口的基准一致），
            # 裸 GetWindowRect 在 Win10/11 含不可见边框，会让 ROI 整体偏移几个像素
            try:
                from src.tools.window_utils import get_window_rect_dwm
                rect = get_window_rect_dwm(hwnd)
            except Exception:
                rect = None
            if rect is None:
                import win32gui as _wg
                rect = _wg.GetWindowRect(hwnd)  # (left, top, right, bottom)
            out.append((hwnd, rect))
        if not out:
            return []
        if len(out) > 1:
            ctx.log(f"[{tag}] 目标条件命中 {len(out)} 个候选窗口", "DEBUG")
        return out
    except Exception as e:
        ctx.log(f"[{tag}] 解析目标窗口失败: {type(e).__name__}: {e}", "WARN")
        return []


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
def _target_window_foreground(ctx, target):
    """目标窗口前置上下文管理器。

    传入已解析好的 target=(hwnd, rect)（或 None）。操作前把目标窗口前置到最前面，
    让截图/找图/点击都作用在它身上；操作结束后不做任何「还原」（本项目已没有全局目标窗口，
    不再需要把某个窗口还原到最前）。

    用法：
        with _target_window_foreground(ctx, target):
            # 执行操作
    """
    if target is None:
        yield
        return
    _bring_window_to_front_safe(ctx, target[0])
    time.sleep(0.15)  # 等待窗口切换完成
    yield


def _mouse_final_pos(ctx, params: Dict[str, Any], inputs: Dict[str, Any], target) -> Tuple[int, int]:
    """按 range_mode 把 pos(tuple2) 或 in_pos 转换为屏幕绝对坐标。

    in_pos / pos 的坐标系取决于 Mouse 节点的 range_mode：
      - fullscreen：直接当绝对屏幕坐标
      - window    ：当相对目标窗口坐标，加上窗口左上角偏移

    target：已由 execute_Mouse 解析好的 (hwnd, rect) 或 None。
    range_mode=window 时 target 必不为 None（找不到窗口的情况在 execute_Mouse 里已走 err）。
    """
    mode = str(params.get("range_mode", "window") or "window")
    in_pos = inputs.get("in_pos")
    if in_pos is not None and isinstance(in_pos, (tuple, list)) and len(in_pos) == 2:
        x, y = int(in_pos[0]), int(in_pos[1])
    else:
        x, y = _tuple2(params.get("pos", (0, 0)))
    if mode == "window" and target is not None:
        rect = target[1]
        return (int(rect[0]) + x, int(rect[1]) + y)
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


def _mouse_final_pos_for(ctx, params: Dict[str, Any], point_param_name: str, target) -> Tuple[int, int]:
    """按 range_mode 把指定点参数（from_pos / to_pos）转换为屏幕绝对坐标。"""
    x, y = _tuple2(params.get(point_param_name, (0, 0)))
    if str(params.get("range_mode", "window") or "window") == "window" and target is not None:
        rect = target[1]
        return (int(rect[0]) + x, int(rect[1]) + y)
    return (x, y)


def execute_Mouse(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """统一 mouse 执行：根据 action 分派（click/move/drag/scroll）。"""
    import random
    p = dict(node.params or {})
    action = str(p.get("action", "click") or "click")
    btn = str(p.get("button", "left") or "left")
    ia = getattr(ctx, "input_actions", None)

    # 范围解析：range_mode=window 时必须先拿到目标窗口，拿不到直接走 err——
    # 不按绝对坐标硬点（原来会偷偷退化成绝对坐标，属于静默做错事）。
    range_mode = str(p.get("range_mode", "window") or "window")
    target = None
    if range_mode == "window":
        target = _resolve_target_rect(ctx, p, tag="Mouse")
        if target is None:
            err = _target_err_msg(p, "Mouse")
            ctx.log(f"[Mouse] {err}", "ERROR")
            annotate_step(ctx, node, "mouse_err", note=err[:60])
            return {}, ["err"], err

    post_delay_ms = param_num(p.get("post_delay_ms"), 50, int, lo=0)
    post_range = (post_delay_ms, post_delay_ms)  # 单值区间：game_click 内部 random.randint 退化为固定

    with _target_window_foreground(ctx, target):
        try:
            pos = _mouse_final_pos(ctx, p, inputs, target)
            wait_if_paused(ctx)

            if action == "click":
                clicks = max(1, int(p.get("click_count", 1) or 1))
                hold_ms = param_num(p.get("hold_ms"), 50, int, lo=1)
                hold_range = (hold_ms, hold_ms)
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
                from_pt = _mouse_final_pos_for(ctx, p, "from_pos", target)
                to_pt = _mouse_final_pos_for(ctx, p, "to_pos", target)
                dur = param_num(p.get("drag_duration_ms"), 300, int, lo=0)
                from_x, from_y = from_pt
                to_x, to_y = to_pt
                _drag_btn_held = False
                if ia is not None:
                    try:
                        steps = max(2, dur // 20)
                        ia._hw_mouse_move(from_x, from_y)
                        time.sleep(0.05)
                        ia._hw_mouse_down(btn)
                        _drag_btn_held = True
                        time.sleep(0.05)
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

            wait_if_paused(ctx)
            post_delay_ms = random.randint(int(post_range[0]), int(post_range[1]))
            if post_delay_ms > 0:
                ctx.log(f"[Mouse] 操作后延迟 {post_delay_ms}ms 再执行下一节点", "DEBUG")
                time.sleep(post_delay_ms / 1000.0)

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
            # 持剪贴板锁读：避免读到别的并行分支刚写一半的内容
            with _CLIPBOARD_LOCK:
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

            # ★ 写 + 读回校验必须整体持锁：否则并行分支会在「写完」和「读回校验」之间
            #   把剪贴板改掉，导致校验假失败（内容其实是对的）。
            write_ok = False
            with _CLIPBOARD_LOCK:
                # 重试 2 次（防其他进程占用剪贴板）
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
            # ★ 清空 + 读回校验同样整体持锁（理由同 write）
            clear_ok = False
            with _CLIPBOARD_LOCK:
                # 重试 2 次（防其他进程占用剪贴板）
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
    timeout_ms = param_num(params.get("timeout_ms"), 10000, int, lo=0)  # 0 = 永不超时
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


# ============================================================
# 「打开网页」节点用的浏览器清单
#   严格模式：指定了浏览器就必须用它，找不到就走 err，绝不静默换成默认浏览器。
#   （用户明确要求：有些操作是绑定浏览器的，兜底会把「本该失败」变成「悄悄做错」。）
# ============================================================
_BROWSER_LABELS = {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox"}

_BROWSER_EXE_CANDIDATES: Dict[str, List[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        "google-chrome", "chrome", "chromium",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        "msedge", "edge",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\firefox.exe",
        "firefox", "mozilla",
    ],
}


def _find_browser_exe(browser_name: str) -> Optional[str]:
    """在标准安装路径 + PATH 里找指定浏览器的可执行文件。

    返回绝对路径或 PATH 命中项；找不到返回 None（调用方必须走 err，不允许兜底）。
    支持 %LOCALAPPDATA% 这类环境变量写法。
    """
    import shutil as _shutil

    for cand in _BROWSER_EXE_CANDIDATES.get(browser_name, []):
        try:
            expanded = os.path.expandvars(cand)
            if os.path.isfile(expanded):
                return expanded
            hit = _shutil.which(expanded)
            if hit:
                return hit
        except Exception:
            continue
    return None


def execute_WebBrowser(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """【打开网页】用浏览器打开指定 URL。支持 {var} 插值 + 选择 默认/Chrome/Edge/Firefox。

    严格模式（用户要求，不做任何静默兜底）：
      · 浏览器=默认   → 用系统默认浏览器打开；
      · 浏览器=Chrome/Edge/Firefox → 必须找到该浏览器可执行文件，找不到直接走 err；
      · 浏览器完整路径（browser_path）非空 → 优先用它，文件不存在直接走 err；
        绿色版/装在非标准位置的浏览器用这个参数，绕开标准路径清单的限制。
      启动失败也走 err。绝不改用系统默认浏览器顶替。
      原因：有些操作是绑定浏览器的，静默兜底会把「本该失败」变成「悄悄做错」，
      而且旧实现的日志还会打着 [chrome] 的旗号，实际是别的浏览器打开的。

    打开后会等待 wait_sec_after_open 秒（默认 5）再走下一节点，等待可被暂停/停止打断。

    启动方式刻意用 Popen 且不 wait：webbrowser.GenericBrowser.open() 内部是
    Popen(...) 之后 p.wait()，浏览器进程若常驻（首次冷启动时往往如此），
    这里会一直等到浏览器被关闭才返回，表现为蓝图「卡住」。
    """
    import subprocess as _sp
    import webbrowser

    p = dict(node.params or {})
    _BROWSER_MAP = {
        "默认": "default", "default": "default",
        "chrome": "chrome",
        "edge": "edge",
        "firefox": "firefox",
    }
    url_template = str(p.get("url", "") or "")
    browser_raw = str(p.get("browser", "默认") or "默认").strip().lower()
    browser_name = _BROWSER_MAP.get(browser_raw, "default")
    # 完整路径：去掉首尾空白与可能粘上的引号（用户从资源管理器「复制为路径」会带引号）
    browser_path_raw = str(p.get("browser_path", "") or "").strip().strip('"').strip("'").strip()

    # 打开后等待秒数：0 = 不等。走 param_num 取值——不能用 `x or 默认值`，
    # 那样用户填 0 会被静默吃成默认值。
    wait_sec = param_num(p.get("wait_sec_after_open"), 5.0, float, lo=0.0, hi=3600.0)

    try:
        wait_if_paused(ctx)
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(getattr(ctx, "variables", {}) or {})
        if engine.has_any_marker(url_template):
            url_template = str(engine.interpolate(url_template))
        url = url_template if url_template else ""
        # 完整路径也允许 {var} 插值（例：{browser_dir}\chrome.exe）
        if browser_path_raw and engine.has_any_marker(browser_path_raw):
            browser_path_raw = str(engine.interpolate(browser_path_raw)).strip().strip('"').strip("'").strip()

        if not url:
            err = "URL 为空（打开网页必须填 URL）"
            annotate_step(ctx, node, "web_browser_err", note=err)
            ctx.log(f"[WebBrowser] {err}", "ERROR")
            return {}, ["err"], err

        # ---------- 目标浏览器解析（严格模式：找不到就走 err，不做任何兜底） ----------
        # 优先级：①「浏览器完整路径」显式指定 → 必须存在；
        #         ②「浏览器」下拉按名字在标准路径/PATH 里查找。
        # 两条都失败 → err，绝不改用系统默认浏览器顶替。
        exe_path = None
        if browser_path_raw:
            if not os.path.isfile(browser_path_raw):
                err = (
                    f"「浏览器完整路径」指向的文件不存在：{browser_path_raw} ——"
                    f"严格模式不会改用系统默认浏览器。"
                    f"请检查该路径，或清空本参数改用「浏览器」下拉选择"
                )
                annotate_step(ctx, node, "web_browser_err", note="FAIL 完整路径不存在")
                ctx.log(f"[WebBrowser] {err}", "ERROR")
                return {}, ["err"], err
            exe_path = browser_path_raw
            # 日志写真实启动的程序：优先用下拉里的名字，没选就用 exe 文件名
            label = _BROWSER_LABELS.get(browser_name) or os.path.basename(exe_path) or exe_path
            ctx.log(f"[WebBrowser] 使用指定的浏览器完整路径（{label}）→ {exe_path}", "INFO")
        elif browser_name != "default":
            label = _BROWSER_LABELS.get(browser_name, browser_name)
            exe_path = _find_browser_exe(browser_name)
            if exe_path is None:
                err = (
                    f"未找到 {label} 的可执行文件（已检查标准安装路径与 PATH）——"
                    f"严格模式不会改用系统默认浏览器。"
                    f"请先安装 {label}，或把「浏览器」参数改成「默认」，"
                    f"或用「浏览器完整路径」直接指定 exe 位置"
                )
                annotate_step(ctx, node, "web_browser_err", note=f"FAIL 未找到 {label}")
                ctx.log(f"[WebBrowser] {err}", "ERROR")
                return {}, ["err"], err
            ctx.log(f"[WebBrowser] 指定浏览器 {label} → {exe_path}", "INFO")
        else:
            label = "默认浏览器"

        # ---------- 启动（严格模式：指定了浏览器就只认它） ----------
        opened = False
        last_err = None
        if exe_path is not None:
            try:
                _sp.Popen([exe_path, url])  # 不 wait：详见函数头说明
                opened = True
            except Exception as e:
                last_err = e
                opened = False
        else:
            # 「默认」分支：目标本身就是系统默认浏览器，这里的二次尝试不构成「冒充」
            try:
                opened = bool(webbrowser.open(url, new=2, autoraise=True))
            except Exception as e:
                last_err = e
                opened = False
            if not opened and os.name == "nt":
                try:
                    os.startfile(url)
                    opened = True
                    ctx.log("[WebBrowser] webbrowser 打开失败，改用 os.startfile（同为系统默认浏览器）", "WARN")
                except Exception as e2:
                    last_err = last_err or e2

        if not opened:
            err_msg = f"启动浏览器失败（{label}）: {last_err}" if last_err else f"启动浏览器失败（{label}）"
            annotate_step(ctx, node, "web_browser_err", note=err_msg)
            ctx.log(f"[WebBrowser] {err_msg}", "ERROR")
            return {}, ["err"], err_msg

        note = f"[{label}] 打开 → {url[:100]}"
        annotate_step(ctx, node, "web_browser_open", note=note)
        ctx.log(f"[WebBrowser] {note}", "SUCCESS")

        # ---------- 打开后等待：浏览器起窗口/页面加载都不是一瞬间，等一会儿再走下一节点 ----------
        # 等待过程可被「暂停 / 停止」中断（和 Wait 节点同一套机制）。
        if wait_sec > 0:
            ctx.log(f"[WebBrowser] 打开后等待 {wait_sec:g}s 再走下一节点", "INFO")
            _t0 = time.time()
            while ctx.is_running and (time.time() - _t0) < wait_sec:
                wait_if_paused(ctx)
                time.sleep(0.05)
            wait_if_paused(ctx)

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
    # 数据出口：变量名 → 本次刚设置的值（节点右侧「同名出参」端口直接输出它，
    # 下游可连到「组件」节点的入参端口 / 组件蓝图「结束」节点的返回值端口）
    out_vals: Dict[str, Any] = {}

    _batch_raw_any = node.params.get("batch_text")
    if isinstance(_batch_raw_any, str):
        batch_raw = _batch_raw_any
    else:
        batch_raw = ""
        if _batch_raw_any not in (None, ""):
            # 参数被意外写成了 list/dict（老蓝图或手改 JSON）→ 不崩，按空处理并提示
            ctx.log(
                f"[Var] SetVariable batch_text 类型异常（{type(_batch_raw_any).__name__}），已按空处理。",
                "WARN",
            )
    if batch_raw.strip():
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
            out_vals[k] = final_val
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
    # 顺便核一下「变量数 vs 出参端口数」：出参只在画布右侧，数量对不上时下游会静默拿到 None
    try:
        from src.blueprint.core.node_library import _setvar_output_var_names
        port_names = _setvar_output_var_names(node.params.get("batch_text"))
        extra_vars = [k for k in out_vals.keys() if k not in port_names and k != "out_exec"]
        if extra_vars:
            ctx.log(
                f"[Var] 有 {len(extra_vars)} 个变量没有对应的数据出口端口（{', '.join(extra_vars[:6])}），"
                f"超出上限 20 个或与 out_exec 重名；这些值仍存进了变量，但没有端口可以往外连。",
                "WARN",
            )
    except Exception:
        pass
    return out_vals, ["out_exec"], first_err or ""


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


def _varswitch_eval_condition(cond: Dict[str, Any], engine) -> Tuple[bool, str, str]:
    """对解析后的单条条件做求值。返回 (命中, 可读说明, 警告)。

    警告非空 = 这条条件「语法合法、但语义很可能写错了」（典型：右侧 {变量} 插值没生效，
    说明变量名拼错/端口变量名写错）。调用方必须把它打到日志里 —— 否则用户写了
    `ht == {__input__0}` 这种（正确写法是全大写 {__INPUTS__0}）会恒走「不满足」且毫无提示。
    """
    import re as _re
    label = cond.get("label", "")
    var = cond.get("var", "")
    op = cond.get("op", "==")
    target = cond.get("value")
    warn = ""
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
        # 插值之后仍留着 {名字} / ${名字} → 该变量不存在，被当成普通字符串参与比较
        if isinstance(target, str):
            # 变量名字符集不限 ASCII（汉字也能当变量名），所以这里只判断「插值后还剩没剩 {…}」
            _m = _re.search(r"\$\{[^{}]+\}|\{[^{}]+\}", target)
            if _m:
                warn = (
                    f"右侧 {_m.group(0)} 变量不存在、插值未生效（正按字符串 '{_m.group(0)}' 比较）；"
                    f"端口值请写 {{__INPUTS__0}}（全大写），或检查变量名是否拼错"
                )
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
            return hit, f"{var}={a!r} == {b!r} → {hit}", warn
        if op == "!=":
            hit_eq, _eq_info, warn_eq = _varswitch_eval_condition({**cond, "op": "=="}, engine)
            return (not hit_eq), f"{var}={a!r} != {b!r} → {not hit_eq}", (warn or warn_eq)
        if op in (">", "<", ">=", "<="):
            # 必须双方都能转数字才合法，否则 False
            if not a_ok or not b_ok:
                return False, f"{var}={a!r} {op} {b!r} → 非数字无法比较", warn
            if op == ">":
                hit = an > bn
            elif op == "<":
                hit = an < bn
            elif op == ">=":
                hit = an >= bn
            else:  # <=
                hit = an <= bn
            return hit, f"{var}={an} {op} {bn} → {hit}", warn
    except Exception as e:
        return False, f"求值异常 {e}", warn
    return False, f"未知运算符 {op}", warn


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

    # ② 条件语法错误必须打日志（以前是静默过滤 → 用户写错语法，运行时一个字提示都没有）
    invalid = [c for c in conditions if c.get("error")]
    if invalid:
        _preview = "；".join(
            f"第{c.get('line_no')}行「{str(c.get('raw_line', '')).strip()[:40]}」{c.get('error')}"
            for c in invalid[:3]
        )
        ctx.log(
            f"[VarSwitch] {node.id[:6]} 有 {len(invalid)} 条条件语法非法，这些行会被忽略："
            f"{_preview}{' …' if len(invalid) > 3 else ''}",
            "WARN",
        )

    valid_conditions = [c for c in conditions if not c.get("error")]
    if not valid_conditions:
        ctx.log(
            f"[VarSwitch] {node.id[:6]} 没有可用条件 → 直接走「不满足」出口"
            + ("（条件行全部语法非法，见上方 WARN）" if invalid else "（条件设置为空）"),
            "WARN",
        )

    notes = []
    warns = []
    all_true = True
    for c in valid_conditions:
        hit, info, _w = _varswitch_eval_condition(c, engine)
        notes.append(info)
        if _w:
            warns.append(f"第{c.get('line_no')}行「{str(c.get('raw_line', '')).strip()[:40]}」{_w}")
        if not hit:
            all_true = False
    # ③ 「语法合法、语义写错」也要提示（例如 {__input__0} 少了大写 → 插值不生效，恒不满足）
    if warns:
        ctx.log(
            f"[VarSwitch] {node.id[:6]} 有 {len(warns)} 条条件可能写错："
            f"{'；'.join(warns[:3])}{' …' if len(warns) > 3 else ''}",
            "WARN",
        )
    if valid_conditions:
        port = "yes" if all_true else "no"
    else:
        port = "no"
    note = f"VarSwitch → {port} ({len(inputs_list)}入; {'; '.join(notes[:3])}{' …' if len(notes) > 3 else ''})"
    ctx.log(f"[VarSwitch] {note}", "DEBUG")
    annotate_step(ctx, node, "condition", note=f"→ {port} ({len(valid_conditions)} 条件 AND, {len(inputs_list)} 输入)")
    return {}, [port], ""


def execute_Sequence(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """顺序执行：纯串行队列。执行 seq_body 子画布（子图容器），从子图 Start 出发
    一条线串行跑到终点，不做任何条件判断。跑完走 out_done。

    与 Loop 循环体一致：seq_body 是塞在 node.params 里的子图 dict，双击节点进入编辑。
    """
    seq_body = node.params.get("seq_body")
    if not (isinstance(seq_body, dict) and seq_body.get("nodes")):
        ctx.log(f"[Sequence] {node.id[:6]} 顺序体子画布为空，直接走完成。", "WARN")
        annotate_step(ctx, node, "seq", note="空顺序体→out_done")
        return {}, ["out_done"], ""

    runner = getattr(ctx, "_runner_ref", None)
    if runner is None:
        msg = "[Sequence] ctx 缺少 runner 引用，无法执行顺序体子画布。"
        ctx.log(msg, "ERROR")
        annotate_step(ctx, node, "seq", note="FAIL: 缺少runner_ref")
        return {}, ["out_done"], msg

    sub = _subgraph_from_dict(seq_body, name=f"{node.id}::seq_body")
    sub._runtime_prefix = node.id
    sub._runtime_path = _exec_child_path(node)

    # 找子图入口：优先 Start，否则第一个节点
    entry_id = None
    for nid, n in sub.nodes.items():
        if n.type == "Start":
            entry_id = nid
            break
    if entry_id is None and sub.nodes:
        entry_id = next(iter(sub.nodes.keys()))
    if entry_id is None:
        ctx.log(f"[Sequence] {node.id[:6]} 顺序体为空，直接走完成。", "WARN")
        return {}, ["out_done"], ""

    # Start 无 in_exec，直接跳到它的下游作为真正入口
    start_node = sub.nodes.get(entry_id)
    if start_node is not None and start_node.type == "Start":
        outs = _exec_out_edges(sub, entry_id, "out_exec")
        if outs:
            entry_id, _ = outs[0]
        else:
            ctx.log(f"[Sequence] {node.id[:6]} 顺序体只有 Start 无内容。", "WARN")
            return {}, ["out_done"], ""

    annotate_step(ctx, node, "seq", note="进入顺序体子画布")
    ctx.log(f"[Sequence] {node.id[:6]} 串行执行顺序体子画布", "DEBUG")

    ctrl = _run_subgraph_until_control(ctx, sub, runner, entry_id)
    # 顺序体里不应有 Continue/Break（那是循环体专用），出现则视为结束
    if ctrl in ("break", "continue"):
        ctrl = "end"
    if ctrl == "stop":
        return {}, [], "运行被停止"
    annotate_step(ctx, node, "seq", note="顺序体执行完成")
    ctx.log(f"[Sequence] {node.id[:6]} 顺序体执行完成 → out_done", "INFO")
    return {}, ["out_done"], ""


def execute_Branch(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """并行分支入口：仅出现在并行体子画布内，作为一条并行分支的起点。"""
    return {}, ["out_exec"], ""


def execute_Parallel(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """并行执行：多分支并发。执行 par_body 子画布里的所有「分支入口(Branch)」节点，
    每条分支用线程并发 walk，各跑各的互不等待。

    - 全部分支跑完 → out_done；任一条分支失败（终结于失败语义端口）→ err。
    - 不中断其它分支：一条失败不影响其它分支继续跑完。
    """
    par_body = node.params.get("par_body")
    if not (isinstance(par_body, dict) and par_body.get("nodes")):
        ctx.log(f"[Parallel] {node.id[:6]} 并行体子画布为空，直接走完成。", "WARN")
        annotate_step(ctx, node, "parallel", note="空并行体→out_done")
        return {}, ["out_done"], ""

    runner = getattr(ctx, "_runner_ref", None)
    if runner is None:
        msg = "[Parallel] ctx 缺少 runner 引用，无法执行并行体子画布。"
        ctx.log(msg, "ERROR")
        annotate_step(ctx, node, "parallel", note="FAIL: 缺少runner_ref")
        return {}, ["err"], msg

    sub = _subgraph_from_dict(par_body, name=f"{node.id}::par_body")
    sub._runtime_prefix = node.id
    sub._runtime_path = _exec_child_path(node)

    # 收集所有 Branch 分支入口
    branches = [n for n in sub.nodes.values() if n.type == "Branch"]
    if not branches:
        ctx.log(f"[Parallel] {node.id[:6]} 并行体没有「分支入口」节点，直接走完成。", "WARN")
        annotate_step(ctx, node, "parallel", note="无分支入口→out_done")
        return {}, ["out_done"], ""

    annotate_step(ctx, node, "parallel", note=f"进入并行体子画布（{len(branches)} 分支）")
    ctx.log(f"[Parallel] {node.id[:6]} 并行执行 {len(branches)} 条分支", "INFO")

    # 每条分支在独立线程里并发 walk（从 Branch 的 out_exec 下游开始）
    import concurrent.futures as _cf

    results: Dict[str, Tuple[bool, str]] = {}
    with _cf.ThreadPoolExecutor(max_workers=len(branches)) as pool:
        futures = {}
        for b in branches:
            fut = pool.submit(_run_parallel_branch, ctx, sub, runner, b.id)
            futures[fut] = b
        for fut in _cf.as_completed(futures):
            b = futures[fut]
            try:
                ok, reason = fut.result()
            except Exception as e:
                ok, reason = False, f"分支执行异常: {type(e).__name__}: {e}"
            results[b.id] = (ok, reason)

    failed = [(bid, r) for bid, (ok, r) in results.items() if not ok]
    if failed:
        note = f"并行体有 {len(failed)} 条分支失败：{failed[0][1][:60]}"
        ctx.log(f"[Parallel] {node.id[:6]} {note}", "WARN")
        annotate_step(ctx, node, "parallel", note=f"✗{len(failed)}分支失败→err")
        return {}, ["err"], ""
    ctx.log(f"[Parallel] {node.id[:6]} 全部 {len(branches)} 条分支完成 → out_done", "INFO")
    annotate_step(ctx, node, "parallel", note=f"✓{len(branches)}分支→out_done")
    return {}, ["out_done"], ""


def _run_parallel_branch(ctx: ExecutionContext, sub: "BlueprintGraph", runner, branch_id: str) -> Tuple[bool, str]:
    """在独立线程里串行 walk 一条并行分支（从 Branch 节点的 out_exec 下游开始），
    返回 (success, reason)。终结于失败语义端口（无下游）→ 失败。

    与主执行流不同：这里不切换 ctx._graph_ref（避免与其它分支线程互相覆盖），
    直接操作 sub 图；对共享 ctx 的写操作（log / variables / 计数）用锁保护。
    """
    sub_prefix = getattr(sub, "_runtime_prefix", "__sub__")
    # 并行分支跑在线程池里，线程是新建或复用的 —— 本线程的容器链不继承父线程，
    # 必须按「本图路径」重新 seed，否则分支内再套容器（如分支里放个循环）算出的路径会少一层。
    # 刻意不 restore：每个分支进来都会重新 seed；这样也免去给整段 while 加缩进。
    _exec_path_set(_exec_path_of(sub))
    edges = _exec_out_edges(sub, branch_id, "out_exec")
    if not edges:
        return False, f"分支入口 {branch_id[:8]} 无下游连线"

    lock = _parallel_ctx_lock()
    curr_id, _curr_in = edges[0]
    safety = 50000
    while safety > 0 and ctx.is_running:
        safety -= 1
        node = sub.nodes.get(curr_id)
        if node is None:
            return False, f"分支节点不存在 id={curr_id[:8]}"

        wait_if_paused(ctx)

        with lock:
            try:
                runner.highlight_node.emit(curr_id, _exec_path_of(sub))
                runner.progress_step.emit(ctx.step_counter + 1, node.type)
            except Exception:
                pass
            try:
                ctx.current_node_id = curr_id
                runner.current_node_changed.emit(curr_id, node.type)
            except Exception:
                pass
            inputs = _resolve_data_inputs(sub, ctx, node, id_prefix=f"{sub_prefix}::")
            bump_step(ctx)
            ctx.log(f"[Node▶] {node.type}  id={curr_id[:8]} [并行分支]", "INFO")

        fn = EXECUTORS.get(node.type)
        try:
            if fn is None:
                outputs, next_ports, _note = {}, ["out_exec"], ""
            else:
                outputs, next_ports, _note = fn(ctx, node, inputs)
        except Exception as e:
            tb = traceback.format_exc()
            with lock:
                ctx.log(f"[Node] 并行分支内执行异常 {node.type}: {e}\n{tb}", "ERROR")
            return False, f"节点 {node.type} 执行异常: {e}"

        if not ctx.is_running:
            return False, "运行被用户停止"

        sub_prefix = getattr(sub, "_runtime_prefix", "__sub__")
        with lock:
            ctx.set_outputs(f"{sub_prefix}::{curr_id}", outputs or {})
            ctx.last_progress_ts = time.time()

        if not next_ports:
            return True, f"分支 {branch_id[:8]} 到达终点"
        pname = next_ports[0]
        outs = _exec_out_edges(sub, curr_id, pname)
        if not outs:
            if pname in FAIL_SEMANTIC:
                return False, f"节点 {node.type} 终结于失败端口 {pname}"
            return True, f"分支 {branch_id[:8]} 到达终点"
        curr_id, _curr_in = outs[0]

    if not ctx.is_running:
        return False, "运行被用户停止"
    return False, "分支执行超过安全步数（疑似死循环）"


_parallel_lock = None


def _parallel_ctx_lock():
    """并行分支共享 ctx 的线程锁（模块级单例）。"""
    global _parallel_lock
    if _parallel_lock is None:
        import threading
        _parallel_lock = threading.Lock()
    return _parallel_lock


def _resolve_component_path(ctx: ExecutionContext, node: Node) -> Optional[str]:
    """解析 Component 节点的 blueprint_path（相对当前工程根）。

    优先从 ctx.runtime_attrs 拿工程根（BlueprintRunner 注入），拿不到则回退 ProjectManager。
    """
    raw = str(node.params.get("blueprint_path", "") or "").strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw if os.path.isfile(raw) else None
    # 相对路径：基于工程根
    proj_root = None
    try:
        proj_root = ctx.runtime_attrs.get("project_root")
    except Exception:
        proj_root = None
    if not proj_root:
        try:
            from src.blueprint.project_manager import ProjectManager
            pm = ProjectManager.instance()
            proj_root = pm.project_root
        except Exception:
            proj_root = None
    if not proj_root:
        return None
    cand = os.path.join(proj_root, raw)
    return cand if os.path.isfile(cand) else None


def execute_Component(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """调用组件：加载一张子蓝图（预制体），递归执行它，跑完返回 out_done。

    - 显式传参：输入端口 in_0... 的值写入 ctx 变量 __arg_0...，子蓝图 Start 的
      param_0... 端口（execute_Start）会读取这些变量输出；子蓝图 End 的 ret_0...
      端口值写入 __ret_0...，本函数读回作为 out_0... 输出。
    - 变量全局共享：子蓝图和父蓝图用同一个 ctx.variables。
    - 子蓝图入口 = 它的 Start 节点；结束 = 它的 End 节点或执行流自然走到终点。
    - 找不到蓝图文件 / 加载失败 → 走 out_done（视为空操作）并打 WARN，不中断父流程。
    """
    path = _resolve_component_path(ctx, node)
    if path is None:
        raw = str(node.params.get("blueprint_path", "") or "").strip()
        note = f"组件蓝图未找到或路径为空: {raw or '(空)'}"
        ctx.log(f"[Component] {node.id[:6]} {note}", "WARN")
        annotate_step(ctx, node, "component", note=f"FAIL: {note[:40]}")
        return _empty_component_outputs(node), ["out_done"], ""

    from src.blueprint.core.graph_store import BlueprintStore
    sub = BlueprintStore.load(path)
    if sub is None:
        note = f"组件蓝图加载失败: {path}"
        ctx.log(f"[Component] {node.id[:6]} {note}", "WARN")
        annotate_step(ctx, node, "component", note=f"FAIL: {note[:40]}")
        return _empty_component_outputs(node), ["out_done"], ""

    runner = getattr(ctx, "_runner_ref", None)
    if runner is None:
        note = "缺少 runner 引用，无法递归执行组件。"
        ctx.log(f"[Component] {node.id[:6]} {note}", "ERROR")
        annotate_step(ctx, node, "component", note="FAIL: 缺少runner_ref")
        return _empty_component_outputs(node), ["out_done"], ""

    # ---------- 循环引用 / 递归深度保护 ----------
    # 组件是文件引用，A 调 B、B 调 A 会让运行时无限递归直到栈溢出（整个程序被杀掉）。
    # 这里维护一条「当前组件调用链」，命中重复 → 立刻判定循环引用并中止该分支。
    _chain = getattr(ctx, "_component_chain", None)
    if not isinstance(_chain, list):
        _chain = []
        try:
            ctx._component_chain = _chain
        except Exception:
            pass
    _self_norm = os.path.normcase(os.path.abspath(path))
    _chain_norms = [os.path.normcase(os.path.abspath(c)) for c in _chain]
    if _self_norm in _chain_norms:
        _loop = " → ".join(os.path.basename(c) for c in _chain + [path])
        note = f"检测到组件循环引用，已中止：{_loop}"
        ctx.log(f"[Component] {node.id[:6]} {note}", "ERROR")
        annotate_step(ctx, node, "component", note="FAIL: 组件循环引用")
        return _empty_component_outputs(node), ["out_done"], ""
    if len(_chain) >= MAX_COMPONENT_DEPTH:
        note = f"组件递归层数超过 {MAX_COMPONENT_DEPTH} 层，已中止（疑似循环引用）"
        ctx.log(f"[Component] {node.id[:6]} {note}", "ERROR")
        annotate_step(ctx, node, "component", note="FAIL: 组件嵌套过深")
        return _empty_component_outputs(node), ["out_done"], ""

    # 找子蓝图入口（Start 的下游作为真正起点）
    start_node = sub.find_start_node()
    if start_node is None:
        ctx.log(f"[Component] {node.id[:6]} 子蓝图没有 Start 节点，跳过。", "WARN")
        return _empty_component_outputs(node), ["out_done"], ""
    entry_id = start_node.id
    outs = _exec_out_edges(sub, entry_id, "out_exec")
    if outs:
        entry_id, _ = outs[0]
    else:
        ctx.log(f"[Component] {node.id[:6]} 子蓝图只有 Start 无内容，视为空组件。", "WARN")
        return _empty_component_outputs(node), ["out_done"], ""

    sub._runtime_prefix = node.id
    # ⚠ 刻意不设 sub._runtime_path：组件是「另一张蓝图」，不在当前画布上，编辑器无法跟随进去。
    #   不设 → _exec_path_of() 返回 None → 编辑器不做画布跳转（只按 id 高亮，找不到就忽略）。
    annotate_step(ctx, node, "component", note=f"进入组件 {os.path.basename(path)}")
    ctx.log(f"[Component] {node.id[:6]} 执行子蓝图 {os.path.basename(path)}", "INFO")

    _chain.append(path)
    try:
        # ---------- 传参：输入端口 in_0... → ctx 变量 __arg_0... ----------
        num_in = int(node.params.get("num_inputs", 0) or 0)
        for i in range(num_in):
            ctx.variables[f"__arg_{i}"] = inputs.get(f"in_{i}")

        # ---------- 执行子蓝图 Start 节点，把入参输出到 param_0... 端口（供子图内连线取用）----------
        # Start 无 in_exec，这里手动执行一次，让它的 param_0... 输出写入 last_outputs（带前缀）
        try:
            start_outputs, _, _ = execute_Start(ctx, start_node, {})
            ctx.set_outputs(f"{node.id}::{start_node.id}", start_outputs or {})
        except Exception as e:
            ctx.log(f"[Component] 执行子蓝图 Start 异常: {e}", "WARN")

        ctrl = _run_subgraph_until_control(ctx, sub, runner, entry_id)
        # 组件内不应有 Continue/Break（那是循环体专用），若出现按正常结束处理
        if ctrl in ("break", "continue"):
            ctrl = "end"
        if ctrl == "stop":
            return {}, [], "运行被停止"
        annotate_step(ctx, node, "component", note=f"组件完成 {os.path.basename(path)}")

        # ---------- 回传：ctx 变量 __ret_0... → 输出端口 out_0... ----------
        num_out = int(node.params.get("num_outputs", 0) or 0)
        out_vals: Dict[str, Any] = {}
        for i in range(num_out):
            out_vals[f"out_{i}"] = ctx.variables.get(f"__ret_{i}")
        return out_vals, ["out_done"], ""
    finally:
        # 出链：任何返回路径都要把本组件从调用链上摘掉，避免误报循环引用
        try:
            if _chain and os.path.normcase(os.path.abspath(_chain[-1] or "")) == _self_norm:
                _chain.pop()
        except Exception:
            pass


def _empty_component_outputs(node: Node) -> Dict[str, Any]:
    """组件未执行成功时，输出端口 out_0... 统一给 None。"""
    num_out = int(node.params.get("num_outputs", 0) or 0)
    return {f"out_{i}": None for i in range(num_out)}


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
    # ⚠ 正文里不要再写一遍 [WARN]：日志面板会按 level 参数自己加 "[WARN]" 标签列，
    #    运行日志文件也会写成 "[时间] [WARN] ..."。正文再加一遍就成了 "[WARN] [WARN] 标题"。
    log_body = str(msg_final)
    if len(log_body) > 4000:
        log_body = log_body[:4000] + "\n...（截断，完整内容看系统弹窗）"
    ctx.log(f"{title_final}\n{log_body}" if log_body else str(title_final), level_final)

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
    strategy = str(data.get("match_strategy", "hybrid") or "hybrid")
    eval_mode = str(data.get("eval_mode", "complete") or "complete")
    range_mode = str(data.get("range_mode", "window") or "window")

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

    # 目标窗口解析：window 模式必须先拿到窗口，找不到直接 err（不退化为全屏）
    target = None
    if range_mode == "window":
        target = _resolve_target_rect(ctx, data, tag="IfImg")
        if target is None:
            err = _target_err_msg(data, "IfImg")
            ctx.log(f"[IfImg] {err}", "ERROR")
            annotate_step(ctx, node, "find", note="ERROR: target window not found")
            return {}, ["err"], err

    with _target_window_foreground(ctx, target):
        try:
            # region 在窗口前置之后计算，避免最小化窗口恢复后 rect 失效
            region = None
            if target is not None:
                rect = target[1]
                region = (int(rect[0]), int(rect[1]), int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1]))
                ctx.log(f"[IfImg] 目标窗口内检测 region={region}", "DEBUG")

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
                        else:  # hybrid：彩色 → 灰度，两级级联
                            pos = ctx.matcher.find_image(use_path, region=region, threshold=threshold, fast_mode=True)
                            if pos is None:
                                pos = ctx.matcher.find_image_gray(use_path, region=region, threshold=threshold, fast_mode=True)
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
    - roi: [x, y, w, h]（必须框选，宽/高为 0 直接走 err）
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

    dt = param_num(p.get("diff_threshold"), 0.01, float, lo=0.0)
    stillness_seconds = max(0.1, float(p.get("stillness_seconds", 5.0) or 5.0))
    sample_interval_seconds = max(0.03, float(p.get("sample_interval_seconds", 1.0) or 1.0))
    timeout_seconds = max(1.0, float(p.get("timeout_seconds", 20.0) or 20.0))
    range_mode = str(p.get("range_mode", "window") or "window")

    if w <= 0 or h <= 0:
        # 未设置 ROI 属于「配置还没做完」，不是「画面一直在动」——
        # 走 no（端口 label 是「超时」）会把配置错误伪装成一次正常的超时判定，
        # 让下游以为"等够了但没静止"，真正的原因被吞掉。所以这里走 err。
        err = ("if_static：未设置 ROI（roi 宽/高为 0）——请先在右侧面板点「🖱 框选ROI」框一个区域。"
               "本节点判定的是「某块区域画面有没有变化」，没有 ROI 就没有判定对象，"
               "不会静默拿整个检测范围当 ROI。")
        ctx.log(f"[if_static] {err}", "ERROR")
        annotate_step(ctx, node, "static", note="ERROR: ROI 未设置")
        return {}, ["err"], err

    # 目标窗口解析：window 模式必须先拿到窗口，找不到直接 err（不退化为全屏）
    target = None
    if range_mode == "window":
        target = _resolve_target_rect(ctx, p, tag="if_static")
        if target is None:
            err = _target_err_msg(p, "if_static")
            ctx.log(f"[if_static] {err}", "ERROR")
            annotate_step(ctx, node, "static", note="ERROR: target window not found")
            return {}, ["err"], err

    # 窗口偏移：window 模式下 ROI 坐标基于目标窗口左上角；fullscreen 模式基于屏幕左上角
    _win_offset = (0, 0)
    if target is not None:
        _r = target[1]
        _win_offset = (int(_r[0]), int(_r[1]))

    prev_roi: Optional[np.ndarray] = None
    stable_since_ts: Optional[float] = None
    t0 = time.monotonic()
    timeout_s = timeout_seconds
    interval_s = sample_interval_seconds
    last_diff = None  # None=未采样过，float=最近一次帧差

    with _target_window_foreground(ctx, target):
        while (time.monotonic() - t0) < timeout_s and getattr(ctx, "is_running", True):
            wait_if_paused(ctx)
            if not getattr(ctx, "is_running", True):
                break
            shot_bgr = None
            # ① 优先后台截窗：PrintWindow(PW_RENDERFULLCONTENT)，与「截图 / 文字」节点同款。
            #    目标窗口被别的窗口盖住时也能拿到"它自己"的实时画面；若只用屏幕抓取，
            #    帧差比较看的会是遮挡物，判定直接失真（同一组视觉节点口径必须一致）。
            if target is not None:
                try:
                    from src.blueprint.runtime.screen_capture import capture_window_bgr
                    _win_img = capture_window_bgr(target[0])
                except Exception:
                    _win_img = None
                if _win_img is not None:
                    shot_bgr = _crop_roi(_win_img, (x, y, w, h))
            # ② 回退：屏幕抓取（同一 ROI 口径，window 模式下加窗口左上角偏移）。
            #    截窗失败（最小化/拒绝渲染）或 ROI 越界时走这里。
            if shot_bgr is None:
                try:
                    if ctx.screen is not None:
                        ox, oy = _win_offset
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


# ============================================================
# 节点类型 -> 执行函数 映射
# ============================================================
EXECUTORS = {
    "Start": execute_Start,
    "End": execute_End,
    "Loop": execute_Loop,
    "Continue": execute_Continue,
    "Break": execute_Break,
    "Sequence": execute_Sequence,
    "Component": execute_Component,
    "Wait": execute_Wait,
    "VarSwitch": execute_VarSwitch,
    "Notify": execute_Notify,
    "OpenApp": execute_OpenApp,
    "FindImage": execute_FindImage,
    "Mouse": execute_Mouse,
    "PressKey": execute_PressKey,
    "SetVariable": execute_SetVariable,
    "IfImageDetect": execute_IfImageDetect,
    "StillnessDetect": execute_StillnessDetect,
    "Clipboard": execute_Clipboard,
    "Command": execute_Command,
    "WebBrowser": execute_WebBrowser,
    "HttpRequest": execute_HttpRequest,
}


# ============================================================
# 截图节点
# ============================================================
def _crop_roi(img_bgr, roi):
    """按 (x, y, w, h) 裁剪；全 0 或非法则返回原图。坐标越界自动 clamp。"""
    if img_bgr is None:
        return None
    try:
        x, y, w, h = (int(v) for v in roi)
    except Exception:
        return img_bgr
    if w <= 0 or h <= 0:
        return img_bgr
    H, W = img_bgr.shape[:2]
    x2 = min(W, x + w)
    y2 = min(H, y + h)
    x = max(0, x)
    y = max(0, y)
    if x2 <= x or y2 <= y:
        return None
    return img_bgr[y:y2, x:x2]


def execute_Screenshot(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """截图节点：截目标窗口/全屏的 ROI，输出图像（BGR / PNG 字节 / 可选落盘路径）。

    range_mode=window 时目标窗口没找到 -> 走 err（不退化为全屏截图）。
    """
    import cv2

    wait_if_paused(ctx)
    p = dict(node.params or {})
    range_mode = str(p.get("range_mode", "window") or "window")
    roi = p.get("roi", (0, 0, 0, 0))
    save_path = str(p.get("save_path", "") or "")

    try:
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
    except Exception:
        engine = None

    # 解析 ROI（支持变量插值）
    if isinstance(roi, str) and engine is not None and engine.has_any_marker(roi):
        roi = engine.interpolate(roi)
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        roi = (0, 0, 0, 0)
    try:
        roi = tuple(int(v) for v in roi)
    except Exception:
        roi = (0, 0, 0, 0)

    # 解析保存路径（支持变量插值）
    if save_path and engine is not None and engine.has_any_marker(save_path):
        save_path = str(engine.interpolate(save_path))

    # 目标窗口解析（会还原最小化窗口并取 DWM 可见边界矩形）。
    # 严格模式：选了「目标窗口」就必须真的截到那个窗口，找不到直接走 err——
    # 绝不退化成全屏截图（下游会把「整屏」当「窗口画面」用，属于静默做错事）。
    target = None
    if range_mode == "window":
        target = _resolve_target_rect(ctx, p, tag="Screenshot")
        if target is None:
            err = _target_err_msg(p, "Screenshot")
            ctx.log(f"[Screenshot] {err}", "ERROR")
            annotate_step(ctx, node, "screenshot_err", note=err[:60])
            return {"image_bgr": None, "image_png_bytes": b"", "image_path": ""}, ["err"], err

    img = None
    if target is not None:
        # ① 优先后台截窗：PrintWindow(PW_RENDERFULLCONTENT)，
        #    窗口被遮挡时也能拿到实时画面，不前置、不抢焦点、不闪窗
        try:
            from src.blueprint.runtime.screen_capture import capture_window_bgr
            img = capture_window_bgr(target[0])
        except Exception:
            img = None

    if img is None:
        # ② 回退：还原+置顶目标窗口后全屏截取、按窗口矩形裁剪
        #   （注意：这是「同一目标窗口」的取图方式回退，不是改成截全屏——
        #    window 模式走到这里 target 必不为 None，前面已经拦掉了「找不到窗口」的情况）
        if target is not None:
            _bring_window_to_front_safe(ctx, target[0])
            time.sleep(0.15)  # 等待窗口前置与重绘完成

        # 截取
        full = ScreenCapture().capture_full_bgr()
        if full is None:
            err = "全屏截图失败"
            ctx.log(f"[Screenshot] {err}", "ERROR")
            return {"image_bgr": None, "image_png_bytes": b"", "image_path": ""}, ["err"], err

        img = full
        if target is not None:
            # 用目标窗口限定截图范围
            rect = target[1]  # (left, top, right, bottom)
            l, t, r, b = (int(v) for v in rect)
            H, W = full.shape[:2]
            l = max(0, l)
            t = max(0, t)
            r = min(W, r)
            b = min(H, b)
            if r > l and b > t:
                img = full[t:b, l:r]
            else:
                err = "目标窗口矩形无效（窗口可能完全在屏幕外、已最小化或被隐藏）"
                ctx.log(f"[Screenshot] {err}", "ERROR")
                annotate_step(ctx, node, "screenshot_err", note=err[:60])
                return {"image_bgr": None, "image_png_bytes": b"", "image_path": ""}, ["err"], err

    # 应用 ROI
    img = _crop_roi(img, roi)
    if img is None:
        err = "ROI 区域裁剪结果为空"
        ctx.log(f"[Screenshot] {err}", "ERROR")
        return {"image_bgr": None, "image_png_bytes": b"", "image_path": ""}, ["err"], err

    # 编码 PNG 字节
    ok, buf = cv2.imencode(".png", img)
    png_bytes = buf.tobytes() if ok else b""

    # 可选落盘
    saved_path = ""
    if save_path:
        try:
            d = os.path.dirname(save_path)
            if d:
                os.makedirs(d, exist_ok=True)
            if ok:
                buf.tofile(save_path)
                saved_path = save_path
            else:
                cv2.imwrite(save_path, img)
                saved_path = save_path
        except Exception as e:
            ctx.log(f"[Screenshot] 保存 PNG 失败: {e}", "WARN")

    H, W = img.shape[:2]
    note = f"{'目标窗口' if target is not None else '全屏'} {W}x{H}" + (f" -> {saved_path}" if saved_path else "")
    annotate_step(ctx, node, "screenshot", note=note)
    ctx.log(f"[Screenshot] {note}", "SUCCESS")
    return {
        "image_bgr": img,
        "image_png_bytes": png_bytes,
        "image_path": saved_path or "",
    }, ["out_done"], ""


# ============================================================
# 文字/数字识别节点
# ============================================================
def execute_OcrRecognize(ctx: ExecutionContext, node: Node, inputs: Dict[str, Any]):
    """OCR 识别节点：现场截图（目标窗口 / 全屏）→ 识别 → 输出整屏文本 + 识别条目列表。

    出口：
      · `out_done` 完成   —— 识别流程正常跑完（识别到文字时）
      · `err` 失败        —— 截图失败 / 目标窗口没定位到 / 引擎异常 / 匹配表达式非法
      · `no_text` 无文字  —— 跑完了但一条文字都没识别到（与「失败」分开）
      · `matched` / `not_matched` —— 填了「匹配文本」时才有：整屏文本命中 / 未命中
    range_mode=window 时目标窗口没找到 -> 只走 err（不退化为截全屏）。
    """
    wait_if_paused(ctx)
    p = dict(node.params or {})
    range_mode = str(p.get("range_mode", "window") or "window")
    lang = str(p.get("lang", "ch") or "ch")
    roi = p.get("roi", (0, 0, 0, 0))
    match_mode = str(p.get("match_mode", "contains") or "contains")
    match_text = str(p.get("match_text", "") or "")
    empty_out = {"text": "", "items": []}

    try:
        from src.blueprint.core.variable_engine import VariableEngine
        engine = getattr(ctx, "var_engine", None) or VariableEngine(ctx.variables)
    except Exception:
        engine = None

    # ROI 变量插值 + 规范化
    if isinstance(roi, str) and engine is not None and engine.has_any_marker(roi):
        roi = engine.interpolate(roi)
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        roi = (0, 0, 0, 0)
    try:
        roi = tuple(int(v) for v in roi)
    except Exception:
        roi = (0, 0, 0, 0)

    # 取图像：range_mode=window 时截目标窗口，fullscreen 时截整屏
    # 目标窗口解析：window 模式必须先拿到窗口，找不到直接 err（不退化为全屏）
    target = None
    if range_mode == "window":
        target = _resolve_target_rect(ctx, p, tag="OCR")
        if target is None:
            err = _target_err_msg(p, "OCR")
            ctx.log(f"[OCR] {err}", "ERROR")
            annotate_step(ctx, node, "ocr_err", note=err[:60])
            return empty_out, ["err"], err
    img = None
    if target is not None:
        # ① 优先后台截窗：窗口被遮挡时也能拿到实时画面，不前置、不抢焦点
        try:
            from src.blueprint.runtime.screen_capture import capture_window_bgr
            img = capture_window_bgr(target[0])
        except Exception:
            img = None
    if img is None:
        # ② 回退：还原+置顶目标窗口后全屏截取、按窗口矩形裁剪
        #   （这是「同一目标窗口」的取图方式回退，不是改成截全屏）
        if target is not None:
            _bring_window_to_front_safe(ctx, target[0])
            time.sleep(0.15)  # 等待窗口前置与重绘完成
        full = ScreenCapture().capture_full_bgr()
        if full is None:
            err = "截图失败"
            ctx.log(f"[OCR] {err}", "ERROR")
            return empty_out, ["err"], err
        img = full
        if target is not None:
            rect = target[1]
            l, t, r, b = (int(v) for v in rect)
            H, W = full.shape[:2]
            l = max(0, l)
            t = max(0, t)
            r = min(W, r)
            b = min(H, b)
            if r > l and b > t:
                img = full[t:b, l:r]
            else:
                err = "目标窗口矩形无效（窗口可能完全在屏幕外、已最小化或被隐藏）"
                ctx.log(f"[OCR] {err}", "ERROR")
                annotate_step(ctx, node, "ocr_err", note=err[:60])
                return empty_out, ["err"], err
    img = _crop_roi(img, roi)

    if img is None or getattr(img, "size", 0) == 0:
        err = "待识别图像为空"
        ctx.log(f"[OCR] {err}", "ERROR")
        return empty_out, ["err"], err

    # 识别（懒加载引擎，首次较慢，之后复用）
    try:
        from .ocr_engine import recognize as ocr_recognize
        result = ocr_recognize(img, lang=lang)
    except Exception as e:
        err = f"OCR 识别失败: {type(e).__name__}: {e}"
        ctx.log(f"[OCR] {err}", "ERROR")
        annotate_step(ctx, node, "ocr", note="ERROR: " + err[:80])
        return empty_out, ["err"], err

    items = result.get("items") or []
    text = result.get("text") or ""
    if result.get("error"):
        ctx.log(f"[OCR] 引擎返回异常: {result['error']}", "WARN")

    # 耗时证据：区分「引擎初始化/首跑慢」和「稳态单张推理慢」（解法完全不同）
    infer_ms = result.get("infer_ms")
    init_ms = result.get("init_ms") or 0
    engine_info = result.get("engine_info") or ""
    timing = ""
    if infer_ms is not None:
        timing = f" 推理={infer_ms}ms 图={img.shape[1]}x{img.shape[0]}"
        first = []
        if engine_info:
            first.append(engine_info)
        if init_ms:
            first.append(f"引擎初始化 {init_ms}ms")
        if first:
            timing += "（本条首次：" + " + ".join(first) + "，仅出现一次）"

    out = {"text": text, "items": items}

    # 一条文字都没识别到 -> 走「无文字」（与「失败」分开，也不再往下做匹配）
    if not items:
        note = f"lang={lang} items=0 无文字{timing}"
        annotate_step(ctx, node, "ocr", note=note)
        ctx.log(f"[OCR] {note}", "WARN")
        return empty_out, ["out_done", "no_text"], ""

    # 文本匹配（只在填了「匹配文本」时做）：命中 -> matched，未命中 -> not_matched
    ports = ["out_done"]
    match_note = "match=off"
    if match_text:
        try:
            if match_mode == "regex":
                import re as _re
                hit = _re.search(match_text, text) is not None
            else:
                hit = match_text in text
        except Exception as e:
            err = f"匹配表达式非法（{match_mode}）：{e}"
            ctx.log(f"[OCR] {err}", "ERROR")
            annotate_step(ctx, node, "ocr", note="ERROR: " + err[:80])
            return out, ["err"], err
        ports.append("matched" if hit else "not_matched")
        match_note = f"match[{match_mode}]={match_text!r} -> {'命中' if hit else '未命中'}"

    note = f"lang={lang} items={len(items)} text={text[:30]!r} {match_note}{timing}"
    annotate_step(ctx, node, "ocr", note=note)
    ctx.log(f"[OCR] {note}", "SUCCESS")
    return out, ports, ""


# 截图 / OCR 节点的执行函数定义在 EXECUTORS 之后，这里补注册
EXECUTORS["Screenshot"] = execute_Screenshot
EXECUTORS["OcrRecognize"] = execute_OcrRecognize
# 并行节点（Parallel / Branch）定义在 EXECUTORS 之后，这里补注册
EXECUTORS["Parallel"] = execute_Parallel
EXECUTORS["Branch"] = execute_Branch


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
      - Signal: log_message(str, str) / highlight_node(str, object) / finished_ok / finished_err(str)
      - 严格不直接操作 Qt 控件
    """

    log_message = Signal(str, str)
    # 参数2 = 执行点所在的「容器链」（list / None）：
    #   [] = 根图；[LoopId] = 循环体；[LoopId, ParId] = 循环体里的并行体；None = 不可导航（组件子蓝图）
    # 编辑器靠它把画布「钻」进正在执行的子画布层再高亮（节点 id 各图独立编号，只看裸 id 无法定位层级）
    highlight_node = Signal(str, object)
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

    def __init__(self, graph: BlueprintGraph, parent=None, initial_variables: Optional[dict] = None):
        super().__init__(parent)
        self._graph = graph
        self._ctx: Optional[ExecutionContext] = None
        # 仅编辑器「组件蓝图独立运行」使用：把临时测试入参（__arg_0…）预置进变量表，
        # 让组件 Start 的 param_0… 端口能取到值。这只是运行时内存里的变量，
        # 不写回蓝图文件；导出后的 EXE 不会传这个参数。
        self._initial_variables: Dict[str, Any] = dict(initial_variables or {})

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

        # 预置变量（组件蓝图独立运行时的临时入参）：只进变量表，不碰蓝图文件
        if self._initial_variables:
            try:
                ctx.variables.update(self._initial_variables)
                ctx.log(
                    f"[Runtime] 已注入 {len(self._initial_variables)} 个临时入参"
                    f"（__arg_0…），供本次运行调试使用；不会写入蓝图。",
                    "INFO",
                )
            except Exception:
                pass

        # 运行期不再有「全局目标窗口」概念：每个节点的操作范围 100% 由它自己的
        # range_mode + 目标窗口定位条件决定（window=只在该窗口内操作，fullscreen=整屏）。
        _write_log("[Runtime] 目标窗口由各节点的「范围模式」决定（目标窗口 / 全屏），无全局绑定。", "INFO")

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
        # 根图的执行路径 = []（编辑器把画布跟到根图这一层），并 seed 本线程的容器链，
        # 供根图上的子画布容器算出自身路径（见文件头「执行路径」说明）
        try:
            graph._runtime_path = []
            _exec_path_set([])
        except Exception:
            pass
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
        # 第二个参数 = 该节点所在的「容器链」：编辑器据此把画布跟随到对应子画布层
        self.highlight_node.emit(node.id, _exec_path_of(graph))
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
