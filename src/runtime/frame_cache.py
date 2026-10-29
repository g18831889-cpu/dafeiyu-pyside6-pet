"""src/runtime/frame_cache.py - 动作帧缓存（按显示尺寸预缩放）

两个优化目标
------------
1. **内存**：原来 `self.pms = [QPixmap.fromImage(img) ...]` 每只宠物各持有一整份动作
   （241 帧 × 640×360×4B ≈ 222MB/只），30 只就是 6.6GB。改为按 (动作, 尺寸) 缓存，
   同动作同尺寸的宠物共享同一批 QPixmap（QPixmap 隐式共享，不复制像素）。
   **注意：共享的是帧数据，不是动作选择——每只照常随机挑自己的动作。**

2. **CPU**：原来 paintEvent 每帧用 `drawPixmap(QRectF缩放)` + SmoothPixmapTransform
   把 640×360 重采样到显示尺寸，30 只 × 24fps = 每秒 720 次 CPU 重采样。
   现在**加载时就缩放到显示尺寸**，paintEvent 1:1 贴图，没有重采样。

锚点来自大肥鱼：它 `DECODED_FRAME_CACHE = 24` 按需解码、`drawPixmap(QRectF, pix, QRectF源)`
交给 Qt 合成；这里因为单进程承载几十只，改成"按尺寸预缩放 + 有界动作缓存"。
"""
import os
from collections import OrderedDict


# 同时驻留几个动作的帧。3 个 ≈ 3 × 212MB（640×360 原图 × 241 帧）。
# ★ 2026-10-30：帧改为**原始尺寸**（不再预缩放），因为倾斜/Q弹要做
#   rotate/scale，预缩放的图再变换就是二次重采样（锐度只剩 67.6%）。
#   代价是单帧内存变大（506KB → 900KB），但不再同时存原图+缩放图，
#   总内存反而从 357MB 降到 331MB。
MAX_ENTRIES = max(1, int(os.environ.get("DSH_PET_FRAME_CACHE_ACTIONS", "3")))

_cache = OrderedDict()  # (name, 0, 0) -> list[QImage]
_hits = 0
_misses = 0


def get(name, w, h):
    global _hits
    key = (name, w, h)
    frames = _cache.get(key)
    if frames is None:
        return None
    _cache.move_to_end(key)
    _hits += 1
    return frames


def prepare(name, w, h, images):
    """登记已解码/已缩放的帧列表（QImage），命中缓存则直接复用。

    缩放已经在 LoadThread 里做完（QImage 线程安全），这里**不做任何像素转换**——
    原先在这里做 241 次 QPixmap.fromImage+scaled，单次 130~180ms，正是卡帧根源。
    """
    global _misses
    frames = get(name, w, h)
    if frames is not None:
        return frames
    if not images:
        return images
    _cache[(name, w, h)] = list(images)
    _cache.move_to_end((name, w, h))
    _misses += 1
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)
    return _cache[(name, w, h)]

