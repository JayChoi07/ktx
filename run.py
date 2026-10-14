"""실행: uv run python run.py  →  http://localhost:8000"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("ktx.app:app", host="127.0.0.1", port=8000)
