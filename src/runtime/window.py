"""src/runtime/window.py - 桌宠渲染窗口（完美拖拽与悬停版）"""
import os
import time
import math
import random
import ctypes
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QPoint, QElapsedTimer, QRect, QRectF
from PySide6.QtGui import QPixmap, QGuiApplication, QCursor
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QMenu

from src.runtime.loader import LoadThread
from src.runtime import frame_cache
from src.runtime.bubble import BubbleWindow
from src.shared.pricing import deepseek_pricing_tier, balance_text, balance_html
from src.shared.pickers import pick_big_pool_equal, pick_category_action
from src.shared.physics import (update_physics, landing_squash, squash_scale,
                                SQ_DURATION_MS, SQ_SQUASH)
from src.shared.flock import (steer_target, speed_up_for, roam_chance_for_overlap,
                              overlap_ratio, resolve_push, separate_overlaps)
from src.shared.tilt import (TiltState, apply_tilt, TILT_ORIGIN_Y_FALLBACK,
                             TILT_MAX_DEG, TILT_MAX_PX)
from src.shared.jelly import JellyState

# ★ 变换帧（倾斜/Q 弹/镜像）绘制时用的「源图超采样倍数」。
#   原图 640×360 一次性缩到 288×162（2.2 倍）时，Qt 的双线性只采样 2×2 邻域，
#   大量源像素被跳过 → 混叠 → 边缘发糊、发脏。这正是"鼠标一靠近就明显变糊"的成因：
#   角度还没起来，绘制路径已经从「1:1 贴缓存图」切到「重采样」了。
#   改成先用面积平均缩到 1.5× 显示尺寸，再交给 drawImage 做小倍率缩放+旋转。
#   ⚠ 中间图**绝不能超过原图尺寸**（见 paintEvent 里的 min(...) 钳制）：
#     scale=1.0 时显示尺寸就等于原图，1.5× 会变成"放大再缩回"，凭空多两次插值。
TILT_SRC_SUPERSAMPLE = float(os.environ.get("DSH_PET_TILT_SUPERSAMPLE", "1.5"))

# ★ 预渲染用的重采样器。PIL 的 BICUBIC 支撑域是 4×4（Qt 的 drawImage 只有 2×2
#   双线性），旋转后高频保留 96.7%，基本和静止持平；代价是 640×360 一次 15ms。
#   只在「角度档变化」时算一次，之后命中缓存几乎零成本。
#   PIL 是可选的：没装就自动退回 Qt 双线性（画质 ≈ 71%），不影响运行。
try:
    from PIL import Image as _PIL_IMAGE
except Exception:  # pragma: no cover - 没装 Pillow 时走降级路径
    _PIL_IMAGE = None

# ★ 倾斜帧「预渲染 + 1:1 贴图」缓存的角量档数（每侧）。
#   为什么必须这么做：静止时是「像素完美 1:1 贴图」（零重采样），而只要一旋转，
#   Qt 就必须重采样，画质**无论如何**都比静止时软一点 —— 这就是"鼠标一靠近就变糊"
#   的根源。把旋转结果预先算好、缓存成 pw×ph 的图，之后每帧只做 1:1 整数贴图，
#   倾斜时的画质就和静止时完全一致了。
#   代价是角度被量化（3.5°/24 ≈ 0.15°，边缘位移约 0.75px），倾斜会有一点点阶跃感。
TILT_CACHE_STEPS = int(os.environ.get("DSH_PET_TILT_STEPS", "24"))
# 生成缓存图时用的超采样倍数（只付一次，值可以给大）
# 生成缓存图时的超采样倍数。
# ★ 关键不是这个数字，而是**顺序**：先在 tw×th 上「只旋转、不缩放」，再用
#   QImage.scaled(面积平均) 收到 pw×ph。只要让 drawImage 直接把大图缩到小图，
#   Qt 就退回 2×2 双线性、**跳过源像素** → 混叠 →"开了摇晃整张变糊"。
#   原理和 GPU 的 mipmap 一样：先把多余像素用面积平均吃掉，再做几何变换。
TILT_CACHE_SUPERSAMPLE = float(os.environ.get("DSH_PET_TILT_CACHE_SS", "2.0"))
# 置 0 可关掉预渲染快路径，退回「现场旋转」的旧行为（排查用）
# ★ 默认关闭（2026-10-30）：预渲染缓存在静态单帧上清晰，但真机连续动画体感更糊
#   （角度量化跳变 + 双重滤波变软），用户实测否定。代码保留，置 1 可再试。
TILT_CACHE_ENABLED = os.environ.get("DSH_PET_TILT_CACHE", "1") not in ("0", "false", "no")

ALL_PETS = []
# 默认模式（新窗口的初值）；实际判定一律看每个实例自己的 self.flock_enabled
FLOCK_ENABLED = False

# ---- 摇晃总开关（菜单「设置 ▸ 摇晃」控制，全局生效）----
# 关掉后 tilt_enabled() 一律 False，且 _tick_tilt 立即归零 → 零开销。
_SWAY_ENABLED = [True]

# ---- 漫游总开关（菜单「设置 ▸ 漫游」控制，用户 2026-10-30 提出）----
# 关掉后：move 类动作**照播动画但不位移**（原地跑），朝向仍随机。
# 群体模式的 roam 位移（cat_type == 'roam'）**不受此开关影响** —— 用户原话：
#   "不生效，但是漫游关闭则不能切换模式，并且气泡提示漫游没有打开"
# 所以它同时也是一个"模式闸门"：漫游关 → 不能切到群体；关的瞬间若正在群体，
#   自动切回单体（见 set_roam）。
_ROAM_ENABLED = [True]


def roam_enabled():
    """漫游是否开启（全局）。移动动作只有它为 True 才会真的位移。"""
    return bool(_ROAM_ENABLED[0])


def set_roam(on):
    """设置漫游开关（全局）。关闭时把群体模式的主体自动拉回单体。"""
    _ROAM_ENABLED[0] = bool(on)
    if not _ROAM_ENABLED[0]:
        # 群体模式离开 roam 就只剩五只原地站着 —— 用户要求"自动切回单体"。
        for p in list(ALL_PETS):
            if getattr(p, 'is_main', False) and bool(getattr(p, 'flock_enabled', False)):
                try:
                    p.toggle_flock(False)
                except Exception:
                    pass
                break


def tilt_sway_enabled():
    """摇晃功能是否开启（全局）。"""
    return bool(_SWAY_ENABLED[0])


def set_tilt_sway(on):
    """设置摇晃开关（全局）。关闭时立即把所有宠物归零。"""
    _SWAY_ENABLED[0] = bool(on)
    if not _SWAY_ENABLED[0]:
        for p in list(ALL_PETS):
            try:
                if getattr(p, 'tilt', None) is not None:
                    p.tilt.reset()
                p.update()
            except Exception:
                pass


# ---- 窗口置顶总开关（菜单「置顶：开/关」控制，全局生效）----
# 默认 True = 保持改造前的行为。关掉后桌宠会被其他窗口遮住。
_TOPMOST_ENABLED = [True]


def topmost_enabled():
    """窗口置顶是否开启（全局）。"""
    return bool(_TOPMOST_ENABLED[0])


def _apply_topmost_to(widget, on):
    """把置顶标志应用到一个顶层窗口上（注意：改 flag 会隐藏原生窗口，必须重新 show）。

    Qt 的坑：setWindowFlag() 之后窗口会变成隐藏状态，且原生 HWND 可能被重建，
    位置/尺寸有概率被系统重置 —— 所以这里先记住几何位置，改完 flag 再还原。
    """
    if widget is None:
        return
    try:
        geo = widget.geometry()
        was_visible = widget.isVisible()
        widget.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if was_visible:
            widget.show()
            widget.setGeometry(geo)
            if on:
                widget.raise_()
    except Exception:
        pass

    # Win32 层面同步一次：Qt 的 flag 有时不彻底（尤其被其他 TOPMOST 窗口压着时）
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        hwnd = int(widget.winId())
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST),
            0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def set_topmost(on):
    """设置窗口置顶（全局）。连同气泡与所有个体一起切换。"""
    _TOPMOST_ENABLED[0] = bool(on)
    for p in list(ALL_PETS):
        try:
            _apply_topmost_to(p, on)
            bub = getattr(p, 'bubble', None)
            if bub is not None:
                _apply_topmost_to(bub, on)
        except Exception:
            pass

# ---- 同动作解码去重（性能）----
# key = (动作名, 宽, 高) -> [(pet, 动作名, cat_type, msg), ...]
# 30 只同时切到同一动作时只解码一次，其余等结果；否则 30 个线程各解 241 帧，
# 实测启动瞬间 RSS 6.7GB / CPU 740%。
_INFLIGHT = {}

# 召唤序号：0 = 主体，>0 = 多开。**单调递增、永不复用**。
# 原来用 len(ALL_PETS) 赋值，关掉几只多开后计数回落，新建的多开会拿到 0
# （被误判成主体），"关闭全部多开时跳过主体"就失效了。
_SPAWN_SEQ = [0]

# 待回收的 QThread：关闭多开时 requestInterruption+quit 后丢进来，
# 由全局定时器在它们 isFinished() 之后移除引用。
# 直接丢引用会让 QThread 在运行中被 GC → "Destroyed while thread is still running"。
_PENDING_THREADS = []


def _atexit_drain():
    """解释器退出前的最后一道保险。

    实测崩溃现场：atexit 里 drain 等 3 秒仍不够（线程卡在 QImage 解码大 webp），
    随后解释器销毁 QApplication → 线程还在 new QImage → 访问违例 0xC0000005。

    所以 drain 之后**直接 os._exit(0)**：跳过解释器对 Qt 对象的销毁，
    线程与句柄交给操作系统回收。日志是行缓冲，已经落盘，不会丢。
    """
    try:
        _drain_threads(2000)
    except Exception:
        pass
    try:
        import os as _os
        _os._exit(0)
    except Exception:
        pass


try:
    import atexit as _atexit
    _atexit.register(_atexit_drain)
except Exception:
    pass


_DSH_EXE_NAME = "DeepSeek Harness.exe"


def _iter_dsh_pids():
    """枚举所有 DeepSeek Harness 进程（主进程 + Electron 辅助进程）。

    ★ 三路互补（2026-10-29）：
      ① **窗口反查**：枚举顶层窗口 → `OpenProcess` + `QueryFullProcessImageNameW`
         比对 exe 名。最可靠（不依赖进程枚举权限，实测受限环境下 Toolhelp
         快照会漏掉主进程，而这条能拿到）；
      ② **进程快照**：`CreateToolhelp32Snapshot`，纯 Win32、无子进程、约 1~3ms；
      ③ `tasklist` 兜底：①②都空时退回原实现。

    为什么不用 `tasklist` 当主路径：它是**同步子进程**，实测 60~120ms，
    双击要实时调用，会让点击有可感知的卡顿；且受限环境下会直接 WinError 5。
    """
    pids = []
    seen = set()
    for src in (_iter_dsh_pids_via_windows, _iter_dsh_pids_fast):
        try:
            for p in src():
                if p and p not in seen:
                    seen.add(p)
                    pids.append(p)
        except Exception:
            pass
    if pids:
        return pids

    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % _DSH_EXE_NAME, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


_PID_EXE_CACHE = {}


def _pid_exe_name(pid):
    """取进程 exe 文件名（如 'DeepSeek Harness.exe'）。失败返回 ''。"""
    if pid in _PID_EXE_CACHE:
        return _PID_EXE_CACHE[pid]
    import ctypes
    from ctypes import wintypes

    name = ""
    kernel32 = ctypes.windll.kernel32
    h = None
    try:
        h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf,
                                                   ctypes.byref(size)):
                path = buf.value or ""
                name = path.rsplit("\\", 1)[-1]
    except Exception:
        name = ""
    finally:
        try:
            if h:
                kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:
            pass
    # 只缓存"查到了"的结果：查不到可能是权限暂时不足，下次再试
    if name:
        _PID_EXE_CACHE[pid] = name
    return name


def _iter_dsh_pids_via_windows():
    """枚举顶层窗口 → 反查所属进程 exe 名 → 收集 DSH 的 PID。

    比进程枚举更可靠：只要窗口能被枚举到，就能知道它属于哪个进程，
    不受 `CreateToolhelp32Snapshot` 在某些受限环境下漏进程的影响。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []
    seen = set()
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            p = int(pid.value)
            if not p or p in seen:
                return True
            if _pid_exe_name(p).lower() == _DSH_EXE_NAME.lower():
                seen.add(p)
                found.append(p)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        pass
    return found


def _iter_dsh_pids_fast():
    """用 Toolhelp 快照枚举 DSH 进程 PID（无子进程，毫秒级）。失败返回 []。"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAX_PATH_LONG = 32768

    kernel32 = ctypes.windll.kernel32

    class PROCESSENTRY32W(ctypes.Structure):
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
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []

    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            name = entry.szExeFile or ""
            if name.lower() == _DSH_EXE_NAME.lower():
                found.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    except Exception:
        return []
    finally:
        try:
            kernel32.CloseHandle(snap)
        except Exception:
            pass

    # 主进程之外还有同名辅助进程；若只有 0 号/无效 PID 则视为没找到
    return [p for p in found if p > 0]


def is_dsh_running():
    """DSH 是否正在运行。"""
    return len(_iter_dsh_pids()) > 0


# Electron 主窗口的窗口类名。实测（2026-10-29）：
#   · `Chrome_WidgetWin_1` = 真正的主窗口（有标题、可最小化）
#   · `Chrome_WidgetWin_0` = 离屏/辅助窗口（不可见，不能动它）
#   · `IME`                = 输入法窗口（标题 `Default IME`，**绝不能当成主窗口**）
_DSH_WINDOW_CLASS = "Chrome_WidgetWin_1"
_DSH_WINDOW_CLASS_PREFIX = "Chrome_WidgetWin_"
_DSH_BAD_TITLES = ("Default IME", "MSCTFIME UI", "")


def _is_dsh_main_window(hwnd, title):
    """判断一个顶层窗口是不是 DSH 的**主窗口**。

    只靠"属于 DSH 进程 + 有标题"会把输入法窗口（`Default IME`）也选进来 ——
    实测踩到过：双击后 `bring_dsh_to_front()` 返回了 `'Default IME'`，
    前台被一个不可见的输入法窗口占走，DSH 依然看不见。

    三重过滤：
      ① 标题不是 IME/空；
      ② 窗口类名是 `Chrome_WidgetWin_*`，且**不是** `Chrome_WidgetWin_0`（离屏窗口）；
      ③ 无 owner（`GW_OWNER == 0`）—— 工具窗口/浮层都带 owner。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        if not title or title in _DSH_BAD_TITLES:
            return False
        buf = ctypes.create_unicode_buffer(256)
        n = user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        cls = (buf.value if n else "") or ""
        if cls == "IME" or cls.startswith("MSCTFIME"):
            return False
        if not cls.startswith(_DSH_WINDOW_CLASS_PREFIX):
            return False
        if cls == "Chrome_WidgetWin_0":
            return False              # 离屏窗口
        if int(user32.GetWindow(wintypes.HWND(hwnd), 4) or 0) != 0:   # GW_OWNER
            return False
        return True
    except Exception:
        return False


def _enum_dsh_main_windows():
    """枚举 DSH 的**主窗口**（可见、有标题、类名过滤后的顶层窗口）。

    返回 [(hwnd, title, is_minimized), ...]。
    Electron 会创建多个窗口（helper、offscreen、IME 等），全部过滤掉，
    只留下真正能"给用户看"的那个主窗口。
    """
    import ctypes
    from ctypes import wintypes

    pids = set(_iter_dsh_pids())
    if not pids:
        return []

    user32 = ctypes.windll.user32
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            # ★ 过滤输入法窗口 / 离屏窗口 / 带 owner 的浮层
            if not _is_dsh_main_window(hwnd, title):
                return True
            found.append((int(hwnd), title, bool(user32.IsIconic(hwnd))))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        pass
    return found


def _enum_dsh_hidden_windows():
    """枚举 DSH 的**隐藏**顶层窗口（有标题但不可见）。

    用户把 DSH 最小化到托盘时（Electron 调 hide()），窗口 `IsWindowVisible` 为 False，
    只靠 `_enum_dsh_main_windows()` 会一个都找不到 → 双击像是没反应。
    这里补一条回退路径：有标题、但当前不可见的 DSH 顶层窗口。
    返回 [(hwnd, title, is_minimized), ...]（与可见版同结构）。
    """
    import ctypes
    from ctypes import wintypes

    pids = set(_iter_dsh_pids())
    if not pids:
        return []

    user32 = ctypes.windll.user32
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            if user32.IsWindowVisible(hwnd):
                return True          # 可见的归 _enum_dsh_main_windows 管
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True          # 无标题的一律不碰（Electron 的 helper 窗口）
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if not _is_dsh_main_window(hwnd, title):
                return True          # 同样过滤 IME / 离屏 / 带 owner 的浮层
            found.append((int(hwnd), title, bool(user32.IsIconic(hwnd))))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        pass
    return found


def bring_dsh_to_front():
    """把 DSH 主窗口**提到最前**，让用户能直接看到它（必要时先还原最小化）。

    ★ 语义边界（2026-10-29 用户明确）：
      · **只做一次性前台提升**，绝不给 DSH 加 `HWND_TOPMOST` ——
        提上来之后点别的窗口，它会被正常盖住；
      · **桌宠自己仍然是 topmost**（`Qt.WindowStaysOnTopHint`），
        所以提上来之后大肥鱼依然浮在 DSH 之上 ——"大肥鱼本身才应该是最上层"。
      · **只在 DSH 已运行时动作**：进程没起就不做任何事（绝不顺手拉起新进程）。
      · **只由用户双击触发**：全项目唯一的调用点是 mouseReleaseEvent 的双击分支，
        没有任何定时器/自动路径会调它（2026-10-30 已逐一核实）。

    ★ 注意（2026-10-30）：跑测试时 DSH 会被拽到前台，那是
      `test_double_click.py` 模拟双击触发了本函数 —— 属于"改桌宠时的副作用"，
      正常运行中的桌宠**不会**自动跳。如需跑测试时不打扰，
      在该测试里 mock 掉本函数即可（test_restore_dsh.py 已有此写法）。

    三种情况都能处理：
      1. 已最小化 → `ShowWindow(SW_RESTORE)` + 提前台；

      2. 正常显示但被别的窗口遮住 → 直接提前台；
      3. 最小化到托盘（窗口被 hide，枚举不到可见窗口）→ 对隐藏主窗口 `SW_SHOW` 再提前台。

    返回 `(是否做了动作, 窗口标题)`。已在最前时不动作（返回 False，幂等）。
    **任何异常都吞掉**，绝不影响双击的其它功能（查余额）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    # ---------- 1) 找目标窗口 ----------
    try:
        wins = _enum_dsh_main_windows()
    except Exception:
        wins = []
    was_hidden = False
    if not wins:
        try:
            wins = _enum_dsh_hidden_windows()
        except Exception:
            wins = []
        was_hidden = bool(wins)
    if not wins:
        return False, ""          # DSH 没运行 / 没有主窗口 → 什么都不做

    try:
        fg = int(user32.GetForegroundWindow() or 0)
    except Exception:
        fg = 0

    # 已经在最前、且不是隐藏状态 → 不打扰
    if not was_hidden:
        for hwnd, title, mini in wins:
            if hwnd == fg and not mini:
                return False, title

    # 目标：优先最小化的那只，其次列表第一个
    target = None
    for w in wins:
        if w[2]:
            target = w
            break
    if target is None:
        target = wins[0]
    hwnd, title, _mini = target

    # ---------- 2) 还原 / 显示 ----------
    # ★ 为什么不能只靠 ShowWindow：
    #   实测（2026-10-29）跨进程调 ShowWindow 会被系统拒绝并返回 0 / GetLastError=5，
    #   窗口一动不动；而 PostMessage(WM_SYSCOMMAND, SC_RESTORE) 能正常还原。
    #   所以这里"先 ShowWindow，再用 IsIconic/IsWindowVisible 复核，没成就走消息兜底"。
    SW_RESTORE = 9
    SW_SHOW = 5
    WM_SYSCOMMAND = 0x0112
    SC_RESTORE = 0xF120
    _cmd = SW_SHOW if was_hidden else SW_RESTORE

    for fn in ("ShowWindow", "ShowWindowAsync"):
        try:
            getattr(user32, fn)(wintypes.HWND(hwnd), _cmd)
        except Exception:
            pass

    def _still_hidden_or_minimized():
        try:
            if was_hidden and not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                return True
            return bool(user32.IsIconic(wintypes.HWND(hwnd)))
        except Exception:
            return False

    if _still_hidden_or_minimized():
        # 兜底①：给窗口所在线程发还原命令（跨进程也有效）
        try:
            user32.PostMessageW(wintypes.HWND(hwnd), WM_SYSCOMMAND, SC_RESTORE, 0)
        except Exception:
            pass
        # 兜底②：同步版本（PostMessage 排队后可能还没处理）
        try:
            if _still_hidden_or_minimized():
                _res = wintypes.DWORD()
                user32.SendMessageTimeoutW(
                    wintypes.HWND(hwnd), WM_SYSCOMMAND, SC_RESTORE, 0,
                    0x0002, 2000, ctypes.byref(_res))   # SMTO_ABORTIFHUNG
        except Exception:
            pass
        # 兜底③：最后再试一次 SW_SHOWNORMAL
        try:
            if _still_hidden_or_minimized():
                user32.ShowWindow(wintypes.HWND(hwnd), 1)   # SW_SHOWNORMAL
        except Exception:
            pass

    # ---------- 3) 提前台 ----------
    # Windows 有"前台锁"：非前台进程调 SetForegroundWindow 会被静默拒绝
    # （返回 0，窗口不会到前面）。三种绕法依次尝试：
    #   ① 正常调用（用户刚双击过桌宠，桌宠就是前台进程 → 这一步就成功）
    #   ② AttachThreadInput 挂到当前前台线程的输入队列
    #   ③ 模拟按一下 Alt —— 系统认为"用户在操作"，解除前台锁（最经典的兜底）
    try:
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    except Exception:
        pass

    try:
        fg_now = int(user32.GetForegroundWindow() or 0)
    except Exception:
        fg_now = 0

    if fg_now != hwnd:
        # ② AttachThreadInput（跨进程常被拒绝，失败就往下走）
        try:
            kernel32 = ctypes.windll.kernel32
            cur_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg_now), None) if fg_now else 0
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                user32.BringWindowToTop(wintypes.HWND(hwnd))
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
                user32.SetActiveWindow(wintypes.HWND(hwnd))
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            pass

    try:
        fg_now = int(user32.GetForegroundWindow() or 0)
    except Exception:
        fg_now = 0

    if fg_now != hwnd:
        # ③ 模拟 Alt 键（VK_MENU=0x12）解除前台锁
        try:
            KEYEVENTF_KEYUP = 0x0002
            VK_MENU = 0x12
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
        except Exception:
            pass

    # ---------- 4) 确认；没提到就闪任务栏（不抢输入焦点） ----------
    ok = False
    try:
        ok = (int(user32.GetForegroundWindow() or 0) == hwnd)
    except Exception:
        ok = False
    if not ok:
        try:
            user32.FlashWindow(wintypes.HWND(hwnd), True)
        except Exception:
            pass

    return True, title


