"""Read a document explicitly selected by the browsing model. Never discovers links."""
import io
import zipfile

MAX_BYTES = 8 * 1024 * 1024


def document_text(data: bytes, member: str = "") -> str:
    if len(data) > MAX_BYTES:
        raise ValueError("Document exceeds the extraction size limit")
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if not member:
                return "Archive members (choose a member with read_document):\n" + "\n".join(archive.namelist()[:100])
            info = archive.getinfo(member)
            if info.file_size > MAX_BYTES or info.is_dir():
                raise ValueError("Selected archive member is too large or is a directory")
            data = archive.read(info)
    if data.startswith(b"%PDF"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > 100:
            raise ValueError("PDF exceeds 100 pages; choose a narrower document")
        return "\n\n".join(page.extract_text() or "[Image-only page: visual inspection required]" for page in reader.pages)
    return data.decode("utf-8-sig", errors="replace")
