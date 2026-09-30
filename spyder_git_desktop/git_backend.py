"""Thin, dependency-free wrapper around the ``git`` command line.

Everything in here is blocking and Qt-free, so it can run in a worker thread
and be unit-tested on its own.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, List, NamedTuple, Optional, Set, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Errors and process helpers
# --------------------------------------------------------------------------- #
_github_token = None

# One-shot credential helper: answers `get` with the OAuth token held in an
# environment variable (so the token never appears on the command line).
_CRED_HELPER = (
    '!f() { test "$1" = get && echo username=x-access-token'
    ' && echo "password=$SPYDER_GIT_DESKTOP_TOKEN"; }; f'
)


def set_github_token(token: Optional[str]):
    """Token used for https://github.com remotes (None to stop using one)."""
    global _github_token
    _github_token = token or None


class GitError(Exception):
    """Raised when a git command fails."""

    def __init__(self, message: str, returncode: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.returncode = returncode

    @property
    def needs_identity(self) -> bool:
        text = self.message.lower()
        return (
            "please tell me who you are" in text
            or "unable to auto-detect email" in text
            or "empty ident name" in text
        )

    _AUTH_MARKERS = (
        "terminal prompts disabled",
        "authentication failed",
        "could not read username",
        "could not read password",
        "invalid username or password",
        "support for password authentication was removed",
        "requested url returned error: 403",
        "requested url returned error: 401",
    )

    @property
    def is_auth_error(self) -> bool:
        """HTTPS authentication failure (signing in to GitHub can fix it)."""
        text = self.message.lower()
        return any(m in text for m in self._AUTH_MARKERS)

    @property
    def hint(self) -> Optional[str]:
        if self.is_auth_error:
            return (
                "Git could not authenticate. Use the Repository menu → "
                "“Sign in to GitHub…”, or set up a credential helper."
            )
        if "permission denied (publickey)" in self.message.lower():
            return (
                "SSH authentication failed. Load your key into ssh-agent, or "
                "switch the remote to https:// and sign in to GitHub."
            )
        return None


class _Result(NamedTuple):
    stdout: str
    stderr: str
    returncode: int


def git_executable() -> Optional[str]:
    return shutil.which("git")


def _env() -> dict:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # never hang waiting for a tty
            "GIT_EDITOR": "true",  # never open an editor
            "GIT_MERGE_AUTOEDIT": "no",
            "GIT_OPTIONAL_LOCKS": "0",  # polling `status` must not take index.lock
            "GIT_LITERAL_PATHSPECS": "1",  # file names are never globs
            "LC_MESSAGES": "C",  # stable English messages for error detection
            "LANGUAGE": "C",
        }
    )
    if _github_token:
        env["SPYDER_GIT_DESKTOP_TOKEN"] = _github_token
    return env


def _run(args, cwd, input_text=None, check=True, timeout=None) -> _Result:
    exe = git_executable()
    if exe is None:
        raise GitError(
            "Git executable not found. Install Git and make sure it is on "
            "your PATH."
        )
    cmd = [exe, "-c", "core.quotepath=false", "-c", "color.ui=false"]
    if _github_token:
        cmd += ["-c", f"credential.https://github.com.helper={_CRED_HELPER}"]
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            creationflags=_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise GitError(str(exc)) from exc
    res = _Result(
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
        proc.returncode,
    )
    if check and res.returncode != 0:
        raise GitError(
            (res.stderr.strip() or res.stdout.strip() or f"git {args[0]} failed"),
            res.returncode,
        )
    return res


def _chunks(seq, size=200):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FileChange:
    """One entry of ``git status`` (X = index column, Y = worktree column)."""

    path: str
    index: str
    worktree: str
    orig_path: Optional[str] = None

    @property
    def untracked(self) -> bool:
        return self.index == "?"

    @property
    def conflicted(self) -> bool:
        return (
            "U" in (self.index, self.worktree)
            or (self.index, self.worktree) in {("A", "A"), ("D", "D")}
        )

    @property
    def code(self) -> str:
        """Single letter used in the UI: A, M, D, R or U."""
        if self.conflicted:
            return "U"
        if self.untracked or self.index == "A":
            return "A"
        if "D" in (self.index, self.worktree):
            return "D"
        if "R" in (self.index, self.worktree):
            return "R"
        return "M"


