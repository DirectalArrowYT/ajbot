"""Character-select grid geometry and cursor routing.

Layout is derived from DT_VsCharSelect's file order (see ajbot/roster.py) and
was verified cell-by-cell against the live screen.

Geometry, as measured in game:
  * 12 rows x 6 columns, only 3 rows visible at a time (it scrolls).
  * Vertical movement wraps: 12 DOWN presses return to the start row.
  * Horizontal movement wraps through SEVEN positions, not six - the Random
    cell sits on the inside edge between the 1P and 2P panels and is part of
    every row's horizontal cycle.
  * For the 2P panel the cycle is [RANDOM, c0, c1, c2, c3, c4, c5]; for 1P the
    Random cell is on the other side, so the cycle is mirrored.
  * The last row holds only five characters. Its sixth slot is not a cursor
    position: moving RIGHT onto it lands on Random instead, and moving DOWN
    into it lands on the fifth column. That column shift sticks, so a vertical
    move that merely PASSES THROUGH the last row while in column 5 comes out
    in column 4.
"""
from .roster import DISPLAY_ORDER, index_of

ROWS = 12
COLS = 6
RANDOM = "RANDOM"
LAST_ROW = ROWS - 1
LAST_ROW_COLS = len(DISPLAY_ORDER) - LAST_ROW * COLS   # 5, not 6


def cell(row, col):
    i = row * COLS + col
    return DISPLAY_ORDER[i] if i < len(DISPLAY_ORDER) else "EMPTY"


def position_of(character_id):
    """(row, col) of a character in the grid."""
    return divmod(index_of(character_id), COLS)


def cols_in_row(row):
    """Selectable columns in a row - the last row is one short."""
    return LAST_ROW_COLS if row == LAST_ROW else COLS


def hslots(row):
    """Horizontal cursor stops in a row: its characters plus the Random cell."""
    return cols_in_row(row) + 1


def _hslot(col, side="2P"):
    """Map a column to its horizontal cursor slot, accounting for Random."""
    if side == "2P":
        return col + 1        # RANDOM occupies slot 0
    return col                # 1P: RANDOM sits after the last column


def _rows_visited(row, drows):
    step = 1 if drows > 0 else -1
    return [(row + step * i) % ROWS for i in range(1, abs(drows) + 1)]


def column_after_vertical(col, row, drows):
    """Column you actually end up in after `drows` presses.

    Short rows clamp the cursor inward, and the clamp is not undone by
    continuing past them.
    """
    for r in _rows_visited(row, drows):
        col = min(col, cols_in_row(r) - 1)
    return col


def _wrapped(delta, size):
    """Signed step count, taking the wrapped direction when it is shorter."""
    if delta > size / 2:
        return delta - size
    if delta < -size / 2:
        return delta + size
    return delta


def _vertical(frm_row, to_row):
    """Signed row presses, never wrapping UPWARD past the top row.

    Downward wrapping is verified (12 DOWN presses return to the start row).
    Upward wrapping is not: a P1 pick of ShigarakiFinal / AllForOne / Muscular
    routed from the top row as "up x5" through the wrap and every character
    landed exactly one row low (Twice / Compress / Deku Full Cowl) - the wrap
    upward costs an extra press. So an upward move is only taken when it stays
    on the grid; otherwise the cursor goes the long way down.
    """
    dr = to_row - frm_row
    down = dr % ROWS                      # 0..11 presses down (may wrap)
    if dr < 0 and -dr < down:             # straight up, no wrap: shorter
        return dr
    return down


def route(frm, to, side="2P"):
    """(d_rows, d_cols) signed presses from `frm` to `to`, vertical applied first.

    Callers must move vertically before horizontally - the horizontal leg is
    measured from wherever the vertical leg actually leaves the cursor.
    """
    dr = _vertical(frm[0], to[0])
    landed = column_after_vertical(frm[1], frm[0], dr)
    delta = _hslot(to[1], side) - _hslot(landed, side)
    if side == "1P":
        # Player 1's row has one more stop past the last fighter than this
        # model knows about (Random is SEVEN steps right of the first
        # fighter, confirmed in game, not six) - so never wrap sideways on
        # 1P; the direct route is at most five presses.
        dc = delta
    else:
        dc = _wrapped(delta, hslots(to[0]))
    return dr, dc


def describe(frm, to, side="2P"):
    dr, dc = route(frm, to, side)
    v = f"{'down' if dr > 0 else 'up'} x{abs(dr)}" if dr else "-"
    h = f"{'right' if dc > 0 else 'left'} x{abs(dc)}" if dc else "-"
    return f"{v}, {h}"
