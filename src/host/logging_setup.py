# -*- coding: utf-8 -*-
"""日志系统：崩溃与异常全部落盘，用户复现后可直接看 logs/。

三类记录：
  1. logs/pet.log      常规日志（滚动，默认 5MB × 3 份）
  2. logs/crash.log     硬崩溃栈（faulthandler：段错误 / 访问违例）
  3. logs/qt.log        Qt 内部消息（含 "QThread: Destroyed while thread is still running"）

用法：
    from src.host.logging_setup import setup_logging, get_logger
    log = setup_logging()          # 在 QApplication 之后调用一次
    log.info("xxx")
    log.exception("yyy")           # 带栈
"""
import logging
import logging.handlers
import os
import sys
import threading
import traceback

_LOGGER_NAME = "dsh-pet"
_setup_done = [False]
_SEH_HANDLER = [None]
LOG_DIR = [None]


def _log_dir(base_dir=None):
    """日志目录：优先安装目录下的 logs/，失败退回临时目录。"""
    if LOG_DIR[0] is not None:
        return LOG_DIR[0]
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, "logs"))
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "logs"))
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser("~"), ".dsh-pet-logs"))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            LOG_DIR[0] = d
            return d
        except Exception:
            continue
    LOG_DIR[0] = "."
    return "."


def setup_logging(base_dir=None, level=logging.INFO):
    """初始化日志（幂等）。返回 logger。"""
    if _setup_done[0]:
        return get_logger()

    d = _log_dir(base_dir)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(threadName)s %(message)s",
        datefmt="%H:%M:%S")

    # 滚动文件：5MB × 3
    try:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(d, "pet.log"), maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    # 控制台（有 stderr 时才加）
    try:
        if sys.stderr is not None:
            sh = logging.StreamHandler(sys.stderr)
            sh.setLevel(level)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
    except Exception:
        pass

    # ---- 未捕获异常 ----
    def _excepthook(etype, value, tb):
        try:
            logger.error("未捕获异常:\n%s" % "".join(
                traceback.format_exception(etype, value, tb)))
        except Exception:
            pass
        try:
            if sys.__excepthook__ is not None:
                sys.__excepthook__(etype, value, tb)
        except Exception:
            pass

    sys.excepthook = _excepthook

    # ---- 子线程未捕获异常 ----
    def _threadhook(args):
        try:
            logger.error("线程未捕获异常 (%s):\n%s" % (
                getattr(args.thread, 'name', '?'),
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))))
        except Exception:
            pass

    try:
        threading.excepthook = _threadhook
    except Exception:
        pass

    # ---- 硬崩溃（段错误/访问违例） ----
    try:
        import faulthandler
        crash_path = os.path.join(d, "crash.log")
        f = open(crash_path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=f, all_threads=True)
        logger.info("faulthandler 已启用 → %s", crash_path)
    except Exception:
        pass

    # ---- Windows 访问违例（SEH）----
    try:
        import ctypes
        from ctypes import wintypes

        log_path = os.path.join(d, "crash.log")

        EXCEPTION_CONTINUE_SEARCH = 0
        handler_type = ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.c_void_p)

        def _seh_filter(exc_info_ptr):
            try:
                import datetime
                code = None
                try:
                    # EXCEPTION_POINTERS -> ExceptionRecord -> ExceptionCode
                    rec_ptr = ctypes.cast(exc_info_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
                    code = ctypes.cast(rec_ptr, ctypes.POINTER(wintypes.DWORD))[0]
                except Exception:
                    pass
                with open(log_path, "a", encoding="utf-8", buffering=1) as f:
                    f.write("\n=== SEH 崩溃 %s  异常码=0x%08X ===\n" % (
                        datetime.datetime.now().isoformat(), code or 0))
                    f.write("（这是 Windows 结构化异常，通常是访问已销毁的窗口/对象）\n")
                    try:
                        import traceback
                        f.write("当前 Python 栈:\n")
                        f.write("".join(traceback.format_stack()))
                    except Exception:
                        pass
            except Exception:
                pass
            return EXCEPTION_CONTINUE_SEARCH

        _SEH_HANDLER[0] = handler_type(_seh_filter)   # 必须保住引用，否则被 GC
        ctypes.windll.kernel32.SetUnhandledExceptionFilter(_SEH_HANDLER[0])
        logger.info("SEH 异常过滤器已安装")
    except Exception as e:
        logger.warning("SEH 过滤器安装失败: %s", e)

    # ---- Qt 内部消息 ----
    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _qt_handler(mode, ctx, msg):
            try:
                logger.warning("Qt: %s", msg)
            except Exception:
                pass

        qInstallMessageHandler(_qt_handler)
        logger.info("Qt 消息处理器已安装")
    except Exception:
        pass

    _setup_done[0] = True
    logger.info("=" * 60)
    logger.info("日志启动  目录=%s  进程=%s  python=%s",
                d, os.getpid(), sys.version.split()[0])
    return logger


def get_logger():
    return logging.getLogger(_LOGGER_NAME)
