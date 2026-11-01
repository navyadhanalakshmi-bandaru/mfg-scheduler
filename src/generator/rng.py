"""Seeded random streams.

Every generator module asks for its own named stream. All streams derive from
the one challenge seed (20261022), so output is reproducible, and changing one
module never shifts the random numbers used by another.
"""
import random

from config.settings import SEED


def make_rng(stream: str, seed: int = SEED) -> random.Random:
    """Return a deterministic random generator for a named stream."""
    return random.Random(f"{seed}:{stream}")