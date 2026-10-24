"""下载/校验 + 清单检查（本地真实 HTTP 服务器，不用 mock 假协议）。"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.update.client import check_for_update
from src.update.downloader import DownloadError, download_package
from src.update.manifest import parse_manifest
from tests.update_helpers import make_package, sha256_bytes


class _QuietHandler(SimpleHTTPRequestHandler):
    redirect_target = "/manifest.json"

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", self.redirect_target)
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, *args):  # noqa: D401 - 静默测试服务器日志
        pass


@pytest.fixture()
def http_server(tmp_path: Path):
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        yield root, base
    finally:
        server.shutdown()
        server.server_close()


def _manifest(base: str, filename: str, data: bytes, **overrides):
    payload = {
        "version": "1.0.2",
        "package_url": "%s/%s" % (base, filename),
        "sha256": sha256_bytes(data),
        "package_size": len(data),
        "release_notes": ["测试"],
        "update_type": "incremental",
        "from_versions": ["1.0.1"],
    }
    payload.update(overrides)
    return parse_manifest(payload, allow_http=True)


def test_download_success_with_progress(tmp_path: Path, http_server) -> None:
    root, base = http_server
    data = make_package(tmp_path / "pkg.zip", version="1.0.2",
                        files={"_internal/a.dll": b"x" * 5000}).read_bytes()
    (root / "update.zip").write_bytes(data)
    manifest = _manifest(base, "update.zip", data)
    progress = []
    target = asyncio.run(download_package(manifest, tmp_path / "dl",
                                          on_progress=progress.append))
    assert target.exists()
    assert target.read_bytes() == data
    assert progress and progress[-1].downloaded == len(data)
    assert not list((tmp_path / "dl").glob("*.part"))


def test_download_sha_mismatch_leaves_no_package(tmp_path: Path, http_server) -> None:
    root, base = http_server
    data = b"real package bytes"
    (root / "u.zip").write_bytes(data)
    manifest = _manifest(base, "u.zip", data, sha256="0" * 64)
    with pytest.raises(DownloadError, match="SHA-256"):
        asyncio.run(download_package(manifest, tmp_path / "dl"))
    assert not list((tmp_path / "dl").glob("*.zip"))
    assert not list((tmp_path / "dl").glob("*.part"))


def test_download_size_mismatch(tmp_path: Path, http_server) -> None:
    root, base = http_server
    data = b"abc"
    (root / "u.zip").write_bytes(data)
    manifest = _manifest(base, "u.zip", data, package_size=999)
    with pytest.raises(DownloadError):
        asyncio.run(download_package(manifest, tmp_path / "dl"))


def test_download_404(tmp_path: Path, http_server) -> None:
    _, base = http_server
    manifest = _manifest(base, "missing.zip", b"x")
    with pytest.raises(DownloadError, match="HTTP 404"):
        asyncio.run(download_package(manifest, tmp_path / "dl"))


def test_download_cancel(tmp_path: Path, http_server) -> None:
    root, base = http_server
    data = b"x" * 200000
    (root / "u.zip").write_bytes(data)
    manifest = _manifest(base, "u.zip", data)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadError, match="取消"):
        asyncio.run(download_package(manifest, tmp_path / "dl", cancel=cancel))


def test_download_retries_transient_server_error(tmp_path: Path) -> None:
    class FlakyHandler(_QuietHandler):
        requests = 0

        def do_GET(self):
            type(self).requests += 1
            if type(self).requests == 1:
                self.send_response(500)
                self.end_headers()
                return
            return super().do_GET()

    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(FlakyHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        data = b"retry-ok"
        (root / "u.zip").write_bytes(data)
        manifest = _manifest(
            "http://127.0.0.1:%d" % server.server_address[1], "u.zip", data)
        target = asyncio.run(download_package(
            manifest, tmp_path / "dl", attempts=2))
        assert target.read_bytes() == data
        assert FlakyHandler.requests == 2
    finally:
        server.shutdown()
        server.server_close()


def test_verified_existing_download_is_reused(tmp_path: Path, http_server) -> None:
    root, base = http_server
    data = b"already-downloaded"
    (root / "u.zip").write_bytes(data)
    manifest = _manifest(base, "u.zip", data)
    dest = tmp_path / "dl"
    first = asyncio.run(download_package(manifest, dest))
    (root / "u.zip").unlink()              # 若再次请求会得到 404
    second = asyncio.run(download_package(manifest, dest))
    assert second == first
    assert second.read_bytes() == data


def test_check_for_update_uses_real_http(tmp_path: Path, http_server) -> None:
    root, base = http_server
    payload = {"version": "1.0.2", "package_url": base + "/u.zip",
               "sha256": "a" * 64, "release_notes": ["x"],
               "update_type": "full", "from_versions": []}
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = asyncio.run(check_for_update("1.0.1", base + "/manifest.json",
                                          allow_http=True))
    assert result.status == "update"
    assert result.has_update and str(result.manifest.version) == "1.0.2"

    latest = asyncio.run(check_for_update("1.0.2", base + "/manifest.json",
                                          allow_http=True))
    assert latest.status == "latest"


def test_check_for_update_never_raises_on_network_error(tmp_path: Path,
                                                        http_server) -> None:
    _, base = http_server
    result = asyncio.run(check_for_update("1.0.1", base + "/nope.json",
                                          allow_http=True))
    assert result.status == "error"
    assert result.error


def test_check_for_update_follows_https_style_redirect(tmp_path: Path,
                                                       http_server) -> None:
    """GitHub releases/latest/download 会 302，客户端必须跟随重定向。"""
    root, base = http_server
    payload = {"version": "1.0.2", "package_url": base + "/u.zip",
               "sha256": "a" * 64, "release_notes": ["x"],
               "update_type": "full", "from_versions": []}
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = asyncio.run(check_for_update("1.0.1", base + "/redirect",
                                          allow_http=True))
    assert result.status == "update", result.error
    assert str(result.manifest.version) == "1.0.2"


def test_check_for_update_disabled_when_no_url() -> None:
    result = asyncio.run(check_for_update("1.0.1", ""))
    assert result.status == "disabled"
    assert "未配置" in (result.error or "")
