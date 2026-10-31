"""src/shared/flock.py - Boids 群体涌现（对齐 pet 架构）

设计基准：
  · 只调制，不产生位移：涌现只决定方向/速度，实际移动由漫游动作驱动
  · flock_force 返回像素级推力（separationRadius × weight），不是归一化 0~1
  · 近距分离绝对优先：身体重叠时 cohesion 归零，避免被反向拉回
  · 允许重叠：软推力，只在贴墙 + 重叠时硬翻转脱离
  · blend = 0.9：有合力时 90% 听合力 / 10% 随机；无合力时 100% 随机
"""
import math

DEFAULT_FLOCK = {
    'neighbourRadius': 420,
    'separationRadius': 300,
    'cohesionRadius': 600,
    'separationWeight': 1.0,
    'cohesionWeight': 0.2,
    'alignmentWeight': 0.3,
    'blend': 0.9,
    'speedGain': 0.5,
    'wallRepelOverlap': 0.2,
    'wallRepelHardPx': 10,
    'wallRepelRangePx': 150,
    'wallRepelPx': 260,
}

DEFAULT_ROAM = {
    'base': 0.25,
    'floor': 0.2,
    'start': 0.4,
    'peakOverlap': 0.8,
    'peak': 1.0,
    'hardOverlap': 0.8,
    'tierStepMax': 0.4,
    'tierCrabMax': 0.6,
    'forceTtlMs': 2500,
}


def overlap_ratio(self_cx, self_cy, half_w, half_h, mates):
    """逐只相加；可 >1（多只叠一摞时 overlap 飙升，越该全跑）。"""
    r = max(1, min(half_w, half_h))
    acc = 0.0
    for m in mates:
        mx, my = m[0], m[1]
        dist = math.hypot(mx - self_cx, my - self_cy)
        if dist >= 2 * r:
            continue
        acc += max(0.0, 1.0 - dist / (2 * r))
    return acc


def flock_force(self_cx, self_cy, half_w, half_h, cur_dir, mates, params=None):
    """Boids 三力合成，返回像素级推力 (fx, fy)。

    mates: [(cx, cy, dir), ...]，dir 为 ±1。
    """
    if params is None:
        params = DEFAULT_FLOCK
    sep_x = sep_y = sep_w = 0.0
    coh_x = coh_y = coh_n = 0
    ali_sum = 0
    ali_n = 0
    body_r = max(1.0, min(half_w, half_h))
    overlapping = False

    for m in mates:
        m_cx = m[0]
        m_cy = m[1]
        m_dir = m[2] if len(m) > 2 else 1
        dx = m_cx - self_cx
        dy = m_cy - self_cy
        dist = math.hypot(dx, dy)
        # 完全/几乎重合：跳过（合力归零 → 调用方走 360° 随机散开）
        if dist < 2:
            continue
        if dist > params['neighbourRadius']:
            continue
        if dist < 2 * body_r:
            overlapping = True
        nx = dx / dist
        ny = dy / dist
        if dist < params['separationRadius']:
            strength = (params['separationRadius'] - dist) / params['separationRadius']
            sep_x -= nx * strength
            sep_y -= ny * strength
            sep_w += strength
        if dist < params['cohesionRadius']:
            coh_x += nx
            coh_y += ny
            coh_n += 1
        ali_sum += m_dir
        ali_n += 1

    if sep_w <= 0 and coh_n <= 0 and ali_n <= 0:
        return 0.0, 0.0

    coh_scale = 0.0 if overlapping else 1.0
    fx = 0.0
    fy = 0.0
    if sep_w > 0:
        mag = math.hypot(sep_x, sep_y) or 1.0
        k = params['separationWeight'] * params['separationRadius'] * min(1.0, sep_w)
        fx += (sep_x / mag) * k
        fy += (sep_y / mag) * k
    if coh_n > 0 and coh_scale > 0:
        mag = math.hypot(coh_x, coh_y) or 1.0
        k = params['cohesionWeight'] * params['cohesionRadius'] * 0.5
        fx += (coh_x / mag) * k
        fy += (coh_y / mag) * k
    if ali_n > 0:
        majority = 1 if ali_sum > 0 else (-1 if ali_sum < 0 else cur_dir)
        if majority != cur_dir:
            fx += majority * params['alignmentWeight'] * 40.0
    return fx, fy


