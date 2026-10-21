# -*- coding: utf-8 -*-
"""Smooth edge for a window clipped to a capsule.

The panel is a WebView2 window: it can't be per-pixel transparent, so its
dock/peek capsule is cut with SetWindowRgn — a 1-bit region, hence the
stair-stepped curve. This module draws the missing anti-aliasing with a
second, click-through, per-pixel-alpha window (UpdateLayeredWindow) that
sits right behind the main one and covers exactly the band between the
hard region and the ideal capsule:

    main window   : hard region, a few px inside the capsule  (untouched)
    halo window   : ideal capsule's anti-aliased coverage, minus the hard
                    region's pixels — so the two never overlap

The main window's edge is plain white at the layered alpha the panel uses,
so the halo paints the same white at the same alpha and there's no seam.
Nothing inside the capsule is redrawn: only the outer 1-3 px change.
"""
import ctypes
import ctypes.wintypes as wt
import threading

from PIL import Image, ImageDraw

SS = 4                  # supersampling for the ideal capsule's coverage
INSET = 2               # hard region sits this many px inside the capsule

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)

_HRGN = wt.HANDLE
_g.CreateRoundRectRgn.restype = _HRGN
_g.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_g.PtInRegion.argtypes = [_HRGN, ctypes.c_int, ctypes.c_int]
_g.DeleteObject.argtypes = [wt.HGDIOBJ]


def hard_region(w, h, inset=INSET):
    """The 1-bit capsule the main window is cut to (caller owns it)."""
    d = max(2, min(w, h) - 2 * inset)
    return _g.CreateRoundRectRgn(inset, inset, w - inset + 1,
                                 h - inset + 1, d, d)


def halo_bgra(w, h, rgb=(255, 255, 255), alpha=240, inset=INSET):
    """Premultiplied BGRA bytes for a w x h halo: the ideal capsule's AA
    coverage x `alpha`, zero wherever the hard region already paints."""
    big = Image.new("L", (w * SS, h * SS), 0)
    r = min(w, h) * SS / 2.0
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, w * SS - 1, h * SS - 1), radius=r, fill=255)
    cov = big.resize((w, h), Image.LANCZOS)     # ideal capsule, AA'd
    rgn = hard_region(w, h, inset)
    try:
        cut = Image.new("L", (w, h), 0)
        px = cut.load()
        for y in range(h):
            for x in range(w):
                if _g.PtInRegion(rgn, x, y):
                    px[x, y] = 255
    finally:
        _g.DeleteObject(rgn)
    a = Image.composite(Image.new("L", (w, h), 0), cov, cut)  # cov - cut
    a = a.point(lambda v: v * alpha // 255)
    # premultiplied colour = rgb * a / 255
    chan = [a.point(lambda v, c=c: c * v // 255) for c in rgb]
    # memory order B, G, R, A
    return Image.merge("RGBA", (chan[2], chan[1], chan[0], a)).tobytes()


# ------------------------------------------------------------- win32 glue

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


_u.DefWindowProcW.restype = LRESULT
_u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
_u.CreateWindowExW.restype = wt.HWND
_u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wt.HWND, wt.HMENU,
                               wt.HINSTANCE, wt.LPVOID]
_u.GetDC.restype = wt.HDC
_u.GetDC.argtypes = [wt.HWND]
_u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_u.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
    ctypes.POINTER(_BLENDFUNCTION), wt.DWORD]
_u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, wt.UINT]
_u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_u.SetWindowRgn.argtypes = [wt.HWND, _HRGN, wt.BOOL]
_g.CreateCompatibleDC.restype = wt.HDC
_g.CreateCompatibleDC.argtypes = [wt.HDC]
_g.CreateDIBSection.restype = wt.HBITMAP
_g.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                ctypes.POINTER(ctypes.c_void_p),
                                wt.HANDLE, wt.DWORD]
_g.SelectObject.restype = wt.HGDIOBJ
_g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_g.DeleteDC.argtypes = [wt.HDC]
_k.GetModuleHandleW.restype = wt.HMODULE
_k.GetModuleHandleW.argtypes = [wt.LPCWSTR]

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
WS_EX_TOPMOST = 0x8
HWND_TOPMOST = wt.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_NOSIZE = 0x10, 0x40, 0x1
WM_APP, WM_CLOSE = 0x8000, 0x10
CLASS = "CacheBuddyEdgeHalo"


class EdgeHalo:
    """The click-through window that paints a capsule's anti-aliased edge
    behind `owner_hwnd`. Lives on its own thread with its own message pump
    (a layered window nobody pumps gets "not responding" ghosted)."""

    def __init__(self, alpha=240, rgb=(255, 255, 255)):
        self.alpha, self.rgb = alpha, rgb
        self.hwnd = None
        self._size = None                 # (w, h) of the painted bitmap
        self._cache = {}                  # (w, h) -> BGRA bytes
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)   # keep a reference alive
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(3)

    def _wndproc(self, hwnd, msg, wp, lp):
        return _u.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self):
        hinst = _k.GetModuleHandleW(None)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = CLASS
        _u.RegisterClassExW(ctypes.byref(wc))    # 0 if already registered
        self.hwnd = _u.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            CLASS, "", WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        self._ready.set()
        msg = wt.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))

    def _paint(self, x, y, w, h):
        # a handful of sizes recur (dock, peek with 1..n rings): build each
        # rim once (25-40 ms), then dock <-> peek is just a blit
        data = self._cache.get((w, h))
        if data is None:
            data = self._cache[(w, h)] = halo_bgra(w, h, self.rgb, self.alpha)
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth, bi.biHeight = w, -h          # negative = top-down rows
        bi.biPlanes, bi.biBitCount = 1, 32
        screen = _u.GetDC(None)
        mem = _g.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bmp = _g.CreateDIBSection(mem, ctypes.byref(bi), 0,
                                  ctypes.byref(bits), None, 0)
        old = _g.SelectObject(mem, bmp)
        try:
            ctypes.memmove(bits, data, len(data))
            blend = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel
            _u.UpdateLayeredWindow(
                self.hwnd, screen, ctypes.byref(wt.POINT(x, y)),
                ctypes.byref(wt.SIZE(w, h)), mem,
                ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend),
                2)                                # ULW_ALPHA
        finally:
            _g.SelectObject(mem, old)
            _g.DeleteObject(bmp)
            _g.DeleteDC(mem)
            _u.ReleaseDC(None, screen)

    def show(self, owner, x, y, w, h):
        """Cover the owner's capsule edge at (x, y, w, h), just behind it."""
        if not self.hwnd:
            return
        if (w, h) != self._size:
            self._paint(x, y, w, h)
            self._size = (w, h)
        # right behind the owner in z-order; both stay topmost
        _u.SetWindowPos(self.hwnd, owner, x, y, 0, 0,
                        SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def move(self, owner, x, y):
        if self.hwnd and self._size:
            _u.SetWindowPos(self.hwnd, owner, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE)

    def hide(self):
        if self.hwnd:
            _u.ShowWindow(self.hwnd, 0)          # SW_HIDE

    def close(self):
        if self.hwnd:
            _u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self.hwnd = None


def set_capsule(hwnd, w, h, inset=INSET):
    """Cut `hwnd` to the hard capsule the halo was drawn around."""
    rgn = hard_region(w, h, inset)
    if not _u.SetWindowRgn(hwnd, rgn, True):
        _g.DeleteObject(rgn)                     # the system owns it on success
        return False
    return True
