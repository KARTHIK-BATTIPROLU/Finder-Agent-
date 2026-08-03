"""
graph.py - LangGraph Orchestration & Stateful Execution with MongoDB Checkpointing.

Manages the autonomous scraping lifecycle, state transitions, index advancement,
and failure recovery for the Autonomous Allotment Extraction System.
"""

import logging
import os
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from dotenv import load_dotenv

from db import get_checkpoint, save_checkpoint
from scraper import AllotmentScraper, sanitize_filename

load_dotenv()
logger = logging.getLogger(__name__)

# Default Output directory
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "outputs")


class ScraperState(TypedDict):
    """
    LangGraph State Schema tracking autonomous scraper state and checkpoint metadata.
    """
    url: str
    exam_name: str
    college_index: int
    branch_index: int
    colleges: List[Dict[str, str]]
    branches: List[Dict[str, str]]
    completed: bool
    error: Optional[str]
    output_dir: str


# Global scraper instance managed per execution run
_scraper: Optional[AllotmentScraper] = None


async def get_active_scraper() -> AllotmentScraper:
    """Returns or initializes the active scraper instance."""
    global _scraper
    if _scraper is None or _scraper.page is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        timeout_ms = int(os.getenv("BROWSER_TIMEOUT_MS", "30000"))
        _scraper = AllotmentScraper(timeout_ms=timeout_ms, headless=headless)
        await _scraper.start()
    return _scraper


async def cleanup_active_scraper() -> None:
    """Stops and cleans up the active browser session."""
    global _scraper
    if _scraper:
        await _scraper.stop()
        _scraper = None


# Node 1: Initialize State & Resume Checkpoint
async def init_node(state: ScraperState) -> ScraperState:
    """
    Initializes state and fetches existing MongoDB checkpoints to resume progress.
    """
    url = state["url"]
    exam_name = state["exam_name"]
    logger.info(f"--- [NODE: Init] Initializing scraper state for {exam_name} ({url}) ---")

    checkpoint = await get_checkpoint(url)
    if checkpoint:
        college_idx = checkpoint.get("college_index", 0)
        branch_idx = checkpoint.get("branch_index", 0)
        is_completed = checkpoint.get("completed", False)
        logger.info(
            f"Resuming from MongoDB checkpoint: College Index={college_idx}, "
            f"Branch Index={branch_idx}, Completed={is_completed}"
        )
        return {
            **state,
            "college_index": college_idx,
            "branch_index": branch_idx,
            "completed": is_completed,
            "error": None
        }

    logger.info("No existing checkpoint found. Starting from index 0.")
    return {
        **state,
        "college_index": 0,
        "branch_index": 0,
        "completed": False,
        "error": None
    }


# Node 2: Fetch Colleges
async def fetch_colleges_node(state: ScraperState) -> ScraperState:
    """
    Navigates to URL and extracts all college dropdown options.
    """
    if state["completed"]:
        return state

    url = state["url"]
    logger.info(f"--- [NODE: Fetch Colleges] Navigating to {url} ---")
    try:
        scraper = await get_active_scraper()
        await scraper.navigate(url)
        colleges = await scraper.get_college_options()
        
        if not colleges:
            error_msg = f"No college options found on page {url}"
            logger.error(error_msg)
            return {**state, "error": error_msg}

        logger.info(f"Found {len(colleges)} colleges on page.")
        return {**state, "colleges": colleges, "error": None}
    except Exception as e:
        logger.error(f"Error fetching colleges: {e}")
        return {**state, "error": str(e)}


# Node 3: Select College & Fetch Branches
async def select_college_node(state: ScraperState) -> ScraperState:
    """
    Selects the college at `college_index` and extracts its branches.
    """
    if state["completed"] or state.get("error"):
        return state

    college_idx = state["college_index"]
    colleges = state["colleges"]

    if college_idx >= len(colleges):
        logger.info("All colleges processed successfully.")
        await save_checkpoint(
            url=state["url"],
            exam_name=state["exam_name"],
            college_index=college_idx,
            branch_index=0,
            completed=True
        )
        return {**state, "completed": True}

    current_college = colleges[college_idx]
    logger.info(
        f"--- [NODE: Select College] Index [{college_idx}/{len(colleges)-1}]: "
        f"'{current_college['text']}' ---"
    )

    try:
        scraper = await get_active_scraper()
        await scraper.select_college(value=current_college["value"], text=current_college["text"])
        branches = await scraper.get_branch_options()

        if not branches:
            logger.warning(f"No branches found for college '{current_college['text']}'. Skipping to next college.")
            return {
                **state,
                "branches": [],
                "college_index": college_idx + 1,
                "branch_index": 0
            }

        return {**state, "branches": branches, "error": None}
    except Exception as e:
        logger.error(f"Error selecting college {current_college['text']}: {e}")
        return {**state, "error": str(e)}


