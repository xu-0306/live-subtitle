"""Update check: version order, release parsing, throttling and the real HTTP path (against a local server)."""
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gui import update_check as uc

ROOT = Path(__file__).resolve().parents[1]

# The same tables are mirrored in chrome-extension/tests/update-check.test.js.
NEWER = [("0.4.2", "0.4.1"), ("0.5.0", "0.4.9"), ("1.0.0", "0.99.99"), ("v0.10.0", "0.9.0"),
         ("0.4.1.1", "0.4.1"), ("1.0.0", "1.0.0-rc.1"), ("1.0.0-rc.2", "1.0.0-rc.1"),
         ("1.0.0-rc.10", "1.0.0-rc.2"), ("1.0.0-rc.1", "1.0.0-beta"), ("1.0.0-alpha.1", "1.0.0-alpha")]
NOT_NEWER = [("0.4.1", "0.4.1"), ("0.4.0", "0.4.1"), ("v0.4.1", "0.4.1"), ("1.0.0-rc.1", "1.0.0"),
             ("1.0.0+build.5", "1.0.0"), ("0.4.1.0", "0.4.1"), ("0.4.9", "0.5.0")]
INVALID = ["", None, "latest", "1.2", "v", "1.2.3.4.5", "1.2.x", "1.2.3-", "1..3", 5]


@pytest.mark.parametrize("candidate, current", NEWER)
def test_newer_versions(candidate, current):
    assert uc.is_newer(candidate, current)
    assert not uc.is_newer(current, candidate)


@pytest.mark.parametrize("candidate, current", NOT_NEWER)
def test_equal_or_older_versions_are_not_newer(candidate, current):
    assert not uc.is_newer(candidate, current)


@pytest.mark.parametrize("bad", INVALID)
def test_invalid_versions_never_count_as_newer(bad):
    assert uc.parse_version(bad) is None
    assert not uc.is_newer(bad, "0.4.1") and not uc.is_newer("9.9.9", bad)


def _payload(**overrides):
    payload = {"tag_name": "v0.5.0", "name": "Live Subtitle v0.5.0", "draft": False, "prerelease": False,
               "html_url": "https://github.com/xu-0306/live-subtitle/releases/tag/v0.5.0",
               "published_at": "2026-12-01T00:00:00Z", "body": "# What's new\r\n\r\n- faster\r\n"}
    payload.update(overrides)
    return payload


def test_parse_release_reads_the_github_shape():
    release = uc.parse_release(_payload())
    assert (release.version, release.tag) == ("0.5.0", "v0.5.0")
    assert release.url == "https://github.com/xu-0306/live-subtitle/releases/tag/v0.5.0"
    assert release.notes == "# What's new\n\n- faster"
    assert release.published == "2026-12-01T00:00:00Z"


@pytest.mark.parametrize("payload", [None, [], "x", {}, _payload(draft=True), _payload(prerelease=True),
                                     _payload(tag_name="latest"), _payload(tag_name=None), _payload(tag_name=5)])
def test_parse_release_rejects_what_is_not_an_installable_release(payload):
    assert uc.parse_release(payload) is None


@pytest.mark.parametrize("url", [None, 5, "", "http://github.com/x/y", "https://evil.example/x",
                                 "https://github.com.evil.example/x", "javascript:alert(1)", "file:///C:/x"])
def test_download_link_must_be_an_https_github_page(url):
    assert uc.parse_release(_payload(html_url=url)).url == uc.RELEASES_PAGE


def test_missing_or_oversized_notes_are_tolerated():
    assert uc.parse_release(_payload(body=None)).notes == ""
    assert len(uc.parse_release(_payload(body="x" * 50_000)).notes) <= uc.MAX_NOTES + 2


def test_check_new_current_and_older_than_this_build():
    def reply(version):
        return lambda: uc.parse_release(_payload(tag_name=f"v{version}"))
    new = uc.check("0.4.1", reply("0.5.0"))
    assert (new.status, new.release.version) == ("new", "0.5.0")
    assert uc.check("0.4.1", reply("0.4.1")).status == "current"
    assert uc.check("0.6.0-dev", reply("0.5.0")).status == "current"  # a build ahead of the release stays quiet


def test_check_turns_every_failure_into_an_error_result():
    def offline():
        raise uc.UpdateCheckError("Could not reach GitHub.")

    def broken():
        raise RuntimeError("boom")
    assert uc.check("0.4.1", offline) == uc.Result("error", None, "Could not reach GitHub.")
    assert uc.check("0.4.1", broken).error == "RuntimeError: boom"
    assert uc.check("", lambda: pytest.fail("must not ask GitHub without a version")).status == "error"


# --- the real HTTP path, against a local server -------------------------------------------------

class _Server:
    def __init__(self):
        outer = self
        self.status, self.body, self.requests = 200, b"{}", []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *_args):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/latest"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = _Server()
    yield srv
    srv.close()


def test_fetch_parses_a_real_response_and_sends_only_a_version_query(server):
    server.body = json.dumps(_payload()).encode()
    release = uc.fetch_latest_release(server.url, timeout=3)
    assert release.version == "0.5.0"
    (path, headers), = server.requests
    assert path == "/latest"
    assert headers["User-Agent"] == "Live-Subtitle-update-check"
    assert not {"Authorization", "Cookie", "Referer"} & set(headers)


@pytest.mark.parametrize("status, message", [(404, "No release"), (403, "limiting"), (429, "limiting"),
                                             (500, "HTTP 500")])
