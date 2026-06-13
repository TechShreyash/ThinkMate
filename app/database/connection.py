"""MongoDB client lifecycle: a lazily-created async client singleton, a session context
manager for handler injection, a connectivity probe, and index initialization.
"""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from contextlib import asynccontextmanager
from loguru import logger
from app.config import config

# Global client singleton
_client: AsyncIOMotorClient | None = None


def get_db_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        logger.info("Initializing AsyncIOMotorClient...")
        _client = AsyncIOMotorClient(config.MONGODB_URI, serverSelectionTimeoutMS=10000)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_db_client()[config.MONGODB_DB]


@asynccontextmanager
async def db_session():
    """Context manager yielding the active MongoDB database instance."""
    yield get_db()


async def ping_db():
    """Verify connectivity to MongoDB, raising promptly if the server is unreachable."""
    await get_db_client().admin.command("ping")
    logger.info("MongoDB connection verified.")


async def init_db():
    """Create indexes used for query performance and audit-log retention."""
    db = get_db()
    logger.info("Initializing MongoDB indexes...")

    # Compound index: audit queries filtered by user_id and sorted by timestamp.
    await db["llm_audit_log"].create_index([("user_id", 1), ("timestamp", -1)])

    # TTL index: auto-expire audit entries after the configured retention window.
    # Wrapped defensively so an unsupported backend (e.g. mongomock) can't block startup.
    try:
        await db["llm_audit_log"].create_index(
            [("timestamp", 1)],
            expireAfterSeconds=config.AUDIT_LOG_RETENTION_DAYS * 86400,
            name="audit_ttl",
        )
    except Exception as e:  # noqa: BLE001 - retention is best-effort, never fatal
        logger.warning(f"Could not create audit-log TTL index: {e}")

    logger.info("MongoDB indexes initialized successfully.")
