"""src/host/config_loader.py - 解析并过滤 config.jsonc"""
import re
import json

def load_config(base_dir, config_file):
    print("Loading config...", flush=True)
    avail = set()
    if base_dir.exists():
        all_dirs = [d for d in base_dir.iterdir() if d.is_dir()]
        all_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        avail_dirs = all_dirs  # 抽帧已全部完成，不再排除最新目录
        avail = {d.name for d in avail_dirs}
        print(f"Available actions: {len(avail)}", flush=True)
        
    weights = {'idle': 10, 'turn': 5, 'move': 5}
    pools = {'idle': ['待机呼吸休闲'], 'turn': ['东张西望'], 'move': [], 'categories': []}
    move_params = {}
    
    if config_file.exists():
        try:
            txt = config_file.read_text(encoding='utf-8')
            txt = re.sub(r'//.*', '', txt)
            txt = re.sub(r'/\*[\s\S]*?\*/', '', txt)
            txt = re.sub(r',\s*([\]}])', r'\1', txt)
            cfg = json.loads(txt)
            anims = cfg.get('animations', {})
            if 'idle' in anims: pools['idle'] = anims['idle']
            if 'turn' in anims: pools['turn'] = anims['turn']
            if 'moves' in anims and 'actions' in anims['moves']:
                # moves.default 是每个动作的兜底参数（leadSec/tailSec/minDist/maxDist/margin）。
                # 原来直接取 a['params']，没配 leadSec/tailSec 的动作（螃蟹走路、原地漂浮踏步）
                # 会取到 0 → roam_ratio 窗口退化成全程线性 → 画面已经停住、位置却还在滑。
                _move_default = anims['moves'].get('default') or {}
                for a in anims['moves']['actions']:
                    pools['move'].append(a['name'])
                    _params = dict(_move_default)
                    _params.update(a.get('params') or {})
                    move_params[a['name']] = _params
                # ★ 单体模式的移动档权重（run/crab/step），对齐 pet 的 moves.tiers
                tiers = (anims['moves'].get('tiers') or {})
                if isinstance(tiers, dict) and tiers:
                    pools['move_tier_weights'] = {
                        k: float((tiers.get(k) or {}).get('weight', 0) or 0)
                        for k in ('run', 'crab', 'step')
                    }
            if 'categories' in anims:
                for c in anims['categories']:
                    pools['categories'].append({
                        'id': c.get('id', ''),
                        'weight': c.get('weight', 0),
                        'actions': c.get('actions', []),
                        # ★ 朝右（被镜像）时不播这类：文字会颠倒。对齐 pet 的 pickWeightedCategory
                        'noMirror': bool(c.get('noMirror', False)),
                    })
            if 'animationWeights' in cfg:
                w = cfg['animationWeights']
                weights['idle'] = w.get('idle', 10)
                weights['turn'] = w.get('turn', 5)
                weights['move'] = w.get('move', 5)
        except Exception as e:
            print(f"Config load error: {e}", flush=True)
            
    pools['idle'] = [a for a in pools['idle'] if a in avail]
    pools['turn'] = [a for a in pools['turn'] if a in avail]
    pools['move'] = [a for a in pools['move'] if a in avail]
    
    valid_cats = []
    for cat in pools['categories']:
        valid_acts = [a for a in cat['actions'] if a in avail]
        if valid_acts:
            valid_cats.append({
                'id': cat.get('id', ''),
                'weight': cat['weight'],
                'actions': valid_acts,
                'noMirror': cat.get('noMirror', False),
            })
    pools['categories'] = valid_cats
    
    print(f"Pools filtered: idle={len(pools['idle'])}, turn={len(pools['turn'])}, move={len(pools['move'])}, cats={len(valid_cats)}", flush=True)
    if not pools['idle'] and not pools['move'] and not valid_cats:
        print("Fallback to all available", flush=True)
        pools['idle'] = list(avail)
        
    # 提取工作状态动作池与文案
    work_status_anims = []
    work_status_texts = []
    cfg = {}                      # ★ 必须在 try 外初始化：配置文件缺失/解析失败时
    if config_file.exists():      #   下面 return 仍要读 cfg，否则 UnboundLocalError 直接崩
        try:
            cfg = json.loads(txt)
            anims = cfg.get('animations', {})
            if 'events' in anims and 'workStatus' in anims['events']:
                work_status_anims = anims['events']['workStatus']
            work_status_texts = cfg.get('workStatusTexts', [])
        except Exception:
            cfg = {}
            work_status_anims = []
            work_status_texts = []

    return weights, pools, move_params, work_status_anims, work_status_texts, cfg.get("animations", {})