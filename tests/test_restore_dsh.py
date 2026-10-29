"""双击把 DSH 窗口提到最前 —— 逻辑与安全边界测试。

覆盖（2026-10-29 改版后语义）：
  · DSH 没运行 / 没有主窗口 → 一律不动作；
  · 已最小化 → ShowWindow(SW_RESTORE) + 提前台；
  · 正常显示但被遮住 → 提前台；
  · 最小化到托盘（窗口 hide）→ 从隐藏窗口里捞回来 SW_SHOW + 提前台；
  · 已经在最前 → 幂等不打扰；
  · 枚举异常不冒泡（不影响同一次双击里的余额查询）；
  · 只做**一次性**前台提升，绝不给 DSH 加 TOPMOST；
  · 桌宠自己用 SWP_NOACTIVATE 重新压回 TOPMOST（不抢 DSH 焦点）。

真实的最小化/还原链路另有端到端脚本验证；这里测分支逻辑。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import ctypes as _ct
import src.runtime.window as W
from src.runtime.window import (PetWindow, bring_dsh_to_front,
                                _enum_dsh_main_windows, _enum_dsh_hidden_windows)
from src.host.config_loader import load_config

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


class FakeStore:
    def get_state(self): return {'counter': 0, 'workStatus': None}
    def get_balance(self): return {'status': 'ready', 'balances': [{'total': '10'}]}


cfg = load_config(PROJ / 'frames', PROJ / 'config.jsonc')
pet = PetWindow(PROJ / 'frames', cfg, FakeStore())
pet.show()
app.processEvents()

msgs = []
pet.bubble.show_message = lambda t, m, *a, **kw: msgs.append((t, m))


def _hval(h):
    """把 ctypes 句柄（c_void_p / int / None）还原成 int。

    ★ 不能直接 `int(h)`：`int(c_void_p(222))` 会抛 TypeError，
      测试桩会把"调用失败"误报成"实现没调"。真实 user32 接受 c_void_p。
    """
    if h is None:
        return 0
    v = getattr(h, 'value', h)
    return int(v or 0)


class FakeUser32:
    """记录所有被调用的 Win32 函数，可配置前台窗口与返回值。"""
    def __init__(self):
        self.calls = []
        self.foreground = 0
        # 模拟 Windows 前台锁：调用"成功返回"但前台不变
        self.deny_foreground = False

    def GetForegroundWindow(self):
        self.calls.append(('GetForegroundWindow',))
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, ptr):
        self.calls.append(('GetWindowThreadProcessId', _hval(hwnd)))
        return 100

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(('ShowWindow', _hval(hwnd), cmd))
        return True

    def ShowWindowAsync(self, hwnd, cmd):
        self.calls.append(('ShowWindowAsync', _hval(hwnd), cmd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(('SetForegroundWindow', _hval(hwnd)))
        if not self.deny_foreground:
            self.foreground = _hval(hwnd)
        return True

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(('keybd_event', vk, flags))
        # 模拟"按过 Alt 后前台锁被解除"
        if self.deny_foreground and flags & 0x0002:
            self.deny_foreground = False
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(('BringWindowToTop', _hval(hwnd)))
        return True

    def SetActiveWindow(self, hwnd):
        self.calls.append(('SetActiveWindow', _hval(hwnd)))
        return True

    def AttachThreadInput(self, a, b, attach):
        self.calls.append(('AttachThreadInput', a, b, attach))
        return True

    def FlashWindow(self, hwnd, b):
        self.calls.append(('FlashWindow', _hval(hwnd), b))
        return True

    def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
        self.calls.append(('SetWindowPos', _hval(hwnd), _hval(insert_after), flags))
        return True

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 200


class FakeWindll:
    def __init__(self, user32):
        self.user32 = user32
        self.kernel32 = FakeKernel32()


REAL_WINDLL = _ct.windll
REAL_ITER = W._iter_dsh_pids
REAL_ENUM = W._enum_dsh_main_windows
REAL_ENUM_HIDDEN = W._enum_dsh_hidden_windows


def install_fakes(wins_visible, wins_hidden=(), foreground=0):
    """装好 fake user32 + 固定枚举结果。"""
    u32 = FakeUser32()
    u32.foreground = foreground
    _ct.windll = FakeWindll(u32)
    W._iter_dsh_pids = lambda: [1234]
    W._enum_dsh_main_windows = lambda: list(wins_visible)
    W._enum_dsh_hidden_windows = lambda: list(wins_hidden)
    return u32


def restore_all():
    _ct.windll = REAL_WINDLL
    W._iter_dsh_pids = REAL_ITER
    W._enum_dsh_main_windows = REAL_ENUM
    W._enum_dsh_hidden_windows = REAL_ENUM_HIDDEN


# ---------- 1. DSH 未运行 → 不动 ----------
W._iter_dsh_pids = lambda: []
r, t = bring_dsh_to_front()
check('DSH 未运行 → 不动作', r is False and t == "", f"r={r}")
check('DSH 未运行 → 无可见窗口', _enum_dsh_main_windows() == [])
check('DSH 未运行 → 无隐藏窗口', _enum_dsh_hidden_windows() == [])

# ---------- 2. 有进程但没有主窗口 → 不动 ----------
u32 = install_fakes([])
r, t = bring_dsh_to_front()
check('无窗口 → 不动作', r is False and t == "")
check('无窗口 → 没碰 Win32', u32.count('ShowWindow') == 0)
restore_all()

# ---------- 3. 已在最前且未最小化 → 幂等不打扰 ----------
u32 = install_fakes([(111, "DSH 主窗口", False)], foreground=111)
r, t = bring_dsh_to_front()
check('已在最前 → 不动作', r is False and t == "DSH 主窗口", f"r={r} t={t!r}")
check('已在最前 → 没调 ShowWindow', u32.count('ShowWindow') == 0)
check('已在最前 → 没调 SetForegroundWindow', u32.count('SetForegroundWindow') == 0)
restore_all()

# ---------- 4. 最小化 → SW_RESTORE + 提前台 ----------
u32 = install_fakes([(222, "DSH 主窗口", True)], foreground=999)
r, t = bring_dsh_to_front()
check('最小化 → 执行提前台', r is True and t == "DSH 主窗口", f"r={r} t={t!r}")
check('最小化 → ShowWindow(SW_RESTORE=9)', ('ShowWindow', 222, 9) in u32.calls, str(u32.calls))
check('最小化 → SetForegroundWindow', ('SetForegroundWindow', 222) in u32.calls)
check('最小化 → 前台已切到目标', u32.foreground == 222, f"fg={u32.foreground}")
check('一次成功就不走 AttachThreadInput 兜底', u32.count('AttachThreadInput') == 0,
      f"ati={u32.count('AttachThreadInput')}")
restore_all()

# ---------- 4b. SetForegroundWindow 被前台锁拒绝 → 走兜底 ----------
u32 = install_fakes([(777, "被锁的 DSH", True)], foreground=999)
u32.deny_foreground = True          # 模拟系统拒绝：调用无效、前台不变
r, t = bring_dsh_to_front()
check('前台锁拒绝 → 仍报已动作', r is True and t == "被锁的 DSH", f"r={r} t={t!r}")
check('前台锁拒绝 → 尝试 AttachThreadInput 兜底', u32.count('AttachThreadInput') >= 2,
      f"ati={u32.count('AttachThreadInput')}")
check('前台锁拒绝 → 兜底用 Alt 键解锁', u32.count('keybd_event') == 2,
      f"kb={u32.count('keybd_event')}")
check('前台锁拒绝 → Alt 解锁后前台到位', u32.foreground == 777,
      f"fg={u32.foreground}")
restore_all()

# ---------- 5. 可见但被遮住 → 提前台 ----------
u32 = install_fakes([(333, "被遮住的 DSH", False)], foreground=888)
r, t = bring_dsh_to_front()
check('被遮住 → 提前台', r is True and t == "被遮住的 DSH", f"r={r}")
check('被遮住 → ShowWindow(SW_RESTORE=9)', ('ShowWindow', 333, 9) in u32.calls)
check('被遮住 → SetForegroundWindow', ('SetForegroundWindow', 333) in u32.calls)
restore_all()

# ---------- 6. 最小化到托盘（窗口 hide）→ SW_SHOW ----------
u32 = install_fakes([], wins_hidden=[(444, "托盘的 DSH", False)], foreground=0)
r, t = bring_dsh_to_front()
check('托盘隐藏 → 捞回来', r is True and t == "托盘的 DSH", f"r={r} t={t!r}")
check('托盘隐藏 → ShowWindow(SW_SHOW=5)', ('ShowWindow', 444, 5) in u32.calls, str(u32.calls))
check('托盘隐藏 → SetForegroundWindow', ('SetForegroundWindow', 444) in u32.calls)
restore_all()

# ---------- 7. 多窗口：优先最小化的那只 ----------
u32 = install_fakes([(1, "正常窗口", False), (2, "最小化A", True), (3, "最小化B", True)],
                    foreground=999)
r, t = bring_dsh_to_front()
check('多窗口优先最小化的', r is True and t == "最小化A", f"t={t!r}")
check('多窗口只提一个', u32.count('SetForegroundWindow') == 1)
restore_all()

# ---------- 8. 绝不加 TOPMOST ----------
u32 = install_fakes([(555, "DSH", True)], foreground=999)
bring_dsh_to_front()
topmost_calls = [c for c in u32.calls if c[0] == 'SetWindowPos' and c[2] == -1]
check('不给 DSH 加 TOPMOST', len(topmost_calls) == 0, str(topmost_calls))
restore_all()

# ---------- 8b. 主窗口过滤：IME / 离屏 / 带 owner 的浮层都不能算主窗口 ----------
from src.runtime.window import _is_dsh_main_window


class _ClsUser32:
    """只提供 GetClassNameW / GetWindow，用来验证 _is_dsh_main_window 的过滤规则。"""
    def __init__(self, cls, owner=0):
        self.cls = cls
        self.owner = owner

    def GetClassNameW(self, hwnd, buf, n):
        buf.value = self.cls
        return len(self.cls)

    def GetWindow(self, hwnd, idx):
        return self.owner


def probe(cls, title, owner=0):
    _ct.windll = FakeWindll(_ClsUser32(cls, owner))
    try:
        return _is_dsh_main_window(1, title)
    finally:
        _ct.windll = REAL_WINDLL


check('IME 窗口不算主窗口', probe("IME", "Default IME") is False)
check('空标题不算主窗口', probe("Chrome_WidgetWin_1", "") is False)
check('离屏 Chrome_WidgetWin_0 不算', probe("Chrome_WidgetWin_0", "DSH") is False)
check('带 owner 的浮层不算', probe("Chrome_WidgetWin_1", "DSH", owner=99) is False)
check('非 Chrome 类名不算', probe("Notepad", "DSH") is False)
check('真正的主窗口算', probe("Chrome_WidgetWin_1", "大肥鱼桌宠 — DeepSeek Harness") is True)

# 真实环境回归：枚举结果里绝不能出现 Default IME
_real_mains = _enum_dsh_main_windows()
check('真实枚举不含 IME 窗口',
      all("IME" not in (w[1] or "") for w in _real_mains), str(_real_mains))

# ---------- 9. 异常不冒泡 ----------
def _boom(*a, **kw):
    raise RuntimeError("boom")


u32 = install_fakes([])
W._enum_dsh_main_windows = _boom
W._enum_dsh_hidden_windows = _boom
try:
    r, t = bring_dsh_to_front()
    check('枚举异常不冒泡', r is False and t == "", f"r={r} t={t!r}")
except Exception as e:
    check('枚举异常不冒泡', False, f"异常冒泡: {e!r}")
restore_all()

# ---------- 10. 桌宠自己重新压回 TOPMOST，但不抢焦点 ----------
u32 = install_fakes([(666, "DSH", True)], foreground=999)
try:
    pet._reassert_topmost()
    swp = [c for c in u32.calls if c[0] == 'SetWindowPos']
    check('_reassert_topmost 调了 SetWindowPos', len(swp) >= 1, str(swp))
    # HWND_TOPMOST = -1，经 c_void_p 往返会变成无符号 2^64-1，两种都算对
    check('桌宠压到 HWND_TOPMOST(-1)',
          all(c[2] in (-1, 18446744073709551615) for c in swp), str(swp))
    check('带 SWP_NOACTIVATE(0x10)', all(c[3] & 0x0010 for c in swp), str(swp))
except Exception as e:
    check('_reassert_topmost 不抛异常', False, str(e))
restore_all()

# ---------- 11. 双击接线：DSH 在跑才提，子体不触发 ----------
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt, QPointF
import time as _time
from src.runtime.window import DOUBLE_CLICK_MS

pet.bubble.show_message = lambda t, m, *a, **kw: msgs.append((t, m))
hits = {'bring': 0}
orig_bring = W.bring_dsh_to_front
W.bring_dsh_to_front = lambda: (hits.__setitem__('bring', hits['bring'] + 1) or (True, "DSH"))


def double_click(p):
    for _ in range(2):
        p.press_pos = QPointF(498, 498).toPoint()
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                         QPointF(p.rect().center()), QPointF(500, 500),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        p.drag_on = True
        p.mouseReleaseEvent(ev)
        _time.sleep(0.12)


double_click(pet)
check('主体双击 → 调 bring_dsh_to_front', hits['bring'] == 1, f"n={hits['bring']}")

kid = PetWindow(PROJ / 'frames', cfg, FakeStore())
kid.show()
app.processEvents()
hits['bring'] = 0
double_click(kid)
check('子体双击 → 不提前台（避免抢焦点）', hits['bring'] == 0, f"n={hits['bring']}")
try:
    kid.close()
except Exception:
    pass
W.bring_dsh_to_front = orig_bring

pet.close()
print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
