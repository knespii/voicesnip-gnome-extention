"""
The capsule's material: an azure bloom, a lit outline and a deep glass body.

This replaces the nebula that used to fill the pill. The look comes from a
reference the user supplied - a glass panel lit from within, with an intense
glow spilling out past a bright hairline border - restated in the overlay's
own azure.

Nothing here is a function of time, only of the capsule's size and two
scalars the caller supplies: how hard the bloom is burning, and how much of
the outline has been drawn. That is what lets the whole material be repainted
on every frame of a resize without a cache.

The bloom is built from the one fact that makes a rounded rectangle easy: its
distance field is exactly radial at the corners and exactly perpendicular
along the straight edges. Four radial gradients and four linear ones
reproduce it with no rings to band and no approximation at any size.
"""

import math

import cairo

# -- palette ----------------------------------------------------------------

AZURE = (0.22, 0.85, 0.85)     # the anchor: the overlay's existing cyan-teal
BLOOM_RGB = (0.16, 0.70, 1.00)  # the light that spills outside the border
RIM_HI = (0.88, 0.99, 1.00)    # the border, lit from above
RIM_LO = (0.16, 0.72, 0.96)    # ...and its underside
GLOW_RGB = (0.30, 0.88, 1.00)  # light bleeding inwards from the border

# The body is nearly opaque on purpose. There is no compositor blur behind
# this window, so translucency does not read as glass, it reads as a washed
# out grey over a bright wallpaper. The nebula used to paint an opaque ground
# of its own; without it these two alphas are the only thing holding the
# desktop back.
FILL_TOP = (13 / 255, 27 / 255, 36 / 255, 0.96)
FILL_BOTTOM = (5 / 255, 10 / 255, 15 / 255, 0.97)

# -- the bloom --------------------------------------------------------------

BLOOM_REACH = 52               # how far the light carries, in logical px.
                               # The window's MARGIN must be at least this.
BLOOM_PEAK = 0.45              # the one dial for how loud the whole effect is:
                               # it scales the tight core, not the wide halo,
                               # so turning it down dims the glow without
                               # shrinking it.
BLOOM_HALO = 0.40              # and this one is the halo's own reach

# Sampled where the curve is steep, sparse where it is flat. The worst linear
# interpolation error between neighbouring stops is under 2/255.
_BLOOM_STOPS = (0.0, 0.02, 0.05, 0.09, 0.14, 0.20, 0.28, 0.38, 0.50, 0.65,
                0.82, 1.0)
BLOOM_PAD = BLOOM_REACH + 4    # room the glow needs around the capsule; the
                               # overlay's MARGIN must be at least this
SLICE_STRIP = 16               # the cached glow's stretchable middle band
_SLICE = None                  # (scale, pad, radius) -> the cached glow

# -- the inner bleed --------------------------------------------------------

# Two parts with opposite freedoms. The core is bright but its depth is
# capped, because the waveform's crest comes within 9.3px of the border and
# the text starts at 13. The tail is free to reach deep because its alpha is
# inside the contrast budget: white text stays above 11:1 anywhere it lands.
CORE_ALPHA = 0.34
CORE_MIN, CORE_MAX, CORE_FRACTION = 1.5, 5.0, 0.075
TAIL_ALPHA = 0.10
TAIL_MIN, TAIL_MAX, TAIL_FRACTION = 10.0, 30.0, 0.32
UNDER_SCALE = 0.62             # the underside is lit more weakly than the top
FALLOFF = 2.2                  # how sharply either part decays inward

# -- the outline ------------------------------------------------------------

RIM_TOP_ALPHA = 0.88
RIM_BOTTOM_ALPHA = 0.72
SPEC_ALPHA = 0.55              # the specular highlight at the top-left corner
SPEC_RGB = (0.90, 0.99, 1.00)  # never neutral white: a stroke that loses its
                               # chroma reads as a drawn border, not as light
SPEC_MIN, SPEC_MAX = 36.0, 220.0

# -- grain ------------------------------------------------------------------

_GRAIN = None
_GRAIN_N = 96
GRAIN_ALPHA = 0.5              # Carried over from the nebula, which quietly
                               # did this job. The body's ramp is only eight
                               # quantisation steps wide; across a tall card
                               # that is a Mach band every 50px without it.


def _rounded_rect(cr, x, y, w, h, radius):
    """Append the capsule's outline to the current path.

    A sub-path, not a fresh one: the bloom's clip is this shape punched out
    of a rectangle, and starting a new path would throw the rectangle away.
    """
    r = max(0.0, min(radius, w * 0.5, h * 0.5))
    if r <= 0.0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def perimeter(w, h, r):
    """Length of the rounded rect's outline, for dashing it."""
    r = max(0.0, min(r, w * 0.5, h * 0.5))
    return 2.0 * (w - 2.0 * r) + 2.0 * (h - 2.0 * r) + 2.0 * math.pi * r


