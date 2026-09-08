"""Render the real Settings tab and photograph it.

Driven from inside Tk rather than by synthetic clicks: SendMessage into a Tk
notebook does not reliably change tabs, and the attempts that half-worked kept
leaving the window collapsed. Selecting the tab through the widget itself is
both reliable and closer to what the code actually does.

Capture is PrintWindow against an exact window title, never a screen region --
a region grab takes whatever is on that part of the screen, which is how private
windows ended up in two earlier screenshots.
"""
import ctypes, os, sys, time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)

import tkinter as tk
from tkinter import ttk
from PIL import Image

TITLE = "Vlocalhost.AI — Meeting Notes"
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32


def find_exact(title):
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    return found


class BMI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def grab(hwnd, out):
    r = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    dc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(dc)
    bmp = gdi32.CreateCompatibleBitmap(dc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)
    bi = BMI(); bi.biSize = ctypes.sizeof(BMI); bi.biWidth = w
    bi.biHeight = -h; bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(out)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(hwnd, dc)
    return w, h


def notebook_of(widget):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
        got = notebook_of(child)
        if got:
            return got
    return None


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else HERE
    import gui

    root = tk.Tk()
    app = gui.App(root)
    root.geometry("980x900")

    shots = []

    def shoot(name):
        """Photograph *this* window, found through Tk rather than by title.

        Title matching picked the wrong window every time: a previous instance
        of the app left a stale 237x39 window behind that still answered to the
        same name, and it sorted first. ``GetAncestor(winfo_id(), GA_ROOT)`` is
        the handle Tk itself is drawing into, so it cannot be another process's
        window and cannot be a leftover.
        """
        root.update_idletasks()
        hwnd = user32.GetAncestor(root.winfo_id(), 2)      # GA_ROOT
        path = os.path.join(out_dir, name)
        print("  %s %s" % (name, grab(hwnd, path)), flush=True)
        shots.append(path)

    def canvases(w, acc):
        for c in w.winfo_children():
            if isinstance(c, tk.Canvas):
                acc.append(c)
            canvases(c, acc)
        return acc

    # Everything runs on the Tk thread, spaced by after() so the window is
    # actually mapped and laid out before anything is photographed. Driving
    # this from outside the loop is what produced a 237x39 title bar four
    # times: without mainloop the window exists but was never sized.
    def step_tabs():
        nb = notebook_of(root)
        print("tabs:", [nb.tab(i, "text") for i in range(nb.index("end"))], flush=True)
        nb.select(app.tab_set)
        root.after(1200, step_top)

    def step_top():
        shoot("settings-top.png")
        root.after(300, step_scroll)

    def step_scroll():
        cvs = canvases(app.tab_set, [])
        if not cvs:
            print("  (no scrollable canvas on the settings tab)", flush=True)
            return root.after(200, done)
        cv = cvs[0]
        fracs = [(0.28, "settings-mid.png"), (0.55, "settings-notes.png"),
                 (0.80, "settings-lower.png"), (1.0, "settings-bottom.png")]

        def nxt(i=0):
            if i >= len(fracs):
                return root.after(200, done)
            frac, name = fracs[i]
            cv.yview_moveto(frac)
            root.update_idletasks()
            shoot(name)
            root.after(400, lambda: nxt(i + 1))

        nxt()

    def done():
        root.destroy()

    root.after(1500, step_tabs)
    root.mainloop()
    print(os.linesep.join(shots))


if __name__ == "__main__":
    main()
