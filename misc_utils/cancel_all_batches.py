#!/usr/bin/env python3
"""Cancel non-terminal OpenAI batch jobs for the current account.

This script lists batches via the provider and cancels any batch whose
status is not one of the terminal states (completed, failed, expired,
cancelled). By default it runs in dry-run mode and only prints which
batches *would* be cancelled. Pass --yes to actually perform cancellations.

Usage:
  python cancel_all_batches.py        # dry run
  python cancel_all_batches.py --yes  # actually cancel

You can also pass --status to only target batches with a specific status
(e.g. in_progress).

This script uses the same provider factory as the rest of the project and
loads .env so it picks up OPENAI_API_KEY.
"""

import argparse
import time
from dotenv import load_dotenv
from provider_factory import get_provider

TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel non-terminal OpenAI batches")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually cancel batches. Without this flag the script only prints what it would do.",
    )
    parser.add_argument(
        "--status",
        type=str,
        default=None,
        help="Optional status to target (e.g. in_progress). If omitted, targets all non-terminal batches.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size when listing batches.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Max pages to retrieve when listing batches.",
    )

    args = parser.parse_args()

    load_dotenv()
    provider = get_provider()

    # Retrieve batches (paginated)
    print("Listing batches from OpenAI...")
    batches = provider.list_batches(status=args.status, page_size=args.page_size, max_pages=args.max_pages)

    if not batches:
        print("No batches found.")
        return

    to_cancel = []
    for b in batches:
        bid = getattr(b, "id", None) or getattr(b, "batch_id", None)
        status = getattr(b, "status", None)
        if bid is None:
            continue
        if args.status:
            # If user requested a specific status, only pick those
            if status == args.status:
                to_cancel.append((bid, status))
        else:
            if status not in TERMINAL_STATUSES:
                to_cancel.append((bid, status))

    if not to_cancel:
        print("No matching batches to cancel.")
        return

    print(f"Found {len(to_cancel)} batches to cancel (dry-run={not args.yes}):")
    for bid, status in to_cancel:
        print(f"  {bid}\t{status}")

    if not args.yes:
        print('\nDry run complete. Rerun with --yes to cancel the listed batches.')
        return

    print('\nCancelling batches...')
    for bid, status in to_cancel:
        try:
            # The OpenAI SDK exposes a cancel method on batches (client.batches.cancel)
            provider.client.batches.cancel(bid)
            print(f"Cancelled {bid} (was {status})")
        except Exception as exc:
            print(f"Failed to cancel {bid}: {exc}")
        # Gentle pacing to avoid bursts
        time.sleep(0.2)

    print('Done.')


if __name__ == "__main__":
    main()
