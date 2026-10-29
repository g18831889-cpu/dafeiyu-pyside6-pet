"""单体模式硬碰撞 —— 窗口层端到端验证（offscreen，真 PetWindow）。

    python tests/test_single_collide_window.py

验证 window.py 的接线是否正确：
  1. 单体鱼 collide_participates() = True，群体鱼 = False；
  2. 拖拽中 / 物理飞行中 一律不参与；
  3. body_aabb / apply_body_center 往返一致；
  4. _resolve_move_with_collision 撞到人时真的把对方顶开；
  5. _settle_single_collisions 会把残余重叠推开；
  6. 群体鱼完全不参与（不推动别人、也不被别人推）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import src.runtime.window as W
from src.runtime.window import PetWindow, BODY_BOX, _settle_single_collisions
from src.host.config_loader import load_config

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


cfg = load_config(PROJ / "frames", PROJ / "config.jsonc")

# 清空全局表，避免受已有窗口影响
W.ALL_PETS.clear()

base = PROJ / "frames"
if not base.exists():
    print("frames 目录不存在，跳过（这是部署目录才有的素材）")
    sys.exit(0)


def make_pet(flock=False):
    p = PetWindow(base, cfg, FakeStore(), scale=0.75)
    p.flock_enabled = flock
    p.show()
    return p


print("\n=== 1. 参与碰撞的判定 ===")
s1 = make_pet(flock=False)
s2 = make_pet(flock=False)
g1 = make_pet(flock=True)
check("单体鱼参与", s1.collide_participates() is True)
check("群体鱼不参与", g1.collide_participates() is False)

print("\n=== 2. 拖拽 / 飞行豁免 ===")
s1.drag_on = True
check("拖拽中不参与", s1.collide_participates() is False)
s1.drag_on = False
check("松手后恢复参与", s1.collide_participates() is True)

print("\n=== 3. 身体框往返 ===")
s1.apply_body_center(800.0, 500.0)
cx, cy, hw, hh = s1.body_aabb()
check("中心写入后读回一致", abs(cx - 800.0) < 1.0 and abs(cy - 500.0) < 1.0,
      f"({cx:.1f},{cy:.1f})")
s = 0.75
check("半尺寸按 scale 换算",
      abs(hw - BODY_BOX[2] * s / 2.0) < 1e-6 and abs(hh - BODY_BOX[3] * s / 2.0) < 1e-6,
      f"({hw:.2f},{hh:.2f})")

print("\n=== 4. 撞到人 → 对方被顶开 ===")
body_w = BODY_BOX[2] * s
# ★ offscreen 的虚拟屏只有 800×800，位置必须落在屏内，
#   否则会被 clamp_body_center 夹到边缘、推不动（那是环境限制，不是 bug）。
s1.apply_body_center(200.0, 400.0)
s2.apply_body_center(200.0 + body_w, 400.0)
before2 = s2.body_aabb()[0]
cxa, cya, hwa, hha = s1.body_aabb()
nx, ny = s1._resolve_move_with_collision(cxa, cya, cxa + 120.0, cya)
after2 = s2.body_aabb()[0]
check("撞人者能前进一些", nx > cxa + 1.0, f"dx={nx - cxa:.1f}")
check("被撞者被顶开", after2 > before2 + 1.0, f"dx={after2 - before2:.1f}")
gap = s2.body_aabb()[0] - s1.body_aabb()[0]
check("碰后不重叠", gap >= body_w - 1.5, f"gap={gap:.1f} / need={body_w:.1f}")

print("\n=== 5. 全局兜底分离 ===")
s1.apply_body_center(200.0, 400.0)
s2.apply_body_center(200.0 + body_w * 0.4, 400.0)   # 故意叠在一起
for _ in range(40):
    _settle_single_collisions()
gap = abs(s2.body_aabb()[0] - s1.body_aabb()[0])
check("多帧兜底后不重叠", gap >= body_w - 1.5, f"gap={gap:.1f}")

print("\n=== 6. 群体鱼完全不参与 ===")
g1.apply_body_center(400.0, 700.0)
s1.apply_body_center(400.0, 700.0)          # 与群体鱼完全重合
before_g = g1.body_aabb()[0]
cxa, cya, _, _ = s1.body_aabb()
s1._resolve_move_with_collision(cxa, cya, cxa + 100.0, cya)
check("群体鱼没被推动", abs(g1.body_aabb()[0] - before_g) < 1e-6,
      f"dx={g1.body_aabb()[0] - before_g:.2f}")
# 兜底也不该动群体鱼
before_g = g1.body_aabb()[0]
for _ in range(10):
    _settle_single_collisions()
check("兜底也不动群体鱼", abs(g1.body_aabb()[0] - before_g) < 1e-6,
      f"dx={g1.body_aabb()[0] - before_g:.2f}")

print("\n=== 7. 群体鱼不会挡单体鱼的路 ===")
W.ALL_PETS.clear()
s3 = make_pet(flock=False)
g2 = make_pet(flock=True)
s3.apply_body_center(600.0, 500.0)
g2.apply_body_center(600.0 + body_w, 500.0)   # 正好挡在前面
cxa, cya, _, _ = s3.body_aabb()
nx, ny = s3._resolve_move_with_collision(cxa, cya, cxa + 150.0, cya)
check("单体鱼穿过群体鱼不被挡", abs(nx - (cxa + 150.0)) < 1e-6, f"dx={nx - cxa:.1f}")

for p in list(W.ALL_PETS):
    try:
        p.close()
    except Exception:
        pass

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
