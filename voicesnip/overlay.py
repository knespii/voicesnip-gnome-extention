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

FONT_FAMILIES = "Montserrat, Inter, Poppins, Roboto, Cantarell, Sans"
FONT_PX = 15
FONT_WEIGHT = 500              # medium: a touch heavier than the body weight
PAD_X = 20                     # generous padding: the capsule should feel calm
PAD_Y = 13
MIN_H = 48                     # one line of text; the capsule is this tall
RADIUS = 18                    # A rounded rectangle, not a stadium: the
                               # reference the design follows has a soft
                               # corner, not a capsule end. Fixed at every
                               # size - a radius proportional to the height
                               # would animate the shape's whole character
                               # during a resize, which reads as morphing
                               # rather than growing.
MAX_W = 560
MAX_H_FRACTION = 0.40          # of the monitor height, as requested
TOP_FRACTION = 0.18            # distance from the top edge; it grows downwards
MARGIN = 56                    # transparent room around the capsule for its
                               # bloom; must be at least skin.BLOOM_REACH

WAVE_W = 92                    # the spectral waveform's box
WAVE_H = 30
WAVE_GAP = 14                  # between the waveform and the text
TEXT_MAX_W = MAX_W - 2 * PAD_X # finished text wraps at this width

# The capsule's colours, its bloom and its outline all live in overlay_skin.
TEXT_RGBA = (245 / 255, 245 / 255, 247 / 255, 1.0)
WAVE_TIME = 0.18               # the waveform's pace; the dial to turn if it
                               # ever feels too fast or too sleepy
BLOOM_ACTIVE = 0.78            # How hard the bloom burns. While the microphone
BLOOM_VOICE = 0.22             # is open it leans on the same slow envelope the
BLOOM_IDLE = 0.62              # waveform uses, so the pill breathes with the
                               # voice instead of sitting dead; behind finished
                               # text it settles to one calm value.

EDGE_FADE = 12                 # scrolled text fades out over this many pixels

MORPH_MS = 240                 # a small change in size
MORPH_MS_FAR = 420             # ...and a big one, so a card unfolding reads as
                               # a movement rather than a jump
OPEN_MS = 520                  # The capsule draws itself on: the lit outline
OPEN_W = float(MIN_H)          # is traced from the top-left corner while the
TRACE_LAG = 0.18               # shape widens out of a narrow token, and the
                               # material follows a little behind the tip. One
                               # gesture rather than a thing appearing.
CLOSE_MS = 300                 # leaving is the arrival reversed: this for a
CLOSE_MS_FAR = 460             # status pill, and this for a full card, which
                               # has twice as far to go
EDGE_FADE_MS = 360             # the border light comes up slower than the pill
_REPORT_FPS = os.environ.get("VOICESNIP_OVERLAY_FPS") == "1"
PROCESSING_FRAME_MS = 33       # while Whisper works the waveform is only a
                               # stand-in, and the transcription wants the CPU
                               # more than the animation does
FRAME_MS = 16                  # target 60 fps: fine hairlines in motion strobe
                               # at 30, and frame rate buys more here than
                               # extra strands do. We draw on a
                               # whole number of the display's refreshes, never
                               # on a timer: on a 165 Hz screen a 30 fps timer
                               # lands 5 or 6 refreshes apart by turns, which is
                               # exactly what judder looks like.
LEVEL_ATTACK_S = 0.12          # The waveform follows the voice with a time
LEVEL_RELEASE_S = 0.40         # constant rather than a per-frame fraction.
                               # Speech is spiky - syllables and plosives - and
                               # a fast attack turns that into visible jumping.
DENSITY_TAU_S = 1.6            # A slow envelope of the voice: it decides how
                               # rich the waveform's shape is and how hard the
                               # bloom burns, neither of which should change
                               # with every syllable.
SCROLL_STEP = 36

# Screen-edge glow: a second, click-through window that lights the border of
# the display for as long as the capsule is up.
EDGE_BAND = 96                 # how far the light reaches in, in logical pixels
EDGE_LOWRES = 0.12             # painted at a twelfth and scaled up; at full
                               # resolution a full-screen effect could not be
                               # redrawn every frame
