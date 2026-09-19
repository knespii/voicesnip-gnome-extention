"""
Layout-aware keystroke synthesis for Wayland.

ydotool emits raw evdev keycodes and the compositor turns them into characters
using whatever XKB layout is currently active. Typing "ě" can therefore not be
done by guessing US key positions - it needs to know which key produces "ě" on
*this* user's layout.

This module inverts the active keymap. It compiles the layout with
libxkbcommon (through ctypes; libxkbcommon is present on every Wayland
session, no extra package) and builds a character -> (keycode, modifiers)
table from it. Characters that no single key produces are composed from a dead
key plus a base letter, found via Unicode NFD decomposition: "Č" decomposes to
"C" + U+030C, U+030C is produced by dead_caron, so the sequence becomes
dead_caron followed by Shift+C.

On a Czech layout that covers the whole alphabet: ěščřžýáíé sit on the number
row and ú/ů on the home row, while ďťňó and every uppercase accented letter
come from the dead keys on the '=' and ';' keys.

Dead key composition is resolved by the target application's input method
(GTK, Qt and VTE all do it), the same way it works when typing by hand.

Standalone use:
    python3 -m voicesnip.xkb_typer "Příliš žluťoučký kůň úpěl ďábelské ódy"
    python3 -m voicesnip.xkb_typer --dry-run "text"    # show the plan only
"""

import ast
import ctypes
import ctypes.util
import os
import subprocess
import unicodedata

# An evdev keycode is the XKB keycode minus 8.
XKB_KEYCODE_OFFSET = 8

XKB_KEY_UP, XKB_KEY_DOWN = 0, 1
XKB_STATE_MODS_EFFECTIVE = 1 << 3

# Modifiers we refuse to use: reaching a level by toggling a lock would leave
# Caps Lock on afterwards.
_LOCK_MOD_NAMES = frozenset({"Lock", "NumLock", "ScrollLock"})

# Preferred physical modifier keys, tried in this order when several keys set
# the same modifier (on a Czech layout both KEY_RIGHTALT and the <LVL3> alias
# give AltGr; the real AltGr key is the better choice).
_PREFERRED_MOD_KEYCODES = (42, 29, 56, 100, 54, 97, 125, 126)

# Above this, keycodes are multimedia/vendor keys rather than the ordinary PC
# keyboard block. Some layouts put a character there as well (a dedicated Euro
# key next to AltGr+E); the ordinary key is the safer choice.
_STANDARD_KEYCODE_MAX = 127

# Characters that are keys, not printable keysyms.
_KEYCODE_OVERRIDES = {"\n": 28, "\t": 15}  # KEY_ENTER, KEY_TAB

# Unicode combining marks -> the XKB dead keysym that emits them.
_DEAD_KEYSYM_FOR_MARK = {
    "\u0300": "dead_grave",        # à
    "\u0301": "dead_acute",        # á  ó
    "\u0302": "dead_circumflex",   # â
    "\u0303": "dead_tilde",        # ã
    "\u0304": "dead_macron",       # ā
    "\u0306": "dead_breve",        # ă
    "\u0307": "dead_abovedot",     # ż
    "\u0308": "dead_diaeresis",    # ä
    "\u0309": "dead_hook",         # ả
    "\u030a": "dead_abovering",    # ů
    "\u030b": "dead_doubleacute",  # ő
    "\u030c": "dead_caron",        # č  ď  ť  ň
    "\u031b": "dead_horn",         # ơ
    "\u0323": "dead_belowdot",     # ạ
    "\u0327": "dead_cedilla",      # ç
    "\u0328": "dead_ogonek",       # ą
}


# ---------------------------------------------------------------------------
# libxkbcommon via ctypes
# ---------------------------------------------------------------------------

