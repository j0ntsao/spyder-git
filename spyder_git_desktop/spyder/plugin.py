"""Spyder 6 plugin entry point."""

import os

from qtpy.QtGui import QIcon

from spyder.api.plugin_registration.decorators import (
    on_plugin_available,
    on_plugin_teardown,
)
from spyder.api.plugins import Plugins, SpyderDockablePlugin
from spyder.api.translations import _

from spyder_git_desktop.spyder.widgets import GitDesktopWidget
from spyder_git_desktop.utils import icon


class GitDesktop(SpyderDockablePlugin):
    NAME = "git_desktop"
    REQUIRES = []
    OPTIONAL = [Plugins.Editor, Plugins.Projects]
    TABIFY = [Plugins.Help]
    WIDGET_CLASS = GitDesktopWidget
    CONF_SECTION = NAME
    CONF_FILE = False

    # -- SpyderPluginV2 API ------------------------------------------------- #
    @staticmethod
    def get_name():
        return _("Git Desktop")

    def get_description(self):
        return _("A GitHub Desktop-style Git client: changes, commits, history, branches and sync.")

    def get_icon(self):
        ico = icon("mdi.source-branch")
        return ico if not ico.isNull() else QIcon()

    def on_initialize(self):
        widget = self.get_widget()
        widget.panel.sig_open_file.connect(self._open_file)
        widget.panel.context_provider = self._current_editor_file

    def on_close(self, cancellable=True):
        self.get_widget().panel.shutdown()
        return True

    # -- Editor ------------------------------------------------------------- #
    @on_plugin_available(plugin=Plugins.Editor)
    def on_editor_available(self):
        editor = self.get_plugin(Plugins.Editor)
        signal = getattr(editor, "sig_file_opened_closed_or_updated", None)
        if signal is not None:
            signal.connect(self._on_editor_file_changed)

    @on_plugin_teardown(plugin=Plugins.Editor)
    def on_editor_teardown(self):
        editor = self.get_plugin(Plugins.Editor)
        signal = getattr(editor, "sig_file_opened_closed_or_updated", None)
        if signal is not None:
            try:
                signal.disconnect(self._on_editor_file_changed)
            except (TypeError, RuntimeError):
                pass

    def _on_editor_file_changed(self, filename, _language=None):
        if filename and os.path.exists(filename):
            self.get_widget().panel.set_context_dir(filename)

    def _current_editor_file(self):
        editor = self.get_plugin(Plugins.Editor, error=False)
        if editor is None:
            return None
        try:
            filename = editor.get_current_filename()
        except Exception:
            return None
        return filename if filename and os.path.exists(filename) else None

    def _open_file(self, path):
        editor = self.get_plugin(Plugins.Editor, error=False)
        if editor is not None:
            editor.load(path)

    # -- Projects ----------------------------------------------------------- #
    @on_plugin_available(plugin=Plugins.Projects)
    def on_projects_available(self):
        projects = self.get_plugin(Plugins.Projects)
        projects.sig_project_loaded.connect(self._on_project_loaded)
        projects.sig_project_closed.connect(self._on_project_closed)
        path = projects.get_active_project_path()
        if path:
            self._on_project_loaded(path)

    @on_plugin_teardown(plugin=Plugins.Projects)
    def on_projects_teardown(self):
        projects = self.get_plugin(Plugins.Projects)
        for sig, slot in (
            (projects.sig_project_loaded, self._on_project_loaded),
            (projects.sig_project_closed, self._on_project_closed),
        ):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_project_loaded(self, path):
        self.get_widget().panel.set_project_dir(path)

    def _on_project_closed(self, _path=None):
        self.get_widget().panel.set_project_dir(None)
