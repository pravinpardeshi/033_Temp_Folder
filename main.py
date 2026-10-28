"""Local development launcher.

AgentCore Runtime uses ``runtime_entrypoint.py`` as its entry point.
This file simply launches the exact same app locally on port 8080.
"""

from config import settings
from runtime_entrypoint import app

if __name__ == "__main__":
    app.run(port=settings.APP_PORT, host=settings.APP_HOST)
