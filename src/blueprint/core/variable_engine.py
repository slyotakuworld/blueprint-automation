import re
import ast
from typing import Any, Dict, Optional, Tuple


# ============================================================
# 变量插值正则：
#   - ${var}  优先匹配（含 {ALL_VARS} 等）
#   - {var}   兼容
# 统一先把 ${...} 归一化到 {_...}，最后再解析内层名字
# ============================================================
_PATTERN_BRACE_DOLLAR = re.compile(r"\$\{([^{}]+)\}")   # ${...}
_PATTERN_BRACE_BARE = re.compile(r"\{([^{}]+)\}")      # {...}  （不含花括号，表达式 {} 用 {= ... } 特殊分隔）
_PATTERN_EXPR = re.compile(r"\{\=\s*([\s\S]+?)\s*\}")  # {= expr }  表达式（允许换行当多行，不允许嵌套花括号表达式）


# 单测友好：提取所有 字面量+变量+表达式 token，用于“已插值/未插值”判断
_TOKEN_LITERAL = "L"
_TOKEN_VAR = "V"
_TOKEN_EXPR = "E"


class VariableEngine:
    """图级变量插值引擎：
        1) ${var}/{var} 变量替换
        2) {= 1+2 } 表达式 eval（受限环境，无import/内置危险函数）
        3) {ALL_VARS} 魔法展开
    优先级：DATA连线值 > 插值结果 > 默认参数值（此 Engine 只负责插值，优先级由 Executor 组装参数时保证）。
    """

    MAGIC_ALL_VARS = "ALL_VARS"

    # 内置白名单：eval 仅能访问这些，彻底禁止 __import__ / exec / eval / getattr/ setattr 等
    _SAFE_BUILTINS: Dict[str, Any] = {
        "True": True,
        "False": False,
        "None": None,
        "abs": abs,
        "min": min,
        "max": max,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "dict": dict,
        "tuple": tuple,
        "set": set,
        "round": round,
        "sum": sum,
        "sorted": sorted,
        "reversed": reversed,
        "range": range,
        "enumerate": enumerate,
        "zip": zip,
        "any": any,
        "all": all,
        "divmod": divmod,
        "pow": pow,
        "hex": hex,
        "oct": oct,
        "bin": bin,
        "ord": ord,
        "chr": chr,
        "isinstance": isinstance,
    }

    def __init__(self, variables: Optional[Dict[str, Any]] = None):
        self.vars: Dict[str, Any] = dict(variables) if variables else {}
        # 防循环引用：同一字符串插值递归深度限制
        self._MAX_RECURSE = 6

    # ------------------------------------------------------------
    # 公共 API：变量读写
    # ------------------------------------------------------------
    def set(self, name: str, value: Any):
        name = str(name or "").strip()
        if not name:
            return
        # 禁止保留名（表达式语法糖关键字）
        if name in ("True", "False", "None"):
            return
        self.vars[name] = value

    def get(self, name: str, default: Any = None):
        name = str(name or "").strip()
        if name == self.MAGIC_ALL_VARS:
            return self._render_all_vars()
        return self.vars.get(name, default)

    def has(self, name: str) -> bool:
        name = str(name or "").strip()
        if name == self.MAGIC_ALL_VARS:
            return True
        return name in self.vars

    def update(self, mapping: Dict[str, Any]):
        for k, v in (mapping or {}).items():
            self.set(k, v)

    def clear(self):
        self.vars.clear()

    def names(self):
        return list(self.vars.keys())

    # ------------------------------------------------------------
    # 公共 API：核心插值
    # ------------------------------------------------------------
    def interpolate(self, value: Any, *, depth: int = 0, disable_all_vars: bool = False) -> Any:
        """递归插值：str 做 ${var}/{var}/{= expr} 替换；list/tuple/dict 按结构递归。"""
        if depth > self._MAX_RECURSE:
            return value
        if isinstance(value, str):
            return self._interpolate_str(value, depth=depth, disable_all_vars=disable_all_vars)
        if isinstance(value, list):
            return [self.interpolate(v, depth=depth + 1, disable_all_vars=disable_all_vars) for v in value]
        if isinstance(value, tuple):
            return tuple(self.interpolate(v, depth=depth + 1, disable_all_vars=disable_all_vars) for v in value)
        if isinstance(value, dict):
            return {
                self.interpolate(k, depth=depth + 1, disable_all_vars=disable_all_vars)
                if isinstance(k, str) else k:
                self.interpolate(v, depth=depth + 1, disable_all_vars=disable_all_vars)
                for k, v in value.items()
            }
        # 其他：int/bool/PIL/Path → 原样
        return value

    def has_any_marker(self, value: Any) -> bool:
        """快速判断 value 是否含任何插值占位符（用于 Executor 短路优化：没标记就不 interpolate）。"""
        return self._scan_marker_str(value) if isinstance(value, str) else self._scan_marker_any(value)

    # ------------------------------------------------------------
    # 内部：字符串插值主流程
    # ------------------------------------------------------------
    def _interpolate_str(self, s: str, *, depth: int, disable_all_vars: bool) -> Any:
        if depth > self._MAX_RECURSE:
            return s
        # 先替换 ${var} → {var}
        def replace_dollar(m: re.Match) -> str:
            return "{" + m.group(1) + "}"

        # 表达式 {= ...} 优先（花括号里是表达式，里面的 {} 不匹配外层，不会与 ${var} 冲突）
        expr_placeholders: Dict[str, Any] = {}

        def replace_expr(m: re.Match) -> str:
            raw = m.group(1)
            key = f"__EXPR_{len(expr_placeholders)}__"
            expr_placeholders[key] = self._safe_eval(raw, disable_all_vars=disable_all_vars)
            return "{" + key + "}"

        stage1 = _PATTERN_EXPR.sub(replace_expr, s)
        stage2 = _PATTERN_BRACE_DOLLAR.sub(replace_dollar, stage1)

        # 解析所有 {xxx} 占位，把表达式 placeholder 直接回填，其他按变量名解析
        final_parts = []
        last = 0
        whole_is_expr = (len(expr_placeholders) == 1 and len(stage2) > 2 and stage2 == list(_iter_one_expr_token(stage2)))  # 只有一个表达式且整串就是它
        for m in _PATTERN_BRACE_BARE.finditer(stage2):
            if m.start() > last:
                final_parts.append(stage2[last : m.start()])
            name = m.group(1).strip()
            if not name:
                final_parts.append(m.group(0))
            elif name in expr_placeholders:
                val = expr_placeholders[name]
                final_parts.append(ExprLiteral(val))  # 用 Sentinel 标记：整串拼接时若是字面量，保留原类型
            else:
                val = self._resolve_var(name, disable_all_vars=disable_all_vars)
                if isinstance(val, MissingVar):
                    # 找不到：保留原占位（让 Executor 可按需回退到默认参数；也避免误替换成空串丢失信息）
                    final_parts.append(m.group(0))
                else:
                    final_parts.append(VarLiteral(val))
            last = m.end()
        if last < len(stage2):
            final_parts.append(stage2[last:])

        # ---------------- 合并策略：
        #  1) 若整个字符串最终只有"一个非字符串 token 且两边无字面量" → 直接返回该 token（str化不强制）
        #      例：整串是 "{= 42}" → 返回 int 42，不是 "42"
        #      例：整串是 "{port}" 且 port=8080 → 返回 int 8080，不是 "8080"（对 Click 的 x/y 也安全）
        #  2) 否则一律 str() 拼接，遇到 None → "None" 回退
        non_str_tokens = [p for p in final_parts if isinstance(p, (ExprLiteral, VarLiteral))]
        if len(final_parts) == 1 and len(non_str_tokens) == 1:
            return non_str_tokens[0].value
        # 多 token → 字符串拼接
        pieces = []
        for p in final_parts:
            if isinstance(p, (ExprLiteral, VarLiteral)):
                v = p.value
                if v is None:
                    pieces.append("")
                else:
                    pieces.append(str(v))
            else:
                pieces.append(p)
        return "".join(pieces)

    def _resolve_var(self, name: str, *, disable_all_vars: bool) -> Any:
        if disable_all_vars and name == self.MAGIC_ALL_VARS:
            return MissingVar()
        if name == self.MAGIC_ALL_VARS:
            return self._render_all_vars()
        if name in self.vars:
            return self.vars[name]
        # 支持点号属性访问：如 "target.hwnd" → getattr(vars["target"], "hwnd")，取不到则 MissingVar
        if "." in name:
            parts = name.split(".")
            cur = self.vars.get(parts[0])
            if cur is None:
                return MissingVar()
            for seg in parts[1:]:
                if isinstance(cur, dict):
                    if seg in cur:
                        cur = cur[seg]
                    else:
                        return MissingVar()
                else:
                    try:
                        cur = getattr(cur, seg)
                    except Exception:
                        return MissingVar()
            return cur
        return MissingVar()

    def _render_all_vars(self) -> str:
        lines = [f"{k} = {self._safe_str_repr(v)}" for k, v in self.vars.items()]
        return "\n".join(lines)

    @staticmethod
    def _safe_str_repr(v: Any) -> str:
        try:
            if isinstance(v, str):
                return v
            s = repr(v)
        except Exception:
            s = f"<{type(v).__name__}>"
        if len(s) > 2000:
            s = s[:2000] + "..."
        return s

    # ------------------------------------------------------------
    # 内部：安全 eval（AST 黑名单：禁止 import / 危险双下划线访问 / lambda 仅允许无副作用短语句）
    # ------------------------------------------------------------
    def _safe_eval(self, expr: str, *, disable_all_vars: bool) -> Any:
        expr = (expr or "").strip()
        if not expr:
            return ""
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            return f"<syntax_error: {expr[:60]}>"

        for node in ast.walk(tree):
            # 禁止：import / Name 用 __开头 / Attribute 访问 __*__
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return "<forbidden: import>"
            if isinstance(node, ast.Lambda):
                # 允许lambda，但限制不能太长（简单表达式 lambda x:x+1 没问题）
                if len(ast.dump(node)) > 400:
                    return "<forbidden: lambda_too_long>"
            if isinstance(node, ast.Name):
                if node.id.startswith("__") and node.id.endswith("__"):
                    return "<forbidden: dunder_access>"
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and node.attr.endswith("__"):
                    return "<forbidden: dunder_attr>"
            # 禁止直接调用 eval/exec/open/subprocess 等（即使他们在白名单之外）
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    bad = {"eval", "exec", "open", "compile", "globals", "locals", "vars", "breakpoint", "exit", "quit"}
                    if fn.id in bad:
                        return f"<forbidden_call: {fn.id}>"

        local_ns = {k: v for k, v in self.vars.items()}
        if not disable_all_vars:
            local_ns[self.MAGIC_ALL_VARS] = self._render_all_vars()

        try:
            code = compile(tree, "<vengine_expr>", "eval")
            return eval(code, {"__builtins__": self._SAFE_BUILTINS}, local_ns)
        except Exception as e:
            return f"<eval_error: {type(e).__name__} {str(e)[:60]}>"

    # ------------------------------------------------------------
    # 内部：占位符快速扫描
    # ------------------------------------------------------------
    def _scan_marker_str(self, s: str) -> bool:
        if not isinstance(s, str):
            return False
        return bool(_PATTERN_BRACE_DOLLAR.search(s) or _PATTERN_BRACE_BARE.search(s) or _PATTERN_EXPR.search(s))

    def _scan_marker_any(self, v: Any) -> bool:
        if isinstance(v, str):
            return self._scan_marker_str(v)
        if isinstance(v, (list, tuple)):
            return any(self._scan_marker_any(x) for x in v)
        if isinstance(v, dict):
            return any(self._scan_marker_any(k) or self._scan_marker_any(val) for k, val in v.items())
        return False


