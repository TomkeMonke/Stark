"""The Jarvis-style heads-up display.

A frameless, transparent, always-on-top window that fades into the centre of the
screen. It draws a glowing arc-reactor: concentric rings, rotating arc segments,
tick marks and a pulsing core. Colour and speed change with Stark's state.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, Property, QRectF, Slot, QPointF,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QFont, QRadialGradient, QGuiApplication,
)
from PySide6.QtWidgets import QWidget

# State -> accent colour (Jarvis cyan, amber while thinking, bright while talking,
# a cooler muted cyan while holding the mic open for a follow-up)
STATE_COLORS = {
    "listening": QColor(45, 212, 238),
    "thinking": QColor(255, 176, 59),
    "speaking": QColor(120, 230, 255),
    "followup": QColor(58, 156, 180),
    "idle": QColor(45, 212, 238),
}


class Hud(QWidget):
    def __init__(self, size: int = 460) -> None:
        super().__init__(None)
        self._size = size
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # keep it off the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # click-through
        self.resize(size, size + 90)

        self._state = "idle"
        self._angle = 0.0
        self._pulse = 0.0
        self._text = ""
        # While a follow-up window is open, the outer ring drains away to show
        # how long is left to speak without saying the wake word again.
        self._followup_len = 0.0
        self._followup_start = 0.0

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.setInterval(16)  # ~60 fps

        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(350)
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
        self._state = state
        self.update()

    @Slot(float)
    def start_followup(self, seconds: float) -> None:
        """Hold the HUD open with a draining ring for the follow-up window."""
        self._followup_len = max(0.0, seconds)
        self._followup_start = time.monotonic()
        self._state = "followup"
        self.update()

    @Slot(str)
    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    @Slot()
    def appear(self) -> None:
        self._center_on_screen()
        self.show()
        self.raise_()
        self._anim_timer.start()
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    @Slot()
    def vanish(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _after_fade_out(self) -> None:
        if self.windowOpacity() <= 0.01:
            self._anim_timer.stop()
            self.hide()
            self._text = ""

    # ----- animation -----------------------------------------------------
    def _tick(self) -> None:
        speed = {"thinking": 4.5, "listening": 1.6, "speaking": 2.6,
                 "followup": 0.9}.get(self._state, 1.2)
        self._angle = (self._angle + speed) % 360
        self._pulse = (self._pulse + 0.05) % (2 * math.pi)
        self.update()

    # ----- painting ------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        accent = STATE_COLORS.get(self._state, STATE_COLORS["idle"])
        cx, cy = self.width() / 2, self._size / 2 + 10
        R = self._size / 2 - 20
        pulse = (math.sin(self._pulse) + 1) / 2  # 0..1

        # Solid dark backdrop (rounded panel) that fully hides whatever is on
        # screen behind the HUD, so nothing collides with the reactor or text.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(6, 10, 16, 155))  # semi-transparent: hologram feel
        p.drawRoundedRect(QRectF(2, 2, self.width() - 4, self.height() - 4), 26, 26)
        # Subtle accent glow for depth against the dark panel.
        glow = QRadialGradient(QPointF(cx, cy), R * 1.15)
        g0 = QColor(accent); g0.setAlpha(32)
        g1 = QColor(accent); g1.setAlpha(0)
        glow.setColorAt(0.0, g0)
        glow.setColorAt(1.0, g1)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), R * 1.15, R * 1.15)

        # Glowing core
        core_r = R * (0.28 + 0.04 * pulse)
        grad = QRadialGradient(QPointF(cx, cy), core_r)
        c0 = QColor(accent); c0.setAlpha(230)
        c1 = QColor(accent); c1.setAlpha(0)
        grad.setColorAt(0.0, QColor(255, 255, 255, 230))
        grad.setColorAt(0.35, c0)
        grad.setColorAt(1.0, c1)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)

        def ring(radius, width, alpha):
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            pen.setWidthF(width)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), radius, radius)

        # Static concentric rings
        ring(R, 2.0, 90)
        ring(R * 0.82, 1.2, 60)
        ring(R * 0.5, 1.0, 50)

        # Rotating arc segments at a few radii
        def arc(radius, start_deg, span_deg, width, alpha):
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            pen.setWidthF(width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            p.drawArc(rect, int(start_deg * 16), int(span_deg * 16))

        a = self._angle
        arc(R, a, 60, 3.0, 230)
        arc(R, a + 150, 40, 3.0, 180)
        arc(R, a + 250, 25, 3.0, 150)
        arc(R * 0.82, -a * 1.4, 80, 2.2, 170)
        arc(R * 0.82, -a * 1.4 + 180, 50, 2.2, 130)
        arc(R * 0.64, a * 0.8, 110, 1.6, 120)

        # Tick marks around the outer ring
        p.save()
        p.translate(cx, cy)
        tickpen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 110))
        tickpen.setWidthF(1.4)
        p.setPen(tickpen)
        for i in range(60):
            ang = math.radians(i * 6)
            r0 = R * 0.88
            r1 = R * 0.93 if i % 5 else R * 0.96
            p.drawLine(
                QPointF(r0 * math.cos(ang), r0 * math.sin(ang)),
                QPointF(r1 * math.cos(ang), r1 * math.sin(ang)),
            )
        p.restore()

        # Follow-up window: a ring that drains clockwise from twelve o'clock,
        # so the user can see how long they have to just keep talking.
        if self._state == "followup" and self._followup_len > 0:
            elapsed = time.monotonic() - self._followup_start
            left = max(0.0, min(1.0, 1.0 - elapsed / self._followup_len))
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 235))
            pen.setWidthF(4.0)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            rect = QRectF(cx - R * 1.02, cy - R * 1.02, R * 2.04, R * 2.04)
            p.drawArc(rect, 90 * 16, int(-360 * left * 16))

        # Status word in the centre
        p.setPen(QColor(220, 245, 255, 230))
        f = QFont("Consolas", 11)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        p.setFont(f)
        label = {"listening": "LISTENING", "thinking": "PROCESSING",
                 "speaking": "STARK", "followup": "GO AHEAD",
                 "idle": "STARK"}.get(self._state, "STARK")
        p.drawText(QRectF(0, cy - 10, self.width(), 20), Qt.AlignHCenter, label)

        # Transcript / reply text below the reactor, on a solid panel
        if self._text:
            tf = QFont("Segoe UI", 12)
            tf.setBold(True)
            p.setFont(tf)
            flags = int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap)
            area = QRectF(18, self._size + 4, self.width() - 36, 80)
            # Measure the wrapped text so the panel hugs it.
            used = p.boundingRect(area, flags, self._text)
            panel = used.adjusted(-12, -8, 12, 8)
            p.setBrush(QColor(8, 12, 18, 225))
            border = QColor(accent.red(), accent.green(), accent.blue(), 150)
            p.setPen(QPen(border, 1.2))
            p.drawRoundedRect(panel, 10, 10)
            p.setPen(QColor(228, 246, 255))
            p.drawText(area, flags, self._text)
        p.end()
