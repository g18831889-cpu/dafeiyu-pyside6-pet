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
    return max(0.0, t_enter)


def overlap_depth(ax, ay, ahw, ahh, bx, by, bhw, bhh):
    """两个轴对齐框的穿透深度 (dx, dy)：都 >0 表示重叠，取小的那个轴作为脱离方向。"""
    ox = (ahw + bhw) - abs(ax - bx)
    oy = (ahh + bhh) - abs(ay - by)
    return ox, oy


def resolve_push(mover, mates, dx, dy, clamp_fn):
    """单体模式推箱子碰撞：mover 想走 (dx, dy)，沿途把挡路的顶开。

    参数
      mover   = (cx, cy, half_w, half_h)  身体中心 + 半尺寸
      mates   = [(key, cx, cy, half_w, half_h), ...]  其他单体鱼
      clamp_fn(key, cx, cy) -> (cx, cy)
                把某只的中心夹进它允许的范围（屏幕边缘）。
                key 为 None 表示 mover 自己。
    返回
      (new_cx, new_cy, pushes)
        pushes = [(key, new_cx, new_cy), ...]  **被顶开**的每一只的最终中心
                （没动过的不会出现在这里，调用方可以据此跳过 setGeometry）

    设计要点（推箱子语义）
      · 撞人者前进，被撞者被顶开同样的量，链式一路传下去；
      · 任何一环被屏幕边缘卡住 → 整条链一起停，撞人者也不前进；
      · **起手就已经重叠时**（比如刚召唤完叠在屏幕正中）先按穿透深度把
        挡路者沿推进方向推开，把它"吐"出去，而不是两只贴着一起漂移。

    注意：这是纯数学，不依赖 Qt；每帧调用一次，复杂度 O(N²)，N=10 时约百次判定。
    """
    dist = math.hypot(dx, dy)
    if dist < COLLIDE_EPS:
        return mover[0], mover[1], []
    ux, uy = dx / dist, dy / dist

    state = {None: [float(mover[0]), float(mover[1]), float(mover[2]), float(mover[3])]}
    for key, cx, cy, hw, hh in mates:
        state[key] = [float(cx), float(cy), float(hw), float(hh)]

    moved = set()      # 被顶开的 key
    visiting = set()   # 防环

    def _advance(key, amount, depth=0):
        """让 key 沿 u 前进至多 amount，返回实际前进距离。

        语义 = 推箱子：
          · 没挡路 → 走满；
          · 撞上 → 先走到贴脸处，再把挡路者顶开，**自己跟着它前进的量走**；
          · 挡路者顶不动（贴墙/链尾卡死）→ 自己也就停在贴脸处。
        """
        if amount <= COLLIDE_EPS or depth > len(state) + 1:
            return 0.0
        cx, cy, hw, hh = state[key]

        # ① 屏幕边缘：先夹紧，取沿 u 的可行距离
        tx, ty = clamp_fn(key, cx + ux * amount, cy + uy * amount)
        wall = (tx - cx) * ux + (ty - cy) * uy
        limit = min(amount, max(0.0, wall))
        if limit <= COLLIDE_EPS:
            return 0.0

        # ② 找最近的挡路者（取进入距离最小的那只）
        hit_key = None
        hit_t = math.inf
        for other, st in state.items():
            if other == key or other in visiting:
                continue
            t = aabb_contact(cx, cy, hw, hh, st[0], st[1], st[2], st[3], ux, uy)
            if t is not None and t < hit_t:
                hit_t = t
                hit_key = other

        # ③ 畅通：直接走
        if hit_key is None or hit_t >= limit:
            state[key][0] = cx + ux * limit
            state[key][1] = cy + uy * limit
            if key is not None:
                moved.add(key)
            return limit

        # ④ 先走到贴脸处
        state[key][0] = cx + ux * hit_t
        state[key][1] = cy + uy * hit_t
        if key is not None and hit_t > COLLIDE_EPS:
            moved.add(key)

        rest = limit - hit_t
        if rest <= COLLIDE_EPS:
            return hit_t

        # ⑤ 起手就重叠 → 先把被撞者沿 u 吐出去（不算撞人者的行程）
        _unstick(key, hit_key, ux, uy)

        # ⑥ 顶它。它让开多少，自己就跟着走多少。
        visiting.add(hit_key)
        pushed = _advance(hit_key, rest, depth + 1)
        visiting.discard(hit_key)

        state[key][0] += ux * pushed
        state[key][1] += uy * pushed
        if key is not None and pushed > COLLIDE_EPS:
            moved.add(key)
        return hit_t + pushed

    def _unstick(a_key, b_key, ux, uy):
        """a 与 b 已重叠时，沿 u 把 b 顶出重叠区，返回顶出的距离。

        ★ 必须沿 **u 本身** 退到不重叠，而不是按最短脱离轴：
          按最短轴会把 b 往侧面弹，与"被推着沿同一方向移动"不符。
          做法是把 b 沿 u 二分/解析推进到「不再重叠」的最小距离。
        """
        a = state[a_key]
        b = state[b_key]
        if overlap_depth(a[0], a[1], a[2], a[3], b[0], b[1], b[2], b[3])[0] <= COLLIDE_EPS:
            return 0.0
        ox, oy = overlap_depth(a[0], a[1], a[2], a[3], b[0], b[1], b[2], b[3])
        if oy <= COLLIDE_EPS:
            return 0.0
        # 沿 u 退出所需的距离：两个轴分别算出"还要退多少才分开"
        need = 0.0
        if abs(ux) > COLLIDE_EPS:
            need = max(need, ox / abs(ux))
        if abs(uy) > COLLIDE_EPS:
            need = max(need, oy / abs(uy))
        if need <= COLLIDE_EPS:
            return 0.0
        # b 沿 u 后退可能被墙卡住 → 取实际可行量
        tx, ty = clamp_fn(b_key, b[0] + ux * need, b[1] + uy * need)
        got = (tx - b[0]) * ux + (ty - b[1]) * uy
        if got <= COLLIDE_EPS:
            return 0.0
        b[0] += ux * got
        b[1] += uy * got
        moved.add(b_key)
        return got

    _advance(None, dist)
    pushes = [(k, state[k][0], state[k][1]) for k in moved if k is not None]
    return state[None][0], state[None][1], pushes


SEPARATE_MAX_STEP = 8.0   # 单次分离最多挪这么多 px（防瞬移观感）


def separate_overlaps(entries, clamp_fn, iterations=3, max_step=SEPARATE_MAX_STEP):
    """残留重叠的全局分离（兜底）。

    为什么需要它：每只宠物各自调 resolve_push 时，看到的是**上一帧**的邻居位置。
    两只相向而行时，各自都以为对方没动，结算完就会留下重叠。
    这里在一帧的所有移动都落定后统一跑一遍，把残余重叠推开。

    与 resolve_push 的分工：
      · resolve_push = 主动推进时的"推着走"（撞者前进、被撞者被顶，链式）；
      · separate_overlaps = 事后兜底，只处理"已经叠上了"的残余。

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