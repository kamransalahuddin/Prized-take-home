"""Round-3 safe-termination regressions: signal storms never kill board by the signal
(N1), nested handlers never deadlock or recurse (N2), git never opens an editor (N3),
and read-only commands give up on a server that never answers."""

from __future__ import annotations

import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

BOARD_ARGV = [sys.executable, "-m", "board.cli"]
STOP = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT]
LOOP = ["sh", "-c", "while :; do echo tick; sleep 0.1; done"]


def storm(proc, seconds=10.0) -> int:
    """Send random stop signals back to back until proc exits; return how many."""
    n, t0 = 0, time.monotonic()
    while proc.poll() is None and time.monotonic() - t0 < seconds:
        try:
            proc.send_signal(random.choice(STOP))
            n += 1
        except ProcessLookupError:
            break
    return n


# ---------------------------------------------------------------- N1


@pytest.mark.parametrize("mode", ["log", "pipe", "append"])
def test_N1_signal_storm_exits_128_plus_n_never_by_the_signal(board_env, board, store, mode):
    for _ in range(3):
        stdin = subprocess.DEVNULL
        if mode == "log":
            args = ["log", "-a", "t", "--", *LOOP]
        elif mode == "pipe":
            args, stdin = ["log", "-a", "t"], subprocess.PIPE
        else:
            seed = board("log", "-a", "t", "--", "true").stdout.strip()
            args, stdin = ["append", seed, "-"], subprocess.PIPE
        proc = subprocess.Popen([*BOARD_ARGV, *args], env=board_env, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        if mode == "append":
            pid = seed
            proc.stdin.write(b"hello\n")
            proc.stdin.flush()
            time.sleep(1.0)
        else:
            if mode == "pipe":
                proc.stdin.write(b"hello\n")
                proc.stdin.flush()
            pid = proc.stdout.readline().decode().strip()
            time.sleep(0.3)
        sent = storm(proc)
        _, err = proc.communicate(timeout=30)
        assert sent > 0
        # 128+N of a stop signal (or the child's own code, e.g. sh killed by TERM: 143);
        # never negative (= killed by the signal), never a core dump
        assert proc.returncode in {128 + s for s in STOP}, (proc.returncode, err[-500:])
        assert b"Traceback" not in err and b"race condition" not in err, err[-500:]
        assert store.get_post(pid)["live"] is False


# ---------------------------------------------------------------- N2

BURST_SCRIPT = textwrap.dedent("""
    import os, sys, threading
    from board import cli
    cli.KEEP_SIGNAL_HANDLERS = True
    ev = threading.Event()
    seen = []
    def on_signal(signum):  # what run_child/stream_input do: Event methods, a syscall
        seen.append(signum)
        ev.set()
        os.getpgid(0)
    with cli.StopSignals(on_signal) as sigs:
        print("ready", flush=True)
        sys.stdin.readline()  # the parent sends its bursts, then a line
    print(len(sigs.got), len(seen), flush=True)
""")


def burst_target():
    return subprocess.Popen([sys.executable, "-c", BURST_SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def finish_target(proc, timeout=20):
    out, err = proc.communicate("go\n", timeout=timeout)  # a deadlocked handler: TimeoutExpired
    assert proc.returncode == 0, err[-800:]
    got, seen = map(int, out.split())
    assert got > 0 and got == seen  # every recorded signal reached on_signal, in the watcher
    assert "Traceback" not in err and "RecursionError" not in err
    return got


def test_N2_bursts_of_signals_never_deadlock_the_handlers():
    proc = burst_target()
    assert proc.stdout.readline().strip() == "ready"
    t0 = time.monotonic()
    for _ in range(100):  # 100 bursts of 50 back-to-back signals (round 3: ~1/115 hung)
        for _ in range(50):
            os.kill(proc.pid, random.choice(STOP))
        time.sleep(0.005)
    finish_target(proc)
    assert time.monotonic() - t0 < 20


def test_N2b_eleven_thousand_signals_no_recursion_error():
    proc = burst_target()
    assert proc.stdout.readline().strip() == "ready"
    for _ in range(11000):
        os.kill(proc.pid, random.choice(STOP))
    finish_target(proc, timeout=60)


def test_N2_storm_during_command_start_keeps_first_signal_and_closes_post(board_env, store):
    """Signals racing the spawn (pending list, forwarded once it exists) still end the
    child, and board exits 128+N."""
    proc = subprocess.Popen([*BOARD_ARGV, "log", "-a", "t", "--", *LOOP], env=board_env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    pid = proc.stdout.readline().decode().strip()
    storm(proc)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode in {128 + s for s in STOP}, err[-500:]
    assert store.get_post(pid)["live"] is False


# ---------------------------------------------------------------- N3


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_N3_exported_git_editor_cannot_hang_git_commit(board_env, store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    genv = dict(board_env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    subprocess.run(["git", "init", "-q", str(repo)], env=genv, check=True)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f"], env=genv, check=True)
    env = dict(genv, GIT_EDITOR="vim", GIT_SEQUENCE_EDITOR="vim", VISUAL="vim", EDITOR="vim")
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "log", "-a", "t", "--", "git", "-C", str(repo), "commit"], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert time.monotonic() - t0 < 15
    assert r.returncode == 1  # empty message: git aborts at once instead of waiting on vim
    assert "empty commit message" in store.get_post(r.stdout.strip())["payload"]["text"]


# ---------------------------------------------------------------- reads vs a silent server


@pytest.fixture
def silent_server():
    """Accepts connections, reads the request, never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    conns, stop = [], threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                conns.append(c)
            except OSError:
                pass

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    stop.set()
    th.join()
    for c in conns:
        c.close()
    srv.close()


def test_ls_gives_up_on_a_server_that_never_answers(board_env, silent_server):
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "ls"], env=dict(board_env, BOARD_URL=silent_server),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
    took = time.monotonic() - t0
    assert r.returncode == 3, r.stderr
    assert 4 < took < 8, took
    assert "did not answer" in r.stderr and "Traceback" not in r.stderr


def test_wait_timeout_holds_against_a_server_that_never_answers(board_env, silent_server):
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "wait", "p_abcdefg", "--timeout", "2"],
                       env=dict(board_env, BOARD_URL=silent_server), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=40)
    assert r.returncode == 4, r.stderr
    assert time.monotonic() - t0 < 5
