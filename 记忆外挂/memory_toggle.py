"""memory_toggle.py —— 桌宠菜单里的「记忆模式」开关

本文件属于**记忆外挂**，与桌宠核心功能完全解耦
================================================

桌宠本体只在两处碰外挂：
  1. `src/runtime/window.py` 右键菜单「技能 ▸」里加一项（整块包 try/except）；
  2. `src/runtime/window.py` 的 `poll_state()` 里调 `_poll_memory_bridge()`（同样包 try/except）。

删掉整个 `记忆外挂/` 目录，桌宠一切照常 —— 动作、漫游、拖拽、抛掷、多开、
群体、右键菜单、托盘全部不受影响。

本文件负责什么
==============
只负责**开关**：把"记忆模式"这个状态同步到两个地方，让桌宠和 DSH 插件都知道。

  ① 桌宠进程内：`store.set_memory_mode(True/False)` —— 立刻生效，
     关掉原生 `_fire_whisper` 定时器，改由灵魂驱动。
  ② 磁盘：`<工程根>/记忆外挂/state.json` —— 持久化，桌宠重启后状态不丢。

**状态以磁盘为准**（与 `skill_toggle` 同一思路）：每次读 state.json 现场判断，
你在外面手动改过文件，菜单也会自动跟上。

为什么不在这里调 LLM / 读记忆
==============================
那是「灵魂」（DSH 插件）的事，在另一个工程里（C 盘 `鱼的记忆/`）。
本文件刻意只做开关，一行网络请求、一行 LLM 调用都没有 ——
这样外挂删掉、灵魂没装、DSH 没开，桌宠都不受影响。
"""
import json
import os
from pathlib import Path

STATE_FILE = "state.json"

DEFAULT_STATE = {
    "enabled": False,      # 记忆模式开关
    "updatedAt": None,     # 最后改动时间（ISO 字符串，仅便于排查）
}


def bridge_dir() -> Path:
    """记忆外挂目录（本文件所在目录）。"""
    return Path(__file__).resolve().parent


def state_path() -> Path:
    return bridge_dir() / STATE_FILE


def is_installed() -> bool:
    """外挂是否在位。本文件能被加载，就说明在位。"""
    return True


def read_state() -> dict:
    """读磁盘状态。文件缺失/损坏一律回默认值，不抛异常。"""
    try:
        raw = state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return dict(DEFAULT_STATE)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        merged["enabled"] = bool(merged.get("enabled"))
        return merged
    except Exception:
        return dict(DEFAULT_STATE)


def write_state(state: dict) -> bool:
    """原子写盘（tmp + rename），避免写到一半断电留下半个文件。"""
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def is_enabled() -> bool:
    """记忆模式是否开启（以磁盘为准）。"""
    return bool(read_state().get("enabled"))


def set_enabled(on: bool, store=None) -> bool:
    """设置开关。同时写磁盘 + 通知桌宠进程内 store。返回最终状态。"""
    import datetime
    st = read_state()
    st["enabled"] = bool(on)
    try:
        st["updatedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    except Exception:
        st["updatedAt"] = None
    write_state(st)
    # 通知桌宠进程（store 是活的，立刻生效；写盘只是为了重启后不丢）
    try:
        if store is not None and hasattr(store, "set_memory_mode"):
            store.set_memory_mode(bool(on))
    except Exception:
        pass
    return bool(on)


def toggle(store=None) -> bool:
    """翻转开关，返回翻转后的状态。"""
    return set_enabled(not is_enabled(), store=store)


def menu_label() -> str:
    """菜单上显示的文字。"""
    return "记忆模式（已开启）" if is_enabled() else "记忆模式"


def sync_to_store(store) -> bool:
    """桌宠启动时把磁盘状态同步进进程内 store（否则重启后磁盘是开、store 是关）。"""
    try:
        on = is_enabled()
        if store is not None and hasattr(store, "set_memory_mode"):
            store.set_memory_mode(on)
        return on
    except Exception:
        return False
