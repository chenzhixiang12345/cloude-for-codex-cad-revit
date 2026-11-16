from functools import lru_cache
from importlib.resources import files

import yaml


@lru_cache
def defaults():
    return yaml.safe_load(files(__package__).joinpath("default.yaml").read_text(encoding="utf-8"))
