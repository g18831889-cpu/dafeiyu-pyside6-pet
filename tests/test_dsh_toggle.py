"""验证 close_dsh / open_dsh 加固逻辑（不触碰真实 DSH）。

用 monkeypatch 替换子进程调用，模拟三种场景：
  1. 优雅关闭成功
  2. 优雅关闭超时 → 强杀
  3. 打开后进程立即退出 → 清锁重试
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import src.runtime.window as W
from src.runtime.window import PetWindow
from src.host.config_loader import load_config

results = []
def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


class FakeStore:
    def get_state(self): return {'counter': 0, 'workStatus': None}
    def get_balance(self): return None


cfg = load_config(PROJ / 'frames', PROJ / 'config.jsonc')
pet = PetWindow(PROJ / 'frames', cfg, FakeStore())

msgs = []
pet.bubble.show_message = lambda title, msg, *a, **kw: msgs.append((title, msg))

# ---------- 场景 1：优雅关闭成功 ----------
state = {'running': True, 'soft_called': 0, 'kill_called': 0, 'locks': 0}
W.is_dsh_running = lambda: state['running']
W._iter_dsh_pids = lambda: ([123, 456] if state['running'] else [])
def soft_close():
    state['soft_called'] += 1
    state['running'] = False      # 立刻消失
    return True
W.soft_close_dsh = soft_close
W.wait_dsh_gone = lambda t=8.0: not state['running']
W.kill_dsh = lambda: (state.__setitem__('kill_called', state['kill_called'] + 1) or 2)
W.clear_dsh_locks = lambda: (state.__setitem__('locks', state['locks'] + 1) or 1)

msgs.clear()
pet.close_dsh()
check('优雅关闭被调用', state['soft_called'] == 1)
check('优雅关闭成功时不强杀', state['kill_called'] == 0)
check('关闭后清理了锁', state['locks'] == 1)
check('气泡提示已关闭', any('已关闭' in m[0] for m in msgs), str(msgs))

# ---------- 场景 2：优雅关闭超时 → 强杀 ----------
state.update({'running': True, 'soft_called': 0, 'kill_called': 0, 'locks': 0})
W.soft_close_dsh = lambda: (state.__setitem__('soft_called', 1) or True)
calls = {'n': 0}
def wait_gone(t=8.0):
    calls['n'] += 1
    if calls['n'] == 1:
        return False          # 第一次等：没退
    state['running'] = False  # 强杀后：退了
    return True
W.wait_dsh_gone = wait_gone
msgs.clear()
pet.close_dsh()
check('优雅关闭失败后强杀', state['kill_called'] == 1)
check('强杀后清锁', state['locks'] == 1)
check('提示含"强制结束"', any('强制结束' in m[1] for m in msgs), str(msgs))

# ---------- 场景 3：打开后立即退出 → 清锁重试 ----------
state.update({'running': False, 'locks': 0, 'popen': 0})
W.is_dsh_running = lambda: False
W.clear_dsh_locks = lambda: (state.__setitem__('locks', state['locks'] + 1) or 1)

class FakeProc:
    def __init__(self, alive): self._alive = alive
    def poll(self): return None if self._alive else 0

popen_results = [FakeProc(False), FakeProc(True)]   # 第一次秒退，第二次活
class FakeSubprocess:
    @staticmethod
    def Popen(*a, **kw):
        state['popen'] += 1
        return popen_results[min(state['popen'] - 1, 1)]

real_time_sleep = time.sleep
W.time = None
import types
# 用 sleep 的替身（避免真等 3 秒）
orig_sleep = time.sleep
time.sleep = lambda s: None
msgs.clear()
# 让 open_dsh 用的 subprocess 变成 fake
real_subprocess = __import__('subprocess')
import subprocess as sp_mod
orig_popen = sp_mod.Popen
sp_mod.Popen = FakeSubprocess.Popen
try:
    pet.open_dsh()
finally:
    sp_mod.Popen = orig_popen
    time.sleep = orig_sleep

check('第一次启动失败后重试', state['popen'] == 2, f"popen={state['popen']}")
check('重试前清理了锁', state['locks'] == 1)
check('第二次成功后不再报错', not any('打开 DSH 失败' in m[0] for m in msgs), str(msgs))

pet.close()
print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
