"""倾斜跟随（TiltState）纯逻辑测试 —— 不依赖 Qt。

    python tests/test_tilt.py

覆盖用户确认的行为：
  1. 感应带：横向 ±140px，纵向 -40/+60，带外回正
  2. 输出范围：角度 ±3.5°，横移 ±5px
  3. 弹簧收敛速度（k=0.30，6 帧到 90%）
  4. 边界：零输入、超远、正好在边界
  5. reset 立刻归零
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.tilt import (TiltState, TILT_RADIUS, TILT_MAX_DEG, TILT_MAX_PX,
                             TILT_SPRING, TILT_ORIGIN_Y_FALLBACK)

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# 窗口矩形：400x300，位于 (200, 200)
RECT = (200.0, 200.0, 600.0, 500.0)
CX = (RECT[0] + RECT[2]) / 2.0     # 400
CY = (RECT[1] + RECT[3]) / 2.0     # 350

print("\n=== 1. 感应带边界（2026-10-30 新语义）===")
# 新语义：归一化基准 = 窗口半宽 + TILT_RADIUS，且边缘有渐变衰减
HALF_SPAN = (RECT[2] - RECT[0]) / 2.0 + TILT_RADIUS     # 200 + 140 = 340
t = TiltState()
# 正中心 → 0
t.update_target(CX, CY, RECT)
check("正中心 → 目标 0", abs(t.tgt) < 1e-9, f"tgt={t.tgt}")

# 偏右半个感应带 → 约 +0.5（受边缘渐变影响会略小）
t.update_target(CX + HALF_SPAN * 0.5, CY, RECT)
check("右移半个感应带 → 约 +0.5", 0.3 < t.tgt < 0.6, f"tgt={t.tgt:.3f}")

# 偏左对称
t.update_target(CX - HALF_SPAN * 0.5, CY, RECT)
check("左移对称", -0.6 < t.tgt < -0.3, f"tgt={t.tgt:.3f}")

# 接近边界：因渐变衰减，值应明显小于 1
t.update_target(RECT[2] - 2, CY, RECT)
check("接近右边界时被渐变衰减", 0 < t.tgt < 0.9, f"tgt={t.tgt:.3f}")

# 超出感应带 → 回正
t.update_target(RECT[2] + TILT_RADIUS + 50, CY, RECT)
check("超出右边界 → 回正", abs(t.tgt) < 1e-9, f"tgt={t.tgt}")

print("\n=== 2. 纵向感应带（-40 / +60）===")
t = TiltState()
t.update_target(CX + 100, RECT[1] - 10, RECT)      # 上方 10px，带内（靠近边缘）
check("上方 10px 仍有值", abs(t.tgt) > 0.01, f"tgt={t.tgt:.3f}")

t.update_target(CX + 100, RECT[1] - 50, RECT)      # 上方 50px，超带
check("上方 50px 超出 → 回正", abs(t.tgt) < 1e-9, f"tgt={t.tgt}")

t.update_target(CX + 100, RECT[3] + 10, RECT)      # 下方 10px，带内
check("下方 10px 仍有值", abs(t.tgt) > 0.01, f"tgt={t.tgt:.3f}")

t.update_target(CX + 100, RECT[3] + 70, RECT)      # 下方 70px，超带
check("下方 70px 超出 → 回正", abs(t.tgt) < 1e-9, f"tgt={t.tgt}")

print("\n=== 3. 输出范围 ===")
t = TiltState()
t.update_target(CX + 9999, CY, RECT)   # 超远（会被横向带判掉，先设满值）
t.cur = t.tgt = 1.0
check("满值角度 = +3.5°", abs(t.angle - TILT_MAX_DEG) < 1e-9, f"angle={t.angle}")
check("满值横移 = +5px", abs(t.offset_x - TILT_MAX_PX) < 1e-9, f"off={t.offset_x}")
t.cur = t.tgt = -1.0
check("负满值角度 = -3.5°", abs(t.angle + TILT_MAX_DEG) < 1e-9, f"angle={t.angle}")
check("负满值横移 = -5px", abs(t.offset_x + TILT_MAX_PX) < 1e-9, f"off={t.offset_x}")

print("\n=== 4. 弹簧收敛速度 ===")
t = TiltState()
t.cur = 0.0
t.tgt = 1.0
frames_90 = None
for i in range(60):
    t.step()
    if frames_90 is None and t.cur >= 0.9:
        frames_90 = i + 1
check("k=0.30 时 90% 收敛在 7 帧内", frames_90 is not None and frames_90 <= 7,
      f"{frames_90} 帧")

# 与 whale 的 0.14 对比（说明我们更快）
cur = 0.0
f14 = None
for i in range(60):
    cur += (1.0 - cur) * 0.14
    if f14 is None and cur >= 0.9:
        f14 = i + 1
check("对照：whale 的 0.14 需要更多帧", f14 > frames_90, f"0.14 需 {f14} 帧 vs 0.30 需 {frames_90} 帧")

print("\n=== 5. 收敛后停止重绘 ===")
t = TiltState()
t.tgt = 0.0
t.cur = 0.0
moved = t.step()
check("已经归零 → 不需要重绘", moved is False, f"moved={moved}")

t = TiltState()
t.cur = 0.5
t.tgt = 0.0
moved = t.step()
check("正在回正 → 需要重绘", moved is True, f"moved={moved}")
# 推到完全归零
for _ in range(200):
    t.step()
check("最终归零", abs(t.cur) < 1e-9, f"cur={t.cur}")

print("\n=== 6. reset ===")
t = TiltState()
t.cur = 0.8
t.tgt = 0.9
t.reset()
check("reset 后 cur=0", t.cur == 0.0)
check("reset 后 tgt=0", t.tgt == 0.0)
check("reset 后 inactive", t.active is False)

print("\n=== 6b. 回归：鼠标静止时不得周期性抽动（心跳 bug）===")
# 旧 bug：收敛后无条件 cur=0 → 重新收敛 → 再归零，周期约 60 帧
t = TiltState()
t.update_target(CX + 100, CY, RECT)
for _ in range(80):
    t.step()
v_stable = t.cur
check("已收敛到非零值", abs(v_stable) > 0.1, f"cur={v_stable:.4f}")

redraws = 0
max_jump = 0.0
prev = t.cur
for _ in range(300):
    t.update_target(CX + 100, CY, RECT)      # 鼠标完全不动
    if t.step():
        redraws += 1
    max_jump = max(max_jump, abs(t.cur - prev))
    prev = t.cur

check("静止 300 帧内零重绘", redraws == 0, f"redraws={redraws}")
check("静止期间无突变", max_jump < 1e-9, f"max_jump={max_jump:.9f}")
check("值保持稳定", abs(t.cur - v_stable) < 1e-9,
      f"{v_stable:.6f} -> {t.cur:.6f}")

print("\n=== 6c. 收敛后停在目标值（不归零）===")
t = TiltState()
t.tgt = 0.25
t.cur = 0.0
for _ in range(300):
    t.step()
check("停在目标值 0.25", abs(t.cur - 0.25) < 1e-6, f"cur={t.cur:.6f}")

print("\n=== 6d. 目标为 0 时仍能收成 0 并停机 ===")
t = TiltState()
t.tgt = 0.0
t.cur = 0.3
for _ in range(300):
    t.step()
check("归零", abs(t.cur) < 1e-9, f"cur={t.cur}")
check("停机（不再重绘）", t.step() is False)

print("\n=== 7. is_identity（跳过 painter 变换）===")
t = TiltState()
check("初始是 identity", t.is_identity() is True)
t.cur = 1e-9
check("极小值仍算 identity", t.is_identity() is True)
t.cur = 0.1
check("有明显值时不是 identity", t.is_identity() is False)

print("\n=== 8. 边界：极端输入不崩 ===")
t = TiltState()
t.update_target(-1e9, -1e9, RECT)
check("极端坐标不崩", abs(t.tgt) < 1e-9)
t.update_target(0, 0, (0, 0, 0, 0))     # 零尺寸窗口
check("零尺寸窗口不崩", True)

print("\n=== 9. 变换原点常量 ===")
check("脚底原点 = 0.917", abs(TILT_ORIGIN_Y_FALLBACK - 0.917) < 1e-9,
      f"{TILT_ORIGIN_Y_FALLBACK}")
check("与 whale 的 91.7% 一致", abs(TILT_ORIGIN_Y_FALLBACK * 100 - 91.7) < 0.01)

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