def _load_libxkbcommon():
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    lib = ctypes.CDLL(name)
    u32, vp, sz = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t
    sig = {
        "xkb_context_new": (vp, [ctypes.c_int]),
        "xkb_context_unref": (None, [vp]),
        "xkb_keymap_new_from_names": (vp, [vp, vp, ctypes.c_int]),
        "xkb_keymap_unref": (None, [vp]),
        "xkb_keymap_min_keycode": (u32, [vp]),
        "xkb_keymap_max_keycode": (u32, [vp]),
        "xkb_keymap_num_levels_for_key": (u32, [vp, u32, u32]),
        "xkb_keymap_key_get_syms_by_level": (
            ctypes.c_int, [vp, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]),
        "xkb_keymap_key_get_mods_for_level": (sz, [vp, u32, u32, u32, ctypes.POINTER(u32), sz]),
        "xkb_keymap_num_mods": (u32, [vp]),
        "xkb_state_new": (vp, [vp]),
        "xkb_state_unref": (None, [vp]),
        "xkb_state_update_key": (ctypes.c_int, [vp, u32, ctypes.c_int]),
        "xkb_state_serialize_mods": (u32, [vp, ctypes.c_int]),
        "xkb_keymap_mod_get_name": (ctypes.c_char_p, [vp, u32]),
        "xkb_keysym_to_utf8": (ctypes.c_int, [u32, ctypes.c_char_p, sz]),
        "xkb_keysym_get_name": (ctypes.c_int, [u32, ctypes.c_char_p, sz]),
    }
    for fname, (restype, argtypes) in sig.items():
        fn = getattr(lib, fname)
        fn.restype = restype
        fn.argtypes = argtypes
    return lib


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


# ---------------------------------------------------------------------------
# Which layout is active
# ---------------------------------------------------------------------------

def _gsettings(key):
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.input-sources", key],
            capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    # gsettings prints GVariant syntax: "uint32 0", "@as []", "[('xkb', 'cz')]".
    for prefix in ("uint32 ", "@as ", "@a(ss) "):
        if value.startswith(prefix):
            value = value[len(prefix):]
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None


def _gnome_active_source():
    """Active GNOME input source as (layout, variant), or None."""
    sources = _gsettings("sources")
    if not sources:
        return None
    index = _gsettings("current")
    if not isinstance(index, int) or index >= len(sources):
        index = 0
    kind, name = sources[index]
    if kind != "xkb":
        # An IBus engine (pinyin, mozc, ...) is not an XKB layout; we cannot
        # drive it with keycodes.
        return None
    layout, _, variant = name.partition("+")
    return layout, (variant or None)


