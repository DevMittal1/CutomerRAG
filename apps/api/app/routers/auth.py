from datetime import datetime, timezone
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pymongo.errors import DuplicateKeyError
from app.auth import (
    get_current_user,
    hash_password,
    verify_password,
    create_access_token,
)
from app.config import settings
from app.db import get_db
from app.logging_config import get_logger
from app.schemas import UserResponse, UserSignIn, UserSignUp, Token

logger = get_logger("app.routers.auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="Registers a new user in the system with credentials validation and secure password hashing.",
)
async def signup(payload: UserSignUp, db: Annotated[Any, Depends(get_db)]):
    """
    Creates a new user record in MongoDB. Email is guaranteed unique by DB unique index.
    """
    logger.info(f"Initiating user signup process for email: {payload.email}")

    # Hash password using bcrypt
    hashed = hash_password(payload.password)

    user_doc = {
        "email": payload.email,
        "password_hash": hashed,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    try:
        # Write to database
        result = await db.users.insert_one(user_doc)
        inserted_id = str(result.inserted_id)
        logger.info(
            f"Successfully registered user: {payload.email} with ID: {inserted_id}"
        )

        return UserResponse(
            id=inserted_id, email=payload.email, created_at=user_doc["created_at"]
        )
    except DuplicateKeyError:
        logger.warning(
            f"Registration failed: Email '{payload.email}' already exists in DB."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email address is already registered.",
        )
    except Exception as e:
        logger.exception(f"Unexpected database failure during signup: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal database error occurred while registering the user.",
        )


@router.post(
    "/signin",
    response_model=Token,
    summary="User authentication login",
    description="Authenticates credentials and returns a secure JWT access token.",
)
async def signin(payload: UserSignIn, db: Annotated[Any, Depends(get_db)]):
    """
    Validates user credentials and issues a JWT token containing the user sub and email.
    """
    logger.info(f"Authentication request for user: {payload.email}")

    # Query user document
    user = await db.users.find_one({"email": payload.email})

    if not user or not verify_password(payload.password, user["password_hash"]):
        logger.warning(
            f"Authentication failed: Invalid credentials provided for: {payload.email}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # User is valid; create access token
    user_id_str = str(user["_id"])
    token_data = {"sub": user_id_str, "email": user["email"]}

    access_token = create_access_token(data=token_data)
    logger.info(f"User '{payload.email}' authenticated successfully. Issued JWT.")

    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user details",
    description="Returns profile information for the currently authenticated session.",
)
async def get_me(current_user: Annotated[dict, Depends(get_current_user)]):
    """
    Fetches the profile info of the currently logged-in user.
    """
    return UserResponse(
        id=str(current_user["_id"]),
        email=current_user["email"],
        created_at=current_user["created_at"],
    )

@router.post(
    "/token",
    response_model=Token,
    summary="OAuth2 compatible token login",
    description="Authenticates credentials using standard OAuth2 form-data and returns a secure JWT access token.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Incorrect email or password",
            "content": {
                "application/json": {
                    "example": {"detail": "Incorrect email or password"}
                }
            }
        }
    }
)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[Any, Depends(get_db)],
):
    user = await db.users.find_one({
        "email": form_data.username
    })

    if not user or not verify_password(
        form_data.password,
        user["password_hash"]
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_data = {
        "sub": str(user["_id"]),
        "email": user["email"],
    }

    access_token = create_access_token(data=token_data)

    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )