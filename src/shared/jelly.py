"""src/shared/jelly.py - 拖拽 Q 弹形变（照搬 dsh-whale-pet 的 wp-fx / wp-breathe 层）

三个机制（与 whale 一一对应）：
  ① DragStretch  : 拖拽中按速度沿运动方向拉伸   (client.js:3403 fxDragTransform)
  ② Settle       : 松手瞬间从「撑开」回弹       (client.js:1955 wpSettle)
  ③ Pop          : 单击时弹一下                 (client.js:1950 wpPop)

与 whale 的差异：
  · whale 分 wp-fx / wp-breathe 两层 DOM 避免 transform 打架；
    Qt 只有一个 QPainter，所以在**同一个变换栈里按顺序复合**。
  · 顺序固定为：拖拽拉伸 → 倾斜 → 落地 squash（见 apply_to_painter）。
    拖拽期间倾斜会被置零（与 whale 一致），所以不会出现两个旋转叠加。

设计要点：
  · 全部是**纯数学**，不依赖 Qt（apply_to_painter 除外，它只做矩阵运算）
  · 时间用秒，方便单测
"""
import math

# ---- ① 拖拽速度拉伸（whale: client.js:3403）----
DRAG_SPEED_TO_STRETCH = 0.09   # px/ms → 拉伸量
DRAG_STRETCH_MAX = 0.16        # 拉伸上限 16%
DRAG_STRETCH_SPRING = 0.30     # 低通系数 k
DRAG_STALE_MS = 200.0          # 停顿超过这个时长不更新（防停下后仍拉伸）
DRAG_MIN_STRETCH = 0.005       # 小于这个值不变换
DRAG_MIN_DT_MS = 4.0           # 最小时间间隔，防除零

# ---- ② 松手回落（whale: wpSettle）----
SETTLE_DURATION = 0.5          # 秒
SETTLE_SX0 = 1.09              # 起点：横向撑开 9%
SETTLE_SY0 = 0.90              # 起点：纵向压扁 10%
SETTLE_SX_MID = 0.96           # 45% 处过冲
SETTLE_SY_MID = 1.04
SETTLE_MID_T = 0.45

# ---- ③ 点击弹跳（whale: wpPop，amp=0.06）----
POP_DURATION = 0.42
POP_AMP = 0.06
POP_AMP_STRONG = 0.12          # 连点时加大（whale 用 --pop-amp:0.12）
POP_T1 = 0.30                  # 压扁峰值
POP_T2 = 0.62                  # 反向过冲

# ---- 变换原点 ----
ORIGIN_Y = 0.917               # 脚底（FEET_Y 330 / CANVAS_H 360）


def _cubic_bezier(p0, p1, p2, p3, t):
    """三次贝塞尔求值（一维）。"""
    u = 1.0 - t
    return (u * u * u * p0 + 3 * u * u * t * p1
            + 3 * u * t * t * p2 + t * t * t * p3)


