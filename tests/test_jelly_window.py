"""拖拽 Q 弹的窗口层验证。

    python tests/test_jelly_window.py

验证：
  1. 拖拽时按速度产生拉伸，且方向跟随运动
  2. 拖拽期间倾斜被关闭（与 whale 一致，避免两个旋转叠加）
  3. 松手触发 settle，且拉伸被清零
  4. 单击触发 pop
  5. 形变不改变窗口几何（只影响绘制）
  6. 群体模式下也照常 Q 弹（Q 弹不限模式，倾斜才限）
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
from src.runtime.window import PetWindow
from src.host.config_loader import load_config
from src.shared.jelly import SETTLE_SX0, SETTLE_SY0, POP_DURATION, SETTLE_DURATION

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


print("\n=== 1. 拖拽产生拉伸 ===")
p = make()
# 模拟快速向右拖拽 1000px/s（每 16ms 走 16px）
t0 = time.monotonic() * 1000.0
for i in range(80):
    p.jelly.drag.update(float(i * 16), 0.0, t0 + i * 16.0)
check("拖拽中 drag.active", p.jelly.drag.active is True, f"s={p.jelly.drag.s:.4f}")
check("拉伸量接近 0.09", abs(p.jelly.drag.s - 0.09) < 0.01, f"s={p.jelly.drag.s:.4f}")
check("横向放大", p.jelly.drag.scale_x > 1.05, f"{p.jelly.drag.scale_x:.3f}")
check("纵向压缩", p.jelly.drag.scale_y < 0.97, f"{p.jelly.drag.scale_y:.3f}")
check("方向朝右（≈0）", abs(p.jelly.drag_angle()) < 0.05, f"{p.jelly.drag_angle():.3f}")

print("\n=== 2. 拖拽期间倾斜关闭 ===")
p.drag_on = True
check("拖拽中 tilt_enabled=False", p.tilt_enabled() is False)
p.drag_on = False
check("松手后 tilt_enabled=True", p.tilt_enabled() is True)

print("\n=== 3. 松手触发 settle ===")
p2 = make()
t0 = time.monotonic()
p2.jelly.drag.s = 0.10          # 模拟已有拉伸
p2.jelly.start_settle(t0)
check("settle 在运行", p2.jelly.settle.running is True)
check("拉伸被清零", abs(p2.jelly.drag.s) < 1e-9, f"{p2.jelly.drag.s}")
sx, sy = p2.jelly.combined_scale(t0)
check("起点横向 1.09", abs(sx - SETTLE_SX0) < 1e-6, f"{sx:.4f}")
check("起点纵向 0.90", abs(sy - SETTLE_SY0) < 1e-6, f"{sy:.4f}")
# 结束时回到 1
sx2, sy2 = p2.jelly.combined_scale(t0 + SETTLE_DURATION + 0.01)
check("结束回到 1", abs(sx2 - 1) < 1e-6 and abs(sy2 - 1) < 1e-6, f"({sx2:.3f},{sy2:.3f})")

print("\n=== 4. 单击触发 pop ===")
p3 = make()
t0 = time.monotonic()
p3.jelly.start_pop(t0)
check("pop 在运行", p3.jelly.pop.running is True)
sx, sy = p3.jelly.combined_scale(t0 + POP_DURATION * 0.30)
check("30% 处横向 >1", sx > 1.0, f"{sx:.4f}")
check("30% 处纵向 <1", sy < 1.0, f"{sy:.4f}")
check("结束后 pop.running=False",
      p3.jelly.pop.scales(t0 + POP_DURATION + 0.01) == (1.0, 1.0))

print("\n=== 5. 形变不改变窗口几何 ===")
p4 = make()
# 给 p4 同步塞一帧真实素材（异步加载线程在脚本里来不及）
try:
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import Qt as _Qt2
    for _d in (PROJ / "frames").iterdir():
        if _d.is_dir() and "待机" in _d.name:
            _fs = sorted(_d.glob("*.webp"))
            if _fs:
                _pix = QPixmap(str(_fs[0]))
                _s = p4.scale
                p4.cur_pix = _pix.scaled(int(_pix.width() * _s), int(_pix.height() * _s),
                                         _Qt2.KeepAspectRatio,
                                         _Qt2.SmoothTransformation).toImage()
                p4.pms = [p4.cur_pix]
                break
except Exception:
    pass
# 先停掉碰撞影响：清空其他宠物，避免被挤开
W.ALL_PETS.clear()
W.ALL_PETS.append(p4)
p4.update()
app.processEvents()
w0, h0 = p4.width(), p4.height()
x0, y0 = p4.x(), p4.y()
p4.jelly.drag.s = 0.16
p4.jelly.start_pop(time.monotonic())
p4.update()
app.processEvents()
check("窗口宽度不变", p4.width() == w0, f"{p4.width()} vs {w0}")
check("窗口高度不变", p4.height() == h0, f"{p4.height()} vs {h0}")
check("窗口位置不变", p4.x() == x0 and p4.y() == y0,
      f"({p4.x()},{p4.y()}) vs ({x0},{y0})")

print("\n=== 5b. 缩放锚点：横向不漂移（回归测试）===")
# 之前 bug：纯缩放只 translate y，导致 scale(sx>1) 以原点放大 → 图形右漂 22px
from PySide6.QtGui import QImage, QPainter
from PySide6.QtCore import Qt as _Qt


def _render_scaled(pet, sx, sy, anchor_x):
    """anchor_x=True 用新写法（translate x+y），False 用旧写法（只 y）。"""
    img = QImage(pet.width(), pet.height(), QImage.Format_ARGB32_Premultiplied)
    img.fill(_Qt.transparent)
    pr = QPainter(img)
    pr.setRenderHint(QPainter.SmoothPixmapTransform, True)
    pw, ph = pet.cur_pix.width(), pet.cur_pix.height()
    x = (pet.width() - pw) // 2
    y = (pet.height() - ph) // 2
    if anchor_x:
        px, py = float(x + pw / 2.0), float(y + ph)
        pr.translate(px, py)
        pr.scale(sx, sy)
        pr.translate(-px, -py)
    else:
        py = float(y + ph)
        pr.translate(0.0, py)
        pr.scale(sx, sy)
        pr.translate(0.0, -py)
    pr.drawImage(x, y, pet.cur_pix)
    pr.end()
    return img


def _bbox_cx(img):
    left, right = 10**9, -1
    for yy in range(0, img.height(), 2):
        for xx in range(0, img.width(), 2):
            if img.pixelColor(xx, yy).alpha() > 20:
                left = min(left, xx)
                right = max(right, xx)
    return (left + right) / 2.0 if right >= 0 else 0.0


p6 = p4
if p6.cur_pix is None:
    print("  （cur_pix 未加载，跳过锚点像素验证）")
else:
    base_cx = _bbox_cx(_render_scaled(p6, 1.0, 1.0, True))
    cx_old = _bbox_cx(_render_scaled(p6, 1.09, 0.90, False))
    cx_new = _bbox_cx(_render_scaled(p6, 1.09, 0.90, True))
    check("旧写法会横向漂移（复现 bug）", abs(cx_old - base_cx) > 10,
          f"漂移 {cx_old - base_cx:+.1f}px")
    check("新写法横向不漂移", abs(cx_new - base_cx) < 2.0,
          f"漂移 {cx_new - base_cx:+.1f}px")

print("\n=== 6. 群体模式也有 Q 弹（Q 弹不限模式）===")
g = make(flock=True)
t0 = time.monotonic()
g.jelly.start_pop(t0)
sx, sy = g.jelly.combined_scale(t0 + POP_DURATION * 0.30)
check("群体模式 pop 生效", sx > 1.0, f"{sx:.4f}")
check("但群体模式倾斜仍关闭", g.tilt_enabled() is False)

print("\n=== 7. 组合：拖拽 + 弹跳缩放相乘 ===")
p5 = make()
t0 = time.monotonic() * 1000.0
for i in range(80):
    p5.jelly.drag.update(float(i * 16), 0.0, t0 + i * 16.0)
p5.jelly.start_pop(time.monotonic())
now_s = time.monotonic() + POP_DURATION * 0.30
sx, sy = p5.jelly.combined_scale(now_s)
expect = p5.jelly.drag.scale_x * (1 + 0.06)
check("拖拽与弹跳缩放相乘", abs(sx - expect) < 0.03, f"{sx:.4f} vs {expect:.4f}")

for pp in list(W.ALL_PETS):
    try:
        pp.close()
    except Exception:
        pass

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