def kill_dsh():
    """彻底结束所有 DSH 进程，返回结束的个数。"""
    import subprocess
    pids = _iter_dsh_pids()
    if not pids:
        return 0
    try:
        subprocess.run(
            ["taskkill", "/F", "/T"] + sum([["/PID", str(p)] for p in pids], []),
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
    return len(pids)


def dsh_log(msg):
    """记录「打开/关闭 DSH」全过程到 logs/dsh_launch.log。

    为什么单独一个文件：pet.log 记的是 HTTP 事件，翻起来看不到菜单操作；
    排查"点了没反应"必须有一份按时间排列的启动轨迹。
    """
    import os
    import time as _time
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(root, "logs")
        os.makedirs(d, exist_ok=True)
        line = "[%s] %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(os.path.join(d, "dsh_launch.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _alive(obj):
    """对象是否仍然存活（底层 C++ 对象未被销毁）。

    deleteLater() 之后 Python 包装对象还在，但底层 QWidget 已经销毁；
    此时再调 isVisible()/winId()/update() 会直接崩溃（偶发，很难复现）。
    """
    try:
        from shiboken6 import isValid
        return isValid(obj)
    except Exception:
        return True


_PET_TIMER_ATTRS = ('phys_timer', '_squash_timer', 'state_timer',
                    'flock_timer', '_whisper_timer')


def _withdraw_inflight(pet):
    """把窗口从所有"正在解码"的等待队列里摘掉。

    为什么必须摘（2026-10-30 多开崩溃）：
      `_INFLIGHT[key]` 里存的是 `(self, ...)`，即**强引用着窗口对象**。
      窗口 deleteLater() 之后 C++ 已经销毁，但 Python 对象被这个引用拖着不死；
      等解码线程 emit 时 `_first_batch` / `_finish_frames` 还会遍历到它、
      调 `on_first_batch` → 内部 `self.update()` → 访问已销毁的 QWidget → 0xC0000005。
    """
    for key, lst in list(_INFLIGHT.items()):
        try:
            lst[:] = [e for e in lst if e[0] is not pet]
            if not lst:
                _INFLIGHT.pop(key, None)
        except Exception:
            pass


def _dispose_pet_qt(pet):
    """在主线程里**主动**拆掉窗口持有的 QTimer（关闭路径必须调用）。

    为什么必须主动拆 —— 这是"多开偶尔崩溃"的根因：
      · 这些 QTimer 都 connect 到 `self.xxx` 的 bound method，timer 持有 self、
        self 持有 timer → **循环引用**，引用计数永远不归零，只能等 GC 收
      · 窗口 deleteLater() 后被 GC 时，GC 可能在**工作线程**里跑
        （崩溃日志里那个 `Dummy-78` 线程）
      · 在非主线程析构 QObject → Qt 打
        `QObject::~QObject: Timers cannot be stopped from another thread`
        → 紧跟 0xC0000005 访问违例，进程直接死
    所以关窗时就把 timer stop + disconnect + 立刻析构，全程在主线程完成；
    之后 GC 再碰到这些 Python 包装时，C++ 活对象已经不在了。
    """
    targets = []
    for attr in _PET_TIMER_ATTRS:
        targets.append((pet, attr))
    bub = getattr(pet, 'bubble', None)
    if bub is not None:
        targets.append((bub, 'timer'))

    # ★ 打上"Qt 资源已拆"的标记：关窗后仍可能有一次性的
    #   QTimer.singleShot(...) 回调打回来（召唤缓冲、DSH 轮询等），
    #   那些入口一律早退，否则会摸到已经被置 None 的 timer 抛 AttributeError。
    try:
        pet._qt_disposed = True
    except Exception:
        pass

    for owner, attr in targets:
        tm = getattr(owner, attr, None)
        if tm is None:
            continue
        try:
            tm.stop()
        except Exception:
            pass
        try:
            tm.timeout.disconnect()
        except Exception:
            pass
        try:
            tm.setParent(None)
        except Exception:
            pass
        try:
            from shiboken6 import delete as _sb_delete
            _sb_delete(tm)          # 主线程当场析构 C++ 对象
        except Exception:
            pass
        try:
            setattr(owner, attr, None)   # 断开 owner → timer 的引用
        except Exception:
            pass


def _drain_threads(max_ms=5000):
    """退出前等所有解码线程**真正结束**。

    ★ 这一步不能省、也不能用短超时：
      解释器退出时若还有 LoadThread 在 run() 里 new QImage，
      而 QGuiApplication 已经销毁 → 直接访问违例（0xC0000005），
      也就是用户看到的"突然崩溃"。
    """
    import time as _t

    # 1) 收集：待回收表 + 每只窗口自己的 loaders
    threads = []
    for th in list(_PENDING_THREADS):
        threads.append(th)
    for pet in list(ALL_PETS):
        for th in list(getattr(pet, 'loaders', []) or []):
            if th not in threads:
                threads.append(th)

    # 2) 先统一请求中断
    for th in threads:
        try:
            if th.isRunning():
                th.requestInterruption()
                th.quit()
        except Exception:
            pass

    # 3) 再逐个等到真正结束（总预算 max_ms）
    deadline = _t.monotonic() + max_ms / 1000.0
    for th in threads:
        try:
            if th.isRunning():
                left = int(max(0.0, (deadline - _t.monotonic())) * 1000)
                if left <= 0:
                    break
                if not th.wait(left):
                    # 还没结束：再补一次，避免解释器退出时它仍在跑
                    th.wait(max(50, left // 2))
        except Exception:
            pass

    _PENDING_THREADS.clear()
    for pet in list(ALL_PETS):
        try:
            pet.loaders = []
        except Exception:
            pass


def _stop_global_tick():
    try:
        if _GLOBAL_TICK[0] is not None:
            _GLOBAL_TICK[0].stop()
    except Exception:
        pass


def _reap_threads():
    for th in list(_PENDING_THREADS):
        try:
            if th.isFinished():
                _PENDING_THREADS.remove(th)
        except Exception:
            try:
                _PENDING_THREADS.remove(th)
            except Exception:
                pass

# ---- 全局帧定时器（单一）----
# 为什么不用"每只一个 QTimer"：单进程多窗口时，N 只就有 N 个 14ms 定时器，
# 全部挤在同一个 Qt 事件队列互相抢，tick 间隔抖动 → 画面抽搐。
# 一个全局定时器遍历所有宠物，回调次数从 N×71/秒 降到 71/秒，调度稳定。
_GLOBAL_TICK = [None]


def _start_global_tick():
    if _GLOBAL_TICK[0] is not None:
        return
    try:
        tm = QTimer()
        tm.setTimerType(Qt.PreciseTimer)
        tm.setInterval(14)   # 41.7ms / 3，与大肥鱼的 sub-frame poll 同口径
        tm.timeout.connect(lambda: _tick_all())
        tm.start()
        _GLOBAL_TICK[0] = tm
    except Exception:
        _GLOBAL_TICK[0] = None


_REAP_COUNTER = [0]


def _tick_all():
    # 每 ~30 次 tick（约 0.4 秒）回收一次线程引用
    _REAP_COUNTER[0] += 1
    if _REAP_COUNTER[0] % 30 == 0:
        _reap_threads()
    for pet in list(ALL_PETS):
        if not _alive(pet):
            try:
                ALL_PETS.discard(pet)
            except Exception:
                pass
            continue
        try:
            pet.tick()
        except RuntimeError:
            pass          # 窗口已销毁
        except Exception:
            pass
    # ★ 单体模式硬碰撞的兜底：每只各自结算时看到的是**上一帧**的邻居位置，
    #   两只相向而行会留下残余重叠。整帧落定后统一推一次。
    try:
        _settle_single_collisions()
    except Exception:
        pass


def _settle_single_collisions():
    """单体模式的残余重叠兜底分离（群体模式完全不参与）。"""
    singles = []
    for pet in list(ALL_PETS):
        if not _alive(pet):
            continue
        try:
            if pet.collide_participates():
                singles.append(pet)
        except Exception:
            pass
    if len(singles) < 2:
        return
    entries = []
    for pet in singles:
        try:
            cx, cy, hw, hh = pet.body_aabb()
            entries.append((pet, cx, cy, hw, hh))
        except Exception:
            pass
    if len(entries) < 2:
        return

    def _clamp(key, cx, cy):
        return key.clamp_body_center(cx, cy)

    try:
        fixed = separate_overlaps(entries, _clamp, iterations=2)
    except Exception:
        return
    for pet, cx, cy in fixed:
        try:
            pet.apply_body_center(cx, cy)
        except Exception:
            pass


def _first_batch(key, name, images, total):
    """首批帧就绪：立即让等待者开播（不必等整段解码完）。

    带 generation 校验：这只宠物在等待期间若又切了别的动作，本次结果已过期，丢弃。
    否则迟到的旧动作会把正在播的新动作顶掉 —— 表现为"播个开头就被切"。
    """
    waiters = _INFLIGHT.get(key)
    if not waiters:
        return
    for pet, anim, cat, msg, gen in waiters:
        # ★ 窗口可能已经关了（deleteLater 后 C++ 已销毁）：
        #   此时回调进去会 self.update() → 0xC0000005，必须跳过。
        if not _alive(pet):
            continue
        if getattr(pet, '_load_gen', 0) != gen:
            continue
        try:
            pet.on_first_batch(name, images, total, cat)
        except Exception:
            pass


def _finish_frames(key, name, qimages):
    waiters = _INFLIGHT.pop(key, [])
    for pet, anim, cat, msg, gen in waiters:
        if not _alive(pet):
            continue
        if getattr(pet, '_load_gen', 0) != gen:
            continue
        try:
            pet.on_loaded(name, qimages, cat, msg)
        except Exception:
            try:
                pet.load_next_action()
            except Exception:
                pass
_SCR_CACHE = [None]
_GLOBAL_CURSORS = {}

_GLOBAL_HIT = [None]

def _start_global_hit():
    if _GLOBAL_HIT[0] is not None:
        return
    tm = QTimer()
    tm.setTimerType(Qt.CoarseTimer)
    def _hit_all():
        gpos = QCursor.pos()
        for pet in list(ALL_PETS):
            if not _alive(pet):
                try:
                    ALL_PETS.discard(pet)
                except Exception:
                    pass
                continue
            try:
                pet.check_hover_and_click_through_global(gpos)
            except Exception:
                pass
    tm.timeout.connect(_hit_all)
    tm.start(100)
    _GLOBAL_HIT[0] = tm

def _scr():
    """主屏可用区域（带缓存，与 _screens 同源；分辨率变化 1 秒内生效）。"""
    rects = _screens()
    return rects[0] if rects else QRect(0, 0, 1920, 1080)


_SCREENS_CACHE = [None, 0.0]   # [rects, 缓存时刻]
_SCREENS_TTL = 1.0             # 秒；显示器热插拔 1 秒内生效，足够


def _screens():
    """所有显示器的可用区域（扩展屏支持）。

    ★ 带 1 秒缓存：`QGuiApplication.screens()` + `availableGeometry()` 是系统调用，
    移动中的宠物每个 tick 都会走到这里，实测会把 tick 拖慢、造成帧间隔抖动。
    显示器变化不频繁，1 秒的延迟完全可以接受。
    """
    now = time.monotonic()
    cached = _SCREENS_CACHE[0]
    if cached is not None and (now - _SCREENS_CACHE[1]) < _SCREENS_TTL:
        return cached
    scr = QGuiApplication.screens() or []
    rects = [s.availableGeometry() for s in scr]
    if not rects:
        rects = [_scr()]
    _SCREENS_CACHE[0] = rects
    _SCREENS_CACHE[1] = now
    return rects


def _has_screen_at(x, y):
    """该点是否落在任意一块屏的可用区域内。

    用于区分「真屏幕边缘」和「屏缝」：
      · 边缘外侧没有任何屏 → 真边缘 → 反弹
      · 边缘外侧还有屏     → 屏缝   → 不反弹，让它走过去
    """
    for r in _screens():
        if r.left() <= x <= r.right() and r.top() <= y <= r.bottom():
            return True
    return False


def _screen_at(cx, cy):
    """按点选屏：落在某块屏内就用它；都不在（屏缝/空洞）就取中心最近的那块。"""
    rects = _screens()
    for r in rects:
        if r.left() <= cx <= r.right() and r.top() <= cy <= r.bottom():
            return r
    best = rects[0]
    best_d = None
    for r in rects:
        dx = max(r.left() - cx, 0, cx - r.right())
        dy = max(r.top() - cy, 0, cy - r.bottom())
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best_d = d
            best = r
    return best


def roam_ratio(progress, window):
    """把 clip 播放进度映射成位移比例（照搬大肥鱼 animation_model.roam_ratio）。

    · progress <= window[0]：起步准备，位置冻结在起点
    · progress >= window[1]：收尾站定，位置已在终点
    · 中间：线性插值
    """
    start, end = window
    if progress <= start:
        return 0.0
    if progress >= end:
        return 1.0
    return (progress - start) / max(1e-6, end - start)


def crossfade_duration(previous_clip, current_clip, drag_clips=()):
    """照搬大肥鱼：只在**片段切换**时淡化，不混合相邻帧；拖拽片段不淡化。"""
    if not previous_clip or previous_clip == current_clip:
        return None
    if previous_clip in drag_clips or current_clip in drag_clips:
        return None
    return 0.10   # 100ms


def throw_bounds_in(area, size, side_allow):
    """抛掷边界：左右允许窗口出屏 side_allow（身体贴边），上下按窗口贴边。

    ⚠ 语义与 pet 的 `throwBoundsIn` **不同**：pet 的 throwStep 判断 `x > b.maxX`（左上角上限），
    而我们的 `update_physics` 判断 `x + w > max_x`（**右缘/底缘上限**）。
    所以这里必须返回右缘/底缘，否则墙会往里缩整整一个窗口尺寸（宽 480 / 高 270）。
    """
    return (
        float(area.left()) - float(side_allow),          # 窗口左缘下限
        float(area.top()) - TOP_SHIFT,                   # 窗口上缘下限（整体上移 20）
        float(area.left() + area.width()) + float(side_allow),  # ★ 窗口右缘上限
        float(area.top() + area.height()) + BOTTOM_EXTEND - TOP_SHIFT,  # 底缘（+50 再上移 20 = +30）
    )


# 碰撞框 = 实测身体范围（640×360 画布坐标：待机首帧按亮度>26 取非黑像素包围盒）。
# 与 dsh-pet 的 BODY_BOX 完全一致 —— 抽帧后仍是 640×360，两者同源同尺寸。
# ---- 抛掷物理（与 pet 的 shared-core.js 同值）----
GRAVITY = 1400.0          # px/s²
RESTITUTION = 0.78        # 弹性
GROUND_FRICTION = 2.5     # 落地摩擦
REST_VX = 15.0            # 静止阈值（水平）
REST_VY = 40.0            # 静止阈值（垂直）
MAX_STEP_DT = 0.05        # 单步上限
MAX_THROW_SPEED = 3600.0  # 初速上限
RELEASE_STALE_MS = 120.0  # 松手前这么久没动 → 不抛
RELEASE_WINDOW_MS = 150.0
MIN_THROW_SPEED = 220.0   # 低于它只算放下

# ---- 工作状态轮播的动画（两者交替播放）----
WS_WORK_ANIMS = ("写代码", "工作状态-忙碌点按")

# ---- 拖拽动画三段（实测 `被鼠标拖拽悬空反馈` 的结构，单位：帧）----
DRAG_STAND_END = 21     # 站立结束
DRAG_LIFT_END = 42      # 上升结束（悬空开始）
DRAG_HOVER_END = 200    # 悬空结束（落下开始）
DRAG_FALL_END = 240     # 落下结束（= 最后一帧）

# 双击判定窗口（毫秒）。300 而非系统默认 500：
# 多数人双击间隔 150~250ms，300 留了余量，又能让"手慢的两次单击"照常播动作。
DOUBLE_CLICK_MS = 300


def estimate_release_velocity(trail, now_ms):
    """从拖拽轨迹估松手速度；返回 (vx, vy) 或 None（太慢 / 太久没动）。"""
    if len(trail) < 2:
        return None
    if now_ms - trail[-1][0] > RELEASE_STALE_MS:
        return None
    win = [p for p in trail if now_ms - p[0] <= RELEASE_WINDOW_MS]
    if len(win) < 2:
        return None
    t0, x0, y0 = win[0]
    t1, x1, y1 = win[-1]
    span = t1 - t0
    if span < 16.0:
        return None
    vx = (x1 - x0) / span * 1000.0
    vy = (y1 - y0) / span * 1000.0
    if (vx * vx + vy * vy) ** 0.5 < MIN_THROW_SPEED:
        return None
    return vx, vy


# 抛掷/漫游底边向下拓展的像素数：让宠物能"踩"到屏幕底缘以下一点，
# 视觉上贴地更自然（不跟随缩放，按物理像素补偿）。
BOTTOM_EXTEND = 50.0

# 整个活动区域向上平移的像素数（顶边同减）：用户要求"上移 20"。
# 底边净效果 = 屏幕底 + BOTTOM_EXTEND - TOP_SHIFT = +30。
TOP_SHIFT = 20.0

# 所有抽帧素材的画布尺寸（106 个动作实测一致，640×360）
FRAME_W, FRAME_H = 640, 360

BODY_BOX = (210, 62, 217, 269)

# 漫游时身体允许被屏幕边缘遮住的比例（用户实测口径 1/7；
# 1/3 时视觉上像被遮了 2/3，因为圆润角色切边看起来比数字更狠）
WALL_COVER = 1.0 / 7.0

# 多开召唤后"叠着不动"的时长：期间只循环播待机，到期一起散开
SPAWN_HOLD_MS = 2000

# 设置 Win32 点击穿透
def set_click_through(hwnd, enable):
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x80000
    WS_EX_TRANSPARENT = 0x20
    try:
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enable:
            ex_style |= (WS_EX_LAYERED | WS_EX_TRANSPARENT)
        else:
            ex_style &= ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
        # 核心：必须调用 SetWindowPos 才能让扩展样式立即生效
        # SWP_NOMOVE = 0x0002, SWP_NOSIZE = 0x0001, SWP_NOZORDER = 0x0004, SWP_NOACTIVATE = 0x0010, SWP_FRAMECHANGED = 0x0020
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)
    except:
        pass

class PetWindow(QWidget):
    def build_menu_tree(self):
        # 构造 (分类名, [动作名...]) 的列表
        groups = []
        # 基础池
        pools = [
            ("待机", self.raw_anims.get("idle", [])),
            ("转向", self.raw_anims.get("turn", [])),
            ("拖拽", self.raw_anims.get("drag", [])),
            ("点击回应", self.raw_anims.get("clicks", [])),
            ("移动", [m["name"] for m in self.raw_anims.get("moves", {}).get("actions", [])])
        ]
        for name, pool in pools:
            if pool: groups.append((name, pool))
            
        # 随机动作分类
        for cat in self.raw_anims.get("categories", []):
            acts = cat.get("actions", [])
            if acts: groups.append((cat.get("id", "分类"), acts))
            
        # 事件动画
        events = self.raw_anims.get("events", {})
        event_labels = {"balance": "余额档位", "whisper": "碎碎念", "workStatus": "工作状态"}
        for key, pool in events.items():
            if pool:
                label = event_labels.get(key, key)
                groups.append((label, pool))
        return groups

    def __init__(self, base_dir, config_data, store, scale=None):
        # ★ 置顶标志跟随全局开关：用户关掉「置顶」后多开的新个体也不该压在最上层
        _flags = Qt.FramelessWindowHint | Qt.Tool
        if topmost_enabled():
            _flags |= Qt.WindowStaysOnTopHint
        super().__init__(None, _flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)  # 核心修复：允许悬停时触发 mouseMoveEvent
        
        self.base_dir = base_dir
        self.store = store
        self.weights, self.pools, self.move_params, self.ws_anims, self.ws_texts, self.raw_anims = config_data
        
        self.cur_pix = None
        self._facing_right = False
        # ★ 光标倾斜跟随（照搬 whale-pet 的 wp-fx 层）。
        #   只给**单体模式**用（见 tilt_enabled()）：主体和子体都启用，群体模式不启用。
        #   状态是纯数学对象，paintEvent 里按需应用变换。
        self.tilt = TiltState()
        # ★ 无变换帧的缩放缓存：{(id(帧), w, h): 缩放后的 QImage}
        #   只在"完全没有变换"的帧使用，把常态 CPU 拉回改造前水平。
        self._paint_scale_cache = {}
        # ★ 拖拽 Q 弹（照搬 whale-pet 的 wp-fx / wp-breathe）。
        #   三个机制：拖拽中速度拉伸 / 松手回落 / 单击弹跳。
        #   与倾斜共用同一个 painter 变换栈，顺序见 paintEvent。
        self.jelly = JellyState()
        
        self.fps = 24.0
        self.frame_ms = 1000.0 / self.fps
        self.clock = QElapsedTimer()
        self.clock.start()
        
        self.current_name = None
        self.pms = []
        self.frame_idx = -1
        self.loaders = []
        
        # ★ 不再每只一个 QTimer：由模块级全局定时器统一驱动（见 _start_global_tick）。
        #   每只各自持有 14ms 定时器时，30 只 = 2143 次/秒回调挤在一个事件队列，会抖成抽搐。
        # 帧内余数（ms）：divmod 的余数，照搬大肥鱼的 frame_elapsed_ms
        self._frame_elapsed = 0.0
        # 片段切换交叉淡化（照搬大肥鱼：旧帧不透明垫底 + 新帧淡入）
        self._fade_from = None      # 旧片段最后一帧（QImage）
        self._fade_t0 = 0.0
        self._fade_dur = 0.0
        self._last_tick_t = 0.0     # 上次 tick 的单调时钟
        _start_global_tick()   # 确保全局定时器已启动
        
        self.drag_on = False
        # ---- 抛掷物理状态 ----
        self.phys_timer = QTimer(self)
        self.phys_timer.setTimerType(Qt.PreciseTimer)
        self.phys_timer.timeout.connect(self.tick_physics)
        self.phys_timer.setInterval(16)
        self.phys_x = 0.0
        self.phys_y = 0.0
        self.phys_vx = 0.0
        self.phys_vy = 0.0
        self._phys_last_t = 0.0
        self.drag_trail = []   # [(t_ms, x, y)] 拖拽轨迹，用于松手估速
        # ---- 落地 Q 弹（照搬 pet 的 startSquash）----
        self._squash_scale = 1.0
        self._squash_depth = SQ_SQUASH
        self._squash_t0 = 0.0
        self._squash_timer = QTimer(self)
        self._squash_timer.setTimerType(Qt.PreciseTimer)
        self._squash_timer.setInterval(16)
        self._squash_timer.timeout.connect(self._tick_squash)
        self._prev_grounded = False
        # 正在等待新动作解码：期间旧动画继续循环，避免定格
        self._loading_new = False
        # 拖拽三段播放范围 [start, end)；None 表示按整段循环
        self._drag_range = None
        # 待播的工作状态动画：(动作名, 气泡文本)；当前动画播完才播（不硬插）
        self._pending_ws = None
        # ★ 完整播放保护：非交互触发排队 / "下一个动作"待续 / 本动作时长
        self._queued_action = None
        self._pending_next = False
        self._action_t0 = 0.0
        self._action_dur = 0.0
        # 当前工作状态（None = 空闲）。非 None 时只播工作动画，不进随机链
        self._ws_state = None
        self._ws_anim = ""      # 当前状态对应的动画名
        self._ws_last_at = 0.0  # 最后一次收到工作状态事件的时间
        self._ws_turn_t0 = 0.0  # 当前 turn 的起始时间（防卡死）
        self._last_action_switch_t = 0.0   # 上次切换动作的时间（防 ended 死循环）
        # 本动作的**总帧数**（首批可能只到了前 60 帧，播放长度按 total 算）
        self._total_frames = 0
        self._ground_frames = 0   # 连续贴地帧数（用于收掉尾段小幅弹跳）
        self.drag_off = QPoint()
        self.last_pos = QPoint()
        self.last_time = time.time()
        self.vx = 0.0
        self.vy = 0.0
        

        self.move_target_x = 0
        self.move_start_x = 0
        self.is_moving_action = False
        self.is_first_load = True
        self.is_click_through = False
        
        # 30ms 全局鼠标悬停与穿透检测
        self.hit_timer = None  # 由全局定时器统一驱动
        
        # 加载 .cur 光标：优先项目根（独立安装版），其次开发环境的 dafeiyu/assets
        pic_dir = self.base_dir.parent
        if not (pic_dir / 'cursor_grab.cur').is_file():
            alt = self.base_dir.parent.parent / 'dafeiyu' / 'assets'
            if (alt / 'cursor_grab.cur').is_file():
                pic_dir = alt
        if 'grab' not in _GLOBAL_CURSORS:
            grab_pm = QPixmap(str(pic_dir / 'cursor_grab.cur'))
            grabbing_pm = QPixmap(str(pic_dir / 'cursor_grabbing.cur'))
            _GLOBAL_CURSORS['grab'] = QCursor(grab_pm) if not grab_pm.isNull() else QCursor(Qt.OpenHandCursor)
            _GLOBAL_CURSORS['grabbing'] = QCursor(grabbing_pm) if not grabbing_pm.isNull() else QCursor(Qt.ClosedHandCursor)
        self.cursor_grab = _GLOBAL_CURSORS['grab']
        self.cursor_grabbing = _GLOBAL_CURSORS['grabbing']
        
        self.bubble = BubbleWindow(self)
        
        self.last_ws_counter = -1
        self.state_timer = QTimer()
        self.state_timer.timeout.connect(self.poll_state)
        self.state_timer.start(500)
        self.config_data = config_data
        # 多开直接继承主体大小：创建时就设好，避免"先按默认大小出现、再缩一下"
        self.scale = float(scale) if (scale is not None and float(scale) > 0) else 0.75
        self.pms_disp = []
        self._scale_cache = {}
        self._scale_cache_order = []
        # 0 = 主体，>0 = 多开；用自增序号而不是 len(ALL_PETS)，避免关闭后序号复用
        self.spawn_idx = _SPAWN_SEQ[0]
        _SPAWN_SEQ[0] += 1
        # 主体标志：全程序统一用它判断"谁是主体"，比到处写 spawn_idx==0 清楚。
        # 注意不能用"有没有气泡"判断——每个 PetWindow 在 __init__ 里都创建了 BubbleWindow，
        # 只是是否显示不同。
        self.is_main = (self.spawn_idx == 0)
        # 上次单击松手的时间（monotonic 秒），用于判双击 → 查看余额
        self._last_click_t = 0.0
        # 显示名（照搬 pet）：主体用真实名字，多开用**数字编号**，
        # 这样"哪只镜像反了"可以直接报编号。
        self.pet_label = "大肥鱼" if self.is_main else str(self.spawn_idx)
        # 名字气泡（照搬 pet 的 renderTip）：**窗口内子控件**，跟随光标显示名字/编号。
        # 不用独立顶层窗口——实测 show() 在某些环境下不生效（isVisible 恒 False）。
        self.tip = QLabel(self)
        self.tip.setStyleSheet(
            "background: rgba(255,255,255,0.97); color: #2b2b2b;"
            " border: 1px solid rgba(0,0,0,0.14); border-radius: 7px;"
            " padding: 1px 7px;"
            " font-family: 'Microsoft YaHei UI'; font-size: 12px;")
        self.tip.setText(self.pet_label)
        self.tip.adjustSize()
        self.tip.hide()
        ALL_PETS.append(self)
        _start_global_hit()
        self.spawn_random = False
        self.is_roaming = False
        self._move_loading = False
        self._facing_right = False
        # ★ 多开的第一趟移动强制 360° 随机（对齐 dsh-pet 的 firstMoveRandom）：
        #   一批副本起手的 facing/微小合力几乎一致，走涌现判定极易被统一成"齐步走"。
        # ★ 模式是**个体级**的：多开默认群体，个体可自行切单体
        self.flock_enabled = False
        # 抛掷期间临时进单体，结束后恢复到这里
        self._mode_before_throw = None
        self._first_move_random = self.spawn_idx > 0
        # ★ 预输入：overlap 破 0.8 时**只记录意图**，不打断当前动作，播完再兑现
        self._pending_force_roam_at = 0.0
        # ★ 召唤缓冲：多开叠在屏幕正中循环播待机，SPAWN_HOLD_MS 后一起散开
        self._spawn_hold_until = 0.0
        if self.spawn_idx > 0:
            self._spawn_hold_until = time.monotonic() * 1000.0 + SPAWN_HOLD_MS
            QTimer.singleShot(SPAWN_HOLD_MS, self._end_spawn_hold)
        self.move_tiers = {'step': [], 'crab': [], 'run': []}
        # 单体模式移动档权重（pet 的 moves.tiers.weight：run 3 / crab 5 / step 4）
        self.move_tier_weights = self.pools.get('move_tier_weights') or {'run': 3, 'crab': 5, 'step': 4}
        self.move_tier_caps = {'step': 140, 'crab': 260, 'run': 99999}
        for _mn in (self.pools.get('move') or []):
            _p = self.move_params.get(_mn, {})
            _cap = _p.get('maxDist', 240)
            if _cap <= 140:
                self.move_tiers['step'].append(_mn)
            elif _cap <= 260:
                self.move_tiers['crab'].append(_mn)
            else:
                self.move_tiers['run'].append(_mn)
        if not self.move_tiers['step'] and not self.move_tiers['crab'] and not self.move_tiers['run']:
            if self.pools.get('move'):
                self.move_tiers['crab'] = list(self.pools['move'])
        self.move_target_x = self.x()
        self.move_target_y = self.y()
        self.move_anim_total = 1
        self._pending_roam = False
        self.flock_timer = QTimer(self)
        self.flock_timer.timeout.connect(self.tick_flock)
        self.flock_timer.start(150)
        self._whisper_timer = QTimer(self)
        self._whisper_timer.setSingleShot(True)
        self._whisper_timer.timeout.connect(self._fire_whisper)
        self.schedule_whisper()
        
        self.load_next_action()

    def paintEvent(self, ev):
        if self.cur_pix is None or self.cur_pix.isNull():
            return
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        # ★ 必须手动清屏：窗口设了 WA_NoSystemBackground，Qt **不会**自动擦背景。
        #   不擦的话，旋转/形变后原位置露出的像素没被清除，会残留成"帧撕裂"。
        #   （CompositionMode_Source 直接把目标写成透明，比 fillRect 默认模式干净）
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        # ★ cur_pix 现在是**原始 640×360 帧**（loader 不再预缩放）。
        #   绘制时用 drawImage(目标矩形, 原图, 源矩形) 做一次「缩放+变换」复合，
        #   避免「先缩到 480×270、再旋转」的二次重采样（那样锐度只剩 67.6%）。
        #   目标矩形 = 窗口内按 scale 缩放后的区域。
        iw, ih = self.cur_pix.width(), self.cur_pix.height()
        _s = self.scale
        pw = max(1, int(round(iw * _s)))
        ph = max(1, int(round(ih * _s)))
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        src_rect = QRectF(0.0, 0.0, float(iw), float(ih))
        dst_rect = QRectF(float(x), float(y), float(pw), float(ph))
        # ★★★ 关键：SmoothPixmapTransform 必须**无条件**开启。
        #   之前只在倾斜/Q弹生效时才 setRenderHint，导致鼠标移开后倾斜归零、
        #   hint 不再被设置 → drawImage 退回最近邻插值 → 画面变糊 + 锯齿。
        #   现在改成每帧开头无条件开启，保证任何缩放都是双线性/高质量采样。
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Q 弹：围绕**底边**做 scaleY（与 pet 的 transform-origin:bottom 一致）
        squash = getattr(self, '_squash_scale', 1.0)
        # ★ 镜像只读「移动指令的水平值」：dx = move_target_x - move_start_x。
        #   不依赖 _facing_right 这个缓存状态——它和实际位移可能不同步。
        #   也只有**移动类**动作才镜像：站位动作左右翻转没有语义。
        mirror = False
        if self.current_name in (self.pools.get('move') or []):
            mirror = (self.move_target_x - self.move_start_x) >= 0

        # ★★ 性能：判断本帧是否**完全没有变换**。
        #   无变换 = 没有镜像、没有 squash、没有倾斜、没有 Q 弹。
        #   这是绝大多数帧（站着不动、鼠标不在附近）。
        #   此时走「预缩放缓存 + 1:1 贴图」，恢复原来的低开销；
        #   只有真正需要变换的帧才用「原图 + 复合变换」（高清但略贵）。
        _tilt_on = (self.tilt is not None and not self.tilt.is_identity())
        _jelly_on = False
        _jsx = _jsy = 1.0
        _jdrag = False
        if self.jelly is not None:
            try:
                _jsx, _jsy = self.jelly.combined_scale(time.monotonic())
                _jdrag = self.jelly.drag.active
                _jelly_on = _jdrag or abs(_jsx - 1.0) > 1e-4 or abs(_jsy - 1.0) > 1e-4
            except Exception:
                pass
        _static = (not _tilt_on and not _jelly_on
                   and squash == 1.0 and not mirror)

        # ★★ 纯倾斜帧（没有 Q 弹、没有 squash）：走「预渲染角度缓存 + 1:1 整数贴图」。
        #   这是让倾斜画质**追平静止**的唯一办法 —— 任何现场旋转都要重采样，
        #   而 1:1 贴图是零重采样。缓存图按角度档预先生成（用 2× 超采样保证质量），
        #   之后每帧只付一次 drawImage 整数坐标贴图。
        #   淡化期间（动作切换那 0.18s）不走这条，免得漏掉垫底旧帧。
        _only_tilt = (TILT_CACHE_ENABLED and _tilt_on and not _jelly_on
                      and squash == 1.0)
        if _only_tilt and self._fade_from is None:
            try:
                _q = round(self.tilt.cur * TILT_CACHE_STEPS) / float(TILT_CACHE_STEPS)
                _cimg = self._tilt_rendered(_q, mirror, pw, ph)
            except Exception:
                _cimg = None
            if _cimg is not None and not _cimg.isNull():
                _dx = int(round(self.tilt.offset_x))
                painter.drawImage(int(x) + _dx, int(y), _cimg)
                return

        painter.save()
        if mirror:
            cx = x + pw / 2.0
            painter.translate(cx, 0.0)
            painter.scale(-1.0, 1.0)
            painter.translate(-cx, 0.0)
        if squash != 1.0:
            painter.translate(0.0, float(y + ph))
            painter.scale(1.0, squash)
            painter.translate(0.0, -float(y + ph))
        # ★ 光标倾斜（单体模式启用；群体模式 tilt_enabled() 为 False，
        #   此时 is_identity() 直接跳过，零开销）。
        #   绕脚底（高度×0.917）旋转，与 whale-pet 的 transform-origin:50% 91.7% 一致。
        #   注意：这里用的是**窗口坐标**原点，不是 cur_pix 的，因为整窗一起转。
        if self.tilt is not None and not self.tilt.is_identity():
            apply_tilt(painter, self.tilt, self._tilt_pivot_y())
        # ★ 拖拽 Q 弹（whale 的 wp-fx / wp-breathe）：
        #   ① 拖拽中：沿**运动方向**拉伸（rotate→scale→rotate 反向转回）
        #   ② 松手回落 / ③ 单击弹跳：纯缩放，绕脚底
        #   与倾斜共用同一变换栈。拖拽期间倾斜恒为 0（tilt_enabled 返回 False），
        #   所以不会出现两个旋转叠加导致形变失真。
        if self.jelly is not None:
            try:
                _now_s = time.monotonic()
                _jsx, _jsy = self.jelly.combined_scale(_now_s)
                _jactive = self.jelly.drag.active
                if _jactive:
                    # ① 沿运动方向拉伸
                    _px = x + pw / 2.0
                    _py = y + ph          # 锚点：图形底边（与脚底一致）
                    _ang = self.jelly.drag_angle()
                    painter.translate(_px, _py)
                    painter.rotate(math.degrees(_ang))
                    painter.scale(_jsx, _jsy)
                    painter.rotate(-math.degrees(_ang))
                    painter.translate(-_px, -_py)
                elif abs(_jsx - 1.0) > 1e-4 or abs(_jsy - 1.0) > 1e-4:
                    # ② ③ 纯缩放：锚点必须同时 translate x 和 y。
                    #   ★ 只 translate y 是错的：scale(sx, sy) 会以原点(0,0)为基准
                    #     放大横向，导致图形整体向右漂 —— 表现为"往右弹一下"。
                    #   锚点取「图形中心 x、底边 y」：横向对称、纵向贴脚底。
                    _px = float(x + pw / 2.0)
                    _py = float(y + ph)
                    painter.translate(_px, _py)
                    painter.scale(_jsx, _jsy)
                    painter.translate(-_px, -_py)
            except Exception:
                pass
        # ★ 片段切换淡化（照搬大肥鱼）：旧帧**不透明**垫底，新帧按 alpha 淡入。
        #   这样不会出现"两者都不全"的空白帧，也不会像 0.18s 交叉淡化那样重影。
        fade_a = 1.0
        if self._fade_from is not None and self._fade_dur > 0:
            el = time.monotonic() - self._fade_t0
            if el >= self._fade_dur:
                self._fade_from = None
                self._fade_dur = 0.0
            else:
                fade_a = min(1.0, (el / self._fade_dur) ** 0.7)   # 缓动，与大肥鱼一致
        if self._fade_from is not None and fade_a < 1.0:
            painter.setOpacity(1.0)
            # 垫底的旧帧同样按目标矩形绘制（它也是原图尺寸）
            _fiw = self._fade_from.width()
            _fih = self._fade_from.height()
            if _fiw > 0 and _fih > 0:
                painter.drawImage(dst_rect, self._fade_from,
                                  QRectF(0.0, 0.0, float(_fiw), float(_fih)))
            painter.setOpacity(fade_a)
        # ★ 一次复合变换：drawImage(dst, src_image, src_rect)
        #   Qt 会把「缩放 + 当前 painter 变换（旋转/拉伸/倾斜）」合成一次重采样。
        if _static:
            # ★ 无变换路径：用按需缓存的「已缩放图」1:1 贴图，恢复原性能。
            #   （缓存按 (帧对象, 尺寸) 命中，同一帧重复绘制零重采样）
            _scaled = self._scaled_frame_for_paint(pw, ph)
            if _scaled is not None:
                painter.drawImage(int(x), int(y), _scaled)
            else:
                painter.drawImage(dst_rect, self.cur_pix, src_rect)
        else:
            # 有变换路径。**只有带旋转**（倾斜 / 拖拽拉伸）时才值得用超采样源图：
            # 旋转会把每个输出像素摊到 4 个源像素上，此时缩小倍率大就容易混叠。
            # 纯镜像 / 纯缩放（settle、pop、squash）不旋转，用原图即可 ——
            # 群体模式里满屏都是镜像的移动帧，给它们也生成中间图会把 CPU 拖垮。
            _need_hi = _tilt_on or (_jelly_on and _jdrag)
            _ssrc = None
            if _need_hi:
                # 先用**面积平均**缩到 1.5× 显示尺寸，再交给 drawImage 做
                # 「缩 0.67 倍 + 旋转」这一次重采样。原因见 TILT_SRC_SUPERSAMPLE。
                # ★ 中间图绝不能超过原图尺寸：scale=1.0 时 pw 就等于原图宽，
                #   1.5× 会变成「放大再缩回」，凭空多两次插值、只剩损失
                #   （实测高频 10.19% → 6.25%，反而比不超采样还低）。
                try:
                    _tw = min(max(1, int(round(pw * TILT_SRC_SUPERSAMPLE))),
                              self.cur_pix.width())
                    _th = min(max(1, int(round(ph * TILT_SRC_SUPERSAMPLE))),
                              self.cur_pix.height())
                    _ssrc = self._scaled_frame_for_paint(_tw, _th)
                except Exception:
                    _ssrc = None
            if _ssrc is not None and not _ssrc.isNull():
                painter.drawImage(dst_rect, _ssrc,
                                  QRectF(0.0, 0.0, float(_ssrc.width()), float(_ssrc.height())))
            else:
                painter.drawImage(dst_rect, self.cur_pix, src_rect)
        painter.setOpacity(1.0)
        painter.restore()

    def _tilt_rendered(self, quant, mirror_on, pw, ph):
        """按角度档预渲染一张「已旋转好」的 pw×ph 图（**不含**横向位移）。

        quant 是量化到 1/TILT_CACHE_STEPS 的 cur 值（-1~1），横向位移由调用方
        用整数坐标叠加，这样缓存只按角度分档，不必为每个位移量再存一份。

        生成时先用 TILT_CACHE_SUPERSAMPLE 倍的**面积平均**图（默认 1.0，即和静止
        用的是同一张 288×162），painter 只做**旋转**、不做缩放 —— 一旦让 drawImage
        去缩小，Qt 就退回 2×2 双线性、跳过源像素，整张图会糊。
        之后每帧只是 1:1 整数贴图，零重采样。
        """
        # 角度归零且不镜像：直接用静止那张，保证和「没摇晃」时**像素级一致**
        if not quant and not mirror_on:
            still = self._scaled_frame_for_paint(pw, ph)
            if still is not None and not still.isNull():
                return still
        key = (id(self.cur_pix), quant, bool(mirror_on), pw, ph)
        from PySide6.QtGui import QImage, QPainter
        c = getattr(self, '_tilt_paint_cache', None)
        if c is None:
            c = {}
            self._tilt_paint_cache = c
        hit = c.get(key)
        if hit is not None:
            return hit
        # 只保留当前帧的条目（帧一换，旧的全部作废）。
        # 不设阈值直接清：一帧最多 TILT_CACHE_STEPS×2 张，内存可控。
        cur_id = id(self.cur_pix)
        if any(k[0] != cur_id for k in c):
            for k in [k for k in c if k[0] != cur_id]:
                c.pop(k, None)
        # ★ 优先用 PIL 的 BICUBIC（支撑域 4×4，高频保留 ≈ 97%）；
        #   Qt 的 drawImage 只有 2×2 双线性，旋转一次就掉到 ≈ 71%。
        #   实测 640×360 一次 bicubic 旋转 15ms，只在换角度档时发生。
        if _PIL_IMAGE is not None:
            got = self._tilt_rendered_pil(quant, mirror_on, pw, ph)
            if got is not None:
                c[key] = got
                return got
        tw = min(max(1, int(round(pw * TILT_CACHE_SUPERSAMPLE))), self.cur_pix.width())
        th = min(max(1, int(round(ph * TILT_CACHE_SUPERSAMPLE))), self.cur_pix.height())
        ssrc = self._scaled_frame_for_paint(tw, th)
        if ssrc is None or ssrc.isNull():
            return None
        # ★ 画布也是 tw×th：让 painter 在该尺寸上**只旋转、不缩放**（1:1），
        #   最后再用 QImage.scaled(面积平均) 缩到 pw×ph。
        #   顺序很关键 —— 一旦让 drawImage 直接缩到 pw×ph，Qt 就用 2×2 双线性
        #   跳过源像素，混叠会原样回来（这正是"开了摇晃就糊"的根源）。
        try:
            canvas = QImage(tw, th, QImage.Format_ARGB32_Premultiplied)
        except Exception:
            return None
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        if mirror_on:
            p.translate(float(tw), 0.0)
            p.scale(-1.0, 1.0)
        pivot_y = self._tilt_pivot_y() * (float(th) / float(ph))
        p.translate(tw / 2.0, pivot_y)
        p.rotate(float(quant) * TILT_MAX_DEG)
        p.translate(-tw / 2.0, -pivot_y)
        p.drawImage(QRectF(0.0, 0.0, float(tw), float(th)), ssrc,
                    QRectF(0.0, 0.0, float(ssrc.width()), float(ssrc.height())))
        p.end()
        if (tw, th) != (pw, ph):
            # 面积平均收尾（这是 Qt 里唯一「每个源像素都参与」的缩小路径）
            canvas = canvas.scaled(pw, ph, Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)
        c[key] = canvas
        return canvas

    def _tilt_rendered_pil(self, quant, mirror_on, pw, ph):
        """用 PIL 的 BICUBIC 按角度档预渲染（高频保留 ≈ 静止的 96.7%）。

        为什么值得：Qt 的 drawImage 只有 2×2 双线性，3.5° 旋转后高频从
        10.19% 掉到 7.27%（只剩 71%）；BICUBIC 支撑域 4×4，能到 9.85%（96.7%），
        肉眼基本看不出和「不摇晃」的差别。

        坐标约定（踩过的坑）：PIL 的 AFFINE 把输出像素 (x, y) 映射到输入
        (a*x+b*y+c, ...)，其中 (0, 0) **就是第一个像素的中心** —— 实测 offset=0
        时恒等变换与 NEAREST 逐像素完全一致，offset=±0.5 则整体偏移半像素。
        所以这里不要额外加 0.5。
        """
        if _PIL_IMAGE is None:
            return None
        from PySide6.QtGui import QImage
        src = self.cur_pix
        if src is None or src.isNull():
            return None
        sw, sh = src.width(), src.height()
        if sw <= 0 or sh <= 0 or pw <= 0 or ph <= 0:
            return None
        try:
            q = src.convertToFormat(QImage.Format_RGBA8888)
            im = _PIL_IMAGE.frombytes("RGBA", (sw, sh), bytes(q.constBits()))
            if mirror_on:
                im = im.transpose(_PIL_IMAGE.FLIP_LEFT_RIGHT)
            kx = sw / float(pw)          # 输出 → 源的缩放比
            ky = sh / float(ph)
            ang = math.radians(float(quant) * TILT_MAX_DEG)
            ca, sa = math.cos(ang), math.sin(ang)
            pxc = pw / 2.0
            pyc = ph * TILT_ORIGIN_Y_FALLBACK
            c0 = kx * (ca * (-pxc) + sa * (-pyc) + pxc)
            f0 = ky * (-sa * (-pxc) + ca * (-pyc) + pyc)
            out = im.transform((pw, ph), _PIL_IMAGE.AFFINE,
                               (kx * ca, kx * sa, c0, -ky * sa, ky * ca, f0),
                               resample=_PIL_IMAGE.BICUBIC)
            qi = QImage(out.tobytes(), pw, ph, pw * 4, QImage.Format_RGBA8888)
            return qi.convertToFormat(
                QImage.Format_ARGB32_Premultiplied).copy()
        except Exception:
            return None

    def _scaled_frame_for_paint(self, pw, ph):
        """给「无变换」帧用的已缩放图缓存（按当前帧 + 目标尺寸）。

        为什么需要：原图 640×360 每帧 drawImage 缩放约 0.19ms，
        30 只 24fps 就是常态 18% 单核。缓存后命中路径接近 0.07ms，
        恢复改造前的性能水平。
        只在**同一帧重复绘制**时命中（动画 24fps、tick 70Hz，
        所以每帧大约会被画 2~3 次，命中率约 60%）。
        """
        try:
            key = (id(self.cur_pix), pw, ph)
            c = getattr(self, '_paint_scale_cache', None)
            if c is None:
                c = {}
                self._paint_scale_cache = c
            hit = c.get(key)
            if hit is not None:
                return hit
            # ★ 按**尺寸分组**各留 2 张（当前帧 + 上一帧），不是按总条数。
            #   倾斜时会在「288 显示图」和「432 变换源图」两个尺寸之间来回请求，
            #   用总条数限制会让两组互相清空 → 帧帧重算。
            #   按尺寸分组则两组各自稳定，内存也不会随条数膨胀
            #   （群体 100 只只用得到 288 那一组，约 0.37MB/只）。
            same = [k for k in c if k[1] == pw and k[2] == ph]
            if len(same) > 2:
                for k in same[:len(same) - 2]:
                    c.pop(k, None)
            scaled = self.cur_pix.scaled(pw, ph,
                                         Qt.IgnoreAspectRatio,
                                         Qt.SmoothTransformation)
            c[key] = scaled
            return scaled
        except Exception:
            return None

    def is_in_body(self, pos):
        s = self.scale
        bx, by, bw, bh = BODY_BOX[0]*s, BODY_BOX[1]*s, BODY_BOX[2]*s, BODY_BOX[3]*s
        # 容差 6px（按缩放折算）：BODY_BOX 是亮度阈值实测的包围盒，
        # 边缘的毛发/半透明像素天然落在框外，不加容差会"看着碰到却点不中"。
        pad = 6.0 * s
        return (bx - pad) <= pos.x() <= (bx + bw + pad) and (by - pad) <= pos.y() <= (by + bh + pad)

    def load_next_action(self, force=False):
        # ★ 已关窗（Qt 资源拆完）：一次性回调可能还会打回来，直接早退
        if getattr(self, '_qt_disposed', False):
            return
        # ★ 拖拽与飞行期间不换动作：拖拽最高优先级，飞行中也不该被随机动作打断。
        if self.drag_on or self.phys_timer.isActive():
            return
        # ★ 完整播放保护：当前动作还没播完 → 记下"待续"，等 ended 时再抽。
        #   （召唤散开、抛掷落地等自动路径都走这里，不能中途换掉正在播的动作）
        #   force=True 是唯一例外：召唤缓冲到期要"全体同时散开"，
        #   等待机整段播完会让 10 只的散开时刻各不相同，爆发感就没了。
        if not force and self._action_incomplete():
            self._pending_next = True
            return
        self._last_action_switch_t = time.monotonic()
        # ★ **不停帧定时器**：新动作要 1~1.7 秒才解码好，这期间旧动画继续播
        #   （tick 里 _loading_new 分支会循环播放）。停掉 timer 就是"定格卡帧"。
        self.is_roaming = False
        self.is_moving_action = False

        # ---- 召唤缓冲：叠在一起时只循环播**待机**，不抽动作 ----
        if self._spawn_hold_until and time.monotonic() * 1000.0 < self._spawn_hold_until:
            idle_pool = self.pools.get('idle') or []
            if idle_pool:
                hold_name = random.choice(idle_pool)
                self._request_frames(hold_name, 'hold')
                return

        # ---- 预输入消费：overlap 破 0.8 时记下的意图（TTL 2500ms）----
        forced_roam = False
        if self._pending_force_roam_at:
            if time.monotonic() * 1000.0 - self._pending_force_roam_at <= 2500:
                forced_roam = True
            self._pending_force_roam_at = 0.0

        # === 群体模式：决定是否走漫游池 ===
        roam_decision = forced_roam
        overlap = 0.0
        # 单体模式不在这里判定漫游：见 else 分支的「统一大池」，抽到移动动作即漫游。
        if self.flock_enabled and len(ALL_PETS) > 1:
            try:
                s = self.scale
                bx, by, bw, bh = BODY_BOX
                cx = self.x() + (bx + bw / 2.0) * s
                cy = self.y() + (by + bh / 2.0) * s
                half_w = max(1.0, bw * s / 2.0)
                half_h = max(1.0, bh * s / 2.0)
                mates2 = []
                for pp in ALL_PETS:
                    if pp is self or not pp.isVisible():
                        continue
                    ps = getattr(pp, 'scale', 1.0)
                    mates2.append((pp.x() + (bx + bw / 2.0) * ps, pp.y() + (by + bh / 2.0) * ps))
                if mates2:
                    overlap = overlap_ratio(cx, cy, half_w, half_h, mates2)
                    chance = roam_chance_for_overlap(overlap)
                    if overlap >= 0.8 or random.random() < chance:
                        roam_decision = True
            except Exception:
                pass

        if roam_decision and (self.move_tiers['step'] or self.move_tiers['crab'] or self.move_tiers['run']):
            # 先由涌现算本段位移距离（已含 speedUp），再按距离选 tier
            s = self.scale
            bx, by, bw, bh = BODY_BOX
            cx = self.x() + (bx + bw / 2.0) * s
            cy = self.y() + (by + bh / 2.0) * s
            half_w = max(1.0, bw * s / 2.0)
            half_h = max(1.0, bh * s / 2.0)
            mates = []
            for pp in ALL_PETS:
                if pp is self or not pp.isVisible():
                    continue
                ps = getattr(pp, 'scale', 1.0)
                mates.append((pp.x() + (bx + bw / 2.0) * ps, pp.y() + (by + bh / 2.0) * ps, getattr(pp, '_facing_dir', 1)))
            base_dist = random.randint(60, 240)  # 对齐 dsh-pet moves.default（60~240）
            up = speed_up_for(overlap)
            want_dist = base_dist * up

            # 按距离选 tier
            chosen_tier = 'run'
            for tname in ('step', 'crab', 'run'):
                if self.move_tiers[tname] and want_dist <= self.move_tier_caps[tname]:
                    chosen_tier = tname
                    break
            cand = [m for m in self.move_tiers[chosen_tier] if m != self.current_name] or self.move_tiers[chosen_tier]
            if not cand:
                cand = self.pools.get('move') or []
            if cand:
                next_name = random.choice(cand)
                # 算目标点：steer_target 只调制方向和距离
                _s = _scr()
                body_left = self.x() + bx * s
                body_right = self.x() + (bx + bw) * s
                body_top = self.y() + by * s
                body_bottom = self.y() + (by + bh) * s
                gl = max(0.0, body_left - _s.left())
                gr = max(0.0, _s.right() - body_right)
                gt = max(0.0, body_top - _s.top())
                gb = max(0.0, _s.bottom() - body_bottom)
                if self._first_move_random:
                    # ★ 首次移动强制 360° 随机：不看合力、不看朝向。
                    #   保证"第一趟真正跑出去"的方向一定是散的，之后才交给涌现。
                    ang = random.uniform(0, 2 * math.pi)
                    tx = cx + math.cos(ang) * want_dist
                    ty = cy + math.sin(ang) * want_dist
                    new_dir = 1 if math.cos(ang) >= 0 else -1
                    self._first_move_random = False
                else:
                    tx, ty, new_dir, has_force = steer_target(cx, cy, half_w, half_h,
                        getattr(self, '_facing_dir', 1), mates, want_dist,
                        (gl, gr, gt, gb), overlap)
                    if not has_force:
                        # 合力为零（全部重叠 / 无邻居）→ 360° 全角度随机，
                        # 不能继承 facing（一群都是 left 就会齐步往左跑）
                        ang = random.uniform(0, 2 * math.pi)
                        tx = cx + math.cos(ang) * want_dist
                        ty = cy + math.sin(ang) * want_dist
                        new_dir = 1 if math.cos(ang) >= 0 else -1
                # 朝向按最终目标点相对中心的方向，保证与位移一致
                self._facing_dir = 1 if (tx - cx) >= 0 else -1
                self.move_target_x = tx - (bx + bw / 2.0) * s
                self.move_target_y = ty - (by + bh / 2.0) * s
                self.is_roaming = True
                self.is_moving_action = True
                self.move_start_x = self.x()
                self.move_start_y = self.y()
                self._request_frames(next_name, 'roam')
                return

        # 普通动作
        if self.flock_enabled and len(ALL_PETS) > 1:
            # 群体模式的大池 = idle + turn + 全部 categories，**等概率**
            # （对齐 dsh-pet 的 pickBigPoolAction：82 个非移动动作平分 75% 权重）
            next_name = pick_big_pool_equal(self.pools, self.current_name)
            cat_type = 'pool'
        else:
            # ★ 单体模式：**所有动作统一进大池**（idle + turn + move + 全部 categories），
            #   等概率抽一个；抽到移动动作就执行位移（漫游绑定在动作上），抽到别的正常播。
            facing_right = getattr(self, '_facing_dir', 1) >= 0
            next_name = pick_big_pool_equal(self.pools, self.current_name,
                                            include_moves=True,
                                            facing_right=facing_right)
            if next_name in (self.pools.get('move') or []):
                cat_type = 'move'
            elif next_name in (self.pools.get('idle') or []):
                cat_type = 'idle'
            elif next_name in (self.pools.get('turn') or []):
                cat_type = 'turn'
            else:
                cat_type = 'cat'
            if not next_name:
                next_name = pick_category_action(self.pools.get('categories') or [],
                                                 self.pools.get('idle') or [],
                                                 facing_right, self.current_name)
                cat_type = 'cat'
        if not next_name:
            try:
                from src.host.logging_setup import get_logger
                get_logger().error('动作池为空 → 直接退出进程（这是"自发退出"的一种）')
            except Exception:
                pass
            return QApplication.quit()
        self._request_frames(next_name, cat_type)

    def _request_frames(self, name, cat_type, msg='', restart=False):
        """统一入口：命中缓存同步喂，正在解码则排队，否则起一个 LoadThread。

        ★ 缓存键不带尺寸：帧现在是原始 640×360（loader 不预缩放），
          缩放由 paintEvent 的 drawImage(dst, src, srcRect) 一次完成。
          好处：不同 scale 共享同一份帧；set_scale 不再触发重解码。
        """
        pw = max(1, int(FRAME_W * self.scale))
        ph = max(1, int(FRAME_H * self.scale))
        cached = frame_cache.get(name, 0, 0)
        if cached is not None:
            self._load_gen = getattr(self, '_load_gen', 0) + 1
            # ★ 重播同一个动作时，on_loaded 的 upgrading 分支会直接 return、
            #   不重置 frame_idx → 动画停在末帧永远不动 / 立刻又 ended 死循环。
            #   restart=True 时显式重置，保证"再播一次"从第 0 帧开始。
            if restart or (name == self.current_name and self.pms):
                self.pms = cached
                self._total_frames = len(cached)
                self.frame_idx = -1
                self._frame_elapsed = 0.0
                self._last_tick_t = time.monotonic()
                self._loading_new = False
                if cached:
                    self.cur_pix = cached[0]
            self.on_loaded(name, cached, cat_type, msg)
            return
        self._loading_new = True
        self._load_gen = getattr(self, '_load_gen', 0) + 1
        gen = self._load_gen
        key = (name, 0, 0)
        if key in _INFLIGHT:
            # ★ 同一只宠物重复请求同一动作：**更新已有条目**，不要追加。
            #   追加会让 _first_batch 对同一只调用两次 on_first_batch →
            #   _after_action_switch 的 move 分支重新随机方向 →
            #   位置朝 A、画面朝 B（个体越多越容易撞上，表现为"镜像错误率随数量上升"）。
            lst = _INFLIGHT[key]
            for i, entry in enumerate(lst):
                if entry[0] is self:
                    lst[i] = (self, name, cat_type, msg, gen)
                    return
            lst.append((self, name, cat_type, msg, gen))
            return
        _INFLIGHT[key] = [(self, name, cat_type, msg, gen)]
        path = self.base_dir / name
        # 把显示尺寸交给工作线程：解码后直接缩放，主线程零转换
        loader = LoadThread(name, path, pw, ph)
        # ★ 首批帧一到就开播（不等整段）；done 时再把完整列表补上
        loader.first.connect(lambda n, imgs, total: _first_batch(key, n, imgs, total))
        loader.done.connect(lambda n, imgs: _finish_frames(key, n, imgs))
        loader.error.connect(self.on_error)
        self.loaders.append(loader)
        loader.start()

    def _action_incomplete(self):
        """当前动作是否还没播完（至少一遍）。

        用于"完整播放"保护：非交互触发（工作状态 / 碎碎念 / 召唤散开等）
        必须等当前动作播完，不能中途换掉；只有交互（拖拽 / 点击 / 双击 / 菜单）
        才允许立即打断。
        """
        t0 = getattr(self, '_action_t0', 0.0)
        dur = getattr(self, '_action_dur', 0.0)
        if not t0 or dur <= 0.0:
            return False
        return (time.monotonic() - t0) < dur * 0.97

    def _mark_action_start(self, total):
        """记下本次动作的起始时间与预计时长（由调用方在首批帧就绪时调用）。"""
        self._action_t0 = time.monotonic()
        self._action_dur = max(0.0, (max(1, total) - 1) * self.frame_ms / 1000.0)

    def force_action(self, anim_name, msg="", interrupt=False):
        """切动作。

        interrupt=False（默认）：当前动作没播完就**排队**，等播完再切 —— 绝不中途打断。
        interrupt=True：交互触发（拖拽/点击/双击/菜单/改大小），立即打断并开播。
        """
        if not interrupt and self._action_incomplete():
            # 后到覆盖先到，只留最新一条
            self._queued_action = (anim_name, msg)
            return
        self._queued_action = None
        # 同样不停 timer：新动作就绪前，旧动画继续播
        self._last_action_switch_t = time.monotonic()
        self._request_frames(anim_name, 'event', msg, restart=True)

    def on_error(self, name, msg):
        self.load_next_action()

    def _place_on_first_load(self):
        """首次加载：定窗口尺寸 + 定位 + show。

        流式加载后 on_loaded 会走 upgrading 分支直接 return，首次的 show() 就永远不执行，
        表现为"启动后桌宠默认隐藏"。所以 on_first_batch 必须也调它。
        """
        if not self.is_first_load:
            return
        self.is_first_load = False
        if not self.pms:
            return
        w, h = self.pms[0].width(), self.pms[0].height()
        if w == 0 or h == 0:
            return
        win_w = max(1, int(FRAME_W * self.scale))
        win_h = max(1, int(FRAME_H * self.scale))
        if self.width() != win_w or self.height() != win_h:
            self.setFixedSize(win_w, win_h)
        scr = _scr()
        if getattr(self, 'spawn_random', False) or self.spawn_idx > 0:
            # 多开**全部叠在屏幕正中**（刻意设计）：缓冲期内 overlap 破表、预输入记下意图，
            # 2 秒后一起散开 → 起手就是四散奔逃。人工错开落点反而削弱爆发。
            self.move((scr.left() + scr.right() - self.width()) // 2,
                      (scr.top() + scr.bottom() - self.height()) // 2)
        else:
            self.move(scr.right() - win_w - 40, scr.bottom() - win_h - 20)
        self.show()

    def on_first_batch(self, name, images, total, cat_type='roam', msg=''):
        """首批帧就绪：立刻切到新动作开播，剩余帧由 on_loaded 补齐。

        片段切换时做 100ms 淡化（照搬大肥鱼 crossfade_duration）：
        旧帧**不透明垫底**、新帧按 alpha 淡入 —— 不会出现空白帧，也不会重影。
        注意必须在 current_name 被覆盖**之前**记下旧名字，否则淡化判断永远失败。
        """
        self._move_loading = False
        if not images:
            return
        # ★ 幂等：同一个动作 + 同一个加载代次只处理一次。
        #   重复处理会重新随机移动目标点，导致"位置朝一边、画面朝另一边"。
        _tag = (name, getattr(self, '_load_gen', 0))
        if getattr(self, '_last_first_tag', None) == _tag:
            return
        self._last_first_tag = _tag
        try:
            prev_pix = self.cur_pix
            prev_name = self.current_name
            self.current_name = name
            self.pms = images
            self._total_frames = total or len(images)
            self._loading_new = True   # 仍在补齐：播到末帧时用循环兜底
            self._scale_cache = {}
            self._scale_cache_order = []
            self.cur_pix = self.pms[0]
            self.frame_idx = -1
            # ★ 记录本动作的时长：作为"完整播放"保护的依据
            self._mark_action_start(self._total_frames)
            # 重置累积时间（不用 clock.restart，避免绝对时间漂移）
            self._frame_elapsed = 0.0
            self._last_tick_t = time.monotonic()
            # 片段切换淡化（★ 工作动画之间不淡化：它们属于同一场工作，
            #   来回切会变成整段时间都在闪，观感是持续抽搐）
            _both_ws = (prev_name in WS_WORK_ANIMS and name in WS_WORK_ANIMS)
            dur = None if (self.drag_on or _both_ws) else crossfade_duration(prev_name, name)
            if dur is not None and prev_pix is not None and prev_name:
                self._fade_from = prev_pix
                self._fade_t0 = time.monotonic()
                self._fade_dur = dur
            else:
                self._fade_from = None
                self._fade_dur = 0.0
            self._place_on_first_load()
            self._after_action_switch(name, cat_type)
            # ★ 首批就绪时就显示消息气泡：不能等 on_loaded（它可能走 upgrading 分支提前返回）
            if msg:
                self.bubble.show_message(getattr(self, 'pet_label', "大肥鱼"), msg, 12000)
            self.update()
        except Exception:
            pass

    def on_loaded(self, name, qimages, cat_type, msg=""):
        self._loading_new = False
        self._move_loading = False
        if not qimages:
            return self.load_next_action()
        try:
            self.current_name = name
            # ★ 帧列表进 (动作, 尺寸) 缓存，并**预缩放到显示尺寸**：
            #   · 内存：原来每只各持一整份 640×360 原图（222MB/只），30 只 6.6GB。
            #   · CPU：原来 paintEvent 每帧 SmoothPixmapTransform 重采样，30 只 24fps = 720 次/秒。
            #   共享的只是帧数据，动作选择仍然各自随机。
            pw = max(1, int(FRAME_W * self.scale))
            ph = max(1, int(FRAME_H * self.scale))
            # ★ 只要还是同一个动作，就**绝不重置播放进度**：
            #   补帧（首批 60 → 全量 241）、缓存命中重播、set_scale 重播都算这一类。
            #   原先多了个 `len(qimages) > len(self.pms)` 条件，同动作重播时判定失败，
            #   于是走完整初始化把 cur_pix 拍回第 0 帧、_frame_elapsed 归零 ——
            #   实测帧间隔会掉到 3.7ms（正常应 >=27ms），视觉上就是抽搐。
            upgrading = (name == self.current_name and bool(self.pms))
            # ★ 缓存键不再带 (pw, ph)：帧现在是**原始 640×360**，
            #   与显示尺寸无关，所有 scale 共享同一份原图。
            #   这样多开不同大小时也能复用，且 set_scale 不再触发重解码。
            self.pms = frame_cache.prepare(name, 0, 0, qimages)
            self._total_frames = len(self.pms)
            self._scale_cache = {}
            self._scale_cache_order = []
            if upgrading:
                # ★ 补帧到达：只补充帧数据，**不碰播放进度**。
                #   原来这里 `self.cur_pix = self.pms[0]` 会把播到一半的动画拽回第 0 帧，
                #   正是"抽搐 / 播个开头被切"的直接来源。
                #   时长要按补全后的帧数更新（首批只有 60 帧，算出来的时长偏短）。
                self._action_dur = max(
                    0.0, (max(1, len(self.pms)) - 1) * self.frame_ms / 1000.0)
                if 0 <= self.frame_idx < len(self.pms):
                    self.cur_pix = self.pms[self.frame_idx]
                elif self.pms:
                    self.cur_pix = self.pms[0]
                self.update()
                return
            # ★ 片段切换淡化：记下旧片段当前帧作为垫底
            prev_pix = self.cur_pix
            prev_name = self.current_name
            self.cur_pix = self.pms[0]
            self._frame_elapsed = 0.0
            self._last_tick_t = time.monotonic()
            self.frame_idx = -1
            _both_ws = (prev_name in WS_WORK_ANIMS and name in WS_WORK_ANIMS)
            dur = None if (self.drag_on or _both_ws) else crossfade_duration(prev_name, name)
            if dur is not None and prev_pix is not None and prev_name != name:
                self._fade_from = prev_pix
                self._fade_t0 = time.monotonic()
                self._fade_dur = dur
            else:
                self._fade_from = None
                self._fade_dur = 0.0
            self.update()
        except Exception:
            return self.load_next_action()
        w, h = self.pms[0].width(), self.pms[0].height()
        if w == 0 or h == 0:
            return self.load_next_action()
        win_w = max(1, int(FRAME_W * self.scale))
        win_h = max(1, int(FRAME_H * self.scale))
        if self.width() != win_w or self.height() != win_h:
            self.setFixedSize(win_w, win_h)
        self._place_on_first_load()
        self.frame_idx = -1
        self._frame_elapsed = 0.0
        self._last_tick_t = time.monotonic()
        self._after_action_switch(name, cat_type)
        self.tick()
        if msg:
            # 12 秒：覆盖一整个动作（10 秒），避免"动画还在播、气泡已经没了"
            self.bubble.show_message(getattr(self, 'pet_label', "大肥鱼"), msg, 12000)

    def _after_action_switch(self, name, cat_type):
        """动作切换后的统一处理：是否位移、起点、目标点、朝向。

        ★ 流式加载下切换动作走的是 on_first_batch，而 on_loaded 会因 upgrading
        直接 return。如果这段逻辑只写在 on_loaded 里就永远不执行 ——
        `is_moving_action` 恒为 False，移动作不位移、朝向不设置，
        表现为"移动方向反了 / 走得不对"。
        """
        if cat_type == 'roam':
            self.is_moving_action = True
            self.move_start_x = self.x()
            self.move_start_y = self.y()
            # ★ 朝向按**实际位移方向**判定：_facing_dir 来自 steer_target 的 new_dir，
            #   与最终 move_target_x 的符号可能不一致（合力/边界修正会改目标点），
            #   那样就会出现"向右走却朝左"。
            self._apply_facing((self.move_target_x - self.move_start_x) >= 0)
        else:
            # ★ 漫游开关（用户 2026-10-30）：move 类动作照播动画，但**不位移** ——
            #   表现为"原地跑步"。动作池不动（仍会抽到它），只是走路这件事被取消。
            #   朝向照旧随机，否则会永远朝着同一边原地跑，看着像卡住了。
            _is_move = (cat_type == 'move') and not self.is_roaming
            self.is_moving_action = _is_move and roam_enabled()
            if _is_move and not self.is_moving_action:
                self._apply_facing(random.random() > 0.5)
            if self.is_moving_action:
                self.move_start_x = self.x()
                self.move_start_y = self.y()
                params = self.move_params.get(name, {})
                max_d = params.get('maxDist', 240)
                min_d = params.get('minDist', 60)
                dist = random.randint(min_d, max_d)
                if random.random() > 0.5:
                    self.move_target_x = self.move_start_x + dist
                    self._apply_facing(True)
                else:
                    self.move_target_x = self.move_start_x - dist
                    self._apply_facing(False)
                self.move_target_y = self.move_start_y
                # 与位移方向再对齐一次（dist 可能被边界裁剪）
                self._apply_facing((self.move_target_x - self.move_start_x) >= 0)
                # ★ 单体模式硬碰撞：目标点先过一遍推箱子，算出真正能到的位置。
                #   这一步只改"目标点"，不改动画；被撞者在这时就被顶开了。
                if self.collide_participates():
                    try:
                        cx0, cy0, _, _ = self.body_aabb()
                        s_ = self.scale
                        bx_, by_, bw_, bh_ = BODY_BOX
                        tx_ = self.move_target_x + (bx_ + bw_ / 2.0) * s_
                        ty_ = self.move_target_y + (by_ + bh_ / 2.0) * s_
                        ncx, ncy = self._resolve_move_with_collision(cx0, cy0, tx_, ty_)
                        self.move_target_x = ncx - (bx_ + bw_ / 2.0) * s_
                        self.move_target_y = ncy - (by_ + bh_ / 2.0) * s_
                    except Exception:
                        pass

    def advance(self, elapsed_ms):
        """照搬大肥鱼的 animation_model.advance。

        返回 'ended' 表示这一段播完了（调用方决定接什么），否则返回 None。
        关键点：
          · `divmod(frame_elapsed_ms + elapsed_ms, frame_ms)` → 走几帧 + 余数
          · 余数保留（不是清零），所以长跑不会累积漂移
          · 循环片段用取模，非循环停在末帧
        """
        if elapsed_ms < 0:
            return None
        n = max(len(self.pms), getattr(self, '_total_frames', 0) or 0)
        if n <= 0 or self.frame_ms <= 0:
            return None

        steps, self._frame_elapsed = divmod(self._frame_elapsed + elapsed_ms, self.frame_ms)
        steps = int(steps)
        next_frame = self.frame_idx + steps

        # ★ 拖拽三段：把播放范围限制在 [r0, r1) 内
        rng = getattr(self, '_drag_range', None)
        # ★ 防死循环：帧索引还没就位（-1 或超出）时，先归位到 0，不报告 ended
        if self.frame_idx < 0 or self.frame_idx >= n:
            self.frame_idx = 0
            self._frame_elapsed = 0.0
            return None

        if rng is not None:
            r0, r1 = rng
            r0 = max(0, min(int(r0), n - 1))
            r1 = max(r0 + 1, min(int(r1), n))
            if self.frame_idx < r0 or self.frame_idx >= r1:
                self.frame_idx = r0
                return None
            steps_in = self.frame_idx - r0 + steps
            span = max(1, r1 - r0)
            # 段落是否要循环：拖拽中(lift/hover)或物理飞行中 → 循环；否则播完结束
            seg_loop = self.drag_on or self.phys_timer.isActive()
            if not seg_loop:
                if steps_in >= span:
                    # 播到本段末尾（含素材末帧）→ 停住并报告结束
                    self.frame_idx = r1 - 1
                    self._frame_elapsed = 0.0
                    return "ended"
                self.frame_idx = r0 + steps_in
                return None
            self.frame_idx = r0 + (steps_in % span)
            return None

        # 抛物线飞行期间：动画循环播放，不打断（物理 tick 负责位置）
        looping = self._loading_new or self.drag_on or self.phys_timer.isActive()
        if next_frame < n:
            self.frame_idx = next_frame
            return None
        if looping:
            self.frame_idx = next_frame % n
            return None
        # 非循环且播完：停在最后一帧，余数清零，交给调用方接下一个动作
        self.frame_idx = n - 1
        self._frame_elapsed = 0.0
        return "ended"

    def tick(self):
        """全局定时器每 ~14ms 调一次：只负责推进模型 + 同步渲染。

        ★ 倾斜 / 拖拽 Q 弹的推进放在外层 finally：tick 内部有多条提前 return
          路径（动作播完 / 加载中 / 拖拽中），形变不能跟着停。
        ★ 性能：不参与倾斜的宠物（群体子体、摇晃关闭）直接跳过整段，
          不产生任何额外调用开销。
        """
        try:
            self._tick_body()
        finally:
            # 只有「可能倾斜」的宠物才走这段（群体子体/关闭开关时零开销）
            try:
                if self.tilt is not None and self.tilt_enabled():
                    if self._tick_tilt():
                        self.update()
                elif self.tilt is not None and self.tilt.active:
                    # 从"可倾斜"变成"不可倾斜"（如切到群体子体）：归零一次
                    self.tilt.reset()
                    self.update()
            except Exception:
                pass
            # Q 弹：仅在有活动形变时才检查（空闲时 active_at 也要算一次时间，省掉）
            try:
                if self.jelly is not None and self.jelly.drag.s > 1e-4:
                    if self.jelly.active_at(time.monotonic()):
                        self.update()
                elif self.jelly is not None and (
                        self.jelly.settle.running or self.jelly.pop.running):
                    if self.jelly.active_at(time.monotonic()):
                        self.update()
            except Exception:
                pass

    def _tick_body(self):
        if not self.pms:
            return

        # 与大肥鱼 helper._tick 同款：增量时间
        now = time.monotonic()
        if not self._last_tick_t:
            self._last_tick_t = now
            return
        elapsed_ms = (now - self._last_tick_t) * 1000.0
        self._last_tick_t = now
        if elapsed_ms < 0:
            elapsed_ms = 0.0
        if elapsed_ms > 100.0:
            elapsed_ms = 100.0     # 卡顿保护：单次最多推进 100ms

        prev_idx = self.frame_idx
        result = self.advance(elapsed_ms)

        # ★ 拖拽三段推进：lift（0→42）播完 → 进入 hover（42→200 循环）
        if (self.drag_on and getattr(self, '_drag_phase', None) == 'lift'
                and self.frame_idx >= DRAG_LIFT_END - 1):
            self._drag_phase = 'hover'
            self._drag_range = (DRAG_LIFT_END, DRAG_HOVER_END)

        if result == "ended":
            # ★ 防死循环：如果这次 ended 距离上次切换动作不到 50ms，
            #   说明动作没真正播起来（加载/缓存同步返回），跳过本次切换。
            _now_e = time.monotonic()
            if _now_e - getattr(self, '_last_action_switch_t', 0.0) < 0.05:
                self.frame_idx = 0
                self._frame_elapsed = 0.0
                return
            # ★ 落下段播完：清掉范围限制，交回动作链
            self._drag_range = None
            self._drag_phase = None
            # 播完：turn 翻转朝向
            if self.current_name in (self.pools.get('turn') or []):
                self._facing_dir = -1 if getattr(self, '_facing_dir', 1) >= 0 else 1
                self._apply_facing(self._facing_dir >= 0)
            if self.phys_timer.isActive():
                return
            # ★ 最高优先：非交互触发排队下来的动作（碎碎念 / 召唤散开等），
            #   它们被"完整播放"保护挡在了队列里。
            queued = getattr(self, '_queued_action', None)
            if queued is not None:
                self._queued_action = None
                name, msg = queued
                if name != self.current_name or msg:
                    self.force_action(name, msg, interrupt=True)
                    return
            # ★ 待续的"下一个动作"（load_next_action 被保护挡下时记的）
            if getattr(self, '_pending_next', False):
                self._pending_next = False
                self.load_next_action()
                return
            # ★ 优先播排队中的工作状态动画（当前动画已完整播完）
            pending = self._pending_ws
            if pending is not None:
                self._pending_ws = None
                name, msg = pending
                if name != self.current_name and (self.base_dir / name).exists():
                    self.force_action(name, msg)
                    return
            # ★ 解锁兜底（两条）：
            #   1) 空闲超时：90 秒没有任何工作状态事件 → 空闲（防 DSH 断连）
            #   2) turn 卡死：同一个 turn 持续 300 秒仍是「忙碌」→ 强制空闲
            #      （DSH 会一直发 tool/call 刷新事件，空闲超时触发不了，
            #        但真正的 turn/end 可能因插件没转发而永远不来）
            WS_IDLE_TIMEOUT = 90.0
            WS_TURN_MAX = 300.0
            now_ws = time.monotonic()
            _ws = getattr(self, '_ws_state', None)
            if _ws is not None:
                _last = getattr(self, '_ws_last_at', 0) or 0
                _turn_t0 = getattr(self, '_ws_turn_t0', 0) or 0
                if (_last and now_ws - _last > WS_IDLE_TIMEOUT) or \
                        (_turn_t0 and now_ws - _turn_t0 > WS_TURN_MAX):
                    self._ws_state = None
                    self._ws_anim = ""
                    self._pending_ws = None
                    self._ws_turn_t0 = 0.0
            # ★ 仍在工作状态 → 在「写代码 / 忙碌点按」之间轮播，**不进随机链**
            if getattr(self, '_ws_state', None) is not None:
                avail = [a for a in WS_WORK_ANIMS if (self.base_dir / a).exists()]
                if avail:
                    cur = self.current_name
                    # 交替：优先选不是当前这个的
                    nxt = [a for a in avail if a != cur]
                    pick = random.choice(nxt) if nxt else random.choice(avail)
                    self._ws_anim = pick
                    self.force_action(pick)
                    return
            self.load_next_action()
            return

        if self.frame_idx != prev_idx:
            n = max(len(self.pms), getattr(self, '_total_frames', 0) or 0)
            if 0 <= self.frame_idx < len(self.pms):
                self.cur_pix = self.pms[self.frame_idx]
            self.update()

            # 移动类动作：位置按 roam_ratio 映射（照搬大肥鱼：起步/收尾冻结）
            # 拖拽/飞行期间不接管位置，否则会和鼠标跟随打架
            if self.is_moving_action and not self.drag_on and not self.phys_timer.isActive():
                progress = self.frame_idx / float(n - 1) if n > 1 else 1.0
                params = self.move_params.get(self.current_name, {}) or {}
                lead = float(params.get('leadSec', 0) or 0)
                tail = float(params.get('tailSec', 0) or 0)
                dur = (n - 1) * self.frame_ms / 1000.0 if n > 1 else 1.0
                win_start = min(0.45, lead / dur) if dur > 0 else 0.0
                win_end = max(0.55, 1.0 - (tail / dur if dur > 0 else 0.0))
                ease = roam_ratio(progress, (win_start, win_end))
                cur_x = self.move_start_x + (self.move_target_x - self.move_start_x) * ease
                cur_y = self.move_start_y + (self.move_target_y - self.move_start_y) * ease
                _s = self.scale
                _bx, _by, _bw, _bh = BODY_BOX
                scr = _screen_at(self.x() + (_bx + _bw / 2.0) * _s,
                                 self.y() + (_by + _bh / 2.0) * _s)
                body_w = _bw * _s
                body_h = _bh * _s
                allow_x = body_w * WALL_COVER
                allow_y = body_h * WALL_COVER
                min_x = scr.left() - allow_x - _bx * _s
                max_x = scr.right() + allow_x - _bx * _s - body_w
                min_y = scr.top() - TOP_SHIFT - allow_y - _by * _s
                max_y = scr.bottom() + BOTTOM_EXTEND - TOP_SHIFT + allow_y - _by * _s - body_h
                cx2 = max(int(min_x), min(int(cur_x), int(max_x)))
                cy2 = max(int(min_y), min(int(cur_y), int(max_y)))
                # ★ 单体模式硬碰撞：逐帧夹紧，保证插值中途也不会穿过别人。
                #   被撞者同样被顶着走（只改位置，动画照旧）。
                if self.collide_participates():
                    try:
                        _s2 = self.scale
                        _bx2, _by2, _bw2, _bh2 = BODY_BOX
                        cxa, cya, _, _ = self.body_aabb()
                        ncx2, ncy2 = self._resolve_move_with_collision(
                            cxa, cya,
                            cx2 + (_bx2 + _bw2 / 2.0) * _s2,
                            cy2 + (_by2 + _bh2 / 2.0) * _s2)
                        cx2 = int(round(ncx2 - (_bx2 + _bw2 / 2.0) * _s2))
                        cy2 = int(round(ncy2 - (_by2 + _bh2 / 2.0) * _s2))
                    except Exception:
                        pass
                self.move(cx2, cy2)
                if self.bubble.isVisible():
                    self.bubble.update_position()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not self.is_in_body(e.position().toPoint()):
                e.ignore()
                return
            # ★ 拖拽最高优先级：立刻停掉物理飞行，并强制打断当前动作。
            self.phys_timer.stop()
            self.phys_vx = self.phys_vy = 0.0
            self.drag_on = True
            # ★ 必须清掉移动状态：否则 tick 里的移动插值仍会 self.move(移动目标)，
            #   与 mouseMoveEvent 的 self.move(鼠标位置) 每帧互相覆盖 →
            #   窗口在"被抓住的位置"和"原移动目标"之间闪烁。
            self.is_moving_action = False
            self.is_roaming = False
            self.drag_trail = []
            # 拖拽最高优先级：排队中的工作状态动画作废（只有主体会有 pending）
            if getattr(self, 'is_main', False):
                self._pending_ws = None
            self.press_pos = e.globalPosition().toPoint()
            self.setCursor(self.cursor_grabbing)
            self.grabMouse()
            self.drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            # ★ 拖拽 Q 弹：重置拉伸状态，并从这一下开始采样
            try:
                self.jelly.reset_drag()
                self.jelly.pop.stop()
                _t = time.monotonic()
                _gp = e.globalPosition()
                self.jelly.drag.update(float(_gp.x()), float(_gp.y()), _t * 1000.0)
            except Exception:
                pass
            # ★ 三段化：先播「站立→悬空」(0→42)，播完自动在悬空段 (42→200) 循环
            drag_anim = "被鼠标拖拽悬空反馈"
            if (self.base_dir / drag_anim).exists():
                self._drag_phase = 'lift'          # lift → hover → fall
                self._drag_range = (0, DRAG_LIFT_END)
                self.force_action(drag_anim, interrupt=True)
                self.drag_lift_done_at = None
            e.accept()

    def mouseMoveEvent(self, e):
        if self.drag_on:
            new_pos = e.globalPosition().toPoint()
            self.move(new_pos - self.drag_off)
            # 采样轨迹（松手用它估速度）
            now_ms = time.monotonic() * 1000.0
            self.drag_trail.append((now_ms, float(self.x()), float(self.y())))
            if len(self.drag_trail) > 60:
                self.drag_trail.pop(0)
            # ★ 拖拽 Q 弹：按鼠标移动速度更新拉伸
            try:
                gp = e.globalPosition()
                if self.jelly.drag.update(float(gp.x()), float(gp.y()), now_ms):
                    self.update()
            except Exception:
                pass
            if self.bubble.isVisible():
                self.bubble.update_position()
            e.accept()

    def mouseReleaseEvent(self, e):
        if self.drag_on:
            self.drag_on = False
            self.releaseMouse()
            if self.is_in_body(e.position().toPoint()):
                self.setCursor(self.cursor_grab)
            else:
                self.setCursor(Qt.ArrowCursor)
            pp = getattr(self, 'press_pos', None)
            moved = 9999
            if pp is not None:
                gp = e.globalPosition().toPoint()
                moved = abs(gp.x()-pp.x()) + abs(gp.y()-pp.y())
            if moved < 15:
                # ★ 单击（不是拖拽）→ 点击弹跳 fxPop（whale 的 wpPop）
                try:
                    self.jelly.pop.start(time.monotonic())
                except Exception:
                    pass
                # ★ 双击 → 查看余额；单击 → 随机点击回应动作。
                #   时间戳自己判：拖拽里有 grabMouse，Qt 的 mouseDoubleClickEvent
                #   可能被吞掉/重复派发，不如直接比两次松手的时间可靠。
                #   拖拽不会误判 —— 拖拽本身耗时远超 300ms 窗口。
                now = time.monotonic()
                last = getattr(self, '_last_click_t', 0.0)
                gap = now - last
                self._last_click_t = now
                if gap <= DOUBLE_CLICK_MS / 1000.0:
                    self._last_click_t = 0.0     # 用掉这次，避免三击又触发
                    # ★ 双击：查余额 + 把 DSH 窗口提到最前（两者不冲突）
                    #   · 只在 DSH 已运行时才动作（bring_dsh_to_front 内部先看进程，
                    #     没起就返回 False，绝不顺手拉起新进程）
                    #   · 只由主体触发，多开时避免 N 只同时抢前台
                    #   · 提前台是**一次性**的，不加永久 TOPMOST；
                    #     桌宠自己仍是 topmost，所以大肥鱼依旧浮在最上层
                    if getattr(self, 'is_main', False):
                        try:
                            # ★ 变量名不能用 moved：外层 moved 是拖拽位移量，
                            #   在这里覆盖会破坏后续判断（虽然当前分支之后不再读它，
                            #   但同名遮蔽是隐患）。
                            did_move, title = bring_dsh_to_front()
                            if did_move:
                                dsh_log("双击: 已把 DSH 窗口提到最前 %r" % (title,))
                                self._reassert_topmost()
                        except Exception as e:
                            dsh_log("双击: 提 DSH 窗口异常 %r" % (e,))
                    # 没接 DSH 时降级为普通点击动作，不弹"未连接"气泡
                    if self._balance_available():
                        self._request_balance()
                        return
                clicks = self.raw_anims.get('clicks', []) or []
                if clicks:
                    self.force_action(random.choice(clicks), interrupt=True)
                    return
            # ★ 抛掷全模式可触发：群体里的个体也能被甩出去。
            #   但抛掷期间会**临时切到单体**（见 start_throw），静止后再恢复原模式。
            vel = estimate_release_velocity(self.drag_trail, time.monotonic() * 1000.0)
            self.drag_trail = []
            # ★ 拖拽 Q 弹：松手回落 fxSettle（whale 的 wpSettle）。
            #   顺序必须是「先 reset 拉伸、再 settle」——否则 fx 层的拉伸
            #   会和回落动画叠加，观感是"拉长着弹回来"。
            try:
                self.jelly.start_settle(time.monotonic())
            except Exception:
                pass
            if vel:
                # ★ 进入抛物线：**完整播放整段动画**（不截断、不设范围）
                self._drag_range = None
                self._drag_phase = 'throw'
                self.start_throw(vel[0], vel[1])
            else:
                # ★ 轻放：不抛，播「落下段」(200→240)，播完回动作链
                self._drag_range = (DRAG_HOVER_END, DRAG_FALL_END)
                self._drag_phase = 'fall'

    def start_throw(self, vx, vy):
        """松开瞬间给定初速，启动物理 ticker。

        ★ 抛掷期间**临时进入单体模式**：飞行中的重力/反弹与群体涌现互斥
        （涌现会不断改方向，物理轨迹就乱了）。静止后恢复原模式。
        """
        self._mode_before_throw = self.flock_enabled
        self.flock_enabled = False
        vx = max(-MAX_THROW_SPEED, min(MAX_THROW_SPEED, float(vx)))
        vy = max(-MAX_THROW_SPEED, min(MAX_THROW_SPEED, float(vy)))
        self.phys_x = float(self.x())
        self.phys_y = float(self.y())
        self.phys_vx = vx
        self.phys_vy = vy
        self._phys_last_t = time.monotonic()
        self.phys_timer.start(16)
        # 飞行姿态：沿用拖拽悬空动画（与 pet 抛掷期间的表现一致）
        drag_anim = "被鼠标拖拽悬空反馈"
        if (self.base_dir / drag_anim).exists():
            self.force_action(drag_anim, interrupt=True)

    def tick_physics(self):
        """16ms 物理步进：重力 + 边缘弹性反弹 + 落地摩擦；静止后回动作链。"""
        now = time.monotonic()
        dt = now - self._phys_last_t
        self._phys_last_t = now
        if dt > MAX_STEP_DT:
            dt = MAX_STEP_DT
        if dt < 0.001:
            dt = 0.001
        # ★ 多屏：按**身体中心**选屏；边界照搬 pet 的 throwBoundsIn（左右允许出屏 side_allow）
        s = self.scale
        bx, by, bw, bh = BODY_BOX
        cx = self.phys_x + (bx + bw / 2.0) * s
        cy = self.phys_y + (by + bh / 2.0) * s
        scr = _screen_at(cx, cy)
        side = bx * s
        bounds = list(throw_bounds_in(scr, float(self.width()), side))
        # ★ 只在**真屏幕边缘**反弹：外侧还有屏（屏缝）的方向放开，
        #   这样宠物能从一块屏滚到相邻屏，而不是在拼接处被无形墙挡住。
        body_left = self.phys_x + bx * s
        body_right = self.phys_x + (bx + bw) * s
        if _has_screen_at(body_left - 2.0, cy):
            bounds[0] = -1e9      # 左边还有屏 → 不设左墙
        if _has_screen_at(body_right + 2.0, cy):
            bounds[2] = 1e9       # 右边还有屏 → 不设右墙
        if _has_screen_at(cx, self.phys_y - 2.0):
            bounds[1] = -1e9      # 上方还有屏 → 不设顶墙
        if _has_screen_at(cx, self.phys_y + self.height() + 2.0):
            bounds[3] = 1e9       # 下方还有屏 → 不设底墙
        bounds = tuple(bounds)
        # 与 pet 一致：单步积分（dt 已在上面 clamp 到 MAX_STEP_DT）。
        # 落地归零阈值按重力动态取：离散步长下小幅弹跳的不动点约 v ≈ 2g·dt/(1-E)，
        # 固定 40 盖不住（16ms 时不动点约 204），表现为贴地后一直抖。
        settle_vy = max(REST_VY, 8.0 * GRAVITY * dt)
        x, y, vx, vy = update_physics(
            self.phys_x, self.phys_y, self.phys_vx, self.phys_vy,
            float(self.width()), float(self.height()), bounds,
            GRAVITY, RESTITUTION, GROUND_FRICTION, dt, settle_vy,
        )
        self.phys_x, self.phys_y = float(x), float(y)
        self.phys_vx, self.phys_vy = float(vx), float(vy)
        self.move(int(self.phys_x), int(self.phys_y))
        if self.bubble.isVisible():
            self.bubble.update_position()

        # ★ 落地 Q 弹：只在「空中 → 地面」那一帧触发一次（照搬 pet 的 !prevGrounded 判定）。
        #   力度随冲击速度：轻落几乎不压（0.8），重砸压得狠（0.55）。
        grounded = self.phys_y + self.height() >= bounds[3] - 1.0
        if grounded and not self._prev_grounded:
            self._start_squash(self.phys_vy)
        self._prev_grounded = grounded
        # ★ 静止判定照搬 pet 的 atRest（两个条件取或）：
        #   atRest = (y >= maxY-1 && |vy| < 1 && |vx| < REST_VX)
        #         || (bounced && speed < REST_VY && |vy| < 1)
        #   第二式是关键：刚弹跳过、总速度已很小就别再等了。
        on_ground = self.phys_y + self.height() >= bounds[3] - 1.0
        speed = (self.phys_vx ** 2 + self.phys_vy ** 2) ** 0.5
        # 收尾阈值：小幅弹跳在离散积分下有个不动点
        #   v ≈ 2·g·dt / (1 - E) = 2*1400*0.016/0.22 ≈ 204 px/s
        # 它高于 pet 的 REST_VY(40)，照抄会在贴地后一直抖（幅度约 15px）。
        # 这里按重力把"可收尾速度"动态放宽到能盖住该不动点。
        settle_speed = settle_vy
        # 连续贴地帧数：尾段小幅弹跳每帧都贴地（离地 <48ms），3 帧就收；
        # 正常的大幅反弹离地明显，计数会被重置，不受影响。
        if on_ground:
            self._ground_frames += 1
        else:
            self._ground_frames = 0
        rest_a = on_ground and abs(self.phys_vy) < 1.0 and abs(self.phys_vx) < REST_VX
        rest_b = (self.phys_y + self.height() >= bounds[3] - 2.0) and speed < settle_speed and abs(self.phys_vy) < 1.0
        rest_c = on_ground and self._ground_frames >= 3
        if rest_a or rest_b or rest_c:
            self.phys_timer.stop()
            self.phys_vx = self.phys_vy = 0.0
            # ★ 飞行结束：恢复抛掷前的模式
            if self._mode_before_throw is not None:
                self.flock_enabled = self._mode_before_throw
                self._mode_before_throw = None
            self.load_next_action()

    def _start_squash(self, impact_speed):
        """落地挤压：depth 由冲击速度决定，220ms 内压扁再回弹（带回弹过冲）。"""
        try:
            self._squash_depth = landing_squash(impact_speed)
        except Exception:
            self._squash_depth = SQ_SQUASH
        self._squash_t0 = time.monotonic()
        self._squash_timer.start(16)

    def _tick_squash(self):
        u = (time.monotonic() - self._squash_t0) / (SQ_DURATION_MS / 1000.0)
        if u >= 1.0:
            self._squash_timer.stop()
            self._squash_scale = 1.0
        else:
            self._squash_scale = squash_scale(u, self._squash_depth)
        self.update()

    def leaveEvent(self, e):
        if not self.drag_on:
            self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(e)

    def check_hover_and_click_through_global(self, gpos):
        if not self.isVisible():
            return
        # ★ 右键菜单打开期间**绝不切穿透**：光标在菜单上（不在身体框内），
        #   按位置判定会设成穿透，菜单随即失去输入、一弹出就被打断。
        if getattr(self, '_menu_open', False):
            return
        if not hasattr(self, '_hwnd'):
            self._hwnd = int(self.winId())
        if self.drag_on:
            if self.is_click_through:
                set_click_through(self._hwnd, False)
                self.is_click_through = False
            return
        local_pos = self.mapFromGlobal(gpos)
        inside = self.rect().contains(local_pos) and self.is_in_body(local_pos)
        if inside:
            if self.is_click_through:
                set_click_through(self._hwnd, False)
                self.is_click_through = False
            # ★ 每次命中都强制重设光标，不做状态缓存：
            #   系统在窗口激活/穿透切换/操作其它窗口后会把光标重置成默认，
            #   缓存会让手套消失后永远不回来（右键菜单能恢复是因为重建了状态）。
            self.setCursor(self.cursor_grab)
            self._cursor_state = 'grab'
            # ★ 名字气泡：光标旁显示编号/名字（窗口内子控件，绝对可见）
            if getattr(self, 'tip', None) is not None:
                try:
                    if self.tip.text() != self.pet_label:
                        self.tip.setText(self.pet_label)
                        self.tip.adjustSize()
                    tw = self.tip.width()
                    th = self.tip.height()
                    # 光标右下 12px，夹在窗口内（气泡不能被窗口裁掉）
                    tx = min(max(2, local_pos.x() + 12), max(2, self.width() - tw - 2))
                    ty = min(max(2, local_pos.y() + 10), max(2, self.height() - th - 2))
                    self.tip.move(tx, ty)
                    self.tip.show()
                    self.tip.raise_()
                except Exception:
                    pass
        else:
            if not self.is_click_through:
                set_click_through(self._hwnd, True)
                self.is_click_through = True
            if getattr(self, '_cursor_state', None) != 'arrow':
                self.setCursor(Qt.ArrowCursor)
                self._cursor_state = 'arrow'
            if getattr(self, 'tip', None) is not None:
                try:
                    self.tip.hide()
                except Exception:
                    pass

    def check_hover_and_click_through(self):
        if self.drag_on:
            if self.is_click_through:
                set_click_through(int(self.winId()), False)
                self.is_click_through = False
            return
        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        if self.rect().contains(local_pos) and self.is_in_body(local_pos):
            if self.is_click_through:
                set_click_through(int(self.winId()), False)
                self.is_click_through = False
            self.setCursor(self.cursor_grab)
        else:
            if not self.is_click_through:
                set_click_through(int(self.winId()), True)
                self.is_click_through = True
            self.setCursor(Qt.ArrowCursor)

    def poll_state(self):
        if not self.store: return
        if getattr(self, 'spawn_idx', 0) != 0: return
        state = self.store.get_state()
        if state['counter'] != self.last_ws_counter:
            self.last_ws_counter = state['counter']
            ws = state['workStatus']
            if ws is not None:
                anim_name = ""
                msg = ""
                # ★ 工作期间只在「写代码 / 忙碌点按」之间轮播（用户要求）
                avail = [a for a in WS_WORK_ANIMS if (self.base_dir / a).exists()]
                anim_name = random.choice(avail) if avail else ""
                if not anim_name and ws < len(self.ws_anims):
                    anim_name = self.ws_anims[ws]
                if ws < len(self.ws_texts) and len(self.ws_texts[ws]) > 0:
                    msg = random.choice(self.ws_texts[ws])
                if not msg: msg = f"状态已更新为 {ws}"
                # 标题：大肥鱼正在工作
                self.bubble.show_message("大肥鱼正在工作", msg)
                # ★ 记下状态：非 None 时只播工作动画，不插随机动作
                self._ws_state = ws
                self._ws_anim = anim_name
                self._ws_last_at = time.monotonic()
                if not getattr(self, '_ws_turn_t0', 0):
                    self._ws_turn_t0 = time.monotonic()
                if anim_name and (self.base_dir / anim_name).exists():
                    # 不 hard-cut：排队等当前动画播完（后到覆盖先到，只留最新）
                    # 拖拽仍然最高优先级 —— 由 mousePressEvent 直接打断。
                    self._pending_ws = (anim_name, msg)
            else:
                # 状态回到空闲 → 允许随机链
                self._ws_state = None
                self._ws_anim = ""
                self._pending_ws = None
                self._ws_last_at = 0.0
                self._ws_turn_t0 = 0.0

    def closeEvent(self, event):
        # ★ 先在主线程拆掉 QTimer 与解码等待队列，再收线程、关窗口：
        #   否则这些 QObject 要等 GC 回收，而 GC 可能跑在工作线程里 →
        #   "Timers cannot be stopped from another thread" + 0xC0000005。
        _withdraw_inflight(self)
        _dispose_pet_qt(self)
        for ld in list(self.loaders):
            try:
                if ld.isRunning():
                    ld.requestInterruption()   # run() 每帧检查，能立刻退出
                    ld.quit()
                    # ★ 不在这里 wait：50ms 根本不够（QImage 解码一个大动作要 1~2 秒），
                    #   等不到就丢引用会让 QThread 在运行中被 GC →
                    #   "QThread: Destroyed while thread is still running" → 有时直接崩。
                    #   统一交给全局待回收表，由 _reap_threads 在 isFinished 后清理。
                    _PENDING_THREADS.append(ld)
            except Exception:
                pass
        self.loaders = []
        if self in ALL_PETS:
            ALL_PETS.remove(self)
        try: self.flock_timer.stop()
        except Exception: pass
        self.bubble.close()
        super().closeEvent(event)

    def contextMenuEvent(self, e):
        self._menu_open = True
        try:
            return self._show_context_menu(e)
        finally:
            self._menu_open = False

    def _show_context_menu(self, e):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: rgba(255,255,255,0.98); color: #2b2b2b; border: 1px solid rgba(0,0,0,0.12); border-radius: 8px; padding: 4px; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
            QMenu::item { padding: 5px 24px 5px 12px; border-radius: 6px; }
            QMenu::item:selected { background-color: rgba(43,99,255,0.14); }
            QMenu::separator { height: 1px; background: rgba(0,0,0,0.1); margin: 4px 8px; }
        """)
        # DSH 在跑时显示「关闭 DSH」，否则「打开 DSH」
        try:
            dsh_running = is_dsh_running()
        except Exception:
            dsh_running = False
        action_site = menu.addAction("关闭 DSH" if dsh_running else "打开 DSH")
        action_site.triggered.connect(self.toggle_dsh)
        action_balance = menu.addAction("查看余额")
        action_balance.triggered.connect(self.check_balance)
        # ★ 三格：行为 / 多开 / 关闭全部多开
        #   · 行为：群体 ⇄ 单体（点击切换名称）
        #   · 多开：十只 ⇄ 一只（随之切换；点这一格也能切）
        #   · 关闭全部多开：主体点 = 关掉所有多开（保留自己）；子体点 = 关掉其他子体（保留主体）
        others = max(0, sum(1 for _p in ALL_PETS
                            if _p is not self and not getattr(_p, 'is_main', False)))
        is_flock = bool(getattr(self, 'flock_enabled', False))
        action_behavior = menu.addAction("行为：%s" % ("群体" if is_flock else "单体"))
        action_behavior.triggered.connect(self.toggle_behavior)
        # 多开格：**点击就按当前行为模式多开**（群体→十只、单体→一只）。
        # 注意是"追加"，不是切换模式、也不是删除。
        action_spawn = menu.addAction("多开%s" % ("十只" if is_flock else "一只"))
        action_spawn.triggered.connect(self.spawn_by_mode)
        action_close_all = menu.addAction("关闭所有（%d）" % others)
        action_close_all.setEnabled(others > 0)
        action_close_all.triggered.connect(self.close_all_pets)
        # ★ 以下开关统一收进「设置 ▸」（用户 2026-10-30 要求）：
        #   系统级 / 功能级开关全放这里，主菜单只留常用操作。
        #   统一写法：名称：值、点击切换、不用 setCheckable。
        menu.addSeparator()
        settings_menu = menu.addMenu("设置")
        # · 漫游：关掉后移动动作原地播放（不位移），群体 roam 不受影响
        try:
            _ro_on = roam_enabled()
            action_roam = settings_menu.addAction(
                "漫游：%s" % ("开" if _ro_on else "关"))
            action_roam.triggered.connect(self.toggle_roam)
        except Exception:
            pass
        # · 摇晃：光标靠近时身体倾斜
        try:
            _sw_on = tilt_sway_enabled()
            action_sway = settings_menu.addAction(
                "摇晃：%s" % ("开" if _sw_on else "关"))
            action_sway.triggered.connect(self.toggle_sway)
        except Exception:
            pass
        # · 置顶：全局生效（所有个体 + 气泡一起切）
        try:
            action_top = settings_menu.addAction(
                "置顶：%s" % ("开" if topmost_enabled() else "关"))
            action_top.triggered.connect(self.toggle_topmost)
        except Exception:
            pass
        # · 开机自启：状态以**注册表**为准（不另存 settings.json），
        #   用户在任务管理器里禁用后菜单能如实反映。只允许主体操作。
        try:
            from src.host import autostart
            act_auto = settings_menu.addAction(autostart.menu_label(
                Path(self.base_dir).parent))
            act_auto.setEnabled(bool(getattr(self, 'is_main', False)))
            act_auto.triggered.connect(self.toggle_autostart)
        except Exception:
            pass            # 可选功能，异常不该影响菜单弹出
        # ★ 「技能 ▸」单独一栏：目前只有拷问一个，以后加技能往里塞。
        #   状态以磁盘为准（~/.dsh/AGENTS.md 里有没有受管区块），每次开菜单现场探测。
        skills_menu = menu.addMenu("技能")
        try:
            from src.host import skill_toggle
            act_skill = skills_menu.addAction(skill_toggle.menu_label())
            act_skill.setEnabled(bool(skill_toggle.is_installed()
                                      and getattr(self, 'is_main', False)))
            act_skill.triggered.connect(self.toggle_grill_me)
        except Exception:
            # 开关是可选功能，任何异常都不该影响菜单弹出；整栏空着就藏掉
            skills_menu.menuAction().setVisible(False)
        size_menu = menu.addMenu("大小")
        size_opts = [("迷你", 0.3), ("小", 0.45), ("中", 0.6), ("标准", 0.75), ("大", 1.0), ("特大", 1.4), ("自适应", 0.0)]
        from PySide6.QtGui import QActionGroup
        sg = QActionGroup(size_menu)
        sg.setExclusive(True)
        for lbl, scv in size_opts:
            a = size_menu.addAction(lbl)
            a.setCheckable(True)
            if (scv == 0.0 and getattr(self, '_auto_scale', False)) or (scv > 0 and abs(self.scale - scv) < 0.01):
                a.setChecked(True)
            sg.addAction(a)
            a.triggered.connect(lambda checked=False, s=scv: self.set_scale(s))
        menu.addSeparator()
        action_menu = menu.addMenu("动作")
        groups = self.build_menu_tree()
        for group_name, pool in groups:
            cat_menu = action_menu.addMenu(group_name)
            for anim_name in pool:
                act = cat_menu.addAction(anim_name)
                act.triggered.connect(lambda checked=False, name=anim_name: self.force_action(name, interrupt=True))
        menu.addSeparator()
        # 主体：退出整个程序；子体：只关自己（不动主体和其他多开）
        action_quit = menu.addAction("退出桌宠" if getattr(self, 'is_main', False) else "关闭本只")
        action_quit.triggered.connect(QApplication.quit if getattr(self, 'is_main', False) else self.close_self)
        menu.exec(e.globalPos())

    def toggle_grill_me(self):
        """切换「拷问模式」并在气泡里反馈。

        ★ 2026-10-29 修正：原来只挪技能目录，用户反馈"从来没生效过，除非特意去说"。
          根因：技能只是**说明书**，DSH 不会因为目录存在就主动调用它 ——
          模型只有语义匹配时才去读。要"开关即模式"，必须同时写 `~/.dsh/AGENTS.md`
          的受管区块（每轮注入）。两处一起动见 `skill_toggle.set_enabled()`。
        """
        try:
            from src.host import skill_toggle
            if not skill_toggle.is_installed():
                self.bubble.show_message("未安装 grill-me",
                                         "请先放到 ~/.dsh/skills/grill-me")
                return
            before = skill_toggle.is_mode_active()
            on = skill_toggle.toggle()
            if on == before:                    # 状态没变 = 写入失败（权限/标记残缺）
                self.bubble.show_message(
                    "切换失败",
                    "无法写入 %s，请检查权限或标记是否残缺"
                    % skill_toggle.agents_path())
            elif on:
                self.bubble.show_message(
                    "拷问模式已开启",
                    "之后我会先逐条问清方案再动手，直到你关掉它")
            else:
                self.bubble.show_message(
                    "拷问模式已关闭",
                    "已恢复普通模式，不会再自动追问")
        except Exception as e:
            self.bubble.show_message("技能开关不可用", str(e))

    def toggle_dsh(self):
        """DSH 在运行 → 彻底结束进程；没运行 → 启动。"""
        if is_dsh_running():
            self.close_dsh()
        else:
            self.open_dsh()

    def close_dsh(self):
        """关闭 DSH：直接强杀所有进程，然后异步确认退出并清残留锁。

        ★ 为什么不再"优雅关闭"（2026-10-26 修正）：
          真正导致"菜单关不掉/打不开"的根因是启动时继承了
          `ELECTRON_RUN_AS_NODE=1`（见 open_dsh），与关闭方式无关。
          优雅关闭在这里实测 2 秒内基本无效（DSH 的子进程不响应 WM_CLOSE），
          反而拖慢体验，因此恢复为直接 `taskkill /F /T`。

        保留的清理逻辑：强杀后 lockfile 可能残留（实测句柄释放滞后），
        退出后清一并清掉，避免下次启动误判"已有实例"。
        """
        try:
            pids = _iter_dsh_pids()
            dsh_log("close_dsh: 目标进程 %r" % (pids,))
            n = kill_dsh()
            dsh_log("close_dsh: 已发出强杀，进程数=%s" % n)
        except Exception as e:
            dsh_log("close_dsh: 异常 %r" % (e,))
            self.bubble.show_message("关闭 DSH 失败", str(e))
            return

        if n <= 0:
            self.bubble.show_message("关闭 DSH", "没找到运行中的 DSH")
            return

        self.bubble.show_message("已关闭 DSH", "结束 %d 个进程" % n)

        # 异步确认退出 → 清残留锁（不阻塞 UI）
        self._wait_dsh_gone_tries = 0
        QTimer.singleShot(150, self._poll_dsh_gone)

    def _poll_dsh_gone(self):
        """轮询 DSH 是否已完全退出；退出后清理残留锁。"""
        if getattr(self, '_qt_disposed', False):
            return
        try:
            self._wait_dsh_gone_tries = getattr(self, '_wait_dsh_gone_tries', 0) + 1
            if is_dsh_running():
                if self._wait_dsh_gone_tries < 40:      # 40 × 150ms ≈ 6s
                    QTimer.singleShot(150, self._poll_dsh_gone)
                else:
                    dsh_log("close_dsh: 等待 6 秒后仍有进程存活")
                return

            dsh_log("close_dsh: 进程已全部退出（等了 %d × 150ms）"
                    % self._wait_dsh_gone_tries)
            self._clear_dsh_locks()
        except Exception as e:
            dsh_log("close_dsh: 轮询异常 %r" % (e,))

    def _clear_dsh_locks(self):
        """删除 DSH 残留的单实例锁文件（仅在确认进程已退出后调用）。"""
        import os
        removed = []
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return removed
        base = os.path.join(appdata, "@deepseek-ai", "dsh-desktop")
        for name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = os.path.join(base, name)
            try:
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(name)
            except Exception as e:
                dsh_log("clear_locks: 删除 %s 失败 %r" % (name, e))
        if removed:
            dsh_log("clear_locks: 已清理 %r" % (removed,))
        return removed

    def _resolve_dsh_exe(self):
        """解析 DSH exe 路径 —— **只认用户显式绑定**，不做任何自动探测。

        这是"初始状态"语义：大肥鱼并不知道这台电脑装没装 DSH。
          · `settings.json` 里有 `dsh_exe` 且文件有效 → 用它
          · 否则 → 返回 (None, None)，由调用方弹文件选择框让用户绑定

        返回 (exe, base) 或 (None, None)。
        """
        import os
        bound = None
        try:
            from src.host import user_settings
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            bound = user_settings.load(root).get("dsh_exe")
        except Exception:
            bound = None

        if isinstance(bound, str) and bound and os.path.isfile(bound):
            return bound, os.path.dirname(bound)
        return None, None

    def _bind_dsh_exe(self):
        """弹文件选择框让用户指定 DSH exe，成功则写入 settings.json 并返回路径。"""
        import os
        from PySide6.QtWidgets import QFileDialog

        try:
            from src.host import user_settings
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        except Exception:
            return None

        start_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DeepSeek Harness")
        if not os.path.isdir(start_dir):
            start_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")

        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 DeepSeek Harness.exe",
            start_dir,
            "可执行文件 (*.exe);;所有文件 (*.*)",
        )
        if not path:
            self.bubble.show_message("未绑定 DSH", "已取消选择")
            return None

        if not os.path.isfile(path):
            self.bubble.show_message("绑定失败", "文件不存在：%s" % path)
            return None

        user_settings.save(root, dsh_exe=path)
        return path

    def open_dsh(self):
        """打开 DSH：有绑定就直接启动；没有就弹文件选择框让你绑定。

        初始状态下大肥鱼并不知道 DSH 装在哪，所以第一次点这个菜单
        会弹出文件选择框，选中的 exe 会写入 `settings.json` 的 `dsh_exe`，
        下次直接启动，不再询问。
        """
        import subprocess
        import os

        exe, base = self._resolve_dsh_exe()
        dsh_log("open_dsh: 解析绑定 -> %r (base=%r)" % (exe, base))

        if not exe:
            dsh_log("open_dsh: 未绑定，弹文件选择框")
            # 未绑定 → 先弹选择框
            self.bubble.show_message("未绑定 DSH", "请选择 DeepSeek Harness.exe")
            exe = self._bind_dsh_exe()
            if not exe:
                dsh_log("open_dsh: 用户取消绑定")
                return
            base = os.path.dirname(exe)
            dsh_log("open_dsh: 绑定成功 -> %r" % exe)

        # ★ 启动前清理残留单实例锁。
        #   实测：DSH 已经完全退出，但 %APPDATA%\@deepseek-ai\dsh-desktop\lockfile
        #   还在（taskkill /F 强杀来不及清理），新实例启动时读到锁 → 误判
        #   "已有实例" → Electron 直接 quit()（退出码 0），表现就是点了没反应。
        #   手动双击桌面快捷方式却能启动，因为系统启动路径会先做清理。
        if not is_dsh_running():
            self._clear_dsh_locks()

        # ★★ 根因修复（2026-10-26）：剔除 ELECTRON_* 环境变量
        #
        # 桌宠若由 DSH 插件拉起，会继承 `ELECTRON_RUN_AS_NODE=1`。
        # 该变量让 Electron 以**纯 Node 模式**运行（不创建 GUI 窗口），
        # 进程随即退出 —— **退出码 0、stdout/stderr 全空**，与实测完全吻合。
        #
        # 手动双击能启动，是因为 explorer 的环境里没有这个变量。
        # 这也解释了"更新前正常"：旧版 DSH 不响应这个变量。
        env = dict(os.environ)
        removed_env = [k for k in list(env) if k.upper().startswith("ELECTRON")]
        for k in removed_env:
            env.pop(k, None)
        if removed_env:
            dsh_log("open_dsh: 已剔除环境变量 %r" % (removed_env,))

        try:
            proc = subprocess.Popen([exe], cwd=base, env=env)
            dsh_log("open_dsh: Popen 成功 pid=%s" % proc.pid)
        except Exception as e:
            dsh_log("open_dsh: Popen 异常 %r" % (e,))
            self.bubble.show_message("打开失败", str(e))
            return

        # ★ 异步校验：DSH 是 Electron 应用，拿不到单实例锁会 quit()（退出码 0），
        #   Popen 本身不会报错。这里用 QTimer 延迟 3 秒检查，**绝不用 sleep 阻塞**。
        QTimer.singleShot(3000, lambda: self._verify_dsh_started(proc))

    def _verify_dsh_started(self, proc):
        """启动后 3 秒的存活校验。进程已退出则清锁重试一次，仍失败才报错。"""
        import subprocess

        if getattr(self, '_qt_disposed', False):
            return
        try:
            rc = proc.poll()
        except Exception as e:
            dsh_log("verify: poll 异常 %r" % (e,))
            return
        if rc is None:
            dsh_log("verify: 3 秒后仍存活 -> 启动成功 (pid=%s)" % getattr(proc, "pid", "?"))
            return                       # 还活着 → 启动成功，不打扰

        dsh_log("verify: 进程已退出 退出码=%s (pid=%s)" % (rc, getattr(proc, "pid", "?")))

        # ★ 退出码 0 + 没有别的实例在跑 → 重试一次（同样剔除 ELECTRON_*）
        if rc == 0 and not is_dsh_running():
            dsh_log("verify: 退出码 0 且无其他实例，清锁并重试")
            removed = self._clear_dsh_locks()
            dsh_log("verify: 重试前清理锁 %r" % (removed,))
            try:
                import os as _os
                exe, base = self._resolve_dsh_exe()
                if exe:
                    env = dict(_os.environ)
                    for k in [k for k in env if k.upper().startswith("ELECTRON")]:
                        env.pop(k, None)
                    proc2 = subprocess.Popen([exe], cwd=base, env=env)
                    dsh_log("verify: 重试 Popen 成功 pid=%s" % proc2.pid)
                    QTimer.singleShot(3000, lambda: self._verify_retry(proc2))
                    return
            except Exception as e:
                dsh_log("verify: 重试异常 %r" % (e,))

        if rc == 0:
            self.bubble.show_message(
                "打开 DSH 失败",
                "进程启动后立即退出（退出码 0）\n可能已有实例在运行，或单实例锁未释放")
        else:
            self.bubble.show_message(
                "打开 DSH 失败",
                "进程启动后立即退出（退出码 %s）" % rc)

    def _verify_retry(self, proc):
        """重试后的校验（不再递归重试，避免死循环）。"""
        if getattr(self, '_qt_disposed', False):
            return
        try:
            rc = proc.poll()
        except Exception:
            return
        if rc is None:
            dsh_log("verify_retry: 重试成功，进程存活 (pid=%s)" % getattr(proc, "pid", "?"))
            return
        dsh_log("verify_retry: 重试后仍退出 退出码=%s" % rc)
        self.bubble.show_message(
            "打开 DSH 失败",
            "清理残留锁后重试仍失败（退出码 %s）\n请手动启动一次 DSH" % rc)

    def _balance_available(self):
        """余额快照是否可用（有 DSH 插件在推）。不可用时双击降级为普通动作。"""
        try:
            snap = self.store.get_balance() if getattr(self, 'store', None) else None
        except Exception:
            snap = None
        if not snap:
            return False
        return snap.get('status') not in (None, 'disabled')

    def _request_balance(self):
        """双击查余额。子体转发给主体 —— 余额气泡永远从没编号的那只弹出。"""
        if getattr(self, 'is_main', True):
            self.check_balance()
            return
        main_pet = None
        for pp in list(ALL_PETS):
            if getattr(pp, 'is_main', False) and pp.isVisible():
                main_pet = pp
                break
        if main_pet is not None:
            main_pet.check_balance()

    def check_balance(self):
        # 真实余额来自 DSH 插件（dsh-pet-pyside6）推送的 balance 快照。
        # 独立模式没人推 → 明确说"未连接"，不再显示假的 99.0。
        snap = None
        try:
            snap = self.store.get_balance() if getattr(self, 'store', None) else None
        except Exception:
            snap = None
        bal_anims = self.raw_anims.get('events', {}).get('balance', []) or []

        if not snap or snap.get('status') in (None, 'disabled'):
            # 独立运行（没有 DSH 插件推送）才会走到这里
            self.bubble.show_message("余额查询", "未连接 DSH，暂无余额数据")
            return
        if snap.get('status') == 'loading':
            self.bubble.show_message("余额查询", "正在查询余额…")
            return
        if snap.get('status') not in ('ready',):
            msg = snap.get('error') or snap.get('summary') or "余额暂不可用"
            self.bubble.show_message("余额查询", str(msg))
            return

        bal = 0.0
        for row in (snap.get('balances') or []):
            if isinstance(row, dict):
                try:
                    bal += float(row.get('total') or 0)
                except Exception:
                    pass

        if bal < 1:   idx = 0
        elif bal < 10: idx = 3
        elif bal < 50: idx = 1
        elif bal < 200: idx = 4
        elif bal < 500: idx = 5
        else: idx = 2
        # ★ 峰谷档位（与 dsh-pet 的 deepseekPricingTier 同口径）：
        #   工作日 9-12 / 14-18 为高峰，其余低谷；周末全天低谷。
        #   快照里若已带 tier 就直接用，否则按当前北京时间算。
        tier = snap.get('tier')
        if tier not in ('peak', 'idle'):
            tier = deepseek_pricing_tier()
        text = balance_text(bal, tier)
        html = balance_html(bal, tier)   # 气泡用：峰/谷 加粗灰字，与 pet 一致
        # ★ 气泡**直接显示**，不依赖 force_action 的 msg 透传：
        #   流式加载下动作是异步的，on_first_batch 先到、on_loaded 又走 upgrading 分支
        #   early return，msg 永远传不到气泡 —— 表现就是"有动画没气泡"。
        self.bubble.show_message("余额查询", html, 12000)
        if idx < len(bal_anims):
            self.force_action(bal_anims[idx], interrupt=True)

    def _end_spawn_hold(self):
        """召唤缓冲到期：**立刻**散开（不等当前待机播完）。

        历史坑（2026-10-30 修）：早期实现只把 _spawn_hold_until 清零再调
        load_next_action()，结果被 `_action_incomplete()` 的"完整播放保护"挡下
        → 待机整段（约 10 秒）播完才动，中途 `_pending_force_roam_at` 的
        2500ms TTL 还过期了 → 观感是"2 秒到了却一直发呆"，四散奔逃消失。
        现在 force=True 直接打断，并当场写入 roam 意图，保证 2 秒整全体一起跑。
        """
        if getattr(self, '_qt_disposed', False):
            return          # 缓冲期内被关掉了，别再摸已拆掉的 Qt 资源
        try:
            self._spawn_hold_until = 0.0
            self._pending_next = False
            if self._move_loading:
                # 缓冲期内待机的第一批帧还没解出来（罕见）：等它开播后再兑现
                self._pending_next = True
                return
            # 强制造 roam 意图：此刻全员叠在屏幕正中，overlap 破表，
            # 配合 _first_move_random 的 360° 随机方向 → 起手就是四散奔逃。
            self._pending_force_roam_at = time.monotonic() * 1000.0
            self.load_next_action(force=True)
        except RuntimeError:
            pass  # 窗口在缓冲期内被销毁时，QTimer.singleShot 仍会触发一次

    def tick_flock(self):
        if (not self.flock_enabled) or self.drag_on or len(ALL_PETS) <= 1:
            return
        if self.is_moving_action:
            return
        if self._move_loading:
            return
        now_pets = [pp for pp in ALL_PETS if pp is not self and pp.isVisible()]
        if not now_pets:
            return
        s = self.scale
        bx, by, bw, bh = BODY_BOX
        cx = self.x() + (bx + bw / 2.0) * s
        cy = self.y() + (by + bh / 2.0) * s
        half_w = max(1.0, bw * s / 2.0)
        half_h = max(1.0, bh * s / 2.0)
        mates = []
        for pp in now_pets:
            ps = getattr(pp, 'scale', 1.0)
            mates.append((pp.x() + (bx + bw / 2.0) * ps, pp.y() + (by + bh / 2.0) * ps))
        ov = overlap_ratio(cx, cy, half_w, half_h, mates)
        # ★ 预输入：只记意图，**绝不打断**当前动作。
        #   直接调 load_next_action 会把正在播的动作半途截断（观感突兀、动作碎），
        #   而且硬触发会和多开同时命中，10 只一起跳帧。
        if ov >= 0.8:
            self._pending_force_roam_at = time.monotonic() * 1000.0

    # ------------------------------------------------------------------
    # 光标倾斜跟随（照搬 dsh-whale-pet 的 wp-fx 层）
    #
    # ★ 作用域（2026-10-30 最终口径）：
    #   · 单体模式           → 主体 + 子体都倾斜
    #   · 群体模式           → **只有主体**倾斜（子体不倾斜）
    #   · 拖拽中 / 物理飞行中 → 不倾斜（与 whale 的豁免一致）
    # ------------------------------------------------------------------
    def tilt_enabled(self):
        """本只是否启用光标倾斜。

        ★ 作用域（2026-10-30 最终口径）：
          · **单体模式** → 启用（主体、子体都启用）
          · **群体模式** → 只有**主体**启用，子体不倾斜
                          （群体子体有自己的涌现行为，一起晃会干扰观感）
          · 拖拽中 / 物理飞行中 → 不启用（与 whale 的豁免一致）
          · 隐藏         → 不启用

        历史：
          · 最初为"防止破坏主体"只开了单体子体；
          · 2026-10-29 解除主体限制，单体模式下主体+子体都开；
          · 2026-10-30 群体模式下补上主体（子体仍不晃）。
        """
        try:
            if not _SWAY_ENABLED[0]:
                return False                      # 摇晃总开关关闭
            _is_main = bool(getattr(self, 'is_main', False))
            if getattr(self, 'flock_enabled', False):
                # 群体模式：只让主体晃，子体保持原样
                if not _is_main:
                    return False
            if getattr(self, 'drag_on', False):
                return False
            if self.phys_timer.isActive():
                return False
            if not self.isVisible():
                return False
        except Exception:
            return False
        return True

    def toggle_autostart(self):
        """切换「开机自启动」。

        状态以注册表 `HKCU\\...\\Run\\DaFeiyuPet` 为准，不落 settings.json ——
        用户在「任务管理器 → 启动」里禁用/删除后，菜单下次打开即如实反映。
        """
        try:
            from src.host import autostart
            root = Path(self.base_dir).parent      # base_dir 是 frames/，上一级是程序根
            before = autostart.is_enabled(root)
            after = autostart.toggle(root)
            if after == before:
                # 写入失败（权限/找不到 pythonw/main.py）
                self.bubble.show_message(
                    "开机自启", "设置失败，请检查程序完整性", 4000)
                return
            self.bubble.show_message(
                "开机自启", "已开启" if after else "已关闭", 3000)
        except Exception:
            pass

    def toggle_sway(self):
        """切换摇晃开关（全局）。名称即状态，与「行为」「拷问」同一写法。"""
        try:
            new_on = not tilt_sway_enabled()
            set_tilt_sway(new_on)
            self.bubble.show_message("摇晃", "已开启" if new_on else "已关闭", 3000)
        except Exception:
            pass

    def toggle_roam(self):
        """切换漫游开关（全局）。

        关掉后移动类动作**原地播放**、不再位移；群体模式的 roam 位移不受影响，
        但"再切到群体"会被拦下（气泡提示漫游没有打开）；
        若关闭瞬间正处于群体模式，set_roam 会把主体自动拉回单体。
        """
        try:
            new_on = not roam_enabled()
            set_roam(new_on)
            if new_on:
                self.bubble.show_message("漫游", "已开启", 3000)
            else:
                self.bubble.show_message(
                    "漫游已关闭",
                    "移动动作改为原地播放；要切群体请先打开漫游", 5000)
        except Exception:
            pass

    def toggle_topmost(self):
        """切换「窗口置顶」（全局）。

        关掉后桌宠不再压在最上层，会被其他窗口正常遮住 ——
        适合想全屏干活又不想关掉桌宠的场合。状态是进程内全局，不落盘。
        """
        try:
            new_on = not topmost_enabled()
            set_topmost(new_on)
            self.bubble.show_message(
                "窗口置顶", "已开启" if new_on else "已关闭", 3000)
        except Exception:
            pass

    def _tilt_pivot_y(self):
        """倾斜/形变的旋转原点（脚底）。"""
        return float(self.height()) * TILT_ORIGIN_Y_FALLBACK

    def _tick_tilt(self):
        """每帧推进倾斜弹簧；返回 True 表示需要重绘。

        触发源用 QCursor.pos() 轮询（tick 14ms ≈ 70Hz）。
        whale 用 pointermove 事件（~125Hz），这里靠加快弹簧系数补偿采样差。
        """
        if not self.tilt_enabled():
            if self.tilt.active:
                self.tilt.reset()
                return True
            return False
        try:
            gpos = QCursor.pos()
            g = self.frameGeometry()
            self.tilt.update_target(
                float(gpos.x()), float(gpos.y()),
                (float(g.left()), float(g.top()),
                 float(g.right()), float(g.bottom())))
        except Exception:
            return False
        return self.tilt.step()

    # ------------------------------------------------------------------
    # 单体模式硬碰撞（碰撞框 = BODY_BOX 换算成屏幕坐标）
    #
    # 只作用于**单体模式**：群体模式刻意允许重叠（flock.py 的软推力设计），
    # 所以 collide_participates() 在群体模式下一律返回 False，那边一行不改。
    # ------------------------------------------------------------------
    def collide_participates(self):
        """本只是否参与硬碰撞。

        三条豁免（用户确认）：
          · 群体模式 → 不参与（群体允许重叠，走 Boids 软推力）；
          · 正在被拖拽 → 不参与（鼠标说了算）；
          · 正在物理飞行（抛掷）→ 不参与（弹跳路径不被碰撞改写）。
        """
        try:
            if getattr(self, 'flock_enabled', False):
                return False
            if getattr(self, 'drag_on', False):
                return False
            if self.phys_timer.isActive():
                return False
            if not self.isVisible():
                return False
        except Exception:
            return False
        return True

    def body_aabb(self):
        """当前身体框在屏幕坐标系里的 (中心x, 中心y, 半宽, 半高)。"""
        s = self.scale
        bx, by, bw, bh = BODY_BOX
        cx = self.x() + (bx + bw / 2.0) * s
        cy = self.y() + (by + bh / 2.0) * s
        return cx, cy, max(1.0, bw * s / 2.0), max(1.0, bh * s / 2.0)

    def clamp_body_center(self, cx, cy):
        """把身体中心夹进该屏允许的范围（与漫游时同一套 WALL_COVER 口径）。"""
        s = self.scale
        bx, by, bw, bh = BODY_BOX
        scr = _screen_at(cx, cy)
        body_w = bw * s
        body_h = bh * s
        allow_x = body_w * WALL_COVER
        allow_y = body_h * WALL_COVER
        min_x = scr.left() - allow_x + body_w / 2.0
        max_x = scr.right() + allow_x - body_w / 2.0
        min_y = scr.top() - TOP_SHIFT - allow_y + body_h / 2.0
        max_y = scr.bottom() + BOTTOM_EXTEND - TOP_SHIFT + allow_y - body_h / 2.0
        if min_x > max_x:
            cx = (min_x + max_x) / 2.0
        else:
            cx = max(min_x, min(cx, max_x))
        if min_y > max_y:
            cy = (min_y + max_y) / 2.0
        else:
            cy = max(min_y, min(cy, max_y))
        return cx, cy

    def apply_body_center(self, cx, cy):
        """把身体中心写回窗口位置（只动位置，动画照旧）。"""
        s = self.scale
        bx, by, bw, bh = BODY_BOX
        self.move(int(round(cx - (bx + bw / 2.0) * s)),
                  int(round(cy - (by + bh / 2.0) * s)))
        if self.bubble.isVisible():
            self.bubble.update_position()

    def _collide_mates(self):
        """参与碰撞的其他单体鱼：[(pet, cx, cy, hw, hh), ...]。"""
        out = []
        for pp in list(ALL_PETS):
            if pp is self or not _alive(pp):
                continue
            try:
                if not pp.collide_participates():
                    continue
                cx, cy, hw, hh = pp.body_aabb()
            except Exception:
                continue
            out.append((pp, cx, cy, hw, hh))
        return out

    def _resolve_move_with_collision(self, from_cx, from_cy, to_cx, to_cy):
        """单体模式：把"想走到的位置"过一遍推箱子碰撞，返回实际落点。

        撞人者按原速前进，被撞者被顶着同速后退；链式一路传；
        任何一环被屏幕边缘卡住 → 整条链一起停。
        """
        mates = self._collide_mates()
        if not mates:
            return to_cx, to_cy
        hw = self.body_aabb()[2]
        hh = self.body_aabb()[3]

        def _clamp(key, cx, cy):
            if key is None:
                return self.clamp_body_center(cx, cy)
            return key.clamp_body_center(cx, cy)

        try:
            nx, ny, pushes = resolve_push(
                (from_cx, from_cy, hw, hh),
                [(pp, cx, cy, phw, phh) for pp, cx, cy, phw, phh in mates],
                to_cx - from_cx, to_cy - from_cy, _clamp)
        except Exception:
            return to_cx, to_cy
        # 被顶开的那几只：只改位置，动画照旧
        for pp, px, py in pushes:
            try:
                pp.apply_body_center(px, py)
            except Exception:
                pass
        return nx, ny

    def _apply_facing(self, right):
        prev = getattr(self, '_facing_right', None)
        self._facing_right = right
        if prev is not None and prev == right:
            return
        self.update()

    def _reassert_topmost(self):
        """**不抢焦点**地把桌宠重新压回 TOPMOST 层。

        场景：双击把 DSH 提到最前后，DSH 成了前台窗口。桌宠本身带
        `WindowStaysOnTopHint`，理论上仍在最上层；但个别情况下（切换前台、
        任务栏操作）置顶标志会被系统重置，这里补一次 `SetWindowPos`。

        ★ 必须带 `SWP_NOACTIVATE`：**不能**调 `activateWindow()`，
          否则会把焦点从刚提上来的 DSH 又抢回桌宠，用户就看不到 DSH 了。
        """
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            for w in [self] + [pp for pp in list(ALL_PETS)
                               if pp is not self and pp.isVisible()]:
                try:
                    user32.SetWindowPos(
                        wintypes.HWND(int(w.winId())),
                        wintypes.HWND(HWND_TOPMOST),
                        0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
                    )
                except Exception:
                    pass
            # 气泡同样（它是独立顶层窗口，会被 DSH 盖住）
            bub = getattr(self, 'bubble', None)
            if bub is not None and bub.isVisible():
                try:
                    user32.SetWindowPos(
                        wintypes.HWND(int(bub.winId())),
                        wintypes.HWND(HWND_TOPMOST),
                        0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def bring_to_front(self):
        """把桌宠强制刷到最上层（解决被其他窗口遮住）。

        Windows 上 `raise_()` 单独用不够：别的进程窗口可能抢在前头。
        这里用 SetWindowPos + HWND_TOPMOST，并配合 Qt 的 show/raise/activate，
        确保不管之前被什么遮住，都能立刻回到最前。

        同时把气泡也顶上来（气泡是独立顶层窗口，会单独被遮）。
        """
        try:
            # 1) Qt 层面：重新应用置顶标志（某些情况下标志会失效）
            #    ★ 必须尊重「置顶」开关：关掉时不能偷偷把它又设成最上层，
            #      否则用户关了菜单里的置顶、双击一下又回来了。
            self.setWindowFlag(Qt.WindowStaysOnTopHint, topmost_enabled())
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

        # 2) Win32 层面：SetWindowPos 强制 TOPMOST（置顶关闭时只抬到普通层）
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            SWP_NOACTIVATE = 0x0010
            hwnd = int(self.winId())
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(HWND_TOPMOST if topmost_enabled() else HWND_NOTOPMOST),
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW | SWP_NOACTIVATE,
            )
        except Exception:
            pass

        # 3) 气泡也顶上来（如果正在显示）
        try:
            if getattr(self, 'bubble', None) is not None and self.bubble.isVisible():
                self.bubble.raise_()
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                hwnd = int(self.bubble.winId())
                user32.SetWindowPos(
                    wintypes.HWND(hwnd),
                    wintypes.HWND(-1 if topmost_enabled() else -2),
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0040 | 0x0010,
                )
        except Exception:
            pass

        # 4) 多开时所有个体一起刷新
        try:
            for pp in list(ALL_PETS):
                if pp is self or not pp.isVisible():
                    continue
                try:
                    pp.raise_()
                    import ctypes
                    from ctypes import wintypes
                    user32 = ctypes.windll.user32
                    hwnd = int(pp.winId())
                    user32.SetWindowPos(
                        wintypes.HWND(hwnd),
                        wintypes.HWND(-1 if topmost_enabled() else -2),
                        0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0040 | 0x0010,
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _persist_settings(self):
        """把当前**大小**立即写盘（只对主体生效）。

        ★ 行为模式（群体/单体）不持久化 —— 用户只要大小。
        ★ 不在退出时保存：退出钩子跑的时候窗口可能已经被销毁，
          ALL_PETS 里找不到主体 → 静默不写（上一版就是这样丢的）。
          改成"切换即写盘"，任何时刻强杀都不会丢。
        """
        if not getattr(self, 'is_main', False):
            return          # 多开个体的临时大小不持久化
        try:
            from src.host.user_settings import save as save_settings
            root = Path(self.base_dir).parent      # base_dir 是 frames/，上一级是程序根
            save_settings(root, scale=float(getattr(self, 'scale', 0.75) or 0.75))
        except Exception:
            pass            # 只读目录等情况静默跳过，不影响运行

    def set_scale(self, s):
        if s <= 0:
            scr = _scr()
            if self.pms:
                # pms 现在是**已按 scale 缩放**的帧，原图尺寸要用常量算
                fw, fh = FRAME_W, FRAME_H
                if fw > 0 and fh > 0:
                    s = max(0.3, min(scr.width() / fw, scr.height() / fh))
                else:
                    s = 1.0
            else:
                s = 1.0
            self._auto_scale = True
        else:
            self._auto_scale = False
        self.scale = float(s)
        self._scale_cache = {}
        self._scale_cache_order = []
        win_w = max(1, int(FRAME_W * self.scale))
        win_h = max(1, int(FRAME_H * self.scale))
        self.setFixedSize(win_w, win_h)
        scr = _scr()
        nx = max(scr.left(), min(self.x(), scr.right() - win_w))
        ny = max(scr.top(), min(self.y(), scr.bottom() - win_h))
        self.move(nx, ny)
        if self.bubble.isVisible():
            self.bubble.update_position()
        if self.current_name:
            self.force_action(self.current_name, interrupt=True)
        self._persist_settings()      # ★ 切换大小 → 立即写盘

    def schedule_whisper(self):
        try:
            ms = random.randint(180000, 480000)
            self._whisper_timer.start(ms)
        except Exception:
            pass

    def _fire_whisper(self):
        ws = self.raw_anims.get('events', {}).get('whisper', []) or []
        if ws and not self.drag_on:
            try:
                self.force_action(random.choice(ws))
            except Exception:
                pass
        self.schedule_whisper()

    def spawn_by_mode(self):
        """按当前行为模式追加多开：群体 +10 只、单体 +1 只。

        与「行为」格的区别：行为格是**切换模式并重建**，这里是**按现有模式追加**。
        """
        flock = bool(getattr(self, 'flock_enabled', False))
        self.spawn_many(10 if flock else 1, flock)

    def toggle_behavior(self):
        """点击「行为」格：群体 ⇄ 单体，并把多开重建为 10 / 1 只。

        菜单只有两格：行为（点它切换名称）和多开（跟随行为变数量）。
        """
        want_flock = not bool(getattr(self, 'flock_enabled', False))
        # ★ 漫游闸门：群体模式靠 roam 位移才有意义，漫游关着就不许切过去。
        if want_flock and not roam_enabled():
            try:
                self.bubble.show_message(
                    "漫游没有打开",
                    "先在「设置 ▸ 漫游」里打开，才能切到群体模式", 4000)
            except Exception:
                pass
            return
        if not getattr(self, 'is_main', getattr(self, 'spawn_idx', 0) == 0):
            # 子体：只切自己的行为，不改场上数量
            self.flock_enabled = want_flock
            return
        self.flock_enabled = want_flock
        self.close_all_pets()
        self.spawn_many(10 if self.flock_enabled else 1, bool(self.flock_enabled))

    def toggle_flock(self, checked):
        """切换**本只**的模式。

        设计（用户要求）：
          · 勾选「本只群体行为」 = **群体模式** → 场上重置为 **10 只**
          · 取消勾选           = **单体模式** → 场上重置为 **1 只**
        主体切换时按模式重建多开；子体只改自己，不动别人的数量。
        """
        if checked and not roam_enabled():
            # ★ 与 toggle_behavior 同一道闸：漫游关着不许进群体模式。
            try:
                self.bubble.show_message(
                    "漫游没有打开",
                    "先在「设置 ▸ 漫游」里打开，才能切到群体模式", 4000)
            except Exception:
                pass
            return
        self.flock_enabled = bool(checked)
        if not getattr(self, 'is_main', getattr(self, 'spawn_idx', 0) == 0):
            # 子体：只切自己的行为，不影响场上数量
            return
        # 主体 = 决策者：按模式重置多开数量
        self.close_all_pets()
        self.spawn_many(10 if checked else 1, bool(checked))

    def renumber_kids(self):
        """按当前多开列表重排编号 1..N（主体不参与）。

        用户要"有几只编几号"：关掉几只再开，编号从 1 重新排，
        而不是沿用全局自增序号一直涨。
        """
        kids = [p for p in list(ALL_PETS) if not getattr(p, 'is_main', False)]
        # 按创建顺序（spawn_idx）稳定排序，避免每次重排编号乱跳
        kids.sort(key=lambda p: getattr(p, 'spawn_idx', 0))
        for i, p in enumerate(kids, 1):
            try:
                p.pet_label = str(i)
            except Exception:
                pass

    def spawn_many(self, count=10, flock=True):
        """添加多开。

        count/flock 组合（菜单里给两项）：
          · 10 只 → 默认**群体**模式（一起涌现、一起散开）
          · 1 只  → **单体**模式（独立个体的原行为）
        个体之后可以在自己的右键菜单里切换「本只群体行为」。
        """
        try:
            for _ in range(count):
                # ★ 创建时就带主体大小：不会出现"先标准大小、再缩一下"
                p = PetWindow(self.base_dir, self.config_data, self.store,
                              scale=float(self.scale))
                p.spawn_random = True
                p.flock_enabled = bool(flock)
        except Exception as e:
            self.bubble.show_message("多开失败", str(e))
            # ★ 编号重排：有几只编几号（不用全局自增序号）
            self.renumber_kids()

    def close_self(self):
        """关闭本只（子体菜单用）：只关自己，主体和其他多开不受影响。"""
        try:
            if getattr(self, 'is_main', False):
                # 兜底：主体不该走这里
                return QApplication.quit()
        except Exception:
            pass
        # ★ 主线程先拆 QTimer + 摘解码队列（见 _dispose_pet_qt 的说明）
        _withdraw_inflight(self)
        _dispose_pet_qt(self)
        try:
            self.bubble.close()
        except Exception:
            pass
        try:
            if self in ALL_PETS:
                ALL_PETS.remove(self)
        except Exception:
            pass
        try:
            for ld in list(getattr(self, 'loaders', [])):
                try:
                    if ld.isRunning():
                        ld.requestInterruption()
                        ld.quit()
                        _PENDING_THREADS.append(ld)
                except Exception:
                    pass
            self.loaders = []
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass

    def close_all_pets(self):
        """关闭全部多开，**永远保留主体**。

        性能要点（对齐大肥鱼/pet 的"顺畅关闭"）：
          · 不在主线程逐个 wait 线程 —— 那是 100 只 × 100ms 的串行等待，观感就是卡死
          · 先 hide()：视觉上瞬间消失
          · 线程 requestInterruption + quit 后丢进全局待回收表，由定时器清引用
          · 窗口用 deleteLater() 异步销毁，同时立刻从 ALL_PETS 摘除
        """
        def _is_main(p):
            return bool(getattr(p, 'is_main', False)) or getattr(p, 'spawn_idx', 0) == 0

        targets = [pp for pp in list(ALL_PETS) if pp is not self and not _is_main(pp)]
        if not targets:
            return

        # 1) 立即隐藏：这一帧之后画面上就没有了
        for pp in targets:
            try:
                pp.hide()
            except Exception:
                pass

        # 2) 中断线程：不等待，引用交给全局回收表
        #    ★ 同时摘掉解码等待队列 + 在主线程拆掉 QTimer：
        #      不摘的话 `_INFLIGHT` 会一直强引用这些窗口，线程 emit 时回调到
        #      已 deleteLater 的 QWidget（0xC0000005）；
        #      不拆 timer 的话它们要等 GC，而 GC 可能跑在工作线程里 →
        #      "Timers cannot be stopped from another thread" 然后崩溃。
        for pp in targets:
            try:
                _withdraw_inflight(pp)
                _dispose_pet_qt(pp)
            except Exception:
                pass
            try:
                for ld in list(getattr(pp, 'loaders', [])):
                    try:
                        if ld.isRunning():
                            ld.requestInterruption()
                            ld.quit()
                            _PENDING_THREADS.append(ld)
                    except Exception:
                        pass
                pp.loaders = []
            except Exception:
                pass

        # 3) 异步销毁 + 立刻从全局表摘除
        for pp in targets:
            try:
                try:
                    pp.bubble.close()
                except Exception:
                    pass
                try:
                    ALL_PETS.remove(pp)
                except (KeyError, ValueError):
                    pass
                pp.deleteLater()
            except Exception:
                pass

        # ★ 关闭后重排：剩下几只就编 1..N
        self.renumber_kids()