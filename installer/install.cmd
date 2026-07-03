@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "APP_NAME=HuaweiTrafficMonitor"
set "INSTALL_DIR=%LOCALAPPDATA%\%APP_NAME%"
set "TASK_NAME=HuaweiTrafficMonitor"
set "ZIP_FILE=%~dp0huawei_traffic_monitor.zip"

echo 正在安装华为交换机接口流量监控...

if not exist "%ZIP_FILE%" (
  echo 未找到安装数据包：%ZIP_FILE%
  pause
  exit /b 1
)

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '%INSTALL_DIR%' -Force"
if errorlevel 1 (
  echo 解压安装文件失败。
  pause
  exit /b 1
)

set "PYTHON_EXE="
if not exist "%INSTALL_DIR%\HuaweiTrafficMonitor.exe" (
  for %%P in (python.exe pythonw.exe) do (
    for /f "delims=" %%I in ('where %%P 2^>nul') do (
      if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
    )
  )

  if not defined PYTHON_EXE (
    echo 未找到独立运行程序，也未找到 Python。安装无法继续。
    pause
    exit /b 1
  )
)

(
  echo @echo off
  echo cd /d "%%~dp0"
  echo if exist "%%~dp0HuaweiTrafficMonitor.exe" ^(
  echo   start "HuaweiTrafficMonitor" /min "%%~dp0HuaweiTrafficMonitor.exe" --data-dir "%%~dp0data"
  echo ^) else ^(
  echo   start "HuaweiTrafficMonitor" /min "%PYTHON_EXE%" "%%~dp0monitor.py" --data-dir "%%~dp0data"
  echo ^)
) > "%INSTALL_DIR%\start_monitor.cmd"

(
  echo @echo off
  echo powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -like '*HuaweiTrafficMonitor*monitor.py*' -or $_.CommandLine -like '*%INSTALL_DIR:\=\\%*monitor.py*' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
) > "%INSTALL_DIR%\stop_monitor.cmd"

(
  echo @echo off
  echo start http://127.0.0.1:8088/
) > "%INSTALL_DIR%\open_dashboard.cmd"

(
  echo @echo off
  echo cd /d "%%~dp0"
  echo if exist "%%~dp0HuaweiTrafficMonitor.exe" ^(
  echo   "%%~dp0HuaweiTrafficMonitor.exe" --collect-once --data-dir "%%~dp0data"
  echo ^) else ^(
  echo   "%PYTHON_EXE%" "%%~dp0monitor.py" --collect-once --data-dir "%%~dp0data"
  echo ^)
  echo pause
) > "%INSTALL_DIR%\collect_once.cmd"

(
  echo @echo off
  echo schtasks /Delete /TN "%TASK_NAME%" /F ^>nul 2^>nul
  echo call "%%~dp0stop_monitor.cmd"
  echo echo 已停止并移除计划任务。程序目录保留在：%%~dp0
  echo pause
) > "%INSTALL_DIR%\uninstall.cmd"

schtasks /Create /TN "%TASK_NAME%" /SC ONLOGON /TR "\"%INSTALL_DIR%\start_monitor.cmd\"" /F >nul
if errorlevel 1 (
  echo 创建开机登录启动任务失败，但程序文件已安装到：%INSTALL_DIR%
) else (
  echo 已创建登录自动启动任务：%TASK_NAME%
)

call "%INSTALL_DIR%\start_monitor.cmd"
timeout /t 2 /nobreak >nul
start http://127.0.0.1:8088/

echo.
echo 安装完成。
echo 程序目录：%INSTALL_DIR%
echo 看板地址：http://127.0.0.1:8088/
echo 配置文件：%INSTALL_DIR%\data\config.json
pause
exit /b 0
