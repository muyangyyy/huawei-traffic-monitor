import os
import subprocess
import sys
import textwrap
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
import zipfile
from pathlib import Path


APP_NAME = "HuaweiTrafficMonitor"
TASK_NAME = "HuaweiTrafficMonitor"
PORTAL_URL = "http://127.0.0.1:8088/"
SETTINGS_URL = "http://127.0.0.1:8088/settings"


def main() -> int:
    install_dir = choose_install_dir()
    if install_dir is None:
        return 0
    payload = resource_path("huawei_traffic_monitor.zip")
    if not payload.exists():
        return fail(f"未找到安装载荷：{payload}")

    print("正在安装华为交换机接口流量监控...")
    install_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(payload, "r") as archive:
        archive.extractall(install_dir)

    write_scripts(install_dir)
    create_startup_task(install_dir)
    start_monitor(install_dir)
    webbrowser.open(PORTAL_URL)

    print()
    print("安装完成。")
    print(f"程序目录：{install_dir}")
    print(f"看板地址：{PORTAL_URL}")
    print(f"对接设置：{SETTINGS_URL}")
    print("如需接入真实交换机，请打开对接设置，添加设备、输入团体字、选择 OID 模板并配置监控接口。")
    return 0


def choose_install_dir() -> Path | None:
    default_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
    selected: Path | None = None

    root = tk.Tk()
    root.title("华为交换机接口流量监控安装")
    root.geometry("560x230")
    root.resizable(False, False)

    path_var = tk.StringVar(value=str(default_dir))

    def browse() -> None:
        initial = Path(path_var.get()).parent if path_var.get().strip() else default_dir.parent
        path = filedialog.askdirectory(title="选择安装目录", initialdir=str(initial), mustexist=False)
        if path:
            path_var.set(path)

    def install() -> None:
        nonlocal selected
        value = path_var.get().strip().strip('"')
        if not value:
            messagebox.showerror("安装路径", "请选择安装目录。")
            return
        selected = Path(value).expanduser()
        root.destroy()

    def cancel() -> None:
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", cancel)

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text="选择安装路径", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    tk.Label(frame, text="程序文件会安装到下方目录，数据和配置默认保存在该目录的 data 文件夹。", fg="#52616f").pack(anchor="w", pady=(8, 14))

    row = tk.Frame(frame)
    row.pack(fill="x")
    tk.Entry(row, textvariable=path_var).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="浏览...", width=10, command=browse).pack(side="left", padx=(8, 0))

    buttons = tk.Frame(frame)
    buttons.pack(fill="x", pady=(24, 0))
    tk.Button(buttons, text="取消", width=10, command=cancel).pack(side="right")
    tk.Button(buttons, text="安装", width=10, command=install).pack(side="right", padx=(0, 8))

    root.mainloop()
    return selected


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def write_scripts(install_dir: Path) -> None:
    start_script = install_dir / "start_monitor.cmd"
    stop_script = install_dir / "stop_monitor.cmd"
    open_script = install_dir / "open_dashboard.cmd"
    collect_script = install_dir / "collect_once.cmd"
    uninstall_script = install_dir / "uninstall.cmd"

    start_script.write_text(
        textwrap.dedent(
            f"""\
            @echo off
            cd /d "%~dp0"
            start "{APP_NAME}" /min "%~dp0HuaweiTrafficMonitor.exe" --data-dir "%~dp0data"
            """
        ),
        encoding="utf-8",
    )
    stop_script.write_text(
        textwrap.dedent(
            f"""\
            @echo off
            powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{APP_NAME}*HuaweiTrafficMonitor.exe*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
            """
        ),
        encoding="utf-8",
    )
    open_script.write_text(f"@echo off\r\nstart {PORTAL_URL}\r\n", encoding="utf-8")
    collect_script.write_text(
        textwrap.dedent(
            """\
            @echo off
            cd /d "%~dp0"
            "%~dp0HuaweiTrafficMonitor.exe" --collect-once --data-dir "%~dp0data"
            pause
            """
        ),
        encoding="utf-8",
    )
    uninstall_script.write_text(
        textwrap.dedent(
            f"""\
            @echo off
            schtasks /Delete /TN "{TASK_NAME}" /F >nul 2>nul
            call "%~dp0stop_monitor.cmd"
            echo 已停止并移除计划任务。程序目录保留在：%~dp0
            pause
            """
        ),
        encoding="utf-8",
    )


def create_startup_task(install_dir: Path) -> None:
    start_script = install_dir / "start_monitor.cmd"
    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            str(start_script),
            "/F",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"已创建登录自动启动任务：{TASK_NAME}")
    else:
        print("创建登录自动启动任务失败，程序仍可手动启动。")
        print(result.stderr.strip() or result.stdout.strip())


def start_monitor(install_dir: Path) -> None:
    subprocess.Popen(
        [str(install_dir / "start_monitor.cmd")],
        cwd=str(install_dir),
        shell=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def fail(message: str) -> int:
    print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
