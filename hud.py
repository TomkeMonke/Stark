"""The Jarvis-style heads-up display.

A frameless, click-through, always-on-top overlay that assembles itself in the
middle of the screen when Stark wakes up.

The look is built from three ideas:

1. **Light, not panels.** There is no card behind the HUD - only a soft radial
   haze that darkens the desktop under the reactor and fades to nothing well
   inside the window edge, so there is never a rectangle on screen.
2. **Bloom.** Everything emissive is drawn once into an offscreen layer, that
   layer is scaled down and back up twice (a cheap two-radius blur), and the
   crisp version is laid over the top. That is what makes the lines read as
   glowing glass rather than as vector strokes.
3. **Density from many thin things.** A stack of concentric instruments -
   ticks, gear blocks, tangential readouts, a dashed ring, a radar sweep, a
   voiceprint that answers the microphone, orbits, coils, and the reactor core
   itself - each turning at its own rate and direction.

Colour and speed change with Stark's state; the accent eases between states
rather than snapping. Everything is laid out in fractions of ``R`` so the whole
thing scales from one ``hud_size``.
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QRectF, QPointF, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QFontMetricsF, QGuiApplication,
    QImage, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QWidget

# State -> accent colour (Jarvis cyan, amber while thinking, brighter while
# talking, a cooler muted cyan while holding the mic open for a follow-up)
STATE_COLORS = {
    "listening": QColor(72, 216, 255),
    "thinking": QColor(255, 176, 59),
    "speaking": QColor(126, 234, 255),
    "followup": QColor(74, 172, 200),
    "idle": QColor(56, 196, 232),
}
STATE_LABELS = {
    "listening": "LISTENING",
    "thinking": "PROCESSING",
    "speaking": "STARK",
    "followup": "GO AHEAD",
    "idle": "STARK",
}
# The small tag over the caption. Says who the words below belong to.
STATE_TAGS = {
    "listening": "INPUT",
    "thinking": "QUERY",
    "speaking": "STARK",
    "followup": "INPUT",
    "idle": "STARK",
}
# Degrees per frame at 60fps for the master rotation.
SPIN = {"thinking": 4.2, "listening": 1.5, "speaking": 2.4,
        "followup": 0.9, "idle": 1.1}

BOOT_SEC = 0.85       # how long the assemble animation takes
WAVE_BARS = 96        # bars in the voiceprint ring
READOUT_KEYS = ("SYS", "PWR", "NET", "ASR", "TTS", "MEM")


def _a(c: QColor, alpha: float) -> QColor:
    """The accent at a given alpha."""
    return QColor(c.red(), c.green(), c.blue(), max(0, min(255, int(alpha))))


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def _ease(t: float) -> float:
    """Ease-out cubic: fast arrival, soft landing."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _stage(boot: float, i: int) -> float:
    """Assemble progress for the i-th ring, so they arrive outward in turn."""
    return _ease((boot - i * 0.05) / 0.5)


def _poly(cx: float, cy: float, r: float, n: int, rot: float) -> QPolygonF:
    pts = []
    for i in range(n):
        ang = math.radians(rot + i * 360.0 / n)
        pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return QPolygonF(pts)


