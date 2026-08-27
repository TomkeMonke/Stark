"""System-tray icon for Stark: a small arc-reactor with a right-click menu.

Lets you quit Stark when it's running windowless (no console).
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

ACCENT = QColor(45, 212, 238)  # Jarvis cyan


def make_reactor_icon(size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = size / 2
    R = size / 2 - 4

    # glowing core
    grad = QRadialGradient(QPointF(c, c), R * 0.55)
    grad.setColorAt(0.0, QColor(255, 255, 255, 235))
    inner = QColor(ACCENT); inner.setAlpha(220)
    outer = QColor(ACCENT); outer.setAlpha(0)
    grad.setColorAt(0.4, inner)
    grad.setColorAt(1.0, outer)
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(c, c), R * 0.55, R * 0.55)

    # outer ring
    pen = QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 230))
    pen.setWidthF(max(2.0, size / 28))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(c, c), R, R)

    # three arc segments
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    rect = pm.rect().adjusted(4, 4, -4, -4)
    for start in (30, 150, 270):
        p.drawArc(rect, start * 16, 50 * 16)
    p.end()
    return QIcon(pm)


def create_tray(app, on_quit, on_toggle_pause=None, start_paused=False,
                on_listen_now=None, hotkey_label="") -> QSystemTrayIcon:
    tray = QSystemTrayIcon(make_reactor_icon(), parent=app)

    menu = QMenu()

    if on_listen_now is not None:
        # Same door as the push-to-talk hotkey, for when the mic is a bad idea.
        listen_action = QAction("Listen now", menu)
        if hotkey_label:
            listen_action.setText(f"Listen now\t{hotkey_label}")
        listen_action.triggered.connect(lambda: on_listen_now())
        menu.addAction(listen_action)
        menu.addSeparator()

    pause_action = QAction("Pause listening", menu)
    pause_action.setCheckable(True)

    def _on_pause(checked: bool) -> None:
        pause_action.setText("Resume listening" if checked else "Pause listening")
        tray.setToolTip("Stark - paused" if checked else 'Stark - say "Hey Stark"')
        if on_toggle_pause is not None:
            on_toggle_pause(checked)

    pause_action.toggled.connect(_on_pause)
    # Reflect the remembered pause state (also sets the label + tooltip).
    pause_action.setChecked(start_paused)
    if not start_paused:
        tray.setToolTip('Stark - say "Hey Stark"')
    menu.addAction(pause_action)

    menu.addSeparator()

    quit_action = QAction("Quit Stark", menu)
    quit_action.triggered.connect(on_quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    return tray
