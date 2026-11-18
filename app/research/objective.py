"""What the owner actually wants done, separated from how they said it.

An objective arriving from chat carries the conversation around it: a
confirmation prompt, a greeting, a note about where to run it. Left in, those
lines are read as part of the task. One research task ended with the office
concluding "yes, confirmed to start" — it had answered the confirmation
instruction at the end of the objective instead of the research question above
it.

Only trailing instruction lines are removed, and only when something
substantial remains. Nothing here tries to interpret the work itself.
"""

from __future__ import annotations

import re

# Lines that ask the reader to do something about the request rather than
# describing the work. Kept narrow on purpose: a line that merely mentions
# confirming is not removed unless the line is itself the instruction.
INSTRUCTION = re.compile(
    r"^\s*(?:"
    r"(?:please\s+)?(?:reply|respond|answer|confirm|approve)\b[^\n]*"
    r"|(?:请|請)[^\n]*(?:确认|確認|回复|回覆|回復)[^\n]*"
    r"|[^\n]*(?:确认后|確認後)[^\n]*(?:启动|啟動|开始|開始)[^\n]*"
    r"|(?:let me know|tell me)\b[^\n]*"
    r"|(?:thanks|thank you|谢谢|謝謝)[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)
MIN_REMAINING = 12


def strip_instructions(objective: str) -> str:
    """The objective without the trailing lines that talk about the request.

    Returns the objective unchanged when stripping would leave too little to
    work from: a short objective is more likely to be the request itself.
    """
    lines = objective.rstrip().splitlines()
    kept = list(lines)
    while kept and (not kept[-1].strip() or INSTRUCTION.match(kept[-1])):
        kept.pop()
    remaining = "\n".join(kept).strip()
    if len(remaining) < MIN_REMAINING:
        return objective.strip()
    return remaining
