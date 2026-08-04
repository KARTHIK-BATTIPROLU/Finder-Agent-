"""
main.py - Entry Point for the Autonomous Allotment Extraction System.

Initializes MongoDB connections, constructs the LangGraph state machine,
and continuously iterates over target Telangana entrance exam portals with automatic
retry from MongoDB checkpoints until target college (WITS) is fully completed.
"""

import asyncio
import logging
import os
import sys
from typing import Dict, List

from dotenv import load_dotenv

from db import close_db, get_db
from graph import ScraperState, build_scraper_graph, cleanup_active_scraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("AllotmentExtractionSystem")

TARGET_CONFIGS: List[Dict[str, str]] = [
    {
        "exam_name": "TG_ECET",
        "url": "https://tgecet.nic.in/college_allotment.aspx"
    },
    {
        "exam_name": "TG_POLYCET",
        "url": "https://tgpolycet.nic.in/college_allotment.aspx"
    }
]


async def run_allotment_extraction():
    """Main execution routine orchestrating extraction across target URLs with resilient retries."""
    logger.info("==========================================================")
    logger.info("   Starting Autonomous Allotment Extraction System       ")
    logger.info("==========================================================")

    try:
        await get_db()
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        logger.error("Please ensure MongoDB is running or check your MONGO_URI in .env")
        return

    app_graph = build_scraper_graph()
    output_dir = os.getenv("OUTPUT_DIR", "outputs")
    stop_code = os.getenv("STOP_AT_COLLEGE_CODE", "WITS")

    try:
        for config in TARGET_CONFIGS:
            exam_name = config["exam_name"]
            url = config["url"]

            logger.info(f"\n>>> Starting Processing for Exam Portal: {exam_name} ({url}) <<<")
            logger.info(f"Target College Stop Code: '{stop_code}'")

            initial_state: ScraperState = {
                "url": url,
                "exam_name": exam_name,
                "college_index": 0,
                "branch_index": 0,
                "colleges": [],
                "branches": [],
                "completed": False,
                "error": None,
                "output_dir": output_dir,
                "stop_at_college_code": stop_code
            }

            # Resilient retry loop from MongoDB checkpoints
            retry_count = 0
            max_retries = 50

            while retry_count < max_retries:
                final_state = await app_graph.ainvoke(initial_state)

                if final_state.get("completed"):
                    logger.info(f"SUCCESS: Completed extraction for {exam_name} up to '{stop_code}'.")
                    break

                if final_state.get("error"):
                    retry_count += 1
                    logger.warning(
                        f"Network timeout/error encountered for {exam_name}: {final_state['error']}. "
                        f"Auto-resuming attempt {retry_count}/{max_retries} from MongoDB checkpoint in 3s..."
                    )
                    await cleanup_active_scraper()
                    await asyncio.sleep(3.0)
                else:
                    break

    except KeyboardInterrupt:
        logger.warning("Execution interrupted by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception during extraction execution: {e}", exc_info=True)
    finally:
        logger.info("Cleaning up browser and database resources...")
        await cleanup_active_scraper()
        await close_db()
        logger.info("Extraction System Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(run_allotment_extraction())
