from collections.abc import Callable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton


def nav_row(page: int, pages: int, make_cb: Callable[[int], CallbackData]) -> list[InlineKeyboardButton]:
    """◀/▶ buttons for a paginated list; empty when there is a single page."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀ 上一页", callback_data=make_cb(page - 1).pack()))
    if page < pages - 1:
        row.append(InlineKeyboardButton(text="下一页 ▶", callback_data=make_cb(page + 1).pack()))
    return row
