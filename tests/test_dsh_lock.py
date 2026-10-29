"""验证单实例锁清理逻辑（不触碰真实 DSH）。"""
import os
import sys
import tempfile
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

# 用临时目录模拟 APPDATA
tmp = tempfile.mkdtemp()
fake_appdata = os.path.join(tmp, "AppData", "Roaming")
lockdir = os.path.join(fake_appdata, "@deepseek-ai", "dsh-desktop")
os.makedirs(lockdir, exist_ok=True)
for n in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
    open(os.path.join(lockdir, n), "w").close()

orig_env = os.environ.get("APPDATA")
os.environ["APPDATA"] = fake_appdata

# 1) 清理锁
removed = pet._clear_dsh_locks()
check('清锁返回 4 个', len(removed) == 4, str(removed))
left = [f for f in os.listdir(lockdir) if f.startswith(("lockfile", "Singleton"))]
check('锁文件已删除', len(left) == 0, str(left))

# 2) 无锁时再清一次不报错
removed2 = pet._clear_dsh_locks()
check('重复清理不报错', removed2 == [], str(removed2))

# 3) 清理不存在的目录不崩
os.environ["APPDATA"] = os.path.join(tmp, "nonexistent")
try:
    r = pet._clear_dsh_locks()
    check('目录不存在不崩', r == [])
except Exception as e:
    check('目录不存在不崩', False, str(e))

# 4) 轮询：DSH 不在跑 → 立即清锁
os.environ["APPDATA"] = fake_appdata
for n in ("lockfile",):
    open(os.path.join(lockdir, n), "w").close()
W.is_dsh_running = lambda: False
pet._wait_dsh_gone_tries = 0
pet._poll_dsh_gone()
check('进程已退出时清锁', not os.path.exists(os.path.join(lockdir, "lockfile")))

# 5) 轮询：DSH 还在跑 → 继续等（不崩）
W.is_dsh_running = lambda: True
pet._wait_dsh_gone_tries = 0
try:
    pet._poll_dsh_gone()
    check('进程存活时继续轮询不崩', True)
except Exception as e:
    check('进程存活时继续轮询不崩', False, str(e))

# 恢复
if orig_env is not None:
    os.environ["APPDATA"] = orig_env
else:
    os.environ.pop("APPDATA", None)

pet.close()
print(f"\n{sum(1 for _, c in results if c)}/{len(results)} passed")
sys.exit(0 if all(c for _, c in results) else 1)
