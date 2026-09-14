"""Reading and writing Dominions 6 turn orders.

This is the write path — the part that lets the assistant actually play rather
than only observe. Everything here is derived from controlled experiments
against a live game; see knowledge/2h_format.md for the diffs.

The single most important finding: **the game does not validate the .2h
trailer on load.** A file edited in place, with its stale checksum left alone,
loaded and showed the edited order. So orders can be written by patching fields
and nothing has to be recomputed.

Both halves of that are now confirmed. An edited file with a stale trailer was
written setting Estorgant to sneak to Kratas; the player reloaded to check the
client showed it, then ended the turn unchanged. The order resolved — the
Troubadour's record sits at province 91, home 93, in the turn 2 and turn 3 files.
So the host accepts these files too, and the trailer genuinely never needs
recomputing.

Orders are not written directly. Tools record **intent** to the database and
`materialize` rebuilds the file from a pristine base, so the `.2h` is a pure
function of the intent rows rather than an accumulation of edits. See
materialize.py for why that is worth the indirection.

Because a wrong order produces no error — just a turn that quietly does
something else — every write goes through `verify_roundtrip`, and anything not
confirmed by experiment raises rather than guesses.
"""
from __future__ import annotations

# NOTE: `materialize` the function is deliberately NOT re-exported here. This
# package contains a module of the same name, and binding the function to that
# name in the package namespace shadows it — `from ... import materialize as M`
# then yields the function, and every M.record_order call fails with a confusing
# AttributeError on a 'function' object. Import the module directly instead:
#
#     from dom6_assistant.orders import materialize
#     materialize.materialize(...)
from .materialize import (          # noqa: F401
    MaterializeResult,
    current_intent,
    record_order,
    reset_base,
)
from .orders_2h import (            # noqa: F401
    ORDER_CODES,
    Order,
    OrderTableNotLocated,
    OrdersEditor,
    find_order_blocks,
    read_orders,
)
