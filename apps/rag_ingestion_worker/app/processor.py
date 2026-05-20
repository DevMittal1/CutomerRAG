import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional
from pymongo import AsyncMongoClient
from .config import settings
from .utils.logging import get_worker_logger

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
        existing = await self.db[settings.COLL_DOCUMENTS].find_one({
            "$or": [
                {"last_sqs_message_id": message_id},
                {"file_key": file_key, "status": "completed"}
            ]
        })
        return existing is not None

    def _should_retry(self, exception: Exception, attempt: int, max_retries: int) -> bool:
        """Determines if the exception warrants a retry attempt."""
        if attempt >= max_retries:
            return False

        if isinstance(exception, httpx.HTTPStatusError):
            status = exception.response.status_code
            return status == 429 or status >= 500

        return isinstance(exception, (httpx.RequestError, httpx.TimeoutException))

    def _get_retry_delay(self, attempt: int, base_delay: float, max_delay: float) -> float:
        """Calculates exponential backoff with full jitter."""
        import random
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        return delay * random.uniform(0.5, 1.5)

    async def _attempt_submission(
        self, client: httpx.AsyncClient, headers: dict, files: dict, data: dict
    ) -> str:
        """Executes a single HTTP request attempt to Landing AI."""
        response = await client.post(
            f"{settings.LANDING_AI_BASE_URL}/parse/jobs",
            headers=headers,
            files=files,
            data=data
        )
        response.raise_for_status()
        return response.json()["job_id"]

    async def submit_to_landing_ai(self, file_content: bytes, filename: str) -> str:
        """
        Uploads file to Landing AI and returns job_id.
        Includes a robust retry mechanism with exponential backoff and jitter for transient errors.
        """
        if not settings.LANDING_AI_API_KEY:
            raise ValueError("LANDING_AI_API_KEY is not set")

        headers = {
            "Authorization": f"Bearer {settings.LANDING_AI_API_KEY}"
        }
        files = {
            "document": (filename, file_content)
        }
        data = {
            "model": "dpt-2-latest"
        }

        max_retries = 5
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"Submitting to Landing AI (attempt {attempt}/{max_retries})")
                    return await self._attempt_submission(client, headers, files, data)
                except Exception as e:
                    if not self._should_retry(e, attempt, max_retries):
                        raise

                    delay = self._get_retry_delay(attempt, 1.0, 10.0)
                    logger.warning(
                        f"Landing AI attempt {attempt} failed ({e}). Retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError("Landing AI submission failed after all retries.")

    async def run(
        self,
        bucket: str,
        key: str,
        message_id: str,
        content_bytes: bytes,
        _s3_content_type: Optional[str] = None,
    ):
        """
        Submits document to Landing AI and updates state to pending.
        """
        log_extra = {"bucket": bucket, "key": key, "message_id": message_id}
        logger.info("Submitting document to Landing AI", extra=log_extra)

        doc_filter = {"file_key": key}

        # Initial update
        await self.db[settings.COLL_DOCUMENTS].update_one(
            doc_filter,
            {
                "$set": {
                    "status": "processing",
                    "last_sqs_message_id": message_id,
                    "updated_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        try:
            # 1. Submit to Landing AI
            job_id = await self.submit_to_landing_ai(content_bytes, key)

            # 2. Update state to landing_ai_pending
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "landing_ai_pending",
                        "landing_ai_job_id": job_id,
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            logger.info(f"Document submitted to Landing AI. JobID: {job_id}", extra=log_extra)

        except Exception as e:
            logger.exception("Landing AI submission failed", extra=log_extra)
            await self.db[settings.COLL_DOCUMENTS].update_one(
                doc_filter,
                {
                    "$set": {
                        "status": "failed",
                        "error": str(e),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            raise


async def heartbeat_extender(sqs_client, queue_url: str, receipt_handle: str, interval: int = 15):
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
                VisibilityTimeout=30  # Extend for another 30s
            )
            logger.info("Heartbeat: extended visibility timeout")
    except asyncio.CancelledError:
        logger.info("Heartbeat stopped")
        raise
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
