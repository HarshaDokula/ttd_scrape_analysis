import argparse
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from provider_factory import get_provider


def _format_ts(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    try:
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    except Exception:
        return str(ts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "List OpenAI batch jobs created by this project using the /v1/batches API."
        )
    )
    parser.add_argument(
        "--status",
        type=str,
        default=None,
        help=(
            "Optional status filter (e.g. validating, in_progress, completed, "
            "failed, expired, cancelling, cancelled)."
        ),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Number of batches to request per page (limit).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum number of pages to fetch for safety.",
    )

    args = parser.parse_args()

    # Load environment variables from .env (OPENAI_API_KEY, model, etc.)
    load_dotenv()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. Ensure it is in your environment or .env file."
        )

    provider = get_provider()

    batches = provider.list_batches(
        status=args.status,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )

    if not batches:
        print("No batches found.")
        return

    # Header
    print(
        f"{'ID':<32}\t{'STATUS':<12}\t{'CREATED_AT(UTC)':<25}\t{'COMPLETED_AT(UTC)'}"
    )

    for b in batches:
        # if getattr(b,'status') == "failed":
        created_at = _format_ts(getattr(b, "created_at", None))
        completed_at = _format_ts(getattr(b, "completed_at", None))
        print(f"{getattr(b, 'id', '-'):<32}\t{getattr(b, 'status', '-'): <12}\t{created_at:<25}\t{completed_at}")
        
        # print(b)



    

if __name__ == "__main__":  # pragma: no cover
    main()
