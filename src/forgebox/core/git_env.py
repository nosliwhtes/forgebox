"""The small host environment needed by local Git subprocesses."""

import os


_HOST_VARIABLES = frozenset({
    "PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "TMPDIR",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})


def git_environment() -> dict[str, str]:
    """Keep Git config locations/identity/locale, not provider keys or overrides.

    XDG_CONFIG_HOME preserves global ignore rules and core.excludesFile settings
    used when snapshots exclude untracked secrets.
    Repository hooks and filters inherit this environment too. This limits
    accidental credential exposure; it does not sandbox repository code.
    """
    environment = {
        key: value for key, value in os.environ.items()
        if key in _HOST_VARIABLES or key.startswith("LC_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment
