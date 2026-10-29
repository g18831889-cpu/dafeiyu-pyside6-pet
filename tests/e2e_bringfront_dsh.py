# -*- coding: utf-8 -*-
"""端到端：真实最小化 DSH 窗口 → bring_dsh_to_front() → 校验恢复。

★ 沙箱里 tasklist 子进程被拒（CreatePipe WinError 5），
  所以用「按窗口标题反查 PID」喂 _iter_dsh_pids，
  枚举/ShowWindow/提前台全部走真实 Win32。

★ 前台锁：本脚本不是用户双击触发的，进程不在前台，
  SetForegroundWindow 会被系统拒绝（这是 Windows 的既定行为，不是 bug）。
  因此这里分两段验证：
    A. 还原（IsIconic 变 False）—— 可确定性验证；
    B. 前台提升 —— 先 SetForegroundWindow 把自己变成前台，再验证。
"""
import os, sys, time, ctypes
from ctypes import wintypes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import src.runtime.window as W
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_MINIMIZE = 6

def find_dsh_windows():
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "DeepSeek Harness" in buf.value:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((int(hwnd), buf.value, pid.value,
                            bool(user32.IsWindowVisible(hwnd)),
                            bool(user32.IsIconic(hwnd))))
        return True
    user32.EnumWindows(EnumProc(_cb), 0)
    return out

def send_minimize(hwnd):
    """非前台进程不能 ShowWindow(SW_MINIMIZE)（err 5），用 WM_SYSCOMMAND 更可靠。"""
    WM_SYSCOMMAND = 0x0112
    SC_MINIMIZE = 0xF020
    user32.PostMessageW(wintypes.HWND(hwnd), WM_SYSCOMMAND, SC_MINIMIZE, 0)

wins = find_dsh_windows()
print("=== 0. 找到的 DSH 窗口 ===")
for w in wins:
    print("   hwnd=%s pid=%s visible=%s iconic=%s title=%r" % (w[0], w[2], w[3], w[4], w[1]))
if not wins:
    print("RESULT: SKIP（没有标题含 DeepSeek Harness 的窗口）")
    sys.exit(0)

pid = wins[0][2]
# 真实路径：_iter_dsh_pids 已能通过窗口反查拿到 DSH 进程，无需打桩
main = [w for w in wins if w[3]]
if not main:
    print("RESULT: SKIP（主窗口不可见）")
    sys.exit(0)
hwnd, title = main[0][0], main[0][1]
print("\n目标窗口: hwnd=%s title=%r" % (hwnd, title))

# ---------- A. 还原能力 ----------
print("\n=== A. 最小化 → bring_dsh_to_front() 是否还原 ===")
send_minimize(hwnd)
time.sleep(1.0)
iconic_before = bool(user32.IsIconic(wintypes.HWND(hwnd)))
print("  最小化后 IsIconic =", iconic_before)

t0 = time.perf_counter()
moved, t = W.bring_dsh_to_front()
ms = (time.perf_counter() - t0) * 1000
time.sleep(1.0)
iconic_after = bool(user32.IsIconic(wintypes.HWND(hwnd)))
print("  -> moved=%r title=%r 耗时=%.1fms" % (moved, t, ms))
print("  还原后 IsIconic =", iconic_after)
okA = (moved is True) and (iconic_before is True) and (iconic_after is False)

# ---------- B. 前台提升能力 ----------
print("\n=== B. 把本进程变成前台 → 再提 DSH ===")
# 建一个真窗口并强制到前台，模拟"用户刚点过桌宠"的状态
from PySide6.QtWidgets import QWidget
w = QWidget()
w.resize(200, 100)
w.show()
app.processEvents()
mine = int(w.winId())
for attempt in range(6):
    try:
        user32.SetForegroundWindow(wintypes.HWND(mine))
    except Exception:
        pass
    app.processEvents()
    time.sleep(0.15)
    if int(user32.GetForegroundWindow() or 0) == mine:
        break
print("  当前前台是否为本进程窗口:", int(user32.GetForegroundWindow() or 0) == mine)

# 再最小化 DSH，然后（此时本进程在前台）提前台
send_minimize(hwnd)
time.sleep(0.9)
print("  再次最小化 IsIconic =", bool(user32.IsIconic(wintypes.HWND(hwnd))))
moved2, t2 = W.bring_dsh_to_front()
time.sleep(1.0)
fg = int(user32.GetForegroundWindow() or 0)
iconic2 = bool(user32.IsIconic(wintypes.HWND(hwnd)))
print("  -> moved=%r  foreground==DSH:%s  IsIconic=%s" % (moved2, fg == hwnd, iconic2))
okB = (moved2 is True) and (iconic2 is False) and (fg == hwnd)

# ---------- C. 幂等 ----------
print("\n=== C. 已在最前再调一次（幂等）===")
moved3, t3 = W.bring_dsh_to_front()
print("  -> moved=%r（期望 False）" % (moved3,))
okC = (moved3 is False)

# ---------- D. 未运行 ----------
print("\n=== D. DSH 未运行时不动 ===")
orig = W._iter_dsh_pids
W._iter_dsh_pids = lambda: []
moved4, t4 = W.bring_dsh_to_front()
W._iter_dsh_pids = orig
print("  -> moved=%r（期望 False）" % (moved4,))
okD = (moved4 is False)

w.close()
print("\nRESULT:", "PASS" if (okA and okB and okC and okD) else "FAIL",
      "| 还原=%s 前台=%s 幂等=%s 未运行=%s" % (okA, okB, okC, okD))
sys.exit(0 if (okA and okB and okC and okD) else 1)
