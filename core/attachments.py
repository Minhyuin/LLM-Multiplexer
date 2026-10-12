"""Attachment storage, validation and cleanup helpers for the portal.

Design notes
------------
* Files are stored under ``settings.PRIVATE_UPLOAD_ROOT`` (never ``MEDIA_ROOT``),
  because the reverse proxy serves ``/media/`` directly and would bypass the
  portal's login/permission checks.  A private root is only reachable through the
  authenticated Django view ``core.views.attachment_serve``.
* Storage names are random UUIDs (with a validated extension); the original
  filename is kept only in the database for display.  This makes stored paths
  bounded, unguessable and traversal-proof.
* Every upload is validated (size, extension, magic bytes, decoded image) *before*
  anything is written to disk.  The view only persists after the whole form
  validated, and ``save_attachments`` removes already-written files if a later
  write fails.
* Images are validated with Pillow (a required, pinned dependency):
  ``Image.open`` + ``image.verify()`` proves the bytes really decode as the format
  the extension claims, and ``image.size`` is checked against explicit edge and
  pixel limits so a decompression bomb cannot exhaust memory.

Known limitation (commit-time failures)
---------------------------------------
The filesystem is not transactional.  ``save_attachments`` writes blobs and then
inserts rows inside its own savepoint, and row deletions schedule blob removal with
``transaction.on_commit``.  Both directions are protected against *failures before
commit* (see ``_cleanup_blobs`` / ``_remove_blob_on_commit``).
What cannot be covered is a failure *at* COMMIT: if the database connection drops
between the final blob write and the commit, the rollback leaves the row absent
while the blob stays on disk (an orphan).  Views therefore perform the blob write
as their last fallible step, which shrinks the window to the commit itself.  A
periodic reconciliation job (list ``PRIVATE_UPLOAD_ROOT``, drop files with no
``Attachment.file`` row, and rows whose file is missing) is the intended remedy;
it is deliberately not implemented here to keep the feature self-contained.
"""

from __future__ import annotations

import os
import uuid
import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils.text import get_valid_filename

# --- Allowed formats ---------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Formats Pillow is allowed to report for each image extension.
PILLOW_IMAGE_FORMATS = {
    ".jpg": {"JPEG", "MPO"},
    ".jpeg": {"JPEG", "MPO"},
    ".png": {"PNG"},
    ".gif": {"GIF"},
    ".webp": {"WEBP"},
}
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
}
TEXT_EXTENSIONS = {".txt", ".md", ".log", ".json", ".csv", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz"}
ZIP_CONTAINER_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip"}

DOCUMENT_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
}

ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS | TEXT_EXTENSIONS | ARCHIVE_EXTENSIONS

# Executable / script / web formats that are refused outright.
DANGEROUS_EXTENSIONS = {
    ".exe", ".msi", ".com", ".scr", ".pif", ".cpl", ".dll", ".sys", ".drv", ".ocx",
    ".so", ".dylib", ".bin", ".run", ".app", ".jar", ".apk", ".ipa", ".deb", ".rpm",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".wsf", ".wsh", ".hta", ".lnk",
    ".reg", ".sh", ".bash", ".zsh", ".csh", ".ksh", ".py", ".pyc", ".pyo", ".rb",
    ".pl", ".cgi", ".php", ".php3", ".php4", ".php5", ".phtml", ".phar", ".asp",
    ".aspx", ".jsp", ".jspx", ".do", ".action", ".htaccess", ".svg", ".svgz",
    ".html", ".htm", ".xhtml", ".swf",
}

# Leading magic bytes used to sanity-check non-image formats (images are proven
# by Pillow instead).  Anything that does not match the format implied by its
# extension is rejected, so a script cannot masquerade as a document/archive.
_EXECUTABLE_MAGIC = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe")
_ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh", b"\xfd7zXZ")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def human_size(num_bytes) -> str:
    """Format a byte count for display (used by the template filter)."""
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def extension_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def _read_head(upload, length: int = 64) -> bytes:
    """Read up to ``length`` leading bytes without disturbing the stream position."""
    try:
        position = upload.tell()
    except (AttributeError, OSError):
        position = None
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        return b""
    try:
        return upload.read(length) or b""
    finally:
        if position is not None:
            try:
                upload.seek(position)
            except (AttributeError, OSError):
                pass


def _rewind(upload):
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass


