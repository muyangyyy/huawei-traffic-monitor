import argparse
import builtins
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from app.collector import CollectorService
from app.config import load_config
from app.database import MonitorDatabase
from app.tray import WindowsTrayIcon, is_tray_supported
from app.web import MonitorHttpServer


BACKGROUND_ENV = "HUAWEI_TRAFFIC_MONITOR_BACKGROUND"


def print(*args: object, **kwargs: object) -> None:
    if sys.stdout is not None:
        builtins.print(*args, **kwargs)


def main() -> int:
    args = build_parser().parse_args()
    if should_launch_background(args):
        if launch_background_process():
            return 0

    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    config = load_config(data_dir)
    db = MonitorDatabase(data_dir / "traffic.db")
    collector = CollectorService(config, db)

    def reload_runtime_config() -> None:
        new_config = load_config(data_dir)
        collector.update_config(new_config)
        db.sync_devices(new_config.devices, reset_interfaces=True)
        threading.Thread(target=collector.collect_once, name="traffic-collect-after-save", daemon=True).start()

    collector.collect_once()
    if args.collect_once:
        print(f"采集完成，数据库：{data_dir / 'traffic.db'}")
        return 0

    server = MonitorHttpServer((config.listen_host, config.listen_port), db, data_dir, on_config_saved=reload_runtime_config)
    stopping = False
    tray: WindowsTrayIcon | None = None

    def stop(_signum: int = 0, _frame: object = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        collector.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    collector.start()

    url = f"http://{config.listen_host}:{config.listen_port}/"
    settings_url = f"{url.rstrip('/')}/settings"
    if is_tray_supported() and not args.no_tray:
        tray = WindowsTrayIcon("华为交换机接口流量监控", url, settings_url, stop)
        tray.start()
    print(f"Huawei Traffic Monitor 已启动：{url}")
    print(f"配置文件：{data_dir / 'config.json'}")
    print(f"数据库：{data_dir / 'traffic.db'}")
    try:
        server.serve_forever()
    finally:
        if tray:
            tray.stop()
        collector.stop()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huawei switch interface traffic monitor")
    parser.add_argument("--data-dir", default="data", help="配置和数据库目录")
    parser.add_argument("--collect-once", action="store_true", help="只执行一次采集后退出")
    parser.add_argument("--no-tray", action="store_true", help="不显示 Windows 托盘图标")
    parser.add_argument("--background-child", action="store_true", help=argparse.SUPPRESS)
    return parser


def should_launch_background(args: argparse.Namespace) -> bool:
    return (
        sys.platform == "win32"
        and not getattr(sys, "frozen", False)
        and is_tray_supported()
        and not args.collect_once
        and not args.no_tray
        and not args.background_child
        and os.environ.get(BACKGROUND_ENV) != "1"
        and sys.stdout is not None
    )


def launch_background_process() -> bool:
    script = Path(__file__).resolve()
    executable = background_python_executable()
    command = [executable, str(script), *sys.argv[1:], "--background-child"]
    env = os.environ.copy()
    env[BACKGROUND_ENV] = "1"
    creationflags = 0
    for flag in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
        creationflags |= getattr(subprocess, flag, 0)
    try:
        subprocess.Popen(
            command,
            cwd=str(script.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        return True
    except OSError as exc:
        print(f"后台启动失败，继续在当前窗口运行：{exc}")
        return False


def background_python_executable() -> str:
    candidates: list[Path] = []
    current = Path(sys.executable)
    if current.name.lower() == "python.exe":
        candidates.append(current.with_name("pythonw.exe"))
    base_executable = getattr(sys, "_base_executable", "")
    if base_executable:
        candidates.append(Path(base_executable).with_name("pythonw.exe"))
    candidates.append(Path(sys.base_prefix) / "pythonw.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


if __name__ == "__main__":
    sys.exit(main())
