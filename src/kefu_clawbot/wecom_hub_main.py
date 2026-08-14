from __future__ import annotations

import os

import uvicorn

from .wecom_hub import create_wecom_hub_app


def main() -> None:
    host = str(os.getenv("CLAWBOT_WECOM_HUB_HOST", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("CLAWBOT_WECOM_HUB_PORT", "18081") or "18081")
    except ValueError:
        port = 18081
    uvicorn.run("kefu_clawbot.wecom_hub_main:app", host=host, port=port)

app = create_wecom_hub_app()
