import uuid
from datetime import datetime, timezone
from typing import List, Annotated, Any
import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.logging_config import get_logger
from app.schemas import DocumentResponse, PresignedUrlRequest, PresignedUrlResponse
from app.rate_limiter import rate_limit_dependency

logger = get_logger("app.routers.documents")

router = APIRouter(prefix="/documents", tags=["Documents"])

# Configure aioboto3 Session (thread-safe, non-blocking IO client)
session = aioboto3.Session()


@router.post(
    "/presigned-url",
    response_model=PresignedUrlResponse,
    dependencies=[Depends(rate_limit_dependency)],
    summary="Generate S3 Pre-signed URL",
    description="Generates a secure S3 Pre-signed PUT URL for direct-to-S3 uploads, isolating paths by user ID.",
)
async def generate_upload_url(
    payload: PresignedUrlRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Any, Depends(get_db)],
):
    """
    Validates request payload, creates a tracking record in MongoDB,
    and generates an S3 pre-signed PUT URL.
    """
    user_id = current_user["_id"]
    user_id_str = str(user_id)

    logger.info(
        f"Generating S3 pre-signed URL for user: {user_id_str}, file: {payload.filename}"
    )

    # Structure unique S3 object key: uploads/{user_id}/{uuid}-{filename}
    unique_id = uuid.uuid4().hex
    file_key = f"uploads/{user_id_str}/{unique_id}-{payload.filename}"

    # Expiration for pre-signed URL (default: 1 hour)
    expiration = 3600

    # Generate S3 Pre-signed PUT URL asynchronously
    try:
        async with session.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
            config=Config(signature_version="s3v4"),
        ) as s3_client:
            upload_url = await s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": settings.S3_BUCKET_NAME,
                    "Key": file_key,
                    "ContentType": payload.content_type,
                },
                ExpiresIn=expiration,
            )
    except ClientError as e:
        logger.exception(f"Failed to generate AWS S3 pre-signed URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to interface with AWS S3 storage provider.",
        )

    # Register document tracking metadata in MongoDB with 'pending' status
    doc_metadata = {
        "filename": payload.filename,
        "file_key": file_key,
        "bucket": settings.S3_BUCKET_NAME,
        "status": "pending",
        "content_type": payload.content_type,
        "file_size_bytes": payload.file_size_bytes,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "use_external_parser": payload.use_external_parser,
    }

    try:
        result = await db.documents.insert_one(doc_metadata)
        document_id = str(result.inserted_id)
        logger.info(
            f"Created pending document record ID: {document_id} for user: {user_id_str}"
        )

        return PresignedUrlResponse(
            upload_url=upload_url,
            file_key=file_key,
            bucket=settings.S3_BUCKET_NAME,
            expires_in_seconds=expiration,
            document_id=document_id,
        )
    except Exception as e:
        logger.exception(f"Failed to save document metadata in DB: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register document upload metadata.",
        )


@router.get(
    "/",
    response_model=List[DocumentResponse],
    summary="List user documents",
    description="Returns a chronological list of all documents belonging to the authenticated user.",
)
async def list_documents(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Any, Depends(get_db)],
):
    """
    Queries MongoDB for user's documents and structures them under DocumentResponse.
    """
    user_id = current_user["_id"]
    logger.info(f"Fetching document list for user ID: {str(user_id)}")

    try:
        # Retrieve and sort documents: newest first
        cursor = db.documents.find({"user_id": user_id}).sort("created_at", -1)
        documents = await cursor.to_list(length=100)  # Retrieve max 100 recent items

        response_list = []
        for doc in documents:
            response_list.append(
                DocumentResponse(
                    id=str(doc["_id"]),
                    filename=doc["filename"],
                    file_key=doc["file_key"],
                    bucket=doc["bucket"],
                    status=doc["status"],
                    content_type=doc["content_type"],
                    file_size_bytes=doc["file_size_bytes"],
                    created_at=doc["created_at"],
                    updated_at=doc["updated_at"],
                    use_external_parser=doc.get("use_external_parser", False),
                )
            )
        return response_list
    except Exception as e:
        logger.exception(f"Error querying documents database: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal database failure while listing documents.",
        )


