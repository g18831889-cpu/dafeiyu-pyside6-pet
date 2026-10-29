"""拖拽 Q 弹（jelly）纯逻辑测试 —— 不依赖 Qt。

    python tests/test_jelly.py

对照 whale-pet 源码逐项验证：
  ① DragStretch  : 速度→拉伸映射、低通 k=0.3、上限 0.16、停顿不更新
  ② Settle       : 起点 (1.09, 0.9)、45% 过冲、0.5s 结束、回弹曲线过冲
  ③ Pop          : 30% 压扁峰值、62% 反向过冲、0.42s 结束
  ④ 组合顺序     : reset_drag 先于 settle
"""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.jelly import (DragStretch, Settle, Pop, JellyState,
                              _bezier_ease, _cubic_bezier,
                              DRAG_SPEED_TO_STRETCH, DRAG_STRETCH_MAX,
                              DRAG_STRETCH_SPRING, SETTLE_SX0, SETTLE_SY0,
                              SETTLE_DURATION, POP_AMP, POP_DURATION)

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


print("\n=== 0. 贝塞尔基础 ===")
check("ease(0) = 0", abs(_bezier_ease(0.0, .34, 1.56, .64, 1.0)) < 1e-9)
check("ease(1) = 1", abs(_bezier_ease(1.0, .34, 1.56, .64, 1.0) - 1.0) < 1e-9)
# cubic-bezier(.34,1.56,.64,1) 会过冲（y > 1）
over = max(_bezier_ease(t / 100.0, .34, 1.56, .64, 1.0) for t in range(101))
check("回弹曲线过冲 y>1", over > 1.0, f"最大 {over:.4f}")

print("\n=== 1. DragStretch：速度 → 拉伸 ===")
d = DragStretch()
# 模拟 1000px/s 的水平拖拽：每 16ms 移动 16px
now = 0.0
for i in range(80):
    now += 16.0
    d.update(i * 16.0, 0.0, now)
# 1000px/s = 1 px/ms → target = 0.09，收敛后 s ≈ 0.09
check("1000px/s 收敛到 ≈0.09", abs(d.s - 0.09) < 0.005, f"s={d.s:.4f}")
check("横向 scale = 1.09", abs(d.scale_x - 1.09) < 0.006, f"{d.scale_x:.4f}")
check("纵向 scale = 1-0.09*0.65", abs(d.scale_y - (1 - 0.09 * 0.65)) < 0.006,
      f"{d.scale_y:.4f}")
check("运动方向 = 0（向右）", abs(d.angle) < 0.01, f"{d.angle:.4f}")

# 竖直向下拖
d2 = DragStretch()
now = 0.0
for i in range(80):
    now += 16.0
    d2.update(0.0, i * 16.0, now)
check("向下拖方向 = π/2", abs(d2.angle - math.pi / 2) < 0.01, f"{d2.angle:.4f}")

print("\n=== 2. DragStretch：上限与停顿 ===")
d3 = DragStretch()
now = 0.0
for i in range(120):
    now += 16.0
    d3.update(i * 40.0, 0.0, now)      # 2500px/s，远超上限
check("超高速触顶 0.16", abs(d3.s - DRAG_STRETCH_MAX) < 0.005, f"s={d3.s:.4f}")
check("横向 scale 上限 1.16", abs(d3.scale_x - 1.16) < 0.006, f"{d3.scale_x:.4f}")

# 停顿：位置不变且间隔 > 200ms → 拉伸衰减
# （whale 原版是"不更新"，但那样拉伸会永久卡在上次的值；
#   我们改成朝 0 衰减，避免"鼠标停住、鱼还保持拉伸"的怪状态）
d4 = DragStretch()
d4.update(0.0, 0.0, 0.0)
d4.update(100.0, 0.0, 16.0)
s_before = d4.s
d4.update(100.0, 0.0, 300.0)     # 间隔 284ms > 200
check("停顿超过 200ms → 开始衰减", d4.s < s_before,
      f"{s_before:.4f} -> {d4.s:.4f}")
for i in range(40):
    d4.update(100.0, 0.0, 300.0 + i * 16.0)
check("持续停顿后趋近 0", d4.s < 0.005, f"s={d4.s:.5f}")

print("\n=== 3. DragStretch：低通系数 ===")
# 一帧从 0 冲向 target=0.09，应只走 30%
d5 = DragStretch()
d5.update(0.0, 0.0, 0.0)
d5.update(16.0, 0.0, 16.0)       # 1 px/ms → target 0.09
check("单帧走 30%", abs(d5.s - 0.09 * DRAG_STRETCH_SPRING) < 0.002,
      f"s={d5.s:.4f}")

