"""Entry point: python -m backend [--stop NAME] [--mode train|bus] [--port N] [--host H]
[--show-attribution] [--no-infer-ngr]"""
import argparse
import socket
import sys

import uvicorn

from . import app as app_module
from . import stock


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex((host, port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Queensland rail station / bus stop sign server")
    parser.add_argument("--station", default="Northgate", help="station or stop shown by default (name or id)")
    parser.add_argument(
        "--stop",
        help="stop shown by default: a station or bus stop name, bus stop code or id (overrides --station)",
    )
    parser.add_argument(
        "--mode",
        choices=("train", "bus"),
        help="only look for the default stop among trains or buses (default: either, preferring trains)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument(
        "--show-attribution",
        action="store_true",
        help="show the TransLink data attribution / licence line at the bottom of the sign (hidden by default)",
    )
    parser.add_argument(
        "--no-infer-ngr",
        dest="infer_ngr",
        action="store_false",
        help="do not guess stock from the run code (D = NGR, J = 3 car); only services with "
             "sightings in backend/stock_sightings.csv show a stock icon",
    )
    args = parser.parse_args()
    if _port_in_use(args.host, args.port):
        sys.exit(
            f"Port {args.port} is already in use (is the sign already running?). "
            f"Stop it, or pick another port with --port N."
        )
    app_module.settings["station"] = args.stop or args.station
    app_module.settings["mode"] = args.mode
    app_module.settings["show_attribution"] = args.show_attribution
    stock.USE_RUN_CODE_RULE = args.infer_ngr
    uvicorn.run(app_module.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
