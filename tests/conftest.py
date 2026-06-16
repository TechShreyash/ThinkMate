import pytest
from unittest.mock import patch
import mongomock
from contextlib import asynccontextmanager

# Mock Cursor for simulating motor async cursors
class AsyncMockCursor:
    def __init__(self, cursor):
        self._cursor = cursor
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self._cursor)
        except StopIteration:
            raise StopAsyncIteration

# Mock Collection wrapper to simulate motor's async operations on mongomock
class AsyncMockCollection:
    def __init__(self, collection):
        self._collection = collection

    async def find_one(self, *args, **kwargs):
        return self._collection.find_one(*args, **kwargs)

    async def update_one(self, *args, **kwargs):
        return self._collection.update_one(*args, **kwargs)

    async def find_one_and_update(self, *args, **kwargs):
        return self._collection.find_one_and_update(*args, **kwargs)

    async def insert_one(self, *args, **kwargs):
        return self._collection.insert_one(*args, **kwargs)

    async def delete_one(self, *args, **kwargs):
        return self._collection.delete_one(*args, **kwargs)

    async def delete_many(self, *args, **kwargs):
        return self._collection.delete_many(*args, **kwargs)

    async def create_index(self, keys, *args, **kwargs):
        return self._collection.create_index(keys, *args, **kwargs)

    def find(self, *args, **kwargs):
        cursor = self._collection.find(*args, **kwargs)
        return AsyncMockCursor(cursor)

# Mock Database wrapper
class AsyncMockDatabase:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        return AsyncMockCollection(self._db[name])

    async def drop_collection(self, name):
        self._db.drop_collection(name)

# Mock Client wrapper
class AsyncMockClient:
    def __init__(self):
        self._client = mongomock.MongoClient()

    def __getitem__(self, name):
        return AsyncMockDatabase(self._client[name])

    async def drop_database(self, name):
        self._client.drop_database(name)

# Autouse fixture that intercepts database connections during testing
@pytest.fixture(autouse=True)
def mock_mongodb():
    mock_client = AsyncMockClient()
    mock_db = mock_client["thinkmate_test_db"]
    
    with patch("app.database.connection.get_db_client", return_value=mock_client), \
         patch("app.database.connection.get_db", return_value=mock_db):
        yield mock_db

@pytest.fixture(autouse=True)
def disable_reactions_by_default():
    from app.config import config
    original = config.ENABLE_MESSAGE_REACTIONS
    config.ENABLE_MESSAGE_REACTIONS = False
    yield
    config.ENABLE_MESSAGE_REACTIONS = original


@pytest.fixture(autouse=True)
def setup_test_logs_channel():
    from app.config import config
    original = config.LOGS_CHANNEL_ID
    if config.LOGS_CHANNEL_ID is None:
        config.LOGS_CHANNEL_ID = -1003933328659
    yield
    config.LOGS_CHANNEL_ID = original


@pytest.fixture(autouse=True)
def reset_log_forwarder_state():
    from app.services import log_forwarder
    log_forwarder._buffer = []
    log_forwarder._window_count = 0
    log_forwarder._window_start = 0.0
    yield
    log_forwarder._buffer = []
    log_forwarder._window_count = 0
    log_forwarder._window_start = 0.0


