from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = str(os.getenv("CLAWBOT_HOST", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("CLAWBOT_PORT", "18080") or "18080")
    except ValueError:
        port = 18080
    uvicorn.run("kefu_clawbot.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
