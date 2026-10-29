"""验证 bring_to_front 与气泡双击（不依赖真实窗口管理器）。"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

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
pet.show()
app.processEvents()

# 1) bring_to_front 可调用且不抛异常
try:
    pet.bring_to_front()
    check('bring_to_front 不抛异常', True)
except Exception as e:
    check('bring_to_front 不抛异常', False, str(e))

# 2) 调用后窗口仍可见
check('调用后仍可见', pet.isVisible())

# 3) 气泡双击转发给桌宠
called = {'n': 0}
orig = pet.bring_to_front
def spy():
    called['n'] += 1
    return orig()
pet.bring_to_front = spy

from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt, QPointF
ev = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick,
                 QPointF(10, 10), QPointF(10, 10),
                 Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
pet.bubble.mouseDoubleClickEvent(ev)
check('气泡双击触发置顶', called['n'] == 1, f"n={called['n']}")

# 4) 气泡未绑定 parent 时不崩
from src.runtime.bubble import BubbleWindow
orphan = BubbleWindow(None)
try:
    orphan.mouseDoubleClickEvent(ev)
    check('无 parent 气泡不崩', True)
except Exception as e:
    check('无 parent 气泡不崩', False, str(e))
orphan.close()

# 5) 多开时一起置顶（不崩即可）
try:
    pet.bring_to_front()
    check('多开场景不崩', True)
except Exception as e:
    check('多开场景不崩', False, str(e))

pet.close()
print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
