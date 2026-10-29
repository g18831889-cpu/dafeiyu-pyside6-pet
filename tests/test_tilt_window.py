"""倾斜跟随的窗口层验证 —— 重点验证「作用域约束」。

    python tests/test_tilt_window.py

最终口径（2026-10-29 用户确认）：
  **倾斜限制在单体模式**，主体和子体都启用。
  逐条验证：
  1. 单体模式的主体        → True   ★ 启用
  2. 单体模式的子体        → True   ★ 启用
  3. 群体模式的主体        → False  ← 群体不倾斜
  4. 群体模式的子体        → False  ← 群体不倾斜
  5. 拖拽中 / 飞行中       → False
  6. 隐藏                   → False
  7. 群体主体不施加变换（几何不变）
  8. 倾斜后角色本体不越出窗口（不裁剪）
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
from src.runtime.window import PetWindow, BODY_BOX
from src.host.config_loader import load_config
from src.shared.tilt import TiltState, TILT_MAX_DEG

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


print("\n=== 1. 单体模式：主体启用 ===")
m = make(flock=False, main=True)
check("单体主体 tilt_enabled = True", m.tilt_enabled() is True)
check("单体主体 is_main 正确", m.is_main is True)

print("\n=== 2. 单体模式：子体启用 ===")
s1 = make(flock=False, main=False)
check("单体子体 tilt_enabled = True", s1.tilt_enabled() is True)
check("单体子体 is_main = False", s1.is_main is False)

print("\n=== 3. 群体模式：主体倾斜（2026-10-30 新增）===")
gm = make(flock=True, main=True)
check("群体主体 tilt_enabled = True", gm.tilt_enabled() is True)

print("\n=== 4. 群体模式：子体不倾斜 ===")
g1 = make(flock=True, main=False)
check("群体子体 tilt_enabled = False", g1.tilt_enabled() is False)

print("\n=== 5. 拖拽 / 飞行豁免 ===")
s2 = make(flock=False, main=False)
s2.drag_on = True
check("拖拽中 = False", s2.tilt_enabled() is False)
s2.drag_on = False
check("松手后恢复 = True", s2.tilt_enabled() is True)

print("\n=== 6. 隐藏时不动 ===")
s3 = make(flock=False, main=False)
s3.hide()
check("隐藏时 = False", s3.tilt_enabled() is False)
s3.show()

print("\n=== 7. 群体子体的倾斜被强制归零 ===")
g1.tilt.cur = 1.0
g1.tilt.tgt = 1.0
g1._tick_tilt()
check("群体子体 tilt 被归零", abs(g1.tilt.cur) < 1e-9, f"cur={g1.tilt.cur}")
check("群体子体 tilt 是 identity", g1.tilt.is_identity() is True)

print("\n=== 7b. 群体主体的倾斜生效（不被归零）===")
gm.tilt.cur = 0.0
gm.tilt.tgt = 0.0
_g = gm.frameGeometry()
gm.tilt.update_target(float(_g.right()) + 100.0, float(_g.center().y()),
                      (float(_g.left()), float(_g.top()),
                       float(_g.right()), float(_g.bottom())))
check("群体主体能算出非零目标", abs(gm.tilt.tgt) > 0.1, f"tgt={gm.tilt.tgt:.2f}")
gm.tilt.step()
check("群体主体 cur 开始变化", abs(gm.tilt.cur) > 1e-6, f"cur={gm.tilt.cur:.4f}")

print("\n=== 8. 单体主体倾斜生效（不被归零）===")
m.tilt.cur = 0.0
m.tilt.tgt = 0.0
# 模拟光标在窗口右侧边缘，制造非零目标
g = m.frameGeometry()
m.tilt.update_target(float(g.right()) + 100.0, float(g.center().y()),
                     (float(g.left()), float(g.top()),
                      float(g.right()), float(g.bottom())))
check("单体主体能算出非零目标", abs(m.tilt.tgt) > 0.1, f"tgt={m.tilt.tgt:.2f}")
m.tilt.step()
check("单体主体 cur 开始变化", abs(m.tilt.cur) > 1e-6, f"cur={m.tilt.cur:.4f}")

print("\n=== 9. 倾斜后角色本体不越界 ===")
s4 = make(flock=False, main=False)
import math
s4.tilt.cur = 1.0
s4.tilt.tgt = 1.0
W_, H_ = s4.width(), s4.height()
s = s4.scale
bx, by, bw, bh = BODY_BOX
cx0, cy0 = bx * s, by * s
cx1, cy1 = (bx + bw) * s, (by + bh) * s
pivot_y = H_ * 0.917
ang = math.radians(TILT_MAX_DEG)
off = 5.0
worst = 0.0
for cx in (cx0, cx1):
    for cy in (cy0, cy1):
        nx = W_ / 2 + (cx - W_ / 2) * math.cos(ang) - (cy - pivot_y) * math.sin(ang) + off
        ny = pivot_y + (cx - W_ / 2) * math.sin(ang) + (cy - pivot_y) * math.cos(ang)
        worst = max(worst, -nx, nx - W_, -ny, ny - H_)
check("角色本体越界量 <= 0", worst <= 0.5, f"最大越界 {worst:.2f}px")
check("→ 不需要加宽窗口", worst <= 0.5)

print("\n=== 10. 倾斜值到角度/位移的映射 ===")
t = s4.tilt
t.cur = 1.0
check("满值 → 3.5°", abs(t.angle - 3.5) < 1e-9, f"{t.angle}")
check("满值 → 5px", abs(t.offset_x - 5.0) < 1e-9, f"{t.offset_x}")
t.cur = 0.5
check("半值 → 1.75°", abs(t.angle - 1.75) < 1e-9, f"{t.angle}")

print("\n=== 11. 变换原点 = 脚底 ===")
check("pivot_y = 高度 × 0.917",
      abs(s4._tilt_pivot_y() - H_ * 0.917) < 1e-9,
      f"{s4._tilt_pivot_y():.2f}")

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
