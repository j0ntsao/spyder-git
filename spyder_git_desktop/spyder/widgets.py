"""Spyder ``PluginMainWidget`` that hosts the Qt-only :class:`GitPanel`."""

from qtpy.QtWidgets import QVBoxLayout

from spyder.api.translations import _
from spyder.api.widgets.main_widget import PluginMainWidget

from spyder_git_desktop.panel import GitPanel
from spyder_git_desktop.utils import icon


class GitDesktopActions:
    Refresh = "git_desktop_refresh"
    Fetch = "git_desktop_fetch"
    Choose = "git_desktop_choose_repository"
    Clone = "git_desktop_clone_repository"
    OpenFolder = "git_desktop_open_folder"
    AddRemote = "git_desktop_add_remote"
    Identity = "git_desktop_identity"
    GitHub = "git_desktop_github"


class GitDesktopMenuSections:
    Repository = "repository_section"
    Config = "config_section"


class GitDesktopWidget(PluginMainWidget):
    ENABLE_SPINNER = True

    def __init__(self, name, plugin, parent=None):
        super().__init__(name, plugin, parent)
        self.panel = GitPanel(self)
        self.panel.sig_busy_changed.connect(self._on_busy_changed)

    # -- PluginMainWidget API ---------------------------------------------- #
    def get_title(self):
        return _("Git Desktop")

    def get_focus_widget(self):
        return self.panel

    def setup(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel)
        self.setLayout(layout)

        panel = self.panel
        refresh = self.create_action(
            GitDesktopActions.Refresh, text=_("Refresh"),
            icon=icon("mdi.refresh"), triggered=lambda: panel.refresh(),
        )
        fetch = self.create_action(
            GitDesktopActions.Fetch, text=_("Fetch"),
            icon=icon("mdi.sync"), triggered=lambda: panel.fetch(),
        )
        choose = self.create_action(
            GitDesktopActions.Choose, text=_("Choose repository…"),
            triggered=lambda: panel.choose_repository(),
        )
        clone = self.create_action(
            GitDesktopActions.Clone, text=_("Clone repository…"),
            triggered=lambda: panel.clone_repository(),
        )
        open_folder = self.create_action(
            GitDesktopActions.OpenFolder, text=_("Open repository folder"),
            triggered=lambda: panel.open_repository_folder(),
        )
        add_remote = self.create_action(
            GitDesktopActions.AddRemote, text=_("Add remote…"),
            triggered=lambda: panel.add_remote(),
        )
        identity = self.create_action(
            GitDesktopActions.Identity, text=_("Configure Git identity…"),
            triggered=lambda: panel.configure_identity(),
        )

        github = self.create_action(
            GitDesktopActions.GitHub, text=_("Sign in / out of GitHub…"),
            triggered=lambda: panel.github_action(),
        )

        toolbar = self.get_main_toolbar()
        for action in (refresh, fetch):
            self.add_item_to_toolbar(action, toolbar=toolbar, section="main")

        menu = self.get_options_menu()
        for action in (choose, clone, open_folder):
            self.add_item_to_menu(action, menu=menu, section=GitDesktopMenuSections.Repository)
        for action in (add_remote, identity, github):
            self.add_item_to_menu(action, menu=menu, section=GitDesktopMenuSections.Config)

    def update_actions(self):
        pass

    # -- helpers ------------------------------------------------------------ #
    def _on_busy_changed(self, busy):
        if busy:
            self.start_spinner()
        else:
            self.stop_spinner()
