# 🧰 环境安装 FAQ

> 用 `start.cmd` 一键装依赖时报错？尤其是卡在 **PyAudio 编译失败**？
> 本篇按「症状 → 原因 → 修复」说明，照着做即可。

---

## 一、装依赖失败（提示「pip 退出码非 0」/「依赖未就绪，已中止启动」）

### 症状 1.1：安装依赖时 PyAudio 报错，最后提示依赖未就绪

典型报错（二选一）：

```
Building wheel for PyAudio (pyproject.toml) ... error
fatal error C1083: 无法打开包括文件: "io.h": No such file or directory
```

```
error: Microsoft Visual C++ 14.0 or greater is required.
```

> **原因（关键）**
> PyAudio 在 PyPI 上**只提供源码包、没有官方 Windows 预编译包（wheel）**，
> 所以无论用哪个 Python 版本，pip 都会尝试**在本地编译**它；而编译需要
> **Visual Studio 的 C++ 工具链 + Windows SDK**。新电脑上常常装了 VS 却没勾选 SDK，
> 编译就会卡在找不到 `io.h` 这类系统头文件。

> **为什么影响这么大**
> pip 的安装是「先把所有包都构建成功、再统一安装」。PyAudio 一个失败，就会导致
> **这一批依赖全部没装上**（PySide6、cv2、numpy 等也一起缺），编辑器因此启动不了。

**先记住一点**：PyAudio **不是编辑器运行必需的**，它只是「声音检测」节点的兜底方案
（首选是 `soundcard`，两者都没有时该节点只会提示并走 no，不影响编辑器启动）。

因此有两条路：**先让编辑器跑起来**（修复 A），或**补齐编译环境一劳永逸**（修复 B）。

---

### 修复 A：先跳过 PyAudio，让编辑器能启动（最快，无需管理员权限）

在项目根目录（含 `start.cmd` 的那层）打开命令行，先单独安装除 PyAudio 外的依赖：

```cmd
.venv\Scripts\python.exe -m pip install PySide6 opencv-python "numpy>=1.24" pillow pyautogui pydirectinput pynput pywin32 psutil "soundcard>=0.4.3"
```

> 注意：`"numpy>=1.24"` **必须带引号**，否则 cmd 会把 `>` 当成重定向。

装完后再运行 `start.cmd` 选 `1` 即可启动编辑器（关键依赖已齐，脚本不会再触发整批重装）。

---

### 修复 B：补齐编译环境，让 PyAudio 也能装上（一劳永逸，需管理员权限）

PyAudio 要编译成功，必须有 **Windows SDK**（提供 `io.h` 等头文件）。

**方式一：用 Visual Studio Installer 补装（推荐，可视化）**

1. 开始菜单打开 **Visual Studio Installer**。
2. 找到已安装的 VS（如 2022 Community）→ 点 **修改**。
3. 「工作负载」里勾选 **「使用 C++ 的桌面开发」**。
4. 切到 **「单个组件」** 标签，搜索并勾选 **Windows 10/11 SDK**（即提供 `io.h` 的组件）。
5. 右下角点 **修改**，等待下载安装完成（数 GB，耗时较长）。
6. **重开一个终端**，再执行：

```cmd
.venv\Scripts\python.exe -m pip install pyaudio
```

**方式二：命令行静默安装（可选，仍需管理员权限）**

```cmd
"C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe" modify ^
  --installPath "C:\Program Files\Microsoft Visual Studio\2022\Community" ^
  --add Microsoft.VisualStudio.Component.Windows11SDK.22621 ^
  --quiet --norestart
```

> 组件 ID 与 `--installPath` 随 VS 版本/安装位置而变，请按实际情况调整。
> 公司电脑若没有管理员权限或策略禁用 VS Installer，请走修复 A 或修复 C。

---

### 修复 C：用预编译 wheel 绕过编译（免装 SDK）

若不想装 SDK，可尝试现成的第三方预编译包：

```cmd
.venv\Scripts\python.exe -m pip install pipwin
.venv\Scripts\python.exe -m pipwin install pyaudio
```

> 说明：`pipwin` 走的是第三方预编译 wheel，可持续维护性一般；
> 且这类 wheel **覆盖的 Python 版本有限，越新的 Python（如 3.14）越可能没有对应版本**，失败属正常。
> 也可自行到第三方 PyAudio wheel 发布页下载匹配版本的 `.whl` 后用 `pip install 文件路径.whl` 安装。

---

### 修复 D：改用 Python 3.12 提高兼容性（可选）

Python 版本越新，第三方预编译 wheel 越不齐全。若你打算走修复 C，建议把虚拟环境换成 **Python 3.12**：

1. 安装 Python 3.12；
2. 删除项目下的 `.venv` 目录；
3. 重新运行 `start.cmd`，脚本会自动用 3.12 重建虚拟环境。

> 注意：换 3.12 **并不能**让 PyAudio 免编译（PyPI 上各版本都只有源码包），
> 只是能让「修复 C」的第三方预编译包更容易找到。要彻底编译成功仍需「修复 B」。

---

## 二、编辑器能启动，但「声音检测」节点提示采集库不可用

### 症状 2.1：SoundDetect 日志提示「音频采集库未安装或设备不可用，自动走 no」

**含义**：`soundcard` 和 `pyaudio` 都没装成，节点无法采集声音，只能走 `no` 分支。

**修复**：
1. 先确认首选库 `soundcard` 是否装上：

```cmd
.venv\Scripts\python.exe -c "import soundcard; print('soundcard OK')"
```

2. 若只缺 PyAudio，按前面 **修复 B / 修复 C** 安装即可；
3. 通知类节点需要的声音采集在编辑器内的提示也会给出同样的安装指引。

---

## 三、如何验证是否装好

**方式一：用启动器的环境体检**

运行 `start.cmd` 选 `5`（查看环境信息），逐项确认关键依赖是否为 `✔`。

**方式二：直接探测 PyAudio**

```cmd
.venv\Scripts\python.exe -c "import pyaudio; print('PyAudio OK', pyaudio.__version__)"
```

打印 `PyAudio OK` 即安装成功；报 `ModuleNotFoundError` 则说明还没装上。

---

## 四、快速决策表

| 你的情况 | 推荐做法 |
| --- | --- |
| 只想先跑起来，暂时不用声音检测 | **修复 A** |
| 需要完整功能，有管理员权限 | **修复 B** |
| 不想装 SDK，且 Python 版本较旧（≤3.12） | **修复 C** |
| 想让第三方 wheel 更容易找到 | **修复 D** + 修复 C |
| 公司电脑无管理员权限 | 修复 A（或联系 IT 装 SDK） |

---

> 还有其它安装/启动问题？先在本篇对应「症状」里找，或到「排错调试 FAQ」继续排查。