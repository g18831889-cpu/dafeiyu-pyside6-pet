"""验证「开机自启」菜单项在真实窗口里能正确构建与切换。

不真的弹菜单（exec 会阻塞），把 QMenu.exec 换成收集器，
拿到构建出来的菜单项文本，再断言「开机自启：开/关」在里面，
并确认点击后注册表真的变了（用测试专用值名，不碰真实项）。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtCore import QPoint

app = QApplication(sys.argv)

from src.host import autostart

# 测试专用注册表值名
TEST_NAME = 'DaFeiyuPet__menu_tmp'
REAL_NAME = autostart.AUTOSTART_NAME
autostart.AUTOSTART_NAME = TEST_NAME

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ''))


tmp = Path(tempfile.mkdtemp())
app_root = tmp / 'app'
(app_root / 'frames').mkdir(parents=True)
(app_root / 'main.py').write_text('# fake\n', encoding='utf-8')

import src.runtime.window as W

# 拦住 exec，收集菜单项
captured = {'actions': [], 'menus': []}
_orig_exec = QMenu.exec


def fake_exec(self, *a, **kw):
    for act in self.actions():
        captured['actions'].append(act.text())
    return None


QMenu.exec = fake_exec

try:
    from src.host.config_loader import load_config
    cfg = load_config(app_root / 'frames', PROJ / 'config.jsonc')

    class FakeStore:
        """最小的 store 桩：只实现 window.py 会碰到的几个方法。"""
        def get(self, key, default=None):
            return default

        def get_state(self):
            return {}

    pet = W.PetWindow(app_root / 'frames', cfg, FakeStore())
    pet.is_main = True
    pet.show()
    app.processEvents()

    autostart.set_enabled(False, pet.base_dir)

    print("=== 1. 菜单项存在且默认「关」 ===")
    captured['actions'].clear()
    pet._show_context_menu(type('E', (), {'globalPos': lambda s: QPoint(10, 10)})())
    labels = captured['actions']
    auto_items = [t for t in labels if t.startswith('开机自启')]
    check("菜单里有「开机自启」项", len(auto_items) == 1, str(auto_items))
    check("默认状态是「开机自启：关」",
          auto_items and auto_items[0] == '开机自启：关', str(auto_items))

    print()
    print("=== 2. 点击切换后注册表真的变了 ===")
    check("切换前注册表为关", autostart.is_enabled(pet.base_dir) is False)
    pet.toggle_autostart()
    check("点击后注册表变为开", autostart.is_enabled(pet.base_dir) is True)

    print()
    print("=== 3. 再开菜单应显示「开」 ===")
    captured['actions'].clear()
    pet._show_context_menu(type('E', (), {'globalPos': lambda s: QPoint(10, 10)})())
    auto_items2 = [t for t in captured['actions'] if t.startswith('开机自启')]
    check("菜单跟随状态显示「开机自启：开」",
          auto_items2 and auto_items2[0] == '开机自启：开', str(auto_items2))

    print()
    print("=== 4. 再点一次回到「关」 ===")
    pet.toggle_autostart()
    check("再点后注册表变回关", autostart.is_enabled(pet.base_dir) is False)
    captured['actions'].clear()
    pet._show_context_menu(type('E', (), {'globalPos': lambda s: QPoint(10, 10)})())
    auto_items3 = [t for t in captured['actions'] if t.startswith('开机自启')]
    check("菜单显示「开机自启：关」",
          auto_items3 and auto_items3[0] == '开机自启：关', str(auto_items3))

    print()
    print("=== 5. 与其它菜单栏样式一致 ===")
    # 同批次的「行为：」「摇晃：」都是「名称：值」格式
    btn_style = [t for t in labels if '：' in t]
    check("同批次至少 3 个「名称：值」格式项",
          len(btn_style) >= 3, str(btn_style[:6]))
    check("「开机自启」用的也是全角冒号",
          auto_items and '：' in auto_items[0])

finally:
    QMenu.exec = _orig_exec
    try:
        autostart.set_enabled(False, pet.base_dir)
    except Exception:
        pass
    autostart.AUTOSTART_NAME = REAL_NAME

# 清理确认
import winreg
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as k:
    try:
        winreg.QueryValueEx(k, TEST_NAME)
        left = True
    except FileNotFoundError:
        left = False
check("测试注册表项已清除", left is False)

ok = sum(1 for _, c, _ in results if c)
print()
print(f"{ok}/{len(results)} PASS")
sys.exit(0 if ok == len(results) else 1)
