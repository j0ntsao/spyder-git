"""Small helpers shared by the widgets."""

from __future__ import annotations

from datetime import datetime, timezone

from qtpy.QtGui import QIcon, QPalette
from qtpy.QtWidgets import QApplication

try:  # inside Spyder
    from spyder.api.translations import _
except Exception:  # standalone / tests

    def _(text):
        return text


def _icon_colors():
    """(normal, disabled) icon colors that match the active theme."""
    try:  # inside Spyder: use its theme-aware palette
        from spyder.utils.palette import SpyderPalette

        return SpyderPalette.ICON_1, SpyderPalette.COLOR_DISABLED
    except Exception:  # standalone: fall back to Qt's palette
        pal = QApplication.palette()
        return pal.color(QPalette.ButtonText), pal.color(QPalette.Disabled, QPalette.ButtonText)


def icon(name: str) -> QIcon:
    """A qtawesome icon coloured for the current (light or dark) theme."""
    try:
        import qtawesome as qta

        color, disabled = _icon_colors()
        return qta.icon(name, color=color, color_disabled=disabled)
    except Exception:
        return QIcon()


def relative_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return _("just now")
    if secs < 3600:
        return _("{n} min ago").format(n=int(secs // 60))
    if secs < 86400:
        return _("{n} h ago").format(n=int(secs // 3600))
    if secs < 30 * 86400:
        return _("{n} d ago").format(n=int(secs // 86400))
    return dt.astimezone().strftime("%Y-%m-%d")
