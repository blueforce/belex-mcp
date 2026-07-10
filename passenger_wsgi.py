"""
passenger_wsgi.py — Einstiegspunkt fuer den gehosteten Betrieb (LiteSpeed/Passenger).

Passenger erwartet ein WSGI-Callable namens `application`. Die eigentliche Logik
steckt in src/http_app.py (schlanker, zustandsloser MCP-Adapter).

MCP-Endpunkt nach dem Deploy:  https://belex.blueforce.ch/mcp
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from http_app import application  # noqa: E402,F401