# Node 4: Scrape & Export PDF for Current Branch
async def scrape_branch_node(state: ScraperState) -> ScraperState:
    """
    Selects current branch, renders allotment table, exports PDF, and updates MongoDB checkpoint.
    """
    if state["completed"] or state.get("error"):
        return state

    college_idx = state["college_index"]
    branch_idx = state["branch_index"]
    colleges = state["colleges"]
    branches = state["branches"]
    exam_name = state["exam_name"]

    if branch_idx >= len(branches):
        return state

    current_college = colleges[college_idx]
    current_branch = branches[branch_idx]

    logger.info(
        f"--- [NODE: Scrape Branch] College [{college_idx}] Branch [{branch_idx}/{len(branches)-1}]: "
        f"'{current_branch['text']}' ---"
    )

    try:
        scraper = await get_active_scraper()
        await scraper.select_branch(value=current_branch["value"], text=current_branch["text"])
        await scraper.trigger_show_allotments()

        # Build output filepath: /outputs/<exam_name>/<college_code>_<branch_code>.pdf
        coll_code = sanitize_filename(current_college["value"] or current_college["text"])
        branch_code = sanitize_filename(current_branch["value"] or current_branch["text"])
        pdf_path = os.path.join(
            state.get("output_dir", OUTPUT_DIR),
            exam_name,
            f"{coll_code}_{branch_code}.pdf"
        )

        await scraper.export_pdf(pdf_path)

        # Update MongoDB checkpoint immediately after successful PDF export
        await save_checkpoint(
            url=state["url"],
            exam_name=exam_name,
            college_index=college_idx,
            branch_index=branch_idx + 1,
            completed=False
        )

        return {**state, "error": None}
    except Exception as e:
        logger.error(f"Error scraping branch '{current_branch['text']}': {e}")
        return {**state, "error": str(e)}


# Node 5: Advance Index
async def advance_index_node(state: ScraperState) -> ScraperState:
    """
    Advances branch_index or resets branch_index and advances college_index.
    """
    if state["completed"] or state.get("error"):
        return state

    college_idx = state["college_index"]
    branch_idx = state["branch_index"] + 1
    branches = state["branches"]
    colleges = state["colleges"]

    if branch_idx >= len(branches):
        # All branches for current college finished; advance college
        next_college_idx = college_idx + 1
        next_branch_idx = 0
        if next_college_idx >= len(colleges):
            logger.info("--- All colleges and branches fully extracted! ---")
            await save_checkpoint(
                url=state["url"],
                exam_name=state["exam_name"],
                college_index=next_college_idx,
                branch_index=0,
                completed=True
            )
            return {
                **state,
                "college_index": next_college_idx,
                "branch_index": 0,
                "completed": True
            }
        
        logger.info(f"Advancing to Next College Index: {next_college_idx}")
        return {
            **state,
            "college_index": next_college_idx,
            "branch_index": next_branch_idx
        }

    return {**state, "branch_index": branch_idx}


# Conditional Edge Router
def route_next_step(state: ScraperState) -> str:
    """
    Routes graph execution based on state flags and branch loop progress.
    """
    if state.get("error"):
        logger.error(f"Graph halting due to error: {state['error']}")
        return "error_handler"

    if state["completed"]:
        logger.info("Graph processing completed.")
        return END

    college_idx = state["college_index"]
    colleges = state.get("colleges", [])

    if colleges and college_idx >= len(colleges):
        return END

    branches = state.get("branches", [])
    branch_idx = state["branch_index"]

    if not branches or branch_idx >= len(branches):
        return "select_college"

    return "scrape_branch"


# Error Handler Node
async def error_handler_node(state: ScraperState) -> ScraperState:
    """
    Gracefully logs failure details so state machine can be cleanly re-run from MongoDB checkpoint.
    """
    logger.critical(
        f"Execution failed at College[{state['college_index']}], Branch[{state['branch_index']}]: "
        f"{state.get('error')}. MongoDB checkpoint preserved for instant resume."
    )
    return state


def build_scraper_graph() -> StateGraph:
    """
    Constructs and compiles the LangGraph state machine.
    """
    workflow = StateGraph(ScraperState)

    # Add Nodes
    workflow.add_node("init", init_node)
    workflow.add_node("fetch_colleges", fetch_colleges_node)
    workflow.add_node("select_college", select_college_node)
    workflow.add_node("scrape_branch", scrape_branch_node)
    workflow.add_node("advance_index", advance_index_node)
    workflow.add_node("error_handler", error_handler_node)

    # Define Graph Flow Edges
    workflow.set_entry_point("init")
    workflow.add_edge("init", "fetch_colleges")
    workflow.add_edge("fetch_colleges", "select_college")

    workflow.add_conditional_edges(
        "select_college",
        route_next_step,
        {
            "select_college": "select_college",
            "scrape_branch": "scrape_branch",
            "error_handler": "error_handler",
            END: END
        }
    )

    workflow.add_edge("scrape_branch", "advance_index")

    workflow.add_conditional_edges(
        "advance_index",
        route_next_step,
        {
            "select_college": "select_college",
            "scrape_branch": "scrape_branch",
            "error_handler": "error_handler",
            END: END
        }
    )

    workflow.add_edge("error_handler", END)

    return workflow.compile()
