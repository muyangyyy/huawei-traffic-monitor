import argparse
import builtins
import signal
import sys
import threading
from pathlib import Path

from app.collector import CollectorService
from app.config import load_config
from app.database import MonitorDatabase
from app.tray import WindowsTrayIcon, is_tray_supported
from app.web import MonitorHttpServer


def print(*args: object, **kwargs: object) -> None:
    if sys.stdout is not None:
        builtins.print(*args, **kwargs)


def main() -> int:
    args = build_parser().parse_args()

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
    return parser


if __name__ == "__main__":
    sys.exit(main())
