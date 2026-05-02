from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = 20.0


def get_free_port(host: str = DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _normalize_python_source(content: str) -> str:
    return textwrap.dedent(content).lstrip()


def _python_list_literal(items: list[str]) -> str:
    if not items:
        return "[]"

    lines = ["["]
    lines.extend(f'    "{item}",' for item in items)
    lines.append("]")
    return "\n".join(lines)


def _terminate_process(process: subprocess.Popen[str], timeout: float = 5.0) -> tuple[str, str]:
    if process.poll() is None:
        if platform.system() == "Windows":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)

        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return stdout, stderr
        except subprocess.TimeoutExpired:
            if platform.system() == "Windows":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    process.kill()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    stdout, stderr = process.communicate()
    return stdout, stderr


def _spawn_process(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid
    return subprocess.Popen(command, **kwargs)


def _raise_process_failure(process: subprocess.Popen[str], message: str) -> None:
    stdout, stderr = _terminate_process(process)
    raise AssertionError(f"{message}\nexit_code={process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}")


@dataclass
class RunningServer:
    # ``project`` is optional so callers that spawn the server outside
    # ``create_server_project``/``ServerProject.start`` (e.g. single-file
    # nanodjango apps spawned via ``python myapp.py runbolt``) can still
    # reuse the wait-and-poll machinery. ``RunningServer`` itself never
    # accesses ``project``.
    project: ServerProject | None
    process: subprocess.Popen[str]
    host: str
    port: int
    startup_path: str
    timeout: float = DEFAULT_TIMEOUT
    client: httpx.Client = field(init=False)
    _stdout: str | None = field(default=None, init=False)
    _stderr: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        self.wait_until_ready()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    def wait_until_ready(self) -> None:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                _raise_process_failure(self.process, "runbolt exited before the server became ready")
            try:
                response = self.client.get(self.url(self.startup_path))
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        _raise_process_failure(self.process, f"Server did not become ready on {self.base_url}{self.startup_path}")

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, self.url(path), **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def wait_for_response(
        self,
        path: str,
        predicate: Callable[[httpx.Response], bool],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        interval: float = 0.2,
        **kwargs: Any,
    ) -> httpx.Response:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                _raise_process_failure(self.process, f"runbolt exited while waiting for {path}")
            try:
                response = self.get(path, **kwargs)
                if predicate(response):
                    return response
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(interval)
        if last_error is not None:
            raise AssertionError(f"Timed out waiting for {path}: {last_error}") from last_error
        raise AssertionError(f"Timed out waiting for {path}")

    def wait_for_json(
        self,
        path: str,
        predicate: Callable[[Any], bool],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        interval: float = 0.2,
        **kwargs: Any,
    ) -> Any:
        response = self.wait_for_response(
            path,
            lambda current: current.status_code == 200 and predicate(current.json()),
            timeout=timeout,
            interval=interval,
            **kwargs,
        )
        return response.json()

    def wait_for_text(
        self,
        path: str,
        expected_substring: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        interval: float = 0.2,
        **kwargs: Any,
    ) -> str:
        response = self.wait_for_response(
            path,
            lambda current: current.status_code == 200 and expected_substring in current.text,
            timeout=timeout,
            interval=interval,
            **kwargs,
        )
        return response.text

    def stop(self) -> tuple[str, str]:
        if self._stdout is None or self._stderr is None:
            self.client.close()
            self._stdout, self._stderr = _terminate_process(self.process)
        return self._stdout, self._stderr

    def __enter__(self) -> RunningServer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


@dataclass
class ServerProject:
    root: Path
    package_name: str
    python_executable: str
    preserve_pythonpath: bool = True

    def path(self, relative_path: str) -> Path:
        return self.root / relative_path

    def write_file(self, relative_path: str, content: str | bytes) -> Path:
        path = self.path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            if isinstance(content, bytes):
                temporary_path.write_bytes(content)
            else:
                temporary_path.write_text(_normalize_python_source(content))
            temporary_path.replace(path)
            current_time = time.time()
            os.utime(path, (current_time, current_time))
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def write_project_api(self, api_body: str) -> Path:
        normalized_api_body = _normalize_python_source(api_body).rstrip()
        source = "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import asyncio",
                "import time",
                "",
                "from django_bolt import BoltAPI, StreamingResponse, WebSocket",
                "",
                "api = BoltAPI()",
                "",
                "",
                '@api.get("/health")',
                "async def health():",
                '    return {"status": "ok"}',
            ]
        )
        if normalized_api_body:
            source = f"{source}\n\n\n{normalized_api_body}"
        source = f"{source}\n"
        return self.write_file(
            f"{self.package_name}/api.py",
            source,
        )

    def read_file(self, relative_path: str) -> str:
        return self.path(relative_path).read_text()

    def start(
        self,
        *,
        dev: bool = False,
        processes: int = 1,
        extra_args: list[str] | None = None,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        startup_path: str = "/health",
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> RunningServer:
        port = get_free_port(host) if port is None else port
        command = [
            self.python_executable,
            str(self.path("manage.py")),
            "runbolt",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if dev:
            command.append("--dev")
        if processes != 1:
            command.extend(["--processes", str(processes)])
        if extra_args:
            command.extend(extra_args)

        process_env = os.environ.copy()
        process_env.update(env or {})
        existing_pythonpath = process_env.get("PYTHONPATH", "")
        if self.preserve_pythonpath and existing_pythonpath:
            process_env["PYTHONPATH"] = f"{self.root}{os.pathsep}{existing_pythonpath}"
        else:
            process_env["PYTHONPATH"] = str(self.root)

        process = _spawn_process(command, cwd=self.root, env=process_env)
        return RunningServer(
            project=self,
            process=process,
            host=host,
            port=port,
            startup_path=startup_path,
            timeout=timeout,
        )


def create_server_project(
    root: Path,
    *,
    package_name: str = "testproj",
    project_api_body: str = "",
    urls_content: str = "urlpatterns = []\n",
    settings_extra: str = "",
    extra_files: dict[str, str | bytes] | None = None,
    installed_apps: list[str] | None = None,
    middleware: list[str] | None = None,
    templates: list[dict[str, Any]] | None = None,
    python_executable: str | None = None,
    preserve_pythonpath: bool = True,
) -> ServerProject:
    project = ServerProject(
        root=root,
        package_name=package_name,
        python_executable=python_executable or sys.executable,
        preserve_pythonpath=preserve_pythonpath,
    )
    root.mkdir(parents=True, exist_ok=True)
    package_dir = root / package_name
    package_dir.mkdir(parents=True, exist_ok=True)

    installed_apps = installed_apps or []
    middleware = middleware or []
    templates = templates or []
    all_installed_apps = [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django_bolt",
        *installed_apps,
    ]

    project.write_file(f"{package_name}/__init__.py", "")
    settings_source = "\n".join(
        [
            "from pathlib import Path",
            "",
            "BASE_DIR = Path(__file__).resolve().parent.parent",
            'SECRET_KEY = "django-bolt-server-integration"',
            "DEBUG = True",
            'ALLOWED_HOSTS = ["*"]',
            f"INSTALLED_APPS = {_python_list_literal(all_installed_apps)}",
            "DATABASES = {",
            '    "default": {',
            '        "ENGINE": "django.db.backends.sqlite3",',
            '        "NAME": str(BASE_DIR / "db.sqlite3"),',
            "    }",
            "}",
            "USE_TZ = True",
            'TIME_ZONE = "UTC"',
            f'ROOT_URLCONF = "{package_name}.urls"',
            'DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"',
            f"MIDDLEWARE = {middleware!r}",
            f"TEMPLATES = {templates!r}",
        ]
    )
    normalized_settings_extra = _normalize_python_source(settings_extra).rstrip()
    if normalized_settings_extra:
        settings_source = f"{settings_source}\n\n{normalized_settings_extra}"
    settings_source = f"{settings_source}\n"
    project.write_file(
        f"{package_name}/settings.py",
        settings_source,
    )
    project.write_file(f"{package_name}/urls.py", urls_content)
    project.write_project_api(project_api_body)
    project.write_file(
        "manage.py",
        f"""
        import os
        import sys

        if __name__ == "__main__":
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{package_name}.settings")
            from django.core.management import execute_from_command_line

            execute_from_command_line(sys.argv)
        """,
    )

    for relative_path, content in (extra_files or {}).items():
        project.write_file(relative_path, content)

    return project


class SimpleWebSocketClient:
    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.headers = headers or {}
        self.timeout = timeout
        self.socket: socket.socket | None = None

    def connect(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request_lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for header, value in self.headers.items():
            request_lines.append(f"{header}: {value}")
        request_lines.append("")
        request_lines.append("")

        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.sendall("\r\n".join(request_lines).encode("utf-8"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("WebSocket handshake failed before headers completed")
            response += chunk

        header_bytes, _ = response.split(b"\r\n\r\n", 1)
        header_text = header_bytes.decode("utf-8", errors="replace")
        status_line = header_text.splitlines()[0]
        assert "101" in status_line, f"Unexpected WebSocket handshake response: {header_text}"

        headers: dict[str, str] = {}
        for line in header_text.splitlines()[1:]:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        accept = headers.get("sec-websocket-accept")
        expected_accept = base64.b64encode(
            hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest()
        ).decode()
        assert accept == expected_accept, f"Unexpected Sec-WebSocket-Accept: {accept}"
        self.socket = sock

    def _require_socket(self) -> socket.socket:
        if self.socket is None:
            raise AssertionError("WebSocket client is not connected")
        return self.socket

    def _recv_exact(self, length: int) -> bytes:
        sock = self._require_socket()
        chunks = bytearray()
        while len(chunks) < length:
            chunk = sock.recv(length - len(chunks))
            if not chunk:
                raise AssertionError("WebSocket connection closed while reading a frame")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._require_socket()
        frame = bytearray()
        frame.append(0x80 | opcode)

        payload_length = len(payload)
        mask = secrets.token_bytes(4)
        if payload_length < 126:
            frame.append(0x80 | payload_length)
        elif payload_length < 65536:
            frame.append(0x80 | 126)
            frame.extend(payload_length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(payload_length.to_bytes(8, "big"))

        frame.extend(mask)
        frame.extend(payload[index] ^ mask[index % 4] for index in range(payload_length))
        sock.sendall(frame)

    def _receive_frame(self) -> tuple[int, bytes]:
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        payload_length = header[1] & 0x7F

        if payload_length == 126:
            payload_length = int.from_bytes(self._recv_exact(2), "big")
        elif payload_length == 127:
            payload_length = int.from_bytes(self._recv_exact(8), "big")

        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(payload_length)
        if masked:
            payload = bytes(payload[index] ^ mask[index % 4] for index in range(payload_length))
        return opcode, payload

    def send_text(self, message: str) -> None:
        self._send_frame(0x1, message.encode("utf-8"))

    def receive_text(self) -> str:
        opcode, payload = self._receive_frame()
        assert opcode == 0x1, f"Expected text frame, got opcode {opcode}"
        return payload.decode("utf-8")

    def receive_close(self) -> tuple[int, str]:
        opcode, payload = self._receive_frame()
        assert opcode == 0x8, f"Expected close frame, got opcode {opcode}"
        if len(payload) >= 2:
            code = int.from_bytes(payload[:2], "big")
            reason = payload[2:].decode("utf-8")
            return code, reason
        return 1005, ""

    def close(self, code: int = 1000) -> None:
        if self.socket is None:
            return
        with contextlib.suppress(OSError):
            self._send_frame(0x8, code.to_bytes(2, "big"))
        with contextlib.suppress(OSError):
            self.socket.close()
        self.socket = None

    def __enter__(self) -> SimpleWebSocketClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
