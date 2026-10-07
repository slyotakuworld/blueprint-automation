# -*- coding: utf-8 -*-
"""首次启动免责声明（编辑器侧，2026-10-07）。

背景：本工具能执行系统命令、操控键鼠、读取屏幕、发起网络请求，且全部以当前
Windows 用户权限运行；蓝图又是可导入、可分发的 JSON——「别人发来的蓝图里藏着
危险命令」是真实风险面。启动时先把免责声明亮出来、用户明确同意后才进主界面。

行为：
  - 只在**首次启动**（或声明文本升版 DISCLAIMER_VERSION 后）弹出一次；
  - 同意记录写进 config.json 的 disclaimer 键（保留其他键不动）；
  - 点「不同意」→ 程序直接退出，不进入主界面。

注意：本模块是编辑器侧代码，不在 RUNTIME_SOURCES（运行时模板）里，
改动它**不需要**重打运行时模板。
"""

import json
import os
from datetime import datetime

# 声明文本升版时 +1：老用户会重新看到一次弹窗
DISCLAIMER_VERSION = 1

_HTML = """
<h3>使用须知与免责声明</h3>
<p>在开始使用「赫萝蓝图自动化」之前，请仔细阅读以下条款：</p>
<p><b>1. 软件能力</b>：本软件可模拟键盘鼠标、截屏与图像识别、<b>以你的
Windows 用户权限执行系统命令</b>、发起网络请求等。它没有沙箱，蓝图里的
每一条命令都会被系统真实执行。</p>
<p><b>2. 蓝图内容的责任</b>：你对所创建、导入、运行的全部蓝图内容负责。
<b>导入他人分享的蓝图前，请务必逐个审查其中的「执行命令」「网络请求」节点</b>，
谨防夹带删除文件、格式化磁盘、篡改系统等破坏性指令。</p>
<p><b>3. 风险自担</b>：因使用本软件（包括运行含危险命令的蓝图）造成的任何
直接或间接损失——数据丢失、系统故障、账号处罚、财产损失等——由使用者自行承担。</p>
<p><b>4. 按现状提供</b>：本软件按「现状」提供，作者不对软件缺陷可能造成的
损失承担责任（法律另有强制性规定的除外）。</p>
<p><b>5. 合法使用</b>：仅供个人合法的桌面自动化用途。严禁用于破坏计算机
信息系统、干扰他人设备、作弊外挂、侵犯他人合法权益等违法用途，由此产生的
一切法律责任由行为人自行承担。</p>
<p>点击「同意并继续」即表示你已阅读并接受以上全部条款。</p>
"""


def _config_path() -> str:
    from src.config.app_paths import USER_CONFIG_FILE
    return USER_CONFIG_FILE


def _load_config() -> dict:
    path = _config_path()
    if not path or not os.path.isfile(path):
        return {}
    raw = None
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                raw = json.load(f)
            break
        except Exception:
            continue
    return raw if isinstance(raw, dict) else {}


def _save_config(data: dict) -> None:
    path = _config_path()
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def is_accepted() -> bool:
    """config.json 里是否已同意当前版本的声明。"""
    d = _load_config().get("disclaimer")
    return (
        isinstance(d, dict)
        and d.get("accepted") is True
        and int(d.get("version", -1) or -1) >= DISCLAIMER_VERSION
    )


def mark_accepted() -> None:
    """把「已同意」写进 config.json（保留其他键）。"""
    data = _load_config()
    data["disclaimer"] = {
        "version": DISCLAIMER_VERSION,
        "accepted": True,
        "accepted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_config(data)


def show_if_needed() -> bool:
    """需要时弹出免责声明。

    返回 True = 已同意（或此前已同意过），可以继续启动；
    返回 False = 用户拒绝，调用方应直接退出。
    """
    if is_accepted():
        return True

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox

    dlg = QDialog()
    dlg.setWindowTitle("使用须知与免责声明")
    dlg.setModal(True)
    dlg.setMinimumWidth(520)
    lay = QVBoxLayout(dlg)
    label = QLabel(_HTML)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    lay.addWidget(label)
    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.button(QDialogButtonBox.Ok).setText("同意并继续")
    btns.button(QDialogButtonBox.Cancel).setText("不同意（退出）")
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    lay.addWidget(btns)

    if dlg.exec() == QDialog.Accepted:
        try:
            mark_accepted()
        except Exception:
            pass
        return True

    from PySide6.QtWidgets import QMessageBox
    QMessageBox.information(
        None, "未同意免责声明",
        "你未同意使用须知与免责声明，程序即将退出。",
    )
    return False
