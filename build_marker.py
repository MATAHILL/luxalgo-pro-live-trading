"""Build identity for the deployed process.

Why this module exists
----------------------
There was no way to tell which version of the code was actually running on the
live deployment. /health reported "ok" but never said WHICH build produced it,
so two pages of the same site could disagree and neither could be checked
against a commit. That question now has a five-second answer.

Priority: an explicitly set LUXALGO_BUILD_SHA wins; otherwise Render's own
RENDER_GIT_COMMIT is used; otherwise the checkout is asked for its HEAD. If
none of those resolve, the value is "UNKNOWN" -- never a guess.
"""
from __future__ import annotations

import os
import subprocess

_REQUIRED = 7  # characters of a commit id that are worth showing


def _from_git() -> str:
    """Ask the local checkout, tolerating a read-only or absent .git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, text=True, check=False)
        value = (out.stdout or "").strip()
        return value if len(value) >= _REQUIRED else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def build_identity() -> dict:
    """Return the build identity of THIS process, read once at import time."""
    explicit = os.getenv("LUXALGO_BUILD_SHA", "").strip()
    render = os.getenv("RENDER_GIT_COMMIT", "").strip()
    branch = os.getenv("RENDER_GIT_BRANCH", "").strip()
    source, sha = "UNKNOWN", ""
    for candidate_source, candidate in (("ENV:LUXALGO_BUILD_SHA", explicit),
                                        ("RENDER_GIT_COMMIT", render),
                                        ("GIT_REV_PARSE", _from_git())):
        if candidate:
            source, sha = candidate_source, candidate
            break
    return {"build": (sha[:_REQUIRED] if sha else "UNKNOWN"),
            "build_full": (sha or "UNKNOWN"),
            "build_source": source,
            "build_branch": branch or "UNKNOWN"}


BUILD = build_identity()
