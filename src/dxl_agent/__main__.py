from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("dxl_agent.api:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
