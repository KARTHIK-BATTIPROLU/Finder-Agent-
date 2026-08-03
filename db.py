"""
db.py - Asynchronous MongoDB Checkpoint Persistence Layer using Motor.

Provides checkpoint CRUD operations to track scraping progress for government allotment portals.
Supports resuming execution from exact (college_index, branch_index) checkpoints.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

load_dotenv()

# Logger setup
logger = logging.getLogger(__name__)

# Default MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "allotment_scraper_db")
MONGO_COLLECTION_NAME = os.getenv("MONGO_COLLECTION_NAME", "scraper_checkpoints")

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


async def get_db() -> AsyncIOMotorDatabase:
    """Retrieves or initializes the async Motor database instance."""
    global _client, _db
    if _db is None:
        logger.info(f"Connecting to MongoDB at: {MONGO_URI}")
        _client = AsyncIOMotorClient(MONGO_URI)
        _db = _client[MONGO_DB_NAME]
        
        # Ensure unique index on URL for fast checkpoint lookup
        collection = _db[MONGO_COLLECTION_NAME]
        await collection.create_index("url", unique=True)
        logger.info("MongoDB connection established and index ensured.")
    return _db


async def close_db() -> None:
    """Closes the MongoDB connection."""
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB connection closed.")


async def get_checkpoint(url: str) -> Optional[Dict[str, Any]]:
    """
    Fetches an existing checkpoint document for the given target URL.

    Args:
        url: Target allotment portal URL.

    Returns:
        Checkpoint dictionary if found, otherwise None.
    """
    db = await get_db()
    collection = db[MONGO_COLLECTION_NAME]
    try:
        checkpoint = await collection.find_one({"url": url})
        if checkpoint:
            logger.info(
                f"Checkpoint loaded for {url}: college_idx={checkpoint.get('college_index')}, "
                f"branch_idx={checkpoint.get('branch_index')}, completed={checkpoint.get('completed')}"
            )
        else:
            logger.info(f"No existing checkpoint found for {url}. Starting fresh.")
        return checkpoint
    except Exception as e:
        logger.error(f"Error reading checkpoint for {url} from MongoDB: {e}")
        return None


async def save_checkpoint(
    url: str,
    exam_name: str,
    college_index: int,
    branch_index: int,
    completed: bool = False
) -> bool:
    """
    Upserts the current progress checkpoint in MongoDB.

    Args:
        url: Target allotment portal URL.
        exam_name: Human-readable exam identifier (e.g. TG_ECET).
        college_index: Index of the current college option.
        branch_index: Index of the current branch option.
        completed: Flag indicating if all colleges and branches are fully processed.

    Returns:
        True if save succeeded, False otherwise.
    """
    db = await get_db()
    collection = db[MONGO_COLLECTION_NAME]
    payload = {
        "url": url,
        "exam_name": exam_name,
        "college_index": college_index,
        "branch_index": branch_index,
        "completed": completed,
        "updated_at": datetime.now(timezone.utc)
    }

    try:
        await collection.update_one(
            {"url": url},
            {"$set": payload},
            upsert=True
        )
        logger.debug(
            f"Checkpoint saved for {exam_name} ({url}): "
            f"College[{college_index}], Branch[{branch_index}], Completed={completed}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to save checkpoint for {url}: {e}")
        return False


async def reset_checkpoint(url: str) -> bool:
    """
    Resets or removes a checkpoint for a given URL (e.g., to force re-scraping).
    """
    db = await get_db()
    collection = db[MONGO_COLLECTION_NAME]
    try:
        result = await collection.delete_one({"url": url})
        logger.info(f"Checkpoint reset for {url}. Deleted count: {result.deleted_count}")
        return True
    except Exception as e:
        logger.error(f"Failed to reset checkpoint for {url}: {e}")
        return False
