import sys

if sys.argv[1:2] == ["--wpd-worker"]:
    from app.services.wpd_worker import run_worker

    raise SystemExit(run_worker())

from pathlib import Path
from threading import Thread

import uvicorn

from app.desktop import configure_bundled_motor, interface_is_ready, open_when_ready
from app.factory import create_app
from app.settings import Settings, load_settings

if getattr(sys, "frozen", False):
    configure_bundled_motor(Path(sys.executable))

settings: Settings = load_settings()
app = create_app(settings)


def main() -> None:
    if getattr(sys, "frozen", False):
        origin = f"http://127.0.0.1:{settings.api_port}"
        if interface_is_ready(origin):
            open_when_ready(origin)
            return
        print("Yomira esta iniciando. La pagina se abrira automaticamente.")
        print("Puedes minimizar esta ventana. Para cerrar Yomira, pulsa Ctrl+C.")
        Thread(target=open_when_ready, args=(origin,), daemon=True).start()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
            access_log=False,
        )
    )

    async def request_shutdown() -> None:
        server.should_exit = True

    app.state.request_shutdown = request_shutdown
    server.run()


if __name__ == "__main__":
    main()
