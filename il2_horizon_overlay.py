"""
IL-2 Great Battles — Artificial Horizon Overlay
================================================
Transparent, always-on-top, click-through gauge that sits top-center on the
screen and shows live bank/pitch from IL-2's UDP motion telemetry.

Zero pip dependencies — uses only the Python standard library + Windows APIs
via ctypes. Requires Windows and Python 3.8+.

Setup: see README.md (you must enable [KEY = motiondevice] in IL-2's
data/startup.cfg, and run the game in borderless/windowed fullscreen).

Hotkeys (global, work while the game has focus):
  H        -> toggles the overlay (matches IL-2's default HUD toggle)
  Alt+H    -> also toggles it (matches IL-2's "hide full interface")
  F9       -> dedicated overlay-only toggle, for re-syncing if the game HUD
              and the overlay ever get out of step
"""

import math
import socket
import struct
import threading
import time
import tkinter as tk

# ============================== CONFIG ======================================

UDP_IP            = "0.0.0.0"   # listen address
UDP_PORT          = 4321        # must match 'port' in startup.cfg [KEY = motiondevice]

GAUGE_SIZE        = 180         # px, overall widget size (square)
MARGIN_TOP        = 20          # px from top edge of screen

PX_PER_DEG        = 1.4         # pitch ladder scale (px per degree of pitch)
SHOW_READOUT      = True        # numeric "bank / pitch" text under the gauge

# Flip these if the horizon moves the wrong way on your install.
INVERT_BANK       = False
INVERT_PITCH      = True        # IL-2's motiondevice pitch sign reads nose-down as positive on this install

SYNC_WITH_H_KEY   = True        # toggle overlay when H / Alt+H is pressed
TOGGLE_VK         = 0x78        # VK code for the dedicated toggle key (F9=0x78)
H_VK              = 0x48        # 'H'

STALE_AFTER_S     = 2.0         # dim the gauge if no telemetry for this long

# Colors (TRANSPARENT_KEY is chroma-keyed away — don't use it for artwork)
TRANSPARENT_KEY   = "#010203"
COL_BEZEL_BG      = "#0d0d0d"
COL_BEZEL_RING    = "#3a3a3a"
COL_SKY           = "#2f6fa8"
COL_GROUND        = "#6b4a2a"
COL_LINES         = "#e8e8e8"
COL_TICKS         = "#777777"
COL_AIRCRAFT      = "#39ff6a"
COL_READOUT       = "#999999"
COL_STALE         = "#552222"

# ============================ TELEMETRY =====================================

IL2_MAGIC  = 0x494C0100                 # packetID for motiondevice packets
PKT_STRUCT = struct.Struct("<II9f")     # packetID, tick, yaw,pitch,roll,
                                        # spinX..Z, accX..Z  (44 bytes, LE)
RAD2DEG    = 180.0 / math.pi


class TelemetryListener:
    """Background UDP listener. Keeps only the latest attitude."""

    def __init__(self, ip, port):
        self.ip, self.port = ip, port
        self.lock = threading.Lock()
        self.bank_deg = 0.0
        self.pitch_deg = 0.0
        self.last_packet_t = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        try:
            sock.bind((self.ip, self.port))
        except OSError as e:
            print(f"[!] Could not bind UDP {self.ip}:{self.port} — {e}")
            print("    Is another telemetry app (SimShaker etc.) using this port?")
            print("    You can add a second motiondevice-style consumer by choosing")
            print("    a different port in startup.cfg and in CONFIG above.")
            return
        print(f"[*] Listening for IL-2 motion telemetry on UDP {self.ip}:{self.port}")

        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < PKT_STRUCT.size:
                continue
            fields = PKT_STRUCT.unpack_from(data)
            if fields[0] != IL2_MAGIC:
                continue
            # fields: packetID, tick, yaw, pitch, roll, spinX..Z, accX..Z
            pitch = fields[3] * RAD2DEG
            roll  = fields[4] * RAD2DEG
            if INVERT_PITCH:
                pitch = -pitch
            if INVERT_BANK:
                roll = -roll
            with self.lock:
                self.pitch_deg = pitch
                self.bank_deg = roll
                self.last_packet_t = time.time()
        sock.close()

    def snapshot(self):
        with self.lock:
            return self.bank_deg, self.pitch_deg, self.last_packet_t


# ============================ WIN32 GLUE ====================================

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # click-through
WS_EX_NOACTIVATE  = 0x08000000   # never steals focus
WS_EX_TOOLWINDOW  = 0x00000080   # hidden from alt-tab
HWND_TOPMOST      = -1
SWP_NOMOVE        = 0x0002
SWP_NOSIZE        = 0x0001
SWP_NOACTIVATE    = 0x0010
VK_MENU           = 0x12         # Alt


