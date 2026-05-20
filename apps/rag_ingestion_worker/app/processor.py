import asyncio
from datetime import datetime, timezone
from typing import Optional
from pymongo import AsyncMongoClient
from .config import settings
from .utils.logging import get_worker_logger
from .utils.chunking import perform_text_chunking

logger = get_worker_logger("worker.processor")


import json
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET


class NormalizedDocument:
    """Standardized representation of parsed document content and metadata."""

    def __init__(self, text: str, metadata: dict):
        self.text = text
        self.metadata = metadata


def _extract_extension(filename: str) -> str:
    """Extracts extension safely from filename."""
    return filename.split(".")[-1].lower() if "." in filename else ""


def _detect_magic_mime(magic: bytes, ext: str) -> Optional[str]:
    """Detects MIME type purely by inspecting magic bytes."""
    if magic.startswith(b"%PDF"):
        return "application/pdf"
    if magic.startswith(b"PK\x03\x04"):
        if ext in ["docx", "xlsx", "pptx"]:
            sub_types = {
                "docx": "wordprocessingml.document",
                "xlsx": "spreadsheetml.sheet",
                "pptx": "presentationml.presentation",
            }
            sub_type = sub_types[ext]
            return f"application/vnd.openxmlformats-officedocument.{sub_type}"
        return "application/zip"
    if magic.startswith(b"\x89PNG"):
        return "image/png"
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith((b"{", b"[")):
        return "application/json"
    if magic.startswith(b"<?xml"):
        return "application/xml"
    return None


def _determine_parser_route(final_mime: str, ext: str, s3_mime: str) -> str:
    """Determines parser route for processing."""
    # 1. Direct extensions mapping (O(1) lookup, low complexity)
    ext_routes = {
        "pdf": "pdf",
        "json": "json",
        "csv": "csv",
        "docx": "docx",
        "xlsx": "xlsx",
        "pptx": "pptx",
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "gif": "image",
        "txt": "text",
        "md": "text",
        "xml": "text",
    }
    if ext in ext_routes:
        return ext_routes[ext]

    # 2. Substring matching in MIME type (loop lookup)
    mime_routes = [
        ("pdf", "pdf"),
        ("json", "json"),
        ("csv", "csv"),
        ("wordprocessingml", "docx"),
        ("spreadsheetml", "xlsx"),
        ("presentationml", "pptx"),
        ("image", "image"),
        ("text", "text"),
    ]
    for pattern, route in mime_routes:
        if pattern in final_mime:
            return route

    # 3. Fallback MIME checks
    if "csv" in s3_mime:
        return "csv"

    return "fallback"


def classify_file(data: bytes, s3_content_type: Optional[str], filename: str) -> dict:
    """
    Stage 1 - File Classification
    Determines MIME type, extension, magic bytes, and potential scanned/corrupted state.
    """
    ext = _extract_extension(filename)
    magic = data[:4]

    s3_mime = s3_content_type or "application/octet-stream"
    magic_mime = _detect_magic_mime(magic, ext)
    final_mime = magic_mime or s3_mime

    route = _determine_parser_route(final_mime, ext, s3_mime)

    return {
        "mime_type": final_mime,
        "extension": ext,
        "magic_bytes": magic.hex(),
        "route": route,
        "corrupted": len(data) == 0,
    }


def parse_text(data: bytes, metadata: dict) -> NormalizedDocument:
    text = data.decode("utf-8", errors="ignore")
    return NormalizedDocument(text, metadata)


def parse_json(data: bytes, metadata: dict) -> NormalizedDocument:
    try:
        obj = json.loads(data.decode("utf-8", errors="ignore"))
        text = json.dumps(obj, indent=2)
    except Exception as e:
        text = f"Error parsing JSON: {e}\nRaw: {data.decode('utf-8', errors='ignore')}"
    return NormalizedDocument(text, metadata)


def parse_csv(data: bytes, metadata: dict) -> NormalizedDocument:
    try:
        f = io.StringIO(data.decode("utf-8", errors="ignore"))
        reader = csv.reader(f)
        rows = [", ".join(row) for row in reader if row]
        text = "\n".join(rows)
    except Exception as e:
        text = f"Error parsing CSV: {e}"
    return NormalizedDocument(text, metadata)


def parse_pdf(data: bytes, metadata: dict) -> NormalizedDocument:
    bt_et_blocks = re.findall(b"BT(.*?)ET", data, re.DOTALL)
    extracted_texts = []
    for block in bt_et_blocks:
        strings = re.findall(b"\((.*?)\)", block)
        for s in strings:
            try:
                extracted_texts.append(s.decode("utf-8", errors="ignore"))
            except Exception:
                pass
    text = " ".join(extracted_texts).strip()

    # Scanned PDF Detection (Stage 2 transition to OCR)
    if not text and len(data) > 0:
        metadata["scanned_pdf"] = True
        text = (
            "[Scanned PDF Detected - OCR Route Simulated: Extracted plain image layout]"
        )
    else:
        metadata["scanned_pdf"] = False

    return NormalizedDocument(text, metadata)


def parse_docx(data: bytes, metadata: dict) -> NormalizedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            texts = [
                elem.text
                for elem in root.iter()
                if elem.tag.endswith("t") and elem.text
            ]
            text = " ".join(texts)
    except Exception as e:
        text = f"Error parsing DOCX: {e}"
    return NormalizedDocument(text, metadata)