# ------------------------------------------------------------
# 辅助：Sentinel 类型（表达"这个token是表达式/变量结果，不需要被再次 str 化强制"）
# ------------------------------------------------------------
class MissingVar:
    """占位：变量未找到，保留原 {xxx} 写法（Executor 可据此判断是否回退默认参数）。"""
    pass


class ExprLiteral:
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value


class VarLiteral:
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value


# ------------------------------------------------------------
# 辅助：判断字符串是不是"仅一个 {expr}"（for整串返回原类型）
# ------------------------------------------------------------
def _iter_one_expr_token(s: str):
    """yield 每个字符，用于快速比较相等（判断整串是单一占位符）。"""
    for ch in s:
        yield ch


# ------------------------------------------------------------
# 辅助：SetVariable 批量文本解析（name=value 多行格式）
# ------------------------------------------------------------
_RE_VAR_DEFINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*(.*?)\s*$")


def parse_batch_var_lines(text: str) -> Tuple[Dict[str, str], str]:
    """解析多行 name=value 文本，返回 (变量dict, 错误信息)。

    语法：
        # 行注释
        var1 = 字符串字面量（整行，允许前后空格被strip）
        port = 8080
        msg  = hello world
        target.hwnd = 12345     # 允许点号（set 时按字典 key 的字符串存，实际访问交给 VariableEngine 点号解析）
    空行忽略。
    """
    result: Dict[str, str] = {}
    errors = []
    text = text or ""
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _RE_VAR_DEFINE.match(line)
        if not m:
            errors.append(f"第{idx}行格式错误（应为 name=value）: {raw_line}")
            continue
        name, val = m.group(1), m.group(2)
        # 去掉行尾注释（val 中若有'#'且前面是空格，视作注释开始——简化策略）
        # 但为避免 URL 里的 # 被误删，我们只在"前面有空格"的情况才截
        if " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        result[name] = val
    return result, "\n".join(errors)


