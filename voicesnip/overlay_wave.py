"""
Spectral waveform for the overlay capsule.

A bank of frequency bins summed in quadrature, drawn as a mirrored ribbon with
five hairline strands weaving inside it: flat tails at both ends swelling into
pointed lobes through the middle, white on the left shading to teal on the
right. `level` drives amplitude and density, `t` animates it.

Kept apart from overlay.py only because of its length; it is pure cairo and has
no state beyond memoised gradients.
"""

import math

import cairo

TAU = 2.0 * math.pi

# ---------------------------------------------------------------- parameters

_BINS = 6

# The finest oscillation must stay this many pixels wide. The painter was
# written for a macro view; packed into a 92 px box its top bins land a full
# cycle every two or three pixels, which does not read as a waveform at all -
# it crawls and glitters like an anthill.
_MIN_PERIOD_PX = 22.0

# (amplitude scale, phase rotation in radians, alpha) for the interior strands.
# No interior strands. A bundle of phase-rotated copies is what the concept
# art shows, but that is a macro view; at 92 px the strands weave into a
# crawling lattice. The ribbon alone reads as a waveform and stays calm.
_STRANDS = ()

# Horizontal white -> teal ramp, taken off the concept art.
_RAMP = (
    (0.00, 1.000, 1.000, 1.000),
    (0.34, 0.960, 0.996, 0.994),
    (0.62, 0.790, 0.975, 0.950),
    (0.86, 0.560, 0.925, 0.888),
    (1.00, 0.430, 0.885, 0.856),
)

# Bloom passes around the ribbon boundary: (width in hairlines, alpha).
_BLOOM = ((2.2, 0.105),)                # one pass: the budget buys frame rate instead
_BLOOM_MAX = 2.2

# Ceiling the AGC may pass through, relative to the local average.
_CEIL = 1.08


# ---------------------------------------------------------------- helpers

def _hash01(i):
    """Deterministic pseudo-noise in [0, 1) from an integer index."""
    s = math.sin(i * 127.1 + 311.7) * 43758.5453123
    return s - math.floor(s)


def _smoothstep(e0, e1, v):
    if e1 <= e0:
        return 0.0 if v < e0 else 1.0
    u = (v - e0) / (e1 - e0)
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    return u * u * (3.0 - 2.0 * u)


def _gradient(x, w, alpha, cache):
    """White -> teal ramp at the given alpha, memoised for this call only."""
    g = cache.get(alpha)
    if g is None:
        g = cairo.LinearGradient(x, 0.0, x + w, 0.0)
        for stop, r, gg, b in _RAMP:
            g.add_color_stop_rgba(stop, r, gg, b, alpha)
        cache[alpha] = g
    return g


# ---------------------------------------------------------------- the painter

