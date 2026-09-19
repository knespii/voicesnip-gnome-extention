"""
On-screen overlay for VoiceSnip.

A compact capsule that shows what VoiceSnip is doing, in the spirit of Apple's
dictation HUD and Dynamic Island: a live microphone level meter while
recording, a short wave while Whisper works, then the transcribed text,
centred on its own. The capsule hugs its content - it widens with the text up
to MAX_W, then wraps and grows downwards, and stops at 40% of the screen
height, after which the text scrolls.

Why a separate process
----------------------
The overlay is GTK, VoiceSnip's own window is Tk, and two main loops cannot
share one process. VoiceSnip therefore spawns this module and feeds it one
JSON command per line on stdin:

    {"state": "listening"}
    {"level": 0.0 - 1.0}              microphone level, several times a second
    {"state": "processing"}
    {"state": "text", "text": "..."}
    {"state": "hidden"}
    {"state": "quit"}

Why it does not steal the keyboard focus
----------------------------------------
Text insertion goes to whichever window holds the focus, so an overlay that
took focus would receive VoiceSnip's own keystrokes (or its Ctrl+V) instead of
the application the user is dictating into. The window is created as an X11
override-redirect window (Gtk.WindowType.POPUP, forced onto the X11 backend),
which the window manager never focuses.

Why everything is painted by hand
---------------------------------
GTK3's CSS renderer clipped the border and the shadow to square edges, which
left a light fringe around the rounded corners. Painting the capsule with Cairo
gives exact, antialiased geometry. The same painter renders previews without a
display:

    python3 -m voicesnip.overlay --render preview.png
"""

import json
import math
import os
import subprocess
import sys
import threading
import time

# -- design (logical pixels) -------------------------------------------------

FONT_FAMILIES = "Inter, SF Pro Text, Roboto, Cantarell, Sans"
FONT_PX = 15
PAD_X = 16
PAD_Y = 11
MIN_H = 44                     # one line of text; the capsule's ends are this tall
RADIUS = MIN_H / 2
MAX_W = 560
MAX_H_FRACTION = 0.40          # of the monitor height, as requested
TOP_FRACTION = 0.18            # distance from the top edge; it grows downwards
MARGIN = 28                    # transparent room around the capsule for its shadow

METER_BARS = 5
BAR_W = 3
BAR_GAP = 3
BAR_MIN_H = 3                  # silence: the bars shrink to dots
BAR_MAX_H = 18
METER_W = METER_BARS * BAR_W + (METER_BARS - 1) * BAR_GAP
METER_GAP = 10                 # between the meter and the text
BAR_WEIGHTS = (0.42, 0.72, 1.0, 0.72, 0.42)
TEXT_MAX_W = MAX_W - 2 * PAD_X  # finished text wraps at this width

# Dark material with a specular rim. Opaque enough not to turn muddy over a
# bright wallpaper - without a background blur, translucency only washes out.
FILL_TOP = (34 / 255, 34 / 255, 38 / 255, 0.94)
FILL_BOTTOM = (16 / 255, 16 / 255, 18 / 255, 0.95)
SHEEN_ALPHA = 0.06
RIM_TOP_ALPHA = 0.24
RIM_BOTTOM_ALPHA = 0.06
TEXT_RGBA = (245 / 255, 245 / 255, 247 / 255, 1.0)
SECONDARY_RGBA = (235 / 255, 235 / 255, 245 / 255, 0.60)
BAR_RGBA = (1.0, 1.0, 1.0, 0.95)
SHADOW_ALPHA = 0.32
SHADOW_BLUR = 18
SHADOW_Y = 7
EDGE_FADE = 12                 # scrolled text fades out over this many pixels

MORPH_MS = 240
FADE_MS = 150
FRAME_MS = 16
SCROLL_STEP = 36

LABEL_LISTENING = "Poslouchám…"
LABEL_PROCESSING = "Přepisuji…"
LINE_GAP = 3                   # extra pixels between wrapped lines, so a paragraph breathes


