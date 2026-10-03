"""Test file spooling and auto-cleanup from API perspective."""

from __future__ import annotations

from typing import Annotated

import pytest
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.datastructures import UploadFile
from django_bolt.params import File
from django_bolt.testing import TestClient


@pytest.fixture(autouse=True)
def _upload_limit_for_large_files():
    """The tests upload files up to 6 MB. Allow them, as an app must.

    A request body over BOLT_MAX_UPLOAD_SIZE (default 1 MB) gets 413.
    """
    with override_settings(BOLT_MAX_UPLOAD_SIZE=10 * 1024 * 1024):
        yield


class TestFileAutoCleanup:
    """Test that files are automatically cleaned up after request completes."""

    def test_file_auto_cleanup_without_manual_close(self):
        """
        When handler does NOT manually close UploadFile,
        the framework should auto-close it after request completes.
        """
        api = BoltAPI()
        file_states = []

        @api.post("/upload")
        async def upload_file(file: Annotated[UploadFile, File()]) -> dict:
            content = await file.read()
            file_states.append(
                {
                    "file_obj": file,
                    "content_len": len(content),
                }
            )
            return {"size": len(content)}

        with TestClient(api) as client:
            response = client.post(
                "/upload",
                files={"file": ("test.txt", b"hello world", "text/plain")},
            )
            assert response.status_code == 200

        assert len(file_states) == 1
        assert file_states[0]["file_obj"]._file.closed is True

    def test_large_file_disk_spooled_auto_cleanup(self):
        """Large files that are disk-spooled should also be auto-cleaned up."""
        api = BoltAPI()
        file_states = []

        @api.post("/upload-large")
        async def upload_large(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            content = await file.read()
            file_states.append(
                {
                    "file_obj": file,
                    "rolled_to_disk": file.rolled_to_disk,
                }
            )
            return {"size": len(content)}

        with TestClient(api) as client:
            large_content = b"x" * (2 * 1024 * 1024)  # 2MB
            response = client.post(
                "/upload-large",
                files={"file": ("large.bin", large_content, "application/octet-stream")},
            )
            assert response.status_code == 200

        assert file_states[0]["rolled_to_disk"] is True
        assert file_states[0]["file_obj"]._file.closed is True

    def test_multiple_files_all_auto_cleaned(self):
        """When uploading multiple files, all should be auto-cleaned up."""
        api = BoltAPI()
        file_states = []

        @api.post("/upload-multiple")
        async def upload_multiple(
            files: Annotated[list[UploadFile], File(max_files=10)],
        ) -> dict:
            for f in files:
                await f.read()
                file_states.append(f)
            return {"count": len(files)}

        with TestClient(api) as client:
            response = client.post(
                "/upload-multiple",
                files=[
                    ("files", ("file1.txt", b"content one", "text/plain")),
                    ("files", ("file2.txt", b"content two", "text/plain")),
                    ("files", ("file3.txt", b"content three", "text/plain")),
                ],
            )
            assert response.status_code == 200

        assert len(file_states) == 3
        for f in file_states:
            assert f._file.closed is True

    def test_file_cleanup_on_handler_exception(self):
        """Files should be cleaned up even if handler raises an exception."""
        api = BoltAPI()
        file_states = []

        @api.post("/upload-error")
        async def upload_error(file: Annotated[UploadFile, File()]) -> dict:
            await file.read()
            file_states.append(file)
            raise ValueError("Handler error!")

        with TestClient(api) as client:
            response = client.post(
                "/upload-error",
                files={"file": ("test.txt", b"will error", "text/plain")},
            )
            assert response.status_code == 500

        assert len(file_states) == 1
        assert file_states[0]._file.closed is True

    def test_no_file_descriptor_leak_many_requests(self):
        """Many consecutive file upload requests should not leak file descriptors."""
        api = BoltAPI()
        request_count = 0

        @api.post("/upload-many")
        async def upload_many(file: Annotated[UploadFile, File()]) -> dict:
            nonlocal request_count
            request_count += 1
            await file.read()
            return {"request": request_count}

        with TestClient(api) as client:
            for i in range(200):
                response = client.post(
                    "/upload-many",
                    files={"file": (f"file{i}.txt", f"content {i}".encode(), "text/plain")},
                )
                assert response.status_code == 200

        assert request_count == 200


class TestDiskSpooling:
    """Test that disk spooling works correctly based on file size."""

    def test_large_file_is_disk_spooled(self):
        """
        Files larger than 1MB threshold should be disk-spooled.
        Verify via rolled_to_disk property.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            file_info["size"] = file.size
            file_info["temp_path"] = file._temp_path
            # Verify we can still read the content
            content = await file.read()
            file_info["content_len"] = len(content)
            return {"size": file.size, "rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # Upload 2MB file (above 1MB threshold)
            file_size = 2 * 1024 * 1024
            large_content = b"x" * file_size
            response = client.post(
                "/upload",
                files={"file": ("large.bin", large_content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["rolled_to_disk"] is True
        assert file_info["temp_path"] is not None  # Has temp file path from Rust
        assert file_info["content_len"] == file_size

    def test_small_file_stays_in_memory(self):
        """
        Files smaller than 1MB threshold should stay in memory.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File()]) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            file_info["temp_path"] = file._temp_path
            content = await file.read()
            return {"size": len(content), "rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # Upload 500KB file (under 1MB threshold)
            small_content = b"x" * (500 * 1024)
            response = client.post(
                "/upload",
                files={"file": ("small.bin", small_content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["rolled_to_disk"] is False
        assert file_info["temp_path"] is None  # No temp file path (in memory)

    def test_file_at_threshold_boundary(self):
        """
        File just over 1MB threshold should trigger disk spooling.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=10 * 1024 * 1024)],
        ) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            file_info["size"] = file.size
            return {"rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # Upload exactly 1MB + 1 byte to trigger spooling
            threshold_content = b"x" * (1024 * 1024 + 1)
            response = client.post(
                "/upload",
                files={"file": ("threshold.bin", threshold_content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["rolled_to_disk"] is True

    def test_disk_spooled_file_lazy_open(self):
        """
        Disk-spooled files should be lazily opened - _file should be None
        until actually accessed.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            # Check state BEFORE accessing file content
            file_info["file_before_access"] = file._file
            file_info["temp_path"] = file._temp_path

            # Now access the file
            content = await file.read()

            # Check state AFTER accessing
            file_info["file_after_access"] = file._file

            return {"size": len(content)}

        with TestClient(api) as client:
            large_content = b"x" * (2 * 1024 * 1024)  # 2MB
            response = client.post(
                "/upload",
                files={"file": ("large.bin", large_content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["temp_path"] is not None  # Has Rust temp path
        assert file_info["file_before_access"] is None  # Lazy - not opened yet
        assert file_info["file_after_access"] is not None  # Opened after read()


class TestMemorySpoolThreshold:
    """Test that BOLT_MEMORY_SPOOL_THRESHOLD controls disk spooling."""

    def test_default_threshold_1mb(self):
        """
        With default 1MB threshold, files > 1MB should be disk-spooled.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            await file.read()
            return {"rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # 1.5MB - above default 1MB threshold
            content = b"x" * (1536 * 1024)
            response = client.post(
                "/upload",
                files={"file": ("test.bin", content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["rolled_to_disk"] is True

    def test_file_under_default_threshold_stays_in_memory(self):
        """
        Files under 1MB default threshold should stay in memory.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File()]) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            await file.read()
            return {"rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # 500KB - under 1MB threshold
            content = b"x" * (512 * 1024)
            response = client.post(
                "/upload",
                files={"file": ("test.bin", content, "application/octet-stream")},
            )

        assert response.status_code == 200
        assert file_info["rolled_to_disk"] is False

    @override_settings(BOLT_MEMORY_SPOOL_THRESHOLD=5 * 1024 * 1024)  # 5MB
    def test_custom_threshold_keeps_larger_files_in_memory(self):
        """
        With BOLT_MEMORY_SPOOL_THRESHOLD=5MB, a 2MB file should stay in memory.
        This would fail if the setting wasn't being used (default 1MB would spool it).
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            file_info["temp_path"] = file._temp_path
            await file.read()
            return {"rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # 2MB - above default 1MB but under custom 5MB threshold
            content = b"x" * (2 * 1024 * 1024)
            response = client.post(
                "/upload",
                files={"file": ("test.bin", content, "application/octet-stream")},
            )

        assert response.status_code == 200
        # With 5MB threshold, 2MB file should stay in memory
        assert file_info["rolled_to_disk"] is False
        assert file_info["temp_path"] is None

    @override_settings(BOLT_MEMORY_SPOOL_THRESHOLD=5 * 1024 * 1024)  # 5MB
    def test_custom_threshold_spools_files_over_threshold(self):
        """
        With BOLT_MEMORY_SPOOL_THRESHOLD=5MB, a 6MB file should be disk-spooled.
        """
        api = BoltAPI()
        file_info = {}

        @api.post("/upload")
        async def upload(
            file: Annotated[UploadFile, File(max_size=50 * 1024 * 1024)],
        ) -> dict:
            file_info["rolled_to_disk"] = file.rolled_to_disk
            file_info["temp_path"] = file._temp_path
            await file.read()
            return {"rolled": file.rolled_to_disk}

        with TestClient(api) as client:
            # 6MB - above 5MB custom threshold
            content = b"x" * (6 * 1024 * 1024)
            response = client.post(
                "/upload",
                files={"file": ("test.bin", content, "application/octet-stream")},
            )

        assert response.status_code == 200
        # With 5MB threshold, 6MB file should be disk-spooled
        assert file_info["rolled_to_disk"] is True
        assert file_info["temp_path"] is not None
