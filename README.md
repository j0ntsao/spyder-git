# Spyder Git Desktop
A vibe coded GitHub Desktop-style version control pane for Spyder 6. I asked claude to make me this because I didn't want two apps open. It puts a pane in that looks like github desktop.

## Features
- **Changes**: file list with checkboxes (commit only what you tick), coloured diff viewer, summary + description, "Commit N files to *branch*"
- **History**: commit list (↑ marks unpushed commits), files per commit, per-file diffs, revert / undo last commit / branch from commit
- **Branches**: switch, create, rename, merge, delete, check out remote branches
- **Sync**: one context-aware button: Fetch / Pull ↓N / Push ↑N / Publish branch (auto-fetch every 5 min)
- Merge-conflict banner with *Abort merge*, discard changes, add to `.gitignore`, git identity dialog, init / clone
- Follows the active Spyder **project**, or the folder of the file in the editor; double-click a file to open it in the editor

## Install
Install into the **same environment Spyder runs in**, then restart Spyder:

    pip install .            # or: pip install -e .

Open it from *View → Panes → Git Desktop*.

Requires the `git` command line tool on your PATH.

## Signing in to GitHub (browser login)
Use *Repository menu → Sign in to GitHub…* (or just push and click the button in the error dialog).
Your browser opens, you confirm a code, and the token is stored in your system keyring.
It is used automatically for `https://github.com` remotes.

This uses GitHub's OAuth **device flow**, which needs an OAuth App (GitHub Desktop has its own built in).
On first use the plugin shows a short guide: register an app at
https://github.com/settings/applications/new, tick **Enable Device Flow**, paste the **Client ID**.
To ship it pre-configured, set `DEFAULT_CLIENT_ID` in `github_auth.py`
(or the `SPYDER_GIT_DESKTOP_CLIENT_ID` environment variable).

SSH remotes are unaffected: they need a key loaded in your ssh-agent. The plugin never prompts
for passwords (`GIT_TERMINAL_PROMPT=0`).

## Debugging outside Spyder
    python -m spyder_git_desktop.panel /path/to/repo
