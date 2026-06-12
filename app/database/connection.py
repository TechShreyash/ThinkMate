import asyncio
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
        _client = AsyncIOMotorClient(config.MONGODB_URI)
    return _client

def get_db() -> AsyncIOMotorDatabase:
    client = get_db_client()
    return client[config.MONGODB_DB]

@asynccontextmanager
async def db_session():
    """Context manager yielding the active MongoDB database instance."""
    db = get_db()
    yield db

async def init_db():
    """Initializes MongoDB indexes for optimized query performance."""
    db = get_db()
    logger.info("Initializing MongoDB indexes...")
    
    # 1. Compound index on llm_audit_log for queries filtered by user_id and sorted by timestamp
    await db["llm_audit_log"].create_index([("user_id", 1), ("timestamp", -1)])
    
    logger.info("MongoDB indexes initialized successfully.")
