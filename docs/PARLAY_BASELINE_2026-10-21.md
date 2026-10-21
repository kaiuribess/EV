# Parlay game-board baseline check — October 21, 2026

The authenticated Parlay WebSocket key connected with a `scale` envelope. A
read-only probe used the documented `bookmakers`, `kinds=game`,
`markets=h2h,spreads,totals`, and `limit=1000` filters. No orders were sent and
the probe printed no odds or credentials. Reproduce it with:

```powershell
.\.venv\Scripts\python.exe scripts/probe_parlay_baseline.py --books fanduel,draftkings pinnacle --max-age-s 600
```

At 20:35–20:38 UTC, unfiltered NBA requests for FanDuel, DraftKings, Pinnacle,
their combinations and the application's six-book game scope returned
`snapshot_scope=current_game_board`, `snapshot_complete=true`,
`truncated=false`, and `resume_mode=replace`. The six-book request had 144 rows
across bet365, Caesars, DraftKings, FanDuel and Pinnacle; BetOnline was absent
from that **age-bounded** NBA snapshot. These are connection-time observations,
not a guarantee that every sport, book or future reconnect will behave alike.

An otherwise identical FanDuel/DraftKings NBA request scoped with
`event_id=2026-10-21_Brooklyn_Nets_Charlotte_Hornets` returned four rows and
`snapshot_scope=recent_rows`, `snapshot_complete=false`, `resume_mode=merge`,
despite `truncated=false`. Do not treat an event-filtered frame as a complete
replacement. The live game-board adapter intentionally starts unfiltered and
can record a complete baseline only when the frame carries all three required
flags. A source may still deliver a partial frame or fail a snapshot on another
connection; that must invalidate the scope.

Parlay's [WebSocket reference](https://parlay-api.com/docs/websocket) describes
this explicit current-game-board request. Its [main API reference](https://api.parlay-api.com/docs)
also cautions that DraftKings/FanDuel legacy views may be recent-row samples,
with no guaranteed per-outcome withdrawal or current-status verification. The
observed complete frame is strong **scope replacement** evidence for this
specific connection, but does not certify every selection as active upstream.
`last_update` and price-change age must not be relabeled as a current active
quote simply because a heartbeat or snapshot arrived. Player props are outside
this full-game baseline scope.

The running paper feed may consume these complete game frames for hypothetical
offers, which remain labeled as such. Automatic real execution still requires
exact settlement matches, quote-bound status/freshness evidence, reviewed
mappings, a verified execution account and native order-to-settlement evidence.
