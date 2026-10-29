"""验证 2026-10-30 的四项修改。

    python tests/test_sway_and_perf.py

1. 摇晃开关（全局）
2. 群体子体零影响（不倾斜、不参与任何新逻辑）
3. 抗抽动（死区 + 边缘渐变）
4. 无变换路径用缩放缓存（性能）
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
from src.runtime.window import (PetWindow, tilt_sway_enabled, set_tilt_sway,
                                _SWAY_ENABLED)
from src.host.config_loader import load_config
from src.shared.tilt import TiltState, TILT_DEADZONE, TILT_RADIUS

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


class FakeStore:
    def get(self, k, d=None):
        return d

    def set(self, k, v):
        pass

    def snapshot(self):
        return {}

    def get_state(self):
        return {'counter': 0, 'workStatus': None}


cfg = load_config(PROJ / "frames", PROJ / "config.jsonc")
W.ALL_PETS.clear()
base = PROJ / "frames"

if not base.exists():
    print("frames 不存在，跳过")
    sys.exit(0)


def make(flock=False, main=False):
    p = PetWindow(base, cfg, FakeStore(), scale=0.75)
    p.flock_enabled = flock
    p.show()
    if main:
        p.is_main = True
        p.spawn_idx = 0
    else:
        p.is_main = False
        if p.spawn_idx == 0:
            p.spawn_idx = 1
    return p


print("\n=== 1. 摇晃开关 ===")
_SWAY_ENABLED[0] = True
check("默认开启", tilt_sway_enabled() is True)

s = make()
check("开启时单体主体可晃", s.tilt_enabled() is True)

set_tilt_sway(False)
check("关闭后 tilt_sway_enabled=False", tilt_sway_enabled() is False)
check("关闭后 tilt_enabled=False", s.tilt_enabled() is False)

set_tilt_sway(True)
check("重新开启后可晃", s.tilt_enabled() is True)

print("\n=== 2. 群体子体完全不受影响 ===")
_SWAY_ENABLED[0] = True
g_kid = make(flock=True, main=False)
check("群体子体 tilt_enabled=False", g_kid.tilt_enabled() is False)

# 强制给倾斜值，_tick_tilt 应归零
g_kid.tilt.cur = 1.0
g_kid.tilt.tgt = 1.0
g_kid._tick_tilt()
check("群体子体 tilt 被归零", abs(g_kid.tilt.cur) < 1e-9, f"cur={g_kid.tilt.cur}")

print("\n=== 3. 群体主体可晃 ===")
g_main = make(flock=True, main=True)
check("群体主体 tilt_enabled=True", g_main.tilt_enabled() is True)

print("\n=== 4. 抗抽动：死区 ===")
RECT = (200.0, 200.0, 600.0, 500.0)
CX = (RECT[0] + RECT[2]) / 2.0
CY = (RECT[1] + RECT[3]) / 2.0

t = TiltState()
base_x = CX + 100.0
changes = 0
for i in range(300):
    jitter = (i % 3) - 1
    if t.update_target(base_x + jitter, CY, RECT):
        changes += 1
# 2026-10-30：死区改成「滞回 + 同向直通」后，方向反转时允许少量更新，
# 所以阈值放宽到 5（±1px 抖动 300 次，实测约 1~3 次，远小于 300）
check("鼠标 ±1px 微抖几乎不触发目标变化", changes <= 5, f"{changes} 次")

print("\n=== 5. 抗抽动：边缘渐变（无硬跳） ===")
t2 = TiltState()
t2.update_target(CX + 150, CY, RECT)
v_in = t2.tgt
# 刚出边界
t2.update_target(RECT[2] + TILT_RADIUS + 5, CY, RECT)
v_out = t2.tgt
check("出边界后归零", abs(v_out) < 1e-9, f"in={v_in:.3f} out={v_out:.3f}")
# 边缘附近应该是渐变的（不是满值）
t3 = TiltState()
t3.update_target(RECT[2] - 5, CY, RECT)
v_edge = t3.tgt
check("接近边缘时值被衰减（非满值）", abs(v_edge) < 0.9,
      f"edge={v_edge:.3f}")

print("\n=== 6. 无变换路径使用缩放缓存 ===")
p = make()
# 塞一帧
try:
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import Qt as _Qt
    for _d in (PROJ / "frames").iterdir():
        if _d.is_dir() and "待机" in _d.name:
            _fs = sorted(_d.glob("*.webp"))
            if _fs:
                pix = QPixmap(str(_fs[0]))
                p.cur_pix = pix.toImage()      # 原图，不缩放
                break
except Exception:
    pass

if p.cur_pix is not None:
    pw = int(p.cur_pix.width() * p.scale)
    ph = int(p.cur_pix.height() * p.scale)
    p._paint_scale_cache = {}
    a = p._scaled_frame_for_paint(pw, ph)
    b = p._scaled_frame_for_paint(pw, ph)
    check("首次生成缩放图", a is not None)
    check("第二次命中缓存（同一对象）", a is b)
    check("缓存尺寸正确", a.width() == pw and a.height() == ph,
          f"{a.width()}x{a.height()}")

print("\n=== 7. 性能：tick 开销 ===")
p2 = make(flock=True, main=False)     # 群体子体，不该有任何倾斜开销
t0 = time.perf_counter()
for _ in range(2000):
    p2._tick_tilt()
dt = (time.perf_counter() - t0) / 2000 * 1e6
check("群体子体 _tick_tilt 极快（直接返回）", dt < 20.0, f"{dt:.2f} us")

for pp in list(W.ALL_PETS):
    try:
        pp.close()
    except Exception:
        pass

_SWAY_ENABLED[0] = True

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
