"""验证双击查余额：300ms 窗口、子体转发主体、无 DSH 降级、拖拽不触发。"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt, QPointF

app = QApplication(sys.argv)

from src.runtime.window import PetWindow, DOUBLE_CLICK_MS
from src.host.config_loader import load_config

import src.runtime.window as _W

# ★ 本测试会模拟双击 → 会触发 bring_dsh_to_front()，那是**真实的 Win32 调用**，
#   跑测试时会把用户的 DSH 窗口拽到前台。这里换成桩，避免打扰。
#   （本测试只关心"余额查询"链路，不关心提前台本身）
_DID_BRING = {'n': 0}


def _fake_bring():
    _DID_BRING['n'] += 1
    return False, ""


_W.bring_dsh_to_front = _fake_bring

BASE = PROJ / 'frames'
cfg = load_config(BASE, PROJ / 'config.jsonc')

results = []
def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))


class FakeStore:
    """可控的余额快照。"""
    def __init__(self, snap):
        self.snap = snap
    def get_balance(self):
        return self.snap
    def get_state(self):
        return {'counter': 0, 'workStatus': None}


def make_pet(store):
    p = PetWindow(BASE, cfg, store)
    p.show()
    app.processEvents()
    p.tip.hide(); p.bubble.hide()
    # 开发目录 clicks 池可能为空，注入假的只测分支走向
    p.raw_anims['clicks'] = ['FAKE_CLICK']
    return p


ready_store = FakeStore({'status': 'ready', 'balances': [{'total': '12.34'}]})
empty_store = FakeStore(None)

calls = {'action': 0, 'balance': 0}

def instrument(p):
    def spy_force(name, *a, **kw):
        calls['action'] += 1
    def spy_bal(*a, **kw):
        calls['balance'] += 1
    p.force_action = spy_force
    p.check_balance = spy_bal


def press_release(p, gap=None, gx=500, gy=500):
    """模拟一次松手（moved<15）。gap=None 时先重置计时保证算"第一击"。"""
    if gap is None:
        p._last_click_t = 0.0
        time.sleep(DOUBLE_CLICK_MS / 1000.0 + 0.05)
    p.press_pos = QPointF(gx - 2, gy - 2).toPoint()
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(p.rect().center()),
                     QPointF(gx, gy), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    p.drag_on = True
    p.mouseReleaseEvent(ev)


# ================= 常量 =================
check('window is 300ms', DOUBLE_CLICK_MS == 300, str(DOUBLE_CLICK_MS))

# ================= 有 DSH =================
pet = make_pet(ready_store)
instrument(pet)

# 单击 → 动作
press_release(pet)
c = dict(calls)
check('single click -> action', calls['action'] == c['action'])
check('single click -> no balance', calls['balance'] == 0)

# 双击（间隔 120ms）→ 余额
press_release(pet)
time.sleep(0.12)
press_release(pet, gap=999)
check('double click -> balance', calls['balance'] == 1, f"balance={calls['balance']}")
check('double click -> 1st click still action', calls['action'] == 2, f"action={calls['action']}")

# 三击 → 不再弹余额
c = dict(calls)
time.sleep(0.12)
press_release(pet, gap=999)
check('triple click -> no extra balance', calls['balance'] == c['balance'])
check('triple click -> 3rd is action', calls['action'] == c['action'] + 1)

# 250ms 间隔（在窗口内）→ 双击生效
c = dict(calls)
press_release(pet)
time.sleep(0.25)
press_release(pet, gap=999)
check('250ms gap -> double click', calls['balance'] == c['balance'] + 1,
      f"balance={calls['balance']}")

# 320ms 间隔（超窗口）→ 两次单击
c = dict(calls)
press_release(pet)
time.sleep(0.32)
press_release(pet, gap=999)
check('320ms gap -> two singles', calls['balance'] == c['balance'] and calls['action'] == c['action'] + 2,
      f"balance={calls['balance'] - c['balance']} action+{calls['action'] - c['action']}")

# 拖拽 → 不触发点击
c = dict(calls)
pet.press_pos = QPointF(100, 100).toPoint()
ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(pet.rect().center()),
                 QPointF(300, 300), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
pet.drag_on = True
pet.mouseReleaseEvent(ev)
check('drag -> no click branch', calls['balance'] == c['balance'] and calls['action'] == c['action'])

# ================= 无 DSH：降级为动作 =================
pet2 = make_pet(empty_store)
instrument(pet2)
c = dict(calls)
press_release(pet2)
time.sleep(0.12)
press_release(pet2, gap=999)
check('no DSH -> double click falls back to action', calls['action'] == c['action'] + 2,
      f"action+{calls['action'] - c['action']}")
check('no DSH -> no balance call', calls['balance'] == c['balance'])
check('no DSH -> no "未连接" bubble', not pet2.bubble.isVisible())

# ================= 子体双击 → 主体弹余额 =================
kid = make_pet(ready_store)
instrument(kid)
# 主体用独立计数
main_calls = {'balance': 0}
orig_main_bal = pet.check_balance
pet.check_balance = lambda *a, **kw: (main_calls.__setitem__('balance', main_calls['balance'] + 1))

c = dict(calls)
press_release(kid)
time.sleep(0.12)
press_release(kid, gap=999)
check('kid double click -> main shows balance', main_calls['balance'] == 1,
      f"main balance={main_calls['balance']}")
check('kid itself does not call check_balance', calls['balance'] == c['balance'],
      f"kid balance={calls['balance']}")

pet.check_balance = orig_main_bal
for p in (pet, pet2, kid):
    try: p.close()
    except Exception: pass

passed = sum(1 for _, c2, _ in results if c2)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