# ---------------------------------------------------------------------------
# The bloom
# ---------------------------------------------------------------------------

def _bloom_alpha(t):
    """Light at `t` of the way out from the border. Zero at the far end."""
    u = 1.0 - t
    return BLOOM_PEAK * u ** 4 + BLOOM_HALO * u * u


def _bloom_ramp(gradient, intensity):
    for t in _BLOOM_STOPS:
        gradient.add_color_stop_rgba(t, *BLOOM_RGB, _bloom_alpha(t) * intensity)


def _draw_bloom(cr, x, y, w, h, r, intensity=1.0):
    """The glow outside the capsule, drawn outright.

    Painted as the eight regions of the distance field: a radial ramp around
    each corner arc, a perpendicular ramp along each straight edge. They tile
    the band exactly, and because each is the true field there is nothing to
    band and no approximation at any size.

    Evaluating eight gradients over the whole band costs about four
    milliseconds, which is why this is what builds the cached slice rather
    than what runs every frame. It is still the direct path for a capsule too
    small to slice, where the band is small enough not to matter.
    """
    if intensity <= 0.003 or w <= 0.0 or h <= 0.0:
        return
    r = max(0.0, min(r, w * 0.5, h * 0.5))
    reach = BLOOM_REACH

    cr.save()
    # Everything inside the capsule is the body's business; the glow is only
    # ever outside it, or it would tint the material through its own alpha.
    cr.rectangle(x - reach, y - reach, w + 2 * reach, h + 2 * reach)
    _rounded_rect(cr, x, y, w, h, r)
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.clip()

    # The four corner arc centres, and the box corner each one owns.
    corners = (
        (x + r, y + r, x - reach, y - reach),
        (x + w - r, y + r, x + w - r, y - reach),
        (x + w - r, y + h - r, x + w - r, y + h - r),
        (x + r, y + h - r, x - reach, y + h - r),
    )
    side = r + reach
    for cx, cy, bx, by in corners:
        ring = cairo.RadialGradient(cx, cy, r, cx, cy, r + reach)
        _bloom_ramp(ring, intensity)
        cr.save()
        cr.rectangle(bx, by, side, side)
        cr.clip()
        cr.set_source(ring)
        cr.paint()
        cr.restore()

    # The four straight runs between them.
    edges = (
        (x + r, y - reach, w - 2 * r, reach, x + r, y, x + r, y - reach),
        (x + r, y + h, w - 2 * r, reach, x + r, y + h, x + r, y + h + reach),
        (x - reach, y + r, reach, h - 2 * r, x, y + r, x - reach, y + r),
        (x + w, y + r, reach, h - 2 * r, x + w, y + r, x + w + reach, y + r),
    )
    for bx, by, bw, bh, x0, y0, x1, y1 in edges:
        if bw <= 0.0 or bh <= 0.0:
            continue
        ramp = cairo.LinearGradient(x0, y0, x1, y1)
        _bloom_ramp(ramp, intensity)
        cr.save()
        cr.rectangle(bx, by, bw, bh)
        cr.clip()
        cr.set_source(ramp)
        cr.paint()
        cr.restore()
    cr.restore()


# ---------------------------------------------------------------------------
# The body and the light bleeding into it
# ---------------------------------------------------------------------------

def _bleed_depths(extent):
    core = min(CORE_MAX, max(CORE_MIN, CORE_FRACTION * extent))
    tail = min(TAIL_MAX, max(TAIL_MIN, TAIL_FRACTION * extent))
    return min(core, extent * 0.5), min(tail, extent * 0.5)


def _bleed_alpha(depth, core_d, tail_d, scale=1.0):
    """How much border light reaches `depth` px into the body."""
    a = 0.0
    if core_d > 0.0 and depth < core_d:
        a += CORE_ALPHA * (1.0 - depth / core_d) ** FALLOFF
    if tail_d > 0.0 and depth < tail_d:
        a += TAIL_ALPHA * (1.0 - depth / tail_d) ** FALLOFF
    return min(1.0, a * scale)


def paint_body(cr, x, y, w, h, r):
    """The deep glass body: one fill, top to bottom."""
    fill = cairo.LinearGradient(0, y, 0, y + h)
    fill.add_color_stop_rgba(0.0, *FILL_TOP)
    fill.add_color_stop_rgba(1.0, *FILL_BOTTOM)
    _rounded_rect(cr, x, y, w, h, r)
    cr.set_source(fill)
    cr.fill()


