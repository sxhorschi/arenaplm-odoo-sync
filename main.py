#!/usr/bin/env python3
"""Arena PLM → Odoo ERP Sync Tool.

All configuration is done through the web dashboard.
No .env file needed — API keys are entered in Settings.

Usage:
    python main.py              # Start dashboard (default, port 5000)
    python main.py --port 8080  # Custom port
"""

import argparse
import os

def main():
    parser = argparse.ArgumentParser(description="Arena → Odoo Sync Dashboard")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")))
    args = parser.parse_args()

    from app import app
    print()
    print("  Arena -> Odoo Sync Dashboard")
    print(f"  http://localhost:{args.port}")
    print()
    app.run(host="0.0.0.0", port=args.port, debug=True)


if __name__ == "__main__":
    main()
