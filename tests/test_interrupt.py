"""复现：工作状态 / 余额动作是否被中途打断。

记录每次动作切换的时间、名称、帧数，检测"没播完就换"的情况。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)

from src.runtime.window import PetWindow
from src.host.config_loader import load_config

BASE = PROJ / 'frames'
cfg = load_config(BASE, PROJ / 'config.jsonc')


class Store:
    def __init__(self):
        self.counter = 0
        self.ws = None
    def get_state(self):
        return {'counter': self.counter, 'workStatus': self.ws}
    def get_balance(self):
        return {'status': 'ready', 'balances': [{'total': '12.34'}]}


store = Store()
pet = PetWindow(BASE, cfg, store)
pet.show()

events = []

# 拦截 force_action / _request_frames，记录切换
orig_fa = pet.force_action
def spy_fa(name, msg='', **kw):
    events.append(('force_action', name, round(time.monotonic() - T0, 2)))
    return orig_fa(name, msg, **kw)
pet.force_action = spy_fa

# 记录每次动作实际播放的帧数
orig_adv = pet.advance
frames_played = {'n': 0}
def spy_adv(ms):
    r = orig_adv(ms)
    if r == 'ended':
        events.append(('ended', pet.current_name, round(time.monotonic() - T0, 2),
                       len(pet.pms)))
    return r
pet.advance = spy_adv

T0 = time.monotonic()

# 模拟工作状态高频推送（每 60ms 一次 counter++）
def push_ws():
    store.counter += 1
    store.ws = 1

ws_timer = QTimer()
ws_timer.timeout.connect(push_ws)
ws_timer.start(60)

# 2 秒后模拟双击（触发余额动作）
def do_balance():
    events.append(('DOUBLE_CLICK_BALANCE', '', round(time.monotonic() - T0, 2)))
    pet.check_balance()

QTimer.singleShot(2000, do_balance)

# 跑 8 秒
def finish():
    app.quit()
QTimer.singleShot(8000, finish)

app.exec()

print("\n=== 时间线 ===")
for e in events:
    print('  ', e)

# 检测：force_action 后、ended 前，是否又被 force_action
print("\n=== 中途打断检测 ===")
started = None
interrupted = 0
for e in events:
    if e[0] == 'force_action':
        if started is not None:
            print(f"  ⚠ {started[1]} (t={started[2]}) 未播完就被 {e[1]} (t={e[2]}) 替换")
            interrupted += 1
        started = e
    elif e[0] == 'ended':
        started = None
print(f"\n中途被打断次数: {interrupted}")
