import argparse
import json
import signal
import sys
from .config import load_config
from .deployer import Deployer
from .util import DeployError


def main():
    parser = argparse.ArgumentParser(description="SQLite-aware single-host deployments")
    parser.add_argument("--config", required=True, help="operator configuration JSON")
    parser.add_argument("action", choices=["check", "deploy", "recover"])
    parser.add_argument("--ref", default="HEAD", help="local Git ref to deploy (no automatic fetch)")
    args = parser.parse_args()
    def cancel(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancel)
    try:
        deployer = Deployer(load_config(args.config))
        result = deployer.recover() if args.action == "recover" else getattr(deployer, args.action)(args.ref)
        print(json.dumps(result))
        return 0
    except DeployError as error:
        print(json.dumps({"error": error.code, "message": str(error)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "CANCELLED", "message": "Deployment interrupted; inspect journal if recovery is required."}), file=sys.stderr)
        return 130
    except Exception as error:
        print(json.dumps({"error": "UNEXPECTED_ERROR", "message": "Deployment failed; inspect private logs and journal."}), file=sys.stderr)
        return 1