@dataclass
class RepoStatus:
    branch: str = ""
    detached: bool = False
    oid: Optional[str] = None  # None on an unborn branch
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    changes: List[FileChange] = field(default_factory=list)

    @property
    def unborn(self) -> bool:
        return self.oid is None


@dataclass
class Snapshot:
    status: RepoStatus
    merging: bool = False
    local_branches: Optional[List[str]] = None
    remote_branches: Optional[List[str]] = None
    remotes: Optional[List[str]] = None


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    email: str
    date: str  # strict ISO 8601
    parents: Tuple[str, ...]
    summary: str
    body: str

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass(frozen=True)
class CommitFile:
    code: str
    path: str
    orig_path: Optional[str] = None


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class GitRepo:
    MAX_PREVIEW_BYTES = 1_000_000

    def __init__(self, root: str):
        self.root = os.path.normpath(root)
        self._git_dir: Optional[str] = None

    # -- construction ------------------------------------------------------ #
    @classmethod
    def discover(cls, path: str) -> Optional["GitRepo"]:
        """Return the repository containing ``path`` (file or dir), if any."""
        path = os.path.abspath(path)
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            return None
        res = _run(["rev-parse", "--show-toplevel"], cwd=path, check=False)
        top = res.stdout.strip()
        if res.returncode != 0 or not top:
            return None
        return cls(top)

    @classmethod
    def init(cls, path: str) -> "GitRepo":
        res = _run(["init", "-b", "main"], cwd=path, check=False)
        if res.returncode != 0:  # git < 2.28 has no -b
            _run(["init"], cwd=path)
        repo = cls.discover(path)
        if repo is None:
            raise GitError("Could not initialise the repository.")
        return repo

    @classmethod
    def clone(cls, url: str, dest: str) -> "GitRepo":
        _run(["clone", "--", url, dest], cwd=os.path.dirname(dest) or ".")
        repo = cls.discover(dest)
        if repo is None:
            raise GitError("Clone finished but no repository was found.")
        return repo

    @property
    def name(self) -> str:
        return os.path.basename(self.root) or self.root

    def run(self, *args, input_text=None, check=True) -> str:
        return _run(list(args), self.root, input_text=input_text, check=check).stdout

    def _ok(self, *args) -> bool:
        return _run(list(args), self.root, check=False).returncode == 0

    # -- state ------------------------------------------------------------- #
    def git_dir(self) -> str:
        if self._git_dir is None:
            out = self.run("rev-parse", "--git-dir").strip()
            self._git_dir = out if os.path.isabs(out) else os.path.join(self.root, out)
        return self._git_dir

    def in_merge(self) -> bool:
        return os.path.exists(os.path.join(self.git_dir(), "MERGE_HEAD"))

    def merge_message(self) -> str:
        try:
            with open(os.path.join(self.git_dir(), "MERGE_MSG"), encoding="utf-8") as fh:
                return fh.readline().strip()
        except OSError:
            return ""

    def has_commits(self) -> bool:
        return self._ok("rev-parse", "--verify", "-q", "HEAD")

    def status(self) -> RepoStatus:
        out = self.run(
            "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all"
        )
        st = RepoStatus()
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            line = parts[i]
            i += 1
            if not line:
                continue
            if line.startswith("# "):
                key, _, val = line[2:].partition(" ")
                if key == "branch.oid":
                    st.oid = None if val == "(initial)" else val
                elif key == "branch.head":
                    st.detached = val == "(detached)"
                    st.branch = "" if st.detached else val
                elif key == "branch.upstream":
                    st.upstream = val
                elif key == "branch.ab":
                    m = re.match(r"\+(\d+) -(\d+)", val)
                    if m:
                        st.ahead, st.behind = int(m.group(1)), int(m.group(2))
                continue
            kind = line[0]
            if kind == "1":
                f = line.split(" ", 8)
                xy, path, orig = f[1], f[8], None
            elif kind == "2":
                f = line.split(" ", 9)
                xy, path = f[1], f[9]
                orig = parts[i] if i < len(parts) else None
                i += 1
            elif kind == "u":
                f = line.split(" ", 10)
                xy, path, orig = f[1], f[10], None
            elif kind == "?":
                st.changes.append(FileChange(line[2:], "?", "?"))
                continue
            else:  # '!' ignored, anything unknown
                continue
            x, y = (c.replace(".", " ") for c in xy)
            st.changes.append(FileChange(path, x, y, orig))
        return st

    def branches(self) -> Tuple[List[str], List[str]]:
        local = self.run(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).split()
        remote = [
            b
            for b in self.run(
                "for-each-ref", "--format=%(refname:short)", "refs/remotes"
            ).split()
            if "/" in b and not b.endswith("/HEAD")
        ]
        return sorted(local, key=str.lower), sorted(remote, key=str.lower)

    def remotes(self) -> List[str]:
        return self.run("remote").split()

    def snapshot(self, include_refs: bool = True) -> Snapshot:
        snap = Snapshot(status=self.status(), merging=self.in_merge())
        if include_refs:
            snap.local_branches, snap.remote_branches = self.branches()
            snap.remotes = self.remotes()
        return snap

    def identity(self) -> Tuple[str, str]:
        name = _run(["config", "user.name"], self.root, check=False).stdout.strip()
        email = _run(["config", "user.email"], self.root, check=False).stdout.strip()
        return name, email

    def set_identity(self, name: str, email: str, global_: bool = False):
        scope = ["--global"] if global_ else []
        self.run("config", *scope, "user.name", name)
        self.run("config", *scope, "user.email", email)

    # -- history ----------------------------------------------------------- #
    def log(self, limit: int = 300, skip: int = 0) -> List[Commit]:
        if not self.has_commits():
            return []
        fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%P%x1f%s%x1f%b%x1e"
        out = self.run("log", f"-n{limit}", f"--skip={skip}", f"--pretty=format:{fmt}")
        commits = []
        for rec in out.split("\x1e"):
            rec = rec.lstrip("\n")
            if not rec.strip():
                continue
            f = rec.split("\x1f")
            if len(f) < 7:
                continue
            commits.append(
                Commit(f[0], f[1], f[2], f[3], tuple(f[4].split()), f[5], f[6].strip())
            )
        return commits

    def unpushed(self) -> Set[str]:
        res = _run(["rev-list", "@{upstream}..HEAD"], self.root, check=False)
        return set(res.stdout.split()) if res.returncode == 0 else set()

    def commit_files(self, sha: str) -> List[CommitFile]:
        out = self.run(
            "show", "--no-color", "--name-status", "-M", "-m", "--first-parent",
            "--format=", sha,
        )
        files = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            code = parts[0][:1]
            if code in "RC" and len(parts) >= 3:
                files.append(CommitFile("R" if code == "R" else "A", parts[2], parts[1]))
            elif len(parts) >= 2:
                files.append(CommitFile(code, parts[1]))
        return files

    # -- diffs ------------------------------------------------------------- #
    def diff_working(self, change: FileChange) -> str:
        paths = [change.path] + ([change.orig_path] if change.orig_path else [])
        base = ["--no-color", "--no-ext-diff"]
        if change.untracked:
            return self._new_file_diff(change.path)
        if change.conflicted:
            return self.run("diff", *base, "--", change.path, check=False)
        if self.has_commits():
            return self.run("diff", "HEAD", *base, "-M", "--", *paths, check=False)
        return self.run("diff", "--cached", *base, "--", *paths, check=False)

    def diff_commit(self, sha: str, cf: CommitFile) -> str:
        paths = [cf.path] + ([cf.orig_path] if cf.orig_path else [])
        return self.run(
            "show", "--no-color", "--no-ext-diff", "-M", "-m", "--first-parent",
            "--format=", sha, "--", *paths, check=False,
        )

    def _new_file_diff(self, path: str) -> str:
        full = os.path.join(self.root, path)
        if os.path.isdir(full):
            return "(directory or submodule)"
        try:
            size = os.path.getsize(full)
            if size == 0:
                return "(empty file)"
            if size > self.MAX_PREVIEW_BYTES:
                return f"New file too large to preview ({size:,} bytes)."
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return f"Cannot read file: {exc}"
        if b"\0" in data[:8000]:
            return "Binary file added."
        lines = data.decode("utf-8", errors="replace").splitlines()
        head = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        return head + "\n".join("+" + ln for ln in lines)

    # -- working tree operations ------------------------------------------ #
    def _unstage_all(self):
        if self.has_commits():
            self.run("reset", "-q")
        else:
            self.run("read-tree", "--empty")

    def commit(self, paths: Iterable[str], summary: str, description: str = ""):
        paths = list(dict.fromkeys(paths))
        if self.in_merge():
            self.run("add", "-A")
        else:
            if not paths:
                raise GitError("No files selected for the commit.")
            self._unstage_all()
            for chunk in _chunks(paths):
                self.run("add", "-A", "--", *chunk)
        args = ["commit", "-m", summary]
        if description.strip():
            args += ["-m", description.strip()]
        self.run(*args)

    def discard(self, changes: Iterable[FileChange]):
        restore: List[str] = []
        for c in changes:
            if c.conflicted:
                raise GitError(
                    "Files with merge conflicts cannot be discarded. "
                    "Resolve them or abort the merge."
                )
            full = os.path.join(self.root, c.path)
            if c.untracked:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full, ignore_errors=True)
                elif os.path.lexists(full):
                    os.remove(full)
            elif c.index in ("A", "R"):
                self.run("rm", "-f", "--", c.path)
                if c.orig_path:
                    restore.append(c.orig_path)
            else:
                restore.append(c.path)
        if restore and self.has_commits():
            for chunk in _chunks(restore):
                self.run("checkout", "HEAD", "--", *chunk)

    def ignore(self, path: str):
        gi = os.path.join(self.root, ".gitignore")
        existing = ""
        if os.path.exists(gi):
            with open(gi, encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        with open(gi, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("/" + path.replace("\\", "/") + "\n")

    # -- branches ---------------------------------------------------------- #
    def checkout(self, name: str):
        self.run("checkout", name, "--")

    def checkout_remote(self, remote_branch: str):
        local = remote_branch.split("/", 1)[1]
        if local in self.branches()[0]:
            self.checkout(local)
        else:
            self.run("checkout", "-b", local, "--track", remote_branch)

    def create_branch(self, name: str, start: Optional[str] = None):
        args = ["checkout", "-b", name]
        if start:
            args.append(start)
        self.run(*args)

    def rename_branch(self, new_name: str):
        self.run("branch", "-m", new_name)

    def delete_branch(self, name: str, force: bool = False):
        self.run("branch", "-D" if force else "-d", name)

    def merge(self, name: str):
        self.run("merge", "--no-edit", name)

    def abort_merge(self):
        self.run("merge", "--abort")

    # -- commits ----------------------------------------------------------- #
    def undo_last_commit(self) -> Tuple[str, str]:
        out = self.run("log", "-1", "--pretty=format:%s%x1f%b")
        summary, _, body = out.partition("\x1f")
        if self._ok("rev-parse", "--verify", "-q", "HEAD~1"):
            self.run("reset", "--soft", "HEAD~1")
        else:  # root commit
            self.run("update-ref", "-d", "HEAD")
        return summary, body.strip()

    def revert(self, commit: Commit):
        args = ["revert", "--no-edit"]
        if len(commit.parents) > 1:
            args += ["-m", "1"]
        self.run(*args, commit.sha)

    # -- remotes ----------------------------------------------------------- #
    def add_remote(self, name: str, url: str):
        self.run("remote", "add", name, url)

    def fetch(self):
        self.run("fetch", "--all", "--prune")

    def pull(self):
        self.run("pull", "--no-rebase", "--no-edit")

    def push(self, has_upstream: bool):
        if has_upstream:
            self.run("push")
            return
        remotes = self.remotes()
        if not remotes:
            raise GitError("No remote is configured for this repository.")
        remote = "origin" if "origin" in remotes else remotes[0]
        self.run("push", "-u", remote, "HEAD")
