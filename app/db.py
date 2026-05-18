import logging
from pymongo import AsyncMongoClient
from app.config import settings

logger = logging.getLogger("app.db")


class MongoManager:
    """
    Manages the lifecycle of the MongoDB connection pool.
    """

    client: AsyncMongoClient = None
    db = None


# Singleton manager
mongo_manager = MongoManager()


async def connect_to_mongo() -> None:
    """
    Establishes a connection pool to MongoDB on startup.
    Configured with production-ready connection pool settings to prevent exhaustion and handle scaling.
    """
    logger.info(f"Connecting to MongoDB at {settings.MONGODB_URI} ...")
    try:
        # Configure connections with robust pool settings using PyMongo's native async client
        mongo_manager.client = AsyncMongoClient(
            settings.MONGODB_URI,
            maxPoolSize=100,  # Max 100 connections in pool
            minPoolSize=10,  # Keep 10 idle connections warm
            maxIdleTimeMS=10000,  # Close idle connections after 10s
            waitQueueTimeoutMS=5000,  # Max wait time for a connection to become available
            serverSelectionTimeoutMS=5000,  # Timeout for cluster health check
        )

        # Test connection by pinging admin database
        await mongo_manager.client.admin.command("ping")
        mongo_manager.db = mongo_manager.client[settings.MONGODB_DB_NAME]
        logger.info("Connected to MongoDB successfully and verified connection pool.")

        # Ensure database schemas/indexes are set up
        await init_db_indexes()
    except Exception as e:
        logger.critical(f"Failed to connect to MongoDB: {e}")
        raise e


async def close_mongo_connection() -> None:
    """
    Gracefully terminates all active connections in the MongoDB pool on shutdown.
    """
    if mongo_manager.client:
        logger.info("Closing MongoDB connection pool...")
        await mongo_manager.client.close()
        logger.info("MongoDB connection pool closed gracefully.")


def get_db():
    """
    Dependency generator that yields the active MongoDB database instance.
    """
    if mongo_manager.db is None:
        raise RuntimeError("Database connection not initialized.")
    return mongo_manager.db


async def init_db_indexes() -> None:
    """
    Ensures optimal query performance by provisioning database indexes on startup.
    """
    try:
        db = mongo_manager.db

        # Users Collection: Enforce unique emails for queries and credentials
        await db.users.create_index("email", unique=True)

        # Documents Collection: Optimize listing documents per user sorted by date
        await db.documents.create_index([("user_id", 1), ("created_at", -1)])

        logger.info("Database indexes initialized successfully.")
    except Exception as e:
        logger.exception(f"Error initializing database indexes: {e}")