class Hud(QWidget):
    def __init__(self, size: int = 460) -> None:
        super().__init__(None)
        self._size = size
        # The window is wider and taller than the reactor: the haze has to
        # reach zero alpha before the window edge or it shows up as a box.
        self._pad = 40
        self._caption_h = 140
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # keep it off the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # click-through
        self.resize(size + self._pad, size + self._caption_h)

        self._state = "idle"
        self._accent = QColor(STATE_COLORS["idle"])
        self._target = QColor(STATE_COLORS["idle"])
        self._angle = 0.0
        self._pulse = 0.0
        self._t = 0.0
        self._last = time.monotonic()
        self._text = ""
        self._text_at = 0.0
        self._state_at = -10.0
        self._flicker = 1.0
        # While a follow-up window is open, the outer ring drains away to show
        # how long is left to speak without saying the wake word again.
        self._followup_len = 0.0
        self._followup_start = 0.0
        # Assemble animation: 0 while dark, 1 once fully drawn.
        self._boot = 1.0
        self._boot_start = -BOOT_SEC

        # Voiceprint. `level_source` is a callable handing over one loudness
        # sample per frame; without one the ring runs on a synthesised idle
        # envelope so it never looks dead.
        self.level_source = None
        self._lvl = 0.0
        self._agc = 0.35  # slow-decaying peak, so any microphone fills the ring

        self._readouts = [self._new_readout(k) for k in READOUT_KEYS]
        self._readout_t = 0.0
        self._img: QImage | None = None
        self._cap_cache: tuple = ()
        self._sweep_clip: QPainterPath | None = None
        self._clip_key: tuple = ()

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.setInterval(16)  # ~60 fps

        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(200)  # the assemble carries the reveal
        self._fade.finished.connect(self._after_fade_out)  # connect once
        self.setWindowOpacity(0.0)

        self._center_on_screen()

    # ----- placement -----------------------------------------------------
    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen().geometry()
        self.move(
            screen.center().x() - self.width() // 2,
            screen.center().y() - self.height() // 2,
        )

    # ----- public slots (called via signals from the worker thread) ------
    @Slot(str)
    def set_state(self, state: str) -> None:
        if state != "followup":
            self._followup_len = 0.0
        if state != self._state:
            self._state_at = time.monotonic()  # fires the reconfigure scan
        self._state = state
        self._target = STATE_COLORS.get(state, STATE_COLORS["idle"])
        self.update()

    @Slot(float)
    def start_followup(self, seconds: float) -> None:
        """Hold the HUD open with a draining ring for the follow-up window."""
        self._followup_len = max(0.0, seconds)
        self._followup_start = time.monotonic()
        self.set_state("followup")

    @Slot(str)
    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text_at = time.monotonic()
        self._text = text
        self.update()

    @Slot(object)
    def set_level_source(self, source) -> None:
        """Hand the HUD something to ask for the microphone level.

        ``source()`` returns one 0..1 loudness sample per call, oldest first.
        The voiceprint ring answers it; without a source it runs synthetic.
        """
        self.level_source = source

    @Slot()
    def appear(self) -> None:
        self._center_on_screen()
        if self.windowOpacity() < 0.05:  # coming back from dark: reassemble
            self._boot_start = time.monotonic()
            self._t = 0.0
            self._last = time.monotonic()
        self.show()
        self.raise_()
        self._anim_timer.start()
        self._fade.stop()
        self._fade.setDuration(200)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    @Slot()
    def vanish(self) -> None:
        self._fade.stop()
        self._fade.setDuration(320)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _after_fade_out(self) -> None:
        if self.windowOpacity() <= 0.01:
            self._anim_timer.stop()
            self.hide()
            self._text = ""

    # ----- animation -----------------------------------------------------
    def _new_readout(self, key: str) -> str:
        if key == "PWR":
            return f"PWR {random.uniform(93, 99.9):.1f}"
        if key == "NET":
            return f"NET {random.randint(2, 89):03d}"
        return f"{key} {random.randint(0, 0xFFFF):04X}"

    def _churn(self) -> None:
        """Tick one readout over, so the rim always has something moving."""
        i = random.randrange(len(self._readouts))
        self._readouts[i] = self._new_readout(READOUT_KEYS[i])

    def _pump_level(self, dt: float) -> None:
        """Fold one microphone sample into the level the ring is drawn from."""
        raw = 0.0
        src = self.level_source
        if src is not None:
            try:
                raw = max(0.0, min(1.0, float(src())))
            except Exception:
                raw = 0.0
        # Auto-gain: track a slowly-decaying peak so a quiet mic still fills
        # the ring and a hot one doesn't peg it.
        self._agc = max(0.10, self._agc * 0.997, raw)
        heard = min(1.3, raw / self._agc)
        # A synthesised floor, so the ring breathes even in silence - and so
        # it still looks alive with no source at all (tests, no microphone).
        s = self._t
        idle = (0.30 + 0.22 * math.sin(s * 1.9)) * (0.6 + 0.4 * math.sin(s * 4.7))
        floor = {"speaking": 0.62, "thinking": 0.34, "listening": 0.16,
                 "followup": 0.16}.get(self._state, 0.12)
        want = max(heard, abs(idle) * floor + floor * 0.35)
        # Fast attack, slow release - the shape a level meter should have.
        k = 0.55 if want > self._lvl else 0.10
        self._lvl += (want - self._lvl) * min(1.0, k * dt * 60)

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.05, max(0.0, now - self._last))
        self._last = now
        self._t += dt

        self._angle = (self._angle + SPIN.get(self._state, 1.1) * dt * 60) % 360
        self._pulse = (self._pulse + dt * 3.0) % (2 * math.pi)
        self._boot = min(1.0, (now - self._boot_start) / BOOT_SEC)
        self._accent = _mix(self._accent, self._target, min(1.0, dt * 7.0))
        self._pump_level(dt)

        self._readout_t += dt
        if self._readout_t > 0.32:
            self._readout_t = 0.0
            self._churn()
        # Holographic instability - only ever applied to the glow, never to
        # the crisp pass, so text stays readable.
        self._flicker = 1.0 - random.random() * 0.05
        self.update()

    # ----- painting ------------------------------------------------------
    def _layer(self, w: int, h: int) -> QImage:
        if self._img is None or self._img.width() != w or self._img.height() != h:
            self._img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        self._img.fill(Qt.transparent)
        return self._img

    def paintEvent(self, event) -> None:  # noqa: N802
        w, h = self.width(), self.height()
        cx = w / 2.0
        cy = self._size / 2.0 + self._pad / 2.0
        R = self._size * 0.42
        accent = self._accent
        boot = self._boot
        eb = _ease(boot)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.TextAntialiasing)

        # 1. The haze. Drawn straight onto the widget so it never blooms, and
        #    faded to nothing at 1.28R - inside every window edge. The caption
        #    scrim goes down here too: painted after the bloom it would wash
        #    out the very tag and brackets it sits behind.
        self._draw_haze(p, cx, cy, R, eb)
        self._draw_caption_scrim(p, cx, R, w, eb)

        # 2. Everything that emits light goes into one offscreen layer.
        layer = self._layer(w, h)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.Antialiasing)
        lp.setRenderHint(QPainter.TextAntialiasing)
        self._draw_reactor(lp, cx, cy, R, accent, boot)
        self._draw_caption_chrome(lp, cx, w, accent, eb)
        lp.end()

        # 3. Bloom: the same layer scaled down and back up, twice - a tight
        #    halo and a wide one - then the crisp original over the top.
        for div, op in ((5, 0.60), (13, 0.45)):
            small = layer.scaled(max(1, w // div), max(1, h // div),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            p.setOpacity(self._flicker * op)
            p.drawImage(QRectF(0, 0, w, h), small)
        p.setOpacity(1.0)
        p.drawImage(0, 0, layer)

        # 4. Scanlines over everything, masked to a circle so the window edge
        #    never shows, plus a bright bar when the state has just changed.
        self._draw_scanlines(p, cx, cy, R, w, accent, eb)

        # 5. Caption words last, crisp and unbloomed so they stay readable.
        self._draw_caption_text(p, cx, w, accent, eb)
        p.end()

    # ----- layers --------------------------------------------------------
    def _draw_haze(self, p: QPainter, cx: float, cy: float, R: float,
                   eb: float) -> None:
        r = R * 1.28
        g = QRadialGradient(QPointF(cx, cy), r)
        g.setColorAt(0.00, QColor(3, 7, 12, int(214 * eb)))
        g.setColorAt(0.52, QColor(3, 7, 12, int(184 * eb)))
        g.setColorAt(0.80, QColor(3, 7, 12, int(122 * eb)))
        g.setColorAt(0.92, QColor(3, 7, 12, int(44 * eb)))
        g.setColorAt(1.00, QColor(3, 7, 12, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # A soft well under the status word. The rings run behind it, so
        # without this the letters sit on top of a hairline or two.
        p.save()
        p.translate(cx, cy + R * 0.47)
        p.scale(1.0, 0.17)
        wr = R * 0.36
        g = QRadialGradient(QPointF(0, 0), wr)
        g.setColorAt(0.00, QColor(3, 7, 12, int(150 * eb)))
        g.setColorAt(0.55, QColor(3, 7, 12, int(112 * eb)))
        g.setColorAt(1.00, QColor(3, 7, 12, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(0, 0), wr, wr)
        p.restore()

        # The four diagonal readouts sit out where the haze has nearly faded,
        # so each gets its own little pool of shade. Without them the labels
        # vanish against a bright desktop.
        for deg in (315, 45, 225, 135):
            a = math.radians(deg)
            p.save()
            p.translate(cx + R * 1.13 * math.cos(a),
                        cy + R * 1.13 * math.sin(a))
            p.scale(1.0, 0.30)
            wr = R * 0.34
            g = QRadialGradient(QPointF(0, 0), wr)
            g.setColorAt(0.00, QColor(3, 7, 12, int(112 * eb)))
            g.setColorAt(0.42, QColor(3, 7, 12, int(92 * eb)))
            g.setColorAt(0.72, QColor(3, 7, 12, int(46 * eb)))
            g.setColorAt(1.00, QColor(3, 7, 12, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), wr, wr)
            p.restore()

    def _draw_scanlines(self, p: QPainter, cx: float, cy: float, R: float,
                        w: int, accent: QColor, eb: float) -> None:
        top = int(cy - R * 1.12)
        bottom = int(cy + R * 1.12)
        path = QPainterPath()
        for y in range(max(0, top), min(int(self._size + 6), bottom), 3):
            path.addRect(0.0, float(y), float(w), 1.0)
        g = QRadialGradient(QPointF(cx, cy), R * 1.14)
        g.setColorAt(0.00, _a(accent, 22 * eb))
        g.setColorAt(0.62, _a(accent, 20 * eb))
        g.setColorAt(1.00, _a(accent, 0))
        p.fillPath(path, QBrush(g))

        # A bright bar sweeps down whenever Stark changes state - the HUD
        # visibly reconfiguring rather than just recolouring.
        age = time.monotonic() - self._state_at
        if 0.0 <= age < 0.45:
            k = age / 0.45
            y = cy - R * 1.1 + 2.2 * R * 1.1 * k
            fade = (1.0 - k) * eb
            band = QRadialGradient(QPointF(cx, y), R * 1.05)
            band.setColorAt(0.0, _a(accent, 130 * fade))
            band.setColorAt(1.0, _a(accent, 0))
            p.save()
            p.setPen(Qt.NoPen)
            p.setBrush(band)
            p.drawRect(QRectF(0, y - 1.2, w, 2.4))
            p.restore()

    # ----- the reactor ---------------------------------------------------
    def _draw_reactor(self, p: QPainter, cx: float, cy: float, R: float,
                      accent: QColor, boot: float) -> None:
        c = QPointF(cx, cy)
        ang = self._angle
        lvl = self._lvl
        pulse = (math.sin(self._pulse) + 1) / 2  # 0..1

        def ring(r: float, width: float, alpha: float) -> None:
            pen = QPen(_a(accent, alpha))
            pen.setWidthF(width)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)

        def arc(r: float, start: float, span: float, width: float,
                alpha: float) -> None:
            pen = QPen(_a(accent, alpha))
            pen.setWidthF(width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2),
                      int(start * 16), int(span * 16))

        # --- the core, first out of the gate ---------------------------
        s = _stage(boot, 0)
        if s > 0:
            self._draw_core(p, c, R, accent, pulse, lvl, s)

        # --- hexagonal housing + coil spokes ---------------------------
        s = _stage(boot, 1)
        if s > 0:
            hr = R * 0.29 * (0.7 + 0.3 * s)
            pen = QPen(_a(accent, 120 * s))
            pen.setWidthF(1.3)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(_poly(cx, cy, hr, 6, -ang * 0.25))
            p.drawPolygon(_poly(cx, cy, hr * 0.86, 6, -ang * 0.25 + 30))

            p.save()
            p.translate(cx, cy)
            p.rotate(ang * 0.35)
            pen = QPen(_a(accent, 165 * s))
            pen.setWidthF(2.4)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            r0, r1 = R * 0.335, R * 0.385
            for i in range(12):
                a = math.radians(i * 30)
                ca, sa = math.cos(a), math.sin(a)
                p.drawLine(QPointF(r0 * ca, r0 * sa), QPointF(r1 * ca, r1 * sa))
            p.restore()

        # --- tilted orbits, for a hint of depth ------------------------
        s = _stage(boot, 2)
        if s > 0:
            for tilt, rate, rr, flat in ((16, 0.6, 0.47, 0.13),
                                         (-34, -0.44, 0.40, 0.10)):
                p.save()
                p.translate(cx, cy)
                p.rotate(tilt + ang * 0.12 * (1 if rate > 0 else -1))
                a_r, b_r = R * rr * (0.8 + 0.2 * s), R * rr * flat
                pen = QPen(_a(accent, 58 * s))
                pen.setWidthF(1.0)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(0, 0), a_r, b_r)
                # the satellite riding it
                phi = math.radians(ang * rate * 3.0)
                sx, sy = a_r * math.cos(phi), b_r * math.sin(phi)
                p.setPen(Qt.NoPen)
                p.setBrush(_a(accent, 230 * s))
                p.drawEllipse(QPointF(sx, sy), 2.6, 2.6)
                p.restore()

        # --- inner ring and its rotating arcs --------------------------
        s = _stage(boot, 3)
        if s > 0:
            r = R * 0.52 * (0.82 + 0.18 * s)
            ring(r, 1.0, 62 * s)
            arc(r, -ang * 1.5, 74, 2.0, 175 * s)
            arc(r, -ang * 1.5 + 138, 40, 2.0, 130 * s)
            arc(r, -ang * 1.5 + 250, 22, 2.0, 105 * s)

        # --- radar sweep, under the outer instruments ------------------
        s = _stage(boot, 4)
        if s > 0:
            self._draw_sweep(p, cx, cy, R, accent, ang, s)

        # --- the voiceprint --------------------------------------------
        s = _stage(boot, 5)
        if s > 0:
            self._draw_voiceprint(p, cx, cy, R, accent, lvl, s)

        # --- dashed ring ------------------------------------------------
        s = _stage(boot, 6)
        if s > 0:
            r = R * 0.72 * (0.86 + 0.14 * s)
            p.save()
            p.translate(cx, cy)
            p.rotate(-ang * 1.7)
            pen = QPen(_a(accent, 92 * s))
            pen.setWidthF(1.6)
            p.setPen(pen)
            for i in range(40):
                a0 = i * 9.0
                p.drawArc(QRectF(-r, -r, r * 2, r * 2),
                          int(a0 * 16), int(5.0 * 16))
            p.restore()

        # --- tangential readouts ---------------------------------------
        s = _stage(boot, 7)
        if s > 0:
            self._draw_readouts(p, cx, cy, R, accent, ang, s)

        # --- segmented gear ring ---------------------------------------
        s = _stage(boot, 8)
        if s > 0:
            r = R * 0.88 * (0.88 + 0.12 * s)
            p.save()
            p.translate(cx, cy)
            p.rotate(ang * 0.55)
            pen = QPen(_a(accent, 118 * s))
            pen.setWidthF(5.0)
            p.setPen(pen)
            for i in range(14):
                a0 = i * (360.0 / 14)
                p.drawArc(QRectF(-r, -r, r * 2, r * 2),
                          int(a0 * 16), int(17.0 * 16))
            p.restore()

        # --- fine tick ring ---------------------------------------------
        s = _stage(boot, 9)
        if s > 0:
            p.save()
            p.translate(cx, cy)
            base = R * 0.95 * (0.9 + 0.1 * s)
            major_pen = QPen(_a(accent, 150 * s))
            major_pen.setWidthF(1.6)
            minor_pen = QPen(_a(accent, 88 * s))
            minor_pen.setWidthF(1.1)
            # Both pens are built once and swapped: ninety of each per frame
            # is real money at 60fps.
            for i in range(90):
                a = math.radians(i * 4)
                major = i % 5 == 0
                p.setPen(major_pen if major else minor_pen)
                r0 = base - (R * 0.055 if major else R * 0.028)
                ca, sa = math.cos(a), math.sin(a)
                p.drawLine(QPointF(r0 * ca, r0 * sa),
                           QPointF(base * ca, base * sa))
            p.restore()

        # --- outer hairline + brackets + chevrons -----------------------
        s = _stage(boot, 10)
        if s > 0:
            # A segmented ring rather than a closed circle: the gaps sit on
            # the diagonals, which is where the readouts live, so the labels
            # get a clean radial channel instead of a hairline through them.
            rb = R * (0.94 + 0.06 * s)
            pen = QPen(_a(accent, 150 * s))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            box = QRectF(cx - rb, cy - rb, rb * 2, rb * 2)
            for a0 in (0, 90, 180, 270):
                p.drawArc(box, int((a0 - 31) * 16), int(62 * 16))
            # each arc capped with a short radial tick, the way an instrument
            # bezel is graduated
            pen.setColor(_a(accent, 200 * s))
            pen.setWidthF(2.2)
            p.setPen(pen)
            for a0 in (0, 90, 180, 270):
                for end in (a0 - 31, a0 + 31):
                    ax = math.radians(end)
                    ca, sa = math.cos(ax), -math.sin(ax)
                    p.drawLine(QPointF(cx + rb * ca, cy + rb * sa),
                               QPointF(cx + (rb + R * 0.05) * ca,
                                       cy + (rb + R * 0.05) * sa))
            # cardinal chevrons
            p.setPen(Qt.NoPen)
            p.setBrush(_a(accent, 200 * s))
            for a0 in (0, 90, 180, 270):
                a = math.radians(a0)
                ca, sa = math.cos(a), math.sin(a)
                tip = R * 1.005
                back = R * 1.055
                nx, ny = -sa, ca
                p.drawPolygon(QPolygonF([
                    QPointF(cx + tip * ca, cy + tip * sa),
                    QPointF(cx + back * ca + nx * 4.2, cy + back * sa + ny * 4.2),
                    QPointF(cx + back * ca - nx * 4.2, cy + back * sa - ny * 4.2),
                ]))

        # --- the boot shockwave, once, on the way in --------------------
        if boot < 1.0:
            k = _ease(boot)
            r = R * (0.15 + 1.10 * k)
            pen = QPen(_a(accent, 210 * (1.0 - k)))
            pen.setWidthF(2.0 + 3.0 * (1.0 - k))
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)

        # --- follow-up drain ring ---------------------------------------
        if self._state == "followup" and self._followup_len > 0:
            elapsed = time.monotonic() - self._followup_start
            left = max(0.0, min(1.0, 1.0 - elapsed / self._followup_len))
            # Tucked just inside the bezel: anything from 0.99R outward runs
            # straight through the diagonal readouts.
            r = R * 0.965
            pen = QPen(_a(accent, 70))
            pen.setWidthF(3.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)
            pen = QPen(_a(accent, 240))
            pen.setWidthF(3.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2),
                      90 * 16, int(-360 * left * 16))

        # --- the status word, under the core ----------------------------
        self._draw_status(p, cx, cy, R, accent, boot)

    def _draw_core(self, p: QPainter, c: QPointF, R: float, accent: QColor,
                   pulse: float, lvl: float, s: float) -> None:
        """White-hot centre, a torus of light around it, and a wide falloff."""
        breathe = 1.0 + 0.05 * pulse + 0.10 * lvl
        p.setPen(Qt.NoPen)

        wide = R * 0.44 * s * breathe
        g = QRadialGradient(c, wide)
        g.setColorAt(0.00, _a(accent, 118 * s))
        g.setColorAt(0.42, _a(accent, 44 * s))
        g.setColorAt(1.00, _a(accent, 0))
        p.setBrush(g)
        p.drawEllipse(c, wide, wide)

        # The torus: a ring of light with the falloff pushed outward, so the
        # middle stays comparatively dark instead of blowing out.
        tr = R * 0.19 * s * breathe
        pen = QPen(_a(accent, 200 * s))
        pen.setWidthF(max(1.0, R * 0.045))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, tr, tr)
        pen = QPen(_mix(accent, QColor(255, 255, 255), 0.55))
        pen.setColor(_a(pen.color(), 225 * s))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.drawEllipse(c, tr * 1.14, tr * 1.14)

        hot = R * 0.135 * s * breathe
        g = QRadialGradient(c, hot)
        g.setColorAt(0.00, QColor(255, 255, 255, int(250 * s)))
        g.setColorAt(0.34, _a(_mix(accent, QColor(255, 255, 255), 0.72), 235 * s))
        g.setColorAt(0.74, _a(accent, 150 * s))
        g.setColorAt(1.00, _a(accent, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, hot, hot)

    def _draw_sweep(self, p: QPainter, cx: float, cy: float, R: float,
                    accent: QColor, ang: float, s: float) -> None:
        r1, r0 = R * 0.86, R * 0.36
        # The annulus the sweep is clipped to never changes, and subtracting
        # one path from another is much too expensive to redo 60 times a
        # second - it was the single costliest thing in the frame.
        key = (cx, cy, R)
        if self._clip_key != key:
            outer = QPainterPath()
            outer.addEllipse(QPointF(cx, cy), r1, r1)
            inner = QPainterPath()
            inner.addEllipse(QPointF(cx, cy), r0, r0)
            self._sweep_clip = outer.subtracted(inner)
            self._clip_key = key
        g = QConicalGradient(QPointF(cx, cy), -ang)
        g.setColorAt(0.00, _a(accent, 72 * s))
        g.setColorAt(0.05, _a(accent, 44 * s))
        g.setColorAt(0.18, _a(accent, 18 * s))
        g.setColorAt(0.42, _a(accent, 0))
        g.setColorAt(1.00, _a(accent, 0))
        p.save()
        p.setClipPath(self._sweep_clip)
        p.setPen(Qt.NoPen)
        p.fillRect(QRectF(cx - r1, cy - r1, r1 * 2, r1 * 2), QBrush(g))
        p.restore()
        # the leading edge of the sweep
        a = math.radians(ang)
        ca, sa = math.cos(a), math.sin(a)
        pen = QPen(_a(accent, 150 * s))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.drawLine(QPointF(cx + r0 * ca, cy + r0 * sa),
                   QPointF(cx + r1 * ca, cy + r1 * sa))

    def _draw_voiceprint(self, p: QPainter, cx: float, cy: float, R: float,
                         accent: QColor, lvl: float, s: float) -> None:
        """A ring of bars that answers the microphone.

        The height is real - it comes from the level the voice engine hands
        over - and the shape is procedural, mirrored left to right so it reads
        as one symmetrical voiceprint rather than noise.
        """
        base = R * 0.58
        span = R * 0.15
        t = self._t
        p.save()
        p.translate(cx, cy)
        pen = QPen(_a(accent, 175 * s))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        half = WAVE_BARS // 2
        for i in range(WAVE_BARS):
            k = i if i <= half else WAVE_BARS - i  # mirror across the vertical
            u = k / half
            shape = ((0.55 + 0.45 * math.sin(u * 9.4 + t * 2.1))
                     * (0.60 + 0.40 * math.sin(u * 21.0 - t * 3.3)))
            hgt = span * (0.10 + 0.90 * lvl * shape) * s
            a = math.radians(i * (360.0 / WAVE_BARS) - 90)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(base * ca, base * sa),
                       QPointF((base + hgt) * ca, (base + hgt) * sa))
        # the rail the bars sit on
        pen = QPen(_a(accent, 55 * s))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0, 0), base, base)
        p.restore()

    def _draw_readouts(self, p: QPainter, cx: float, cy: float, R: float,
                       accent: QColor, ang: float, s: float) -> None:
        f = QFont("Consolas", 8)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
        p.setFont(f)
        fm = QFontMetricsF(f)
        # Upright, not tangential: curved text this small is decoration you
        # can't read, and half of it ends up upside down. The six sit on the
        # diagonals, just outside the corner brackets where there is clean
        # space, plus top and bottom, the two places inside the stack where a
        # horizontal label has radial room.
        live = [f"LVL {int(min(0.999, self._lvl) * 100):02d}",
                f"{self._state[:3].upper()} OK"]
        labels = live + list(self._readouts[2:])
        blink = int(self._t * 1.6) % len(labels)  # a cursor moving down the list
        for i, (deg, txt) in enumerate(zip((315, 45, 270, 90, 225, 135), labels)):
            a = math.radians(deg)
            r = R * (0.82 if deg in (90, 270) else 1.13)
            x = cx + r * math.cos(a)
            y = cy + r * math.sin(a)
            if i == blink:
                txt += " _"
            wid = fm.horizontalAdvance(txt)
            p.setPen(_a(accent, 215 * s))
            p.drawText(QRectF(x - 60, y - 8, 120, 15), int(Qt.AlignCenter), txt)
            pen = QPen(_a(accent, 70 * s))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawLine(QPointF(x - wid / 2, y + 8), QPointF(x + wid / 2, y + 8))

    def _draw_status(self, p: QPainter, cx: float, cy: float, R: float,
                     accent: QColor, boot: float) -> None:
        label = STATE_LABELS.get(self._state, "STARK")
        k = _ease((boot - 0.45) / 0.4)
        if k <= 0:
            return
        shown = label[:max(1, int(len(label) * k + 0.5))]  # types itself in
        f = QFont("Consolas", 10)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 3.4)
        p.setFont(f)
        fm = QFontMetricsF(f)
        # Anchored on the whole word rather than centred on what has been typed
        # so far: the reveal then reads left to right, and the working dots
        # don't shove the label sideways every third of a second.
        wide = fm.horizontalAdvance(label)
        x0 = cx - wide / 2
        base = cy + R * 0.47 + (fm.ascent() - fm.descent()) / 2
        p.setPen(_a(_mix(accent, QColor(255, 255, 255), 0.5), 240 * k))
        p.drawText(QPointF(x0, base), shown)
        if self._state == "thinking":  # ... while it works
            p.drawText(QPointF(x0 + wide + 2, base),
                       "." * (int(self._t * 3) % 4))

    # ----- the caption ---------------------------------------------------
    def _caption_lines(self, w: int) -> tuple[str, QRectF]:
        """The transcript, wrapped and trimmed to fit under the reactor."""
        key = (self._text, w)
        if self._cap_cache and self._cap_cache[0] == key:
            return self._cap_cache[1], self._cap_cache[2]
        f = QFont("Segoe UI", 12)
        f.setWeight(QFont.Medium)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
        fm = QFontMetricsF(f)
        area = QRectF(0, 0, w - 104, 1000)
        flags = int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap)
        max_h = 4 * fm.lineSpacing() + 2
        text = self._text
        rect = fm.boundingRect(area, flags, text)
        while rect.height() > max_h and " " in text:
            text = text.rsplit(" ", 1)[0]
            rect = fm.boundingRect(area, flags, text + "...")
        if text != self._text:
            text += "..."
        self._cap_cache = (key, text, rect)
        return text, rect

    def _caption_geometry(self, cx: float, w: int):
        """Where the tag rule and the words go. None when there is nothing."""
        top = self._size + 4
        text, rect = self._caption_lines(w)
        # With nothing said yet the divider still holds its width, so the
        # tag reads as a rule across the HUD rather than a floating word.
        half = max(self._size * 0.23, min(cx - 26.0, rect.width() / 2 + 26))
        return text, rect, top, half

    def _draw_caption_scrim(self, p: QPainter, cx: float, R: float, w: int,
                            eb: float) -> None:
        """Darkness under the caption, wide enough to merge with the haze.

        Any narrower and it reads as a second blob hanging off the bottom of
        the reactor instead of one continuous shadow.
        """
        if eb < 0.3:
            return
        text, rect, top, half = self._caption_geometry(cx, w)
        block = rect.height() + 30 if text else 12.0
        hw = min(cx - 4, max(half + 34, R * 0.92))
        hh = block / 2 + (18 if text else 34)
        # With nothing to protect but the tag, a full-strength scrim is a
        # black bar slung under the reactor. Fade it right down instead.
        k = 1.0 if text else 0.42
        p.save()
        p.translate(cx, top + 4 + block / 2)
        p.scale(1.0, hh / hw)
        g = QRadialGradient(QPointF(0, 0), hw)
        g.setColorAt(0.00, QColor(3, 7, 12, int(224 * eb * k)))
        g.setColorAt(0.58, QColor(3, 7, 12, int(196 * eb * k)))
        g.setColorAt(0.84, QColor(3, 7, 12, int(96 * eb * k)))
        g.setColorAt(1.00, QColor(3, 7, 12, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(0, 0), hw, hw)
        p.restore()

    def _draw_caption_chrome(self, p: QPainter, cx: float, w: int,
                             accent: QColor, eb: float) -> None:
        """The divider, the state tag and the corner brackets - these glow."""
        if eb < 0.3:
            return
        text, rect, top, half = self._caption_geometry(cx, w)
        tag = STATE_TAGS.get(self._state, "STARK")
        f = QFont("Consolas", 8)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2.6)
        p.setFont(f)
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(tag) + 18
        y = top + 8
        p.setPen(_a(accent, 235 * eb))
        p.drawText(QRectF(cx - tw / 2, y - 8, tw, 16),
                   int(Qt.AlignCenter), tag)

        pen = QPen(_a(accent, 120 * eb))
        pen.setWidthF(1.1)
        p.setPen(pen)
        reach = half * eb
        p.drawLine(QPointF(cx - reach, y), QPointF(cx - tw / 2 - 8, y))
        p.drawLine(QPointF(cx + tw / 2 + 8, y), QPointF(cx + reach, y))
        p.setPen(Qt.NoPen)
        p.setBrush(_a(accent, 200 * eb))
        for x in (cx - reach, cx + reach):
            p.drawPolygon(QPolygonF([
                QPointF(x, y - 3.2), QPointF(x + 3.2, y),
                QPointF(x, y + 3.2), QPointF(x - 3.2, y),
            ]))

        if not text:
            return
        # Corner brackets around the words.
        bh = rect.height() + 18
        by = y + 12
        pen = QPen(_a(accent, 150 * eb))
        pen.setWidthF(1.3)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        leg = 9.0
        for sx in (-1, 1):
            for sy in (0, 1):
                x = cx + sx * half
                yy = by if sy == 0 else by + bh
                p.drawLine(QPointF(x, yy), QPointF(x - sx * leg, yy))
                p.drawLine(QPointF(x, yy),
                           QPointF(x, yy + (leg if sy == 0 else -leg)))

    def _draw_caption_text(self, p: QPainter, cx: float, w: int,
                           accent: QColor, eb: float) -> None:
        text, rect, top, half = self._caption_geometry(cx, w)
        if not text or eb < 0.3:
            return
        by = top + 20
        # Newly-set text rises the last few pixels into place.
        age = time.monotonic() - self._text_at
        k = _ease(age / 0.22) if age < 0.22 else 1.0
        f = QFont("Segoe UI", 12)
        f.setWeight(QFont.Medium)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
        p.setFont(f)
        p.setOpacity(k)
        p.setPen(_mix(QColor(228, 246, 255), accent, 0.18))
        area = QRectF(cx - (w - 104) / 2, by + 5 * (1 - k), w - 104,
                      rect.height() + 6)
        p.drawText(area, int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap),
                   text)
        p.setOpacity(1.0)
