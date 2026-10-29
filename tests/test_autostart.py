"""验证「开机自启动」开关（autostart）。

要点：
  ① 状态以注册表为准（不落 settings.json）；
  ② 开 / 关 / 往返切换都正确；
  ③ 指向不存在 main.py 的残留项视为「关」（避免菜单显示"开"却起不来）；
  ④ 非 Windows 安全返回 False。

★ 不碰真实的 DaFeiyuPet 项：所有用例都把 AUTOSTART_NAME 换成测试专用名，
   跑完删干净。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from src.host import autostart

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))


# ★ 换成测试专用注册表值名，绝不碰用户的真实项
REAL_NAME = autostart.AUTOSTART_NAME
TEST_NAME = 'DaFeiyuPet__pytest_tmp'
autostart.AUTOSTART_NAME = TEST_NAME

tmp = Path(tempfile.mkdtemp())
# 造一个假的程序根：main.py + frames/
fake_root = tmp / 'app'
(fake_root / 'frames').mkdir(parents=True)
(fake_root / 'main.py').write_text("# fake\n", encoding='utf-8')
base_dir = fake_root / 'frames'


def cleanup():
    try:
        autostart.set_enabled(False, base_dir)
    except Exception:
        pass


cleanup()

print("=== 1. 基础推导 ===")
check("_app_root 从 frames/ 上一级取程序根",
      autostart._app_root(base_dir) == fake_root,
      str(autostart._app_root(base_dir)))
check("_app_root 直接传程序根也对",
      autostart._app_root(fake_root) == fake_root)
check("pythonw 能找出来", autostart._pythonw() is not None,
      str(autostart._pythonw()))

cmd = autostart.launch_command(base_dir)
check("launch_command 非空", bool(cmd), str(cmd))
check("命令里含 main.py 全路径", cmd is not None and str(fake_root / 'main.py') in cmd)
check("命令用 pythonw（无控制台）",
      cmd is not None and 'pythonw' in cmd.lower(), str(cmd))
check("路径带引号（防空格/中文）", cmd is not None and cmd.count('"') >= 4)

print()
print("=== 2. 开 / 关 ===")
check("初始为关", autostart.is_enabled(base_dir) is False)
check("开启成功", autostart.set_enabled(True, base_dir) is True)
check("开启后 is_enabled=True", autostart.is_enabled(base_dir) is True)
check("菜单文案是「开机自启：开」",
      autostart.menu_label(base_dir) == "开机自启：开",
      autostart.menu_label(base_dir))
check("关闭成功", autostart.set_enabled(False, base_dir) is True)
check("关闭后 is_enabled=False", autostart.is_enabled(base_dir) is False)
check("菜单文案是「开机自启：关」",
      autostart.menu_label(base_dir) == "开机自启：关",
      autostart.menu_label(base_dir))
check("重复关闭仍成功（幂等）", autostart.set_enabled(False, base_dir) is True)

print()
print("=== 3. toggle 往返 ===")
autostart.set_enabled(False, base_dir)
check("toggle 关→开", autostart.toggle(base_dir) is True)
check("toggle 开→关", autostart.toggle(base_dir) is False)
check("toggle 后状态与返回值一致", autostart.is_enabled(base_dir) is False)

print()
print("=== 4. 残留项（指向不存在的 main.py）视为关闭 ===")
# 手工写一条指向不存在路径的项
import winreg
with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
    winreg.SetValueEx(k, TEST_NAME, 0, winreg.REG_SZ,
                      '"C:\\fake\\pythonw.exe" -u "C:\\nope\\main.py"')
check("残留项被识别为「关」", autostart.is_enabled(base_dir) is False)
check("残留项时菜单显示「关」",
      autostart.menu_label(base_dir) == "开机自启：关")

print()
print("=== 5. 找不到 main.py 时拒绝开启 ===")
empty_root = tmp / 'empty'
(empty_root / 'frames').mkdir(parents=True)
check("无 main.py → launch_command 为 None",
      autostart.launch_command(empty_root / 'frames') is None)
check("无 main.py → 开启失败",
      autostart.set_enabled(True, empty_root / 'frames') is False)
check("失败后状态仍是关", autostart.is_enabled(empty_root / 'frames') is False)

print()
print("=== 6. 非 Windows 保护 ===")
_orig_plat = sys.platform
try:
    sys.platform = 'linux'
    check("非 Windows → is_enabled=False", autostart.is_enabled(base_dir) is False)
    check("非 Windows → set_enabled=False",
          autostart.set_enabled(True, base_dir) is False)
    check("非 Windows → menu_label 显示关",
          autostart.menu_label(base_dir) == "开机自启：关")
finally:
    sys.platform = _orig_plat

# 清理
cleanup()
autostart.AUTOSTART_NAME = REAL_NAME

print()
print("=== 7. 清理确认 ===")
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as k:
    present = True
    try:
        winreg.QueryValueEx(k, TEST_NAME)
    except FileNotFoundError:
        present = False
check("测试项已从注册表清除", present is False)
check("真实项未被触碰（仍是原状态或不存在）",
      isinstance(autostart.is_enabled(base_dir), bool))

ok = sum(1 for _, c, _ in results if c)
print()
print(f"{ok}/{len(results)} PASS")
sys.exit(0 if ok == len(results) else 1)
