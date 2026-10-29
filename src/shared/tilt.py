"""src/shared/tilt.py - 光标倾斜跟随（单体模式子体专用）

来源：照搬 dsh-whale-pet 的 wp-fx 层（client.js:3477 / 3387 / 3371）
  · 感应带：横向 ±FX_TILT_RADIUS(140px)，纵向 -40/+60
  · 输出：translateX(±5px) + rotate(±3.5°)
  · 平滑：一阶低通 cur += (tgt-cur) * k

与 whale-pet 的**唯一差异**：弹簧系数。
  whale 用 0.14（跑在 60fps rAF 上，18 帧到 90% ≈ 300ms）
  这里默认 0.30（实测 6 帧到 90% ≈ 100ms，跟手且不抖）

变换原点：脚底 91.7%（FEET_Y 330 / CANVAS_H 360），与 whale 的
transform-origin:50% 91.7% 一致 —— 绕脚底倾斜才像重心在晃。

为什么不用预渲染档位：
  实测预渲染 21 档能到 96.9% 锐度且零重采样，但每只 14MB 内存。
  改用「原图复合变换」（一次重采样）同样是 96.9%，零额外内存。
"""
import math

# ---- 参数（对齐 whale-pet，只调弹簧）----
TILT_RADIUS = 140.0       # 横向感应半径 px
TILT_TOP_EXTRA = 40.0     # 上方感应余量
TILT_BOTTOM_EXTRA = 60.0  # 下方感应余量
TILT_MAX_DEG = 3.5        # 最大旋转角
TILT_MAX_PX = 5.0         # 最大横移
TILT_SPRING = 0.30        # 一阶低通系数（whale=0.14，这里加快到跟手）
TILT_EPS = 4e-3           # 收敛阈值

# ★ 抗抽动（2026-10-30 用户反馈"鼠标悬停在附近角色会抽动"）
#   抽动有两个来源：
#     ① QCursor.pos() 每 14ms 采样，鼠标的微小抖动直接进 tgt → 弹簧来回追；
#     ② 感应带边界处 tgt 从 ±1 突跳到 0，弹簧来不及过渡 → 视觉上"猛地一缩"。
#   对策：
#     · DEADZONE：目标值变化小于它就不更新（滤掉手抖/亚像素抖动）
#     · DEADZONE：目标值变化小于它就不更新（滤掉手抖/亚像素抖动）
#     · EDGE_FADE：感应带边缘做渐变，而不是硬截断（消除边界突跳）
#
# ★ 2026-10-30 调整：死区从 0.04 降到 0.01。
#   0.04 在 480 宽窗口下 = 鼠标横向要动 15.2px 倾斜才变一次，
#   手感是"卡成一格一格 / 像锁死的固定值"。0.01 ≈ 3.8px，
#   既滤掉 ±1~2px 手抖，又能连续跟随。
TILT_DEADZONE = 0.015     # 反向变化死区（≈5.7px）；同向变化直通
TILT_EDGE_FADE = 0.25     # 边缘渐变区：占感应带比例，末段线性衰减到 0

# 变换原点：脚底。与 whale-pet 的 transform-origin:50% 91.7% 一致
# （FEET_Y 330 / CANVAS_H 360 = 0.9167）
TILT_ORIGIN_Y_FALLBACK = 0.917


