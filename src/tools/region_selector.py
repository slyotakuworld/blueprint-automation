import threading

import cv2
import numpy as np
from PIL import ImageGrab


def pil_to_bgr(pil_img):
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


class RegionSelector:
    def __init__(self, on_selected_callback):
        self.on_selected = on_selected_callback
        self.start_x = None
        self.start_y = None
        self.cur_x = None
        self.cur_y = None
        self.drawing = False
        self._tk_root = None
        self._canvas = None
        self._fullscreen_img = None
        self._screen_width = 0
        self._screen_height = 0
        self._result = None
        self._event = threading.Event()

    def _build_fullscreen_toplevel(self):
        import tkinter as tk
        from PIL import ImageTk

        self._fullscreen_img = ImageGrab.grab(all_screens=True)
        self._screen_width = self._fullscreen_img.width
        self._screen_height = self._fullscreen_img.height

        self._tk_root = tk.Tk()
        self._tk_root.attributes("-fullscreen", True)
        try:
            self._tk_root.attributes("-alpha", 0.98)
        except Exception:
            pass
        self._tk_root.configure(cursor="cross")
        self._tk_root.attributes("-topmost", True)

        photo = ImageTk.PhotoImage(self._fullscreen_img)
        self._photo_ref = photo

        self._canvas = tk.Canvas(
            self._tk_root,
            width=self._screen_width,
            height=self._screen_height,
            highlightthickness=0,
            bd=0,
        )
        self._canvas.pack(fill="both", expand=True)
        self._canvas.create_image(0, 0, image=photo, anchor="nw")

        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._tk_root.bind("<Escape>", lambda e: self._cancel())
        self._tk_root.bind("<Control-c>", lambda e: self._cancel())

    def _on_press(self, event):
        self.drawing = True
        self.start_x = event.x
        self.start_y = event.y
        self.cur_x = event.x
        self.cur_y = event.y
        self._redraw_rect()

    def _on_drag(self, event):
        if not self.drawing:
            return
        self.cur_x = event.x
        self.cur_y = event.y
        self._redraw_rect()

    def _on_release(self, event):
        if not self.drawing:
            return
        self.drawing = False
        x1 = min(self.start_x, event.x)
        y1 = min(self.start_y, event.y)
        x2 = max(self.start_x, event.x)
        y2 = max(self.start_y, event.y)
        w = x2 - x1
        h = y2 - y1
        if w < 3 or h < 3:
            self._cancel()
            return
        try:
            crop = self._fullscreen_img.crop((x1, y1, x2, y2))
            image_bgr = pil_to_bgr(crop)
            region = (x1, y1, w, h)
            self._result = (image_bgr, region)
        except Exception:
            self._result = None
        try:
            self._tk_root.destroy()
        except Exception:
            pass
        self._event.set()
        if self.on_selected and self._result:
            try:
                self.on_selected(self._result[0], self._result[1])
            except Exception:
                pass

    def _redraw_rect(self):
        import tkinter as tk
        self._canvas.delete("sel_rect")
        if self.start_x is None or self.cur_x is None:
            return
        x1 = min(self.start_x, self.cur_x)
        y1 = min(self.start_y, self.cur_y)
        x2 = max(self.start_x, self.cur_x)
        y2 = max(self.start_y, self.cur_y)
        self._canvas.create_rectangle(
            x1, y1, x2, y2,
            outline="#FFD60A",
            width=2,
            dash=(4, 2),
            tags="sel_rect",
        )
        label_bg = self._canvas.create_rectangle(
            x1, y1 - 26, x1 + 220, y1 - 2,
            fill="#000000",
            outline="#FFD60A",
            tags="sel_rect",
        )
        self._canvas.create_text(
            x1 + 8, y1 - 14,
            text=f"区域: {x1}, {y1}  尺寸: {x2-x1}x{y2-y1}",
            fill="#FFFFFF",
            anchor="w",
            font=("Consolas", 11, "bold"),
            tags="sel_rect",
        )

    def _cancel(self):
        self._result = None
        try:
            self._tk_root.destroy()
        except Exception:
            pass
        self._event.set()

    def select(self):
        self._event.clear()
        self._result = None

        def run():
            try:
                self._build_fullscreen_toplevel()
                self._tk_root.mainloop()
            except Exception:
                self._event.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=120)
        return self._result
