"""FileResponse with BOLT_ALLOWED_FILE_PATHS on each OS that gets a wheel.

Each OS names a path in its own way. Windows `canonicalize` gives a `\\\\?\\` path,
and a temp dir can have an 8.3 short name. On macOS, /var is a link, and
`realpath` accepts `..` after a file. The platform smoke jobs run these tests
on macOS and Windows, and the test jobs run them on Linux.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from django_bolt import BoltAPI, responses
from django_bolt.responses import FileResponse
from django_bolt.testing import TestClient

pytestmark = pytest.mark.platform_smoke


@pytest.fixture
def tree(settings, monkeypatch, tmp_path):
    """Allow one dir with a file in it. Return it and a dir next to it."""
    public = tmp_path / "public"
    outside = tmp_path / "outside"
    public.mkdir()
    outside.mkdir()
    (public / "known.txt").write_text("known")
    settings.BOLT_ALLOWED_FILE_PATHS = [str(public)]
    monkeypatch.setattr(responses, "_ALLOWED_FILE_PATHS_INITIALIZED", False)
    monkeypatch.setattr(responses, "_ALLOWED_FILE_PATHS_CACHE", None)
    responses.initialize_file_response_settings()
    return public, outside


def _status(path: Path) -> int:
    api = BoltAPI()

    @api.get("/file")
    def file():
        return FileResponse(str(path))

    with TestClient(api) as client:
        return client.get("/file").status_code


def test_a_file_in_the_allowed_dir_is_served(tree):
    public, _ = tree

    assert _status(public / "known.txt") == 200


def test_a_missing_file_in_the_allowed_dir_is_a_404(tree):
    public, _ = tree

    assert _status(public / "missing.txt") == 404


@pytest.mark.parametrize("name", ["secret.txt", "missing.txt"])
def test_a_path_out_of_the_allowed_dir_is_a_403(tree, name):
    _, outside = tree
    (outside / "secret.txt").write_text("secret")

    assert _status(outside / name) == 403


def test_a_path_that_names_the_allowed_dir_in_another_form_gets_the_same_statuses(tree):
    # pytest resolves its temp dir. The OS temp dir can name it through /var on macOS, or a short name on Windows.
    public, _ = tree
    temp = Path(tempfile.gettempdir())
    other = temp / public.relative_to(temp.resolve())

    assert (_status(other / "known.txt"), _status(other / "missing.txt")) == (200, 404)


def test_the_status_of_a_path_with_file_dotdot_does_not_show_whether_the_file_exists(tree):
    # On Linux, the lookup of secret.txt/.. fails when secret.txt is a file. Windows removes ".." from the text first.
    public, outside = tree
    path = public / ".." / outside.name / "secret.txt" / ".." / ".." / public.name / "known.txt"
    without_file = _status(path)
    (outside / "secret.txt").write_text("secret")

    assert _status(path) == without_file
