"""Tests for UploadFile functionality."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

import msgspec
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import File as DjangoFile

from django_bolt import BoltAPI, FileSize, UploadFile
from django_bolt.datastructures import UploadFile as UploadFileClass
from django_bolt.params import File, Form
from django_bolt.testing import TestClient
from tests.test_models import Document


# Define structs at module level to avoid NameError with get_type_hints
class ProfileForm(msgspec.Struct):
    name: str
    avatar: UploadFile


class DocumentForm(msgspec.Struct):
    title: str
    documents: list[UploadFile]


class RegistrationForm(msgspec.Struct):
    username: str
    email: str
    age: int
    profile_photo: UploadFile
    resume: UploadFile


class ProfileFormOptional(msgspec.Struct):
    name: str
    avatar: UploadFile | None = None


class ProfileFormAnnotated(msgspec.Struct):
    """Struct whose file field keeps Annotated metadata visible to msgspec."""

    name: str
    avatar: Annotated[UploadFile, File(max_size=1_000_000, allowed_types=["image/*"])]


class ProfileFormAnnotatedOptional(msgspec.Struct):
    name: str
    avatar: Annotated[UploadFile | None, File(max_size=1_000_000)] = None


class DocumentFormAnnotated(msgspec.Struct):
    title: str
    documents: Annotated[list[UploadFile], File(max_files=5)]


class AdultRegistrationForm(msgspec.Struct):
    """Struct whose scalar field has a msgspec.Meta constraint."""

    username: str
    age: Annotated[int, msgspec.Meta(ge=18)]
    resume: UploadFile


class Shade(Enum):
    RED = "red"


class TypedFileForm(msgspec.Struct):
    """Struct with a file and a field of each type that a form text value becomes."""

    doc: UploadFile
    count: int
    ratio: float
    flag: bool
    token: uuid.UUID
    price: Decimal
    day: date
    shade: Shade
    level: Literal[1, 2]
    ids: list[int]


class RequiredNameForm(msgspec.Struct):
    """Struct with a file and a required text field."""

    name: str
    doc: UploadFile


class PdfTypesByIndex:
    """Iterable through __getitem__ only, with no __iter__."""

    def __getitem__(self, index):
        return ["application/pdf"][index]


class RenamedAvatarForm(msgspec.Struct):
    """Struct whose optional file field has another name on the wire."""

    avatar: Annotated[UploadFile, File(min_size=5)] | None = msgspec.field(name="picture", default=None)


class LimitedFilesForm(msgspec.Struct):
    """Struct whose file fields have small size and count limits."""

    avatar: Annotated[UploadFile, File(max_size=10)]
    docs: Annotated[list[UploadFile], File(max_files=2)]


class PdfDocForm(msgspec.Struct):
    """Struct whose file field accepts only PDF files."""

    doc: Annotated[UploadFile, File(allowed_types=["application/pdf"])]


class TestUploadFileClass:
    """Test basic UploadFile class functionality."""

    @pytest.fixture
    def make_upload(self):
        """Build UploadFile objects and close each one after the test."""
        uploads = []

        def make(**kwargs):
            upload = UploadFileClass(**kwargs)
            uploads.append(upload)
            return upload

        yield make
        for upload in uploads:
            upload.close_sync()

    def test_create_upload_file(self, make_upload):
        """Test creating an UploadFile instance."""
        upload = make_upload(
            filename="test.txt",
            content_type="text/plain",
            size=12,
            file_data=b"Hello World!",
        )

        assert upload.filename == "test.txt"
        assert upload.content_type == "text/plain"
        assert upload.size == 12
        assert upload.headers == {}

    def test_from_file_info(self):
        """Test creating UploadFile from file_info dict."""
        file_info = {
            "filename": "image.png",
            "content": b"fake image data",
            "content_type": "image/png",
            "size": 15,
        }

        upload = UploadFileClass.from_file_info(file_info)

        assert upload.filename == "image.png"
        assert upload.content_type == "image/png"
        assert upload.size == 15

    def test_sync_read(self, make_upload):
        """Test sync read via .file property."""
        upload = make_upload(
            filename="test.txt",
            content_type="text/plain",
            size=5,
            file_data=b"Hello",
        )

        content = upload.file.read()
        assert content == b"Hello"

        # Read again after seek
        upload.file.seek(0)
        content = upload.file.read(3)
        assert content == b"Hel"

    def test_async_read(self, make_upload):
        """Test async read method."""

        async def do_test():
            upload = make_upload(
                filename="test.txt",
                content_type="text/plain",
                size=5,
                file_data=b"Hello",
            )

            content = await upload.read()
            assert content == b"Hello"

            await upload.seek(0)
            content = await upload.read(3)
            assert content == b"Hel"

        asyncio.run(do_test())

    def test_async_close(self, make_upload):
        """Test async close method."""

        async def do_test():
            upload = make_upload(
                filename="test.txt",
                content_type="text/plain",
                size=5,
                file_data=b"Hello",
            )

            await upload.close()
            assert upload.file.closed

        asyncio.run(do_test())

    def test_repr(self, make_upload):
        """Test string representation."""
        upload = make_upload(
            filename="test.txt",
            content_type="text/plain",
            size=100,
            file_data=b"x" * 100,
        )

        repr_str = repr(upload)
        assert "test.txt" in repr_str
        assert "text/plain" in repr_str
        assert "100" in repr_str

    def test_file_is_django_file(self, make_upload):
        """Test that .file returns a Django File object."""
        upload = make_upload(
            filename="document.pdf",
            content_type="application/pdf",
            size=15,
            file_data=b"fake pdf content",
        )

        django_file = upload.file

        # Check it's a Django File
        assert isinstance(django_file, DjangoFile)
        assert django_file.name == "document.pdf"
        assert django_file.read() == b"fake pdf content"


class TestUploadFileFileHandling:
    """Test the file position, file mode and close behavior of UploadFile."""

    CONTENT = b"hello world"

    @pytest.fixture
    def uploads(self):
        """Collect UploadFile objects and close each one after the test."""
        created = []
        yield created
        for upload in created:
            upload.close_sync()

    @pytest.fixture
    def disk_upload(self, tmp_path, uploads):
        """Build an UploadFile that Rust spooled to disk."""
        path = tmp_path / "spooled.bin"
        path.write_bytes(self.CONTENT)
        upload = UploadFileClass.from_file_info(
            {
                "filename": "spooled.bin",
                "content_type": "application/x-test",
                "size": len(self.CONTENT),
                "content": None,
                "temp_path": str(path),
            }
        )
        uploads.append(upload)
        return upload

    @pytest.fixture
    def memory_upload(self, uploads):
        """Build an UploadFile that stays in memory."""
        upload = UploadFileClass.from_file_info(
            {"filename": "memory.bin", "content_type": "text/plain", "size": len(self.CONTENT), "content": self.CONTENT}
        )
        uploads.append(upload)
        return upload

    @pytest.fixture
    def rolled_upload(self, uploads):
        """Build an UploadFile that Python rolled over to disk."""
        upload = UploadFileClass("rolled.bin", file_data=self.CONTENT, max_spool_size=4)
        uploads.append(upload)
        return upload

    @pytest.fixture(params=["disk_upload", "memory_upload", "rolled_upload"])
    def any_upload(self, request):
        """Run a test once for each kind of UploadFile."""
        return request.getfixturevalue(request.param)

    def test_init_defaults(self, uploads):
        """An UploadFile with only a name has the default type, size and headers."""
        upload = UploadFileClass("empty.bin")
        uploads.append(upload)

        assert upload.content_type == "application/octet-stream"
        assert upload.size == 0
        assert upload.headers == {}
        assert upload.rolled_to_disk is False
        assert upload.file.read() == b""

    def test_init_rolls_to_disk_above_max_spool_size(self, uploads):
        """The max_spool_size argument sets when the content goes to disk."""
        small = UploadFileClass("a.bin", file_data=b"x" * 10, max_spool_size=100)
        large = UploadFileClass("b.bin", file_data=b"x" * 10, max_spool_size=4)
        uploads.extend([small, large])

        assert small.rolled_to_disk is False
        assert large.rolled_to_disk is True
        assert large.file.read() == b"x" * 10

    def test_from_file_info_in_memory_copies_every_field(self, uploads):
        """A memory-backed file_info gives an UploadFile with all its fields."""
        upload = UploadFileClass.from_file_info(
            {
                "filename": "image.png",
                "content_type": "image/png",
                "size": 3,
                "headers": {"x-part": "1"},
                "content": b"abc",
            }
        )
        uploads.append(upload)

        assert upload.filename == "image.png"
        assert upload.content_type == "image/png"
        assert upload.size == 3
        assert upload.headers == {"x-part": "1"}
        assert upload.rolled_to_disk is False
        assert isinstance(upload.file, DjangoFile)
        assert upload.file.read() == b"abc"

    def test_from_file_info_in_memory_uses_defaults(self, uploads):
        """A memory-backed file_info with no fields gives the default values."""
        upload = UploadFileClass.from_file_info({})
        uploads.append(upload)

        assert upload.filename == ""
        assert upload.content_type == "application/octet-stream"
        assert upload.size == 0
        assert upload.headers == {}
        assert upload.file.read() == b""

    def test_from_file_info_on_disk_copies_every_field(self, tmp_path, uploads):
        """A disk-backed file_info gives an UploadFile with all its fields."""
        path = tmp_path / "spooled.bin"
        path.write_bytes(b"abc")
        upload = UploadFileClass.from_file_info(
            {
                "filename": "big.bin",
                "content_type": "application/x-big",
                "size": 3,
                "headers": {"x-part": "2"},
                "temp_path": str(path),
            }
        )
        uploads.append(upload)

        assert upload.filename == "big.bin"
        assert upload.content_type == "application/x-big"
        assert upload.size == 3
        assert upload.headers == {"x-part": "2"}
        assert upload.rolled_to_disk is True
        assert isinstance(upload.file, DjangoFile)
        assert upload.file.read() == b"abc"

    def test_from_file_info_on_disk_uses_defaults(self, tmp_path, uploads):
        """A disk-backed file_info with only a path gives the default values."""
        path = tmp_path / "spooled.bin"
        path.write_bytes(b"")
        upload = UploadFileClass.from_file_info({"temp_path": str(path)})
        uploads.append(upload)

        assert upload.filename == ""
        assert upload.content_type == "application/octet-stream"
        assert upload.size == 0
        assert upload.headers == {}

    def test_disk_file_opens_in_binary_read_write_mode(self, disk_upload):
        """The lazy open gives a binary file that accepts a write."""
        raw = disk_upload.raw_file

        assert raw.mode == "rb+"
        assert raw.read(5) == b"hello"
        assert raw.write(b"_WORLD") == 6
        raw.seek(0)
        assert raw.read() == b"hello_WORLD"

    @pytest.mark.asyncio
    async def test_read_returns_only_the_asked_size(self, any_upload):
        """A read with a size returns that many bytes, then the rest."""
        assert await any_upload.read(5) == b"hello"
        assert await any_upload.read() == b" world"
        assert await any_upload.read() == b""

    @pytest.mark.asyncio
    async def test_seek_returns_the_new_position(self, any_upload):
        """Seek honors both the offset and the reference point."""
        assert await any_upload.seek(3) == 3
        assert await any_upload.read(2) == b"lo"
        assert await any_upload.seek(2, 1) == 7
        assert await any_upload.read() == b"orld"
        assert await any_upload.seek(-5, 2) == 6
        assert await any_upload.read() == b"world"

    @pytest.mark.asyncio
    async def test_write_returns_the_count_and_changes_the_content(self, any_upload):
        """A write puts the bytes at the position and returns their count."""
        assert await any_upload.write(b"HELLO") == 5
        await any_upload.seek(0)
        assert await any_upload.read() == b"HELLO world"

    @pytest.mark.asyncio
    async def test_write_on_disk_reaches_the_file(self, disk_upload):
        """A write to a disk-spooled file lands in the file on disk."""
        await disk_upload.write(b"HELLO")
        await disk_upload.close()

        with open(disk_upload._temp_path, "rb") as handle:
            assert handle.read() == b"HELLO world"

    @pytest.mark.asyncio
    async def test_close_closes_the_file(self, any_upload):
        """The async close closes the file, and a second close is safe."""
        raw = any_upload.raw_file
        assert raw.closed is False

        await any_upload.close()
        assert raw.closed is True

        await any_upload.close()
        assert raw.closed is True

    @pytest.mark.asyncio
    async def test_close_of_a_never_opened_file_opens_nothing(self, disk_upload):
        """Closing a disk-spooled file that nobody read does not open it."""
        await disk_upload.close()

        assert disk_upload._file is None

    def test_close_sync_closes_the_file(self, any_upload):
        """The sync close closes the file, and a second close is safe."""
        raw = any_upload.raw_file

        any_upload.close_sync()
        assert raw.closed is True

        any_upload.close_sync()
        assert raw.closed is True

    def test_close_sync_of_a_never_opened_file_opens_nothing(self, disk_upload):
        """Closing a disk-spooled file that nobody read does not fail or open it."""
        disk_upload.close_sync()

        assert disk_upload._file is None

    def test_repr_of_a_memory_file(self, memory_upload):
        """The repr shows the name, type and size only."""
        assert repr(memory_upload) == "UploadFile(filename='memory.bin', content_type='text/plain', size=11)"

    def test_repr_of_a_rolled_file(self, rolled_upload):
        """The repr says that the file rolled to disk."""
        assert repr(rolled_upload) == (
            "UploadFile(filename='rolled.bin', content_type='application/octet-stream', size=0 (rolled to disk))"
        )

    def test_repr_of_a_disk_spooled_file(self, disk_upload):
        """The repr says that the file is on disk, and says when it is open."""
        expected = "UploadFile(filename='spooled.bin', content_type='application/x-test', size=11, disk-spooled"
        assert repr(disk_upload) == expected + ")"

        disk_upload.raw_file  # noqa: B018 - the access opens the file
        assert repr(disk_upload) == expected + ", opened)"


class TestUploadFileValidation:
    """Test file upload validation with File() constraints."""

    @pytest.mark.parametrize(
        "allowed_types", ["image/*", b"image/*", ["image/*", 3], 3], ids=["str", "bytes", "int-item", "int"]
    )
    def test_allowed_types_that_is_not_a_list_of_str_fails_loudly(self, allowed_types):
        """A str is a sequence of characters. "image/*" gave "*", and "*" allowed each type."""
        with pytest.raises(TypeError, match="takes an iterable of MIME type strings"):
            File(allowed_types=allowed_types)

    def test_allowed_types_from_a_generator_checks_each_type(self):
        """A generator of types is read one time. Each of its types applies."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File(allowed_types=(t for t in ["application/pdf"]))]):
            return {"content_type": file.content_type}

        client = TestClient(api)

        response = client.post("/upload", files={"file": ("page.html", b"<html></html>", "text/html")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_invalid_content_type"

        response = client.post("/upload", files={"file": ("doc.pdf", b"%PDF-1.7", "application/pdf")})
        assert response.status_code == 200
        assert response.json() == {"content_type": "application/pdf"}

    def test_allowed_types_with_only_getitem_checks_each_type(self):
        """iter() reads a class that has only __getitem__. isinstance(..., Iterable) rejects that class."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File(allowed_types=PdfTypesByIndex())]):
            return {"content_type": file.content_type}

        client = TestClient(api)

        response = client.post("/upload", files={"file": ("page.html", b"<html></html>", "text/html")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_invalid_content_type"

        response = client.post("/upload", files={"file": ("doc.pdf", b"%PDF-1.7", "application/pdf")})
        assert response.status_code == 200
        assert response.json() == {"content_type": "application/pdf"}

    @pytest.mark.parametrize("allowed_types", [[], ()], ids=["list", "tuple"])
    def test_empty_allowed_types_fails_loudly(self, allowed_types):
        """An empty list allowed each type. A list with no type must not do that."""
        with pytest.raises(ValueError, match="allows no file type"):
            File(allowed_types=allowed_types)

    @pytest.mark.parametrize("name", ["max_size", "min_size", "max_files"])
    @pytest.mark.parametrize(
        ("value", "shown"),
        [("big", "str 'big'"), ("10MB", "str '10MB'"), (1.5, "float 1.5"), (True, "bool True")],
        ids=["str", "size-str", "float", "bool"],
    )
    def test_file_limit_of_the_wrong_type_fails_loudly(self, name, value, shown):
        """Rust ignored a limit that is not an int. File(max_size="10MB") then set no limit."""
        with pytest.raises(TypeError, match=f"^File {name} must be an int, got {shown}$"):
            File(**{name: value})

    @pytest.mark.parametrize("name", ["max_size", "min_size"])
    def test_file_size_below_zero_fails_loudly(self, name):
        with pytest.raises(ValueError, match=f"^File {name} must be 0 or more, got -1$"):
            File(**{name: -1})

    @pytest.mark.parametrize("name", ["max_size", "min_size", "max_files"])
    def test_file_limit_too_large_for_rust_fails_loudly(self, name):
        """Rust reads a limit as a usize. It ignored a larger int, and the limit was gone."""
        value = sys.maxsize + 1
        with pytest.raises(ValueError, match=f"^File {name} must be {sys.maxsize} or less, got {value}$"):
            File(**{name: value})

    def test_largest_file_limit_is_valid(self):
        assert File(max_size=sys.maxsize).max_size == sys.maxsize

    @pytest.mark.parametrize("value", [0, -1])
    def test_max_files_below_one_fails_loudly(self, value):
        """A route with max_files=0 takes no file. That is a mistake, as an empty allowed_types is."""
        with pytest.raises(ValueError, match=f"^File max_files must be 1 or more, got {value}$"):
            File(max_files=value)

    def test_min_size_above_max_size_fails_loudly(self):
        """No file can be larger than max_size and smaller than min_size."""
        with pytest.raises(ValueError, match=r"^File min_size 20 must not be above max_size 10$"):
            File(min_size=20, max_size=10)

    def test_file_limits_that_are_valid_still_work(self):
        File()
        File(max_size=None, min_size=None, max_files=None)
        File(max_size=0, min_size=0, max_files=1)
        File(min_size=10, max_size=10)
        marker = File(max_size=FileSize.MB_1, min_size=1, max_files=3)
        assert (marker.max_size, marker.min_size, marker.max_files) == (FileSize.MB_1, 1, 3)

    def test_file_limits_of_a_valid_marker_apply_end_to_end(self):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(files: Annotated[list[UploadFile], File(max_size=8, min_size=2, max_files=2)]):
            return {"count": len(files)}

        client = TestClient(api)

        response = client.post("/upload", files={"files": ("a.txt", b"12345678", "text/plain")})
        assert response.status_code == 200
        assert response.json() == {"count": 1}

        response = client.post("/upload", files={"files": ("a.txt", b"123456789", "text/plain")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_large"

        response = client.post("/upload", files={"files": ("a.txt", b"1", "text/plain")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_small"

        response = client.post("/upload", files=[("files", (f"{i}.txt", b"abc", "text/plain")) for i in range(3)])
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_many"

    def test_max_size_validation_pass(self):
        """Test that file within max size passes."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File(max_size=1000)]):
            return {"filename": avatar.filename, "size": avatar.size}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("small.txt", b"x" * 100, "text/plain")},
        )

        assert response.status_code == 200
        assert response.json()["size"] == 100

    def test_max_size_validation_fail(self):
        """Test that file exceeding max size fails with 422."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File(max_size=100)]):
            return {"filename": avatar.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("large.txt", b"x" * 200, "text/plain")},
        )

        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_too_large"
        assert errors[0]["loc"] == ["body", "avatar"]
        assert "maximum size" in errors[0]["msg"]
        assert errors[0]["ctx"]["max_size"] == 100
        assert errors[0]["ctx"]["actual_size"] == 200

    def test_min_size_validation_pass(self):
        """Test that file meeting min size passes."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File(min_size=50)]):
            return {"filename": avatar.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("file.txt", b"x" * 100, "text/plain")},
        )

        assert response.status_code == 200

    def test_min_size_validation_fail(self):
        """Test that file below min size fails with 422."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File(min_size=100)]):
            return {"filename": avatar.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("small.txt", b"x" * 50, "text/plain")},
        )

        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_too_small"
        assert errors[0]["loc"] == ["body", "avatar"]
        assert "minimum size" in errors[0]["msg"]
        assert errors[0]["ctx"]["min_size"] == 100
        assert errors[0]["ctx"]["actual_size"] == 50

    def test_allowed_types_exact_match(self):
        """Test allowed_types with exact content type match."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(doc: Annotated[UploadFile, File(allowed_types=["application/pdf"])]):
            return {"content_type": doc.content_type}

        client = TestClient(api)

        # Valid type
        response = client.post(
            "/upload",
            files={"doc": ("doc.pdf", b"pdf content", "application/pdf")},
        )
        assert response.status_code == 200
        assert response.json()["content_type"] == "application/pdf"

        # Invalid type
        response = client.post(
            "/upload",
            files={"doc": ("doc.txt", b"text content", "text/plain")},
        )
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_invalid_content_type"
        assert errors[0]["loc"] == ["body", "doc"]
        assert "text/plain" in errors[0]["msg"]
        assert errors[0]["ctx"]["allowed_types"] == ["application/pdf"]
        assert errors[0]["ctx"]["actual_type"] == "text/plain"

    def test_allowed_types_wildcard(self):
        """Test allowed_types with wildcard patterns like 'image/*'."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File(allowed_types=["image/*"])]):
            return {"content_type": avatar.content_type}

        client = TestClient(api)

        # Valid image types
        for content_type in ["image/png", "image/jpeg", "image/gif", "image/webp"]:
            response = client.post(
                "/upload",
                files={"avatar": ("img.png", b"image data", content_type)},
            )
            assert response.status_code == 200, f"Failed for {content_type}"

        # Invalid type
        response = client.post(
            "/upload",
            files={"avatar": ("doc.pdf", b"pdf content", "application/pdf")},
        )
        assert response.status_code == 422

    def test_max_files_validation(self):
        """Test max_files constraint for list[UploadFile]."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(docs: Annotated[list[UploadFile], File(max_files=2)]):
            return {"count": len(docs)}

        client = TestClient(api)

        # Within limit
        response = client.post(
            "/upload",
            files=[
                ("docs", ("doc1.txt", b"content1", "text/plain")),
                ("docs", ("doc2.txt", b"content2", "text/plain")),
            ],
        )
        assert response.status_code == 200
        assert response.json()["count"] == 2

        # Exceeds limit
        response = client.post(
            "/upload",
            files=[
                ("docs", ("doc1.txt", b"content1", "text/plain")),
                ("docs", ("doc2.txt", b"content2", "text/plain")),
                ("docs", ("doc3.txt", b"content3", "text/plain")),
            ],
        )
        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_too_many"
        assert errors[0]["loc"] == ["body", "docs"]
        assert errors[0]["ctx"]["max_files"] == 2
        assert errors[0]["ctx"]["actual_count"] == 3

    def test_missing_required_file(self):
        """Test that missing required file returns 422."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File()]):
            return {"filename": avatar.filename}

        client = TestClient(api)
        response = client.post("/upload", data={"name": "test"})

        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_missing"
        assert errors[0]["loc"] == ["body", "avatar"]
        assert errors[0]["msg"] == "Missing required file"
        assert errors[0]["input"] is None

    def test_file_parameter_without_default_that_allows_none_is_none_when_missing(self):
        """`UploadFile | None` with no default is optional and gives None."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile | None, File()]):
            return {"avatar": None if avatar is None else avatar.filename}

        client = TestClient(api)
        assert client.post("/upload", data={"name": "x"}).json() == {"avatar": None}
        response = client.post("/upload", files={"avatar": ("a.png", b"x", "image/png")})
        assert response.json() == {"avatar": "a.png"}

    def test_missing_optional_file_takes_the_default_of_the_parameter(self):
        """The default of the parameter is the value of a missing file, also when it is not None."""
        api = BoltAPI()
        fallback = {"filename": "fallback.txt"}

        @api.post("/upload")
        async def upload(doc: Annotated[dict | None, File()] = fallback):
            return {"filename": doc["filename"]}

        client = TestClient(api)
        assert client.post("/upload", data={"name": "x"}).json() == {"filename": "fallback.txt"}
        response = client.post("/upload", files={"doc": ("real.txt", b"x", "text/plain")})
        assert response.json() == {"filename": "real.txt"}

    def test_optional_list_of_dicts_wraps_one_file_in_a_list(self):
        """A `list[dict]` annotation gives a list for one file and for several files."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(files: Annotated[list[dict], File()] = None):
            return {"names": [f["filename"] for f in files]}

        client = TestClient(api)
        one = client.post("/upload", files={"files": ("a.txt", b"a", "text/plain")})
        assert one.json() == {"names": ["a.txt"]}
        two = client.post(
            "/upload",
            files=[("files", ("a.txt", b"a", "text/plain")), ("files", ("b.txt", b"b", "text/plain"))],
        )
        assert two.json() == {"names": ["a.txt", "b.txt"]}

    @pytest.mark.parametrize("required", [True, False], ids=["required", "optional"])
    def test_single_file_parameter_takes_the_first_of_several_files(self, required):
        """A parameter that is not a list gets the first file when the client sends more."""
        api = BoltAPI()

        if required:

            @api.post("/upload")
            async def upload(doc: Annotated[UploadFile, File()]):
                return {"filename": doc.filename}

        else:

            @api.post("/upload")
            async def upload(doc: Annotated[UploadFile | None, File()] = None):
                return {"filename": doc.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files=[("doc", ("a.txt", b"a", "text/plain")), ("doc", ("b.txt", b"b", "text/plain"))],
        )
        assert response.json() == {"filename": "a.txt"}

    def test_optional_file(self):
        """Test optional file upload."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile | None, File()] = None):
            if avatar is None:
                return {"has_file": False}
            return {"has_file": True, "filename": avatar.filename}

        client = TestClient(api)

        # Without file
        response = client.post("/upload", data={"name": "test"})
        assert response.status_code == 200
        assert response.json()["has_file"] is False

        # With file
        response = client.post(
            "/upload",
            files={"avatar": ("photo.jpg", b"image", "image/jpeg")},
        )
        assert response.status_code == 200
        assert response.json()["has_file"] is True

    def test_optional_file_on_a_route_that_awaits(self):
        """The async dispatch has no upload file to close when the client sends none."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile | None, File()] = None):
            await asyncio.sleep(0)
            return {"has_file": avatar is not None}

        with TestClient(api) as client:
            response = client.post("/upload", data={"name": "test"})

        assert response.status_code == 200, response.text
        assert response.json() == {"has_file": False}

    def test_a_part_header_over_64_kb_gets_422(self):
        """The multipart parser keeps at most 64 KB that it cannot use yet, for example a part header."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File()]):
            return {"size": file.size}

        def body(pad: int) -> bytes:
            return (
                b"--b\r\n"
                b'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n'
                b"X-Pad: " + b"a" * pad + b"\r\n"
                b"Content-Type: application/octet-stream\r\n\r\n"
                b"hello\r\n--b--\r\n"
            )

        client = TestClient(api)
        headers = {"content-type": "multipart/form-data; boundary=b"}

        response = client.post("/upload", content=body(1_000), headers=headers)
        assert response.status_code == 200
        assert response.json() == {"size": 5}

        response = client.post("/upload", content=body(70_000), headers=headers)
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "multipart_error"


