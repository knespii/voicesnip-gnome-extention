"""
Screen-edge glow for the overlay.

While VoiceSnip is listening or transcribing, a soft band of light hugs the
edge of the screen in the same violet-teal palette as the capsule, the way
Apple lights the display border during a Siri interaction.

It is painted into a small surface and scaled up by the caller - at a twelfth
of the resolution a soft glow is indistinguishable, and it is the only way a
full-screen effect can be redrawn every frame at a sensible cost.

The middle is carved out with a ramp of rounded rectangles rather than one
hard rectangle, so the band fades inwards instead of ending on a line, and it
follows the screen's rounded corners.
"""

import math

# The capsule's palette, so the border and the pill read as one thing.
COLOURS = (
    (0.62, 0.42, 0.95),        # violet
    (0.22, 0.85, 0.85),        # teal
    (0.26, 0.52, 1.00),        # blue
    (0.45, 0.95, 0.80),        # mint
)
SPEEDS = (0.21, -0.15, 0.13, -0.10)
PHASES = (0.0, 2.3, 4.1, 5.5)

CORNER = 0.055                 # of the shorter side; matches a laptop's screen
FALLOFF_STEPS = 16             # how smoothly the band fades towards the middle


def _rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def paint(cr, cairo, w, h, band, intensity, t):
    """Paint the glow across w x h, reaching `band` units in from the edge.

    Everything is in the units of whatever surface `cr` draws on, so the
    caller decides the resolution.
    """
    if intensity <= 0.002 or band <= 0:
        return

    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    # Blobs drifting around the perimeter. They are wider than the screen, so
    # what shows through the band is a slow wash of colour rather than
    # recognisable shapes.
    reach = max(w, h) * 0.85
    for colour, speed, phase in zip(COLOURS, SPEEDS, PHASES):
        a = t * speed + phase
        cx = w * (0.5 + 0.62 * math.sin(a))
        cy = h * (0.5 + 0.62 * math.cos(a * 0.83 + phase))
        blob = cairo.RadialGradient(cx, cy, 0, cx, cy, reach)
        blob.add_color_stop_rgba(0.0, *colour, 0.62 * intensity)
        blob.add_color_stop_rgba(0.55, *colour, 0.22 * intensity)
        blob.add_color_stop_rgba(1.0, *colour, 0.0)
        cr.set_source(blob)
        cr.paint()

    # Carve the middle away, softly. Nested rectangles from the edge inwards,
    # each removing the same fraction: a point one step in is covered by one
    # rectangle, a point at the inner boundary by all of them, so the glow
    # fades out smoothly instead of ending on a line.
    corner = min(w, h) * CORNER
    step = 1 - 0.005 ** (1.0 / FALLOFF_STEPS)
    cr.set_operator(cairo.OPERATOR_DEST_OUT)
    for i in range(1, FALLOFF_STEPS + 1):
        inset = band * i / FALLOFF_STEPS
        _rounded_rect(cr, inset, inset, w - 2 * inset, h - 2 * inset,
                      max(0.0, corner - inset))
        cr.set_source_rgba(0, 0, 0, step)
        cr.fill()
    cr.set_operator(cairo.OPERATOR_OVER)
