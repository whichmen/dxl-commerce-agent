from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = str(os.getenv("IDENTITY_HOST", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(os.getenv("IDENTITY_PORT", "18082") or "18082")
    except ValueError:
        port = 18082
    uvicorn.run("kefu_identity_service.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