class TestFileConstraintsByPartName:
    """Rust checks a file against the File() constraints of its multipart part name."""

    def test_aliased_file_parameter_checks_its_constraints(self):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(
            doc: Annotated[
                UploadFile, File(alias="document", min_size=10, allowed_types=["application/pdf"], max_files=1)
            ],
        ):
            return {"content_type": doc.content_type}

        client = TestClient(api)

        response = client.post("/upload", files={"document": ("page.html", b"<html>body</html>", "text/html")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_invalid_content_type"
        assert response.json()["detail"][0]["loc"] == ["body", "document"]

        response = client.post("/upload", files={"document": ("small.pdf", b"%PDF", "application/pdf")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_small"

        response = client.post(
            "/upload",
            files=[
                ("document", ("a.pdf", b"%PDF-1.7 one", "application/pdf")),
                ("document", ("b.pdf", b"%PDF-1.7 two", "application/pdf")),
            ],
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_many"

        response = client.post("/upload", files={"document": ("doc.pdf", b"%PDF-1.7 body", "application/pdf")})
        assert response.status_code == 200
        assert response.json() == {"content_type": "application/pdf"}

    def test_form_struct_file_field_checks_its_constraints(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            return {"name": data.name, "content_type": data.avatar.content_type}

        client = TestClient(api)

        response = client.post(
            "/profile",
            data={"name": "Jane"},
            files={"avatar": ("page.html", b"<script>alert(1)</script>", "text/html")},
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_invalid_content_type"
        assert response.json()["detail"][0]["loc"] == ["body", "avatar"]

        response = client.post(
            "/profile",
            data={"name": "Jane"},
            files={"avatar": ("photo.png", b"\x89PNG", "image/png")},
        )
        assert response.status_code == 200
        assert response.json() == {"name": "Jane", "content_type": "image/png"}

    def test_form_struct_file_field_uses_its_encoded_name(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[RenamedAvatarForm, Form()]):
            return {"size": data.avatar.size if data.avatar else None}

        client = TestClient(api)

        response = client.post("/profile", files={"picture": ("photo.png", b"xyz", "image/png")})
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_small"
        assert response.json()["detail"][0]["loc"] == ["body", "picture"]

        response = client.post("/profile", files={"picture": ("photo.png", b"x" * 5, "image/png")})
        assert response.status_code == 200
        assert response.json() == {"size": 5}

    def test_form_struct_file_fields_check_max_size_and_max_files(self):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(data: Annotated[LimitedFilesForm, Form()]):
            return {"docs": len(data.docs)}

        client = TestClient(api)
        small_docs = [
            ("docs", ("a.txt", b"a", "text/plain")),
            ("docs", ("b.txt", b"b", "text/plain")),
        ]

        response = client.post("/upload", files=[("avatar", ("big.png", b"x" * 11, "image/png")), *small_docs])
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_large"
        assert response.json()["detail"][0]["loc"] == ["body", "avatar"]

        response = client.post(
            "/upload",
            files=[
                ("avatar", ("ok.png", b"x", "image/png")),
                *small_docs,
                ("docs", ("c.txt", b"c", "text/plain")),
            ],
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "file_too_many"
        assert response.json()["detail"][0]["loc"] == ["body", "docs"]

        response = client.post("/upload", files=[("avatar", ("ok.png", b"x", "image/png")), *small_docs])
        assert response.status_code == 200
        assert response.json() == {"docs": 2}

    def test_form_struct_max_size_does_not_limit_a_sibling_file_field(self):
        """The max_size of avatar applies to avatar only. docs has no max_size, so it gets the global limit."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(data: Annotated[LimitedFilesForm, Form()]):
            return {"sizes": [doc.size for doc in data.docs]}

        client = TestClient(api)

        response = client.post(
            "/upload",
            files=[
                ("avatar", ("ok.png", b"x", "image/png")),
                ("docs", ("a.txt", b"a" * 1000, "text/plain")),
                ("docs", ("b.txt", b"b" * 20, "text/plain")),
            ],
        )
        assert response.status_code == 200
        assert response.json() == {"sizes": [1000, 20]}

    def test_two_declarations_of_one_part_with_different_constraints_fail(self):
        api = BoltAPI()

        with pytest.raises(ImproperlyConfigured, match="'doc' has two File\\(\\) declarations"):

            @api.post("/upload")
            async def upload(
                data: Annotated[PdfDocForm, Form()],
                other: Annotated[UploadFile, File(alias="doc", max_size=1000)],
            ):
                return {}


class TestUploadFileMultiple:
    """Test multiple file uploads."""

    def test_multiple_files_list(self):
        """Test uploading multiple files as list[UploadFile]."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(docs: Annotated[list[UploadFile], File()]):
            return {
                "count": len(docs),
                "filenames": [d.filename for d in docs],
            }

        client = TestClient(api)
        response = client.post(
            "/upload",
            files=[
                ("docs", ("doc1.pdf", b"content1", "application/pdf")),
                ("docs", ("doc2.pdf", b"content2", "application/pdf")),
                ("docs", ("doc3.pdf", b"content3", "application/pdf")),
            ],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3
        assert set(data["filenames"]) == {"doc1.pdf", "doc2.pdf", "doc3.pdf"}

    def test_single_file_as_list(self):
        """Test that single file is wrapped in list for list[UploadFile]."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(docs: Annotated[list[UploadFile], File()]):
            return {"count": len(docs)}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"docs": ("single.pdf", b"content", "application/pdf")},
        )

        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_optional_list_gets_every_file(self):
        """An optional list[UploadFile] gets all files, one file as a list, or the default."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(docs: Annotated[list[UploadFile] | None, File()] = None):
            if docs is None:
                return {"docs": None}
            return {"docs": [d.filename for d in docs]}

        client = TestClient(api)
        several = client.post(
            "/upload",
            files=[
                ("docs", ("doc1.pdf", b"content1", "application/pdf")),
                ("docs", ("doc2.pdf", b"content2", "application/pdf")),
            ],
        )
        one = client.post("/upload", files={"docs": ("single.pdf", b"content", "application/pdf")})
        none = client.post("/upload", files={"other": ("other.pdf", b"content", "application/pdf")})

        assert several.status_code == 200
        assert sorted(several.json()["docs"]) == ["doc1.pdf", "doc2.pdf"]
        assert one.status_code == 200
        assert one.json()["docs"] == ["single.pdf"]
        assert none.status_code == 200
        assert none.json()["docs"] is None


class TestFormStructWithUploadFile:
    """Test Form() struct with UploadFile fields."""

    def test_struct_with_single_file(self):
        """Test Form struct with a single UploadFile field."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            content = await data.avatar.read()
            return {
                "name": data.name,
                "avatar_filename": data.avatar.filename,
                "avatar_size": len(content),
            }

        client = TestClient(api)
        response = client.post(
            "/profile",
            data={"name": "John Doe"},
            files={"avatar": ("photo.jpg", b"image data here", "image/jpeg")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "John Doe"
        assert data["avatar_filename"] == "photo.jpg"
        assert data["avatar_size"] == 15

    def test_struct_with_multiple_files(self):
        """Test Form struct with list[UploadFile] field."""
        api = BoltAPI()

        @api.post("/documents")
        async def upload_documents(data: Annotated[DocumentForm, Form()]):
            return {
                "title": data.title,
                "count": len(data.documents),
                "filenames": [d.filename for d in data.documents],
            }

        client = TestClient(api)
        response = client.post(
            "/documents",
            data={"title": "My Documents"},
            files=[
                ("documents", ("doc1.pdf", b"pdf1", "application/pdf")),
                ("documents", ("doc2.pdf", b"pdf2", "application/pdf")),
            ],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "My Documents"
        assert data["count"] == 2
        assert set(data["filenames"]) == {"doc1.pdf", "doc2.pdf"}

    def test_struct_mixed_form_and_files(self):
        """Test Form struct with both regular fields and UploadFile fields."""
        api = BoltAPI()

        @api.post("/register")
        async def register(data: Annotated[RegistrationForm, Form()]):
            return {
                "username": data.username,
                "email": data.email,
                "age": data.age,
                "photo_name": data.profile_photo.filename,
                "resume_name": data.resume.filename,
            }

        client = TestClient(api)
        response = client.post(
            "/register",
            data={
                "username": "johndoe",
                "email": "john@example.com",
                "age": "30",
            },
            files=[
                ("profile_photo", ("photo.jpg", b"image", "image/jpeg")),
                ("resume", ("resume.pdf", b"pdf", "application/pdf")),
            ],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "johndoe"
        assert data["email"] == "john@example.com"
        assert data["age"] == 30
        assert data["photo_name"] == "photo.jpg"
        assert data["resume_name"] == "resume.pdf"

    def test_struct_optional_file(self):
        """Test Form struct with optional UploadFile field."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormOptional, Form()]):
            return {
                "name": data.name,
                "has_avatar": data.avatar is not None,
            }

        client = TestClient(api)

        # Without avatar
        response = client.post("/profile", data={"name": "John"})
        assert response.status_code == 200
        assert response.json()["has_avatar"] is False

        # With avatar
        response = client.post(
            "/profile",
            data={"name": "John"},
            files={"avatar": ("photo.jpg", b"image", "image/jpeg")},
        )
        assert response.status_code == 200
        assert response.json()["has_avatar"] is True

    def test_struct_missing_required_file(self):
        """Test that missing required file in struct returns 422."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            return {"name": data.name}

        client = TestClient(api)
        response = client.post("/profile", data={"name": "John"})

        assert response.status_code == 422
        errors = response.json()["detail"]
        assert len(errors) == 1
        assert errors[0]["type"] == "file_missing"
        assert errors[0]["loc"] == ["body", "avatar"]
        assert errors[0]["msg"] == "Missing required file"

    def test_struct_with_bad_field_reports_only_that_field(self):
        """A valid file does not add an error next to the error of a bad field."""
        api = BoltAPI()

        @api.post("/register")
        async def register(data: Annotated[AdultRegistrationForm, Form()]):
            return {"username": data.username}

        client = TestClient(api)
        response = client.post(
            "/register",
            data={"username": "john", "age": "12"},
            files={"resume": ("cv.pdf", b"pdf", "application/pdf")},
        )

        assert response.status_code == 422, response.text
        errors = response.json()["detail"]
        assert [err["loc"] for err in errors] == [["body", "age"]]

    def test_struct_with_a_file_converts_each_text_field_to_its_type(self):
        """A text field of a struct with a file becomes its type, as in a struct without a file."""
        api = BoltAPI()

        @api.post("/typed")
        async def typed(data: Annotated[TypedFileForm, Form()]):
            return {
                "values": [
                    data.count,
                    data.ratio,
                    data.flag,
                    str(data.token),
                    str(data.price),
                    data.day.isoformat(),
                    data.shade.value,
                    data.level,
                    data.ids,
                ],
                "types": [type(getattr(data, name)).__name__ for name in TypedFileForm.__struct_fields__],
            }

        token = uuid.uuid4()
        client = TestClient(api)
        response = client.post(
            "/typed",
            data={
                "count": "3",
                "ratio": "1.5",
                "flag": "true",
                "token": str(token),
                "price": "1.25",
                "day": "2024-02-03",
                "shade": "red",
                "level": "2",
                "ids": ["1", "2"],
            },
            files={"doc": ("a.txt", b"x", "text/plain")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["values"] == [3, 1.5, True, str(token), "1.25", "2024-02-03", "red", 2, [1, 2]]
        assert body["types"] == [
            "UploadFile",
            "int",
            "float",
            "bool",
            "UUID",
            "Decimal",
            "date",
            "Shade",
            "int",
            "list",
        ]

    def test_struct_with_a_file_reports_a_missing_text_field_by_name(self):
        """The message and the place of a missing text field name the form field."""
        api = BoltAPI()

        @api.post("/named")
        async def named(data: Annotated[RequiredNameForm, Form()]):
            return {"name": data.name}

        client = TestClient(api)
        response = client.post("/named", data={}, files={"doc": ("a.txt", b"x", "text/plain")})

        assert response.status_code == 422, response.text
        errors = response.json()["detail"]
        assert [(err["loc"], err["msg"]) for err in errors] == [(["body", "name"], "Missing required form field: name")]

    def test_struct_with_annotated_upload_file(self):
        """Annotated[UploadFile, File(...)] inside a struct must be parsed as a file."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            content = await data.avatar.read()
            return {
                "name": data.name,
                "avatar_filename": data.avatar.filename,
                "avatar_size": len(content),
                "is_upload": isinstance(data.avatar, UploadFileClass),
            }

        client = TestClient(api)
        response = client.post(
            "/profile",
            data={"name": "Jane"},
            files={"avatar": ("photo.png", b"image bytes", "image/png")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Jane"
        assert body["avatar_filename"] == "photo.png"
        assert body["avatar_size"] == len(b"image bytes")
        assert body["is_upload"] is True

    def test_struct_with_annotated_optional_upload_file(self):
        """Annotated[UploadFile | None, File(...)] must still deserialize when provided."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotatedOptional, Form()]):
            return {
                "name": data.name,
                "has_avatar": data.avatar is not None,
                "avatar_filename": data.avatar.filename if data.avatar else None,
            }

        client = TestClient(api)
        response = client.post(
            "/profile",
            data={"name": "Jane"},
            files={"avatar": ("photo.png", b"x", "image/png")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["has_avatar"] is True
        assert body["avatar_filename"] == "photo.png"

    def test_struct_with_annotated_list_upload_file(self):
        """Annotated[list[UploadFile], File(...)] inside a struct must collect all files."""
        api = BoltAPI()

        @api.post("/documents")
        async def upload_documents(data: Annotated[DocumentFormAnnotated, Form()]):
            return {
                "title": data.title,
                "count": len(data.documents),
                "filenames": sorted(d.filename for d in data.documents),
            }

        client = TestClient(api)
        response = client.post(
            "/documents",
            data={"title": "Docs"},
            files=[
                ("documents", ("a.pdf", b"a", "application/pdf")),
                ("documents", ("b.pdf", b"b", "application/pdf")),
            ],
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 2
        assert body["filenames"] == ["a.pdf", "b.pdf"]

    def test_struct_file_field_sent_as_text_returns_422(self):
        """A file field sent as a plain text form value must return 422, not crash."""
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            return {"name": data.name}

        client = TestClient(api)
        response = client.post(
            "/profile",
            data={"name": "Jane", "avatar": "not-a-file.pdf"},
        )

        assert response.status_code == 422, response.text
        errors = response.json()["detail"]
        assert any(err["loc"][-1] == "avatar" for err in errors)
        assert [err["msg"] for err in errors if err["loc"][-1] == "avatar"] == ["Expected uploaded file, got str"]


class TestBackwardCompatibility:
    """Test backward compatibility with dict annotation."""

    def test_dict_annotation_still_works(self):
        """Test that dict annotation returns raw file info dict."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(avatar: Annotated[dict, File()]):
            return {
                "filename": avatar["filename"],
                "content_type": avatar["content_type"],
                "size": avatar["size"],
            }

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("test.png", b"image data", "image/png")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.png"
        assert data["content_type"] == "image/png"

    def test_list_dict_annotation_still_works(self):
        """Test that list[dict] annotation returns list of raw file info dicts."""
        api = BoltAPI()

        @api.post("/upload")
        async def upload(files: Annotated[list[dict], File()]):
            return {"count": len(files), "names": [f["filename"] for f in files]}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files=[
                ("files", ("a.txt", b"content a", "text/plain")),
                ("files", ("b.txt", b"content b", "text/plain")),
            ],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert set(data["names"]) == {"a.txt", "b.txt"}


class TestSyncHandler:
    """Test UploadFile with sync handlers."""

    def test_sync_handler_file_access(self):
        """Test accessing file in sync handler via .file property."""
        api = BoltAPI()

        @api.post("/upload")
        def upload(avatar: Annotated[UploadFile, File()]):
            # Use .file for sync access
            content = avatar.file.read()
            avatar.file.close()
            return {"size": len(content), "filename": avatar.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("photo.jpg", b"sync image data", "image/jpeg")},
        )

        assert response.status_code == 200
        assert response.json()["size"] == 15
        assert response.json()["filename"] == "photo.jpg"


class TestDjangoFileFieldIntegration:
    """Test UploadFile integration with Django FileField."""

    def test_save_to_filefield(self, django_db_setup, db, tmp_path, settings):
        """Test saving uploaded file to Django FileField."""
        # Configure media root to temp directory
        settings.MEDIA_ROOT = str(tmp_path)

        api = BoltAPI()

        @api.post("/documents")
        def create_document(
            title: Annotated[str, Form()],
            file: Annotated[UploadFile, File()],
        ):
            doc = Document(title=title)
            # Save using Django File - this is the key test
            doc.file.save(file.filename, file.file, save=True)
            return {
                "id": doc.id,
                "title": doc.title,
                "filename": doc.file.name,
            }

        client = TestClient(api)
        file_content = b"Test document content for FileField"
        response = client.post(
            "/documents",
            data={"title": "Test Document"},
            files={"file": ("test_doc.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Document"
        assert "test_doc" in data["filename"]

        # Verify file was actually saved
        doc = Document.objects.get(id=data["id"])
        assert doc.title == "Test Document"
        assert doc.file.read() == file_content
        doc.file.close()

    def test_save_to_filefield_async(self, django_db_setup, db, tmp_path, settings):
        """Test saving uploaded file to Django FileField in async handler."""
        settings.MEDIA_ROOT = str(tmp_path)

        api = BoltAPI()

        @api.post("/documents")
        async def create_document(
            title: Annotated[str, Form()],
            file: Annotated[UploadFile, File()],
        ):
            doc = Document(title=title)
            # Use Django's native async save - file.save() with save=False, then asave()
            doc.file.save(file.filename, file.file, save=False)
            await doc.asave()
            return {
                "id": doc.id,
                "title": doc.title,
                "filename": doc.file.name,
            }

        client = TestClient(api)
        file_content = b"Async test document content"
        response = client.post(
            "/documents",
            data={"title": "Async Document"},
            files={"file": ("async_doc.pdf", file_content, "application/pdf")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Async Document"

        doc = Document.objects.get(id=data["id"])
        assert doc.file.read() == file_content
        doc.file.close()

    def test_direct_assign_to_filefield(self, django_db_setup, db, tmp_path, settings):
        """Test directly assigning UploadFile.file to a FileField (no .save() on field)."""
        settings.MEDIA_ROOT = str(tmp_path)

        api = BoltAPI()

        @api.post("/documents")
        def create_document(
            title: Annotated[str, Form()],
            file: Annotated[UploadFile, File()],
        ):
            doc = Document(title=title)
            doc.file = file.file
            doc.save()
            return {
                "id": doc.id,
                "title": doc.title,
                "filename": doc.file.name,
            }

        client = TestClient(api)
        file_content = b"Direct assignment test content"
        response = client.post(
            "/documents",
            data={"title": "Direct Assign"},
            files={"file": ("direct.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Direct Assign"
        assert "direct" in data["filename"]

        # Verify file was saved to storage
        doc = Document.objects.get(id=data["id"])
        assert doc.title == "Direct Assign"
        assert doc.file.read() == file_content
        doc.file.close()

    def test_direct_assign_to_filefield_async(self, django_db_setup, db, tmp_path, settings):
        """Test directly assigning UploadFile.file to a FileField in async handler."""
        settings.MEDIA_ROOT = str(tmp_path)

        api = BoltAPI()

        @api.post("/documents")
        async def create_document(
            title: Annotated[str, Form()],
            file: Annotated[UploadFile, File()],
        ):
            doc = Document(title=title)
            doc.file = file.file
            await doc.asave()
            return {
                "id": doc.id,
                "title": doc.title,
                "filename": doc.file.name,
            }

        client = TestClient(api)
        file_content = b"Async direct assignment content"
        response = client.post(
            "/documents",
            data={"title": "Async Direct"},
            files={"file": ("async_direct.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Async Direct"

        doc = Document.objects.get(id=data["id"])
        assert doc.file.read() == file_content
        doc.file.close()


class TestAutoCleanup:
    """Test framework-level auto-cleanup of UploadFile resources."""

    def test_files_closed_after_handler_success(self):
        """Verify files are automatically closed after successful handler execution."""
        api = BoltAPI()
        tracked_files: list[UploadFile] = []

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File()]):
            # Track the file to check its state after handler completes
            tracked_files.append(avatar)
            # Verify file is open during handler
            assert not avatar._file.closed, "File should be open during handler"
            return {"filename": avatar.filename}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("test.jpg", b"image data", "image/jpeg")},
        )

        assert response.status_code == 200
        assert len(tracked_files) == 1
        # File should be closed after handler completes
        assert tracked_files[0]._file.closed, "File should be closed after handler"

    def test_files_closed_after_handler_exception(self):
        """Verify files are closed even when handler raises an exception."""
        api = BoltAPI()
        tracked_files: list[UploadFile] = []

        @api.post("/upload")
        async def upload(avatar: Annotated[UploadFile, File()]):
            tracked_files.append(avatar)
            assert not avatar._file.closed, "File should be open during handler"
            raise ValueError("Intentional error in handler")

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("test.jpg", b"image data", "image/jpeg")},
        )

        # Handler raised an exception - should get 500
        assert response.status_code == 500
        assert len(tracked_files) == 1
        # File should still be closed despite exception
        assert tracked_files[0]._file.closed, "File should be closed even after exception"

    def test_multiple_files_all_closed(self):
        """Verify all files are closed when multiple files are uploaded."""
        api = BoltAPI()
        tracked_files: list[UploadFile] = []

        @api.post("/upload")
        async def upload(docs: Annotated[list[UploadFile], File()]):
            tracked_files.extend(docs)
            for doc in docs:
                assert not doc._file.closed, "Files should be open during handler"
            return {"count": len(docs)}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files=[
                ("docs", ("doc1.pdf", b"content1", "application/pdf")),
                ("docs", ("doc2.pdf", b"content2", "application/pdf")),
                ("docs", ("doc3.pdf", b"content3", "application/pdf")),
            ],
        )

        assert response.status_code == 200
        assert len(tracked_files) == 3
        # All files should be closed
        for i, upload in enumerate(tracked_files):
            assert upload._file.closed, f"File {i} should be closed"

    def test_files_of_every_parameter_are_closed(self):
        """A later file parameter must not drop the files of an earlier one from the cleanup."""
        api = BoltAPI()
        tracked: dict[str, list[UploadFile]] = {}

        @api.post("/upload")
        async def upload(
            first: Annotated[UploadFile, File()],
            second: Annotated[UploadFile, File()],
            docs: Annotated[list[UploadFile], File()],
            maybe: Annotated[UploadFile | None, File()] = None,
            more: Annotated[list[UploadFile] | None, File()] = None,
        ):
            tracked["first"] = [first]
            tracked["second"] = [second]
            tracked["docs"] = docs
            tracked["maybe"] = [maybe]
            tracked["more"] = more
            return {"ok": True}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files=[
                ("first", ("1.txt", b"1", "text/plain")),
                ("second", ("2.txt", b"2", "text/plain")),
                ("docs", ("3.txt", b"3", "text/plain")),
                ("docs", ("7.txt", b"7", "text/plain")),
                ("maybe", ("4.txt", b"4", "text/plain")),
                ("more", ("5.txt", b"5", "text/plain")),
                ("more", ("6.txt", b"6", "text/plain")),
            ],
        )

        assert response.status_code == 200, response.text
        assert {name: len(files) for name, files in tracked.items()} == {
            "first": 1,
            "second": 1,
            "docs": 2,
            "maybe": 1,
            "more": 2,
        }
        for name, files in tracked.items():
            assert all(upload._file.closed for upload in files), f"{name} should be closed"

    def test_struct_files_closed_after_handler(self):
        """Verify files in Form struct are closed after handler."""
        api = BoltAPI()
        tracked_files: list[UploadFile] = []

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            tracked_files.append(data.avatar)
            assert not data.avatar._file.closed, "File should be open during handler"
            return {"name": data.name}

        client = TestClient(api)
        response = client.post(
            "/profile",
            data={"name": "John"},
            files={"avatar": ("photo.jpg", b"image data", "image/jpeg")},
        )

        assert response.status_code == 200
        assert len(tracked_files) == 1
        assert tracked_files[0]._file.closed, "Struct file should be closed after handler"

    def test_sync_handler_files_closed(self):
        """Verify files are closed after sync handler execution."""
        api = BoltAPI()
        tracked_files: list[UploadFile] = []

        @api.post("/upload")
        def upload(avatar: Annotated[UploadFile, File()]):
            tracked_files.append(avatar)
            assert not avatar._file.closed, "File should be open during handler"
            content = avatar.file.read()
            return {"size": len(content)}

        client = TestClient(api)
        response = client.post(
            "/upload",
            files={"avatar": ("test.txt", b"sync content", "text/plain")},
        )

        assert response.status_code == 200
        assert len(tracked_files) == 1
        assert tracked_files[0]._file.closed, "File should be closed after sync handler"
