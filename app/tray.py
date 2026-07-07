import math
import sys
import threading
import webbrowser
from ctypes import POINTER, WINFUNCTYPE, Structure, byref, c_int, c_ubyte, c_void_p, c_ssize_t, sizeof, windll
from ctypes import wintypes
from typing import Callable


def configure_win32_api() -> None:
    user32 = windll.user32
    shell32 = windll.shell32
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = c_ssize_t
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        c_int,
        c_int,
        c_int,
        c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateIcon.argtypes = [
        wintypes.HINSTANCE,
        c_int,
        c_int,
        c_ubyte,
        c_ubyte,
        POINTER(c_ubyte),
        POINTER(c_ubyte),
    ]
    user32.CreateIcon.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, c_int, c_int, c_int, wintypes.HWND, c_void_p]
    user32.TrackPopupMenu.restype = wintypes.UINT
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class WindowsTrayIcon:
    def __init__(self, title: str, dashboard_url: str, settings_url: str, on_exit: Callable[[], None]) -> None:
        self.title = title
        self.dashboard_url = dashboard_url
        self.settings_url = settings_url
        self.on_exit = on_exit
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.hwnd: int | None = None
        self.hicon: int | None = None
        self._wndproc_ref = None

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self.thread and self.thread.is_alive():
            return True
        self.thread = threading.Thread(target=self._run, name="traffic-monitor-tray", daemon=True)
        self.thread.start()
        return self.ready.wait(timeout=5)

    def stop(self) -> None:
        if sys.platform != "win32" or not self.hwnd:
            return
        windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=5)

    def _run(self) -> None:
        configure_win32_api()
        hinstance = windll.kernel32.GetModuleHandleW(None)
        class_name = "HuaweiTrafficMonitorTrayWindow"
        wndproc = WNDPROC(self._window_proc)
        self._wndproc_ref = wndproc

        window_class = WNDCLASSW()
        window_class.lpfnWndProc = wndproc
        window_class.hInstance = hinstance
        window_class.lpszClassName = class_name
        windll.user32.RegisterClassW(byref(window_class))

        hwnd = windll.user32.CreateWindowExW(0, class_name, self.title, 0, 0, 0, 0, 0, None, None, hinstance, None)
        self.hwnd = hwnd
        self.hicon = create_monitor_icon()
        self._add_icon(hwnd)
        self.ready.set()

        msg = MSG()
        while windll.user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            windll.user32.TranslateMessage(byref(msg))
            windll.user32.DispatchMessageW(byref(msg))

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONDBLCLK:
                webbrowser.open(self.dashboard_url)
                return 0
            if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu(hwnd)
                return 0
        if msg == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == MENU_OPEN_DASHBOARD:
                webbrowser.open(self.dashboard_url)
                return 0
            if command == MENU_OPEN_SETTINGS:
                webbrowser.open(self.settings_url)
                return 0
            if command == MENU_EXIT:
                self.on_exit()
                return 0
        if msg == WM_CLOSE:
            windll.user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._delete_icon(hwnd)
            if self.hicon:
                windll.user32.DestroyIcon(self.hicon)
                self.hicon = None
            windll.user32.PostQuitMessage(0)
            return 0
        return windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        menu = windll.user32.CreatePopupMenu()
        windll.user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_DASHBOARD, "打开看板")
        windll.user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_SETTINGS, "对接设置")
        windll.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        windll.user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "退出并关闭监听端口")
        point = POINT()
        windll.user32.GetCursorPos(byref(point))
        windll.user32.SetForegroundWindow(hwnd)
        command = windll.user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY, point.x, point.y, 0, hwnd, None)
        windll.user32.DestroyMenu(menu)
        if command:
            windll.user32.PostMessageW(hwnd, WM_COMMAND, command, 0)

    def _add_icon(self, hwnd: int) -> None:
        data = self._icon_data(hwnd)
        windll.shell32.Shell_NotifyIconW(NIM_ADD, byref(data))

    def _delete_icon(self, hwnd: int) -> None:
        data = self._icon_data(hwnd)
        windll.shell32.Shell_NotifyIconW(NIM_DELETE, byref(data))

    def _icon_data(self, hwnd: int) -> "NOTIFYICONDATAW":
        data = NOTIFYICONDATAW()
        data.cbSize = sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = self.hicon or windll.user32.LoadIconW(None, IDI_APPLICATION)
        data.szTip = self.title[:127]
        return data


def create_monitor_icon(size: int = 32) -> int:
    row_mask_bytes = ((size + 15) // 16) * 2
    and_mask = (c_ubyte * (row_mask_bytes * size))()
    xor_bits = (c_ubyte * (size * size * 4))()
    pixels = [[pixel_for(size, x, y) for x in range(size)] for y in range(size)]

    for y in range(size):
        source_y = size - 1 - y
        for x in range(size):
            r, g, b, alpha = pixels[source_y][x]
            offset = (y * size + x) * 4
            xor_bits[offset] = b
            xor_bits[offset + 1] = g
            xor_bits[offset + 2] = r
            xor_bits[offset + 3] = alpha
            if alpha == 0:
                and_mask[y * row_mask_bytes + x // 8] |= 0x80 >> (x % 8)

    return windll.user32.CreateIcon(None, size, size, 1, 32, and_mask, xor_bits)


def pixel_for(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    scale = size / 32
    px = (x + 0.5) / scale
    py = (y + 0.5) / scale
    cx = cy = 16.0
    if math.hypot(px - cx, py - cy) > 14.6:
        return (0, 0, 0, 0)

    petals = (
        (16.0, 8.0, 2.35, 7.0, 0),
        (12.1, 9.2, 2.2, 6.6, -26),
        (19.9, 9.2, 2.2, 6.6, 26),
        (8.9, 12.5, 2.0, 6.0, -54),
        (23.1, 12.5, 2.0, 6.0, 54),
        (10.7, 17.0, 1.8, 5.2, -78),
        (21.3, 17.0, 1.8, 5.2, 78),
    )
    for petal in petals:
        if point_in_rotated_ellipse(px, py, *petal):
            if math.hypot(px - 16.0, py - 17.4) < 3.25 or (13.2 <= px <= 18.8 and 15.0 <= py <= 19.4):
                return (255, 255, 255, 255)
            return (207, 10, 44, 255)

    if math.hypot(px - cx, py - cy) > 13.5:
        return (232, 233, 236, 255)
    return (255, 255, 255, 255)


def point_in_rotated_ellipse(
    x: float,
    y: float,
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    angle_degrees: float,
) -> bool:
    angle = math.radians(angle_degrees)
    dx = x - center_x
    dy = y - center_y
    rotated_x = dx * math.cos(angle) + dy * math.sin(angle)
    rotated_y = -dx * math.sin(angle) + dy * math.cos(angle)
    return (rotated_x / radius_x) ** 2 + (rotated_y / radius_y) ** 2 <= 1


def is_tray_supported() -> bool:
    return sys.platform == "win32"


WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_TRAYICON = WM_USER + 20

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IDI_APPLICATION = 32512
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MENU_OPEN_DASHBOARD = 1001
MENU_OPEN_SETTINGS = 1002
MENU_EXIT = 1003

WNDPROC = WINFUNCTYPE(c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class POINT(Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


class WNDCLASSW(Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class NOTIFYICONDATAW(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", c_void_p),
    ]
