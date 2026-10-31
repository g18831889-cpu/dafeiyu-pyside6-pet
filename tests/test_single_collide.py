"""单体模式硬碰撞（被挡就停）纯逻辑测试 —— 不依赖 Qt，直接跑：

    python tests/test_single_collide.py

用户 2026-10-31 改的口径：前方有别的单体鱼就当障碍物，自己走到贴脸处
停下、剩下的动画原地播完，**对方一动不动**（旧版是推箱子把人顶走）。

覆盖的决策：
  1. 碰撞框硬碰撞（AABB，用 BODY_BOX 换算）；
  2. 前方有人 → 自己停在贴脸处，不推人；
  3. 已经贴住 → 一步都走不动（原地跑）；
  4. 畅通 → 走满全程；
  5. 屏幕边缘 → 夹到边缘为止；
  6. 只单体模式（这里测的是函数本身，不涉及模式开关）；
  7. 拖拽/飞行豁免（由 window 侧的 collide_participates 控制）；
  8. 起手已重叠 → 交给 separate_overlaps 分离，不在这里硬挤；
  9. 斜向、零位移、无邻居等边界；
 10. separate_overlaps 全局兜底（每帧最多挪 8px，多帧收敛）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.flock import (aabb_contact, blocked_travel,
                              separate_overlaps)

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# 身体框半尺寸（BODY_BOX = 217×269 → 半宽 108.5 / 半高 134.5）
HW, HH = 108.5, 134.5
BODY_W = 2 * HW
# 世界：屏幕 0..1920 x 0..1080，中心坐标系里身体中心必须落在 [HW, 1920-HW] 之类
BOUNDS = (HW, HH, 1920.0 - HW, 1080.0 - HH)


def clamp(key, cx, cy):
    """把中心夹进屏幕内（key 为 None 表示 mover 自己）。"""
    return (max(BOUNDS[0], min(cx, BOUNDS[2])),
            max(BOUNDS[1], min(cy, BOUNDS[3])))


def walk(mover, mates, dx, dy, want=None):
    """按 blocked_travel 语义走一步，返回 (实际位移, allowed, hit_key)。"""
    import math
    cx, cy = mover[0], mover[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-9:
        return (0.0, 0.0), 0.0, None
    ux, uy = dx / dist, dy / dist
    step = dist if want is None else want
    allowed, hit_key, _t = blocked_travel(mover, mates, ux, uy, step, clamp)
    return (ux * allowed, uy * allowed), allowed, hit_key


print("\n=== 1. AABB 接触距离 ===")
# a 在 (500,500)，b 在 (900,500)，沿 +x 走，接触距离应为 900-500-2*HW
t = aabb_contact(500, 500, HW, HH, 900, 500, HW, HH, 1.0, 0.0)
check("正面接近的接触距离", abs(t - (400 - BODY_W)) < 1e-6, f"t={t}")
# 侧向（y 差超过 2*HH）永不接触
t = aabb_contact(500, 500, HW, HH, 900, 900, HW, HH, 1.0, 0.0)
check("纵向错开永不接触", t is None)
# 已经在重叠 → 接触距离 0
t = aabb_contact(500, 500, HW, HH, 520, 500, HW, HH, 1.0, 0.0)
check("已重叠时接触距离为 0", t is not None and t <= 1e-9, f"t={t}")
# 背对背（b 在身后）→ None
t = aabb_contact(500, 500, HW, HH, 100, 500, HW, HH, 1.0, 0.0)
check("身后的不接触", t is None)

print("\n=== 2. 前方有人 → 停在贴脸处，不推人 ===")
# mover 在 x=400，mate 在 x=700，间距 300，贴脸需要 217 → 只能走 83
d, allowed, hit = walk((400, 500, HW, HH), [("m1", 700, 500, HW, HH)], 100, 0)
check("只走到贴脸处 83px", abs(d[0] - 83.0) < 1e-6, f"dx={d[0]}")
check("报出挡路者", hit == "m1", f"hit={hit}")
check("恰好贴边不重叠", abs((700 - (400 + d[0])) - BODY_W) < 1e-6,
      f"gap={700 - (400 + d[0])}")

print("\n=== 3. 撞不到时走满全程 ===")
d, allowed, hit = walk((400, 500, HW, HH), [("m1", 900, 500, HW, HH)], 50, 0)
check("走满 50", abs(d[0] - 50.0) < 1e-6, f"dx={d[0]}")
check("没有挡路者", hit is None, f"hit={hit}")

print("\n=== 4. 已经贴住 → 一步都走不动（原地跑） ===")
touching = 400.0 + BODY_W
d, allowed, hit = walk((400, 500, HW, HH), [("m1", touching, 500, HW, HH)], 100, 0)
check("位移为 0", abs(d[0]) < 1e-6, f"dx={d[0]}")
check("报出挡路者", hit == "m1", f"hit={hit}")

print("\n=== 5. 多只挡路 → 只认最近的那只 ===")
# near 在 700（贴脸距离 83），far 在 900（贴脸距离 283 > want 200，够不着）
d, allowed, hit = walk((400, 500, HW, HH),
                       [("far", 900, 500, HW, HH), ("near", 700, 500, HW, HH)], 200, 0)
check("停在最近那只前", abs(d[0] - (700 - BODY_W - 400)) < 1e-6, f"dx={d[0]}")
check("挡路者是近的那只", hit == "near", f"hit={hit}")

print("\n=== 6. 链式：只挡第一只，后面的不影响 ===")
# 三只等距排开，mover 只看最近的那只，绝不把后面几只也算进来
x0 = 200.0
x1 = x0 + BODY_W
x2 = x1 + BODY_W
d, allowed, hit = walk((x0, 500, HW, HH),
                       [("m1", x1, 500, HW, HH), ("m2", x2, 500, HW, HH)], 60, 0)
check("贴住第一只就走不动", abs(d[0]) < 1e-6, f"dx={d[0]}")
check("挡路者是第一只", hit == "m1", f"hit={hit}")

print("\n=== 7. 屏幕边缘 → 夹到边缘为止 ===")
# mover 已经贴墙，右走 200 → 一步都走不了
wall_x = 1920.0 - HW
d, allowed, hit = walk((wall_x, 500, HW, HH), [], 200, 0)
check("贴墙时走不动", abs(d[0]) < 1e-6, f"dx={d[0]}")
check("墙不算挡路者", hit is None, f"hit={hit}")
# 离墙 50 → 只能走 50
d, allowed, hit = walk((wall_x - 50.0, 500, HW, HH), [], 200, 0)
check("走到墙边为止", abs(d[0] - 50.0) < 1e-6, f"dx={d[0]}")

print("\n=== 8. 起手已重叠 → 返回 0，交给分离兜底 ===")
# 两只中心几乎重合（模拟召唤后叠在屏幕正中）
d, allowed, hit = walk((800, 500, HW, HH), [("m1", 800, 500, HW, HH)], 50, 0)
check("重叠时一步不走", abs(d[0]) < 1e-6, f"dx={d[0]}")
check("报出挡路者", hit == "m1", f"hit={hit}")

print("\n=== 9. 斜向推进 ===")
# 正上方有人，但纵向够不着（贴脸要 2*HH=269，这里中心距 300）
d, allowed, hit = walk((400, 400, HW, HH), [("m1", 400, 100, HW, HH)], 0, 150)
check("纵向够不着就直走", abs(d[1] - 150.0) < 1e-6 and abs(d[0]) < 1e-6,
      f"({d[0]:.1f},{d[1]:.1f})")
# 斜 45° 朝人走（对方在 (700,100)，本身不重叠），会被截短
d, allowed, hit = walk((400, 400, HW, HH), [("m1", 700, 100, HW, HH)], 400, -400)
check("斜向被截短", 0.0 < allowed < 565.7, f"allowed={allowed:.1f}")
check("斜向报出挡路者", hit == "m1", f"hit={hit}")

print("\n=== 10. 无邻居时直通 ===")
d, allowed, hit = walk((400, 400, HW, HH), [], 120, -80)
check("无邻居自由移动", abs(d[0] - 120.0) < 1e-6 and abs(d[1] - (-80.0)) < 1e-6,
      f"({d[0]:.1f},{d[1]:.1f})")
check("无邻居无挡路者", hit is None)

print("\n=== 11. 零位移不炸 ===")
d, allowed, hit = walk((400, 400, HW, HH), [("m1", 450, 400, HW, HH)], 0, 0)
check("零位移原样返回", abs(d[0]) < 1e-9 and abs(d[1]) < 1e-9, f"({d[0]},{d[1]})")

print("\n=== 12. 全局分离兜底：单帧受 max_step 约束，多帧收敛 ===")
entries = [("a", 800, 500, HW, HH), ("b", 850, 500, HW, HH)]
one = separate_overlaps(entries, clamp, iterations=1)
fd1 = dict((k, (x, y)) for k, x, y in one)
check("单帧有动作", bool(one), f"{one}")
check("单帧位移不超过 max_step",
      all(abs(v[0] - (800 if k == "a" else 850)) <= 8.0 + 1e-6 for k, v in fd1.items()),
      f"{fd1}")
pa, pb = 800.0, 850.0
for _ in range(30):
    fixed = separate_overlaps([("a", pa, 500, HW, HH), ("b", pb, 500, HW, HH)],
                              clamp, iterations=1)
    for k, x, y in fixed:
        if k == "a":
            pa = x
        else:
            pb = x
check("多帧后不再重叠", abs(pb - pa) >= BODY_W - 1e-6, f"gap={abs(pb - pa):.1f}")

print("\n=== 13. 分离兜底：一方贴墙时另一方退全部 ===")
wall_x = 1920.0 - HW
pa, pb = wall_x, wall_x - 100
for _ in range(40):
    fixed = separate_overlaps([("a", pa, 500, HW, HH), ("b", pb, 500, HW, HH)],
                              clamp, iterations=1)
    for k, x, y in fixed:
        if k == "a":
            pa = x
        else:
            pb = x
check("贴墙者仍在墙上", abs(pa - wall_x) < 1e-6, f"pa={pa}")
check("另一只被推离到不重叠", abs(pa - pb) >= BODY_W - 1e-6,
      f"gap={abs(pa - pb):.1f}")

print("\n=== 14. 分离兜底：不重叠时零动作 ===")
entries = [("a", 400, 500, HW, HH), ("b", 900, 500, HW, HH)]
check("无重叠不挪", separate_overlaps(entries, clamp) == [])

print("\n=== 15. 贴边/浅重叠 → 反方向必须能走（用户 2026-10-31 报的 bug）===")
# 回归：旧版 aabb_contact 对"起点已在扩张盒内"一律返回 0，
# 分离兜底每帧只挪几像素 → 长期停在"重叠零点几像素"，连朝反方向都走不了。
for gap, ov in ((BODY_W, 0.0), (BODY_W - 0.5, 0.5), (BODY_W - 2, 2.0), (BODY_W - 5, 5.0)):
    d, allowed, hit = walk((400, 500, HW, HH), [("m1", 400 + gap, 500, HW, HH)], 50, 0)
    check(f"重叠{ov:.1f}px 朝对方走不动", abs(allowed) < 1e-6, f"allowed={allowed}")
    d, allowed, hit = walk((400, 500, HW, HH), [("m1", 400 + gap, 500, HW, HH)], -50, 0)
    check(f"重叠{ov:.1f}px 反方向能走满 50", abs(allowed - 50.0) < 1e-6, f"allowed={allowed}")
    d, allowed, hit = walk((400, 500, HW, HH), [("m1", 400 + gap, 500, HW, HH)], 0, -50)
    check(f"重叠{ov:.1f}px 侧向能走满 50", abs(allowed - 50.0) < 1e-6, f"allowed={allowed}")

# 深重叠（> CONTACT_SOFT_PX）仍交给分离兜底，四向都不走 —— 防止互相穿透
d, allowed, hit = walk((400, 500, HW, HH), [("m1", 410, 500, HW, HH)], -50, 0)
check("深重叠 10px 时四向都停", abs(allowed) < 1e-6, f"allowed={allowed}")

# 深重叠经几帧分离兜底后，必须恢复"能拉开"
pa, pb = 400.0, 410.0
for _ in range(20):
    fixed = separate_overlaps([("a", pa, 500, HW, HH), ("b", pb, 500, HW, HH)],
                              clamp, iterations=2)
    for k, x, y in fixed:
        if k == "a":
            pa = x
        else:
            pb = x
d, allowed, hit = walk((pa, 500, HW, HH), [("m1", pb, 500, HW, HH)], -50, 0)
check("分离几帧后反方向恢复可走", abs(allowed - 50.0) < 1e-6,
      f"gap={abs(pb - pa):.1f} allowed={allowed}")

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
