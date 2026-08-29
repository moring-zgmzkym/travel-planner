"""TripMate 启动入口：python run.py → uvicorn (127.0.0.1:8000)。"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from tripmate.gateway.app import main

if __name__ == "__main__":
    main()
