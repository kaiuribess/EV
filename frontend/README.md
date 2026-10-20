# Aperture local dashboard

React 19, TypeScript, and Vite. No credentials or third-party API clients are included in the frontend. All market data comes from the local backend; an unavailable backend produces an explicit disconnected state.

```powershell
cd frontend
npm ci
npm run build
```

The Python application serves `dist/` at its own local URL. For frontend development:

```powershell
npm run dev
```

Vite runs at `http://127.0.0.1:5173` and proxies `/api` and `/ws` to port 8001. To develop against a different backend:

```powershell
$env:EV_API_TARGET = 'http://127.0.0.1:8002'
npm run dev
```

The selected backend port must be allowed by the backend's local-origin policy.

## Data contract

- `GET /api/state` returns the initial snapshot.
- `/ws` broadcasts `{ "type": "state", "data": ... }`. Disconnects use capped reconnect backoff and HTTP fallback; snapshots older than ten seconds disable controls.
- `GET /api/markets/{id}` returns exact-market quotes, books, history, and canonical settlement metadata.
- Mutations carry `X-EV-Control: local`. Only same-origin local requests should be accepted.
- Plugin cards, supported kinds, and configuration fields derive from backend manifests. Secret/write-only fields are excluded from configuration forms.

Production execution cannot be enabled by the dashboard. Backend configuration and preflight are required. Demo and paper data, order intents, and fills remain labeled as simulated.

## Verification

`npm run build` performs the strict TypeScript check and optimized production build. The app uses system font fallbacks and bundles every dependency required for rendering; it does not fetch remote fonts or image assets.
