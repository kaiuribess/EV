import argparse
import uvicorn
from dotenv import load_dotenv

from .api import create_app
from .core.config import Settings
from .core.paths import PROJECT_ROOT


def main():
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Local EV platform")
    parser.add_argument("--mode", choices=["demo", "paper", "sandbox", "production"], default=None)
    parser.add_argument("--auto-trade", action="store_true", help="Explicitly enable simulated execution")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = Settings()
    if args.mode:
        settings.mode = args.mode
    if args.port:
        settings.port = args.port
    if args.auto_trade:
        if settings.mode not in {"demo", "paper"}:
            parser.error("--auto-trade is limited to paper execution; native preflight is required")
        settings.auto_trade = True
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning",
                                         ws="websockets", access_log=False))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()