def _bleed_band(cr, x, y, w, h, r, horizontal):
    """Light leaning in from two opposite borders, over the body.

    Clipped to the two bands it actually reaches, so a tall card does not pay
    for a fill across its whole middle where the alpha is zero anyway.
    """
    extent = w if horizontal else h
    core_d, tail_d = _bleed_depths(extent)
    depth = max(core_d, tail_d)
    if depth <= 0.0:
        return
    span = max(1.0, extent)
    if horizontal:
        ramp = cairo.LinearGradient(x, 0, x + w, 0)
    else:
        ramp = cairo.LinearGradient(0, y, 0, y + h)
    under = 1.0 if horizontal else UNDER_SCALE
    stops = []
    for step in (0.0, 0.08, 0.18, 0.30, 0.45, 0.64, 0.82, 1.0):
        d = step * depth
        stops.append((min(0.5, d / span), _bleed_alpha(d, core_d, tail_d)))
        stops.append((max(0.5, 1.0 - d / span),
                      _bleed_alpha(d, core_d, tail_d, under)))
    # Cairo wants them in order, and a gradient built out of order paints
    # something that is not a gradient at all.
    for offset, alpha in sorted(stops):
        ramp.add_color_stop_rgba(offset, *GLOW_RGB, alpha)

    cr.save()
    if horizontal:
        cr.rectangle(x, y, depth, h)
        cr.rectangle(x + w - depth, y, depth, h)
    else:
        cr.rectangle(x, y, w, depth)
        cr.rectangle(x, y + h - depth, w, depth)
    cr.clip()
    _rounded_rect(cr, x, y, w, h, r)
    cr.set_source(ramp)
    cr.fill()
    cr.restore()


def paint_bleed(cr, x, y, w, h, r):
    """The border's light leaning into the body from all four sides."""
    _bleed_band(cr, x, y, w, h, r, horizontal=False)
    _bleed_band(cr, x, y, w, h, r, horizontal=True)


def paint_grain(cr, x, y, w, h, r):
    """A powdery tile over the finished body, to stop the ramp banding."""
    pattern = cairo.SurfacePattern(_grain())
    pattern.set_extend(cairo.EXTEND_REPEAT)
    cr.save()
    _rounded_rect(cr, x, y, w, h, r)
    cr.clip()
    cr.set_source(pattern)
    cr.paint_with_alpha(GRAIN_ALPHA)
    cr.restore()


# ---------------------------------------------------------------------------
# The outline
# ---------------------------------------------------------------------------

def rim_width(h, scale):
    """Snapped to whole device pixels: a fractional width shimmers as the
    capsule animates, which on a lit hairline is the first thing you see."""
    want = min(2.4, max(1.3, 1.0 + 0.0028 * h))
    return max(2.0, round(want * scale)) / scale


def paint_rim(cr, x, y, w, h, r, scale, drawn=1.0):
    """The lit border. `drawn` is how much of it has been traced, 0..1.

    The trace runs from the top-left corner clockwise. The path itself starts
    at the top-right, so the dash pattern is offset by the length of the top
    edge rather than rewriting the shared path helper.
    """
    if drawn <= 0.0 or w <= 0.0 or h <= 0.0:
        return
    line = rim_width(h, scale)
    inset = line / 2.0
    iw, ih = w - line, h - line
    if iw <= 0.0 or ih <= 0.0:
        return
    ir = max(0.0, min(r - inset, iw * 0.5, ih * 0.5))
    _rounded_rect(cr, x + inset, y + inset, iw, ih, ir)

    if drawn < 1.0:
        total = perimeter(iw, ih, ir)
        cr.set_dash([drawn * total, (1.0 - drawn) * total],
                    (iw - 2.0 * ir) % total)

    ramp = cairo.LinearGradient(0, y, 0, y + h)
    ramp.add_color_stop_rgba(0.0, *RIM_HI, RIM_TOP_ALPHA)
    ramp.add_color_stop_rgba(0.35, 0.52, 0.93, 0.99, 0.80)
    ramp.add_color_stop_rgba(1.0, *RIM_LO, RIM_BOTTOM_ALPHA)
    cr.set_line_width(line)
    cr.set_source(ramp)
    cr.stroke_preserve()

    # A specular pass on the same path: brightest where a light above and to
    # the left would strike the corner, decaying within a quarter of the
    # perimeter so it reads as a highlight and not as a white outline.
    spec_r = min(SPEC_MAX, max(SPEC_MIN, 0.5 * w))
    spec = cairo.RadialGradient(x + 0.55 * r, y + 0.55 * r, 0.0,
                                x + 0.55 * r, y + 0.55 * r, spec_r)
    spec.add_color_stop_rgba(0.0, *SPEC_RGB, SPEC_ALPHA)
    spec.add_color_stop_rgba(0.22, *SPEC_RGB, SPEC_ALPHA * 0.62)
    spec.add_color_stop_rgba(0.55, 0.80, 0.97, 1.00, SPEC_ALPHA * 0.18)
    spec.add_color_stop_rgba(1.0, 0.70, 0.95, 1.00, 0.0)
    cr.set_source(spec)
    cr.stroke()
    cr.set_dash([])


