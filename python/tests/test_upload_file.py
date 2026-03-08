"""Tests for UploadFile functionality."""

from __future__ import annotations

import asyncio
from typing import Annotated

import msgspec

from django_bolt import BoltAPI, UploadFile
from django_bolt.datastructures import UploadFile as UploadFileClass
from django_bolt.params import File, Form
from django_bolt.testing import TestClient


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


class TestUploadFileClass:
    """Test basic UploadFile class functionality."""

    def test_create_upload_file(self):
        """Test creating an UploadFile instance."""
        upload = UploadFileClass(
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

    def test_sync_read(self):
        """Test sync read via .file property."""
        upload = UploadFileClass(
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

    def test_async_read(self):
        """Test async read method."""

        async def do_test():
            upload = UploadFileClass(
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

    def test_async_close(self):
        """Test async close method."""

        async def do_test():
            upload = UploadFileClass(
                filename="test.txt",
                content_type="text/plain",
                size=5,
                file_data=b"Hello",
            )

            await upload.close()
            assert upload.file.closed

        asyncio.run(do_test())

    def test_repr(self):
        """Test string representation."""
        upload = UploadFileClass(
            filename="test.txt",
            content_type="text/plain",
            size=100,
            file_data=b"x" * 100,
        )

        repr_str = repr(upload)
        assert "test.txt" in repr_str
        assert "text/plain" in repr_str
        assert "100" in repr_str

    def test_file_is_django_file(self):
        """Test that .file returns a Django File object."""
        upload = UploadFileClass(
            filename="document.pdf",
            content_type="application/pdf",
            size=15,
            file_data=b"fake pdf content",
        )

        django_file = upload.file

        # Check it's a Django File
        from django.core.files.base import File

        assert isinstance(django_file, File)
        assert django_file.name == "document.pdf"
        assert django_file.read() == b"fake pdf content"


class TestUploadFileValidation:
    """Test file upload validation with File() constraints."""

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
        assert "Missing required file" in errors[0]["msg"]

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
        assert "Missing required file" in errors[0]["msg"]


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
        from tests.test_models import Document

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
        from tests.test_models import Document

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
        from tests.test_models import Document

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
        from tests.test_models import Document

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
