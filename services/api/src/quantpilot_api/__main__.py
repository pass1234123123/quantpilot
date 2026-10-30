"""Run the API dev server: python -m quantpilot_api"""

from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    uvicorn.run(
        "quantpilot_api.app:app",
        host=config.api_host(),
        port=config.api_port(),
        log_level="info",
    )


if __name__ == "__main__":
    main()
