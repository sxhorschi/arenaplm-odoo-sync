#!/usr/bin/env python3
"""Arena PLM → Odoo ERP Sync Tool.

Usage:
    python main.py              # Start dashboard (default, port 5000)
    python main.py --port 8080  # Custom port

Set PRODUCTION=1 to use waitress WSGI server instead of Flask dev server.
"""

import argparse
import os

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Arena -> Odoo Sync Dashboard")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")))
    args = parser.parse_args()

    from app import app

    production = os.getenv("PRODUCTION", "0") == "1"

    print()
    print("  Arena -> Odoo Sync Dashboard")
    print(f"  http://localhost:{args.port}")
    if production:
        print("  Mode: PRODUCTION (waitress)")
    else:
        print("  Mode: DEVELOPMENT (flask debug)")
    print()

    if production:
        from waitress import serve
        serve(app, host="0.0.0.0", port=args.port)
    else:
        app.run(host="127.0.0.1", port=args.port, debug=True)


if __name__ == "__main__":
    main()