# ---------------------------------------------------------------------------

def _hash2(i, j):
    """Deterministic integer hash -> 0..1.  Stands in for noise; no RNG."""
    n = (i * 374761393 + j * 668265263) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    n ^= n >> 16
    return (n & 0xFFFF) / 65535.0


def _grain():
    """A small tileable ARGB32 grain surface, built once and cached."""
    global _GRAIN
    if _GRAIN is not None:
        return _GRAIN
    n = _GRAIN_N
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, n, n)
    stride = surf.get_stride()
    buf = surf.get_data()
    for j in range(n):
        row = j * stride
        for i in range(n):
            # two octaves so the grain is not a flat hiss
            d = (_hash2(i, j) - 0.5) * 0.72 + (_hash2(i >> 1, j >> 1) - 0.5) * 0.28
            a = int(abs(d) * 2.0 * 16.0)          # peak alpha ~ 16/255
            if a > 255:
                a = 255
            val = a if d > 0.0 else 0             # premultiplied white / black
            o = row + i * 4
            buf[o] = val        # B
            buf[o + 1] = val    # G
            buf[o + 2] = val    # R
            buf[o + 3] = a      # A
    surf.mark_dirty()
    _GRAIN = surf
    return _GRAIN


# ---------------------------------------------------------------------------
# The same glow, cached and stretched
# ---------------------------------------------------------------------------

def _bloom_slice(scale, pad, radius):
    """The glow rendered once, for any size to reuse.

    Along a capsule's straight edges the glow has the same profile
    everywhere, so fixed corners and a stretchable middle reproduce every
    size exactly. How hard the bloom is burning stays a blit-time scalar, so
    breathing never invalidates it.
    """
    global _SLICE
    key = (scale, pad, radius)
    if _SLICE and _SLICE[0] == key:
        return _SLICE[1]
    side = 2.0 * (pad + radius) + SLICE_STRIP
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32,
        max(1, int(math.ceil(side * scale))), max(1, int(math.ceil(side * scale))))
    surface.set_device_scale(scale, scale)
    inner = 2.0 * radius + SLICE_STRIP
    _draw_bloom(cairo.Context(surface), pad, pad, inner, inner, radius)
    surface.flush()
    _SLICE = (key, surface)
    return surface


def sliceable(w, h, radius):
    """Whether the cached glow's fixed corners fit this capsule."""
    return w >= 2.0 * radius and h >= 2.0 * radius


def paint_bloom(cr, x, y, w, h, r, scale, pad, intensity=1.0):
    """The glow outside the capsule, at `intensity` of full strength."""
    if intensity <= 0.003 or w <= 0.0 or h <= 0.0:
        return
    if not sliceable(w, h, r):
        _draw_bloom(cr, x, y, w, h, r, intensity)
        return

    source = _bloom_slice(scale, pad, r)
    corner = pad + r
    box_x, box_y = x - pad, y - pad
    box_w, box_h = w + 2.0 * pad, h + 2.0 * pad
    # The middle bands are sampled from inside the strip, clear of the seams.
    strip, inset = SLICE_STRIP / 2.0, SLICE_STRIP / 4.0
    cols = ((0.0, corner, box_x, corner),
            (corner + inset, strip, box_x + corner, box_w - 2.0 * corner),
            (corner + SLICE_STRIP, corner, box_x + box_w - corner, corner))
    rows = ((0.0, corner, box_y, corner),
            (corner + inset, strip, box_y + corner, box_h - 2.0 * corner),
            (corner + SLICE_STRIP, corner, box_y + box_h - corner, corner))
    for col, (src_x, src_w, dst_x, dst_w) in enumerate(cols):
        for row, (src_y, src_h, dst_y, dst_h) in enumerate(rows):
            if dst_w <= 0.0 or dst_h <= 0.0:
                continue
            if col == 1 and row == 1:
                continue        # the middle is the capsule itself: all zero,
                                # and on a big card most of the blit
            cr.save()
            cr.rectangle(dst_x, dst_y, dst_w, dst_h)
            cr.clip()
            cr.translate(dst_x, dst_y)
            cr.scale(dst_w / src_w, dst_h / src_h)
            cr.set_source_surface(source, -src_x, -src_y)
            # Nearest, not bilinear: the corners land 1:1 on the device grid
            # and the middle bands are constant along the direction they are
            # stretched in, so there is nothing for a filter to interpolate -
            # and at forty times' stretch interpolating it is not cheap.
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.get_source().set_extend(cairo.EXTEND_PAD)
            cr.paint_with_alpha(intensity)
            cr.restore()
