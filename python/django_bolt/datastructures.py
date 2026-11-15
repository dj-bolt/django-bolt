"""Data structures for request handling in Django-Bolt."""

from __future__ import annotations

import asyncio
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO

from django.core.files.base import File

__all__ = ["UploadFile"]

# Default max spool size before rolling to disk (1MB)
DEFAULT_MAX_SPOOL_SIZE = 1024 * 1024


class UploadFile:
    """
    Represents an uploaded file with Django-first interface.

    Provides both sync and async file operations, with direct Django FileField/ImageField
    compatibility via the .file property which returns a Django File object.

    For large files (>1MB), Rust spools them to disk. This class lazily opens
    those files only when accessed, avoiding unnecessary memory copies.

    Attributes:
        filename: Original filename from the upload
        content_type: MIME type of the file
        size: Size in bytes
        headers: Additional headers from the multipart part
        file: Django File wrapper for direct use with FileField/ImageField

    Example:
        # Async handler - save directly to Django FileField
        @api.post("/avatar")
        async def upload(avatar: Annotated[UploadFile, File(max_size=2_000_000)]):
            content = await avatar.read()
            profile.avatar.save(avatar.filename, avatar.file)

        # Sync handler - same pattern works
        @api.post("/avatar")
        def upload(avatar: Annotated[UploadFile, File()]):
            content = avatar.file.read()
            profile.avatar.save(avatar.filename, avatar.file)
    """

    __slots__ = ("filename", "content_type", "size", "headers", "_file", "_django_file", "_temp_path")

    def __init__(
        self,
        filename: str,
        content_type: str = "application/octet-stream",
        size: int = 0,
        headers: dict[str, str] | None = None,
        file_data: bytes | None = None,
        max_spool_size: int = DEFAULT_MAX_SPOOL_SIZE,
    ) -> None:
        """
        Initialize an UploadFile for in-memory files.

        Args:
            filename: Original filename from the upload
            content_type: MIME type of the file
            size: Size in bytes
            headers: Additional headers from the multipart part
            file_data: File content as bytes
            max_spool_size: Size threshold before spooling to disk (default 1MB)
        """
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.headers: dict[str, str] = headers or {}
        self._temp_path: str | None = None  # Not a Rust disk-spooled file
        # File must persist beyond a context manager - closed in close() method
        self._file: SpooledTemporaryFile[bytes] | BinaryIO | None = SpooledTemporaryFile(max_size=max_spool_size)  # noqa: SIM115
        self._django_file: File | None = None

        if file_data:
            self._file.write(file_data)
            self._file.seek(0)

    @classmethod
    def from_file_info(cls, file_info: dict[str, Any]) -> UploadFile:
        """
        Create UploadFile from the file_info dict returned by Rust form parsing.

        For disk-spooled files (temp_path provided), we DON'T read the file into memory.
        Instead, we store the path and open lazily on first access.
        This preserves Rust's disk spooling optimization - no memory spike for large files.

        Args:
            file_info: Dict with keys: filename, content, content_type, size, temp_path

        Returns:
            UploadFile instance
        """
        temp_path = file_info.get("temp_path")

        if temp_path:
            # DISK-SPOOLED FILE: Don't read into memory!
            # Create instance without calling __init__ to avoid SpooledTemporaryFile creation.
            # The file will be opened lazily on first access via _ensure_file_open().
            instance = object.__new__(cls)
            instance.filename = file_info.get("filename", "")
            instance.content_type = file_info.get("content_type", "application/octet-stream")
            instance.size = file_info.get("size", 0)
            instance.headers = file_info.get("headers") or {}
            instance._temp_path = temp_path
            instance._file = None  # Lazy - opened on first access
            instance._django_file = None
            return instance
        else:
            # IN-MEMORY FILE: Rust kept this in memory, so use BytesIO (not SpooledTemporaryFile).
            # This avoids Python re-spooling to disk after Rust already decided it fits in memory.
            instance = object.__new__(cls)
            instance.filename = file_info.get("filename", "")
            instance.content_type = file_info.get("content_type", "application/octet-stream")
            instance.size = file_info.get("size", 0)
            instance.headers = file_info.get("headers") or {}
            instance._temp_path = None
            instance._django_file = None
            content = file_info.get("content")
            if content:
                instance._file = BytesIO(content)
            else:
                instance._file = BytesIO()
            return instance

    def _ensure_file_open(self) -> None:
        """
        Ensure file is open, lazily opening disk-spooled files on first access.

        For Rust disk-spooled files, this opens the temp file directly without
        reading it into memory, preserving the disk spooling optimization.
        """
        if self._file is None:
            # Only a disk-spooled file has no open file, and it has a temp path.
            # Open Rust's temp file directly - no memory copy!
            self._file = open(self._temp_path, "r+b")  # noqa: SIM115

    @property
    def file(self) -> File:
        """
        Django File wrapper for the uploaded file.

        Returns a Django File object that can be directly used with FileField/ImageField:
            profile.avatar.save(upload.filename, upload.file)

        The Django File wraps the underlying file without reading all content into memory,
        making it efficient for large files.
        """
        if self._django_file is None:
            self._ensure_file_open()
            self._django_file = File(self._file, name=self.filename)
        return self._django_file

    @property
    def raw_file(self) -> SpooledTemporaryFile[bytes] | BinaryIO:
        """Direct access to the underlying file object."""
        self._ensure_file_open()
        return self._file  # type: ignore[return-value]

    @property
    def rolled_to_disk(self) -> bool:
        """
        Check if file is stored on disk.

        Returns True if either:
        - File was disk-spooled by Rust (large upload)
        - File was rolled to disk by Python's SpooledTemporaryFile
        """
        if self._temp_path:
            return True  # Rust disk-spooled
        if self._file is None:
            return False
        return getattr(self._file, "_rolled", False)

    # === ASYNC METHODS ===
    # These use run_in_executor when file is on disk to avoid blocking

    async def read(self, size: int = -1) -> bytes:
        """
        Read file contents asynchronously.

        Args:
            size: Number of bytes to read (-1 for all)

        Returns:
            File content as bytes
        """
        self._ensure_file_open()
        if self.rolled_to_disk:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._file.read, size)  # type: ignore[union-attr]
        return self._file.read(size)  # type: ignore[union-attr]

    async def seek(self, offset: int, whence: int = 0) -> int:
        """
        Seek to position asynchronously.

        Args:
            offset: Position offset
            whence: Reference point (0=start, 1=current, 2=end)

        Returns:
            New absolute position
        """
        self._ensure_file_open()
        if self.rolled_to_disk:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._file.seek, offset, whence)  # type: ignore[union-attr]
        return self._file.seek(offset, whence)  # type: ignore[union-attr]

    async def write(self, data: bytes) -> int:
        """
        Write data to file asynchronously.

        Args:
            data: Bytes to write

        Returns:
            Number of bytes written
        """
        self._ensure_file_open()
        if self.rolled_to_disk:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._file.write, data)  # type: ignore[union-attr]
        return self._file.write(data)  # type: ignore[union-attr]

    async def close(self) -> None:
        """Close the file asynchronously."""
        if self._file is None:
            return  # Never opened, nothing to close
        if getattr(self._file, "closed", False):
            return
        if self.rolled_to_disk:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._file.close)
        else:
            self._file.close()

    def close_sync(self) -> None:
        """
        Close the file synchronously.

        Used for framework-level auto-cleanup after request handling.
        Note: Rust's NamedTempFile at _temp_path is cleaned up by Rust
        when FormParseResult goes out of scope at end of handle_request().
        """
        if self._file is not None and not getattr(self._file, "closed", False):
            self._file.close()

    def __repr__(self) -> str:
        if self._temp_path:
            opened = "" if self._file is None else ", opened"
            return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size}, disk-spooled{opened})"
        rolled = " (rolled to disk)" if self.rolled_to_disk else ""
        return f"UploadFile(filename={self.filename!r}, content_type={self.content_type!r}, size={self.size}{rolled})"