def validate_image(upload, ext: str, *, max_edge=None, max_pixels=None):
    """Decode-check an image with Pillow and return ``(width, height)``.

    ``image.verify()`` proves the bytes decode as the claimed format; the size is
    then checked against explicit edge/pixel limits.
    """
    from PIL import Image, UnidentifiedImageError  # required, pinned dependency

    max_edge = settings.ATTACHMENT_MAX_EDGE if max_edge is None else max_edge
    max_pixels = settings.ATTACHMENT_MAX_PIXELS if max_pixels is None else max_pixels
    try:
        _rewind(upload)
        with Image.open(upload) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            image.verify()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValidationError("图片无法解码，可能已损坏或与扩展名不符。") from exc
    finally:
        _rewind(upload)

    if image_format not in PILLOW_IMAGE_FORMATS[ext]:
        raise ValidationError("图片内容与扩展名不符。")
    if not width or not height:
        raise ValidationError("图片尺寸无效。")
    if width > max_edge or height > max_edge or width * height > max_pixels:
        raise ValidationError("图片尺寸过大，请压缩后再上传。")
    return int(width), int(height)


def _assert_zip_container(upload):
    """Office/ODF/zip uploads must actually be readable zip containers."""
    try:
        _rewind(upload)
        if not zipfile.is_zipfile(upload):
            raise ValidationError("压缩包内容无效，文件可能已损坏。")
    finally:
        _rewind(upload)


def _matches_extension(upload, ext: str, head: bytes) -> bool:
    if ext in ZIP_CONTAINER_EXTENSIONS:
        return head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    if ext == ".pdf":
        return head[:5] == b"%PDF-"
    if ext in (".doc", ".xls", ".ppt"):
        return head[:8] == _OLE_MAGIC
    if ext in (".gz", ".tgz"):
        return head[:2] == b"\x1f\x8b"
    if ext == ".tar":
        return head[257:262] == b"ustar"
    if ext in TEXT_EXTENSIONS:
        # Text must not merely be a renamed binary/executable/archive.
        if not head:
            return False
        if head[:4] in _EXECUTABLE_MAGIC or head[:4] in _ARCHIVE_MAGIC or head[:8] == _OLE_MAGIC:
            return False
        if head.startswith(b"#!") or b"\x00" in head:
            return False
        return True
    return False


def validate_attachment(upload, *, max_bytes=None, max_pixels=None, max_edge=None):
    """Validate one uploaded file and return metadata for an Attachment row.

    Raises ``django.core.exceptions.ValidationError`` for anything that is not an
    allowed, well-formed, in-limit upload.  Performs no disk writes.
    """
    max_bytes = settings.ATTACHMENT_MAX_BYTES if max_bytes is None else max_bytes
    max_pixels = settings.ATTACHMENT_MAX_PIXELS if max_pixels is None else max_pixels
    max_edge = settings.ATTACHMENT_MAX_EDGE if max_edge is None else max_edge

    name = getattr(upload, "name", "") or ""
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise ValidationError("不能上传空文件。")
    if size > max_bytes:
        raise ValidationError(f"单个文件不能超过 {human_size(max_bytes)}（当前 {human_size(size)}）。")

    safe_name = get_valid_filename(os.path.basename(name)) or "file"
    ext = extension_of(safe_name)
    if not ext:
        raise ValidationError("文件必须带有扩展名。")
    if ext in DANGEROUS_EXTENSIONS:
        raise ValidationError(f"不允许上传可执行或脚本类文件（{ext}）。")
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"不支持的文件类型（{ext}）。")
    # Reject double extensions such as payload.php.jpg.
    stem_ext = extension_of(os.path.splitext(safe_name)[0])
    if stem_ext in DANGEROUS_EXTENSIONS:
        raise ValidationError("文件名包含危险的双重扩展名。")

    head = _read_head(upload, 64)
    if head[:2] in _EXECUTABLE_MAGIC or head[:4] in _EXECUTABLE_MAGIC:
        raise ValidationError("检测到可执行文件特征，已拒绝。")
    if head.startswith(b"#!"):
        raise ValidationError("检测到脚本文件特征，已拒绝。")

    kind = "image" if ext in IMAGE_EXTENSIONS else ("archive" if ext in ARCHIVE_EXTENSIONS else "document")
    width = height = None
    if kind == "image":
        width, height = validate_image(upload, ext, max_edge=max_edge, max_pixels=max_pixels)
    else:
        if not _matches_extension(upload, ext, head):
            raise ValidationError("文件内容与扩展名不符，已拒绝。")
        if ext in ZIP_CONTAINER_EXTENSIONS:
            _assert_zip_container(upload)

    return {
        "kind": kind,
        "mime_type": IMAGE_MIME.get(ext) or DOCUMENT_MIME.get(ext, "application/octet-stream"),
        "size": int(size),
        "width": width,
        "height": height,
        "original_name": safe_name[:255],
    }


