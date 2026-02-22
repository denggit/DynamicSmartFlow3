#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
监控脚本：持续运行 main.py，若进程退出则自动重启。
用法: python run_with_watchdog.py
停掉整个服务：直接结束本进程即可（Ctrl+C 或 kill run_with_watchdog），会同时结束 main.py。
"""
import os
import signal
import subprocess
import sys
import time

from config.settings import WATCHDOG_RESTART_DELAY, WATCHDOG_CHILD_WAIT_TIMEOUT, BASE_DIR

ROOT = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = str(BASE_DIR / "main.py")

_child_proc = None


def _kill_child():
    """结束当前子进程（main.py），保证停掉 watchdog 时一起停掉。"""
    global _child_proc
    if _child_proc is None:
        return
    try:
        _child_proc.terminate()
        _child_proc.wait(timeout=WATCHDOG_CHILD_WAIT_TIMEOUT)
    except Exception:
        try:
            _child_proc.kill()
        except Exception:
            pass
    _child_proc = None


def main():
    global _child_proc
    os.chdir(ROOT)

    def on_exit(_, __=None):
        print("\n[Watchdog] 收到退出信号，正在停止 main.py 并退出...")
        _kill_child()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, on_exit)
        signal.signal(signal.SIGTERM, on_exit)
    except AttributeError:
        pass  # Windows 无 SIGTERM

    run = 0
    while True:
        run += 1
        print(f"[Watchdog] 第 {run} 次启动 main.py ...")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            _child_proc = subprocess.Popen(
                [sys.executable, MAIN_SCRIPT],
                cwd=ROOT,
            )
            ret = _child_proc.wait()
            _child_proc = None
            if ret == 0:
                print("[Watchdog] main.py 正常退出，不再重启")
                return 0
        except KeyboardInterrupt:
            on_exit(None, None)
        except Exception as e:
            print(f"[Watchdog] 运行异常: {e}", file=sys.stderr)
        print(f"[Watchdog] main.py 已退出，{WATCHDOG_RESTART_DELAY} 秒后重启...")
        time.sleep(WATCHDOG_RESTART_DELAY)


if __name__ == "__main__":
    sys.exit(main() or 0)
