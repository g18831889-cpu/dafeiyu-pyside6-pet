"""src/shared/pickers.py - 动作加权抽签与距离判断"""
import random

def pick_next_action(weights, pools, current_name, flock_enabled=False):
    choices = []
    if flock_enabled:
        # 群体模式：move 池打包成漫游子池占 0.25，其余池按原相对比例占 0.75
        normal = []
        if pools.get('idle'): normal.append(('idle', weights.get('idle', 0), pools['idle']))
        if pools.get('turn'): normal.append(('turn', weights.get('turn', 0), pools['turn']))
        for cat in pools.get('categories', []):
            normal.append(('cat', cat['weight'], cat['actions']))
        norm_total = sum(c[1] for c in normal) or 1.0
        for c in normal:
            choices.append((c[0], 0.75 * c[1] / norm_total, c[2]))
        if pools.get('move'):
            choices.append(('move', 0.25, pools['move']))
    else:
        if pools.get('idle'): choices.append(('idle', weights.get('idle', 0), pools['idle']))
        if pools.get('turn'): choices.append(('turn', weights.get('turn', 0), pools['turn']))
        if pools.get('move'): choices.append(('move', weights.get('move', 0), pools['move']))
        for cat in pools.get('categories', []):
            choices.append(('cat', cat['weight'], cat['actions']))
        
    choices = [c for c in choices if c[1] > 0]
    if not choices: return None, None
    
    total_w = sum(c[1] for c in choices)
    r = random.uniform(0, total_w)
    cum = 0
    picked_pool = choices[0][2]
    cat_type = choices[0][0]
    
    for c in choices:
        cum += c[1]
        if r <= cum:
            picked_pool = c[2]
            cat_type = c[0]
            break
            
    avail = [a for a in picked_pool if a != current_name]
    if not avail: avail = picked_pool
    
    return (random.choice(avail) if avail else None), cat_type

def pick_big_pool_equal(pools, current_name, include_moves=False, facing_right=False):
    """大池抽取：idle + turn +（可选 move）+ 全部 categories，**等概率**抽一个。

    · 群体模式（include_moves=False）：对齐 dsh-pet 的 pickBigPoolAction，
      非移动动作平分概率；漫游由 overlap 单独判定。
    · **单体模式（include_moves=True）**：所有动作统一进大池，
      抽到移动动作就执行位移 —— 漫游绑定在动作上，不预先判定。
    · facing_right=True 时剔除 noMirror 分类（文字类），避免镜像后文字反着。
    """
    no_mirror = set()
    cats = pools.get('categories') or []
    if facing_right:
        for c in cats:
            if c.get('noMirror'):
                no_mirror.update(c.get('actions') or [])

    pool = []
    pool.extend(pools.get('idle') or [])
    pool.extend(pools.get('turn') or [])
    if include_moves:
        pool.extend(pools.get('move') or [])
    for cat in cats:
        pool.extend(cat.get('actions') or [])

    pool = [a for a in pool if a not in no_mirror]
    if not pool:
        return None
    avail = [a for a in pool if a != current_name]
    if not avail:
        avail = pool
    return random.choice(avail)


# ---------------------------------------------------------------------------
# 单体模式：复现 pet（Electron）的调度链（src/shared/pickers.ts 的同一份决策）
# ---------------------------------------------------------------------------

def roll_kind(roll, weights, fixed=False):
    """pet 的 rollKind：idle / turn / move / action 四档。

    idle/turn/move 直接吃百分比，剩余全归 action（分类池）。
    `fixed=True`（宠物固定）时把 turn/move 按 0 算。
    """
    idle = float(weights.get('idle', 0) or 0)
    turn = 0.0 if fixed else float(weights.get('turn', 0) or 0)
    move = 0.0 if fixed else float(weights.get('move', 0) or 0)
    if roll < idle / 100.0:
        return 'idle'
    if roll < (idle + turn) / 100.0:
        return 'turn'
    if roll < (idle + turn + move) / 100.0:
        return 'move'
    return 'action'


def pick_move_tier_weighted(tiers, tier_weights, current_name):
    """单体模式的移动档：run / crab / step **按各自权重**抽一个动作。

    与群体模式的"按距离选档"是两套逻辑：
      · 群体：速度决定动作（pickMoveTierByDistance）
      · 单体：纯权重（pet 的 pickMoveTier，flockPull=0 时等价于原始权重）
    """
    choices = []
    for name in ('run', 'crab', 'step'):
        acts = tiers.get(name) or []
        w = float(tier_weights.get(name, 0) or 0)
        if acts and w > 0:
            choices.append((w, acts))
    if not choices:
        return None
    total = sum(c[0] for c in choices)
    r = random.uniform(0, total)
    cum = 0.0
    for w, acts in choices:
        cum += w
        if r <= cum:
            cand = [a for a in acts if a != current_name] or acts
            return random.choice(cand)
    return None


def pick_category_action(categories, idle_pool, facing_right, current_name):
    """pet 的 pickCategoryAction：按分类权重抽一个分类，再从该分类随机取动作。

    朝右（被镜像）时跳过 `noMirror` 分类——文字类镜像会颠倒。
    没有可用分类则回退到 idle 池。
    """
    usable = []
    for c in categories:
        acts = c.get('actions') or []
        if not acts:
            continue
        if facing_right and c.get('noMirror'):
            continue
        usable.append(c)
    if not usable:
        return random.choice(idle_pool) if idle_pool else None
    total = sum(float(c.get('weight', 0) or 0) for c in usable) or 1.0
    r = random.uniform(0, total)
    cum = 0.0
    for c in usable:
        cum += float(c.get('weight', 0) or 0)
        if r <= cum:
            cand = [a for a in c['actions'] if a != current_name] or c['actions']
            return random.choice(cand)
    return None