EDGE_FPS = 14                  # A full-screen window is expensive whatever we
                               # do with it: merely having one costs about a
                               # third of the frame rate, and refreshing it 20
                               # times a second costs as much again. The glow
                               # drifts slowly enough that ten is plenty.
EDGE_TIME = 0.32               # slow enough that the low refresh never shows
EDGE_ACTIVE = 1.0              # while the microphone is open or Whisper works
EDGE_IDLE = 0.55               # gentler behind the finished text

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

    def prewarm(self):
        """Start the overlay process now, so the first capsule is not late.

        Importing GTK, realizing the windows and rendering a first frame takes
        a few hundred milliseconds. Doing it when the hotkey is first pressed
        is exactly when it shows.
        """
        threading.Thread(target=self._send, args=({"state": "prewarm"},),
                         daemon=True).start()

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


def _ease_away(p):
    """For leaving: the arrival's gesture reversed, on a symmetric curve.

    Easing out again on the way out is not a reversal - it makes both ends
    linger. The exact mirror, a cubic ease-in, is not right either: it spends
    half the departure doing nothing visible and then snaps. This eases in
    and out, so the direction of travel is reversed and nothing is dead.
    """
    return p * p * (3.0 - 2.0 * p)


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

        from . import overlay_skin, overlay_wave

        self.cairo, self.Pango, self.PangoCairo = cairo, Pango, PangoCairo
        self.skin, self.wave = overlay_skin, overlay_wave
        self.max_h = max_height
        self.mode = "hidden"
        self.text = ""

        self.font = Pango.FontDescription.from_string(FONT_FAMILIES)
        self.font.set_absolute_size(FONT_PX * Pango.SCALE)
        self.font.set_weight(Pango.Weight(FONT_WEIGHT))
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
        self._morph_ms = MORPH_MS

        self.level_target = 0.0
        self.level = 0.0
        self.level_at = 0.0                     # when the last level arrived
        self.slow_level = 0.0                   # the voice's slow envelope
        self._stepped_at = None
        self.scroll = 0.0
        self._scale = 1.0
        self.trace = 1.0               # how much of the outline is drawn, 0..1
        self._trace = None             # (from, to, t0, duration)
        self._ease = _ease_out         # ...and out again when leaving

    # -- content -----------------------------------------------------------

    def set_content(self, mode, text, now, from_collapsed=False):
        self._ease = _ease_out
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
        target_w, target_h = self.target_size()
        if from_collapsed:
            # Start as a narrow token at full height and widen out of it while
            # the outline is traced: the capsule draws itself on rather than
            # arriving whole.
            self.w, self.h = min(OPEN_W, target_w), target_h
            self._trace = (0.0, 1.0, now, OPEN_MS)
            self.trace = 0.0
        self._morph = (self.w, self.h, target_w, target_h, now, from_collapsed)
        self._morph_ms = (OPEN_MS if from_collapsed else
                          self._morph_duration(target_w - self.w,
                                               target_h - self.h))

    def _label(self):
        if self.mode == "listening":
            return LABEL_LISTENING
        if self.mode == "processing":
            return LABEL_PROCESSING
        return self.text

    def _meter_w(self):
        """Room the waveform takes; finished text stands alone."""
        return 0 if self.mode == "text" else WAVE_W + WAVE_GAP

    def target_size(self):
        w = 2 * PAD_X + self._meter_w() + self.text_w
        h = max(MIN_H, self.text_h + 2 * PAD_Y)
        return float(max(MIN_H, w)), float(min(self.max_h, h))

    @staticmethod
    def _morph_duration(dw, dh):
        """How long a resize should take: the further it travels, the longer.

        One fixed duration made a small change feel sluggish and a big one -
        a status pill unfolding into a card - feel like a jump cut.
        """
        far = min(1.0, (abs(dw) + 2 * abs(dh)) / 420.0)
        return MORPH_MS + (MORPH_MS_FAR - MORPH_MS) * far

    def collapse(self, now):
        """The way in, run backwards.

        The outline undraws from the tip, the capsule narrows back into the
        token it grew out of, and the material goes first - exactly the
        arrival reversed. It leaves from wherever it happens to be: a card is
        twice the width of a status pill, so the distance is measured rather
        than assumed, and a wide one is given proportionally longer so it
        does not snap shut.
        """
        self._ease = _ease_away
        target_w = min(OPEN_W, self.w)
        span = min(1.0, (self.w - target_w) / max(1.0, MAX_W - OPEN_W))
        duration = CLOSE_MS + (CLOSE_MS_FAR - CLOSE_MS) * span
        self._morph = (self.w, self.h, target_w, self.h, now, False)
        self._morph_ms = duration
        self._trace = (self.trace, 0.0, now, duration)

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
        if self._trace:
            start, end, t0, duration = self._trace
            p = min(1.0, (now - t0) * 1000 / duration)
            self.trace = start + (end - start) * self._ease(p)
            if p >= 1.0:
                self.trace = end
                self._trace = None
            moving = True
        if self._morph:
            fw, fh, tw, th, t0, opening = self._morph
            p = min(1.0, (now - t0) * 1000 / self._morph_ms)
            e = self._ease(p)
            self.w = fw + (tw - fw) * e
            self.h = fh + (th - fh) * e
            if p >= 1.0:
                self._morph = None
            moving = True
        if self.mode in ("listening", "processing"):
            dt = min(0.2, max(0.0, now - (self._stepped_at or now)))
            tau = LEVEL_ATTACK_S if self.level_target > self.level else LEVEL_RELEASE_S
            self.level += (self.level_target - self.level) * (1 - math.exp(-dt / tau))
            self.slow_level += (self.level - self.slow_level) * (1 - math.exp(-dt / DENSITY_TAU_S))
            moving = True
        self._stepped_at = now
        return moving

    def wave_level(self, now):
        """What the waveform should show: the voice, or a stand-in."""
        if self.mode == "processing":
            # Nothing is being heard any more, so keep it moving by itself.
            return 0.34 + 0.22 * math.sin(now * 1.4)
        if now - self.level_at > 0.6:
            # No level feed (an older VoiceSnip, say): breathe gently instead.
            return 0.09 + 0.05 * (0.5 + 0.5 * math.sin(now * 2.2))
        return self.level

    # -- painting ----------------------------------------------------------

    def paint(self, cr, canvas_w, canvas_h, now, scale=1.0):
        self._scale = scale
        # Snapped to whole device pixels. The capsule's width and height are
        # fractional all through an animation, and a lit hairline landing on
        # fractional coordinates visibly softens and sharpens again as it
        # moves. The easing keeps its own fractional values - only what gets
        # drawn is snapped.
        snap = lambda v: round(v * scale) / scale
        w, h = snap(self.w), snap(self.h)
        x, y = snap((canvas_w - w) / 2), float(MARGIN)
        r = max(0.0, min(RADIUS, w / 2, h / 2))
        material = self.material_alpha()
        if material <= 0.002 and self.trace <= 0.0:
            return

        if material > 0.002:
            self.skin.paint_bloom(cr, x, y, w, h, r, scale, MARGIN,
                                  self.bloom_intensity() * material)
            if material < 0.999:
                # Behind the advancing tip the material is still arriving;
                # a group keeps the layers from showing through each other.
                cr.push_group()
            self.skin.paint_body(cr, x, y, w, h, r)
            self.skin.paint_bleed(cr, x, y, w, h, r)
            self.skin.paint_grain(cr, x, y, w, h, r)
            if material < 0.999:
                cr.pop_group_to_source()
                cr.paint_with_alpha(material)

        self.skin.paint_rim(cr, x, y, w, h, r, scale, self.trace)

        # Content, clipped to the capsule. While it is still blooming, fade
        # the content in so it does not spill out of a half-grown capsule.
        cr.save()
        # A plain rectangle, not the rounded path: clipping to a curve makes
        # every stroke inside it dramatically more expensive, and the padding
        # keeps the content well clear of the corners anyway.
        cr.rectangle(x, y, w, h)
        cr.clip()
        target_w, _ = self.target_size()
        grown = (max(0.0, min(1.0, (w - OPEN_W) / max(1.0, target_w - OPEN_W)))
                 if target_w > OPEN_W else 1.0)
        reveal = min(grown, material)

        # Anchored the way the settled capsule will be, not the way a
        # half-grown one would be: otherwise the text jumps at the moment a
        # growing card stops overflowing.
        overflow = max(0.0, self.text_h + 2 * PAD_Y - self.target_size()[1])
        centre_x = x + w / 2
        # Centred vertically whatever the line count; only text taller than
        # the capsule starts at the top padding, where it can scroll.
        text_y = y + PAD_Y - self.scroll if overflow > 0 else y + (h - self.text_h) / 2

        # Waveform and label travel as one centred group.
        if self.mode != "text":
            group_x = centre_x - (self._meter_w() + self.text_w) / 2
            wave_alpha = min(1.0, reveal * 2)
            wave_y = y + (h - WAVE_H) / 2
            if wave_alpha > 0.999:
                # The usual case; a group would only be needed to fade it.
                self.wave.paint_wave(cr, group_x, wave_y, WAVE_W, WAVE_H,
                                     self.wave_level(now), now * WAVE_TIME,
                                     self.wave_density())
            else:
                cr.push_group()
                self.wave.paint_wave(cr, group_x, wave_y, WAVE_W, WAVE_H,
                                     self.wave_level(now), now * WAVE_TIME,
                                     self.wave_density())
                faded = cr.pop_group()
                cr.set_source(faded)
                cr.paint_with_alpha(wave_alpha)
            tx = group_x + self._meter_w()
        else:
            # The layout is TEXT_MAX_W wide with each line centred inside it;
            # centring that box on the capsule centres every line.
            tx = centre_x - TEXT_MAX_W / 2

        # Text.
        self.PangoCairo.update_context(cr, self.pango)
        self.layout.context_changed()
        # White in every mode: the status label sits over the glow.
        text_rgba = TEXT_RGBA
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

            # Scroll indicator, so it is obvious there is more. Not while the
            # capsule is still growing, where it would flick past as a stripe.
            if self._morph is not None:
                cr.restore()
                return
            track_top, track_h = y + 10, h - 20
            thumb_h = max(18.0, track_h * h / (self.text_h + 2 * PAD_Y))
            thumb_y = track_top + (track_h - thumb_h) * (self.scroll / overflow)
            # Clear of the side bleed, and bright enough to be seen against it.
            _rounded_rect(cr, x + w - 10, thumb_y, 3, thumb_h, 1.5)
            cr.set_source_rgba(1, 1, 1, 0.55)
            cr.fill()
        cr.restore()

    def wave_density(self):
        """How rich the waveform's shape is - deliberately slow, so speech
        changes its height without reshuffling its pattern."""
        return 0.25 + 0.75 * self.slow_level

    def bloom_intensity(self):
        """How hard the bloom burns: it breathes with the voice while the
        microphone is open, and settles behind finished text."""
        if self.mode == "text":
            return BLOOM_IDLE
        return BLOOM_ACTIVE + BLOOM_VOICE * self.slow_level

    def material_alpha(self):
        """How much of the body has arrived, trailing the outline's tip.

        The outline is drawn first and the material fills in behind it, so
        the capsule reads as being drawn on rather than switched on. Run the
        same relation backwards and the material is the first thing to go.
        """
        if self._trace is None:
            return 1.0 if self.trace > 0.0 else 0.0
        return max(0.0, min(1.0, (self.trace - TRACE_LAG) / (1.0 - TRACE_LAG)))

    def gone(self):
        """Nothing left on screen: safe to take the window down."""
        return self._trace is None and self.trace <= 0.0

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

    from . import overlay_edge

    def _monitor_geometry():
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    class EdgeWindow(Gtk.Window):
        """The glow around the screen, in the capsule's colours.

        Click-through: its input region is empty, so every pointer event goes
        to whatever is underneath. It never animates on its own - the capsule
        window drives it, so both share one clock and one frame.
        """

        def __init__(self, geometry):
            super().__init__(type=Gtk.WindowType.POPUP)
            self.set_app_paintable(True)
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
            visual = self.get_screen().get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)
            self.geometry = geometry
            self.set_default_size(geometry.width, geometry.height)
            self.move(geometry.x, geometry.y)
            self.intensity = 0.0
            self._now = 0.0
            self._small = None
            self._small_at = None
            self.connect("draw", self._on_draw)
            self.realize()
            self.input_shape_combine_region(cairo.Region())
            # The middle is deliberately NOT cut out of the window. Shaping it
            # handed the compositor a frame instead of the whole display, which
            # was cheaper - but a hard region boundary drawn on a fractionally
            # scaled screen leaves a thin dark rectangle where the shape ends,
            # a hundred pixels in from every edge. Alpha does the shaping now,
            # and only the band is ever redrawn.

        def _on_draw(self, _widget, cr):
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            if self.intensity <= 0.002:
                return True
            w, h = self.geometry.width, self.geometry.height
            sw = max(8, int(w * EDGE_LOWRES))
            sh = max(8, int(h * EDGE_LOWRES))
            if self._small is None or self._small.get_width() != sw:
                self._small = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
                self._small_at = None
            # The band is damaged as four strips, so this handler runs several
            # times per frame; the glow itself only needs painting once.
            if self._small_at != (self._now, self.intensity):
                overlay_edge.paint(cairo.Context(self._small), cairo, sw, sh,
                                   EDGE_BAND * EDGE_LOWRES, self.intensity,
                                   self._now * EDGE_TIME)
                self._small.flush()
                self._small_at = (self._now, self.intensity)
            cr.save()
            cr.scale(w / sw, h / sh)
            cr.set_source_surface(self._small, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.rectangle(0, 0, sw, sh)
            cr.clip()
            cr.paint()
            cr.restore()
            return True

        def reconfigure(self, geometry):
            """Follow the display when it is rescaled or replugged."""
            self.geometry = geometry
            self.resize(geometry.width, geometry.height)
            self.move(geometry.x, geometry.y)
            self._small = None
            self._small_at = None

        def prewarm(self):
            """Render the glow once before anything is waiting for it."""
            w, h = self.geometry.width, self.geometry.height
            sw, sh = max(8, int(w * EDGE_LOWRES)), max(8, int(h * EDGE_LOWRES))
            self._small = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
            overlay_edge.paint(cairo.Context(self._small), cairo, sw, sh,
                               EDGE_BAND * EDGE_LOWRES, EDGE_ACTIVE, 0.0)
            self._small.flush()
            self._small_at = (0.0, EDGE_ACTIVE)

        def update(self, intensity, now):
            self.intensity, self._now = intensity, now
            # Only the band changes, and it is a fraction of the screen. A full
            # redraw would hand the compositor the whole display as a texture
            # on every frame; the middle stays transparent from the first one.
            w, h = self.geometry.width, self.geometry.height
            b = EDGE_BAND
            for area in ((0, 0, w, b), (0, h - b, w, b),
                         (0, b, b, h - 2 * b), (w - b, b, b, h - 2 * b)):
                self.queue_draw_area(*area)

    class OverlayWindow(Gtk.Window):
        def __init__(self):
            super().__init__(type=Gtk.WindowType.POPUP)
            self.set_app_paintable(True)
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
            visual = self.get_screen().get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)

            geometry = _monitor_geometry()
            self.scene = _Scene(max_height=int(geometry.height * MAX_H_FRACTION))
            self.edge = EdgeWindow(geometry)

            # The window is resized once per state change, never during the
            # size animation: a window big enough for the largest card would
            # hand the compositor several times the texture it needs on every
            # frame, which is what made the motion stutter.
            self.geometry = geometry
            self.canvas_w = MAX_W + 2 * MARGIN
            self.canvas_h = self.scene.max_h + 2 * MARGIN
            self.set_default_size(self.canvas_w, self.canvas_h)

            # Draw on the window itself rather than on a child: anything the
            # child does not cover is filled by the theme, which shows up as an
            # opaque box beside the capsule.
            self.area = self
            self.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
            self.connect("draw", self._on_draw)
            self.connect("scroll-event", self._on_scroll)

            self._ticking = None
            self._last_frame = None
            self._refresh = None        # measured display refresh interval
            self._counter = 0
            self._frames, self._reported, self._worst = 0, None, 0.0
            self._times = []
            self._edge_tick = -1
            self._input_rect = None
            self._fade = None           # (from, to, t0, then) - the edge light
            self._leaving = False       # waiting for the outline to undraw
            self._buf = None            # (key, surface, context) frame buffer
            self._visible = False

        def _count_frame(self, now):
            """With VOICESNIP_OVERLAY_FPS=1, report the rate actually drawn."""
            if not _REPORT_FPS:
                return
            self._frames += 1
            if self._reported is None:
                self._reported = now
            elif now - self._reported >= 1.0:
                times = sorted(self._times)
                median = times[len(times) // 2] * 1000 if times else 0.0
                print(f"overlay: {self._frames / (now - self._reported):.1f} fps painted, "
                      f"frame {median:.1f} ms median / {self._worst * 1000:.1f} ms worst",
                      flush=True)
                self._frames, self._reported, self._worst = 0, now, 0.0
                self._times = []

        def _now(self):
            """One clock for everything, shared with the frame callback."""
            clock = self.get_frame_clock()
            return clock.get_frame_time() / 1e6 if clock else time.monotonic()

        # -- drawing -------------------------------------------------------

        def _buffer(self):
            """The image the frame is painted into, reused between frames."""
            scale = self.get_scale_factor()
            want = (self.canvas_w, self.canvas_h, scale)
            if self._buf is None or self._buf[0] != want:
                surface = cairo.ImageSurface(
                    cairo.FORMAT_ARGB32,
                    max(1, int(math.ceil(self.canvas_w * scale))),
                    max(1, int(math.ceil(self.canvas_h * scale))))
                surface.set_device_scale(scale, scale)
                self._buf = (want, surface, cairo.Context(surface))
            return self._buf

        def _on_draw(self, _widget, cr):
            started = time.perf_counter() if _REPORT_FPS else 0
            self._count_frame(self._now())
            # Painted in memory and handed over as one finished image. Drawing
            # gradients, hairlines and text straight onto the X surface meant a
            # server round trip per operation, and cost three times as much.
            (_, _, scale), surface, bcr = self._buffer()
            bcr.save()
            bcr.set_operator(cairo.OPERATOR_SOURCE)
            bcr.set_source_rgba(0, 0, 0, 0)
            bcr.paint()
            bcr.restore()
            self.scene.paint(bcr, self.canvas_w, self.canvas_h, self._now(),
                             scale=scale)
            surface.flush()
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_surface(surface, 0, 0)
            cr.paint()
            if _REPORT_FPS:
                spent = time.perf_counter() - started
                self._worst = max(self._worst, spent)
                self._times.append(spent)
            return True

        def _on_scroll(self, _widget, event):
            ok, _dx, dy = event.get_scroll_deltas()
            if not ok:
                dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction, 0)
            self.scene.scroll_by(dy * SCROLL_STEP)
            self.area.queue_draw()
            return True

        def _update_input_region(self):
            rect = self.scene.capsule_rect(self.canvas_w)
            if rect == self._input_rect:
                return                  # reshaping the window every frame is not free
            self._input_rect = rect
            self.input_shape_combine_region(cairo.Region(cairo.RectangleInt(*rect)))

        # -- animation loop --------------------------------------------------

        def _ensure_ticking(self):
            if self._ticking is None:
                # The frame clock, not a timer: it fires in step with the
                # compositor's frames, so the motion lands evenly instead of
                # beating against the refresh rate.
                self._ticking = self.area.add_tick_callback(self._tick)

        def _tick(self, _widget, clock):
            now = clock.get_frame_time() / 1e6
            if self._last_frame is not None:
                gap = now - self._last_frame
                if gap > 0:
                    self._refresh = gap if self._refresh is None else \
                        self._refresh * 0.85 + gap * 0.15
            self._last_frame = now
            self._counter += 1
            target_ms = (PROCESSING_FRAME_MS if self.scene.mode == "processing"
                         else FRAME_MS)
            stride = 1
            if self._refresh:
                stride = max(1, int(round((target_ms / 1000) / self._refresh)))
            if self._counter % stride:
                return GLib.SOURCE_CONTINUE
            moving = self.scene.step(now)
            if self._fade:
                # Only the border light fades. The capsule has its own way in
                # and out - it draws itself on and undraws itself - so fading
                # the window as well would wash that out.
                start, end, t0, then = self._fade
                p = min(1.0, (now - t0) * 1000 / EDGE_FADE_MS)
                Gtk.Widget.set_opacity(self.edge, start + (end - start) * _ease_out(p))
                if p >= 1.0:
                    self._fade = None
                    if then:
                        then()
                moving = True
            if self._leaving and self._fade is None and self.scene.gone():
                self._leaving = False
                self._hide_windows()
            edge_tick = int(now * EDGE_FPS)
            if edge_tick != self._edge_tick:
                self._edge_tick = edge_tick
                self.edge.update(EDGE_IDLE if self.scene.mode == "text" else EDGE_ACTIVE, now)
            self._update_input_region()
            # The window is now sized to the capsule, so a full repaint is
            # cheap - and unlike a partial one it cannot leave a stale region
            # behind after a resize.
            self.area.queue_draw()
            if not moving:
                self.area.remove_tick_callback(self._ticking)
                self._ticking = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        # -- states ------------------------------------------------------------

        def _fit_window(self, appearing=False):
            """Size the window to the capsule it is about to hold.

            While it is on screen the window only ever grows: resizing an X
            window mid-interaction costs a round trip and a fresh surface, and
            the hitch is visible exactly when the state changes. It shrinks
            back the next time the overlay appears.
            """
            # Where the screen is, now - not where it was when VoiceSnip
            # started. Rescaling the display or replugging a monitor changes
            # this, and a stale copy puts the capsule wherever the screen used
            # to be and leaves it there for the rest of the session.
            geometry = _monitor_geometry()
            moved = (geometry.x, geometry.y, geometry.width, geometry.height) != \
                (self.geometry.x, self.geometry.y,
                 self.geometry.width, self.geometry.height)
            if moved:
                self.geometry = geometry
                self.scene.max_h = int(geometry.height * MAX_H_FRACTION)
                self.edge.reconfigure(geometry)

            target_w, target_h = self.scene.target_size()
            needed_w = int(math.ceil(target_w)) + 2 * MARGIN
            needed_h = int(math.ceil(target_h)) + 2 * MARGIN
            if not appearing and not moved and \
                    needed_w <= self.canvas_w and needed_h <= self.canvas_h:
                return
            self.canvas_w = max(needed_w, self.canvas_w if not appearing else 0)
            self.canvas_h = max(needed_h, self.canvas_h if not appearing else 0)
            self.resize(self.canvas_w, self.canvas_h)
            self.move(self.geometry.x + (self.geometry.width - self.canvas_w) // 2,
                      self.geometry.y + int(self.geometry.height * TOP_FRACTION) - MARGIN)
            self._input_rect = None

        def _prewarm(self):
            """Do the first frame's work while nothing is on screen.

            The first frame is much the most expensive one - it builds the
            grain tile and the font's layout - and it used to land in the
            middle of the opening animation. Every size the capsule passes
            through is painted, from the narrow token it opens out of to a
            full card, so nothing is left to allocate later.
            """
            gdk_window = self.get_window()
            if gdk_window is None:
                return
            surface = gdk_window.create_similar_surface(
                cairo.CONTENT_COLOR_ALPHA, self.canvas_w, self.canvas_h)
            cr = cairo.Context(surface)
            now = self._now()
            self.scene.set_content("listening", "", now)
            for w, h in ((OPEN_W, float(MIN_H)),
                         (float(MIN_H), float(MIN_H)),
                         (float(MAX_W), float(self.scene.max_h))):
                self.scene.w, self.scene.h = w, h
                self.scene.paint(cr, self.canvas_w, self.canvas_h, now,
                                 scale=self.get_scale_factor())
            surface.flush()
            # Place and size the window now, while it is still unmapped. It is
            # realized at the origin at its default size, and doing this at the
            # first show raced the map: the capsule appeared at the edge of the
            # screen for a frame and then jumped to the middle.
            self._fit_window(appearing=True)
            self.scene.mode = "hidden"
            self.scene._morph = None
            self.scene._trace = None
            self.scene.trace = 1.0
            self.edge.prewarm()

        def _show(self, mode, text=""):
            now = self._now()
            appearing = not self._visible
            self.scene.set_content(mode, text, now, from_collapsed=appearing)
            self._fit_window(appearing)
            if appearing:
                self._visible = True
                self._leaving = False
                Gtk.Widget.set_opacity(self, 1.0)
                Gtk.Widget.set_opacity(self.edge, 0.0)
                # Let X apply the geometry before the window is mapped,
                # so it is never composited at the wrong place.
                Gdk.Display.get_default().sync()
                self.show_all()
                self.edge.show_all()
                self._fade = (0.0, 1.0, now, None)
            self._ensure_ticking()

        def _hide(self):
            if not self._visible:
                return
            self._visible = False
            self._leaving = True
            now = self._now()
            # Undraw the outline the way it was drawn, in reverse.
            self.scene.collapse(now)
            self._fade = (Gtk.Widget.get_opacity(self.edge), 0.0, now, None)
            self._ensure_ticking()

        def _hide_windows(self):
            self.hide()
            self.edge.hide()

        def apply(self, command):
            if "level" in command:
                self.scene.set_level(command["level"], self._now())
            state = command.get("state")
            if state in ("listening", "processing"):
                self._show(state)
            elif state == "text":
                self._show("text", command.get("text", ""))
            elif state == "prewarm":
                self._prewarm()
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
        ("listening", "", 0.12, 0.0),      # quiet: only the breathing pulse
        ("listening", "", 0.55, 1.7),
        ("listening", "", 0.95, 3.4),      # speaking up
        ("processing", "", 0.0, 2.2),
        ("text", "Dobrý den, tohle je zkouška diktování.", 0.0, 0.0),
        ("text", "Dobrý den, tohle je zkouška diktování. Příliš žluťoučký kůň úpěl "
                 "ďábelské ódy a rovnou za ním šel ještě jeden, který už tak "
                 "žluťoučký nebyl.", 0.0, 0.0),
        ("text", long_text.strip(), 0.0, 0.0),
    ]
    heights = []
    for mode, text, _, _t in states:
        scene.set_content(mode, text, 0.0)
        heights.append(int(scene.target_size()[1]) + 2 * MARGIN)
    sheet_h = sum(heights)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_w * scale, sheet_h * scale)
    cr = cairo.Context(surface)
    cr.scale(scale, scale)
    sky = cairo.LinearGradient(0, 0, 0, sheet_h)
    # A neutral desktop, deliberately not blue: an azure capsule over a blue
    # wallpaper cannot be judged.
    sky.add_color_stop_rgb(0, 0.38, 0.37, 0.36)
    sky.add_color_stop_rgb(1, 0.82, 0.80, 0.78)
    cr.set_source(sky)
    cr.paint()

    offset = 0
    for (mode, text, level, when), height in zip(states, heights):
        scene.set_content(mode, text, 0.0)
        scene.w, scene.h = scene.target_size()
        scene._morph = None
        scene.level = scene.level_target = level
        scene.level_at = when
        cr.save()
        cr.translate(0, offset)
        scene.paint(cr, canvas_w, height, now=when, scale=scale)
        cr.restore()
        offset += height
    surface.write_to_png(path)
    print(path)


if __name__ == "__main__":
    if "--render" in sys.argv:
        _render(sys.argv[sys.argv.index("--render") + 1])
    else:
        _run()
