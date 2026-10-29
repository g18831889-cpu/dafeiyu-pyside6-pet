"""验证：切换大小时**立即**写盘（不依赖退出），且行为模式不持久化。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from src.runtime.window import PetWindow
from src.host.config_loader import load_config
from src.host.user_settings import load, save, settings_path

fake_root = Path(tempfile.mkdtemp())
fake_frames = fake_root / 'frames'
fake_frames.mkdir()

real_frames = PROJ / 'frames'
cfg = load_config(real_frames, PROJ / 'config.jsonc')

results = []
def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))


pet = PetWindow(fake_frames, cfg, None)
pet.show()
app.processEvents()
check('is main pet', pet.is_main is True)
check('no file before change', not settings_path(fake_root).exists())

# 1) 切换大小 → 立即写
pet.set_scale(0.45)
check('file created on set_scale', settings_path(fake_root).exists())
d = load(fake_root)
check('scale persisted immediately', abs(float(d['scale']) - 0.45) < 1e-9, str(d['scale']))

# 2) 再切一次
pet.set_scale(1.0)
check('scale updated immediately', abs(float(load(fake_root)['scale']) - 1.0) < 1e-9)

# 3) 文件里只有 scale（行为不存）
raw = json.loads(settings_path(fake_root).read_text(encoding='utf-8'))
check('only scale stored', list(raw.keys()) == ['scale'], str(raw))
check('no flock in file', 'flock_enabled' not in raw)

# 4) 切换行为不写盘
before_mtime = settings_path(fake_root).stat().st_mtime_ns
pet.toggle_flock(True)
after_mtime = settings_path(fake_root).stat().st_mtime_ns
check('behavior toggle does not write', before_mtime == after_mtime)
pet.toggle_flock(False)

# 5) 多开个体不写盘
kid = PetWindow(fake_frames, cfg, None, scale=0.6)
kid.show()
app.processEvents()
check('kid is not main', kid.is_main is False)
b = load(fake_root)
kid.set_scale(0.3)
a = load(fake_root)
check('kid does not persist', abs(float(a['scale']) - float(b['scale'])) < 1e-9)

# 6) 重启读回
pet2 = PetWindow(fake_frames, cfg, None, scale=load(fake_root)['scale'])
check('restart restores scale', abs(pet2.scale - 1.0) < 1e-9, f"scale={pet2.scale}")

# 7) 损坏文件回退
settings_path(fake_root).write_text('{broken', encoding='utf-8')
check('corrupt falls back', abs(float(load(fake_root)['scale']) - 0.75) < 1e-9)

for p in (pet, kid, pet2):
    try: p.close()
    except Exception: pass

passed = sum(1 for _, c, _ in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
