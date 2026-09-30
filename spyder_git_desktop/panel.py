"""GitPanel: a GitHub-Desktop-style UI, independent from Spyder's plugin API.

The panel can be run on its own for debugging:

    python -m spyder_git_desktop.panel /path/to/repo
"""

from __future__ import annotations

import html
import os
import sys
import threading
import time
from functools import partial

from qtpy.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from qtpy.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFontDatabase,
    QPalette,
    QTextCursor,
    QTextFormat,
)
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import github_auth
from .git_backend import GitError, GitRepo, git_executable, set_github_token
from .utils import _, icon, relative_time

STATUS_COLORS = {
    "A": "#2ea043",
    "M": "#d29922",
    "D": "#f85149",
    "R": "#388bfd",
    "U": "#a371f7",
}


# --------------------------------------------------------------------------- #
# Background execution (one git command at a time, so index.lock never clashes)
# --------------------------------------------------------------------------- #
class _Job(QRunnable):
    def __init__(self, runner, job_id, fn):
        super().__init__()
        self._runner, self._id, self._fn = runner, job_id, fn

    def run(self):
        try:
            result, error = self._fn(), None
        except Exception as exc:  # noqa: BLE001 - forwarded to the UI thread
            result, error = None, exc
        self._runner.sig_done.emit(self._id, result, error)


class TaskRunner(QObject):
    sig_done = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._next = 0
        self._callbacks = {}
        self.sig_done.connect(self._dispatch)

    def submit(self, fn, callback=None):
        self._next += 1
        self._callbacks[self._next] = callback
        self._pool.start(_Job(self, self._next, fn))
        return self._next

    def _dispatch(self, job_id, result, error):
        callback = self._callbacks.pop(job_id, None)
        if callback is not None:
            callback(result, error)

    def wait(self, msecs=3000):
        self._pool.waitForDone(msecs)


# --------------------------------------------------------------------------- #
# Diff viewer
# --------------------------------------------------------------------------- #
class DiffView(QPlainTextEdit):
    MAX_LINES = 4000

    ADD = QColor(46, 160, 67, 70)
    DEL = QColor(248, 81, 73, 70)
    HUNK = QColor(56, 139, 253, 55)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

    def set_diff(self, text: str):
        lines = text.splitlines()
        # Drop the "diff --git / index / ---/+++" preamble: what matters are hunks.
        for i, line in enumerate(lines):
            if line.startswith("@@"):
                lines = lines[i:]
                break
        truncated = len(lines) > self.MAX_LINES
        if truncated:
            lines = lines[: self.MAX_LINES] + [
                "",
                _("… diff truncated ({n} lines shown)").format(n=self.MAX_LINES),
            ]
        self.setPlainText("\n".join(lines))
        selections = []
        block = self.document().firstBlock()
        while block.isValid():
            t = block.text()
            color = None
            if t.startswith("@@"):
                color = self.HUNK
            elif t.startswith("+"):
                color = self.ADD
            elif t.startswith("-"):
                color = self.DEL
            if color is not None:
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(color)
                sel.format.setProperty(QTextFormat.FullWidthSelection, True)
                sel.cursor = QTextCursor(block)
                sel.cursor.clearSelection()
                selections.append(sel)
            block = block.next()
        self.setExtraSelections(selections)

    def clear_diff(self, message: str = ""):
        self.setExtraSelections([])
        self.setPlainText(message)


# --------------------------------------------------------------------------- #
# Identity dialog
# --------------------------------------------------------------------------- #
class IdentityDialog(QDialog):
    def __init__(self, name="", email="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Configure Git identity"))
        form = QFormLayout(self)
        self.name_edit = QLineEdit(name)
        self.email_edit = QLineEdit(email)
        self.global_box = QCheckBox(_("Use for all repositories (--global)"))
        self.global_box.setChecked(True)
        form.addRow(_("Name"), self.name_edit)
        form.addRow(_("Email"), self.email_edit)
        form.addRow(self.global_box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.email_edit.text().strip(),
            self.global_box.isChecked(),
        )


