"""
ZenLite — Entry Point
Run this to start the AI gateway server.
"""

import uvicorn
from app.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=True,
        log_level="info",
    )