# ------------------------------------------------------------
# 工具：按"优先级 DATA连线值 > 插值结果 > 默认参数值"组装节点最终运行参数
# ------------------------------------------------------------
def assemble_runtime_params(
    engine: Optional[VariableEngine],
    node_type: str,
    default_params: Dict[str, Any],
    node_stored_params: Dict[str, Any],
    data_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Executors 调用这个统一得到最终可执行的 params dict。

    优先级：
      1) DATA 连线覆写值（data_overrides，由 Executor 计算好传进来）—— 不做插值，上游给啥就是啥
      2) 节点已存 params（node_stored_params），若值是字符串/容器且含 {}/${}/{=  } 标记，则过 VariableEngine
      3) NodeMeta.defaults 里默认参数（不含 DATA 连线端口的字段，那些按端口语义在别处处理）
    """
    merged: Dict[str, Any] = {}
    # 默认 → 先铺底
    for k, v in (default_params or {}).items():
        merged[k] = v
    # 节点存储 params（含用户填的插值/硬编码）
    for k, v in (node_stored_params or {}).items():
        merged[k] = v
    # 插值：跳过明确是 DATA 连线覆盖过的值（data_overrides 会在最后一步覆写，这里只是为了避免对 data 值做不必要插值）
    data_overrides = data_overrides or {}
    for k in list(merged.keys()):
        if k in data_overrides:
            continue
        v = merged[k]
        if engine is not None and engine.has_any_marker(v):
            merged[k] = engine.interpolate(v)
    # 最后：DATA 连线最高优先级，直接覆写（不插值）
    for k, v in data_overrides.items():
        merged[k] = v
    return merged