def test_fetch_maps_http_errors_to_short_messages(server, status, message):
    server.status = status
    with pytest.raises(uc.UpdateCheckError, match=message):
        uc.fetch_latest_release(server.url, timeout=3)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"tag_name": "nightly"}', b"\xff\xfe"])
def test_fetch_refuses_answers_it_cannot_use(server, body):
    server.body = body
    with pytest.raises(uc.UpdateCheckError, match="does not understand"):
        uc.fetch_latest_release(server.url, timeout=3)


def test_fetch_refuses_an_oversized_answer(server):
    # Built here, not parametrized: a 1 MB test id overflows the PYTEST_CURRENT_TEST environment variable.
    server.body = b'{"x":"' + b"a" * (uc.MAX_BYTES + 10) + b'"}'
    with pytest.raises(uc.UpdateCheckError, match="does not understand"):
        uc.fetch_latest_release(server.url, timeout=3)


def test_offline_is_a_quiet_error_result_not_an_exception():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # nothing listens on `port` now: the connection is refused straight away
    result = uc.check("0.4.1", lambda: uc.fetch_latest_release(f"http://127.0.0.1:{port}/", timeout=2))
    assert result.status == "error" and result.error == "Could not reach GitHub."


def test_a_server_that_never_answers_times_out():
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)  # accepts the connection into the backlog and never replies
        port = silent.getsockname()[1]
        result = uc.check("0.4.1", lambda: uc.fetch_latest_release(f"http://127.0.0.1:{port}/", timeout=0.5))
    assert result.status == "error" and result.error == "Could not reach GitHub."


# --- state, throttling, wording ---------------------------------------------------------------

def test_state_survives_a_round_trip_and_a_corrupt_or_missing_file(tmp_path):
    path = tmp_path / "update-check.json"
    assert uc.load_state(path) == {}
    assert uc.save_state(path, {"enabled": False, "skipped": "0.5.0"})
    assert uc.load_state(path) == {"enabled": False, "skipped": "0.5.0"}
    path.write_text("{ torn", encoding="utf-8")
    assert uc.load_state(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert uc.load_state(path) == {}
    assert not uc.save_state(tmp_path / "missing" / "x.json", {})  # unwritable: reported, not raised


def test_throttle_checks_daily_after_success_and_hourly_after_failure():
    now = 1_000_000.0
    assert uc.is_due({}, now)
    assert not uc.is_due({"checked_at": now - 3600, "ok": True}, now)
    assert uc.is_due({"checked_at": now - uc.INTERVAL_OK, "ok": True}, now)
    assert not uc.is_due({"checked_at": now - 600, "ok": False}, now)
    assert uc.is_due({"checked_at": now - uc.INTERVAL_FAIL, "ok": False}, now)
    assert uc.is_due({"checked_at": now + 5000, "ok": True}, now)  # clock set back
    assert uc.is_due({"checked_at": "yesterday"}, now)


def test_switching_it_off_stops_the_startup_check():
    assert uc.is_enabled({}) and not uc.is_enabled({"enabled": False})
    assert not uc.is_due({"enabled": False}, 1_000_000.0)


def test_record_result_and_skip():
    release = uc.parse_release(_payload())
    state = uc.record_result({"skipped": "0.4.9"}, uc.Result("new", release), now=42.0)
    assert state == {"skipped": "0.4.9", "checked_at": 42.0, "ok": True, "latest": "0.5.0"}
    assert not uc.is_skipped(state, release)
    assert uc.is_skipped({"skipped": "0.5.0"}, release)
    failed = uc.record_result(state, uc.Result("error", error="x"), now=50.0)
    assert (failed["ok"], failed["latest"], failed["checked_at"]) == (False, "0.5.0", 50.0)


def test_describe_state():
    assert uc.describe_state({}, "0.4.1") == "Not checked yet."
    assert "could not reach" in uc.describe_state({"checked_at": 1.0, "ok": False}, "0.4.1")
    assert "version 0.5.0 is available" in uc.describe_state(
        {"checked_at": 1.0, "ok": True, "latest": "0.5.0"}, "0.4.1")
    assert "latest version" in uc.describe_state({"checked_at": 1.0, "ok": True, "latest": "0.4.1"}, "0.4.1")


# --- one version, one place ---------------------------------------------------------------------

def test_the_app_version_is_the_extension_manifest_version():
    manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    assert uc.read_app_version() == manifest["version"]
    assert uc.parse_version(manifest["version"])


def test_unreadable_manifest_means_unknown_version(tmp_path):
    assert uc.read_app_version(tmp_path / "missing.json") == ""
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert uc.read_app_version(tmp_path / "bad.json") == ""
    (tmp_path / "odd.json").write_text('{"version": "soon"}', encoding="utf-8")
    assert uc.read_app_version(tmp_path / "odd.json") == ""


def test_the_packaged_app_carries_the_manifest():
    spec = (ROOT / "live-subtitle.spec").read_text(encoding="utf-8")
    assert re.search(r'\(str\(ROOT / "chrome-extension" / "manifest\.json"\), "chrome-extension"\)', spec)


def test_the_extension_asks_the_same_two_urls():
    source = (ROOT / "chrome-extension" / "shared" / "update-check.js").read_text(encoding="utf-8")
    assert f'"{uc.RELEASES_API}"' in source
    assert f'"{uc.RELEASES_PAGE}"' in source
