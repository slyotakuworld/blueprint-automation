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
``ocr-runtime``    准备 OCR：库装进 .venv（随模板打包）、模型预取到 ``ocr_runtime/models``。
                   日常由启动器菜单 1/2 自动完成（装库 + 缺模型时自动下载），此命令用于手动补齐，
                   ``--models-only`` 只下模型
``env``            环境与模板新鲜度体检

之所以把构建逻辑放在 Python 而不是批处理里：PyInstaller 的参数（尤其
``--add-data`` 的 ``源;目标`` 与 19 个 ``--hidden-import``）在 cmd 里极易被引号/转义坑掉，
而 subprocess 参数列表是逐项传递、不过 shell，天然可靠。
"""

from __future__ import annotations
import argparse
import os
import re
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

# OCR 运行时目录：2026-10-05 起只放**模型**。
#   ocr_runtime/models  ← 识别模型（paddleocr 首次使用会下到这里）
# OCR 的**库**（paddlepaddle / paddleocr）装进 .venv（ocr-runtime 子命令安装），
# 由 build-runtime 直接打进运行时模板；导出含「文字」节点的蓝图时只携带 models/。
OCR_RUNTIME_DIR = os.path.join(ROOT, "ocr_runtime")
OCR_LIB_DIR = os.path.join(OCR_RUNTIME_DIR, "lib")   # 旧布局残留，仅用于提示清理
OCR_MODELS_DIR = os.path.join(OCR_RUNTIME_DIR, "models")

# OCR 引擎的包名（用于判定 lib/ 是否真的可用：只认包目录，空目录不算）
# paddlepaddle 必须钉 3.2.2：3.3.0/3.3.1 的 oneDNN PIR 转换有回归，
# PP-OCRv6 模型 + MKLDNN 一推理就报
# NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<...>]
# （官方 issue paddlepaddle/paddle#77340）。3.2.2 + MKLDNN 正常且比关 oneDNN 快 3~6 倍。
# onnxruntime 是**推理后端**（不是模型库）：本机实测同一张图 0.86s vs paddle 原生 4.3s，
# 约 5 倍且输出一致——paddle 的卷积走 oneDNN，在只有 AVX2 的老 CPU 上内核严重退化。
# 缺了它功能不受影响（ocr_engine 会自动退回 paddle 后端），只是慢。
OCR_PKGS = ("paddlepaddle==3.2.2", "paddleocr", "onnxruntime")

# 运行时代码：改动这些文件后必须重打模板，否则导出的 EXE 跑的还是旧代码
RUNTIME_SOURCES = [
    os.path.join(ROOT, "src", "blueprint_runner.py"),
    os.path.join(ROOT, "src", "blueprint", "runtime"),
]

# 运行时模板需要手动声明的动态导入（懒加载 / 反射调用，PyInstaller 静态分析不到）
RUNTIME_HIDDEN_IMPORTS = [
    "win32gui", "win32process", "win32clipboard", "win32con", "win32api",
    "pythoncom", "pywintypes", "psutil", "pyautogui", "pyperclip",
    "pydirectinput", "pynput", "pynput.keyboard",
    "cv2", "numpy", "PIL",
]

# 编辑器启动前快速探测的关键包（requirements.txt 的粗粒度代表）
KEY_IMPORTS = ["PySide6", "cv2", "numpy", "PIL", "win32gui", "psutil", "pynput"]


# ---------------------------------------------------------------------------
# 「标准库补齐」——保险性的 stdlib 整包收集
# ---------------------------------------------------------------------------
# 历史（2026-10-05）：当时 paddle 外挂在 ocr_runtime/lib、不在打包期依赖图里，
# 它用到的 stdlib（configparser、http.cookies…）收不进模板，导致导出 EXE 必挂，
# 于是按 sys.stdlib_module_names 全量补齐（实测代价约 +10MB）。
# 现在 paddle 已进 .venv、进依赖图，stdlib 大多会被自动收集；这份补齐保留作为
# 保险（paddle/paddlex 有动态 import，静态图不保证全），删除可省约 10MB。
_STDLIB_SKIP = {
    "tkinter", "turtle", "turtledemo", "idlelib", "venv", "ensurepip",
    "lib2to3", "test", "pydoc_data", "curses", "distutils",
    "antigravity", "this",
}

# 非标准库但 paddle 运行时需要的第三方包，同样整包收集（实测半收集案例）：
#   paddle → No module named 'setuptools.command.easy_install'
#   paddle → cannot import name 'ImageEnhance' from 'PIL'
_EXTRA_COLLECT_PACKAGES = ["setuptools", "PIL"]


def _ocr_metadata_pyi_args() -> list[str]:
    """生成 --copy-metadata 参数：把 venv 里**全部**发行版的 dist-info 打进模板。

    原因：paddlex 创建 pipeline 时用 importlib.metadata 逐一核对依赖的版本
    （paddlex/utils/deps.py），frozen 里缺任何一条 dist-info 就报
    "A dependency error occurred during pipeline creation"。而 paddleocr 还会把
    真实原因包进 __cause__ 吞掉，逐个排查太费劲 —— 干脆全量复制。
    dist-info 都很小（每个几十 KB），全量代价可忽略。
    """
    try:
        import importlib.metadata as _im

        names = sorted({
            d.metadata["Name"] for d in _im.distributions() if d.metadata["Name"]
        })
    except Exception:
        return []
    args: list[str] = []
    for n in names:
        args += ["--copy-metadata", n]
    return args


def _stdlib_supplement_pyi_args() -> list[str]:
    """生成标准库补齐的 PyInstaller 参数（--collect-submodules / --hidden-import）。

    顶层是「包」用 --collect-submodules（连子模块一起收），
    是「单文件模块」用 --hidden-import。
    平台相关、本机 import 不进来的（pwd / grp / termios / fcntl / curses …）
    直接跳过，PyInstaller 也不会因为找不到而报错。
    """
    import importlib

    args: list[str] = []
    for name in sorted(sys.stdlib_module_names) + _EXTRA_COLLECT_PACKAGES:
        if name in _STDLIB_SKIP or name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if getattr(mod, "__path__", None):
            args += ["--collect-submodules", name]
        else:
            args += ["--hidden-import", name]
    return args


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


def _pinned_version_mismatches() -> list[str]:
    """requirements.txt 里 `name==ver` 钉版的包，已装版本不一致的列表。

    光查「包在不在」不够：钉版（如 paddlepaddle==3.2.2）必须核对版本，
    否则快路径永远跳过安装、钉版形同虚设。
    """
    import importlib.metadata

    bad: list[str] = []
    try:
        with open(REQUIREMENTS, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return bad
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;#]+)", s)
        if not m:
            continue
        name, want = m.group(1), m.group(2)
        try:
            got = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            bad.append(f"{name}（未安装，钉 {want}）")
            continue
        if got != want:
            bad.append(f"{name}（当前 {got}，钉 {want}）")
    return bad


def ensure_requirements(*, force: bool = False, auto_install: bool = True) -> bool:
    """检查关键依赖；缺失/版本不符（或 --force）时按 requirements.txt 安装。"""
    if not force:
        missing = [m for m in KEY_IMPORTS if not have_module(m)]
        if not missing and not ocr_lib_ready():
            # OCR 库单独查（find_spec 秒回，不触发 paddle 重型导入）
            missing = ["paddleocr（OCR 库，随 requirements.txt 一起安装）"]
        if not missing:
            bad = _pinned_version_mismatches()
            if not bad:
                ok("关键依赖已就绪（PySide6 / cv2 / numpy / PIL / win32gui / psutil / pynput + OCR 库，版本符合钉定）")
                return True
            warn("依赖版本与 requirements.txt 钉定的不一致：" + "、".join(bad))
        else:
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
        warn("pip 退出码非 0。")
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
        *_stdlib_supplement_pyi_args(),
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

def _deploy_ocr_runtime_to(app_dir: str) -> None:
    """把项目根 ocr_runtime/（OCR 模型）带到发布目录（exe 同级）。

    此前菜单 3/4 只部署 exe，发布目录里没有 ocr_runtime/ —— 发布版编辑器
    （无项目根兜底）既没法在编辑器里测文字节点，导出含文字节点的蓝图时
    exporter.carry_ocr_runtime 也找不到模型源（提示「OCR 未安装」）。
    策略：目标缺 ocr_runtime/models（或 models 为空）才整目录复制（先
    .copying 再原子替换）；已存在则跳过 —— 手动更新模型后删掉发布目录
    里的 ocr_runtime/ 即可在下次打包时重新带上。
    """
    src_models = os.path.join(OCR_MODELS_DIR, "official_models")
    if not os.path.isdir(src_models):
        warn("项目根没有 ocr_runtime/models/（未准备过 OCR 模型），发布目录将不带模型。")
        return
    dst_root = os.path.join(app_dir, "ocr_runtime")
    dst_models = os.path.join(dst_root, "models")
    try:
        if os.path.isdir(dst_models) and any(os.scandir(dst_models)):
            log(f"  OCR 模型已在位，跳过复制：{dst_root}")
            return
    except OSError:
        pass
    try:
        os.makedirs(app_dir, exist_ok=True)
        tmp = dst_root + ".copying"
        _rm_tree(tmp, "OCR 模型临时目录")
        log(f"  复制 OCR 模型 → {dst_root}（整个 ocr_runtime/，首次几秒~十几秒）")
        shutil.copytree(OCR_RUNTIME_DIR, tmp)
        if os.path.isdir(dst_root):
            shutil.rmtree(dst_root, ignore_errors=True)
        os.replace(tmp, dst_root)
        ok(f"OCR 模型已携带 → {dst_root}")
    except OSError as exc:
        warn(f"复制 OCR 模型到 {dst_root} 失败（目标可能正在运行/被占用）：{exc}")
        warn("  可手动复制项目根 ocr_runtime/ 到发布目录补齐。")


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
    # OCR 库（paddle 全家桶）已装进 .venv —— 2026-10-05 起直接打进模板。
    # paddleocr/paddlex 在 ocr_engine 里是懒加载，PyInstaller 静态分析看不到入口，
    # 必须 --collect-all（子模块 + 二进制 + 数据一起收）。没装 OCR 时跳过，
    # 模板照常打包（只是导出的蓝图跑不了文字节点）。
    import importlib.util as _ilu

    for pkg in ("paddleocr", "paddlex"):
        if _ilu.find_spec(pkg) is not None:
            log(f"  OCR 库内置：{pkg}（--collect-all）")
            pyi_args += ["--collect-all", pkg]
    if _ilu.find_spec("onnxruntime") is not None:
        # ONNX Runtime = OCR 的**推理后端**（可选但强烈建议）。本机实测同一张图
        # 0.86s vs paddle 原生 4.3s（约 5 倍，输出一致）。同为懒加载，静态分析
        # 看不到入口；它带一整套 C++ DLL，用 --collect-all 一次收齐。
        # 纯 CPU 包（onnxruntime，非 -gpu），只有 ~20MB，对模板体积影响很小。
        log("  OCR 库内置：onnxruntime（--collect-all，推理后端）")
        pyi_args += ["--collect-all", "onnxruntime", "--copy-metadata", "onnxruntime"]
    if _ilu.find_spec("paddle") is not None:
        # paddle **不能**用 --collect-all / --collect-submodules：那会逐个 import
        # 子模块做枚举，而 paddle.jit.sot 在隔离子进程里 import 直接段错误
        # （exit 3221225477 = 0xC0000005），枚举子进程整颗崩掉 → 构建失败。
        # 改为顶层 hidden-import + 数据/二进制整收，子模块由 modulegraph 静态
        # 分析 import 图收集（静态解析不执行代码，不会踩到 jit.sot 的坑）。
        log("  OCR 库内置：paddle（hidden + data/binaries，规避 jit.sot 段错误）")
        pyi_args += [
            "--hidden-import", "paddle",
            "--collect-data", "paddle",
            "--collect-binaries", "paddle",
            # paddle.jit.sot（符号追踪）在 PyInstaller 的分析子进程里 import 即段错误
            # （0xC0000005，两次构建失败均为它）——推理链路用不到，直接排除。
            "--exclude-module", "paddle.jit.sot",
        ]
        # paddlex 初始化时要读全部必需依赖的 dist-info 元数据做版本核对
        meta_args = _ocr_metadata_pyi_args()
        log(f"  OCR 元数据补齐：{len(meta_args) // 2} 个发行版（--copy-metadata）")
        pyi_args += meta_args
    if not any(p in pyi_args for p in ("paddleocr", "paddle")):
        warn("venv 里没有 OCR 库（paddleocr），模板将不含 OCR —— 需要时先跑 ocr-runtime。")
    # stdlib 保险补齐（说明见 _stdlib_supplement_pyi_args）
    stdlib_args = _stdlib_supplement_pyi_args()
    log(f"  标准库补齐：{len(stdlib_args) // 2} 个顶层包/模块")
    pyi_args += stdlib_args
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
        # 发布目录顺带带上 OCR 模型（项目根本来就有，跳过）——
        # 发布版编辑器没有项目根兜底，exe 同级没有 ocr_runtime/ 就用不了文字节点。
        app_dir = os.path.dirname(os.path.abspath(dst))
        if os.path.normcase(os.path.abspath(app_dir)) != os.path.normcase(os.path.abspath(ROOT)):
            _deploy_ocr_runtime_to(app_dir)

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
# 子命令：ocr-runtime
# ---------------------------------------------------------------------------

def ocr_lib_ready() -> bool:
    """.venv 里是否已装 OCR 库（只查 paddleocr 是否可定位，不触发重初始化）。"""
    import importlib.util

    try:
        return importlib.util.find_spec("paddleocr") is not None
    except Exception:
        return False


def ocr_backend() -> str:
    """当前 venv 下 ocr_engine 会实际选用的推理后端（与运行时同一套 auto 规则）。"""
    pref = (os.environ.get("BP_OCR_ENGINE") or "auto").strip().lower()
    if pref in ("paddle", "paddle_static", "native"):
        return "paddle"
    if pref == "onnxruntime":
        return "onnxruntime"
    import importlib.util

    try:
        has = importlib.util.find_spec("onnxruntime") is not None
    except Exception:
        has = False
    return "onnxruntime" if has else "paddle"


# 运行时真正会用到的模型（必须与 ocr_engine 的配置一致：只开 det + rec 两个阶段）。
# 后端决定需要哪一套格式 —— paddle 后端要 pdmodel/pdiparams，ONNX 后端要 *.onnx。
_OCR_MODELS_PADDLE = ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
_OCR_MODELS_ONNX = ("PP-OCRv6_medium_det_onnx", "PP-OCRv6_medium_rec_onnx")


def ocr_models_ready() -> bool:
    """ocr_runtime/models 里是否已有所需**格式**的 det+rec 模型。

    ⚠ 不能只判断「目录非空」：老环境里只有 paddle 格式的模型，一旦后端切到
    onnxruntime 就必须补下 ONNX 格式，否则导出物在离线机器上跑不起来。
    """
    base = os.path.join(OCR_MODELS_DIR, "official_models")
    names = _OCR_MODELS_ONNX if ocr_backend() == "onnxruntime" else _OCR_MODELS_PADDLE
    return all(os.path.isdir(os.path.join(base, n)) for n in names)


def _unused_model_dirs() -> list:
    """列出 ocr_runtime/models 里当前流水线用不到的模型目录（名, MB）。

    用不到的来源有两类：① 另一后端格式的那一份；② 早就关掉的三个阶段
    （doc_ori / textline_ori / UVDoc）残留 —— 它们对「屏幕截图」零贡献。
    只报告不删除：导出会把整个 models/ 带走，白占体积，交给用户决定。
    """
    base = os.path.join(OCR_MODELS_DIR, "official_models")
    need = set(_OCR_MODELS_ONNX if ocr_backend() == "onnxruntime" else _OCR_MODELS_PADDLE)
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name in need or not os.path.isdir(path):
            continue
        total = 0
        for root_dir, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root_dir, f))
                except OSError:
                    pass
        out.append((name, total / 1024 / 1024))
    return out


def _warmup_ocr_models() -> bool:
    """用 venv 里的库真跑一次识别，把所需模型下到 ocr_runtime/models（供离线导出）。

    ⚠ 必须**复用运行时引擎本身**（`src.blueprint.runtime.ocr_engine`），不要另写一份
    PaddleOCR(...) 参数：历史上这里写的是旧参数（开着 UVDoc/方向分类），导致把用不到的
    模型也拉下来白占几十 MB；后端（paddle / onnxruntime）与模型格式也必须跟运行时一致，
    否则会出现「预热下的是 paddle 模型、运行时却要 onnx 模型」→ 导出后联网才能跑。
    """
    code = (
        "import os, sys\n"
        "os.environ.setdefault('FLAGS_enable_pir_api', '0')\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import numpy as np\n"
        "from src.blueprint.runtime.ocr_engine import recognize\n"
        "r = recognize(np.full((32, 128, 3), 255, dtype=np.uint8), 'ch')\n"
        "assert not r.get('error'), 'OCR 不可用: ' + str(r.get('error'))\n"
        "print('OCR models ready:', r.get('engine_info'))\n"
    )
    env = dict(os.environ)
    env["PADDLE_PDX_CACHE_HOME"] = OCR_MODELS_DIR
    try:
        return subprocess.call([python_exe(), "-c", code], cwd=ROOT, env=env) == 0
    except OSError as exc:
        warn(f"  模型预取启动失败：{exc}")
        return False


def cmd_ocr_runtime(args: argparse.Namespace) -> int:
    """准备 OCR：库装进 .venv（随模板打包），模型预取到 ocr_runtime/models（随导出携带）。

    2026-10-05 方案变更：不再往 ocr_runtime/lib 装库（外挂 lib 体积太大）。
    """
    step("准备 OCR 运行时")
    log(f"  库   → .venv（随运行时模板打包，不再放 ocr_runtime/lib）")
    log(f"  模型 → {OCR_MODELS_DIR}（{ocr_backend()} 格式，导出含「文字」节点的 EXE 时自动携带）")

    models_only = getattr(args, "models_only", False)
    if models_only and not ocr_lib_ready():
        fail("venv 里还没有 OCR 库，请先运行 start.cmd（菜单 1/2 会自动装库）。")
        return 1

    pip_cmd = [
        python_exe(), "-m", "pip", "install", "--upgrade",
        *OCR_PKGS,
    ]

    if args.dry_run:
        if models_only:
            log(f"[dry-run] （跑一次真实识别，把所需模型下到 {OCR_MODELS_DIR}）")
        else:
            log("[dry-run] " + " ".join(pip_cmd))
            if not args.skip_models:
                log(f"[dry-run] （随后跑一次真实识别，把所需模型下到 {OCR_MODELS_DIR}）")
        log("[dry-run] 已跳过实际安装。")
        return 0

    if models_only:
        ok("仅预取模型（--models-only）：OCR 库已就绪，跳过安装步骤。")
    elif ocr_lib_ready() and not args.force:
        ok(f"OCR 库已就绪（.venv）：{OCR_PKGS}")
        log("  （如需重装：加 --force）")
    else:
        step("① 安装 OCR 库到 .venv（paddlepaddle 约 370MB，请耐心等待）")
        if run(pip_cmd) != 0:
            fail("OCR 库安装失败。可手动重试：")
            log(f"  {python_exe()} -m pip install {' '.join(OCR_PKGS)}")
            return 1
        if not ocr_lib_ready():
            fail("安装后仍未找到 paddleocr 包，请检查 pip 输出。")
            return 1
        ok("OCR 库已就绪（.venv）")

    # ② 模型：首次识别会自动下载；这里提前取好，导出的 EXE 才能离线可用。
    if args.skip_models:
        warn("已按 --skip-models 跳过模型预取（首次识别时会联网下载）。")
    elif ocr_models_ready() and not args.force:
        ok(f"OCR 模型已在位（后端 {ocr_backend()}）：{OCR_MODELS_DIR}")
    else:
        step("② 预取 OCR 模型到 ocr_runtime/models（首次约 180MB）")
        if _warmup_ocr_models():
            ok(f"OCR 模型已就绪：{OCR_MODELS_DIR}")
        else:
            warn("模型预取未成功（可稍后重跑本命令；首次识别时会自动下载）。")

    # ③ 旧布局清理提示：lib/ 已废弃，提示用户删掉省 550MB
    if os.path.isdir(OCR_LIB_DIR):
        warn(f"检测到旧布局 {OCR_LIB_DIR}（已废弃，库现在装在 .venv 并随模板打包）。")
        log("  可整个删除该目录腾出约 550MB：ocr_runtime/lib 不会再被使用。")

    # ④ 用不到的模型：导出会把整个 models/ 复制走，这些纯属白占
    dead = _unused_model_dirs()
    if dead:
        keep = "、".join(_OCR_MODELS_ONNX if ocr_backend() == "onnxruntime" else _OCR_MODELS_PADDLE)
        warn(f"models/ 里有 {len(dead)} 个用不到的模型目录（当前后端 {ocr_backend()}，只会用到 {keep}）：")
        base = os.path.join(OCR_MODELS_DIR, "official_models")
        for name, mb in dead:
            log(f"      {mb:7.1f} MB  {name}")
        log(f"  合计约 {sum(mb for _, mb in dead):.0f} MB，删掉不影响功能（导出会整个目录复制）。")
        log(f"  确认后可删除：{base} 下的上述目录。")

    log("")
    ok("OCR 运行时准备完成。")
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
    # OCR：库在 .venv（随模板打包），模型在 ocr_runtime/models（随导出携带）
    log("")
    log("OCR 运行时：")
    _backend = ocr_backend()
    log(f"  {'✔' if ocr_lib_ready() else '✘'} 库（.venv 里的 paddlepaddle/paddleocr，随模板打包）")
    log(f"  {'✔' if _backend == 'onnxruntime' else '·'} 推理后端：{_backend}"
        + ("（onnxruntime，比 paddle 原生快约 5 倍）" if _backend == "onnxruntime"
           else "（未装 onnxruntime，退回 paddle 原生后端）"))
    if ocr_models_ready():
        log(f"  ✔ 模型 models/（{_backend} 格式，det+rec）")
    else:
        log(f"  ✘ 模型 models/（缺 {_backend} 格式的 det/rec，跑 ocr-runtime 会补下）")
    if not ocr_lib_ready():
        log("      → 尚未准备：启动 start.cmd（菜单 1/2）时会自动装库并下载模型")
        log("        （也可手动执行：start.cmd ocr-runtime）")

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

    p = sub.add_parser(
        "ocr-runtime",
        help="准备 OCR：库装进 .venv + 模型预取（日常由启动器菜单 1/2 自动完成，此命令用于手动补齐）",
    )
    p.add_argument("--force", action="store_true", help="重装 OCR 库（默认已就绪则跳过）")
    p.add_argument("--models-only", action="store_true", help="只预取模型，不装库（要求库已就绪）")
    p.add_argument("--skip-models", action="store_true", help="只装库，不预取识别模型")
    p.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    p.set_defaults(func=cmd_ocr_runtime)

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
