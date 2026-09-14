"""Web GUI for the Dominions 6 Assistant.

Start with: dom6-assistant serve
Then open:  http://127.0.0.1:8765
"""

from dom6_assistant.web.app import create_app

__all__ = ["create_app"]
