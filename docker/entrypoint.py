"""Create the initial mounted configuration file, then start Recommendarr."""

import os
import shutil
import sys
from pathlib import Path


EXAMPLE_CONFIG = Path("/opt/recommendarr/config.yaml.example")
CONFIG = Path("/app/config.yaml")


def main() -> None:
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, CONFIG)
        print(
            "Created /app/config.yaml from the included example. "
            "Update it with your Plex and TMDb credentials, then restart Recommendarr.",
            flush=True,
        )

    if len(sys.argv) < 2:
        raise RuntimeError("No command was supplied to the container entrypoint.")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
