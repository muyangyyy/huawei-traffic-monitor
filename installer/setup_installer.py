import builtins
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


def print(*args: object, **kwargs: object) -> None:
    if sys.stdout is not None:
        builtins.print(*args, **kwargs)


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
    create_shortcuts(install_dir)
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
    start_hidden_script = install_dir / "start_monitor_hidden.vbs"
    stop_script = install_dir / "stop_monitor.cmd"
    open_script = install_dir / "open_dashboard.cmd"
    settings_script = install_dir / "open_settings.cmd"
    restart_script = install_dir / "restart_monitor.cmd"
    collect_script = install_dir / "collect_once.cmd"
    uninstall_script = install_dir / "uninstall.cmd"

    start_script.write_text(
        textwrap.dedent(
            f"""\
            @echo off
            cd /d "%~dp0"
            powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$exe=(Resolve-Path '%~dp0HuaweiTrafficMonitor.exe').Path; $running=Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq $exe }}; if (-not $running) {{ Start-Process -FilePath $exe -ArgumentList @('--data-dir', '%~dp0data') -WindowStyle Hidden }}"
            """
        ),
        encoding="utf-8",
    )
    start_hidden_script.write_text(
        textwrap.dedent(
            """\
            Set shell = CreateObject("WScript.Shell")
            Set fso = CreateObject("Scripting.FileSystemObject")
            base = fso.GetParentFolderName(WScript.ScriptFullName)
            shell.Run Chr(34) & base & "\\start_monitor.cmd" & Chr(34), 0, False
            """
        ),
        encoding="utf-8",
    )
    stop_script.write_text(
        textwrap.dedent(
            f"""\
            @echo off
            powershell -NoProfile -ExecutionPolicy Bypass -Command "$exe=(Resolve-Path '%~dp0HuaweiTrafficMonitor.exe').Path; Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq $exe }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
            """
        ),
        encoding="utf-8",
    )
    open_script.write_text(f"@echo off\r\nstart {PORTAL_URL}\r\n", encoding="utf-8")
    settings_script.write_text(f"@echo off\r\nstart {SETTINGS_URL}\r\n", encoding="utf-8")
    restart_script.write_text(
        textwrap.dedent(
            """\
            @echo off
            call "%~dp0stop_monitor.cmd"
            timeout /t 2 /nobreak >nul
            call "%~dp0start_monitor.cmd"
            """
        ),
        encoding="utf-8",
    )
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
            powershell -NoProfile -ExecutionPolicy Bypass -Command "$desktop=[Environment]::GetFolderPath('DesktopDirectory'); $start=[Environment]::GetFolderPath('Programs'); Remove-Item -LiteralPath (Join-Path $desktop '华为交换机流量看板.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath (Join-Path $desktop '华为交换机对接设置.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath (Join-Path $start 'Huawei Traffic Monitor') -Recurse -Force -ErrorAction SilentlyContinue"
            set /p DELETE_DATA=是否删除配置和历史数据 data 文件夹？输入 YES 删除，直接回车保留：
            if /I "%DELETE_DATA%"=="YES" (
              rmdir /s /q "%~dp0data"
              echo 已删除 data 数据目录。
            ) else (
              echo 已保留 data 数据目录。
            )
            echo 已停止并移除计划任务。程序目录保留在：%~dp0
            pause
            """
        ),
        encoding="utf-8",
    )
    uninstall_script.write_text(build_uninstall_script(), encoding="utf-8")


def build_uninstall_script() -> str:
    return textwrap.dedent(
        f"""\
        @echo off
        chcp 65001 >nul
        setlocal EnableExtensions
        set "INSTALL_DIR=%~dp0"
        set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"
        echo 即将卸载华为交换机接口流量监控，并删除安装目录：
        echo %INSTALL_DIR%
        set /p CONFIRM=输入 YES 确认卸载并删除安装文件、配置和历史数据：
        if /I not "%CONFIRM%"=="YES" (
          echo 已取消卸载。
          pause
          exit /b 0
        )
        schtasks /Delete /TN "{TASK_NAME}" /F >nul 2>nul
        call "%~dp0stop_monitor.cmd"
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$desktop=[Environment]::GetFolderPath('DesktopDirectory'); $start=[Environment]::GetFolderPath('Programs'); Remove-Item -LiteralPath (Join-Path $desktop '华为交换机流量看板.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath (Join-Path $desktop '华为交换机对接设置.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath (Join-Path $start 'Huawei Traffic Monitor') -Recurse -Force -ErrorAction SilentlyContinue"
        set "CLEANUP=%TEMP%\\HuaweiTrafficMonitor_uninstall_%RANDOM%.cmd"
        > "%CLEANUP%" echo @echo off
        >> "%CLEANUP%" echo timeout /t 2 /nobreak ^>nul
        >> "%CLEANUP%" echo rmdir /s /q "%INSTALL_DIR%"
        >> "%CLEANUP%" echo del "%%~f0"
        echo 已停止服务并移除快捷方式。安装目录将在窗口关闭后删除。
        start "" /min "%CLEANUP%"
        exit /b 0
        """
    )


def create_shortcuts(install_dir: Path) -> None:
    dashboard_script = install_dir / "open_dashboard.cmd"
    settings_script = install_dir / "open_settings.cmd"
    ps_script = textwrap.dedent(
        f"""
        $desktop=[Environment]::GetFolderPath('DesktopDirectory')
        $programs=[Environment]::GetFolderPath('Programs')
        $folder=Join-Path $programs 'Huawei Traffic Monitor'
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        $shell=New-Object -ComObject WScript.Shell
        foreach ($item in @(
            @{{Path=(Join-Path $desktop '华为交换机流量看板.lnk'); Target='{dashboard_script}'; Description='打开华为交换机接口流量监控看板'}},
            @{{Path=(Join-Path $desktop '华为交换机对接设置.lnk'); Target='{settings_script}'; Description='打开华为交换机 SNMP 对接设置'}},
            @{{Path=(Join-Path $folder '流量看板.lnk'); Target='{dashboard_script}'; Description='打开华为交换机接口流量监控看板'}},
            @{{Path=(Join-Path $folder '对接设置.lnk'); Target='{settings_script}'; Description='打开华为交换机 SNMP 对接设置'}},
            @{{Path=(Join-Path $folder '卸载.lnk'); Target='{install_dir / "uninstall.cmd"}'; Description='卸载华为交换机接口流量监控'}}
        )) {{
            $shortcut=$shell.CreateShortcut($item.Path)
            $shortcut.TargetPath=$item.Target
            $shortcut.WorkingDirectory='{install_dir}'
            $shortcut.Description=$item.Description
            $shortcut.Save()
        }}
        """
    ).strip()
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("已创建桌面和开始菜单快捷方式。")
    else:
        print("创建快捷方式失败，程序仍可通过安装目录脚本启动。")
        print(result.stderr.strip() or result.stdout.strip())


def create_startup_task(install_dir: Path) -> None:
    start_script = install_dir / "start_monitor_hidden.vbs"
    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            f'wscript.exe "{start_script}"',
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
        ["wscript.exe", str(install_dir / "start_monitor_hidden.vbs")],
        cwd=str(install_dir),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def fail(message: str) -> int:
    print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