class TiltState:
    """一只宠物的倾斜状态。纯数学，不依赖 Qt。"""

    __slots__ = ("cur", "tgt", "_active", "_last_dir")

    def __init__(self):
        self.cur = 0.0      # 当前值 [-1, 1]
        self.tgt = 0.0      # 目标值 [-1, 1]
        self._active = False
        self._last_dir = 0  # 上次目标变化方向：+1 / -1 / 0（未定）

    # -- 目标值计算 ---------------------------------------------------
    def update_target(self, pointer_x, pointer_y, rect):
        """根据光标位置算目标值。

        rect = (left, top, right, bottom) 窗口矩形（屏幕坐标）
        返回 True 表示目标值变了。

        ★ 抗抽动两处处理：
          ① 死区：目标值变化 < TILT_DEADZONE 时不更新（滤掉鼠标微抖）
          ② 边缘渐变：接近感应带边界时线性衰减到 0，避免边界硬跳变
        """
        left, top, right, bottom = rect
        w = max(1.0, right - left)
        h = max(1.0, bottom - top)
        cx = left + w / 2.0
        cy = top + h / 2.0

        # ---- 横向：先算带内的归一化位置 ----
        half_span = w / 2.0 + TILT_RADIUS      # 感应带半宽（窗口半宽 + 半径）
        dx = pointer_x - cx
        if abs(dx) > half_span:
            return self._set_target(0.0)

        # 归一化到 [-1, 1]：以窗口中心为 0，±half_span 为边界
        t = dx / half_span

        # ---- 边缘渐变：|t| 超过 (1 - EDGE_FADE) 后线性衰减到 0 ----
        at = abs(t)
        fade_start = 1.0 - TILT_EDGE_FADE
        if at > fade_start:
            k = (1.0 - at) / TILT_EDGE_FADE if TILT_EDGE_FADE > 0 else 0.0
            t *= max(0.0, min(1.0, k))

        # ---- 纵向：超出感应带 → 回正；进入边缘也做渐变 ----
        if (pointer_y < top - TILT_TOP_EXTRA
                or pointer_y > bottom + TILT_BOTTOM_EXTRA):
            return self._set_target(0.0)
        # 纵向越靠近带边，横向倾斜越弱（避免"从上方接近时突然一歪"）
        dy_up = (top - pointer_y) / TILT_TOP_EXTRA if TILT_TOP_EXTRA > 0 else 0.0
        dy_dn = (pointer_y - bottom) / TILT_BOTTOM_EXTRA if TILT_BOTTOM_EXTRA > 0 else 0.0
        dy_edge = max(0.0, dy_up, dy_dn)        # 0 = 带内，1 = 正好在边界
        if dy_edge > 0.0:
            t *= max(0.0, 1.0 - dy_edge)

        return self._set_target(max(-1.0, min(1.0, t)))

    def _set_target(self, v):
        """设置目标值。

        ★ 2026-10-30：改成「滞回 + 同向直通」而不是死区。
          · 纯死区的问题：鼠标单向缓慢移动时，每 3.8px 才更新一次，
            手感是"一格一格"，且小位移完全无响应（用户反馈"像固定值"）。
          · 现在的规则：
              1) 变化方向与上次一致（单向移动）→ **直接更新**，连续跟手；
              2) 变化方向反转（来回抖动）→ 只有幅度超过死区才更新，
                 这样手抖被吃掉，不会左右抽。
        """
        d = v - self.tgt
        if d == 0.0:
            return False
        same_dir = (d > 0 and self._last_dir > 0) or (d < 0 and self._last_dir < 0)
        if same_dir or abs(d) >= TILT_DEADZONE:
            self.tgt = v
            self._last_dir = 1 if d > 0 else -1
            return True
        # 反向且幅度不足死区 → 视为抖动，忽略
        return False

    def reset(self):
        """立刻回正（拖拽开始等场景）。"""
        self.cur = 0.0
        self.tgt = 0.0
        self._active = False
        self._last_dir = 0

    # -- 弹簧推进 -----------------------------------------------------
    def step(self):
        """推进一帧。返回 True 表示画面需要重绘。

        ★★★ 2026-10-30 修复「悬停时左右抽动，像心跳」：
          旧实现收敛后无条件 `self.cur = 0.0`：
              if abs(self.tgt - self.cur) < 1e-9:
                  ...
                  self.cur = 0.0        # ← 错：把已收敛的非零值硬拍成 0
          于是 收敛(≈60 帧) → 归零 → 重新收敛 → 再归零，
          周期约 0.86 秒，鼠标不动也会规律抽动。
          实测：鼠标完全静止时 cur 从 0.26316 掉到 0.20000 再爬回来。

          正确语义：只有当**目标本身就是 0**时才允许把 cur 收成 0；
          目标非零时，收敛后应**停在目标值**并停止重绘。
        """
        gap = self.tgt - self.cur
        if abs(gap) < TILT_EPS:
            # 已经足够接近目标 → 吸附到目标值（而不是 0）
            if abs(self.tgt) < TILT_EPS:
                # 目标是 0：收成 0 并停机
                if self._active or abs(self.cur) > 1e-9:
                    self.cur = 0.0
                    self.tgt = 0.0
                    self._active = False
                    return True
                return False
            # 目标非零：停在目标值上，保持静止（不重绘）
            if self.cur != self.tgt:
                self.cur = self.tgt
                return True
            return False
        # 一阶低通推进
        self.cur += gap * TILT_SPRING
        self._active = True
        return True

    # -- 输出 ---------------------------------------------------------
    @property
    def angle(self):
        return self.cur * TILT_MAX_DEG

    @property
    def offset_x(self):
        return self.cur * TILT_MAX_PX

    @property
    def active(self):
        return self._active or abs(self.cur) > TILT_EPS

    def is_identity(self):
        """当前是否等价于不变换（省掉一次 painter 变换）。"""
        return abs(self.cur) < 1e-6


def apply_tilt(painter, tilt, pivot_y):
    """把倾斜变换应用到 painter 上。

    调用方负责 painter.save()/restore()。
    pivot_y = 窗口高度 × 0.917（脚底）
    """
    if tilt is None or tilt.is_identity():
        return
    w = painter.device().width()
    px = w / 2.0
    painter.translate(px, pivot_y)
    painter.rotate(tilt.angle)
    painter.translate(-px, -pivot_y)
    painter.translate(tilt.offset_x, 0.0)