def wall_repel(gap, side, overlap, params=None):
    """屏幕边缘反斥力（px，带方向）。overlap 不够时不触发（单独贴墙豁免）。"""
    if params is None:
        params = DEFAULT_FLOCK
    if overlap <= params['wallRepelOverlap']:
        return 0.0
    if gap <= params['wallRepelHardPx']:
        return side * params['wallRepelPx']
    if gap >= params['wallRepelRangePx']:
        return 0.0
    k = 1.0 - (gap - params['wallRepelHardPx']) / (params['wallRepelRangePx'] - params['wallRepelHardPx'])
    return side * params['wallRepelPx'] * k


def steer_target(cx, cy, half_w, half_h, cur_dir, mates, distance, gaps, overlap, params=None):
    """把群体合力 + 墙边反斥力 + 随机方向合成成"本段位移目标"。

    只调制，不产生位移：distance 由调用方基于随机 + overlap 加速给出。
    distance 已含 speedUp，内部再乘会二次加速 → 内部不乘。

    gaps = (gap_left, gap_right, gap_top, gap_bottom)，各值为身体边缘到该侧屏幕边的距离。
    返回 (target_x, target_y, dir, has_force)。
    """
    if params is None:
        params = DEFAULT_FLOCK
    fx, fy = flock_force(cx, cy, half_w, half_h, cur_dir, mates, params)
    gap_left, gap_right, gap_top, gap_bottom = gaps

    near_left = gap_left <= gap_right
    if near_left:
        wx = wall_repel(gap_left, 1, overlap, params)
    else:
        wx = wall_repel(gap_right, -1, overlap, params)

    near_top = gap_top <= gap_bottom
    if near_top:
        wy = wall_repel(gap_top, 1, overlap, params)
    else:
        wy = wall_repel(gap_bottom, -1, overlap, params)

    push_x = fx + wx
    push_y = fy + wy
    blend = params['blend']
    mag = math.hypot(push_x, push_y)

    if mag > 1e-6:
        push_dir = 1 if push_x >= 0 else -1
        mixed = cur_dir * (1.0 - blend) + push_dir * blend
        new_dir = 1 if mixed >= 0 else -1
        dy = distance * (push_y / mag)
        return cx + new_dir * distance, cy + dy, new_dir, True

    # 无合力（完全重合 / 无邻居）：纯随机方向。这里只返回朝 cur_dir 的方向，
    # 由调用方在 has_force=False 时自行重随机（360° 全角度）。
    return cx + cur_dir * distance, cy, cur_dir, False


def speed_up_for(overlap, params=None):
    """越挤越快，上限 +50%。"""
    if params is None:
        params = DEFAULT_FLOCK
    return 1.0 + params['speedGain'] * min(1.0, max(0.0, overlap))


def roam_chance_for_overlap(overlap, p=None):
    """overlap → 漫游概率。floor 处跳变到 start，再线性升到 peak。"""
    if p is None:
        p = DEFAULT_ROAM
    if overlap <= p['floor']:
        return p['base']
    if overlap >= p['peakOverlap']:
        return p['peak']
    span = p['peakOverlap'] - p['floor']
    if span <= 0:
        return p['peak']
    t = (overlap - p['floor']) / span
    return p['start'] + (p['peak'] - p['start']) * t


# 兼容别名（window.py 里已有 import）
get_roam_probability = roam_chance_for_overlap


def pick_roam_tier(overlap, p=None):
    """overlap → 移动档位：越挤越选远距离。"""
    if p is None:
        p = DEFAULT_ROAM
    if overlap < p['tierStepMax']:
        return 'step'
    if overlap > p['tierCrabMax']:
        return 'run'
    return 'crab'


# ---------------------------------------------------------------------------
# 单体模式硬碰撞（推箱子）
#
# 与上面的 Boids **完全不同的一套语义**：
#   · 上面的群体模式**刻意允许重叠**（软推力、越挤越快散开）；
#   · 这里是单体模式，用户要求"撞到就推着移动"，所以是硬约束、不允许重叠。
# 两者互不调用，群体模式一行不改。
#
# 求解方式 = 推箱子：
#   撞人者沿移动方向前进 → 贴到挡路者就把它顶开同样距离 →
#   被顶者若又撞到下一只，继续链式传下去 →
#   任何一只被屏幕边缘卡住，整条链一起停（撞人者也不前进）。
# ---------------------------------------------------------------------------