def paint_wave(cr, x, y, w, h, level, t, density=None):
    """Draw the waveform inside the box (x, y, w, h).

    level: 0.0 silence .. 1.0 loud - drives amplitude and density.
    t: seconds, animates the oscillation."""
    if w <= 2.0 or h <= 2.0:
        return
    level = 0.0 if level < 0.0 else (1.0 if level > 1.0 else level)
    # Which bins are awake, and how wide the packet is, follow `density`. Tie
    # them to the instantaneous level instead and the whole shape reshuffles
    # with every syllable, which reads as jumping rather than as a voice.
    density = level if density is None else density
    density = 0.0 if density < 0.0 else (1.0 if density > 1.0 else density)

    yc = y + h * 0.5
    hair = max(0.95, h * 0.034)          # thin, but not sub-pixel: below one
                                         # pixel the strands shimmer as they move
    floor = max(0.55, h * 0.022)         # minimum ribbon half-thickness

    # Reserve room for the ribbon floor and for the widest bloom pass, so
    # nothing -- geometry or glow -- can escape the box at level 1.0.
    amp_max = h * 0.5 - (floor + hair * _BLOOM_MAX * 0.5 + 0.35)
    if amp_max < 1.0:
        amp_max = h * 0.25

    # ---- spectral bank -------------------------------------------------
    # Bins fade in with `level`: silence is a slow low-order swell, loud
    # speech brings in the fine micro-oscillation.
    freqs = []
    amps = []
    phases = []
    for k in range(_BINS):
        n1 = _hash01(k * 3 + 1)
        n2 = _hash01(k * 3 + 2)
        n3 = _hash01(k * 3 + 3)

        # Spread the bank between a readable low order and whatever the box
        # can actually show, instead of a fixed ladder.
        f_top = max(6.0, w / _MIN_PERIOD_PX)
        f = 1.70 + (f_top - 1.70) * ((k + 0.65 * n1) / (_BINS - 1.0))

        gate = _smoothstep(0.0, 0.34, density * 1.34 + 0.17 - k / (_BINS - 1.0))
        if gate <= 0.0:
            continue

        # Speech-like band: energy peaks near 5 cycles (the readable
        # oscillation) and rolls off steeply into the fine micro-detail.
        a = gate / (1.0 + (f / 5.5) ** 2.0)
        a *= _smoothstep(1.1, 4.8, f)
        a *= 0.52 + 0.48 * n2                 # per-bin colour
        a *= 0.62 + 0.38 * math.sin(t * (0.33 + 0.85 * n3) + n1 * TAU)

        freqs.append(TAU * f)
        amps.append(a)
        phases.append(n1 * TAU + t * (1.05 + 0.052 * f + 1.25 * n2))

    # ---- sum the bank in quadrature ------------------------------------
    # 1.5 samples per pixel: at hairline widths the extra points the
    # preview used were invisible and this runs every frame.
    n = int(w * 1.2)
    if n < 72:
        n = 72
    elif n > 300:
        n = 130

    sig_i = [0.0] * (n + 1)
    sig_q = [0.0] * (n + 1)
    for k in range(len(amps)):
        a = amps[k]
        # Advance the phasor by a fixed rotation per sample instead of
        # calling sin/cos for every bin at every sample.
        d = freqs[k] / n
        cd = math.cos(d)
        sd = math.sin(d)
        sv = math.sin(phases[k])
        cv = math.cos(phases[k])
        for i in range(n + 1):
            sig_i[i] += a * sv
            sig_q[i] += a * cv
            sv, cv = sv * cd + cv * sd, cv * cd - sv * sd

    # ---- designed fusiform envelope ------------------------------------
    centre = 0.5 + 0.055 * math.sin(t * 0.62) + 0.028 * math.sin(t * 1.13 + 1.7)
    sigma = 0.215 + 0.060 * (1.0 - density)
    inv2s2 = 1.0 / (2.0 * sigma * sigma)
    amp_scale = amp_max * (0.085 + 0.915 * level ** 1.12)

    xs = [0.0] * (n + 1)
    envs = [0.0] * (n + 1)
    brds = [0.0] * (n + 1)   # rim taper alone, sets the tail thickness
    ana = [0.0] * (n + 1)    # analytic envelope
    peak_ana = 1e-9
    for i in range(n + 1):
        u = i / n
        # Broad support with a quick taper at each rim, so the oscillation
        # fills most of the box instead of bunching into the middle.
        broad = _smoothstep(0.0, 0.115, u) * _smoothstep(0.0, 0.115, 1.0 - u)
        # Syllabic swell: slow drifting undulation, so lobe heights vary the
        # way speech does instead of marching along at one height.
        syl = 0.78 + 0.22 * math.sin(TAU * 1.35 * u + 0.9 + t * 0.55)
        syl *= 0.87 + 0.13 * math.sin(TAU * 2.70 * u - 2.1 - t * 0.37)
        d = u - centre

        xs[i] = x + u * w
        brds[i] = broad
        envs[i] = broad * syl * (0.40 + 0.60 * math.exp(-d * d * inv2s2))

        si = sig_i[i]
        qi = sig_q[i]
        m = math.sqrt(si * si + qi * qi)
        ana[i] = m
        if m > peak_ana:
            peak_ana = m

    # ---- AGC, then scale to the box ------------------------------------
    # Random-phase bins beat against each other, so the analytic envelope is
    # spiky.  Dividing by a smoothed copy evens the oscillation out; a box
    # filter over a running sum keeps that cheap.
    r = n // 14
    if r < 2:
        r = 2
    acc = [0.0]
    run = 0.0
    for v in ana:
        run += v
        acc.append(run)

    reach = 1e-9
    for i in range(n + 1):
        lo = i - r
        hi = i + r + 1
        if lo < 0:
            lo = 0
        if hi > n + 1:
            hi = n + 1
        den = 0.16 * peak_ana + 0.84 * (acc[hi] - acc[lo]) / (hi - lo)
        if den < 1e-9:
            den = 1e-9
        # Limit what the AGC lets through, so one loud beat cannot set the
        # scale for the whole box and squash everything either side of it.
        ratio = ana[i] / den
        if ratio > _CEIL:
            den *= ratio / _CEIL
            ratio = _CEIL
        sig_i[i] /= den
        sig_q[i] /= den
        # `ratio` bounds every rotation at this sample, and the expansion
        # below is monotonic in |v|, so pushing the bound through it gives a
        # true ceiling for every strand.
        v = ratio * (0.80 + 0.20 * ratio * ratio) * envs[i]
        if v > reach:
            reach = v

    # The tallest excursion any strand can reach now lands exactly on the
    # padding line: full use of the box, guaranteed never to leave it.
    env_k = amp_scale / reach
    for i in range(n + 1):
        envs[i] *= env_k

    def offsets(scale, rot):
        """Offsets for one phase-rotated, amplitude-scaled copy."""
        c = math.cos(rot) * scale
        s = math.sin(rot) * scale
        out = [0.0] * (n + 1)
        for i in range(n + 1):
            v = c * sig_i[i] + s * sig_q[i]
            # Mild expansion: shoulders pull in, crests stay put, which
            # sharpens each lobe into the spindle silhouette.
            out[i] = v * (0.80 + 0.20 * v * v) * envs[i]
        return out

    outer = offsets(1.0, 0.0)

    # The filled body is the rectified outline plus a thin floor, so the
    # bundle reads as one continuous luminous ribbon rather than a string of
    # beads pinched off at every zero crossing.
    body = [0.0] * (n + 1)
    for i in range(n + 1):
        v = outer[i]
        body[i] = (v if v >= 0.0 else -v) + floor * brds[i]

    # ---- paint ---------------------------------------------------------
    cache = {}
    cr.save()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)

    # Flat tails: the ribbon tapers away at the rims, and a hairline baseline
    # carries it out to the edge of the box as in the concept.
    cr.move_to(x + 0.8, yc)
    cr.line_to(x + w - 0.8, yc)
    cr.set_line_width(max(0.7, h * 0.028))
    cr.set_source(_gradient(x, w, 0.50, cache))
    cr.stroke()

    # Mirrored fill.
    cr.move_to(xs[0], yc - body[0])
    for i in range(1, n + 1):
        cr.line_to(xs[i], yc - body[i])
    for i in range(n, -1, -1):
        cr.line_to(xs[i], yc + body[i])
    cr.close_path()
    cr.set_source(_gradient(x, w, 0.10 + 0.07 * level, cache))
    cr.fill()

    # Interior contour strands.
    cr.set_line_width(hair)
    for scale, rot, alpha in _STRANDS:
        vals = offsets(scale, rot)
        src = _gradient(x, w, alpha, cache)
        for sign in (-1.0, 1.0):
            cr.move_to(xs[0], yc + sign * vals[0])
            for i in range(1, n + 1):
                cr.line_to(xs[i], yc + sign * vals[i])
            cr.set_source(src)
            cr.stroke()

    # Ribbon boundary, with a soft bloom.
    for sign in (-1.0, 1.0):
        cr.move_to(xs[0], yc + sign * body[0])
        for i in range(1, n + 1):
            cr.line_to(xs[i], yc + sign * body[i])
        for mult, alpha in _BLOOM:
            cr.set_line_width(hair * mult)
            cr.set_source(_gradient(x, w, alpha, cache))
            cr.stroke_preserve()
        cr.set_line_width(hair * 1.08)
        cr.set_source(_gradient(x, w, 0.92, cache))
        cr.stroke()

    cr.restore()


# ---------------------------------------------------------------- preview
