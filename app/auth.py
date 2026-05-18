import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Annotated
import bcrypt
import jwt
from jwt.exceptions import InvalidTokenError
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.config import settings
from app.db import get_db

logger = logging.getLogger("app.auth")

# Setup OAuth2 Bearer token extraction
# Specifies the endpoint from which a token should be acquired
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/signin", auto_error=False)


def hash_password(password: str) -> str:
    """
    Hashes a plain password using bcrypt with a generated salt.
    """
    salt = bcrypt.gensalt(rounds=12)  # Production-standard round complexity
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plain password against the stored bcrypt hash.
    Safe against timing attacks.
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except Exception as e:
        logger.exception(f"Error verifying password hash: {e}")
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Generates a secure cryptographically-signed JWT access token.
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})

    encoded_jwt = jwt.encode(
        to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    return encoded_jwt


async def get_current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
    db: Annotated[Any, Depends(get_db)]
) -> Dict:
    """
    FastAPI dependency that extracts, decodes, and validates the bearer token.
    Fetches the authenticated user object from MongoDB.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate active session credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        logger.warning(
            "Authentication failed: Missing Authorization Bearer token header."
        )
        raise credentials_exception

    try:
        # Decode token payload
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        user_id: Optional[str] = payload.get("sub")
        email: Optional[str] = payload.get("email")

        if user_id is None or email is None:
            logger.warning(
                "Authentication failed: Missing required fields in JWT claims."
            )
            raise credentials_exception

    except InvalidTokenError as e:
        logger.warning(
            f"Authentication failed: Invalid JWT signature or expired token: {e}"
        )
        raise credentials_exception

    try:
        # Convert user_id to BSON ObjectId securely
        obj_id = ObjectId(user_id)
    except InvalidId:
        logger.warning(
            f"Authentication failed: Malformed user ObjectId format in token payload: '{user_id}'"
        )
        raise credentials_exception

    # Query user record from connection pool
    user = await db.users.find_one({"_id": obj_id})
    if user is None:
        logger.warning(
            f"Authentication failed: Authenticated user '{email}' no longer exists in DB."
        )
        raise credentials_exception

    # Retain the user object (we strip the hashed password at response boundaries)
    return user