COLLIDE_EPS = 1e-6
CONTACT_SOFT_PX = 6.0   # 贴边量级的浅重叠：允许朝"拉开"的方向走，不判成挡路


def aabb_contact(ax, ay, ahw, ahh, bx, by, bhw, bhh, ux, uy):
    """a 沿单位方向 (ux, uy) 前进，首次接触 b 的距离；永不接触返回 None。

    做法是 Minkowski 和：把 b 的半尺寸加到 a 上，问题化成
    「点 (a 中心) 沿 u 何时进入扩张后的矩形」。
    """
    t_enter = -math.inf
    t_exit = math.inf
    for c, h, u in ((ax - bx, ahw + bhw, ux), (ay - by, ahh + bhh, uy)):
        if abs(u) < COLLIDE_EPS:
            # 该轴方向没有分量：只要求这一轴本来就不重叠
            if abs(c) > h:
                return None
            continue
        t1 = (-h - c) / u
        t2 = (h - c) / u
        if t1 > t2:
            t1, t2 = t2, t1
        t_enter = max(t_enter, t1)
        t_exit = min(t_exit, t2)
    if t_enter > t_exit:
        return None
    # ★ 必须用 `<= 0`：两只正好贴边时 t_exit 可能恰为 0，
    #   若判成"接触"会把身后的邻居当成挡路者（链式推会原地空转）。
    if t_exit <= 0:
        return None            # b 整个在身后（或刚好贴边且已错开）
    if t_enter <= 0:
        # 起点已经在扩张盒里 = 起手就重叠（或正好贴边）。
        # 这时**不能一律判成 0**：分离兜底每次只挪几个像素，会长期停在
        # "重叠零点几像素"的状态；若连朝反方向（拉开距离）都返回 0，
        # 那只就彻底卡死、再也走不开（用户 2026-10-31 报的现象）。
        #
        # 但也不能一律放行：完全重合时（多开召唤叠在屏幕正中）两只该交给
        # separate_overlaps 分开，放行会互相穿透。
        # 所以按穿透深度分级：
        #   · 浅重叠（≤ CONTACT_SOFT_PX，贴边量级）→ 看方向：在拉开就放行；
        #   · 深重叠 → 返回 0，交给分离兜底。
        _ox0 = (ahw + bhw) - abs(ax - bx)
        _oy0 = (ahh + bhh) - abs(ay - by)
        if min(_ox0, _oy0) > CONTACT_SOFT_PX:
            return 0.0         # 真的叠在一起 → 交给分离兜底
        # 沿 u 走 1px，只要没有任何一轴变得更深，就是在拉开（或平行滑开）
        _eps = 1.0
        _ox1 = (ahw + bhw) - abs(ax + ux * _eps - bx)
        _oy1 = (ahh + bhh) - abs(ay + uy * _eps - by)
        if _ox1 > _ox0 + 1e-9 or _oy1 > _oy0 + 1e-9:
            return 0.0         # 越走越深 → 确实被挡
        return None            # 没有变深 → 正在拉开，放行
    return t_enter


def blocked_travel(mover, mates, ux, uy, want, clamp_fn):
    """mover 沿单位方向 (ux, uy) 最多走 want；前方有人就停在贴脸处，**不推人**。

    单体模式的漫游位移用它：撞不过去就停在原地把动画播完，而不是把别人顶走。

    参数
      mover    = (cx, cy, half_w, half_h)
      mates    = [(key, cx, cy, half_w, half_h), ...]
      ux, uy   = 单位方向（调用方保证已归一化）
      want     = 本次想走的距离（>= 0）
      clamp_fn = clamp_fn(key, cx, cy) -> (cx, cy)，key 传 None 表示 mover 自己

    返回 (allowed, hit_key, hit_t)
      allowed = 实际能走的距离（0 ≤ allowed ≤ want）
      hit_key = 挡住它的那一只（没挡住则 None）
      hit_t   = 与那一只的首次接触距离（没挡住则 None）

    ★ 起手就重叠时返回 0：交给 `separate_overlaps` 每帧分离一点，
      而不是在这里硬挤 —— 否则两只贴在一起会互相穿透着漂移。
    """
    if want <= COLLIDE_EPS:
        return 0.0, None, None
    cx, cy, hw, hh = mover

    # ① 屏幕边缘：先夹紧，取沿 u 的可行距离
    tx, ty = clamp_fn(None, cx + ux * want, cy + uy * want)
    wall = (tx - cx) * ux + (ty - cy) * uy
    limit = max(0.0, min(want, wall))

    # ② 最近的挡路者（取进入距离最小的那只）
    hit_key = None
    hit_t = math.inf
    for key, mx, my, mhw, mhh in mates:
        t = aabb_contact(cx, cy, hw, hh, mx, my, mhw, mhh, ux, uy)
        if t is not None and t < hit_t:
            hit_t = t
            hit_key = key

    if hit_key is None or hit_t >= limit:
        return limit, None, None
    return max(0.0, hit_t), hit_key, hit_t