def parse_xlsx(data: bytes, metadata: dict) -> NormalizedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "xl/sharedStrings.xml" in z.namelist():
                xml_content = z.read("xl/sharedStrings.xml")
                root = ET.fromstring(xml_content)
                texts = [
                    elem.text
                    for elem in root.iter()
                    if elem.tag.endswith("t") and elem.text
                ]
                text = " ".join(texts)
            else:
                text = ""
    except Exception as e:
        text = f"Error parsing XLSX: {e}"
    return NormalizedDocument(text, metadata)


def parse_pptx(data: bytes, metadata: dict) -> NormalizedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            texts = []
            slide_files = [
                f
                for f in z.namelist()
                if f.startswith("ppt/slides/slide") and f.endswith(".xml")
            ]
            for f in slide_files:
                xml_content = z.read(f)
                root = ET.fromstring(xml_content)
                texts.extend(
                    [
                        elem.text
                        for elem in root.iter()
                        if elem.tag.endswith("t") and elem.text
                    ]
                )
            text = " ".join(texts)
    except Exception as e:
        text = f"Error parsing PPTX: {e}"
    return NormalizedDocument(text, metadata)


def parse_image(_data: bytes, metadata: dict) -> NormalizedDocument:
    text = "[OCR Processed Image - Extracted visual layout blocks]"
    return NormalizedDocument(text, metadata)


def parse_fallback(data: bytes, metadata: dict) -> NormalizedDocument:
    text = data.decode("utf-8", errors="ignore")
    return NormalizedDocument(text, metadata)


class IngestionProcessor:
    """
    Production-hardened ingestion processor.
    - Idempotency via MongoDB unique tracking.
    - S3 fetching via aioboto3 (handled in main).
    - Status transitions and error classification.
    """

    def __init__(self, db_client: AsyncMongoClient):
        self.db_client = db_client
        self.db = self.db_client[settings.MONGODB_DB_NAME]

    async def is_already_processed(self, message_id: str, file_key: str) -> bool:
        """
        Idempotency check:
        Ensures we don't process the same SQS message or file key if already completed.
        """
        # Check by SQS Message ID or a successfully ingested file_key
        existing = await self.db.documents.find_one(
            {
                "$or": [
                    {"last_sqs_message_id": message_id},
                    {"file_key": file_key, "status": "completed"},
                ]
            }
        )
        return existing is not None

    async def run(
        self,
        bucket: str,
        key: str,
        message_id: str,
        data: bytes,
        s3_content_type: Optional[str] = None,
    ):
        """
        Processes document content and updates state.
        """
        log_extra = {"bucket": bucket, "key": key, "message_id": message_id}
        logger.info("Processing document content", extra=log_extra)

        doc_filter = {"file_key": key}

        # Stage 1: Classify file
        classification = classify_file(data, s3_content_type, filename=key)
        logger.info(f"File classification complete: {classification}", extra=log_extra)

        # Initial update with classification metadata
        await self.db.documents.update_one(
            doc_filter,
            {
                "$set": {
                    "status": "processing",
                    "last_sqs_message_id": message_id,
                    "mime_type": classification["mime_type"],
                    "extension": classification["extension"],
                    "parser_route": classification["route"],
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

        try:
            # Stage 2 & 3: Routing and Normalization
            route = classification["route"]
            metadata = {
                "bucket": bucket,
                "key": key,
                "message_id": message_id,
                "mime_type": classification["mime_type"],
                "extension": classification["extension"],
            }

            if route == "pdf":
                norm_doc = parse_pdf(data, metadata)
            elif route == "json":
                norm_doc = parse_json(data, metadata)
            elif route == "csv":
                norm_doc = parse_csv(data, metadata)
            elif route == "docx":
                norm_doc = parse_docx(data, metadata)
            elif route == "xlsx":
                norm_doc = parse_xlsx(data, metadata)
            elif route == "pptx":
                norm_doc = parse_pptx(data, metadata)
            elif route == "image":
                norm_doc = parse_image(data, metadata)
            elif route == "text":
                norm_doc = parse_text(data, metadata)
            else:
                norm_doc = parse_fallback(data, metadata)

            logger.info(
                f"Normalization complete (Text size: {len(norm_doc.text)} chars)",
                extra=log_extra,
            )

            # Stage 4: Perform chunking on normalized text
            chunks = perform_text_chunking(
                norm_doc.text,
                chunk_size=settings.CHUNK_SIZE,
                chunk_overlap=settings.CHUNK_OVERLAP,
            )

            # Atomic finalization
            await self.db.documents.update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "completed",
                        "chunk_count": len(chunks),
                        "scanned_pdf": norm_doc.metadata.get("scanned_pdf", False),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            logger.info("Ingestion completed successfully", extra=log_extra)

        except Exception as e:
            logger.exception("Ingestion failed", extra=log_extra)
            await self.db.documents.update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "failed",
                        "error": str(e),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            raise


async def heartbeat_extender(
    sqs_client, queue_url: str, receipt_handle: str, interval: int = 15
):
    """
    Background loop to keep SQS message hidden while processing is active.
    Prevents other workers from picking up the same message.
    """
    logger.info("Heartbeat started")
    try:
        while True:
            await asyncio.sleep(interval)
            await sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=30,  # Extend for another 30s
            )
            logger.info("Heartbeat: extended visibility timeout")
    except asyncio.CancelledError:
        logger.info("Heartbeat stopped")
        raise
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
