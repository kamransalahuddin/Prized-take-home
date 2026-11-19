"""Round-4 timing regressions (BOARD_URL mode): no single HTTP request may outlive the
patience it belongs to, a stop signal's budget (≤ 10 s), or a second signal; a server that
trickles its answer byte by byte cannot defeat the timeouts; a write to a server that
never answers costs about BOARD_WRITE_PATIENCE, not 3x it."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

BOARD_ARGV = [sys.executable, "-m", "board.cli"]


class _Server:
    """Accepts connections on 127.0.0.1; `serve(conn)` handles each one in a thread."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.conns: list[socket.socket] = []
        self.stop = threading.Event()
        self.requests = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            self.requests += 1
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, conn):  # silent: read the request, never answer
        pass

    def close(self):
        self.stop.set()
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


class _Trickle(_Server):
    """Answers with headers at once, then one body byte per second, forever."""

    def serve(self, conn):
        try:
            conn.settimeout(5)
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: 100000\r\n\r\n")
            while not self.stop.is_set():
                conn.sendall(b" ")
                time.sleep(1.0)
        except OSError:
            pass


@pytest.fixture
def silent_url():
    srv = _Server()
    yield srv.url
    srv.close()


@pytest.fixture
def trickle_url():
    srv = _Trickle()
    yield srv.url
    srv.close()


def start(args, env, stdin=subprocess.DEVNULL):
    return subprocess.Popen([*BOARD_ARGV, *args], env=env, stdin=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)


def finish(proc, timeout=40):
    if proc.stdin is not None and proc.stdin.closed:
        proc.stdin = None
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out.decode(), err.decode()


# ---------------------------------------------------------------- 1: stop signals vs a silent server


def test_log_run_mode_sigterm_exits_within_the_interrupt_budget(board_env, silent_url):
    proc = start(["log", "-a", "t", "--", "sleep", "1081"], dict(board_env, BOARD_URL=silent_url))
    time.sleep(4)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    rc, _, err = finish(proc)
    took = time.monotonic() - t0
    assert took <= 12, (took, err)
    assert rc == 128 + signal.SIGTERM, err  # sleep died of the forwarded TERM
    assert "not posted" in err and "Traceback" not in err, err


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_log_pipe_mode_signal_after_eof_exits_within_the_interrupt_budget(board_env, silent_url, sig):
    proc = start(["log", "-a", "t"], dict(board_env, BOARD_URL=silent_url), stdin=subprocess.PIPE)
    proc.stdin.write(b"data\n")
    proc.stdin.close()  # EOF at once: board is posting the end of the output
    time.sleep(2)
    t0 = time.monotonic()
    proc.send_signal(sig)
    rc, _, err = finish(proc)
    took = time.monotonic() - t0
    assert took <= 12, (took, err)
    assert rc == 128 + sig, err
    assert "not posted" in err and "Traceback" not in err, err


def test_log_pipe_mode_signal_while_reading_exits_within_the_interrupt_budget(board_env, silent_url):
    proc = start(["log", "-a", "t"], dict(board_env, BOARD_URL=silent_url), stdin=subprocess.PIPE)
    proc.stdin.write(b"data\n")
    proc.stdin.flush()
    time.sleep(2)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    rc, _, err = finish(proc)
    took = time.monotonic() - t0
    assert took <= 12, (took, err)
    assert rc == 128 + signal.SIGTERM, err
    assert "not posted" in err and "Traceback" not in err, err


@pytest.mark.parametrize("mode", ["run", "pipe"])
def test_second_signal_stops_posting_at_once(board_env, silent_url, mode):
    env = dict(board_env, BOARD_URL=silent_url)
    if mode == "run":
        proc = start(["log", "-a", "t", "--", "sleep", "1081"], env)
    else:
        proc = start(["log", "-a", "t"], env, stdin=subprocess.PIPE)
        proc.stdin.write(b"data\n")
        proc.stdin.close()
    time.sleep(2)
    proc.send_signal(signal.SIGTERM)
    time.sleep(2)  # the child is gone; board is in its final (≤ 10 s) attempt to post
    assert proc.poll() is None
    t0 = time.monotonic()
    proc.send_signal(signal.SIGINT)
    rc, _, err = finish(proc)
    took = time.monotonic() - t0
    assert took <= 2, (took, err)
    assert rc == 128 + signal.SIGTERM, err  # the first signal decides the exit code
    assert "not posted" in err and "Traceback" not in err, err


# ---------------------------------------------------------------- 2: a trickling server


def test_ls_gives_up_on_a_trickling_server(board_env, trickle_url):
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "ls"], env=dict(board_env, BOARD_URL=trickle_url),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
    took = time.monotonic() - t0
    assert r.returncode == 3, r.stderr
    assert took <= 8, took
    assert "did not answer" in r.stderr and "Traceback" not in r.stderr


def test_wait_timeout_holds_against_a_trickling_server(board_env, trickle_url):
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "wait", "p_abcdefg", "--timeout", "2"],
                       env=dict(board_env, BOARD_URL=trickle_url), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=40)
    assert r.returncode == 4, r.stderr
    assert time.monotonic() - t0 <= 5
    assert "Traceback" not in r.stderr


# ---------------------------------------------------------------- 3: writes track the patience


def test_post_to_a_silent_server_takes_about_the_write_patience(board_env, silent_url):
    t0 = time.monotonic()
    r = subprocess.run([*BOARD_ARGV, "post", "-a", "x", "hello"], env=dict(board_env, BOARD_URL=silent_url),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    took = time.monotonic() - t0
    assert r.returncode == 1, r.stderr
    assert "may already have been applied (request id " in r.stderr, r.stderr
    assert 18 <= took <= 25, took


def test_slow_but_working_server_still_succeeds(board_env, tmp_path):
    """Each try may take what is left of the patience (at least 4 s): a server that answers
    after 2 s is not a failure, even with a short patience."""

    class Slow(_Server):
        def serve(self, conn):
            try:
                conn.settimeout(5)
                conn.recv(65536)
                time.sleep(2)
                body = (b'{"id":"p_abcdefg","agent":"x","kind":"text","status":"info","gen":"g",'
                        b'"created":true}')
                conn.sendall(b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            except OSError:
                pass

    srv = Slow()
    try:
        r = subprocess.run([*BOARD_ARGV, "post", "-a", "x", "hello"],
                           env=dict(board_env, BOARD_URL=srv.url, BOARD_WRITE_PATIENCE="1"),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    finally:
        srv.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "p_abcdefg"
    assert srv.requests == 1
