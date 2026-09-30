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


def icon(name: str) -> QIcon:
    """A qtawesome icon coloured for the current (light or dark) palette."""
    try:
        import qtawesome as qta

        color = QApplication.palette().color(QPalette.ButtonText)
        return qta.icon(name, color=color)
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
