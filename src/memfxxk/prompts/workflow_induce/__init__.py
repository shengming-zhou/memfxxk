from importlib.resources import files

from memfxxk.prompts.lesson_distill import SCHEMA as SCHEMA

VERSION = "awm-workflows-v1"
PROMPT = files(__package__).joinpath("v1.md").read_text(encoding="utf-8")