@router.post(
    "/{document_id}/confirm",
    response_model=DocumentResponse,
    summary="Confirm document upload success",
    description="Updates the document status once upload to S3 is finished.",
)
async def confirm_upload(
    document_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Any, Depends(get_db)],
):
    """
    Allows clients to signal upload completion, transitioning state from pending -> uploaded.
    Verifies that the target document belongs to the authenticated requester.
    """
    user_id = current_user["_id"]
    logger.info(
        f"Confirming upload completion for document: {document_id} by user: {str(user_id)}"
    )

    try:
        doc_obj_id = ObjectId(document_id)
    except InvalidId:
        logger.warning(f"Malstructured document ID submitted: '{document_id}'")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed document tracking identifier.",
        )

    # Retrieve existing document
    document = await db.documents.find_one({"_id": doc_obj_id})
    if not document:
        logger.warning(f"Confirm upload failed: Document {document_id} not found.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document record not found."
        )

    # Security check: verify ownership
    if document["user_id"] != user_id:
        logger.warning(
            f"Access violation: User {str(user_id)} attempted access to document {document_id} owned by user {str(document['user_id'])}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: You do not own this document tracking session.",
        )

    # Record upload success without implying parsing/chunking has finished.
    try:
        update_result = await db.documents.find_one_and_update(
            {"_id": doc_obj_id, "status": "pending"},
            {"$set": {"status": "uploaded", "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        if update_result is None:
            update_result = await db.documents.find_one({"_id": doc_obj_id})
            if update_result is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document record not found.",
                )

        logger.info(
            f"Successfully finalized upload for document {document_id}. Status recorded as '{update_result['status']}'."
        )

        return DocumentResponse(
            id=str(update_result["_id"]),
            filename=update_result["filename"],
            file_key=update_result["file_key"],
            bucket=update_result["bucket"],
            status=update_result["status"],
            content_type=update_result["content_type"],
            file_size_bytes=update_result["file_size_bytes"],
            created_at=update_result["created_at"],
            updated_at=update_result["updated_at"],
            use_external_parser=update_result.get("use_external_parser", False),
        )
    except Exception as e:
        logger.exception(f"Unexpected database exception during document confirm: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update document upload status in database.",
        )


@router.post(
    "/{document_id}/regenerate-upload-url",
    response_model=PresignedUrlResponse,
    dependencies=[Depends(rate_limit_dependency)],
    summary="Regenerate expired S3 pre-signed upload URL",
    description="Regenerates a fresh AWS S3 pre-signed PUT URL for an existing pending document tracking session.",
)
async def regenerate_upload_url(
    document_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[Any, Depends(get_db)],
):
    """
    Looks up an existing pending document, verifies ownership, and returns a new valid pre-signed URL.
    """
    user_id = current_user["_id"]
    user_id_str = str(user_id)

    logger.info(
        f"Regenerating pre-signed URL for document: {document_id} by user: {user_id_str}"
    )

    try:
        doc_obj_id = ObjectId(document_id)
    except InvalidId:
        logger.warning(f"Malstructured document ID submitted: '{document_id}'")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed document tracking identifier.",
        )

    # Retrieve existing document
    document = await db.documents.find_one({"_id": doc_obj_id})
    if not document:
        logger.warning(f"Regenerate upload URL failed: Document {document_id} not found.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document record not found."
        )

    # Security check: verify ownership
    if document["user_id"] != user_id:
        logger.warning(
            f"Access violation: User {user_id_str} attempted regeneration on document {document_id} owned by user {str(document['user_id'])}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: You do not own this document tracking session.",
        )

    # Business rule: only allow regeneration for pending uploads
    if document["status"] == "completed":
        logger.warning(
            f"Regeneration rejected: Document {document_id} is already in 'completed' status."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot regenerate upload URL for a completed document. Please initiate a new document request.",
        )

    # Expiration for pre-signed URL (default: 1 hour)
    expiration = 3600

    # Generate a fresh S3 Pre-signed PUT URL using the exact original file_key and content_type asynchronously
    try:
        async with session.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
            config=Config(signature_version="s3v4"),
        ) as s3_client:
            upload_url = await s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": document["bucket"],
                    "Key": document["file_key"],
                    "ContentType": document["content_type"],
                },
                ExpiresIn=expiration,
            )
    except ClientError as e:
        logger.exception(f"Failed to regenerate AWS S3 pre-signed URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to interface with AWS S3 storage provider.",
        )

    # Update updated_at timestamp in database
    try:
        await db.documents.update_one(
            {"_id": doc_obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc)}},
        )
    except Exception as e:
        # Non-blocking log warning since pre-signed URL calculation succeeded
        logger.warning(f"Failed to update document updated_at timestamp in database: {e}")

    logger.info(
        f"Successfully regenerated S3 pre-signed URL for document {document_id} for user {user_id_str}."
    )

    return PresignedUrlResponse(
        upload_url=upload_url,
        file_key=document["file_key"],
        bucket=document["bucket"],
        expires_in_seconds=expiration,
        document_id=document_id,
    )
