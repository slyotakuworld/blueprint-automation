# -*- coding: utf-8 -*-
"""Blueprint Automation 开发 / 打包工具（真实实现）

启动链：``start.cmd``（纯 ASCII 启动器）
        → ``start.mjs``（中文菜单 + 环境依赖编排 + 调度）
        → ``scripts/bp_dev.py``（本文件：真正的构建 / 启动逻辑）

本文件取代了旧的三个批处理：
  * ``run.cmd``           → ``bp_dev.py run``
  * ``build.cmd``         → ``bp_dev.py build-all``
  * ``build_runtime.cmd`` → ``bp_dev.py build-runtime``

子命令
------
``run``            检查依赖后启动编辑器（开发模式）
``build-editor``   打包编辑器（PyInstaller ``--onedir`` → ``dist/BlueprintAutomation``）
``build-runtime``  重打运行时模板 ``BlueprintRuntime.exe``（``--onefile``，供「导出为独立 EXE」用）
``build-all``      编辑器 + 运行时模板（等价旧 ``build.cmd``）
``env``            环境与模板新鲜度体检

之所以把构建逻辑放在 Python 而不是批处理里：PyInstaller 的参数（尤其
``--add-data`` 的 ``源;目标`` 与 19 个 ``--hidden-import``）在 cmd 里极易被引号/转义坑掉，
而 subprocess 参数列表是逐项传递、不过 shell，天然可靠。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))

APP_NAME = "BlueprintAutomation"
RUNTIME_NAME = "BlueprintRuntime"

ENTRY_EDITOR = os.path.join(ROOT, "src", "blueprint_app.py")
ENTRY_RUNTIME = os.path.join(ROOT, "src", "blueprint_runner.py")
ICON = os.path.join(ROOT, "icon.png")

DIST_DIR = os.path.join(ROOT, "dist")
OUT_EDITOR = os.path.join(DIST_DIR, APP_NAME)          # 发布版目录
WORK_EDITOR = os.path.join(DIST_DIR, "build")          # 编辑器中间产物
WORK_RUNTIME = os.path.join(DIST_DIR, "build_runtime")  # 运行时中间产物
DIST_RUNTIME = os.path.join(DIST_DIR, "runtime")       # 运行时产物暂存

RUNTIME_TEMPLATE_ROOT = os.path.join(ROOT, RUNTIME_NAME + ".exe")

REQUIREMENTS = os.path.join(ROOT, "requirements.txt")

# 运行时代码：改动这些文件后必须重打模板，否则导出的 EXE 跑的还是旧代码
RUNTIME_SOURCES = [
    os.path.join(ROOT, "src", "blueprint_runner.py"),
    os.path.join(ROOT, "src", "blueprint", "runtime"),
]

# 运行时模板需要手动声明的动态导入（懒加载 / 反射调用，PyInstaller 静态分析不到）
RUNTIME_HIDDEN_IMPORTS = [
    "win32gui", "win32process", "win32clipboard", "win32con", "win32api",
    "pythoncom", "pywintypes", "psutil", "pyautogui", "pyperclip",
    "pydirectinput", "pynput", "pynput.keyboard", "soundcard", "pyaudio",
    "cv2", "numpy", "PIL",
]

# 编辑器启动前快速探测的关键包（requirements.txt 的粗粒度代表）
KEY_IMPORTS = ["PySide6", "cv2", "numpy", "PIL", "win32gui", "psutil", "pynput"]


# ---------------------------------------------------------------------------
# 输出helper（中文）
# ---------------------------------------------------------------------------

def _enable_utf8() -> None:
    """让中文输出在 cmd / PowerShell 里正常显示（无法改写时静默跳过）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    log(f"[OK] {msg}")


def warn(msg: str) -> None:
    log(f"[警告] {msg}")


def fail(msg: str) -> None:
    log(f"[错误] {msg}")


def step(msg: str) -> None:
    log("")
    log(f"==> {msg}")


