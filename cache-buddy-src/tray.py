"""Notification-area (system tray) icon for the panel, plain ctypes.

Left click opens the panel; right click shows a small menu (open / quit).
The icon is re-added when Explorer restarts (TaskbarCreated). Runs its own
hidden window and message loop on a daemon thread."""
import ctypes
import os
import sys
import threading
from ctypes import wintypes as wt

WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_CLOSE, WM_DESTROY, WM_COMMAND, WM_NULL = 0x0010, 0x0002, 0x0111, 0x0000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
ID_OPEN, ID_QUIT = 1, 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


def _icon_path():
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__))):
        if base:
            p = os.path.join(base, "cache_buddy.ico")
            if os.path.exists(p):
                return p
    return None


class Tray:
    """on_open(), on_quit(): called on the tray thread. lang(): "zh"/"en"."""

    def __init__(self, on_open, on_quit, lang=lambda: "zh", log=print,
                 tip="Cache Buddy"):
        self.on_open, self.on_quit, self.lang = on_open, on_quit, lang
        self.log, self.tip = log, tip
        self.hwnd = None
        self._wndproc = WNDPROC(self._proc)       # kept alive with self
        self._nid = None
        self._icon = None
        self._recreated = -1                      # set before the window

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="tray").start()

    def close(self):
        """Remove the icon now (the process may exit before the tray
        thread runs again), then let the thread wind down."""
        nid, self._nid = self._nid, None
        if nid:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE,
                                                    ctypes.byref(nid))
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ------------------------------------------------------------ win32
    def _load_icon(self):
        u, sh = ctypes.windll.user32, ctypes.windll.shell32
        u.LoadImageW.restype = wt.HANDLE
        u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
        cx, cy = u.GetSystemMetrics(49), u.GetSystemMetrics(50)  # SM_CXSMICON
        p = _icon_path()
        if p:
            h = u.LoadImageW(None, p, 1, cx, cy, 0x10)           # LR_LOADFROMFILE
            if h:
                return h
        if getattr(sys, "frozen", False):                        # the exe's own
            small = wt.HICON()
            if sh.ExtractIconExW(sys.executable, 0, None,
                                 ctypes.byref(small), 1) and small:
                return small
        return u.LoadIconW(None, 32512)                          # IDI_APPLICATION

    def _add(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = self.tip
        self._nid = nid
        if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD,
                                                       ctypes.byref(nid)):
            self.log("tray: add failed")

    def _menu(self):
        u = ctypes.windll.user32
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t,
                                  wt.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wt.HWND,
                                     ctypes.c_void_p]
        u.DestroyMenu.argtypes = [wt.HMENU]
        zh = self.lang() == "zh"
        m = u.CreatePopupMenu()
        u.AppendMenuW(m, 0, ID_OPEN, "打开面板" if zh else "Open panel")
        u.AppendMenuW(m, 0x800, 0, None)                         # separator
        u.AppendMenuW(m, 0, ID_QUIT, "退出 Cache Buddy" if zh
                      else "Quit Cache Buddy")
        u.SetMenuDefaultItem(m, ID_OPEN, 0)
        p = wt.POINT()
        u.GetCursorPos(ctypes.byref(p))
        u.SetForegroundWindow(self.hwnd)      # or the menu never dismisses
        cmd = u.TrackPopupMenu(m, 0x0100 | 0x0020,              # RETURNCMD |
                               p.x, p.y, 0, self.hwnd, None)    # RIGHTBUTTON
        u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u.DestroyMenu(m)
        return cmd

    def _call(self, fn):
        try:
            fn()
        except Exception as e:
            self.log("tray: callback failed: %r" % e)

    def _proc(self, hwnd, msg, wp, lp):
        u = ctypes.windll.user32
        if msg == WM_TRAY:
            ev = lp & 0xFFFF
            if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._call(self.on_open)
            elif ev == WM_RBUTTONUP:
                cmd = self._menu()
                if cmd == ID_OPEN:
                    self._call(self.on_open)
                elif cmd == ID_QUIT:
                    self._call(self.on_quit)
            return 0
        if msg == self._recreated:            # Explorer restarted
            self._add()
            return 0
        if msg == WM_CLOSE:
            nid, self._nid = self._nid, None
            if nid:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE,
                                                        ctypes.byref(nid))
            u.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self):
        u = ctypes.windll.user32
        try:
            u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            u.SetForegroundWindow.argtypes = [wt.HWND]
            u.DestroyWindow.argtypes = [wt.HWND]
            u.DefWindowProcW.restype = LRESULT
            u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM,
                                         wt.LPARAM]
            u.CreateWindowExW.restype = wt.HWND
            self._recreated = u.RegisterWindowMessageW("TaskbarCreated")
            wc = WNDCLASSW(lpfnWndProc=self._wndproc,
                           lpszClassName="CacheBuddyTray")
            u.RegisterClassW(ctypes.byref(wc))
            # a plain hidden top-level window: message-only windows don't
            # hear the TaskbarCreated broadcast
            self.hwnd = u.CreateWindowExW(0, "CacheBuddyTray", "Cache Buddy tray",
                                          0, 0, 0, 0, 0, None, None, None, None)
            if not self.hwnd:
                self.log("tray: window failed")
                return
            self._icon = self._load_icon()
            self._add()
            msg = wt.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self.log("tray: off: %r" % e)
