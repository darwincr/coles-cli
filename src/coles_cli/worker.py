from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from coles_cli.conf import WORKER_IDLE_TIMEOUT_S, coles_cli_home
from coles_cli.session import ColesSession, _locks_dir, session_lock

CONNECT_TIMEOUT_S = 60


def _worker_dir() -> Path:
    path = coles_cli_home() / "workers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)


def socket_path(name: str) -> Path:
    return _worker_dir() / f"{_safe_name(name)}.sock"


def _log_path(name: str) -> Path:
    path = coles_cli_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"worker-{_safe_name(name)}.log"


def _pid_path(name: str) -> Path:
    path = coles_cli_home() / "workers"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{_safe_name(name)}.pid"


@contextmanager
def _startup_lock(name: str):
    import fcntl

    path = _locks_dir() / f"{name}.worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _send_request(path: Path, payload: dict, *, timeout: float | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        if timeout is not None:
            client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    if not chunks:
        raise RuntimeError("coles-cli worker closed the connection without a response")
    return json.loads(b"".join(chunks).decode("utf-8"))


def _try_request(name: str, payload: dict, *, timeout: float | None = None) -> dict | None:
    path = socket_path(name)
    if not path.exists():
        return None
    try:
        return _send_request(path, payload, timeout=timeout)
    except socket.timeout:
        return None
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None


def _recent_log(name: str, *, max_lines: int = 40) -> str:
    path = _log_path(name)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-max_lines:])


def _startup_error(name: str, reason: str) -> RuntimeError:
    message = f"coles-cli worker for session {name!r} {reason}"
    recent_log = _recent_log(name)
    if recent_log:
        message = f"{message}\nRecent worker log ({_log_path(name)}):\n{recent_log}"
    return RuntimeError(message)


def _start_worker(name: str) -> subprocess.Popen:
    log = _log_path(name).open("ab", buffering=0)
    env = os.environ.copy()
    env["COLES_CLI_WORKER"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "coles_cli.worker", name],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        close_fds=True,
        start_new_session=True,
    )


