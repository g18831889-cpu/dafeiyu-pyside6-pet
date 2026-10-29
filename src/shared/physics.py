"""src/shared/physics.py - 桌面抛掷与碰撞物理（对齐 pet 的 throwStep）

关键修复：落地时若 `|vy| < REST_VY` 必须**直接归零**。
原实现无条件 `vy = -vy * restitution`，导致：
  · 永远小幅弹跳，|vy| 降不到静止阈值；
  · 调用方的 `atRest`（要求 |vy| < 1）永远不成立 —— 表现为"抛出去的宠物停不下来、一直横向滑"。
"""
REST_VY_DEFAULT = 40.0   # 与 pet 的 REST_VY 同值
# 每次反弹额外扣掉的固定速度（px/s）。pet 只有百分比衰减，在离散积分下会残留小幅弹跳；
# 加这一项后小速度直接被吃掉，观感是"弹几下干脆停住"。
FIXED_BOUNCE_DECAY = 40.0


def update_physics(x, y, vx, vy, w, h, bounds, gravity, restitution, ground_friction, dt,
                   rest_vy=REST_VY_DEFAULT, fixed_decay=FIXED_BOUNCE_DECAY):
    """返回新的 (x, y, vx, vy)。"""
    if dt > 0.05:
        dt = 0.05

    vy += gravity * dt
    new_x = x + vx * dt
    new_y = y + vy * dt

    min_x, min_y, max_x, max_y = bounds

    if new_x < min_x:
        new_x = min_x
        vx = abs(vx) * restitution
    elif new_x + w > max_x:
        new_x = max_x - w
        vx = -abs(vx) * restitution

    # 与 pet 完全一致：落地时 |vy| < REST_VY(40) 直接归零
    stop_vy = rest_vy

    if new_y < min_y:
        new_y = min_y
        _b = abs(vy) * restitution - fixed_decay
        vy = 0.0 if (abs(vy) < stop_vy or _b <= 0) else _b
    elif new_y + h > max_y:
        new_y = max_y - h
        # ★ 落地：速度已衰减到静止量级 → 直接归零（不再反弹）
        _b = abs(vy) * restitution - fixed_decay
        vy = 0.0 if (abs(vy) < stop_vy or _b <= 0) else -_b
        # 贴地摩擦（pet 原值 2.5，不做放大）
        vx *= max(0.0, 1.0 - ground_friction * dt)

    return int(new_x), int(new_y), vx, vy


# ---------------------------------------------------------------------------
# 落地 Q 弹（照搬 pet 的 landingSquash / squashScale，shared-core.js）
# ---------------------------------------------------------------------------
SQ_SQUASH = 0.55
SQ_DURATION_MS = 220.0
SQ_SOFT_SPEED = 300.0
SQ_HARD_SPEED = 1500.0
SQ_MAX_SQUASH = 0.55


def landing_squash(impact_speed):
    """冲击速度 → 压扁深度：轻落 0.8（几乎不压），重砸 0.55（压得更狠）。"""
    t = (abs(impact_speed) - SQ_SOFT_SPEED) / (SQ_HARD_SPEED - SQ_SOFT_SPEED)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return min(0.8, 1.0 - t * (1.0 - SQ_MAX_SQUASH))


def squash_scale(u, squash=SQ_SQUASH):
    """u∈[0,1] → scaleY。前 45% 平方缓入压扁，后 55% 回弹并轻微过冲（≤1.12）。"""
    if u < 0.45:
        p = u / 0.45
        return 1.0 - (1.0 - squash) * p * p
    p = (u - 0.45) / 0.55
    c1 = 1.70158
    c3 = c1 + 1.0
    f = 1.0 + c3 * (p - 1.0) ** 3 + c1 * (p - 1.0) ** 2
    return min(1.12, squash + (1.0 - squash) * max(f, 0.0))