def human_mtime(path: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return "未知"


def human_size(path: str) -> str:
    try:
        return f"{os.path.getsize(path) / 1048576:.1f} MB"
    except OSError:
        return "未知"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def python_exe() -> str:
    """当前解释器（启动链保证它是 .venv 里的 python）。"""
    return sys.executable


def run(args: list[str], **kw) -> int:
    """前台运行，输出直通终端。"""
    log("[执行] " + " ".join(args))
    try:
        return subprocess.call(args, cwd=ROOT, **kw)
    except FileNotFoundError as exc:
        fail(f"找不到可执行文件：{args[0]}（{exc}）")
        return 1


def run_quiet(args: list[str]) -> tuple[int, str]:
    """静默运行并拿回 stdout（用于探测）。"""
    try:
        p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except OSError:
        return 1, ""


def have_module(name: str) -> bool:
    code = f"import {name}"
    rc, _ = run_quiet([python_exe(), "-c", code])
    return rc == 0


def pyinstaller_version() -> str | None:
    rc, out = run_quiet([python_exe(), "-m", "PyInstaller", "--version"])
    if rc != 0:
        return None
    return out.strip().splitlines()[-1].strip() if out.strip() else "未知"


def ensure_pyinstaller(*, auto_install: bool = True) -> bool:
    ver = pyinstaller_version()
    if ver:
        ok(f"PyInstaller 已安装（{ver}）")
        return True
    warn("未检测到 PyInstaller，打包功能需要它。")
    if not auto_install:
        log("  手动安装：pip install pyinstaller")
        return False
    step("正在安装 PyInstaller（pip install pyinstaller）……")
    if run([python_exe(), "-m", "pip", "install", "pyinstaller"]) != 0:
        fail("PyInstaller 安装失败。可手动执行：pip install pyinstaller")
        return False
    if not pyinstaller_version():
        fail("PyInstaller 安装后仍无法调用，请检查虚拟环境是否损坏。")
        return False
    ok("PyInstaller 安装完成")
    return True


def ensure_requirements(*, force: bool = False, auto_install: bool = True) -> bool:
    """检查关键依赖；缺失（或 --force）时按 requirements.txt 安装。"""
    if not force:
        missing = [m for m in KEY_IMPORTS if not have_module(m)]
        if not missing:
            ok("关键依赖已就绪（PySide6 / cv2 / numpy / PIL / win32gui / psutil / pynput）")
            return True
        warn("缺少依赖：" + "、".join(missing))
    if not os.path.exists(REQUIREMENTS):
        fail(f"找不到依赖清单：{REQUIREMENTS}")
        return False
    if not auto_install:
        log(f"  手动安装：{python_exe()} -m pip install -r requirements.txt")
        return False
    step("正在安装依赖（pip install -r requirements.txt）……")
    # 刻意不执行 pip 自升级：受限/沙箱环境下 pip 卸载旧版会触发安全删除失败，
    # 导致 venv 里的 pip 损坏、后续安装全部失败。venv 自带 pip 足够装常规包。
    rc = run([python_exe(), "-m", "pip", "install", "-r", REQUIREMENTS])
    if rc != 0:
        warn("pip 退出码非 0（个别包如 PyAudio 可能编译失败）。")
        warn(f"  可稍后手动重试：{python_exe()} -m pip install -r requirements.txt")
        return False
    ok("依赖安装完成")
    return True


def _rm_tree(path: str, label: str) -> None:
    """尽力清理中间产物；失败只警告不阻塞（PyInstaller --clean 会兜底）。"""
    if not os.path.exists(path):
        return
    log(f"  清理 {label}：{path}")
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        warn(f"  清理 {label} 失败（可忽略）：{exc}")
    if os.path.exists(path):
        warn(f"  {label} 仍存在（可能有文件被占用），继续交给 PyInstaller 处理")


def _pyinstaller(args: list[str], *, dry_run: bool = False) -> int:
    cmd = [python_exe(), "-m", "PyInstaller", *args]
    if dry_run:
        log("[dry-run] " + " ".join(cmd))
        return 0
    return run(cmd)


def _drop_stray_spec(name: str) -> None:
    """PyInstaller 偶尔会在 cwd 落一个 spec，清掉免得污染仓库根目录。"""
    p = os.path.join(ROOT, f"{name}.spec")
    if os.path.exists(p):
        try:
            os.remove(p)
            log(f"  已删除根目录残留 spec：{os.path.basename(p)}")
        except OSError as exc:
            warn(f"  删除残留 spec 失败：{exc}")


# ---------------------------------------------------------------------------
# 子命令：run
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    if not ensure_requirements(force=args.force, auto_install=not args.no_install):
        fail("依赖未就绪，已中止启动。")
        log(f"  可单独执行：{python_exe()} scripts/bp_dev.py build-editor 之外先补依赖")
        return 1
    if not os.path.exists(ENTRY_EDITOR):
        fail(f"找不到编辑器入口：{ENTRY_EDITOR}")
        return 1

    step("启动蓝图编辑器（开发模式）")
    log(f"  解释器：{python_exe()}")
    log(f"  入口  ：{ENTRY_EDITOR}")
    rc = run([python_exe(), ENTRY_EDITOR])
    if rc != 0:
        fail(f"编辑器退出码 {rc}")
    else:
        ok("编辑器已正常退出")
    return rc


# ---------------------------------------------------------------------------
# 子命令：build-editor
# ---------------------------------------------------------------------------

def cmd_build_editor(args: argparse.Namespace) -> int:
    if not os.path.exists(ENTRY_EDITOR):
        fail(f"找不到编辑器入口：{ENTRY_EDITOR}")
        return 1
    if not ensure_pyinstaller():
        return 1

    step("清理旧的编辑器构建产物")
    if not args.dry_run:
        _rm_tree(os.path.join(ROOT, "build"), "根目录 build/")
        _rm_tree(WORK_EDITOR, "dist/build/")
        os.makedirs(WORK_EDITOR, exist_ok=True)

    step(f"打包编辑器（--onedir）→ dist/{APP_NAME}")
    rc = _pyinstaller([
        "--name", APP_NAME,
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--icon", ICON,
        "--workpath", WORK_EDITOR,
        "--distpath", DIST_DIR,
        "--specpath", WORK_EDITOR,
        # 内置手册与章节图标：打包后位于 INTERNAL_DIR/assets/...（与运行时路径一致）
        "--add-data", f"{os.path.join(ROOT, 'assets', 'doc')};assets/doc",
        "--add-data", f"{os.path.join(ROOT, 'assets', 'doc_icons')};assets/doc_icons",
        "--add-data", f"{ICON};.",
        ENTRY_EDITOR,
    ], dry_run=args.dry_run)
    _drop_stray_spec(APP_NAME)

    if args.dry_run:
        log("[dry-run] 已跳过实际构建。")
        return 0
    if rc != 0:
        fail(f"编辑器打包失败（退出码 {rc}），详见上方 PyInstaller 日志。")
        return rc

    exe = os.path.join(OUT_EDITOR, APP_NAME + ".exe")
    if not os.path.exists(exe):
        fail(f"未找到预期产物：{exe}")
        return 1
    ok(f"编辑器打包完成：{exe}（{human_size(exe)}）")
    log(f"  中间产物：{WORK_EDITOR}")
    return 0


# ---------------------------------------------------------------------------
# 子命令：build-runtime
# ---------------------------------------------------------------------------

def cmd_build_runtime(args: argparse.Namespace) -> int:
    if not os.path.exists(ENTRY_RUNTIME):
        fail(f"找不到运行时入口：{ENTRY_RUNTIME}")
        return 1
    if not ensure_pyinstaller():
        return 1

    step("清理旧的运行时构建产物")
    if not args.dry_run:
        _rm_tree(WORK_RUNTIME, "dist/build_runtime/")

    step("打包运行时模板（--onefile）")
    log("  说明：模板里嵌的是【此刻】的 runtime 代码；改了 src/blueprint/runtime/** 就要重打。")
    pyi_args = [
        "--name", RUNTIME_NAME,
        "--windowed",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--icon", ICON,
        "--workpath", WORK_RUNTIME,
        "--distpath", DIST_RUNTIME,
        "--specpath", WORK_RUNTIME,
        "--paths", ROOT,
    ]
    for mod in RUNTIME_HIDDEN_IMPORTS:
        pyi_args += ["--hidden-import", mod]
    pyi_args.append(ENTRY_RUNTIME)

    t0 = time.time()
    rc = _pyinstaller(pyi_args, dry_run=args.dry_run)
    _drop_stray_spec(RUNTIME_NAME)
    if args.dry_run:
        log("[dry-run] 已跳过实际构建与部署。")
        return 0
    if rc != 0:
        fail(f"运行时模板构建失败（退出码 {rc}），详见上方 PyInstaller 日志。")
        return rc

    built = os.path.join(DIST_RUNTIME, RUNTIME_NAME + ".exe")
    if not os.path.exists(built):
        fail(f"未找到预期产物：{built}")
        return 1

    # 模板一律先落到仓库根（开发模式下编辑器就是读它），需要时再额外复制一份到发布目录。
    targets = [RUNTIME_TEMPLATE_ROOT]
    if args.out:
        targets.append(os.path.normpath(args.out))

    step("部署运行时模板")
    for dst in targets:
        d = os.path.dirname(dst)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        try:
            shutil.copy2(built, dst)
        except OSError as exc:
            fail(f"复制到 {dst} 失败（目标可能正在运行/被占用）：{exc}")
            return 2
        ok(f"{dst}（{human_size(dst)}，{human_mtime(dst)}）")

    ok(f"运行时模板构建完成，耗时 {time.time() - t0:.1f}s")
    return 0


# ---------------------------------------------------------------------------
# 子命令：build-all
# ---------------------------------------------------------------------------

def cmd_build_all(args: argparse.Namespace) -> int:
    log("将依次打包：① 编辑器（onedir） ② 运行时模板（onefile）")
    rc = cmd_build_editor(args)
    if rc != 0:
        fail("编辑器打包未通过，已跳过运行时模板。")
        return rc

    step("继续打包运行时模板 → dist/BlueprintAutomation")
    ns = argparse.Namespace(out=OUT_EDITOR, dry_run=args.dry_run)
    rc = cmd_build_runtime(ns)
    if rc != 0:
        return rc

    if args.dry_run:
        log("[dry-run] 全部命令已打印，未真正构建。")
        return 0

    log("")
    ok("全部打包完成，产物布局：")
    log(f"    发布版程序   = dist/{APP_NAME}/{APP_NAME}.exe")
    log(f"    运行时模板   = {RUNTIME_TEMPLATE_ROOT}（开发模式导出用）")
    log(f"                   dist/{APP_NAME}/{RUNTIME_NAME}.exe（发布版导出用）")
    log(f"    中间产物     = {WORK_EDITOR}")
    return 0


# ---------------------------------------------------------------------------
# 子命令：env
# ---------------------------------------------------------------------------

def _runtime_source_mtime() -> float:
    newest = 0.0
    for p in RUNTIME_SOURCES:
        if os.path.isfile(p):
            newest = max(newest, os.path.getmtime(p))
        elif os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for f in files:
                    if f.endswith(".py"):
                        newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
    return newest


def cmd_env(args: argparse.Namespace) -> int:
    log("Blueprint Automation · 环境信息")
    log("-" * 52)

    # Python
    log(f"项目根目录 : {ROOT}")
    log(f"Python     : {sys.version.split()[0]}  （{python_exe()}）")
    in_venv = os.path.normcase(python_exe()).startswith(os.path.normcase(os.path.join(ROOT, ".venv")))
    log(f"虚拟环境   : {'已启用 .venv' if in_venv else '未使用 .venv（用的是系统 Python）'}")

    # Node / PyInstaller
    rc, out = run_quiet(["node", "--version"])
    log(f"Node.js    : {out.strip() if rc == 0 else '未检测到（start.cmd 会先拦住）'}")
    ver = pyinstaller_version()
    log(f"PyInstaller: {ver if ver else '未安装（打包时自动安装）'}")

    # 依赖
    log("")
    log("关键依赖：")
    for mod in KEY_IMPORTS:
        log(f"  {'✔' if have_module(mod) else '✘'} {mod}")
    for extra in ("paddleocr", "paddle", "pandas", "shapely"):
        log(f"  {'✔' if have_module(extra) else '✘'} {extra}（OCR 相关，运行时模板需要）")

    # 资源（手册 / 图标）
    log("")
    doc_dir = os.path.join(ROOT, "assets", "doc")
    icon_dir = os.path.join(ROOT, "assets", "doc_icons")
    if os.path.isdir(doc_dir):
        mds = [n for n in os.listdir(doc_dir) if n.endswith(".md")]
        log(f"内置手册   : assets/doc（{len(mds)} 篇 md）")
    else:
        warn(f"内置手册目录缺失：{doc_dir}")
    if os.path.isdir(icon_dir):
        pngs = [n for n in os.listdir(icon_dir) if n.endswith(".png")]
        log(f"章节图标   : assets/doc_icons（{len(pngs)} 个 png）")
    else:
        warn(f"章节图标目录缺失：{icon_dir}")

    # 运行时模板新鲜度（导出的 EXE 是否会跑旧代码）
    log("")
    log("运行时模板：")
    src_mt = _runtime_source_mtime()
    if not os.path.exists(RUNTIME_TEMPLATE_ROOT):
        warn(f"  {RUNTIME_NAME}.exe 不存在 → 导出前请先执行：start.cmd build-runtime")
    else:
        tpl_mt = os.path.getmtime(RUNTIME_TEMPLATE_ROOT)
        log(f"  模板：{human_mtime(RUNTIME_TEMPLATE_ROOT)}（{human_size(RUNTIME_TEMPLATE_ROOT)}）")
        log(f"  源码：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(src_mt))}"
            f"（src/blueprint_runner.py + src/blueprint/runtime/**）")
        if src_mt > tpl_mt:
            warn("模板比运行时代码旧 → 导出的 EXE 会跑旧代码，请执行：start.cmd build-runtime")
        else:
            ok("模板是最新的，导出可放心进行")

    out_exe = os.path.join(OUT_EDITOR, APP_NAME + ".exe")
    log("")
    log(f"发布版程序 : {out_exe}（{human_size(out_exe)}，{human_mtime(out_exe)}）"
        if os.path.exists(out_exe) else f"发布版程序 : 尚未打包（start.cmd → 3. 打包发布版）")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bp_dev.py",
        description="Blueprint Automation 开发/打包工具（由 start.cmd → start.mjs 调用）",
    )
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="启动编辑器（开发模式）")
    p.add_argument("--force", action="store_true", help="强制重装依赖")
    p.add_argument("--no-install", action="store_true", help="只检查依赖，不自动安装")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("build-editor", help="打包编辑器（--onedir）")
    p.add_argument("--dry-run", action="store_true", help="只打印 PyInstaller 命令，不真正构建")
    p.set_defaults(func=cmd_build_editor)

    p = sub.add_parser("build-runtime", help="重打运行时模板（--onefile）")
    p.add_argument("--out", default=None, help="额外把模板复制到该目录（默认只落仓库根）")
    p.add_argument("--dry-run", action="store_true", help="只打印 PyInstaller 命令，不真正构建")
    p.set_defaults(func=cmd_build_runtime)

    p = sub.add_parser("build-all", help="编辑器 + 运行时模板（等价旧 build.cmd）")
    p.add_argument("--dry-run", action="store_true", help="只打印两段 PyInstaller 命令")
    p.set_defaults(func=cmd_build_all)

    p = sub.add_parser("env", help="环境与模板新鲜度体检")
    p.set_defaults(func=cmd_env)

    return ap


def main(argv: list[str] | None = None) -> int:
    _enable_utf8()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("")
        warn("已被用户中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