def make_click_through(hwnd):
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def assert_topmost(hwnd):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ============================== GAUGE =======================================

class HorizonOverlay:
    def __init__(self, telem: TelemetryListener):
        self.telem = telem
        self.visible = True
        self._h_was_down = False
        self._f9_was_down = False
        self._topmost_counter = 0

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg=TRANSPARENT_KEY)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            print("[!] -transparentcolor unsupported (non-Windows?) — "
                  "running with opaque background")

        w = GAUGE_SIZE
        h = GAUGE_SIZE + (22 if SHOW_READOUT else 0)
        sw = self.root.winfo_screenwidth()
        x = (sw - w) // 2
        y = MARGIN_TOP
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.canvas = tk.Canvas(self.root, width=w, height=h,
                                bg=TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack()

        self.cx = w / 2
        self.cy = GAUGE_SIZE / 2
        self.radius = GAUGE_SIZE / 2 - 8

        self.root.update_idletasks()
        self.hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        make_click_through(self.hwnd)
        assert_topmost(self.hwnd)

        self._draw_static_reference = None  # ticks are redrawn each frame (cheap)
        self.root.after(20, self._tick)

    # ---- geometry helpers ---------------------------------------------------

    def _horizon_frame(self, bank_deg, pitch_deg):
        """Unit vectors of the horizon line in screen coords.
        d = along-horizon direction, n = 'up towards sky' normal."""
        a = math.radians(-bank_deg)
        d = (math.cos(a), math.sin(a))
        n = (math.sin(a), -math.cos(a))     # screen-up when bank=0 (y grows down)
        # nose up (pitch+) pushes the horizon DOWN the screen -> line center
        # moves opposite to sky normal
        off = pitch_deg * PX_PER_DEG
        cx = self.cx - n[0] * off
        cy = self.cy - n[1] * off
        return d, n, cx, cy

    def _sky_polygon(self, d, n, lx, ly, samples=48):
        """Polygon approximating the sky region of the bezel disc."""
        r, cx, cy = self.radius, self.cx, self.cy
        # signed distance from disc center to horizon line (along sky normal)
        h = (cx - lx) * n[0] + (cy - ly) * n[1]
        if h >= r:       # disc entirely on the sky side -> all sky
            return self._full_disc(samples)
        if h <= -r:      # disc entirely on the ground side -> no sky
            return None
        # chord endpoints
        half = math.sqrt(max(r * r - h * h, 0.0))
        mx, my = cx - n[0] * h, cy - n[1] * h   # foot of perpendicular on line
        p1 = (mx - d[0] * half, my - d[1] * half)
        p2 = (mx + d[0] * half, my + d[1] * half)
        # arc from p2 back to p1 going through the sky side
        a1 = math.atan2(p2[1] - cy, p2[0] - cx)
        a2 = math.atan2(p1[1] - cy, p1[0] - cx)
        # pick sweep direction whose midpoint lies on the sky side of the line
        pts = [p1, p2]
        for sweep in (1, -1):
            da = (a2 - a1) % (2 * math.pi)
            if sweep == -1:
                da = da - 2 * math.pi
            mid_a = a1 + da / 2
            mx2 = cx + r * math.cos(mid_a)
            my2 = cy + r * math.sin(mid_a)
            side = (mx2 - lx) * n[0] + (my2 - ly) * n[1]
            if side > 0:
                arc = [
                    (cx + r * math.cos(a1 + da * i / samples),
                     cy + r * math.sin(a1 + da * i / samples))
                    for i in range(samples + 1)
                ]
                return [p1, p2] + arc[1:]
        return None

    def _full_disc(self, samples=48):
        r, cx, cy = self.radius, self.cx, self.cy
        return [(cx + r * math.cos(2 * math.pi * i / samples),
                 cy + r * math.sin(2 * math.pi * i / samples))
                for i in range(samples)]

    # ---- rendering ----------------------------------------------------------

    def _draw(self, bank, pitch, stale):
        c = self.canvas
        c.delete("all")
        cx, cy, r = self.cx, self.cy, self.radius

        # bezel background disc (ground color as base layer)
        ground = COL_GROUND if not stale else COL_STALE
        sky = COL_SKY if not stale else "#333344"
        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                      fill=ground, outline="")

        # sky polygon on top
        d, n, lx, ly = self._horizon_frame(bank, pitch)
        poly = self._sky_polygon(d, n, lx, ly)
        if poly:
            c.create_polygon([coord for p in poly for coord in p],
                             fill=sky, outline="")

        # horizon line (clipped chord)
        h = (cx - lx) * n[0] + (cy - ly) * n[1]
        if abs(h) < r:
            half = math.sqrt(r * r - h * h)
            mx, my = cx - n[0] * h, cy - n[1] * h
            c.create_line(mx - d[0] * half, my - d[1] * half,
                          mx + d[0] * half, my + d[1] * half,
                          fill=COL_LINES, width=2)

        # pitch ladder: rungs every 10 deg, clipped to disc
        for deg in range(-90, 91, 10):
            if deg == 0:
                continue
            off = (pitch - deg) * PX_PER_DEG   # rung offset from disc center
            # rung center = disc center shifted along -n by (pitch-deg)*scale
            rxc = cx - n[0] * off
            ryc = cy - n[1] * off
            dist = math.hypot(rxc - cx, ryc - cy)
            if dist > r - 6:
                continue
            hw = 15 if deg % 30 else 26
            c.create_line(rxc - d[0] * hw, ryc - d[1] * hw,
                          rxc + d[0] * hw, ryc + d[1] * hw,
                          fill=COL_LINES, width=1)
            if deg % 30 == 0:
                c.create_text(rxc + d[0] * (hw + 10), ryc + d[1] * (hw + 10),
                              text=str(abs(deg)), fill=COL_LINES,
                              font=("Consolas", 7))

        # bezel ring + bank ticks (fixed)
        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                      outline=COL_BEZEL_RING, width=3)
        for tick in (0, 30, -30, 60, -60, 90, -90, 180):
            a = math.radians(tick - 90)
            x1, y1 = cx + (r - 1) * math.cos(a), cy + (r - 1) * math.sin(a)
            x2, y2 = cx + (r - 8) * math.cos(a), cy + (r - 8) * math.sin(a)
            c.create_line(x1, y1, x2, y2, fill=COL_TICKS, width=1)

        # bank pointer (rotates with roll, points at top-center when level)
        pa = math.radians(-bank - 90)
        tipx, tipy = cx + (r - 2) * math.cos(pa), cy + (r - 2) * math.sin(pa)
        b1x = cx + (r - 12) * math.cos(pa + 0.10)
        b1y = cy + (r - 12) * math.sin(pa + 0.10)
        b2x = cx + (r - 12) * math.cos(pa - 0.10)
        b2y = cy + (r - 12) * math.sin(pa - 0.10)
        c.create_polygon(tipx, tipy, b1x, b1y, b2x, b2y,
                         fill=COL_LINES, outline="")

        # fixed aircraft symbol
        c.create_line(cx - 30, cy, cx - 12, cy, fill=COL_AIRCRAFT,
                      width=3, capstyle=tk.ROUND)
        c.create_line(cx + 12, cy, cx + 30, cy, fill=COL_AIRCRAFT,
                      width=3, capstyle=tk.ROUND)
        c.create_line(cx, cy, cx, cy - 8, fill=COL_AIRCRAFT,
                      width=3, capstyle=tk.ROUND)
        c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                      fill=COL_AIRCRAFT, outline="")

        if SHOW_READOUT:
            txt = ("waiting for telemetry…" if stale
                   else f"bank {bank:+.0f}\u00b0 \u00b7 pitch {pitch:+.0f}\u00b0")
            c.create_text(cx, GAUGE_SIZE + 10, text=txt,
                          fill=COL_READOUT, font=("Consolas", 9))

    # ---- main loop ----------------------------------------------------------

    def _poll_hotkeys(self):
        h_down = key_down(H_VK)
        if SYNC_WITH_H_KEY and h_down and not self._h_was_down:
            self.toggle()
        self._h_was_down = h_down

        f9_down = key_down(TOGGLE_VK)
        if f9_down and not self._f9_was_down:
            self.toggle()
        self._f9_was_down = f9_down

    def toggle(self):
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
            assert_topmost(self.hwnd)
        else:
            self.root.withdraw()

    def _tick(self):
        self._poll_hotkeys()
        if self.visible:
            bank, pitch, last_t = self.telem.snapshot()
            stale = (time.time() - last_t) > STALE_AFTER_S
            self._draw(bank, pitch, stale)
            # games love to steal topmost; re-assert about once a second
            self._topmost_counter += 1
            if self._topmost_counter >= 50:
                self._topmost_counter = 0
                assert_topmost(self.hwnd)
        self.root.after(20, self._tick)   # ~50 fps, matches telemetry rate

    def run(self):
        self.root.mainloop()


# =============================== MAIN =======================================

if __name__ == "__main__":
    telem = TelemetryListener(UDP_IP, UDP_PORT)
    telem.start()
    overlay = HorizonOverlay(telem)
    print("[*] Overlay running. H / Alt+H / F9 toggles it. Ctrl+C here to quit.")
    try:
        overlay.run()
    finally:
        telem.stop()
