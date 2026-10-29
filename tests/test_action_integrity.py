"""验证"完整播放"保护：
1. 非交互 force_action 在动作未播完时只排队，不打断
2. 交互 force_action(interrupt=True) 立即打断
3. 排队动作在动作播完后被消费
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
        return None


pet = PetWindow(BASE, cfg, Store())
pet.show()

# 手工构造一个"正在播放"的动作：200 帧 × 40ms = 8 秒
pet.pms = [None] * 200
pet._total_frames = 200
pet.frame_ms = 40.0
pet.current_name = 'FAKE_A'
pet._mark_action_start(200)
pet.frame_idx = 10          # 播到第 10 帧（远未播完）
pet._loading_new = False

results = []
def check(name, cond, detail=''):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))

calls = []
orig_req = pet._request_frames
def spy_req(name, cat, msg='', restart=False):
    calls.append((name, restart))
    return orig_req(name, cat, msg, restart)
pet._request_frames = spy_req

# ---------- 1) 非交互触发应排队 ----------
pet.force_action('WS_ANIM')          # 默认 interrupt=False
check('non-interactive queued (no switch)', len(calls) == 0,
      f"calls={calls}")
check('queued_action set', pet._queued_action == ('WS_ANIM', ''))

# ---------- 2) 交互触发应立即打断 ----------
pet.force_action('CLICK_ANIM', interrupt=True)
check('interactive switches immediately', len(calls) == 1 and calls[0][0] == 'CLICK_ANIM',
      f"calls={calls}")
check('queue cleared by interrupt', pet._queued_action is None)

# ---------- 3) load_next_action 在未播完时只记待续 ----------
pet._mark_action_start(200)     # 重新计时，确保仍未播完
pet._pending_next = False
before = len(calls)
pet.load_next_action()
check('load_next_action deferred', len(calls) == before and pet._pending_next is True)

# ---------- 4) 动作播完后，ended 分支应消费队列 ----------
pet._action_t0 = time.monotonic() - 100.0   # 假装已播完
pet._queued_action = ('NEXT_ANIM', '')
pet._ws_state = None
pet._pending_ws = None
pet._pending_next = False
pet._last_action_switch_t = 0.0
calls.clear()
# 让 advance 直接返回 ended，驱动 tick 的播完分支
pet.advance = lambda ms: 'ended'
pet.pms = [object()]
pet._last_tick_t = time.monotonic() - 0.016   # 非零，否则首次 tick 直接 return
pet.tick()
check('ended consumes queued action', any(c[0] == 'NEXT_ANIM' for c in calls),
      f"calls={calls}")

print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