# ---------------------------------------------------------------------------
# Client side: what VoiceSnip talks to
# ---------------------------------------------------------------------------

def _python_with_gtk():
    """An interpreter that can import gi, or None if there is none.

    VoiceSnip runs from a venv built without system site packages, so its own
    interpreter has no PyGObject - GTK comes from the distribution. Running the
    overlay in its own process is what makes that solvable: it can simply be
    launched with a different interpreter.
    """
    tried = []
    for candidate in (sys.executable, "/usr/bin/python3", "python3"):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        try:
            probe = subprocess.run([candidate, "-c", "import gi"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   timeout=15.0)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


class Overlay:
    """Handle on the overlay process. Every call is best-effort.

    The overlay is cosmetic, so a missing GTK, a crashed process or a broken
    pipe must never interrupt a dictation - all of them just disable it.
    """

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._process = None
        self._python = None
        # Level updates arrive from a feeder thread while state changes come
        # from the recording and processing threads; lines must not interleave.
        self._lock = threading.Lock()

    def _send(self, command):
        if not self.enabled:
            return
        with self._lock:
            try:
                if self._process is None or self._process.poll() is not None:
                    if self._python is None:
                        self._python = _python_with_gtk()
                        if self._python is None:
                            print("Overlay disabled: no Python with GTK3 "
                                  "(install python3-gi and gir1.2-gtk-3.0)")
                            self.enabled = False
                            return
                    self._process = subprocess.Popen(
                        [self._python, "-m", "voicesnip.overlay"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        start_new_session=True,
                    )
                self._process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
                self._process.stdin.flush()
            except (OSError, ValueError):
                # Spawning failed or the pipe is gone; stop trying.
                self.enabled = False
                self._process = None

    def listening(self):
        self._send({"state": "listening"})

    def level(self, value):
        """Microphone level for the meter, 0.0 (silence) to 1.0 (loud)."""
        self._send({"level": round(float(value), 3)})

    def processing(self):
        self._send({"state": "processing"})

    def show_text(self, text):
        self._send({"state": "text", "text": text})

    def hide(self):
        self._send({"state": "hidden"})

    def close(self):
        if self._process is None:
            return
        self._send({"state": "quit"})
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None


# ---------------------------------------------------------------------------
# Painting - shared by the live window and --render
# ---------------------------------------------------------------------------

def _ease_out(p):
    return 1 - (1 - p) ** 3


def _rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _Scene:
    """What the capsule shows, its animated geometry, and how to paint it.

    Imports GTK lazily: this module is also imported by VoiceSnip itself,
    whose venv has no PyGObject.
    """

    def __init__(self, max_height):
        import cairo
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo

        self.cairo, self.Pango, self.PangoCairo = cairo, Pango, PangoCairo
        self.max_h = max_height
        self.mode = "hidden"
        self.text = ""

        self.font = Pango.FontDescription.from_string(FONT_FAMILIES)
        self.font.set_absolute_size(FONT_PX * Pango.SCALE)
        self.pango = PangoCairo.FontMap.get_default().create_context()
        self.layout = Pango.Layout.new(self.pango)
        self.layout.set_font_description(self.font)
        self.layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        # A fixed gap rather than set_line_spacing(): that one multiplies the
        # font's full line height and with Roboto nearly doubled the spacing.
        self.layout.set_spacing(LINE_GAP * Pango.SCALE)

        self.text_w = self.text_h = 0
        self.w = self.h = float(MIN_H)          # current, animated
        self._morph = None                      # (from_w, from_h, to_w, to_h, t0)

        self.level_target = 0.0
        self.level = 0.0
        self.level_at = 0.0                     # when the last level arrived
        self.scroll = 0.0

    # -- content -----------------------------------------------------------

    def set_content(self, mode, text, now, from_collapsed=False):
        self.mode = mode
        self.text = text
        if mode == "text":
            # Wrapped and centred line by line within the widest the capsule
            # gets; painting centres that box on the capsule.
            self.layout.set_alignment(self.Pango.Alignment.CENTER)
            self.layout.set_width(TEXT_MAX_W * self.Pango.SCALE)
        else:
            # A short status label next to the meter: one line, no wrapping.
            self.layout.set_alignment(self.Pango.Alignment.LEFT)
            self.layout.set_width(-1)
        self.layout.set_text(self._label(), -1)
        self.text_w, self.text_h = self.layout.get_pixel_size()
        self.scroll = 0.0
        if from_collapsed:
            # Bloom from a dot, like Dynamic Island.
            self.w = self.h = float(MIN_H)
        target_w, target_h = self.target_size()
        self._morph = (self.w, self.h, target_w, target_h, now)

    def _label(self):
        if self.mode == "listening":
            return LABEL_LISTENING
        if self.mode == "processing":
            return LABEL_PROCESSING
        return self.text

    def _meter_w(self):
        """Room the level meter takes; finished text stands alone."""
        return 0 if self.mode == "text" else METER_W + METER_GAP

    def target_size(self):
        w = 2 * PAD_X + self._meter_w() + self.text_w
        h = max(MIN_H, self.text_h + 2 * PAD_Y)
        return float(max(MIN_H, w)), float(min(self.max_h, h))

    def set_level(self, value, now):
        self.level_target = max(0.0, min(1.0, float(value)))
        self.level_at = now

    def scroll_by(self, delta):
        overflow = self.overflow()
        self.scroll = max(0.0, min(overflow, self.scroll + delta))

    def overflow(self):
        return max(0.0, self.text_h + 2 * PAD_Y - self.h)

    # -- animation ---------------------------------------------------------

    def step(self, now):
        """Advance animations. Returns True while anything is still moving."""
        moving = False
        if self._morph:
            fw, fh, tw, th, t0 = self._morph
            p = min(1.0, (now - t0) * 1000 / MORPH_MS)
            e = _ease_out(p)
            self.w = fw + (tw - fw) * e
            self.h = fh + (th - fh) * e
            if p >= 1.0:
                self._morph = None
            moving = True
        if self.mode in ("listening", "processing"):
            rate = 0.45 if self.level_target > self.level else 0.15
            self.level += (self.level_target - self.level) * rate
            moving = True
        return moving

    def bar_values(self, now):
        if self.mode == "processing":
            return tuple(0.25 + 0.55 * (0.5 + 0.5 * math.sin(now * 6.5 - i * 0.75))
                         for i in range(METER_BARS))
        live = self.level
        if now - self.level_at > 0.6:
            # No level feed (an older VoiceSnip, say): breathe gently instead.
            live = 0.10 + 0.06 * (0.5 + 0.5 * math.sin(now * 2.2))
        return tuple(
            max(0.0, min(1.0, live * weight * (0.62 + 0.38 * math.sin(now * (7 + i) + i * 1.3))))
            for i, weight in enumerate(BAR_WEIGHTS))

    # -- painting ----------------------------------------------------------

    def paint(self, cr, canvas_w, canvas_h, now, scale=1.0):
        cairo = self.cairo
        x = (canvas_w - self.w) / 2
        y = MARGIN
        w, h = self.w, self.h
        r = min(RADIUS, h / 2)

        # Shadow, only outside the capsule so it never greys the material.
        cr.save()
        cr.rectangle(0, 0, canvas_w, canvas_h)
        _rounded_rect(cr, x, y, w, h, r)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()
        rings = 16
        ring_alpha = 1 - (1 - SHADOW_ALPHA) ** (1 / rings)
        for i in range(1, rings + 1):
            s = SHADOW_BLUR * i / rings
            _rounded_rect(cr, x - s / 2, y - s / 2 + SHADOW_Y, w + s, h + s, r + s / 2)
            cr.set_source_rgba(0, 0, 0, ring_alpha)
            cr.fill()
        _rounded_rect(cr, x - 0.5, y + 1, w + 1, h + 1, r + 0.5)      # contact shadow
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.fill()
        cr.restore()

        # Material.
        fill = cairo.LinearGradient(0, y, 0, y + h)
        fill.add_color_stop_rgba(0, *FILL_TOP)
        fill.add_color_stop_rgba(1, *FILL_BOTTOM)
        _rounded_rect(cr, x, y, w, h, r)
        cr.set_source(fill)
        cr.fill()

        sheen = cairo.LinearGradient(0, y, 0, y + MIN_H * 0.9)
        sheen.add_color_stop_rgba(0, 1, 1, 1, SHEEN_ALPHA)
        sheen.add_color_stop_rgba(1, 1, 1, 1, 0)
        _rounded_rect(cr, x, y, w, h, r)
        cr.set_source(sheen)
        cr.fill()

        # Hairline rim: one device pixel, bright on top like a lit edge.
        line = 1.0 / scale
        rim = cairo.LinearGradient(0, y, 0, y + h)
        rim.add_color_stop_rgba(0, 1, 1, 1, RIM_TOP_ALPHA)
        rim.add_color_stop_rgba(1, 1, 1, 1, RIM_BOTTOM_ALPHA)
        _rounded_rect(cr, x + line / 2, y + line / 2, w - line, h - line, r - line / 2)
        cr.set_line_width(line)
        cr.set_source(rim)
        cr.stroke()

        # Content, clipped to the capsule. While it is still blooming, fade
        # the content in so it does not spill out of a half-grown capsule.
        cr.save()
        _rounded_rect(cr, x, y, w, h, r)
        cr.clip()
        target_w, _ = self.target_size()
        reveal = max(0.0, min(1.0, (w - MIN_H) / max(1.0, target_w - MIN_H))) if target_w > MIN_H else 1.0

        overflow = self.overflow()
        centre_x = x + w / 2
        # Centred vertically whatever the line count; only text taller than
        # the capsule starts at the top padding, where it can scroll.
        text_y = y + PAD_Y - self.scroll if overflow > 0 else y + (h - self.text_h) / 2

        # Level meter and its label travel as one centred group.
        if self.mode != "text":
            group_x = centre_x - (self._meter_w() + self.text_w) / 2
            for i, v in enumerate(self.bar_values(now)):
                bh = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * v
                bx = group_x + i * (BAR_W + BAR_GAP)
                _rounded_rect(cr, bx, y + h / 2 - bh / 2, BAR_W, bh, BAR_W / 2)
                cr.set_source_rgba(BAR_RGBA[0], BAR_RGBA[1], BAR_RGBA[2],
                                   BAR_RGBA[3] * min(1.0, reveal * 2))
                cr.fill()
            tx = group_x + self._meter_w()
        else:
            # The layout is TEXT_MAX_W wide with each line centred inside it;
            # centring that box on the capsule centres every line.
            tx = centre_x - TEXT_MAX_W / 2

        # Text.
        self.PangoCairo.update_context(cr, self.pango)
        self.layout.context_changed()
        text_rgba = TEXT_RGBA if self.mode == "text" else SECONDARY_RGBA
        if overflow > 0:
            cr.push_group()
        cr.move_to(tx, text_y)
        cr.set_source_rgba(text_rgba[0], text_rgba[1], text_rgba[2], text_rgba[3] * reveal)
        self.PangoCairo.show_layout(cr, self.layout)
        if overflow > 0:
            pattern = cr.pop_group()
            mask = cairo.LinearGradient(0, y, 0, y + h)
            top = EDGE_FADE / h
            mask.add_color_stop_rgba(0, 0, 0, 0, 0 if self.scroll > 0.5 else 1)
            mask.add_color_stop_rgba(top, 0, 0, 0, 1)
            mask.add_color_stop_rgba(1 - top, 0, 0, 0, 1)
            mask.add_color_stop_rgba(1, 0, 0, 0, 0 if self.scroll < overflow - 0.5 else 1)
            cr.set_source(pattern)
            cr.mask(mask)

            # Scroll indicator, so it is obvious there is more.
            track_top, track_h = y + 10, h - 20
            thumb_h = max(18.0, track_h * h / (self.text_h + 2 * PAD_Y))
            thumb_y = track_top + (track_h - thumb_h) * (self.scroll / overflow)
            _rounded_rect(cr, x + w - 8, thumb_y, 3, thumb_h, 1.5)
            cr.set_source_rgba(1, 1, 1, 0.30)
            cr.fill()
        cr.restore()

    def capsule_rect(self, canvas_w):
        """Integer bounds of the capsule, for the window's input region."""
        x = (canvas_w - self.w) / 2
        return int(x), MARGIN, int(math.ceil(self.w)), int(math.ceil(self.h))


# ---------------------------------------------------------------------------
# The overlay process itself
# ---------------------------------------------------------------------------

def _run():
    # The window must be an X11 override-redirect window; on the Wayland
    # backend GTK has no way to ask for a surface the compositor will not
    # focus, and a focused overlay would swallow the keystrokes meant for the
    # user's application.
    os.environ["GDK_BACKEND"] = "x11"

    import cairo
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk, GLib

    class OverlayWindow(Gtk.Window):
        def __init__(self):
            super().__init__(type=Gtk.WindowType.POPUP)
            self.set_app_paintable(True)
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
            visual = self.get_screen().get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)

            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            geometry = monitor.get_geometry()
            self.scene = _Scene(max_height=int(geometry.height * MAX_H_FRACTION))

            # A fixed canvas big enough for the largest capsule: animating the
            # size then only means repainting, never resizing an X window
            # mid-animation. The input region keeps the empty part click-through.
            self.canvas_w = MAX_W + 2 * MARGIN
            self.canvas_h = self.scene.max_h + 2 * MARGIN
            self.set_default_size(self.canvas_w, self.canvas_h)
            self.resize(self.canvas_w, self.canvas_h)
            self.move(geometry.x + (geometry.width - self.canvas_w) // 2,
                      geometry.y + int(geometry.height * TOP_FRACTION) - MARGIN)

            self.area = Gtk.DrawingArea()
            self.area.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
            self.area.connect("draw", self._on_draw)
            self.area.connect("scroll-event", self._on_scroll)
            self.add(self.area)

            self._ticking = None
            self._fade = None           # (from, to, t0, then)
            self._visible = False

        # -- drawing -------------------------------------------------------

        def _on_draw(self, _widget, cr):
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            self.scene.paint(cr, self.canvas_w, self.canvas_h, time.monotonic(),
                             scale=self.get_scale_factor())
            return True

        def _on_scroll(self, _widget, event):
            ok, _dx, dy = event.get_scroll_deltas()
            if not ok:
                dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction, 0)
            self.scene.scroll_by(dy * SCROLL_STEP)
            self.area.queue_draw()
            return True

        def _update_input_region(self):
            x, y, w, h = self.scene.capsule_rect(self.canvas_w)
            self.input_shape_combine_region(cairo.Region(cairo.RectangleInt(x, y, w, h)))

        # -- animation loop --------------------------------------------------

        def _ensure_ticking(self):
            if self._ticking is None:
                self._ticking = GLib.timeout_add(FRAME_MS, self._tick)

        def _tick(self):
            now = time.monotonic()
            moving = self.scene.step(now)
            if self._fade:
                start, end, t0, then = self._fade
                p = min(1.0, (now - t0) * 1000 / FADE_MS)
                Gtk.Widget.set_opacity(self, start + (end - start) * _ease_out(p))
                if p >= 1.0:
                    self._fade = None
                    if then:
                        then()
                moving = True
            self._update_input_region()
            self.area.queue_draw()
            if not moving:
                self._ticking = None
                return False
            return True

        # -- states ------------------------------------------------------------

        def _show(self, mode, text=""):
            now = time.monotonic()
            appearing = not self._visible
            self.scene.set_content(mode, text, now, from_collapsed=appearing)
            if appearing:
                self._visible = True
                Gtk.Widget.set_opacity(self, 0.0)
                self.show_all()
                self._fade = (0.0, 1.0, now, None)
            self._ensure_ticking()

        def _hide(self):
            if not self._visible:
                return
            self._visible = False
            self._fade = (Gtk.Widget.get_opacity(self), 0.0, time.monotonic(), self.hide)
            self._ensure_ticking()

        def apply(self, command):
            if "level" in command:
                self.scene.set_level(command["level"], time.monotonic())
            state = command.get("state")
            if state in ("listening", "processing"):
                self._show(state)
            elif state == "text":
                self._show("text", command.get("text", ""))
            elif state == "hidden":
                self._hide()
            elif state == "quit":
                Gtk.main_quit()
            return False

    window = OverlayWindow()
    window.realize()
    # VoiceSnip sends stdout to /dev/null, so this only ever shows up when the
    # overlay is run by hand - where knowing the X window id is what lets you
    # inspect or screenshot it.
    print(window.get_window().get_xid(), flush=True)

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except ValueError:
                continue
            GLib.idle_add(window.apply, command)
        GLib.idle_add(Gtk.main_quit)

    threading.Thread(target=reader, daemon=True).start()
    Gtk.main()