def _bezier_ease(t, x1, y1, x2, y2):
    """CSS cubic-bezier(x1,y1,x2,y2) 的近似求值。

    whale 的 wpSettle 用 cubic-bezier(.34,1.56,.64,1) —— 第二控制点 y=1.56
    会**过冲**，这是"Q 弹"手感的关键。
    做法：先在 x 方向二分求出参数 s，再取 y。
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2.0
        x = _cubic_bezier(0.0, x1, x2, 1.0, mid)
        if x < t:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2.0
    return _cubic_bezier(0.0, y1, y2, 1.0, s)


# =====================================================================
# ① 拖拽速度拉伸
# =====================================================================
class DragStretch:
    """拖拽过程中的「沿运动方向拉伸」。

    与 whale 的 fxDragTransform 完全同参。
    """

    __slots__ = ("s", "_last_x", "_last_y", "_last_t", "_ang")

    def __init__(self):
        self.s = 0.0          # 当前拉伸量
        self._last_x = None
        self._last_y = None
        self._last_t = None
        self._ang = 0.0       # 最近一次运动方向（弧度）

    def reset(self):
        self.s = 0.0
        self._last_x = None
        self._last_y = None
        self._last_t = None
        self._ang = 0.0

    def update(self, x, y, now_ms):
        """喂一个鼠标位置（屏幕坐标 + 毫秒时间戳）。

        返回 True 表示形变有变化（调用方需要重绘）。
        """
        prev_s = self.s
        if self._last_x is not None:
            dt = now_ms - self._last_t
            # whale: dt = max(变化时的时间差, 4)，且 < 200 才更新
            if (x != self._last_x or y != self._last_y):
                dt = max(dt, DRAG_MIN_DT_MS)
            else:
                dt = 16.0
            if dt < DRAG_STALE_MS:
                dist = math.hypot(x - self._last_x, y - self._last_y)
                sp = dist / dt                      # px/ms
                target = min(sp * DRAG_SPEED_TO_STRETCH, DRAG_STRETCH_MAX)
                self.s += (target - self.s) * DRAG_STRETCH_SPRING
                if dist > 0.5:
                    self._ang = math.atan2(y - self._last_y, x - self._last_x)
            else:
                # ★ 停顿超过 200ms：速度按 0 处理，让拉伸自然衰减回 0。
                #   whale 的写法是"不更新"，但那样拉伸会**永久停在**上一次的值
                #   （因为它只在 pointermove 里更新，停顿后不再有事件）。
                #   whale 靠 fxDragReset 在松手时清零兜底；我们这里也做衰减，
                #   避免"鼠标停住不动，鱼还保持拉伸"的怪状态。
                self.s += (0.0 - self.s) * DRAG_STRETCH_SPRING
        self._last_x = x
        self._last_y = y
        self._last_t = now_ms
        return abs(self.s - prev_s) > 1e-4

    def decay(self):
        """停止拖拽时衰减到 0（whale 的 fxDragReset 是直接清零）。"""
        self.reset()

    @property
    def active(self):
        return self.s >= DRAG_MIN_STRETCH

    @property
    def angle(self):
        return self._ang

    @property
    def scale_x(self):
        return 1.0 + self.s

    @property
    def scale_y(self):
        return 1.0 - self.s * 0.65


# =====================================================================
# ② 松手回落
# =====================================================================
class Settle:
    """松手瞬间的回落动画（whale 的 wpSettle）。

    从 scale(1.09, .9) 出发，45% 处过冲到 scale(.96, 1.04)，回到 1。
    缓动用 cubic-bezier(.34, 1.56, .64, 1)。
    """

    __slots__ = ("t0", "_running")

    def __init__(self):
        self.t0 = 0.0
        self._running = False

    def start(self, now_sec):
        self.t0 = now_sec
        self._running = True

    def stop(self):
        self._running = False

    @property
    def running(self):
        return self._running

    def scales(self, now_sec):
        """返回 (sx, sy)；未运行或已结束返回 (1, 1)。"""
        if not self._running:
            return 1.0, 1.0
        u = (now_sec - self.t0) / SETTLE_DURATION
        if u >= 1.0:
            self._running = False
            return 1.0, 1.0
        if u <= 0.0:
            return SETTLE_SX0, SETTLE_SY0
        if u < SETTLE_MID_T:
            k = _bezier_ease(u / SETTLE_MID_T, 0.34, 1.56, 0.64, 1.0)
            sx = SETTLE_SX0 + (SETTLE_SX_MID - SETTLE_SX0) * k
            sy = SETTLE_SY0 + (SETTLE_SY_MID - SETTLE_SY0) * k
        else:
            k = _bezier_ease((u - SETTLE_MID_T) / (1.0 - SETTLE_MID_T),
                             0.34, 1.56, 0.64, 1.0)
            sx = SETTLE_SX_MID + (1.0 - SETTLE_SX_MID) * k
            sy = SETTLE_SY_MID + (1.0 - SETTLE_SY_MID) * k
        return sx, sy


# =====================================================================
# ③ 点击弹跳
# =====================================================================
class Pop:
    """单击时的弹跳（whale 的 wpPop）。

    keyframes（amp = 0.06）：
      0%   scale(1)
      30%  scale(1+amp, 1-amp*1.17)
      62%  scale(1-amp*0.5, 1+amp*0.5)
      100% scale(1)
    """

    __slots__ = ("t0", "_running", "_amp")

    def __init__(self):
        self.t0 = 0.0
        self._running = False
        self._amp = POP_AMP

    def start(self, now_sec, strong=False):
        # 连点：whale 是直接加大 --pop-amp，不重启动画
        if self._running:
            self._amp = POP_AMP_STRONG
            return
        self.t0 = now_sec
        self._running = True
        self._amp = POP_AMP_STRONG if strong else POP_AMP

    def stop(self):
        self._running = False

    @property
    def running(self):
        return self._running

    def scales(self, now_sec):
        if not self._running:
            return 1.0, 1.0
        u = (now_sec - self.t0) / POP_DURATION
        if u >= 1.0:
            self._running = False
            return 1.0, 1.0
        if u <= 0.0:
            return 1.0, 1.0
        a = self._amp
        if u < POP_T1:
            k = _bezier_ease(u / POP_T1, 0.34, 1.56, 0.64, 1.0)
            sx = 1.0 + a * k
            sy = 1.0 - a * 1.17 * k
        elif u < POP_T2:
            k = _bezier_ease((u - POP_T1) / (POP_T2 - POP_T1),
                             0.34, 1.56, 0.64, 1.0)
            sx = (1.0 + a) + ((1.0 - a * 0.5) - (1.0 + a)) * k
            sy = (1.0 - a * 1.17) + ((1.0 + a * 0.5) - (1.0 - a * 1.17)) * k
        else:
            k = _bezier_ease((u - POP_T2) / (1.0 - POP_T2),
                             0.34, 1.56, 0.64, 1.0)
            sx = (1.0 - a * 0.5) + (1.0 - (1.0 - a * 0.5)) * k
            sy = (1.0 + a * 0.5) + (1.0 - (1.0 + a * 0.5)) * k
        return sx, sy


# =====================================================================
# 组合：一只宠物当前的全部形变
# =====================================================================
class JellyState:
    """把三个机制合成一个「当前形变」。

    apply_to_painter 的变换顺序（必须固定）：
      1. 拖拽拉伸（带旋转，绕脚底）
      2. 松手回落 / 点击弹跳（纯缩放，绕脚底）
    倾斜由调用方单独处理，且**拖拽期间倾斜为 0**，不会叠加旋转。
    """

    __slots__ = ("drag", "settle", "pop")

    def __init__(self):
        self.drag = DragStretch()
        self.settle = Settle()
        self.pop = Pop()

    def reset_drag(self):
        self.drag.reset()

    def start_settle(self, now_sec):
        self.drag.reset()          # ★ 必须先 reset 再 settle（whale 同序）
        self.settle.start(now_sec)

    def start_pop(self, now_sec, strong=False):
        self.pop.start(now_sec, strong)

    def active(self):
        """是否还有形变需要绘制。

        ★ 必须把 settle / pop 也算进去：它们的 scales() 内部会在
          u >= 1 时把 _running 置 False，但那需要**被调用过**。
          active() 只看 _running 会漏掉"已经结束但还没被查过"的情况。
        """
        return (self.drag.active or self.settle.running or self.pop.running)

    def active_at(self, now_sec):
        """在指定时刻是否还有形变（会顺带推进 settle/pop 的结束判定）。"""
        if self.drag.active:
            return True
        if self.settle.running:
            self.settle.scales(now_sec)
            if self.settle.running:
                return True
        if self.pop.running:
            self.pop.scales(now_sec)
            if self.pop.running:
                return True
        return False

    def combined_scale(self, now_sec):
        """返回 (sx, sy)，把拖拽 / 回落 / 弹跳的缩放相乘。"""
        sx = sy = 1.0
        if self.drag.active:
            sx *= self.drag.scale_x
            sy *= self.drag.scale_y
        if self.settle.running:
            a, b = self.settle.scales(now_sec)
            sx *= a
            sy *= b
        if self.pop.running:
            a, b = self.pop.scales(now_sec)
            sx *= a
            sy *= b
        return sx, sy

    def drag_angle(self):
        return self.drag.angle if self.drag.active else 0.0
