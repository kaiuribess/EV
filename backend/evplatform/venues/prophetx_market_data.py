"""Read-only ProphetX Talaria depth, separate from reviewed execution books.

REST supplies identity and market status. Only a scoped v2 WebSocket selection
message supplies current advertised stake; REST seed prices remain research-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .prophetx import executable_level


class ProphetXMarketData:
    def __init__(self):
        self.catalog: dict[tuple[str, str], dict] = {}
        self.rest_samples: dict[tuple[str, str], list[dict]] = {}
        self.catalog_at: datetime | None = None
        self.books: dict[tuple[str, str, str], dict] = {}
        self.frames = 0
        self.rejected_frames = 0
        self.last_rejection: dict | None = None

    def _reject(self, reason: str, frame: dict):
        data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        scope = frame.get("scope") if isinstance(frame.get("scope"), dict) else {}
        market = self.catalog.get((str(data.get("sport_event_id")), str(data.get("market_id"))))
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        groups = info.get("selections")
        first = next((row for group in groups if isinstance(group, list)
                      for row in group if isinstance(row, dict)), {}) if isinstance(groups, list) else {}
        self.rejected_frames += 1
        self.last_rejection = {"reason": reason, "event_id": str(data.get("sport_event_id")),
            "market_id": str(data.get("market_id")), "scope_event_id": str(scope.get("event_id")),
            "scope_sub_type": scope.get("sub_type"), "catalog_match": market is not None,
            "catalog_sub_type": market.get("sub_type") if market else None,
            "catalog_strike_count": len(market["strikes"]) if market else None,
            "info_fields": sorted(info), "stream_market_status": info.get("status"),
            "first_selection_fields": sorted(first),
            "first_strike_prefix": str(first.get("strike_id", ""))[:8]}

    def replace_catalog(self, responses: list[dict], event_ids: list[str], *, observed_at: datetime):
        """Install one complete, event-keyed REST discovery round atomically."""
        if observed_at.tzinfo is None:
            raise ValueError("Catalog observation needs an aware clock")
        catalog = {}
        rest_samples = {}
        seen = set()
        for response in responses:
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                raise ValueError("Invalid ProphetX market catalog")
            for event_id, markets in data.items():
                if event_id in seen or event_id not in event_ids or not isinstance(markets, list):
                    raise ValueError("Incomplete ProphetX market catalog")
                seen.add(event_id)
                for market in markets:
                    if not isinstance(market, dict) or market.get("id") is None:
                        raise ValueError("Invalid ProphetX market identity")
                    key = (event_id, str(market["id"]))
                    if key in catalog:
                        raise ValueError("Duplicate ProphetX market identity")
                    identities = {}
                    rest_levels = []
                    groups = market.get("selections")
                    if isinstance(groups, list):
                        groups = groups if all(isinstance(group, list) for group in groups) else [groups]
                        for group in groups:
                            for row in group:
                                if not isinstance(row, dict) or not row.get("strike_id") or row.get("outcome_id") is None:
                                    continue
                                strike = str(row["strike_id"])
                                outcome = str(row["outcome_id"])
                                if strike in identities and identities[strike]["outcome_id"] != outcome:
                                    raise ValueError("Conflicting ProphetX strike identity")
                                identities[strike] = {"outcome_id": outcome, "name": str(row.get("name") or "")[:120],
                                                      "status": row.get("status") or "unknown"}
                                if len(rest_levels) < 20:
                                    try:
                                        parsed = executable_level(row)
                                        if parsed["executable_stake_usd"] > 0:
                                            rest_levels.append({"strike_id": strike, "outcome_id": outcome,
                                                "selection_name": identities[strike]["name"],
                                                "american_price": str(parsed["american_price"]),
                                                "rest_advertised_stake_usd": str(parsed["executable_stake_usd"]),
                                                "gross_payout_per_usd_stake": str(parsed["gross_payout_per_usd_stake"]),
                                                "updated_at_raw": parsed["source_observed_at"]})
                                    except (ValueError, KeyError, TypeError, ArithmeticError):
                                        pass  # malformed REST level is not a live stream book
                    catalog[key] = {"name": str(market.get("name") or "")[:120],
                                    "type": market.get("type"), "period": market.get("period"),
                                    "status": market.get("status"), "sub_type": market.get("sub_type"),
                                    "player_id": market.get("player_id"),
                                    "statistic": market.get("statistic"), "line": market.get("line"),
                                    "strikes": identities}
                    if rest_levels:
                        rest_samples[key] = rest_levels
        if seen != set(event_ids):
            raise ValueError("Incomplete ProphetX event market catalog")
        # Changed/withdrawn identities cannot inherit earlier stream liquidity.
        self.books = {key: book for key, book in self.books.items()
                      if (key[0], key[1]) in catalog
                      and self.catalog.get((key[0], key[1])) == catalog[(key[0], key[1])]}
        self.catalog, self.rest_samples, self.catalog_at = catalog, rest_samples, observed_at

    def invalidate(self, *, catalog_uncertain: bool = False):
        self.books.clear()
        if catalog_uncertain:
            self.catalog_at = None

    def apply(self, frame: dict, *, received_at: datetime):
        """Replace only the selection subject named by a verified channel frame."""
        if frame.get("event") in {"market", "v2.market", "market_strike", "v2.market_strike",
                                  "sport_event", "v2.sport_event"}:
            self.invalidate(catalog_uncertain=True)
            return
        if frame.get("event") != "v2.market_selections":
            return
        self.frames += 1
        data = frame.get("data")
        scope = frame.get("scope")
        if not isinstance(data, dict) or not isinstance(scope, dict):
            self._reject("missing_data_or_scope", frame)
            return
        event_id, market_id = str(data.get("sport_event_id")), str(data.get("market_id"))
        key = (event_id, market_id)
        market = self.catalog.get(key)
        info = data.get("info")
        if (str(scope.get("event_id")) != event_id or market is None or not isinstance(info, dict)
                or info.get("id") is None or (scope.get("sub_type") is not None
                    and scope["sub_type"] != market["sub_type"])):
            self._reject("scope_or_catalog_identity", frame)
            self.invalidate(catalog_uncertain=True)
            return
        subject = (event_id, market_id, str(info["id"]))
        if frame.get("envelope", {}).get("op") == "d":
            self.books.pop(subject, None)
            return
        groups = info.get("selections")
        if not isinstance(groups, list) or any(not isinstance(group, list) for group in groups):
            self.books.pop(subject, None)
            self._reject("selection_shape", frame)
            return
        levels = []
        try:
            for group in groups:
                for row in group:
                    if not isinstance(row, dict):
                        raise ValueError("Invalid ProphetX selection")
                    # A zero-value placeholder is not a price level, even if it
                    # carries price=0 or omits price altogether.
                    stake = Decimal(str(row.get("value", 0)))
                    if not stake.is_finite() or stake < 0:
                        raise ValueError("Invalid ProphetX stake")
                    if stake == 0:
                        continue
                    strike = str(row.get("strike_id"))
                    identity = market["strikes"].get(strike)
                    if identity is None:
                        if market["strikes"] or not row.get("strike_id") or row.get("outcome_id") is None:
                            raise ValueError("Unknown ProphetX strike/outcome")
                        # Some V4 market catalogs omit `selections` entirely.
                        # A scoped v2 frame still identifies its strike/outcome,
                        # but this remains research-only stream evidence.
                        identity = {"outcome_id": str(row["outcome_id"]),
                                    "name": str(row.get("name") or "")[:120], "status": "unknown"}
                        identity_evidence = "scoped_v2_stream_only"
                    else:
                        identity_evidence = "v4_rest_and_scoped_v2"
                    if identity["outcome_id"] != str(row.get("outcome_id")):
                        raise ValueError("Conflicting ProphetX strike/outcome")
                    parsed = executable_level(row)
                    levels.append({"strike_id": strike, "outcome_id": identity["outcome_id"],
                                   "selection_name": identity["name"], "selection_status": identity["status"],
                                   "identity_evidence": identity_evidence,
                                   "american_price": str(parsed["american_price"]),
                                   "available_stake_usd": str(parsed["executable_stake_usd"]),
                                   "gross_payout_per_usd_stake": str(parsed["gross_payout_per_usd_stake"]),
                                   "updated_at_raw": parsed["source_observed_at"]})
            if len({(row["strike_id"], row["american_price"]) for row in levels}) != len(levels):
                raise ValueError("Duplicate ProphetX level")
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            self.books.pop(subject, None)
            self._reject(str(exc) if isinstance(exc, ValueError) else "malformed_selection", frame)
            return
        self.books[subject] = {"levels": levels, "received_at": received_at,
                               "sequence": info.get("sequence_number"),
                               "scope_sub_type": scope.get("sub_type"),
                               "stream_market_status": info.get("status")}

    def snapshot(self, *, now: datetime, connected: bool, limit: int = 20,
                 max_receipt_age_seconds: int = 30, max_catalog_age_seconds: int = 600,
                 event_id: str | None = None, market_id: str | None = None,
                 strike_id: str | None = None) -> dict:
        rows = []
        catalog_current = (self.catalog_at is not None and
                           0 <= (now - self.catalog_at).total_seconds() <= max_catalog_age_seconds)
        for (event_id, market_id, subject_id), book in self.books.items():
            market = self.catalog.get((event_id, market_id))
            if market is None:
                continue
            age = (now - book["received_at"]).total_seconds()
            current = (connected and catalog_current and 0 <= age <= max_receipt_age_seconds
                       and market["status"] == "active"
                       and str(book["stream_market_status"]).lower() in {"active", "open"})
            for level in book["levels"]:
                level_current = (current and str(level["selection_status"]).lower() not in {
                    "suspended", "inactive", "closed", "withdrawn", "removed", "settled", "canceled", "cancelled"})
                rows.append({"event_id": event_id, "market_id": market_id, "subject_id": subject_id,
                             "market_name": market["name"], "market_sub_type": market["sub_type"],
                             "channel_scope_sub_type": book["scope_sub_type"],
                             "market_status": market["status"],
                             "stream_market_status": book["stream_market_status"], **level,
                             "local_receipt_at": book["received_at"].astimezone(timezone.utc).isoformat(),
                             "local_receipt_age_seconds": round(age, 3), "stream_current": level_current,
                             "executable_stake_usd": level["available_stake_usd"] if level_current else "0",
                             "research_only": True})
        visible_rows = [row for row in rows
                        if (event_id is None or row["event_id"] == event_id)
                        and (market_id is None or row["market_id"] == market_id)
                        and (strike_id is None or row["strike_id"] == strike_id)]
        visible_rows.sort(key=lambda item: (not item["stream_current"], item["local_receipt_age_seconds"],
                                            item["event_id"], item["market_id"], item["strike_id"]))
        rest_props = [{"event_id": event_id, "market_id": market_id,
                       "market_name": self.catalog[(event_id, market_id)]["name"],
                       "market_sub_type": self.catalog[(event_id, market_id)]["sub_type"],
                       "market_status": self.catalog[(event_id, market_id)]["status"],
                       **level, "stream_current": False, "executable_stake_usd": "0"}
                      for (event_id, market_id), levels in self.rest_samples.items()
                      if str(self.catalog[(event_id, market_id)]["sub_type"]).startswith("player_")
                      for level in levels[:1]]
        return {"catalog_market_count": len(self.catalog), "book_subject_count": len(self.books),
                "market_frame_count": self.frames, "rejected_market_frames": self.rejected_frames,
                "last_rejection": self.last_rejection,
                "catalog_at": self.catalog_at.isoformat() if self.catalog_at else None,
                "catalog_current": catalog_current,
                "rest_player_prop_market_count": len(rest_props),
                "rest_player_prop_samples": rest_props[:5],
                "rest_snapshot_at": self.catalog_at.isoformat() if self.catalog_at else None,
                "rest_price_observation_verified": False,
                "current_game_markets": len({(r["event_id"], r["market_id"]) for r in rows
                                             if r["stream_current"] and r["channel_scope_sub_type"] is None}),
                "current_player_prop_markets": len({(r["event_id"], r["market_id"]) for r in rows
                                                    if r["stream_current"] and r["channel_scope_sub_type"] is not None}),
                "current_player_prop_samples": [r for r in rows
                                                if r["stream_current"] and r["channel_scope_sub_type"] is not None][:5],
                "levels": visible_rows[:limit], "truncated": len(visible_rows) > limit,
                "filtered_level_count": len(visible_rows),
                "read_only": True, "execution_mapping_verified": False}