print("\n=== 4. Settle：松手回落 ===")
st = Settle()
st.start(0.0)
sx0, sy0 = st.scales(0.0)
check("起点 sx = 1.09", abs(sx0 - SETTLE_SX0) < 1e-6, f"{sx0:.4f}")
check("起点 sy = 0.90", abs(sy0 - SETTLE_SY0) < 1e-6, f"{sy0:.4f}")

sx45, sy45 = st.scales(SETTLE_DURATION * 0.45)
check("45% 处 sx ≈ 0.96", abs(sx45 - 0.96) < 0.01, f"{sx45:.4f}")
check("45% 处 sy ≈ 1.04", abs(sy45 - 1.04) < 0.01, f"{sy45:.4f}")

sx_end, sy_end = st.scales(SETTLE_DURATION)
check("结束时回到 1", abs(sx_end - 1) < 1e-6 and abs(sy_end - 1) < 1e-6,
      f"({sx_end:.3f},{sy_end:.3f})")
check("结束后 running=False", st.running is False)

# 中途应有轻微过冲（贝塞尔回弹）
st2 = Settle()
st2.start(0.0)
vals = [st2.scales(SETTLE_DURATION * t / 50.0)[0] for t in range(1, 50)]
check("sx 出现过冲（>1 或 <0.96 之外）",
      max(vals) > 1.0 or min(vals) < 0.96,
      f"min={min(vals):.4f} max={max(vals):.4f}")

print("\n=== 5. Pop：点击弹跳 ===")
pp = Pop()
pp.start(0.0)
sx, sy = pp.scales(0.0)
check("起点是 1", abs(sx - 1) < 1e-6, f"{sx:.4f}")
sx30, sy30 = pp.scales(POP_DURATION * 0.30)
check("30% 横向 ≈ 1+amp", abs(sx30 - (1 + POP_AMP)) < 0.01, f"{sx30:.4f}")
check("30% 纵向 ≈ 1-amp*1.17", abs(sy30 - (1 - POP_AMP * 1.17)) < 0.01,
      f"{sy30:.4f}")
sx62, sy62 = pp.scales(POP_DURATION * 0.62)
check("62% 反向过冲（横向 <1）", sx62 < 1.0, f"{sx62:.4f}")
check("62% 反向过冲（纵向 >1）", sy62 > 1.0, f"{sy62:.4f}")
sxe, sye = pp.scales(POP_DURATION)
check("结束回到 1", abs(sxe - 1) < 1e-6)

print("\n=== 6. 组合：reset_drag 先于 settle ===")
js = JellyState()
# 先制造拖拽拉伸
now = 0.0
for i in range(60):
    now += 16.0
    js.drag.update(i * 16.0, 0.0, now)
check("拖拽时有拉伸", js.drag.active, f"s={js.drag.s:.4f}")

js.start_settle(now / 1000.0)
check("start_settle 后拖拽被清零", abs(js.drag.s) < 1e-9, f"{js.drag.s}")
check("settle 在运行", js.settle.running is True)
sx, sy = js.combined_scale(now / 1000.0)
check("组合缩放 = settle 起点", abs(sx - SETTLE_SX0) < 1e-6, f"({sx:.4f},{sy:.4f})")

print("\n=== 7. 组合：拖拽 + 弹跳同时（缩放相乘）===")
js2 = JellyState()
now = 0.0
for i in range(60):
    now += 16.0
    js2.drag.update(i * 16.0, 0.0, now)
js2.start_pop(now / 1000.0)
sx, sy = js2.combined_scale(now / 1000.0 + POP_DURATION * 0.30)
expect_sx = js2.drag.scale_x * (1 + POP_AMP)
check("两套缩放相乘", abs(sx - expect_sx) < 0.02, f"{sx:.4f} vs {expect_sx:.4f}")

print("\n=== 8. active / 空闲判断 ===")
js3 = JellyState()
check("初始不活跃", js3.active() is False)
js3.start_pop(0.0)
check("弹跳中活跃", js3.active() is True)
# active_at 会推进结束判定
check("超过弹跳时长后 active_at=False", js3.active_at(POP_DURATION + 0.01) is False)
check("此时 pop.running 也变 False", js3.pop.running is False)

print("\n=== 9. 边界：极端输入 ===")
d6 = DragStretch()
d6.update(0, 0, 0)
d6.update(1e9, 1e9, 1.0)     # 极大跳变
check("极大跳变不崩", d6.s <= DRAG_STRETCH_MAX + 1e-9, f"s={d6.s:.4f}")

st3 = Settle()
st3.start(0.0)
a, b = st3.scales(-1.0)      # 时间倒退
check("时间倒退不崩", True)
a, b = st3.scales(999.0)
check("超长时间返回 1", abs(a - 1) < 1e-6)

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} PASS")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
