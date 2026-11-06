"""Run with:  uv run python -m app   (then open http://127.0.0.1:8000)"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Price Ranker running at http://127.0.0.1:{port}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)