SEPARATE_MAX_STEP = 8.0   # 单次分离最多挪这么多 px（防瞬移观感）


def separate_overlaps(entries, clamp_fn, iterations=3, max_step=SEPARATE_MAX_STEP):
    """残留重叠的全局分离（兜底）。

    为什么需要它：漫游只在"已经贴住"时停步，但**起手就叠在一起**的情形
    （多开召唤时全体叠在屏幕正中）谁都不许动 —— 那时靠这里每帧把重叠
    推开一点，几次迭代后各自散开，随后才谈得上漫游。

    沿**穿透最浅的轴**各退一半（一方贴墙时另一方退全部），
    单次最多挪 max_step px，多迭代几次逼近。

    entries: [(key, cx, cy, hw, hh), ...]
    返回  [(key, new_cx, new_cy), ...]，只含**位置有变化**的项。
    """
    if len(entries) < 2:
        return []
    state = {}
    for key, cx, cy, hw, hh in entries:
        state[key] = [float(cx), float(cy), float(hw), float(hh)]
    before = {k: (v[0], v[1]) for k, v in state.items()}

    keys = list(state.keys())
    for _ in range(max(1, int(iterations))):
        any_fix = False
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a = state[keys[i]]
                b = state[keys[j]]
                ox = (a[2] + b[2]) - abs(a[0] - b[0])
                oy = (a[3] + b[3]) - abs(a[1] - b[1])
                if ox <= COLLIDE_EPS or oy <= COLLIDE_EPS:
                    continue
                any_fix = True
                # 沿穿透最浅的轴分离
                if ox < oy:
                    axis, pen = 0, ox
                    sign = 1.0 if a[0] <= b[0] else -1.0
                else:
                    axis, pen = 1, oy
                    sign = 1.0 if a[1] <= b[1] else -1.0
                pen = min(pen, max_step)
                half = pen / 2.0

                def _try(key, amount):
                    """沿 axis 把 key 挪 amount（带符号），返回实际挪到的量。"""
                    st = state[key]
                    nx = st[0] + (amount if axis == 0 else 0.0)
                    ny = st[1] + (amount if axis == 1 else 0.0)
                    tx, ty = clamp_fn(key, nx, ny)
                    got = (tx - st[0]) if axis == 0 else (ty - st[1])
                    st[0], st[1] = tx, ty
                    return got

                # a 往 -sign 退、b 往 +sign 退，各退一半
                got_a = _try(keys[i], -sign * half)
                got_b = _try(keys[j], sign * half)
                # 谁被墙吃掉了额度，就由另一方补上（同方向继续退）
                want_a = -sign * half
                want_b = sign * half
                lack_a = abs(want_a) - abs(got_a)
                lack_b = abs(want_b) - abs(got_b)
                if lack_a > COLLIDE_EPS and lack_b <= COLLIDE_EPS:
                    _try(keys[j], sign * (half + lack_a))
                elif lack_b > COLLIDE_EPS and lack_a <= COLLIDE_EPS:
                    _try(keys[i], -sign * (half + lack_b))
        if not any_fix:
            break

    out = []
    for k, v in state.items():
        if abs(v[0] - before[k][0]) > 1e-9 or abs(v[1] - before[k][1]) > 1e-9:
            out.append((k, v[0], v[1]))
    return out