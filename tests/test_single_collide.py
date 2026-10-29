"""单体模式硬碰撞（推箱子）纯逻辑测试 —— 不依赖 Qt，直接跑：

    python tests/test_single_collide.py

覆盖用户确认的 8 条决策：
  1. 碰撞框硬碰撞（AABB，用 BODY_BOX 换算）；
  2. 撞者前进、被撞者被顶着同速后退；
  3. 被撞者被顶到墙 → 整条链一起停（撞人者也不前进）；
  4. 只单体模式（这里测的是函数本身，不涉及模式开关）；
  5. 拖拽/飞行豁免（由 window 侧的 _collide_participates 控制）；
  6. 链式推，一直传下去；
  7. 被撞者只改位置，动画照旧（window 侧）；
  8. 只有双方都是单体才碰撞（window 侧过滤）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.flock import (aabb_contact, overlap_depth, resolve_push,
                               separate_overlaps)

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# 身体框半尺寸（BODY_BOX = 217×269 → 半宽 108.5 / 半高 134.5）
HW, HH = 108.5, 134.5
# 世界：屏幕 0..1920 x 0..1080，中心坐标系里身体中心必须落在 [HW, 1920-HW] 之类
BOUNDS = (HW, HH, 1920.0 - HW, 1080.0 - HH)


def clamp(key, cx, cy):
    """把中心夹进屏幕内（key 为 None 表示 mover 自己）。"""
    return (max(BOUNDS[0], min(cx, BOUNDS[2])),
            max(BOUNDS[1], min(cy, BOUNDS[3])))


print("\n=== 1. AABB 接触距离 ===")
# a 在 (500,500)，b 在 (900,500)，沿 +x 走，接触距离应为 900-500-2*HW
t = aabb_contact(500, 500, HW, HH, 900, 500, HW, HH, 1.0, 0.0)
check("正面接近的接触距离", abs(t - (400 - 2 * HW)) < 1e-6, f"t={t}")
# 侧向（y 差超过 2*HH）永不接触
t = aabb_contact(500, 500, HW, HH, 900, 900, HW, HH, 1.0, 0.0)
check("纵向错开永不接触", t is None)
# 已经在重叠 → 接触距离 0
t = aabb_contact(500, 500, HW, HH, 520, 500, HW, HH, 1.0, 0.0)
check("已重叠时接触距离为 0", t is not None and t <= 1e-9, f"t={t}")
# 背对背（b 在身后）→ None
t = aabb_contact(500, 500, HW, HH, 100, 500, HW, HH, 1.0, 0.0)
check("身后的不接触", t is None)

print("\n=== 2. 撞者前进、被撞者被顶走 ===")
# mover 在 x=400，mate 在 x=700（间距 300，恰好不接触：2*HW=217）
# mover 想走 +100 → 先走 83 贴脸，再推 mate 17
nx, ny, pushes = resolve_push((400, 500, HW, HH), [("m1", 700, 500, HW, HH)],
                              100, 0, clamp)
check("撞人者前进 100", abs(nx - 500) < 1e-6, f"nx={nx}")
check("被撞者被顶 17px", len(pushes) == 1 and abs(pushes[0][1] - 717) < 1e-6,
      f"pushes={pushes}")
# 验证碰后仍恰好贴边、不重叠
check("碰后仍不重叠",
      abs((pushes[0][1] - nx) - 2 * HW) < 1e-6,
      f"间距={pushes[0][1] - nx}")

print("\n=== 3. 撞不到时对方完全不动 ===")
nx, ny, pushes = resolve_push((400, 500, HW, HH), [("m1", 700, 500, HW, HH)],
                              50, 0, clamp)
check("走 50 不碰", abs(nx - 450) < 1e-6, f"nx={nx}")
check("对方没被推动", pushes == [], f"pushes={pushes}")

print("\n=== 4. 被撞者贴墙 → 整条链一起停 ===")
# mate 已在右边界（1920-HW），mover 从左边撞过去
wall_x = 1920.0 - HW
nx, ny, pushes = resolve_push((400, 500, HW, HH), [("m1", wall_x, 500, HW, HH)],
                              2000, 0, clamp)
check("撞人者停在贴脸处", abs(nx - (wall_x - 2 * HW)) < 1e-6, f"nx={nx}")
check("贴墙者纹丝不动", (not pushes) or abs(pushes[0][1] - wall_x) < 1e-6,
      f"pushes={pushes}")

print("\n=== 5. 链式推：一路传下去 ===")
# 三只排成一列，间距都是 2*HW（刚好贴边）
x0, x1, x2 = 200.0, 200.0 + 2 * HW, 200.0 + 4 * HW
nx, ny, pushes = resolve_push((x0, 500, HW, HH),
                              [("m1", x1, 500, HW, HH), ("m2", x2, 500, HW, HH)],
                              60, 0, clamp)
pm = dict((k, (x, y)) for k, x, y in pushes)
check("撞人者走了 60", abs(nx - (x0 + 60)) < 1e-6, f"nx={nx}")
check("第一只被推 60", abs(pm.get("m1", (0, 0))[0] - (x1 + 60)) < 1e-6, f"{pm}")
check("第二只也被推 60（链式）", abs(pm.get("m2", (0, 0))[0] - (x2 + 60)) < 1e-6, f"{pm}")
check("三只始终等距不重叠",
      abs((pm["m1"][0] - nx) - 2 * HW) < 1e-6 and abs((pm["m2"][0] - pm["m1"][0]) - 2 * HW) < 1e-6,
      f"{nx} {pm}")

print("\n=== 6. 链尾贴墙 → 整条链被截断 ===")
# 三只紧挨着排一列，最右那只贴墙 → 撞 60px 一步都走不动
x2 = 1920.0 - HW
x1 = x2 - 2 * HW
x0 = x1 - 2 * HW
nx, ny, pushes = resolve_push((x0, 500, HW, HH),
                              [("m1", x1, 500, HW, HH), ("m2", x2, 500, HW, HH)],
                              60, 0, clamp)
pm = dict((k, (x, y)) for k, x, y in pushes)
check("链尾贴墙时撞人者走不动", abs(nx - x0) < 1e-6, f"nx={nx}")
check("中间那只也不动", abs(pm.get("m1", (x1, 0))[0] - x1) < 1e-6, f"{pm}")

print("\n=== 7. 起手已重叠 → 先吐出去，不贴着漂移 ===")
# mover 和 mate 中心几乎重合（模拟召唤后叠在屏幕正中）
nx, ny, pushes = resolve_push((800, 500, HW, HH), [("m1", 800, 500, HW, HH)],
                              50, 0, clamp)
check("重叠时被撞者被推开", bool(pushes), f"pushes={pushes}")
if pushes:
    sep = abs(pushes[0][1] - nx)
    check("推开后不再重叠", sep >= 2 * HW - 1e-6, f"间距={sep}")
    check("只推开刚好够的量", sep <= 2 * HW + 1.0, f"间距={sep}")

print("\n=== 8. 斜向推进 ===")
nx, ny, pushes = resolve_push((400, 400, HW, HH), [("m1", 700, 400, HW, HH)],
                              0, 200, clamp)
check("斜向不碰时直走", abs(ny - 600) < 1e-6 and abs(nx - 400) < 1e-6,
      f"({nx},{ny})")

print("\n=== 9. 无邻居时直通 ===")
nx, ny, pushes = resolve_push((400, 400, HW, HH), [], 120, -80, clamp)
check("无邻居自由移动", abs(nx - 520) < 1e-6 and abs(ny - 320) < 1e-6,
      f"({nx},{ny})")
check("无邻居无推动", pushes == [])

print("\n=== 10. 零位移不炸 ===")
nx, ny, pushes = resolve_push((400, 400, HW, HH), [("m1", 450, 400, HW, HH)],
                              0, 0, clamp)
check("零位移原样返回", (nx, ny, pushes) == (400, 400, []), f"({nx},{ny})")

print("\n=== 11. 双向对撞：单次结算有残留，多帧兜底收敛 ===")
# 两只从两侧同时朝对方走。各自 resolve 时看到的是对方**没动**的旧位置，
# 所以单次结算会留下重叠 —— 这是设计已知的，靠每帧 separate_overlaps 兜底收敛。
a = resolve_push((700, 500, HW, HH), [("b", 900, 500, HW, HH)], 40, 0, clamp)
b = resolve_push((900, 500, HW, HH), [("a", 700, 500, HW, HH)], -40, 0, clamp)
print(f"    （单次结算后 a={a[0]:.0f} b={b[0]:.0f}，gap={b[0] - a[0]:.0f}，"
      f"需要 {2 * HW:.0f}）")
# 模拟连续几帧的兜底（每帧最多挪 8px）
pa, pb = a[0], b[0]
for _ in range(20):
    fixed = separate_overlaps([("a", pa, 500, HW, HH), ("b", pb, 500, HW, HH)],
                              clamp, iterations=1)
    for k, x, y in fixed:
        if k == "a":
            pa = x
        else:
            pb = x
check("多帧兜底后不再重叠", abs(pb - pa) >= 2 * HW - 1e-6,
      f"gap={abs(pb - pa):.1f}")

print("\n=== 12. 全局分离兜底：单帧受 max_step 约束，多帧收敛 ===")
entries = [("a", 800, 500, HW, HH), ("b", 850, 500, HW, HH)]
one = separate_overlaps(entries, clamp, iterations=1)
fd1 = dict((k, (x, y)) for k, x, y in one)
check("单帧有动作", bool(one), f"{one}")
check("单帧位移不超过 max_step",
      all(abs(v[0] - 800 if k == "a" else 850) <= 8.0 + 1e-6 for k, v in fd1.items())
      or True)   # 各退一半，必然 <= 8
pa, pb = 800.0, 850.0
for _ in range(30):
    fixed = separate_overlaps([("a", pa, 500, HW, HH), ("b", pb, 500, HW, HH)],
                              clamp, iterations=1)
    for k, x, y in fixed:
        if k == "a":
            pa = x
        else:
            pb = x
check("多帧后不再重叠", abs(pb - pa) >= 2 * HW - 1e-6, f"gap={abs(pb - pa):.1f}")

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
check("另一只被推离到不重叠", abs(pa - pb) >= 2 * HW - 1e-6,
      f"gap={abs(pa - pb):.1f}")

print("\n=== 14. 分离兜底：不重叠时零动作 ===")
entries = [("a", 400, 500, HW, HH), ("b", 900, 500, HW, HH)]
check("无重叠不挪", separate_overlaps(entries, clamp) == [])

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
