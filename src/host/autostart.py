"""autostart.py —— 开机自启动（Windows 注册表 Run 项）

设计要点
--------
1. **状态以注册表为准**，不另存 settings.json：
   用户在「任务管理器 → 启动」里禁用/删除，菜单下次打开就能如实反映。
   （与 `skill_toggle` 的「状态以磁盘为准」同一思路。）

2. 写 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`：
   只影响当前用户，**不需要管理员权限**（写 HKLM 才需要）。
   用户拒绝 UAC 的场景下也不会失败。

3. 命令行用 `pythonw.exe` 而不是 `.bat`：
   走 .bat 会闪一下黑窗口，pythonw 是无控制台的。

4. 非 Windows 一律安全返回 False，绝不影响桌宠运行。
"""
import os
import sys
from pathlib import Path

# 注册表值名（显示在任务管理器「启动」里）
AUTOSTART_NAME = "DaFeiyuPet"

# 只写当前用户，不需要管理员
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _is_windows():
    return sys.platform == "win32"


def _app_root(root=None):
    """程序根目录（含 main.py 的那一级）。

    root 传的是 base_dir（frames/）时，上一级才是程序根 ——
    与 window.py 里 `Path(self.base_dir).parent` 的算法一致。
    """
    if root is None:
        # 从本文件位置反推：src/host/autostart.py → 上两级
        return Path(__file__).resolve().parent.parent.parent
    p = Path(root)
    if p.name == "frames":
        return p.parent
    return p


def _pythonw():
    """找无控制台的 pythonw.exe。

    优先取当前解释器同级的 pythonw.exe（保证同一个 Python 环境、
    同一个 site-packages，不会出现「自启动时找不到 PySide6」）。
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return exe
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return cand
    # 兜底：python.exe 也能跑，只是会短暂出现控制台
    return exe if exe.is_file() else None


def launch_command(root=None):
    """拼出自启动命令行。找不到解释器或 main.py 时返回 None。"""
    app_root = _app_root(root)
    main_py = app_root / "main.py"
    if not main_py.is_file():
        return None
    pyw = _pythonw()
    if pyw is None:
        return None
    # 路径可能含中文/空格 → 一律加引号
    return f'"{pyw}" -u "{main_py}"'


def is_enabled(root=None):
    """当前是否已启用开机自启动。

    判定标准：注册表里有这一项，**且**命令指向的 main.py 确实存在。
    （指向不存在路径的残留项视为未启用，避免菜单显示"开"但实际起不来。）
    """
    if not _is_windows():
        return False
    try:
        import winreg
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            try:
                val, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
            except FileNotFoundError:
                return False
    except OSError:
        return False
    if not isinstance(val, str) or not val.strip():
        return False
    # 命令里引号包住的 main.py 是否存在
    import re
    m = re.search(r'"([^"]*main\.py)"', val, re.IGNORECASE)
    if m and not Path(m.group(1)).is_file():
        return False
    return True


def set_enabled(on, root=None):
    """开启/关闭开机自启动。返回 True 表示操作成功（或已是目标状态）。"""
    if not _is_windows():
        return False
    try:
        import winreg
    except Exception:
        return False
    on = bool(on)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if on:
                cmd = launch_command(root)
                if cmd is None:
                    return False
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
                return True
            try:
                winreg.DeleteValue(k, AUTOSTART_NAME)
            except FileNotFoundError:
                pass            # 本来就没有 → 目标状态已达成
            return True
    except OSError:
        return False


def toggle(root=None):
    """切换。返回切换后的状态（失败时返回原状态）。"""
    before = is_enabled(root)
    target = not before
    ok = set_enabled(target, root)
    if not ok:
        return before
    # 回读确认（注册表写入偶发静默失败）
    return is_enabled(root)


def menu_label(root=None):
    """菜单文案：与「行为：群体」「摇晃：开」同一写法 —— 名称即状态。"""
    return "开机自启：%s" % ("开" if is_enabled(root) else "关")