def _setxkbmap_source():
    """Active layout as (layout, variant) from setxkbmap, or None."""
    try:
        out = subprocess.run(["setxkbmap", "-query"], capture_output=True,
                             text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    if not found.get("layout"):
        return None
    return found["layout"], (found.get("variant") or None)


def detect_rmlvo():
    """Return the (rules, model, layout, variant, options) of the active layout."""
    source = _gnome_active_source() or _setxkbmap_source()
    if source:
        layout, variant = source
    else:
        layout = os.environ.get("XKB_DEFAULT_LAYOUT") or "us"
        variant = os.environ.get("XKB_DEFAULT_VARIANT")

    options = _gsettings("xkb-options")
    options = ",".join(options) if options else os.environ.get("XKB_DEFAULT_OPTIONS")

    return (
        os.environ.get("XKB_DEFAULT_RULES") or "evdev",
        os.environ.get("XKB_DEFAULT_MODEL") or "pc105",
        layout,
        variant,
        options or None,
    )


# ---------------------------------------------------------------------------
# Reverse keymap
# ---------------------------------------------------------------------------

class LayoutTyper:
    """Turns text into evdev keystrokes for the currently active XKB layout."""

    def __init__(self, rmlvo=None):
        self.rules, self.model, self.layout, self.variant, self.options = (
            rmlvo or detect_rmlvo()
        )
        self._lib = _load_libxkbcommon()
        # character / dead keysym -> (cost, ((keycode, pressed), ...)). The cost
        # is kept so a cheaper route to the same character found later in the
        # scan can replace an expensive one; see _offer().
        self._chars = {}
        self._deads = {}
        self._lock_mask = 0        # modifier bits we refuse to touch
        self._mod_keys = []        # [(keycode, mask)] discovered from the keymap
        self._build()

    # -- description -------------------------------------------------------

    @property
    def layout_label(self):
        return f"{self.layout}+{self.variant}" if self.variant else self.layout

    @property
    def direct_characters(self):
        """How many characters sit on a key of their own."""
        return len(self._chars)

    @property
    def dead_keys(self):
        """How many dead keys this layout offers for composing the rest."""
        return len(self._deads)

    # -- keymap compilation ------------------------------------------------

    def _build(self):
        lib = self._lib
        ctx = lib.xkb_context_new(0)
        if not ctx:
            raise RuntimeError("xkb_context_new failed")
        try:
            def enc(value):
                return value.encode("utf-8") if value else None

            names = _RuleNames(enc(self.rules), enc(self.model), enc(self.layout),
                               enc(self.variant), enc(self.options))
            keymap = lib.xkb_keymap_new_from_names(
                ctx, ctypes.cast(ctypes.pointer(names), ctypes.c_void_p), 0)
            if not keymap:
                raise RuntimeError(f"could not compile XKB layout '{self.layout_label}'")
            try:
                self._scan_keymap(keymap)
            finally:
                lib.xkb_keymap_unref(keymap)
        finally:
            lib.xkb_context_unref(ctx)

        # Keys that carry no printable keysym but are needed for plain text.
        for char, keycode in _KEYCODE_OVERRIDES.items():
            self._chars[char] = ((False, 0), ((keycode, 1), (keycode, 0)))

    def _scan_keymap(self, keymap):
        lib = self._lib
        mod_names = [lib.xkb_keymap_mod_get_name(keymap, i).decode("utf-8", "replace")
                     for i in range(lib.xkb_keymap_num_mods(keymap))]
        self._lock_mask = sum(1 << i for i, name in enumerate(mod_names)
                              if name in _LOCK_MOD_NAMES)
        self._mod_keys = self._discover_modifier_keys(keymap)

        for xkb_keycode in range(lib.xkb_keymap_min_keycode(keymap),
                                 lib.xkb_keymap_max_keycode(keymap) + 1):
            keycode = xkb_keycode - XKB_KEYCODE_OFFSET
            if keycode <= 0:
                continue
            for level in range(lib.xkb_keymap_num_levels_for_key(keymap, xkb_keycode, 0)):
                syms = ctypes.POINTER(ctypes.c_uint32)()
                if lib.xkb_keymap_key_get_syms_by_level(
                        keymap, xkb_keycode, 0, level, ctypes.byref(syms)) != 1:
                    # Zero or several keysyms on one level: not a plain character.
                    continue
                keysym = syms[0]
                name = self._keysym_name(keysym)
                if name.startswith("KP_"):
                    # The keypad only yields digits while NumLock is on.
                    continue
                modifiers = self._pressable_modifiers(keymap, xkb_keycode, level)
                if modifiers is None:
                    continue
                cost = (keycode > _STANDARD_KEYCODE_MAX, len(modifiers))
                entry = (cost, self._sequence(keycode, modifiers))
                if name.startswith("dead_"):
                    self._offer(self._deads, name, entry)
                    continue
                text = self._keysym_text(keysym)
                if len(text) == 1 and text.isprintable():
                    self._offer(self._chars, text, entry)

    @staticmethod
    def _offer(mapping, key, entry):
        """Keep the cheapest way to produce `key`.

        Several keys can carry the same character (on a Czech layout "!" is
        both AltGr+'+' and Shift+'§'). An ordinary keyboard key beats a
        multimedia one, then fewest modifiers wins - shorter to type and less
        likely to collide with an application shortcut. Ties keep the first
        hit, so the scan order (low keycode, low level first) decides.
        """
        current = mapping.get(key)
        if current is None or entry[0] < current[0]:
            mapping[key] = entry

    def _discover_modifier_keys(self, keymap):
        """Which key sets which modifier - measured, not assumed.

        AltGr is KEY_RIGHTALT on a Czech layout but plain Alt on a US one, so
        the keycode cannot be hardcoded. Press every key in a scratch XKB state
        and record the modifier mask it actually produces.

        Returns:
            [(keycode, mask)] sorted so the cheapest, most standard key for a
            given modifier comes first.
        """
        lib = self._lib
        state = lib.xkb_state_new(keymap)
        if not state:
            return []
        found = []
        try:
            for xkb_keycode in range(lib.xkb_keymap_min_keycode(keymap),
                                     lib.xkb_keymap_max_keycode(keymap) + 1):
                if xkb_keycode - XKB_KEYCODE_OFFSET <= 0:
                    continue
                lib.xkb_state_update_key(state, xkb_keycode, XKB_KEY_DOWN)
                mask = lib.xkb_state_serialize_mods(state, XKB_STATE_MODS_EFFECTIVE)
                lib.xkb_state_update_key(state, xkb_keycode, XKB_KEY_UP)
                if not mask:
                    continue
                if lib.xkb_state_serialize_mods(state, XKB_STATE_MODS_EFFECTIVE):
                    # Releasing it did not clear the modifier, so it locks
                    # (Caps Lock). Start over: leftover state would skew every
                    # later measurement.
                    lib.xkb_state_unref(state)
                    state = lib.xkb_state_new(keymap)
                    if not state:
                        break
                    continue
                if mask & self._lock_mask:
                    continue
                found.append((xkb_keycode - XKB_KEYCODE_OFFSET, mask))
        finally:
            if state:
                lib.xkb_state_unref(state)

        def rank(entry):
            keycode, mask = entry
            preferred = (_PREFERRED_MOD_KEYCODES.index(keycode)
                         if keycode in _PREFERRED_MOD_KEYCODES
                         else len(_PREFERRED_MOD_KEYCODES))
            return bin(mask).count("1"), preferred, keycode

        found.sort(key=rank)
        return found

    def _pressable_modifiers(self, keymap, xkb_keycode, level):
        """Cheapest set of modifier keycodes that reaches `level`, or None."""
        masks = (ctypes.c_uint32 * 8)()
        count = self._lib.xkb_keymap_key_get_mods_for_level(
            keymap, xkb_keycode, 0, level, masks, 8)
        best = None
        for i in range(count):
            keycodes = self._keys_for_mask(masks[i])
            if keycodes is None:
                continue
            if best is None or len(keycodes) < len(best):
                best = keycodes
        return best

    def _keys_for_mask(self, mask):
        """Keys to hold down for `mask`, or None if it cannot be reached.

        A level reachable only through a lock (Caps Lock, Num Lock) counts as
        unreachable - toggling one would outlive the insertion.
        """
        if mask & self._lock_mask:
            return None
        remaining, keycodes = mask, []
        for keycode, key_mask in self._mod_keys:
            if not remaining:
                break
            if key_mask & ~mask or not key_mask & remaining:
                # Sets something we do not want, or nothing we still need.
                continue
            keycodes.append(keycode)
            remaining &= ~key_mask
        return keycodes if not remaining else None

    @staticmethod
    def _sequence(keycode, modifiers):
        return tuple(
            [(m, 1) for m in modifiers]
            + [(keycode, 1), (keycode, 0)]
            + [(m, 0) for m in reversed(modifiers)]
        )

    def _keysym_name(self, keysym):
        buf = ctypes.create_string_buffer(64)
        self._lib.xkb_keysym_get_name(keysym, buf, len(buf))
        return buf.value.decode("utf-8", "replace")

    def _keysym_text(self, keysym):
        buf = ctypes.create_string_buffer(8)
        if self._lib.xkb_keysym_to_utf8(keysym, buf, len(buf)) <= 0:
            return ""
        return buf.value.decode("utf-8", "replace")

    # -- text -> keystrokes ------------------------------------------------

    def keystrokes_for_char(self, char):
        """Keystrokes producing `char`, or None if the layout cannot reach it."""
        direct = self._chars.get(char)
        if direct:
            return direct[1]

        # Not on any key: rebuild it from a dead key plus the base letter.
        # "Č" -> "C" + U+030C -> dead_caron, then Shift+C.
        decomposed = unicodedata.normalize("NFD", char)
        if len(decomposed) < 2:
            return None
        base = self._chars.get(decomposed[0])
        if not base:
            return None
        sequence = []
        for mark in reversed(decomposed[1:]):
            dead = self._deads.get(_DEAD_KEYSYM_FOR_MARK.get(mark, ""))
            if not dead:
                return None
            sequence.extend(dead[1])
        return tuple(sequence) + base[1]

    def plan(self, text):
        """Split `text` into per-character keystrokes.

        Returns:
            (groups, unsupported) - one keystroke tuple per typeable character
            and the characters this layout cannot produce at all.
        """
        groups, unsupported = [], []
        for char in text:
            keystrokes = self.keystrokes_for_char(char)
            if keystrokes is None:
                unsupported.append(char)
            else:
                groups.append(keystrokes)
        return groups, unsupported


# ---------------------------------------------------------------------------
# Emitting the keystrokes through ydotool
# ---------------------------------------------------------------------------

# ydotool takes a whole key sequence per invocation; cap it so the argument
# list stays sane on long dictations.
_MAX_ARGS_PER_CALL = 480

# 4 ms tested fine in isolation but produced one reordered insertion on a
# loaded system, so the default leaves more headroom. A typical sentence
# still lands in about a second.
DEFAULT_KEY_DELAY_MS = int(os.environ.get("VOICESNIP_KEY_DELAY_MS", "12"))

_CACHED_TYPER = None
_CACHED_RMLVO = None


def get_typer():
    """The typer for the active layout, rebuilt when the layout changes."""
    global _CACHED_TYPER, _CACHED_RMLVO
    rmlvo = detect_rmlvo()
    if _CACHED_TYPER is not None and _CACHED_RMLVO == rmlvo:
        return _CACHED_TYPER
    try:
        _CACHED_TYPER = LayoutTyper(rmlvo)
    except (OSError, RuntimeError) as e:
        print(f"Layout-aware typing unavailable: {e}")
        _CACHED_TYPER = None
    _CACHED_RMLVO = rmlvo
    return _CACHED_TYPER


def _batches(groups):
    """Group per-character keystrokes into ydotool invocations."""
    batch, size = [], 0
    for group in groups:
        if batch and size + len(group) > _MAX_ARGS_PER_CALL:
            yield batch
            batch, size = [], 0
        batch.append(group)
        size += len(group)
    if batch:
        yield batch


def _resolve_env(env):
    """Environment for ydotool, with the daemon socket filled in if needed."""
    env = dict(env or os.environ)
    if env.get("YDOTOOL_SOCKET"):
        return env
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    candidates = ([os.path.join(runtime_dir, ".ydotool_socket")] if runtime_dir else [])
    candidates.append("/tmp/.ydotool_socket")
    for path in candidates:
        if os.path.exists(path):
            env["YDOTOOL_SOCKET"] = path
            break
    return env


def type_text(text, env=None, key_delay_ms=None):
    """Type `text` using the active layout.

    Nothing is typed unless every character can be produced, so the caller can
    fall back to a clipboard paste without ending up with half the text twice.

    Returns:
        (ok, unsupported) - whether the text was typed, and the characters the
        layout cannot produce.
    """
    typer = get_typer()
    if typer is None:
        return False, []

    groups, unsupported = typer.plan(text)
    if unsupported:
        return False, unsupported

    delay = str(DEFAULT_KEY_DELAY_MS if key_delay_ms is None else key_delay_ms)
    env = _resolve_env(env)
    for batch in _batches(groups):
        args = [f"{keycode}:{pressed}"
                for group in batch for keycode, pressed in group]
        try:
            subprocess.run(["ydotool", "key", "--key-delay", delay] + args,
                           check=True, timeout=60.0, env=env)
        except FileNotFoundError:
            print("ydotool not found. Please install: sudo apt install ydotool")
            return False, []
        except subprocess.CalledProcessError as e:
            print(f"Error typing text (ydotoold running? /dev/uinput access?): {e}")
            return False, []
        except subprocess.TimeoutExpired:
            print("Error: typing timed out (ydotoold daemon?)")
            return False, []
    return True, []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv):
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python3 -m voicesnip.xkb_typer",
        description="Type Unicode text on Wayland using the active XKB layout.")
    parser.add_argument("text", nargs="*", help="text to type (default: stdin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the keystroke plan instead of typing")
    parser.add_argument("--key-delay", type=int, default=None, metavar="MS",
                        help=f"delay between key events (default {DEFAULT_KEY_DELAY_MS})")
    args = parser.parse_args(argv)

    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text:
        return 0

    typer = get_typer()
    if typer is None:
        return 1

    print(f"layout: {typer.layout_label}  "
          f"({typer.direct_characters} direct characters, "
          f"{typer.dead_keys} dead keys)")
    _, unsupported = typer.plan(text)
    if unsupported:
        print("cannot be typed on this layout: " + " ".join(sorted(set(unsupported))))

    if args.dry_run:
        for char in text:
            keystrokes = typer.keystrokes_for_char(char)
            codes = (" ".join(f"{k}:{p}" for k, p in keystrokes)
                     if keystrokes else "-- not on this layout --")
            print(f"  {char!r:>8} -> {codes}")
        return 0

    ok, _ = type_text(text, key_delay_ms=args.key_delay)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
