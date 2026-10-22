"""Bounded, read-only Novig v3 stream parser diagnostic.

Subscribes to explicitly supplied market UUIDs and private read channels. It
never submits an order, prints a credential, or persists a frame. Only known
local parser errors are reported; unknown provider values are not echoed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from uuid import UUID

from dotenv import load_dotenv

from evplatform.venues.novig import NovigBook, NovigClient, NovigSettings


KNOWN_PARSER_ERRORS = frozenset({
    "Invalid Novig book row", "Invalid Novig book identifier",
    "Conflicting Novig book identifier aliases", "Invalid Novig snapshot level",
    "Invalid Novig book level", "Unknown Novig book delta",
    "Novig frame must be an object",
})


class ObservedBook(NovigBook):
    def __init__(self):
        super().__init__()
        self.failures: list[str] = []
        self.unknown_delta_shapes: list[dict] = []

    def apply(self, frame):
        try:
            return super().apply(frame)
        except Exception as exc:
            message = str(exc) if type(exc) is ValueError and str(exc) in KNOWN_PARSER_ERRORS else type(exc).__name__
            self.failures.append(message)
            if message == "Unknown Novig book delta" and len(self.unknown_delta_shapes) < 10:
                for market, channels in frame.get("delta", {}).items():
                    for delta in channels.get("book", {}).get("deltas", []):
                        kind = delta.get("kind") if isinstance(delta, dict) else None
                        if kind not in {"add", "remove"}:
                            order = self.books.get(market, {}).get(delta.get("orderId")) if isinstance(delta, dict) else None
                            remaining = delta.get("remaining") if isinstance(delta, dict) else None
                            try:
                                relation = ("unknown_order" if order is None else
                                            "reduced" if int(remaining) < order["qty"] else
                                            "unchanged" if int(remaining) == order["qty"] else "increased")
                            except (TypeError, ValueError, ArithmeticError):
                                relation = "invalid_remaining"
                            self.unknown_delta_shapes.append({
                                "kind": kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_]{1,32}", kind) else "invalid",
                                "fields": sorted(key for key in delta if isinstance(key, str)
                                                 and re.fullmatch(r"[A-Za-z_]{1,40}", key))
                                    if isinstance(delta, dict) else [],
                                "relation": relation,
                            })
                            break
                    if self.unknown_delta_shapes:
                        break
            raise


async def diagnose(market_ids: list[str], seconds: float) -> dict:
    load_dotenv(".env", override=False)
    api_key = os.getenv("NOVIG_PRODUCTION_TRADING_API_KEY")
    private_key_path = os.getenv("NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH")
    if not api_key or not private_key_path:
        return {"ok": False, "reason": "production_credentials_missing"}
    client = NovigClient(NovigSettings(environment="production", api_key=api_key,
                                       private_key_path=private_key_path))
    book = ObservedBook()
    client.book = book
    frames = invalidations = 0
    try:
        await client.connect()
        await client.configure_subscriptions({"market_ids": market_ids})

        async def consume():
            nonlocal frames, invalidations
            async for frame in client.stream_updates():
                if frame.get("type") == "transport_invalidated":
                    invalidations += 1
                else:
                    frames += 1

        try:
            await asyncio.wait_for(consume(), timeout=seconds)
        except TimeoutError:
            pass
        health = client.health_check()
        return {"ok": True, "read_only": True, "observed_seconds": seconds,
                "frames": frames, "invalidations": invalidations,
                "parser_failures": book.failures[:10],
                "unknown_delta_shapes": book.unknown_delta_shapes,
                "last_error_type": health.get("last_error_type"),
                "invalid_book_subjects": health.get("invalid_book_subjects")}
    except Exception as exc:
        return {"ok": False, "reason": "read_only_stream_diagnostic_failed",
                "error_type": type(exc).__name__}
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market_ids", nargs="*", help="Explicit public Novig market UUIDs")
    parser.add_argument("--seconds", type=float, default=20)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or len(args.market_ids) > 5:
        parser.error("Use 1-60 seconds and at most five market IDs")
    for market_id in args.market_ids:
        UUID(market_id)
    print(json.dumps(asyncio.run(diagnose(args.market_ids, args.seconds)), separators=(",", ":")))


if __name__ == "__main__":
    main()
