"""Game journal — persistent SQLite log of manually-entered game state.

Each turn the player fills in:
  - Global: treasury, income, upkeep, gem stockpile
  - Per province: pop, income, resources, recruitment, supply, defense,
    unrest, dominion scales
  - Per province (infrequent): sites/buildings list
"""
from dom6_assistant.journal.db import get_db, init_db

__all__ = ["get_db", "init_db"]
