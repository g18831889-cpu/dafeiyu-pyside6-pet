"""main.py - 双模桌宠启动入口（带系统托盘）"""
import sys
import os
import argparse
from pathlib import Path
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QPixmap, QAction

from src.host.config_loader import load_config
from src.host.server import start_http_server, start_stdio_server, probe_instance, store
from src.runtime.window import PetWindow

def main():
    # 日志系统：pet.log（常规）/ crash.log（硬崩溃栈）/ qt.log（Qt 警告）
    log = None
    try:
        from src.host.logging_setup import setup_logging
        log = setup_logging(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default='standalone', choices=['standalone', 'stdio'])
    parser.add_argument('--config-dir', type=str, default=None)
    args = parser.parse_args()

    if args.config_dir:
        base_dir = Path(args.config_dir) / 'frames'
        config_file = Path(args.config_dir) / 'config.jsonc'
    else:
        base_dir = Path(__file__).parent / 'frames'
        # 依次尝试：
        #   1) 同目录 config.jsonc —— 打包/独立安装版（自包含，拷到任何地方都能跑）
        #   2) DSH 已安装插件自带的（开发环境，最完整、随插件更新）
        #   3) 改造前的备份（最后兜底）
        here = Path(__file__).parent
        candidates = [
            here / 'config.jsonc',
            Path.home() / '.dsh' / 'profiles' / 'desktop' / 'node_modules' / 'dsh-pet' / 'assets' / 'config.jsonc',
            here.parent / 'backup-dsh-pet-原版' / 'assets' / 'config.jsonc',
        ]
        config_file = next((c for c in candidates if c.is_file()), candidates[-1])

    app = QApplication(sys.argv)

    # 用户设置（上次退出时的大小 / 行为模式）
    app_root = Path(__file__).parent
    try:
        from src.host.user_settings import load as load_settings
        user_cfg = load_settings(app_root)
    except Exception:
        user_cfg = {}

    # 退出前：停全局定时器 + 等解码线程结束（否则退出瞬间会打 QThread 警告）
    try:
        from src.runtime.window import _stop_global_tick, _drain_threads
        app.aboutToQuit.connect(_stop_global_tick)
        app.aboutToQuit.connect(lambda: log and log.info('应用退出'))
        app.aboutToQuit.connect(lambda: _drain_threads(5000))
        if log:
            log.info('退出钩子已连接（aboutToQuit + atexit）')
    except Exception as _e:
        print('[Main] 退出钩子连接失败:', _e, file=sys.stderr, flush=True)
    # 注：用户设置不再在这里保存 —— 改为「切换即写盘」，见 window.set_scale /
    #     toggle_behavior。退出钩子只负责收尾，避免 aboutToQuit 时序问题
    #     （窗口先被销毁 → ALL_PETS 里找不到主体 → 静默不写）。
    app.setQuitOnLastWindowClosed(False) # 关闭窗口不退出程序
    
    config_data = load_config(base_dir, config_file)
    
    if args.mode == 'standalone':
        # 单实例：3080 已被占用说明已有实例在跑，本次不再起第二只
        if not start_http_server(3080):
            print('[Main] 已有实例在运行（端口 3080 被占用），本次不重复启动。', file=sys.stderr, flush=True)
            sys.exit(0)
    else:
        # DSH 拉起时先看有没有独立实例在跑：
        #   · 有 → 只做事件转发代理（不建窗口），避免"独立版 + 插件版"两只桌宠
        #   · 没有 → 正常建窗口 + 断尾求生
        if probe_instance(3080):
            print('[Main] 检测到已在运行的实例，切为事件转发模式（不重复拉起桌宠）。',
                  file=sys.stderr, flush=True)
            start_stdio_server(proxy_to=3080)
            sys.exit(app.exec())

    # ★ 用上次保存的**大小**作为默认（行为模式不持久化）
    _sc = user_cfg.get('scale')
    try:
        _sc = float(_sc) if _sc is not None else None
    except Exception:
        _sc = None
    if _sc is not None and _sc < 0.1:
        _sc = None                    # 非法值兜底
    pet = PetWindow(base_dir, config_data, store, scale=_sc)
    if log:
        log.info('PetWindow 已创建（scale=%s）', getattr(pet, 'scale', None))
    
    # 系统托盘图标
    tray_icon_path = Path(__file__).parent / 'pet-avatar.png'
    if tray_icon_path.exists():
        tray_icon = QSystemTrayIcon(QIcon(str(tray_icon_path)), app)
    else:
        tray_icon = QSystemTrayIcon(app)
        
    tray_menu = QMenu()
    show_action = QAction("显示/隐藏桌宠", app)
    show_action.triggered.connect(lambda: pet.setVisible(not pet.isVisible()))
    tray_menu.addAction(show_action)

    front_action = QAction("置顶显示（刷到最上层）", app)
    front_action.triggered.connect(lambda: (log and log.info('托盘菜单：置顶显示'),
                                            pet.bring_to_front()))
    tray_menu.addAction(front_action)

    quit_action = QAction("退出程序", app)
    quit_action.triggered.connect(lambda: (log and log.info('托盘菜单：退出程序被点击'), app.quit()))
    tray_menu.addAction(quit_action)
    
    tray_icon.setContextMenu(tray_menu)
    tray_icon.setToolTip("大肥鱼（双击置顶）")

    # ★ 双击托盘图标 → 把桌宠刷到最上层
    #   场景：桌宠被其他窗口遮住，用户点托盘图标想把它叫回来
    def _on_tray_activated(reason):
        try:
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                log and log.info('托盘图标：双击置顶')
                pet.bring_to_front()
        except Exception as e:
            log and log.info('托盘双击处理异常: %s', e)

    tray_icon.activated.connect(_on_tray_activated)
    tray_icon.show()
    
    # 将托盘交给 pet 持有，防止被垃圾回收
    pet.tray_icon = tray_icon

    sys.exit(app.exec())

if __name__ == '__main__':
    main()