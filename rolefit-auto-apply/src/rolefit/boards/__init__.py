"""Built-in job-board adapters.

Importing this package registers every concrete board. Application code should
depend on :class:`rolefit.boards.base.BaseJobBoard` and resolve a name with
:func:`rolefit.boards.base.get_board`.
"""

from rolefit.boards.base import (
    ApplyContext,
    ApplyResult,
    BaseJobBoard,
    SearchQuery,
    available_boards,
    get_board,
    register_board,
)
from rolefit.boards.greenhouse import GreenhouseBoard

__all__ = [
    "ApplyContext",
    "ApplyResult",
    "BaseJobBoard",
    "GreenhouseBoard",
    "SearchQuery",
    "available_boards",
    "get_board",
    "register_board",
]