# ---------------------------------------------------------------------------
# Previews without a display
# ---------------------------------------------------------------------------

def _render(path, scale=2):
    """Paint every state onto one sheet over a bright sky, like the user's
    wallpaper, so colours and edges can be judged without a live session."""
    import cairo

    screen_h = 1065                               # the user's logical height
    scene = _Scene(max_height=int(screen_h * MAX_H_FRACTION))
    canvas_w = MAX_W + 2 * MARGIN
    long_text = ("Dobrý den, tohle je zkouška diktování. Příliš žluťoučký kůň úpěl "
                 "ďábelské ódy. ") * 16
    states = [
        ("listening", "", 0.75),
        ("processing", "", 0.0),
        ("text", "Dobrý den, tohle je zkouška diktování.", 0.0),
        ("text", "Dobrý den, tohle je zkouška diktování. Příliš žluťoučký kůň úpěl "
                 "ďábelské ódy a rovnou za ním šel ještě jeden, který už tak "
                 "žluťoučký nebyl.", 0.0),
        ("text", long_text.strip(), 0.0),
    ]
    heights = []
    for mode, text, _ in states:
        scene.set_content(mode, text, 0.0)
        heights.append(int(scene.target_size()[1]) + 2 * MARGIN)
    sheet_h = sum(heights)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_w * scale, sheet_h * scale)
    cr = cairo.Context(surface)
    cr.scale(scale, scale)
    sky = cairo.LinearGradient(0, 0, 0, sheet_h)
    sky.add_color_stop_rgb(0, 0.43, 0.64, 0.87)
    sky.add_color_stop_rgb(1, 0.80, 0.87, 0.94)
    cr.set_source(sky)
    cr.paint()
    for cx, cy, rad in ((90, 60, 70), (430, 210, 110), (200, 520, 140), (520, 760, 120)):
        cloud = cairo.RadialGradient(cx, cy, 0, cx, cy, rad)
        cloud.add_color_stop_rgba(0, 1, 1, 1, 0.55)
        cloud.add_color_stop_rgba(1, 1, 1, 1, 0)
        cr.set_source(cloud)
        cr.paint()

    offset = 0
    for (mode, text, level), height in zip(states, heights):
        scene.set_content(mode, text, 0.0)
        scene.w, scene.h = scene.target_size()
        scene._morph = None
        scene.level = scene.level_target = level
        scene.level_at = 1.0
        cr.save()
        cr.translate(0, offset)
        scene.paint(cr, canvas_w, height, now=1.0, scale=scale)
        cr.restore()
        offset += height
    surface.write_to_png(path)
    print(path)


if __name__ == "__main__":
    if "--render" in sys.argv:
        _render(sys.argv[sys.argv.index("--render") + 1])
    else:
        _run()
