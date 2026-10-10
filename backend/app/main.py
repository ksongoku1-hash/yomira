import sys

if sys.argv[1:2] == ["--wpd-worker"]:
    from app.services.wpd_worker import run_worker

    raise SystemExit(run_worker())

import uvicorn

from app.factory import create_app
from app.settings import Settings, load_settings

settings: Settings = load_settings()
app = create_app(settings)


def main() -> None:
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
