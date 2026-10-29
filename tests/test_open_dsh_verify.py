"""验证 open_dsh 的异步启动校验（用假进程，不启动真实 DSH）。"""
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
msgs = []
pet.bubble.show_message = lambda t, m, *a, **kw: msgs.append((t, m))


class FakeProc:
    def __init__(self, rc): self._rc = rc
    def poll(self): return self._rc


# 1) 进程存活 → 不提示
msgs.clear()
pet._verify_dsh_started(FakeProc(None))
check('存活时不打扰', len(msgs) == 0, str(msgs))

# 2) 退出码 0 → 单实例提示
msgs.clear()
pet._verify_dsh_started(FakeProc(0))
check('退出码0 有提示', len(msgs) == 1)
check('退出码0 提示含"单实例"', '单实例' in msgs[0][1], msgs[0][1] if msgs else '')

# 3) 其他退出码 → 带码提示
msgs.clear()
pet._verify_dsh_started(FakeProc(9))
check('退出码9 有提示', len(msgs) == 1)
check('退出码9 提示含码', '9' in msgs[0][1], msgs[0][1] if msgs else '')

# 4) poll 抛异常 → 静默
msgs.clear()
class BadProc:
    def poll(self): raise RuntimeError("boom")
pet._verify_dsh_started(BadProc())
check('poll 异常时静默', len(msgs) == 0)

pet.close()
print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
