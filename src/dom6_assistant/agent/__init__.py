"""The LLM tool surface: what the assistant may know, and what it may do.

Two halves, deliberately separated:

* `visibility` decides what can be seen. Everything the assistant learns about
  the game passes through it.
* `tools` decides what can be done. Every action the assistant takes is a row
  of recorded intent before it is ever a byte in a `.2h`.

Both refuse rather than approximate, which is the same discipline the file
decode was built under and for the same reason: a wrong answer here does not
fail loudly, it produces a turn that quietly did the wrong thing.
"""
from dom6_assistant.agent.visibility import (
    PlayerView, ProvinceView, UnitView, VisibilityError,
)

__all__ = ["PlayerView", "ProvinceView", "UnitView", "VisibilityError"]