def _terminate_worker_startup(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _wait_for_worker(name: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise _startup_error(name, f"exited before startup completed with code {process.returncode}")
        try:
            response = _send_request(socket_path(name), {"ping": True}, timeout=1)
            if response.get("returncode") == 0:
                return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            time.sleep(0.2)
    raise _startup_error(name, f"did not start within {CONNECT_TIMEOUT_S} seconds")


def run_via_worker(name: str, argv: list[str]) -> int:
    payload = {"argv": argv}
    response = _request_existing_worker(name, argv, payload)
    if response is None:
        with _startup_lock(name):
            response = _request_existing_worker(name, argv, payload)
            if response is None:
                process = _start_worker(name)
                try:
                    _wait_for_worker(name, process)
                except Exception:
                    _terminate_worker_startup(process)
                    raise
                response = _send_command(name, argv, payload)

    if response is None:
        response = _send_command(name, argv, payload)

    stdout = response.get("stdout") or ""
    stderr = response.get("stderr") or ""
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    return int(response.get("returncode") or 0)


def _request_existing_worker(name: str, argv: list[str], payload: dict) -> dict | None:
    path = socket_path(name)
    if not path.exists():
        return None
    try:
        _send_request(path, {"ping": True}, timeout=1)
    except socket.timeout:
        return _send_command(name, argv, payload)
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    return _send_command(name, argv, payload)


def _send_command(name: str, argv: list[str], payload: dict) -> dict:
    timeout = _request_timeout(argv)
    try:
        return _send_request(socket_path(name), payload, timeout=timeout)
    except socket.timeout:
        return _timeout_response(argv, timeout)


def _request_timeout(argv: list[str]) -> int:
    explicit = _timeout_arg(argv)
    if explicit is not None:
        return max(60, explicit + 30)
    if argv[:2] == ["login", "--interactive"] or (argv[:2] == ["auth", "interactive"]):
        return 360
    if argv[:2] in (["cart", "checkout"], ["shoppingcart", "checkout"]):
        return 240
    return int(os.environ.get("COLES_CLI_REQUEST_TIMEOUT", "120"))


def _timeout_arg(argv: list[str]) -> int | None:
    for index, value in enumerate(argv):
        if value == "--timeout" and index + 1 < len(argv) and argv[index + 1].isdigit():
            return int(argv[index + 1])
        if value.startswith("--timeout="):
            raw = value.split("=", 1)[1]
            if raw.isdigit():
                return int(raw)
    return None


def _timeout_response(argv: list[str], timeout: int) -> dict:
    message = f"Coles worker did not return within {timeout} seconds; the browser action may still be running. Run `coles session stop` to terminate it."
    if "--json" in argv:
        return {
            "returncode": 1,
            "stdout": json.dumps({"ok": False, "error": {"type": "worker_timeout", "message": message}}) + "\n",
            "stderr": "",
        }
    return {"returncode": 1, "stdout": "", "stderr": f"error: worker_timeout: {message}\n"}


def stop_worker(name: str) -> None:
    response = _try_request(name, {"shutdown": True}, timeout=2)
    if response is None or response.get("busy"):
        _terminate_worker_pid(name)
        return
    path = socket_path(name)
    deadline = time.monotonic() + 10
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)


def _read_worker_pid(name: str) -> int | None:
    try:
        raw = _pid_path(name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(raw) if raw.isdigit() else None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_worker_pid(name: str) -> None:
    pid = _read_worker_pid(name)
    if pid is None or not _pid_is_running(pid):
        _cleanup_worker_files(name)
        return
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        _cleanup_worker_files(name)
        return
    deadline = time.monotonic() + 5
    while _pid_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_is_running(pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    _cleanup_worker_files(name)


def _cleanup_worker_files(name: str) -> None:
    for path in (socket_path(name), _pid_path(name)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _execute_request(session: ColesSession, argv: list[str]) -> dict:
    import contextlib
    import io

    from coles_cli.cli import _execute_verb, _parse_args
    from playwright._impl._errors import TargetClosedError

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            args = _parse_args(argv)
            session.ensure_browser()
            try:
                returncode = _execute_verb(args, session)
            except TargetClosedError:
                if getattr(args, "verb", "") in {"login", "auth-interactive"}:
                    returncode = 1
                    print("error: browser closed before interactive login completed; rerun `coles login --interactive --wait --timeout 300`", file=sys.stderr)
                    return {"returncode": returncode, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
                session.close()
                session.ensure_browser()
                returncode = _execute_verb(args, session)
            except Exception as exc:
                if not _is_driver_connection_closed(exc):
                    raise
                session.close()
                session.ensure_browser()
                returncode = _execute_verb(args, session)
        except SystemExit as exc:
            returncode = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001
            returncode = 1
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return {"returncode": returncode, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}


def _is_driver_connection_closed(exc: Exception) -> bool:
    return "Connection closed while reading from the driver" in str(exc)


def serve(name: str) -> int:
    path = socket_path(name)

    with session_lock(name), ColesSession(name) as session:
        _pid_path(name).write_text(str(os.getpid()), encoding="utf-8")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(64)
        server.settimeout(1)
        work_queue: queue.Queue[tuple[socket.socket, list[str]]] = queue.Queue()
        action_busy = threading.Event()
        shutdown_event = threading.Event()
        idle_deadline = time.monotonic() + WORKER_IDLE_TIMEOUT_S
        acceptor = threading.Thread(target=_accept_connections, args=(server, work_queue, action_busy, shutdown_event), daemon=True)
        acceptor.start()
        try:
            while not shutdown_event.is_set() and time.monotonic() < idle_deadline:
                try:
                    conn, argv = work_queue.get(timeout=1)
                except queue.Empty:
                    continue
                idle_deadline = time.monotonic() + WORKER_IDLE_TIMEOUT_S
                action_busy.set()
                try:
                    response = _execute_request(session, argv)
                    _send_response(conn, response)
                finally:
                    try:
                        conn.close()
                    finally:
                        action_busy.clear()
        finally:
            shutdown_event.set()
            server.close()
            acceptor.join(timeout=1)
            _cleanup_worker_files(name)
    return 0


def _accept_connections(
    server: socket.socket,
    work_queue: queue.Queue[tuple[socket.socket, list[str]]],
    action_busy: threading.Event,
    shutdown_event: threading.Event,
) -> None:
    while not shutdown_event.is_set():
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        _route_connection(conn, work_queue, action_busy, shutdown_event)


def _route_connection(
    conn: socket.socket,
    work_queue: queue.Queue[tuple[socket.socket, list[str]]],
    action_busy: threading.Event,
    shutdown_event: threading.Event,
) -> None:
    raw = b""
    while not raw.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        raw += chunk
    try:
        request = json.loads(raw.decode("utf-8")) if raw else {}
        busy = action_busy.is_set() or not work_queue.empty()
        if request.get("shutdown"):
            if busy:
                _send_response(conn, {"returncode": 0, "stdout": "", "stderr": "", "busy": True})
            else:
                shutdown_event.set()
                _send_response(conn, {"returncode": 0, "stdout": "", "stderr": ""})
            conn.close()
        elif request.get("ping"):
            _send_response(conn, {"returncode": 0, "stdout": "", "stderr": "", "busy": busy})
            conn.close()
        else:
            argv = list(request.get("argv") or [])
            work_queue.put((conn, argv))
    except Exception as exc:  # noqa: BLE001
        _send_response(conn, {"returncode": 1, "stdout": "", "stderr": f"error: worker: {exc}\n"})
        conn.close()


def _send_response(conn: socket.socket, response: dict) -> None:
    try:
        conn.sendall(json.dumps(response).encode("utf-8"))
    except BrokenPipeError:
        pass


def _busy_response(argv: list[str]) -> dict:
    message = "Coles worker is busy running another command; wait for it to finish or run `coles session stop` to terminate it."
    if "--json" in argv:
        return {
            "returncode": 1,
            "stdout": json.dumps({"ok": False, "error": {"type": "worker_busy", "message": message}}) + "\n",
            "stderr": "",
        }
    return {"returncode": 1, "stdout": "", "stderr": f"error: worker_busy: {message}\n"}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m coles_cli.worker <session>", file=sys.stderr)
        return 2
    return serve(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
