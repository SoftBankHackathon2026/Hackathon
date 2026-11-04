"""Installed, explicitly versioned migration rules."""
import json
from importlib.resources import files


def rule_data():
    return json.loads(files(__package__).joinpath("data/supported.json").read_text())
