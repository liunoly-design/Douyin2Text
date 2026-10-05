"""Runtime locations are shared by API, pipeline and artifact storage."""
import json
import os
from pathlib import Path

from .providers.registry import effective_config

DATA = Path(os.environ.get("DOUYIN2TEXT_DATA_DIR", "/Users/mac/Documents/Personal-Wiki-Media")).expanduser().resolve()
CONFIG_PATH = Path(os.environ.get("DOUYIN2TEXT_CONFIG", str(DATA / "state/config.json")))


def load_config():
    return effective_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