def check_batch_limits(uploads, existing_count=0, *, max_per_content=None, max_batch_bytes=None):
    """Reject a submission that adds too many files or too much total data.

    ``existing_count`` is the number of attachments already stored on the parent
    announcement/post, so the per-content cap is enforced across edits too.
    """
    uploads = list(uploads or [])
    max_per_content = settings.ATTACHMENT_MAX_PER_CONTENT if max_per_content is None else max_per_content
    max_batch_bytes = settings.ATTACHMENT_MAX_BATCH_BYTES if max_batch_bytes is None else max_batch_bytes
    if existing_count + len(uploads) > max_per_content:
        remaining = max(0, max_per_content - existing_count)
        raise ValidationError(f"每条内容最多 {max_per_content} 个附件，当前还可添加 {remaining} 个。")
    total = sum(getattr(upload, "size", 0) or 0 for upload in uploads)
    if total > max_batch_bytes:
        raise ValidationError(f"本次上传总大小不能超过 {human_size(max_batch_bytes)}（当前 {human_size(total)}）。")


# --- Storage -----------------------------------------------------------------


class PrivateAttachmentStorage(FileSystemStorage):
    """Store attachments outside MEDIA_ROOT under random UUID names.

    ``location`` is resolved from settings on every access (not cached at import
    time), so ``override_settings(PRIVATE_UPLOAD_ROOT=...)`` — including the
    temporary root used by tests — is honoured.
    """

    def __init__(self, **kwargs):
        kwargs.pop("location", None)
        kwargs.setdefault("base_url", None)
        super().__init__(**kwargs)

    @property
    def location(self):
        return os.path.abspath(str(settings.PRIVATE_UPLOAD_ROOT))

    @location.setter
    def location(self, value):  # pragma: no cover - keeps FileSystemStorage API happy
        pass

    @property
    def base_url(self):
        # Stored blobs are only served through the pk-based attachment-serve view,
        # so the raw storage URL is never a valid download link; point it at the
        # private prefix rather than the public MEDIA_URL.
        return settings.PRIVATE_UPLOAD_URL

    @base_url.setter
    def base_url(self, value):  # pragma: no cover
        pass

    def get_available_name(self, name, max_length=None):
        name = os.path.basename(name)
        if max_length and len(name) > max_length:
            stem, ext = os.path.splitext(name)
            name = stem[: max(0, max_length - len(ext))] + ext
        return super().get_available_name(name, max_length)

    def save(self, name, content, max_length=None):
        ext = extension_of(name)[:10]
        return super().save(f"{uuid.uuid4().hex}{ext}", content, max_length)


# --- Persistence / cleanup ---------------------------------------------------

def delete_storage_file(storage, name):
    """Best-effort removal of one stored blob."""
    if not name:
        return
    try:
        storage.delete(name)
    except OSError:
        pass


def save_attachments(uploads, user, *, announcement=None, post=None):
    """Persist ``uploads`` as Attachment rows on exactly one parent.

    Ordering guarantees:
    * every upload is validated first, so a rejected batch writes nothing;
    * the batch runs in its own savepoint (``transaction.atomic``), so a database
      error rolls the rows back *and* leaves the caller's transaction usable — the
      cleanup below then runs on a healthy connection;
    * each blob is tracked the moment it is written, so a later failure removes it
      even if the corresponding DB row never landed;
    * nothing is deleted for already-stored content here — only blobs this call
      wrote are cleaned up, and the caller's transaction can still roll back.
    """
    from core.models import Attachment  # local import avoids a circular import

    if (announcement is None) == (post is None):
        raise ValueError("save_attachments requires exactly one parent (announcement or post)")
    uploads = list(uploads or [])
    if not uploads:
        return []
    metas = [validate_attachment(upload) for upload in uploads]  # all-or-nothing, no writes yet

    written = []
    rows = []
    try:
        # A savepoint around the whole batch: on a database error the rows are
        # rolled back here instead of poisoning the caller's transaction, so the
        # filesystem cleanup in the ``except`` clause can still run (row.delete()
        # would raise TransactionManagementError on a broken transaction).
        with transaction.atomic():
            for upload, meta in zip(uploads, metas):
                attachment = Attachment(
                    announcement=announcement,
                    post=post,
                    uploader=user,
                    original_name=meta["original_name"],
                    kind=meta["kind"],
                    mime_type=meta["mime_type"],
                    size=meta["size"],
                    width=meta["width"],
                    height=meta["height"],
                )
                # ``FieldFile.save`` returns None, so track the real stored name.
                attachment.file.save(meta["original_name"], upload, save=False)
                written.append((attachment.file.storage, attachment.file.name))
                attachment.save()
                rows.append(attachment)
    except Exception:
        # Rows are already rolled back by the savepoint above; only the blobs this
        # call wrote need removing.
        for storage, name in written:
            delete_storage_file(storage, name)
        raise
    return rows
