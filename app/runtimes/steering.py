"""The owner's comments to a coding task while it works, by kind.

Stored as run controls (`RunRepository.pending_build_notes`) and passed to the
coder on the owner's Mac (`app/coder/handoff.py`): a comment is the coder's next
turn, "now" interrupts the current turn. Pause and Continue were for the coding
agent the office once ran itself; the Mac's coder answers them as not available.
"""

NOTE = "build_note"
NOW = "build_now"
PAUSE = "build_pause"
CONTINUE = "build_continue"
KINDS = (NOTE, NOW, PAUSE, CONTINUE)