class ClientIdDialog(QDialog):
    """One-time setup: the Client ID of a GitHub OAuth App with Device Flow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Set up GitHub sign-in"))
        layout = QVBoxLayout(self)
        info = QLabel(
            _(
                "<p>To sign in with your browser, this plugin needs a GitHub "
                "OAuth App. GitHub Desktop ships its own; you register yours "
                "once (about a minute):</p>"
                "<ol>"
                "<li>Open <a href='https://github.com/settings/applications/new'>"
                "github.com/settings/applications/new</a></li>"
                "<li>Any name; Homepage URL <code>https://github.com</code>; "
                "Callback URL <code>http://localhost</code></li>"
                "<li>After creating it, tick <b>Enable Device Flow</b> and save</li>"
                "<li>Paste its <b>Client ID</b> below (it is public, not a secret)</li>"
                "</ol>"
            )
        )
        info.setTextFormat(Qt.RichText)
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Client ID")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(info)
        layout.addWidget(self.edit)
        layout.addWidget(buttons)
        self.setMinimumWidth(440)

    def _accept(self):
        if self.edit.text().strip():
            self.accept()

    def client_id(self):
        return self.edit.text().strip()


class DeviceCodeDialog(QDialog):
    def __init__(self, code, url, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Sign in to GitHub"))
        self._url = url
        layout = QVBoxLayout(self)
        label = QLabel(
            _("Your browser was opened. Enter this code on GitHub to authorize Spyder (it is also copied to your clipboard):")
        )
        label.setWordWrap(True)
        self.code_label = QLabel(f"<h1><code>{html.escape(code)}</code></h1>")
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        waiting = QLabel(_("Waiting for authorization…"))
        waiting.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        open_btn = QPushButton(_("Open GitHub again"))
        open_btn.clicked.connect(lambda *_a: QDesktopServices.openUrl(QUrl(self._url)))
        cancel = QPushButton(_("Cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(open_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        for w in (label, self.code_label, waiting):
            layout.addWidget(w)
        layout.addLayout(row)
        self.setMinimumWidth(380)


# --------------------------------------------------------------------------- #
# The panel
# --------------------------------------------------------------------------- #
class GitPanel(QWidget):
    sig_open_file = Signal(str)
    sig_busy_changed = Signal(bool)

    POLL_MS = 4000
    AUTO_FETCH_MS = 5 * 60 * 1000
    HISTORY_LIMIT = 300
    WIDE_WIDTH = 760

    def __init__(self, parent=None):
        super().__init__(parent)
        #: optional callable returning the file currently open in the editor
        self.context_provider = None

        self._tasks = TaskRunner(self)
        self._repo = None
        self._snapshot = None
        self._manual_dir = None
        self._project_dir = None
        self._context_dir = None
        self._empty_dir = None
        self._unchecked = set()
        self._busy = 0
        self._refreshing = False
        self._refresh_again = False
        self._history_key = None
        self._last_fetch = None
        self._merge_prefilled = False
        self._github_user = None
        self._auth_cancel = None
        self._device_dialog = None
        self._pending_retry = None
        self._last_op = None
        self._auth_tasks = TaskRunner(self)  # separate thread: polling blocks

        self._build_ui()
        self._auth_tasks.submit(github_auth.load_token, self._on_token_loaded)
        self._show_empty(_("Open a Spyder project or a file that lives inside a Git repository, or choose a repository."))

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(self.POLL_MS)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.timeout.connect(self._auto_fetch)
        self._fetch_timer.start(self.AUTO_FETCH_MS)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- top bar: repository | branch | sync ----------------------
        bar = QHBoxLayout()
        self.repo_btn = self._tool_button(_("Repository"), "mdi.folder-outline", menu=True)
        self.repo_menu = QMenu(self)
        self.repo_menu.addAction(icon("mdi.folder-search-outline"), _("Choose repository…"), self.choose_repository)
        self.repo_menu.addAction(_("Clone repository…"), self.clone_repository)
        self.repo_menu.addSeparator()
        self.act_open_folder = self.repo_menu.addAction(_("Open repository folder"), self.open_repository_folder)
        self.act_add_remote = self.repo_menu.addAction(_("Add remote…"), self.add_remote)
        self.act_identity = self.repo_menu.addAction(_("Configure Git identity…"), self.configure_identity)
        self.repo_menu.addSeparator()
        self.act_github = self.repo_menu.addAction(_("Sign in to GitHub…"), self.github_action)
        self.repo_btn.setMenu(self.repo_menu)

        self.branch_btn = self._tool_button(_("Branch"), "mdi.source-branch", menu=True)
        self.branch_menu = QMenu(self)
        self.branch_menu.aboutToShow.connect(self._populate_branch_menu)
        self.branch_btn.setMenu(self.branch_menu)

        self.sync_btn = self._tool_button(_("Fetch"), "mdi.sync")
        self.sync_btn.clicked.connect(self._sync)

        bar.addWidget(self.repo_btn, 1)
        bar.addWidget(self.branch_btn, 1)
        bar.addWidget(self.sync_btn, 0)
        root.addLayout(bar)

        # ---- stacked: empty state / main ------------------------------
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(1)
        self.empty_label = QLabel()
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        el.addWidget(self.empty_label)
        self.init_btn = QPushButton(_("Create a repository here"))
        self.init_btn.clicked.connect(self._init_repository)
        choose = QPushButton(_("Choose repository…"))
        choose.clicked.connect(self.choose_repository)
        el.addWidget(self.init_btn, 0, Qt.AlignCenter)
        el.addWidget(choose, 0, Qt.AlignCenter)
        el.addStretch(1)
        self.stack.addWidget(empty)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_changes_tab(), _("Changes"))
        self.tabs.addTab(self._build_history_tab(), _("History"))
        self.stack.addWidget(self.tabs)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def _tool_button(self, text, icon_name, menu=False):
        btn = QToolButton()
        btn.setText(text)
        btn.setIcon(icon(icon_name))
        btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        if menu:
            btn.setPopupMode(QToolButton.InstantPopup)
        return btn

    def _file_tree(self):
        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(True)
        tree.setTextElideMode(Qt.ElideLeft)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        header = tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        return tree

    def _build_changes_tab(self):
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        self.merge_banner = QFrame()
        self.merge_banner.setFrameShape(QFrame.StyledPanel)
        mb = QHBoxLayout(self.merge_banner)
        self.merge_label = QLabel()
        self.merge_label.setWordWrap(True)
        abort = QPushButton(_("Abort merge"))
        abort.clicked.connect(self._abort_merge)
        mb.addWidget(self.merge_label, 1)
        mb.addWidget(abort)
        self.merge_banner.hide()
        ll.addWidget(self.merge_banner)

        self.chk_all = QCheckBox()
        self.chk_all.clicked.connect(self._toggle_all)
        ll.addWidget(self.chk_all)

        self.changes_tree = self._file_tree()
        self.changes_tree.itemChanged.connect(self._on_item_changed)
        self.changes_tree.currentItemChanged.connect(self._on_change_selected)
        self.changes_tree.itemDoubleClicked.connect(self._open_change)
        self.changes_tree.customContextMenuRequested.connect(self._changes_menu)
        ll.addWidget(self.changes_tree, 1)

        self.summary_edit = QLineEdit()
        self.summary_edit.setPlaceholderText(_("Summary (required)"))
        self.summary_edit.textChanged.connect(self._update_commit_state)
        self.summary_edit.returnPressed.connect(self._commit_if_enabled)
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText(_("Description"))
        self.desc_edit.setMaximumHeight(80)
        self.commit_btn = QPushButton()
        self.commit_btn.setIcon(icon("mdi.source-commit"))
        self.commit_btn.clicked.connect(self._commit)
        ll.addWidget(self.summary_edit)
        ll.addWidget(self.desc_edit)
        ll.addWidget(self.commit_btn)

        self.diff_view = DiffView()
        self.diff_view.setPlaceholderText(_("Select a file to see its changes."))

        self.changes_split = QSplitter(Qt.Horizontal)
        self.changes_split.addWidget(left)
        self.changes_split.addWidget(self.diff_view)
        self.changes_split.setStretchFactor(0, 1)
        self.changes_split.setStretchFactor(1, 2)
        self.changes_split.setChildrenCollapsible(False)
        return self.changes_split

    def _build_history_tab(self):
        self.history_tree = QTreeWidget()
        self.history_tree.setColumnCount(3)
        self.history_tree.setHeaderLabels([_("Commit"), _("Author"), _("Date")])
        self.history_tree.setRootIsDecorated(False)
        self.history_tree.setUniformRowHeights(True)
        self.history_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        header = self.history_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.history_tree.currentItemChanged.connect(self._on_commit_selected)
        self.history_tree.customContextMenuRequested.connect(self._history_menu)

        self.commit_info = QLabel()
        self.commit_info.setWordWrap(True)
        self.commit_info.setTextFormat(Qt.RichText)
        self.commit_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.commit_files_tree = self._file_tree()
        self.commit_files_tree.currentItemChanged.connect(self._on_commit_file_selected)
        self.commit_files_tree.itemDoubleClicked.connect(self._open_commit_file)
        self.history_diff = DiffView()

        files_box = QWidget()
        fl = QVBoxLayout(files_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self.commit_info)
        fl.addWidget(self.commit_files_tree, 1)

        inner = QSplitter(Qt.Vertical)
        inner.addWidget(files_box)
        inner.addWidget(self.history_diff)
        inner.setChildrenCollapsible(False)

        self.history_split = QSplitter(Qt.Horizontal)
        self.history_split.addWidget(self.history_tree)
        self.history_split.addWidget(inner)
        self.history_split.setChildrenCollapsible(False)
        return self.history_split

    def resizeEvent(self, event):
        super().resizeEvent(event)
        orient = Qt.Horizontal if self.width() >= self.WIDE_WIDTH else Qt.Vertical
        for splitter in (self.changes_split, self.history_split):
            if splitter.orientation() != orient:
                splitter.setOrientation(orient)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._poll_context)
        if self._repo is not None:
            self.refresh(refs=False)

    # ------------------------------------------------------------------ #
    # Public API (used by the Spyder plugin)
    # ------------------------------------------------------------------ #
    def set_project_dir(self, path):
        self._project_dir = path or None
        self._manual_dir = None
        self.resolve_repository()

    def set_context_dir(self, path):
        """Directory (or file) that hints at which repo to show (editor file)."""
        if path and os.path.isfile(path):
            path = os.path.dirname(path)
        if path == self._context_dir:
            return
        self._context_dir = path or None
        if not self._project_dir and not self._manual_dir:
            self.resolve_repository()

    def shutdown(self):
        if self._auth_cancel is not None:
            self._auth_cancel.set()
        self._poll_timer.stop()
        self._fetch_timer.stop()
        self._tasks.wait()
        self._auth_tasks.wait(1000)

    def refresh(self, refs=True):
        if self._repo is None:
            self.resolve_repository()
            return
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        repo = self._repo
        include_refs = refs or self._snapshot is None or self._snapshot.local_branches is None
        self._tasks.submit(
            lambda: repo.snapshot(include_refs=include_refs),
            partial(self._on_snapshot, repo),
        )

    def fetch(self):
        self._run_op(_("Fetching…"), lambda repo: repo.fetch(), ok=self._mark_fetched)

    def choose_repository(self, *_args):
        start = self._repo.root if self._repo else (self._candidate_dir() or os.path.expanduser("~"))
        path = QFileDialog.getExistingDirectory(self, _("Choose repository folder"), start)
        if not path:
            return
        self._manual_dir = path
        self.resolve_repository()

    def clone_repository(self, *_args):
        url, ok = QInputDialog.getText(self, _("Clone repository"), _("Repository URL:"))
        if not ok or not url.strip():
            return
        parent = QFileDialog.getExistingDirectory(self, _("Clone into folder"), os.path.expanduser("~"))
        if not parent:
            return
        stem = url.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        dest = os.path.join(parent, stem[:-4] if stem.endswith(".git") else stem)
        self._begin_busy(_("Cloning…"))
        self._tasks.submit(lambda: GitRepo.clone(url.strip(), dest), self._on_cloned)

    def open_repository_folder(self, *_args):
        if self._repo:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._repo.root))

    def add_remote(self, *_args):
        if not self._repo:
            return
        url, ok = QInputDialog.getText(
            self, _("Add remote"), _("URL of the remote (it will be named 'origin'):")
        )
        if not ok or not url.strip():
            return
        name = "origin" if "origin" not in (self._snapshot.remotes or []) else "upstream"
        self._run_op(_("Adding remote…"), lambda repo: repo.add_remote(name, url.strip()))

    def configure_identity(self, *_args, then=None):
        if not self._repo:
            return
        name, email = self._repo.identity()
        dlg = IdentityDialog(name, email, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        name, email, global_ = dlg.values()
        if not name or not email:
            return
        self._run_op(
            _("Saving identity…"),
            lambda repo: repo.set_identity(name, email, global_),
            ok=(lambda _r: then()) if then else None,
        )

    # ------------------------------------------------------------------ #
    # GitHub sign-in (OAuth device flow)
    # ------------------------------------------------------------------ #
    def github_action(self, *_args):
        if self._github_user:
            self.sign_out_github()
        else:
            self.sign_in_github()

    def _update_github_action(self):
        self.act_github.setText(
            _("Sign out of GitHub ({u})").format(u=self._github_user)
            if self._github_user
            else _("Sign in to GitHub…")
        )

    def _on_token_loaded(self, result, error):
        if error is None and result:
            login, token = result
            set_github_token(token)
            self._github_user = login
            self._update_github_action()

    def sign_in_github(self, retry=None):
        if self._auth_cancel is not None:
            return  # already in progress
        client_id = github_auth.get_client_id()
        if not client_id:
            dlg = ClientIdDialog(self)
            if dlg.exec_() != QDialog.Accepted:
                return
            client_id = dlg.client_id()
            github_auth.save_client_id(client_id)
        self._pending_retry = retry
        self.status_label.setText(_("Contacting GitHub…"))
        self._auth_tasks.submit(
            lambda: github_auth.request_device_code(client_id),
            partial(self._on_device_code, client_id),
        )

    def _on_device_code(self, client_id, device, error):
        self.status_label.setText("")
        if error is not None:
            self._auth_failed(error)
            return
        cancel = threading.Event()
        self._auth_cancel = cancel
        dlg = DeviceCodeDialog(device["user_code"], device["verification_uri"], self)
        dlg.rejected.connect(cancel.set)
        self._device_dialog = dlg
        QApplication.clipboard().setText(device["user_code"])
        QDesktopServices.openUrl(QUrl(device["verification_uri"]))
        dlg.show()
        self._auth_tasks.submit(
            lambda: github_auth.wait_for_token(client_id, device, cancel),
            self._on_auth_done,
        )

    def _on_auth_done(self, result, error):
        dlg, self._device_dialog = self._device_dialog, None
        self._auth_cancel = None
        if dlg is not None:
            try:
                dlg.rejected.disconnect()
            except (TypeError, RuntimeError):
                pass
            dlg.close()
        retry, self._pending_retry = self._pending_retry, None
        if error is not None:
            if not getattr(error, "cancelled", False):
                self._auth_failed(error)
            return
        login, token = result
        set_github_token(token)
        self._github_user = login
        self._update_github_action()
        self.status_label.setText(_("Signed in to GitHub as {u}.").format(u=login))
        if retry is not None:
            label, fn, ok, fail = retry
            self._run_op(label, fn, ok=ok, fail=fail)

    def _auth_failed(self, error):
        code = getattr(error, "code", None)
        if code in ("incorrect_client_credentials", "device_flow_disabled"):
            if code == "incorrect_client_credentials":
                github_auth.save_client_id("")  # ask again next time
        QMessageBox.warning(self, _("GitHub sign-in failed"), str(error))

    def sign_out_github(self):
        set_github_token(None)
        self._github_user = None
        self._update_github_action()
        self._auth_tasks.submit(github_auth.clear_token)
        self.status_label.setText(_("Signed out of GitHub."))

    # ------------------------------------------------------------------ #
    # Repository discovery
    # ------------------------------------------------------------------ #
    def _candidate_dir(self):
        return self._manual_dir or self._project_dir or self._context_dir

    def resolve_repository(self):
        if git_executable() is None:
            self._show_empty(_("Git was not found on your PATH. Install Git and restart Spyder."))
            return
        directory = self._candidate_dir()
        if not directory:
            if self._repo is None:
                self._show_empty(_("Open a Spyder project or a file that lives inside a Git repository, or choose a repository."))
            return
        self._tasks.submit(
            lambda: GitRepo.discover(directory), partial(self._on_discovered, directory)
        )

    def _on_discovered(self, directory, repo, error):
        if directory != self._candidate_dir():
            return  # stale
        if error is not None:
            self._show_empty(str(error))
            return
        if repo is None:
            self._empty_dir = directory
            self._show_empty(
                _("“{name}” is not a Git repository.").format(name=os.path.basename(directory) or directory),
                allow_init=True,
            )
            return
        same = self._repo is not None and os.path.normcase(self._repo.root) == os.path.normcase(repo.root)
        if same:
            return
        self._repo = repo
        self._snapshot = None
        self._unchecked.clear()
        self._history_key = None
        self._merge_prefilled = False
        self.summary_edit.clear()
        self.desc_edit.clear()
        self.repo_btn.setText(repo.name)
        self.repo_btn.setToolTip(repo.root)
        self.stack.setCurrentIndex(1)
        self._set_controls_enabled(True)
        self.refresh(refs=True)

    def _show_empty(self, message, allow_init=False):
        self._repo = None
        self._snapshot = None
        self.empty_label.setText(message)
        self.init_btn.setVisible(allow_init)
        self.stack.setCurrentIndex(0)
        self.repo_btn.setText(_("Repository"))
        self.repo_btn.setToolTip("")
        self.branch_btn.setText(_("Branch"))
        self.sync_btn.setText(_("Fetch"))
        self._set_controls_enabled(False)

    def _set_controls_enabled(self, on):
        self.branch_btn.setEnabled(on and not self._busy)
        self.sync_btn.setEnabled(on and not self._busy)
        for act in (self.act_open_folder, self.act_add_remote, self.act_identity):
            act.setEnabled(on)

    def _init_repository(self):
        directory = self._empty_dir
        if not directory:
            return
        self._begin_busy(_("Creating repository…"))
        self._tasks.submit(lambda: GitRepo.init(directory), self._on_initialized)

    def _on_initialized(self, repo, error):
        self._end_busy()
        if error is not None:
            self._show_error(error)
            return
        self._manual_dir = repo.root
        self.resolve_repository()

    def _on_cloned(self, repo, error):
        self._end_busy()
        if error is not None:
            self._show_error(error, _("Clone failed"))
            return
        self._manual_dir = repo.root
        self.resolve_repository()

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    def _poll_context(self):
        if self.context_provider is None:
            return
        try:
            path = self.context_provider()
        except Exception:  # noqa: BLE001
            return
        if path:
            self.set_context_dir(path)

    def _poll(self):
        if not self.isVisible() or self._busy:
            return
        if QApplication.applicationState() != Qt.ApplicationActive:
            return
        self._poll_context()
        if self._repo is not None:
            self.refresh(refs=False)

    def _auto_fetch(self):
        snap = self._snapshot
        if self._repo is None or self._busy or snap is None or not snap.remotes:
            return
        self._run_op(None, lambda repo: repo.fetch(), ok=self._mark_fetched, silent=True)

    def _mark_fetched(self, _result=None):
        self._last_fetch = time.time()

    # ------------------------------------------------------------------ #
    # Snapshot handling
    # ------------------------------------------------------------------ #
    def _on_snapshot(self, repo, snap, error):
        self._refreshing = False
        if repo is not self._repo:
            return
        if error is not None:
            self.status_label.setText(str(error).splitlines()[0] if str(error) else "")
            return
        old = self._snapshot
        if snap.local_branches is None and old is not None:
            snap.local_branches = old.local_branches
            snap.remote_branches = old.remote_branches
            snap.remotes = old.remotes
        self._snapshot = snap

        self._update_toolbar()
        if old is None or old.status.changes != snap.status.changes:
            self._populate_changes()
        self._update_merge_banner()
        self._update_commit_state()

        key = (snap.status.oid, snap.status.ahead)
        if key != self._history_key:
            self._load_history()

        if self._refresh_again:
            self._refresh_again = False
            self.refresh(refs=False)

    def _update_toolbar(self):
        snap = self._snapshot
        st = snap.status
        self.branch_btn.setText(st.branch or _("(detached HEAD)"))
        text, tip, icon_name, enabled = self._sync_state()
        self.sync_btn.setText(text)
        self.sync_btn.setIcon(icon(icon_name))
        if self._last_fetch:
            tip += "\n" + _("Last fetched {t}").format(t=relative_time_from_epoch(self._last_fetch))
        self.sync_btn.setToolTip(tip)
        self.sync_btn.setEnabled(enabled and not self._busy)

    def _sync_state(self):
        """Returns (text, tooltip, icon, enabled) for the context-sensitive button."""
        snap = self._snapshot
        st = snap.status
        if not snap.remotes:
            return _("Add remote…"), _("This repository has no remote"), "mdi.cloud-plus-outline", True
        if st.unborn:
            return _("Fetch"), _("Make a first commit before publishing"), "mdi.sync", False
        if st.detached:
            return _("Fetch"), _("Fetch from the remotes"), "mdi.sync", True
        if st.upstream is None:
            return _("Publish branch"), _("Push this branch to the remote and track it"), "mdi.cloud-upload-outline", True
        if st.behind:
            text = _("Pull ↓{n}").format(n=st.behind)
            if st.ahead:
                text += f" ↑{st.ahead}"
            return text, _("Pull {u}").format(u=st.upstream), "mdi.arrow-down", True
        if st.ahead:
            return _("Push ↑{n}").format(n=st.ahead), _("Push to {u}").format(u=st.upstream), "mdi.arrow-up", True
        return _("Fetch"), _("Fetch from the remotes"), "mdi.sync", True

    def _update_merge_banner(self):
        snap = self._snapshot
        if not snap.merging:
            self.merge_banner.hide()
            self._merge_prefilled = False
            return
        conflicts = sum(1 for c in snap.status.changes if c.conflicted)
        if conflicts:
            self.merge_label.setText(
                _("Merge in progress: {n} file(s) have conflicts. Resolve them in the editor, then commit.").format(n=conflicts)
            )
        else:
            self.merge_label.setText(_("Merge in progress: all conflicts resolved. Commit to finish the merge."))
        self.merge_banner.show()
        if not self._merge_prefilled and not self.summary_edit.text():
            self.summary_edit.setText(self._repo.merge_message())
            self._merge_prefilled = True

    # ------------------------------------------------------------------ #
    # Changes tab
    # ------------------------------------------------------------------ #
    def _populate_changes(self):
        changes = self._snapshot.status.changes
        tree = self.changes_tree
        current = tree.currentItem()
        current_path = current.data(0, Qt.UserRole).path if current else None

        self._unchecked &= {c.path for c in changes}
        tree.blockSignals(True)
        tree.clear()
        restore = None
        for c in sorted(changes, key=lambda c: c.path.lower()):
            label = f"{c.orig_path} → {c.path}" if c.orig_path else c.path
            item = QTreeWidgetItem([label, c.code])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked if c.path in self._unchecked else Qt.Checked)
            item.setData(0, Qt.UserRole, c)
            item.setToolTip(0, c.path)
            item.setForeground(1, QBrush(QColor(STATUS_COLORS[c.code])))
            item.setTextAlignment(1, Qt.AlignCenter)
            tree.addTopLevelItem(item)
            if c.path == current_path:
                restore = item
        tree.blockSignals(False)

        self._refresh_check_all()
        self.tabs.setTabText(0, _("Changes ({n})").format(n=len(changes)) if changes else _("Changes"))
        if restore is not None:
            tree.setCurrentItem(restore)
            self._on_change_selected(restore, None)  # diff may have changed
        elif changes:
            tree.setCurrentItem(tree.topLevelItem(0))
        else:
            self.diff_view.clear_diff()
            self.diff_view.setPlaceholderText(_("No local changes."))

    def _refresh_check_all(self):
        total = self.changes_tree.topLevelItemCount()
        checked = len(self._checked_changes())
        self.chk_all.blockSignals(True)
        self.chk_all.setEnabled(total > 0)
        self.chk_all.setChecked(total > 0 and checked == total)
        if total == 0:
            self.chk_all.setText(_("No changed files"))
        else:
            self.chk_all.setText(
                _("{c} of {t} changed files selected").format(c=checked, t=total)
                if checked != total
                else (_("{t} changed file").format(t=total) if total == 1 else _("{t} changed files").format(t=total))
            )
        self.chk_all.blockSignals(False)

    def _checked_changes(self):
        tree = self.changes_tree
        out = []
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                out.append(item.data(0, Qt.UserRole))
        return out

    def _toggle_all(self, checked):
        tree = self.changes_tree
        tree.blockSignals(True)
        self._unchecked.clear()
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            if not checked:
                self._unchecked.add(item.data(0, Qt.UserRole).path)
        tree.blockSignals(False)
        self._refresh_check_all()
        self._update_commit_state()

    def _on_item_changed(self, item, column):
        if column != 0:
            return
        path = item.data(0, Qt.UserRole).path
        if item.checkState(0) == Qt.Checked:
            self._unchecked.discard(path)
        else:
            self._unchecked.add(path)
        self._refresh_check_all()
        self._update_commit_state()

    def _on_change_selected(self, current, _previous):
        if current is None or self._repo is None:
            self.diff_view.clear_diff()
            return
        change = current.data(0, Qt.UserRole)
        repo = self._repo
        self._tasks.submit(
            lambda: repo.diff_working(change), partial(self._on_working_diff, repo, change.path)
        )

    def _on_working_diff(self, repo, path, text, error):
        cur = self.changes_tree.currentItem()
        if repo is not self._repo or cur is None or cur.data(0, Qt.UserRole).path != path:
            return
        if error is not None:
            self.diff_view.clear_diff(str(error))
        elif not text.strip():
            self.diff_view.clear_diff(_("(no textual changes)"))
        else:
            self.diff_view.set_diff(text)

    def _selected_changes(self):
        return [i.data(0, Qt.UserRole) for i in self.changes_tree.selectedItems()]

    def _open_change(self, item, _column=None):
        change = item.data(0, Qt.UserRole)
        full = os.path.join(self._repo.root, change.path)
        if os.path.isfile(full):
            self.sig_open_file.emit(full)

    def _changes_menu(self, pos):
        selected = self._selected_changes()
        if not selected:
            return
        menu = QMenu(self)
        menu.addAction(_("Open in editor"), partial(self._open_paths, [c.path for c in selected]))
        menu.addAction(_("Copy file path"), partial(self._copy_paths, [c.path for c in selected]))
        menu.addSeparator()
        menu.addAction(_("Discard changes…"), partial(self._discard, selected))
        untracked = [c for c in selected if c.untracked]
        if untracked:
            menu.addAction(_("Add to .gitignore"), partial(self._ignore, untracked))
        menu.exec_(self.changes_tree.viewport().mapToGlobal(pos))

    def _open_paths(self, paths, *_args):
        for p in paths:
            full = os.path.join(self._repo.root, p)
            if os.path.isfile(full):
                self.sig_open_file.emit(full)

    def _copy_paths(self, paths, *_args):
        QApplication.clipboard().setText("\n".join(os.path.join(self._repo.root, p) for p in paths))

    def _ignore(self, changes, *_args):
        self._run_op(_("Updating .gitignore…"), lambda repo: [repo.ignore(c.path) for c in changes])

    def _discard(self, changes, *_args):
        what = changes[0].path if len(changes) == 1 else _("{n} files").format(n=len(changes))
        if not self._confirm(
            _("Discard changes"),
            _("Discard all changes to {what}?\n\nThis cannot be undone.").format(what=what),
            _("Discard"),
        ):
            return
        self._run_op(_("Discarding…"), lambda repo: repo.discard(changes))

    # ------------------------------------------------------------------ #
    # Committing
    # ------------------------------------------------------------------ #
    def _update_commit_state(self, *_args):
        snap = self._snapshot
        if snap is None:
            self.commit_btn.setEnabled(False)
            self.commit_btn.setText(_("Commit"))
            return
        checked = self._checked_changes()
        has_summary = bool(self.summary_edit.text().strip())
        conflicts = any(c.conflicted for c in snap.status.changes)
        branch = snap.status.branch or _("(detached HEAD)")

        if snap.merging:
            self.commit_btn.setText(_("Commit merge to {b}").format(b=branch))
            ready = has_summary and not conflicts
        else:
            self.commit_btn.setText(
                _("Commit {n} file(s) to {b}").format(n=len(checked), b=branch)
                if checked
                else _("Commit to {b}").format(b=branch)
            )
            ready = has_summary and bool(checked) and not conflicts

        if conflicts:
            tip = _("Resolve merge conflicts first.")
        elif not has_summary:
            tip = _("Enter a summary.")
        elif not checked and not snap.merging:
            tip = _("Select at least one file.")
        else:
            tip = ""
        self.commit_btn.setToolTip(tip)
        self.commit_btn.setEnabled(bool(ready) and not self._busy)

    def _commit_if_enabled(self):
        if self.commit_btn.isEnabled():
            self._commit()

    def _commit(self):
        snap = self._snapshot
        summary = self.summary_edit.text().strip()
        description = self.desc_edit.toPlainText()
        paths = []
        for c in self._checked_changes():
            paths.append(c.path)
            if c.orig_path:
                paths.append(c.orig_path)

        def done(_r):
            self.summary_edit.clear()
            self.desc_edit.clear()
            self._unchecked.clear()
            self.tabs.setCurrentIndex(0)

        def failed(exc):
            if isinstance(exc, GitError) and exc.needs_identity:
                self.configure_identity(then=self._commit)
            else:
                self._show_error(exc, _("Commit failed"))

        self._run_op(
            _("Committing…"),
            lambda repo: repo.commit(paths, summary, description),
            ok=done,
            fail=failed,
        )

    # ------------------------------------------------------------------ #
    # Sync (fetch / pull / push / publish)
    # ------------------------------------------------------------------ #
    def _sync(self):
        snap = self._snapshot
        if snap is None:
            return
        st = snap.status
        if not snap.remotes:
            self.add_remote()
        elif st.detached or st.unborn:
            self.fetch()
        elif st.upstream is None:
            self._run_op(_("Publishing branch…"), lambda repo: repo.push(False), ok=self._mark_fetched)
        elif st.behind:
            self._run_op(_("Pulling…"), lambda repo: repo.pull(), ok=self._mark_fetched)
        elif st.ahead:
            self._run_op(_("Pushing…"), lambda repo: repo.push(True), ok=self._mark_fetched)
        else:
            self.fetch()

    # ------------------------------------------------------------------ #
    # Branches
    # ------------------------------------------------------------------ #
    def _populate_branch_menu(self):
        menu = self.branch_menu
        menu.clear()
        snap = self._snapshot
        if snap is None:
            return
        current = snap.status.branch
        local = snap.local_branches or []
        for name in local:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name == current)
            act.triggered.connect(partial(self._checkout, name))

        remote_only = [
            rb for rb in (snap.remote_branches or []) if rb.split("/", 1)[1] not in local
        ]
        if remote_only:
            sub = menu.addMenu(_("Remote branches"))
            for rb in remote_only:
                sub.addAction(rb, partial(self._checkout_remote, rb))

        menu.addSeparator()
        menu.addAction(icon("mdi.plus"), _("New branch…"), self._new_branch)
        rename = menu.addAction(_("Rename current branch…"), self._rename_branch)
        rename.setEnabled(bool(current))
        others = [b for b in local if b != current]
        merge = menu.addMenu(_("Merge into current branch"))
        merge.setEnabled(bool(others) and bool(current))
        delete = menu.addMenu(_("Delete branch"))
        delete.setEnabled(bool(others))
        for b in others:
            merge.addAction(b, partial(self._merge, b))
            delete.addAction(b, partial(self._delete_branch, b))

    def _checkout(self, name, *_args):
        if self._snapshot and name == self._snapshot.status.branch:
            return
        self._run_op(_("Switching branch…"), lambda repo: repo.checkout(name), fail=self._checkout_failed)

    def _checkout_remote(self, remote_branch, *_args):
        self._run_op(
            _("Switching branch…"),
            lambda repo: repo.checkout_remote(remote_branch),
            fail=self._checkout_failed,
        )

    def _checkout_failed(self, exc):
        self._show_error(exc, _("Could not switch branch"))

    def _new_branch(self, *_args, start=None):
        name, ok = QInputDialog.getText(self, _("New branch"), _("Branch name:"))
        name = name.strip().replace(" ", "-")
        if ok and name:
            self._run_op(_("Creating branch…"), lambda repo: repo.create_branch(name, start))

    def _rename_branch(self, *_args):
        current = self._snapshot.status.branch
        name, ok = QInputDialog.getText(self, _("Rename branch"), _("New name:"), text=current)
        name = name.strip().replace(" ", "-")
        if ok and name and name != current:
            self._run_op(_("Renaming branch…"), lambda repo: repo.rename_branch(name))

    def _merge(self, name, *_args):
        current = self._snapshot.status.branch
        if self._confirm(
            _("Merge branch"),
            _("Merge “{b}” into “{c}”?").format(b=name, c=current),
            _("Merge"),
        ):
            self._run_op(_("Merging…"), lambda repo: repo.merge(name))

    def _abort_merge(self):
        if self._confirm(
            _("Abort merge"),
            _("Abort the merge and return to the state before it started?"),
            _("Abort merge"),
        ):
            self._run_op(_("Aborting merge…"), lambda repo: repo.abort_merge())

    def _delete_branch(self, name, *_args):
        if not self._confirm(
            _("Delete branch"), _("Delete the local branch “{b}”?").format(b=name), _("Delete")
        ):
            return

        def failed(exc):
            if isinstance(exc, GitError) and "not fully merged" in exc.message:
                if self._confirm(
                    _("Branch not merged"),
                    _("“{b}” has commits that are not merged anywhere else. Delete it anyway?").format(b=name),
                    _("Delete anyway"),
                ):
                    self._run_op(_("Deleting branch…"), lambda repo: repo.delete_branch(name, True))
            else:
                self._show_error(exc)

        self._run_op(_("Deleting branch…"), lambda repo: repo.delete_branch(name), fail=failed)

    # ------------------------------------------------------------------ #
    # History tab
    # ------------------------------------------------------------------ #
    def _load_history(self):
        repo, snap = self._repo, self._snapshot
        if repo is None or snap is None:
            return
        self._history_key = (snap.status.oid, snap.status.ahead)
        limit = self.HISTORY_LIMIT
        self._tasks.submit(
            lambda: (repo.log(limit), repo.unpushed()), partial(self._on_history, repo)
        )

    def _on_history(self, repo, result, error):
        if repo is not self._repo or error is not None:
            return
        commits, unpushed = result
        tree = self.history_tree
        cur = self._current_commit()
        cur_sha = cur.sha if cur else None
        tree.blockSignals(True)
        tree.clear()
        restore = None
        for cm in commits:
            item = QTreeWidgetItem(
                [("↑ " if cm.sha in unpushed else "") + cm.summary, cm.author, relative_time(cm.date)]
            )
            item.setData(0, Qt.UserRole, cm)
            item.setToolTip(0, f"{cm.sha}\n{cm.date}")
            tree.addTopLevelItem(item)
            if cm.sha == cur_sha:
                restore = item
        tree.blockSignals(False)
        if commits:
            tree.setCurrentItem(restore or tree.topLevelItem(0))
        else:
            self.commit_info.setText(_("No commits yet."))
            self.commit_files_tree.clear()
            self.history_diff.clear_diff()

    def _current_commit(self):
        item = self.history_tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _on_commit_selected(self, current, _previous):
        if current is None or self._repo is None:
            return
        cm = current.data(0, Qt.UserRole)
        body = html.escape(cm.body).replace("\n", "<br>")
        self.commit_info.setText(
            f"<b>{html.escape(cm.summary)}</b><br>"
            f"{html.escape(cm.author)} &lt;{html.escape(cm.email)}&gt; · "
            f"{html.escape(relative_time(cm.date))} · <code>{cm.short}</code>"
            + (f"<br><br>{body}" if body else "")
        )
        repo = self._repo
        self._tasks.submit(lambda: repo.commit_files(cm.sha), partial(self._on_commit_files, repo, cm.sha))

    def _on_commit_files(self, repo, sha, files, error):
        cur = self._current_commit()
        if repo is not self._repo or cur is None or cur.sha != sha or error is not None:
            return
        tree = self.commit_files_tree
        tree.blockSignals(True)
        tree.clear()
        for cf in files:
            label = f"{cf.orig_path} → {cf.path}" if cf.orig_path else cf.path
            item = QTreeWidgetItem([label, cf.code])
            item.setData(0, Qt.UserRole, cf)
            item.setToolTip(0, cf.path)
            item.setForeground(1, QBrush(QColor(STATUS_COLORS.get(cf.code, "#888888"))))
            item.setTextAlignment(1, Qt.AlignCenter)
            tree.addTopLevelItem(item)
        tree.blockSignals(False)
        if files:
            tree.setCurrentItem(tree.topLevelItem(0))
        else:
            self.history_diff.clear_diff(_("(no file changes)"))

    def _on_commit_file_selected(self, current, _previous):
        cm = self._current_commit()
        if current is None or cm is None or self._repo is None:
            return
        cf = current.data(0, Qt.UserRole)
        repo = self._repo
        self._tasks.submit(
            lambda: repo.diff_commit(cm.sha, cf), partial(self._on_commit_diff, repo, cm.sha, cf.path)
        )

    def _on_commit_diff(self, repo, sha, path, text, error):
        cm = self._current_commit()
        item = self.commit_files_tree.currentItem()
        if (
            repo is not self._repo
            or cm is None
            or cm.sha != sha
            or item is None
            or item.data(0, Qt.UserRole).path != path
        ):
            return
        if error is not None:
            self.history_diff.clear_diff(str(error))
        elif not text.strip():
            self.history_diff.clear_diff(_("(no textual changes)"))
        else:
            self.history_diff.set_diff(text)

    def _open_commit_file(self, item, _column=None):
        cf = item.data(0, Qt.UserRole)
        full = os.path.join(self._repo.root, cf.path)
        if os.path.isfile(full):
            self.sig_open_file.emit(full)

    def _history_menu(self, pos):
        item = self.history_tree.itemAt(pos)
        if item is None:
            return
        cm = item.data(0, Qt.UserRole)
        is_head = self.history_tree.indexOfTopLevelItem(item) == 0
        menu = QMenu(self)
        menu.addAction(_("Copy SHA"), lambda *_a: QApplication.clipboard().setText(cm.sha))
        menu.addAction(_("Create branch from this commit…"), lambda *_a: self._new_branch(start=cm.sha))
        menu.addSeparator()
        menu.addAction(_("Revert this commit"), partial(self._revert, cm))
        undo = menu.addAction(_("Undo this commit"), self._undo_commit)
        undo.setEnabled(is_head)
        undo.setToolTip(_("Only the latest commit can be undone"))
        menu.exec_(self.history_tree.viewport().mapToGlobal(pos))

    def _revert(self, commit, *_args):
        if self._confirm(
            _("Revert commit"),
            _("Create a new commit that reverts “{s}”?").format(s=commit.summary),
            _("Revert"),
        ):
            self._run_op(
                _("Reverting…"), lambda repo: repo.revert(commit),
                fail=lambda exc: self._show_error(exc, _("Revert failed")),
            )

    def _undo_commit(self, *_args):
        cm = self._current_commit()
        if cm is None or not self._confirm(
            _("Undo commit"),
            _("Undo “{s}”?\n\nIts changes return to the Changes tab and the message is restored.").format(s=cm.summary),
            _("Undo commit"),
        ):
            return

        def done(result):
            summary, body = result
            if not self.summary_edit.text():
                self.summary_edit.setText(summary)
                self.desc_edit.setPlainText(body)
            self.tabs.setCurrentIndex(0)

        self._run_op(_("Undoing commit…"), lambda repo: repo.undo_last_commit(), ok=done)

    # ------------------------------------------------------------------ #
    # Generic operation runner and dialogs
    # ------------------------------------------------------------------ #
    def _begin_busy(self, label):
        self._busy += 1
        if self._busy == 1:
            self.sig_busy_changed.emit(True)
        if label:
            self.status_label.setText(label)
        self._set_controls_enabled(self._repo is not None)
        self._update_commit_state()

    def _end_busy(self):
        self._busy = max(0, self._busy - 1)
        if self._busy == 0:
            self.sig_busy_changed.emit(False)
            self.status_label.setText("")
        self._set_controls_enabled(self._repo is not None)
        if self._snapshot is not None:
            self._update_toolbar()
        self._update_commit_state()

    def _run_op(self, label, fn, ok=None, fail=None, silent=False):
        """Run ``fn(repo)`` in the worker thread, then refresh everything."""
        repo = self._repo
        if repo is None:
            return
        if not silent:
            self._last_op = (label, fn, ok, fail)
        self._begin_busy(label)
        self._tasks.submit(lambda: fn(repo), partial(self._op_finished, repo, ok, fail, silent))

    def _op_finished(self, repo, ok, fail, silent, result, error):
        self._end_busy()
        if error is not None:
            if fail is not None:
                fail(error)
            elif not silent:
                self._show_error(error)
        elif ok is not None:
            ok(result)
        if repo is self._repo:
            self.refresh(refs=True)
            self._load_history()

    def _show_error(self, exc, title=None):
        text = (exc.message if isinstance(exc, GitError) else str(exc)).strip()
        lines = text.splitlines() or [_("Unknown error")]
        box = QMessageBox(QMessageBox.Warning, title or _("Git error"), lines[0][:400], QMessageBox.Ok, self)
        auth = isinstance(exc, GitError) and exc.is_auth_error
        signin = None
        if auth and not self._github_user:
            box.setInformativeText(exc.hint)
            signin = box.addButton(_("Sign in to GitHub…"), QMessageBox.ActionRole)
        elif auth:
            box.setInformativeText(
                _(
                    "You are signed in as {u}, but GitHub refused access. For an "
                    "organization repository, authorize the OAuth app for that "
                    "organization (GitHub → Settings → Applications)."
                ).format(u=self._github_user)
            )
        elif getattr(exc, "hint", None):
            box.setInformativeText(exc.hint)
        if len(lines) > 1:
            box.setDetailedText(text)
        box.exec_()
        if signin is not None and box.clickedButton() is signin:
            self.sign_in_github(retry=self._last_op)

    def _confirm(self, title, text, accept_label):
        box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Cancel, self)
        accept = box.addButton(accept_label, QMessageBox.AcceptRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec_()
        return box.clickedButton() is accept


def relative_time_from_epoch(epoch: float) -> str:
    secs = time.time() - epoch
    if secs < 60:
        return _("just now")
    if secs < 3600:
        return _("{n} min ago").format(n=int(secs // 60))
    return _("{n} h ago").format(n=int(secs // 3600))


def _main():  # pragma: no cover - manual debugging helper
    app = QApplication(sys.argv)
    panel = GitPanel()
    panel.resize(1000, 650)
    panel.show()
    panel.set_context_dir(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    sys.exit(app.exec_())


if __name__ == "__main__":  # pragma: no cover
    _main()
