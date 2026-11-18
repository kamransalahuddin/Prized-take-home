import os
import subprocess
import sys

import pytest

from board.store import Store

BOARD_ARGV = [sys.executable, "-m", "board.cli"]


@pytest.fixture
def board_dir(tmp_path):
    d = tmp_path / "board"
    d.mkdir()
    return d


@pytest.fixture
def board_env(board_dir):
    """Environment for CLI subprocesses: private BOARD_DIR, never ~/.board."""
    env = {k: v for k, v in os.environ.items() if k not in ("BOARD_URL", "BOARD_AGENT", "BOARD_DATA")}
    env["BOARD_DIR"] = str(board_dir)
    return env


@pytest.fixture
def store(board_dir):
    s = Store(board_dir)
    yield s
    s.close()


@pytest.fixture
def board(board_env):
    """Run the CLI in a subprocess, like an agent would.

    board(*args, input=str|bytes|None, env={...}) -> CompletedProcess (text unless input is bytes).
    Without input, stdin is /dev/null (not a TTY, empty)."""

    def run(*args, input=None, env=None, timeout=30):
        e = dict(board_env, **(env or {}))
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            [*BOARD_ARGV, *args],
            env=e,
            capture_output=True,
            text=not isinstance(input, bytes),
            timeout=timeout,
            **kw,
        )

    return run
