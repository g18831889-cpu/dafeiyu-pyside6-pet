"""user_settings.py —— 记住桌宠大小

需求：切换大小时立即写盘，下次启动沿用。

存储位置：程序根目录的 `settings.json`（与 main.py 同级）。
这样"整个文件夹复制到别处"时设置跟着走；目录不可写时静默跳过，
绝不影响桌宠运行。
"""
import json
import os
from pathlib import Path

FILENAME = "settings.json"

# 只保存大小与 DSH 绑定。行为模式（群体/单体）不持久化。
_DEFAULTS = {
    "scale": 0.75,        # 具体数值；自适应模式下也存当前实际值
    # DSH exe 绑定（用户手动选择后写入）：
    #   None  = 未绑定 → 点「打开 DSH」会弹文件选择框让你指定
    #   "C:\\...\\DeepSeek Harness.exe" = 已绑定，直接启动
    "dsh_exe": None,
}


def settings_path(app_root):
    return Path(app_root) / FILENAME


def load(app_root):
    """读取设置。缺失/损坏一律返回默认值，不抛异常。"""
    out = dict(_DEFAULTS)
    p = settings_path(app_root)
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "scale" in data:
                    out["scale"] = data["scale"]
                if "dsh_exe" in data:
                    out["dsh_exe"] = data["dsh_exe"]
    except Exception:
        pass
    return out


def save(app_root, **kw):
    """保存设置。失败静默（只读目录、权限不足等）。"""
    try:
        p = settings_path(app_root)
        cur = load(app_root)
        for key in ("scale", "dsh_exe"):
            if key in kw:
                cur[key] = kw[key]
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False
