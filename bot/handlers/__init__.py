from aiogram import Router

from bot.handlers import (
    admin_acl,
    admin_model,
    admin_provider,
    admin_settings,
    admin_status,
    chat,
    common,
    membership,
)


def build_router() -> Router:
    root = Router(name="root")
    # Order matters: commands before the catch-all chat handlers.
    root.include_routers(
        common.build(),
        admin_acl.build(),
        admin_model.build(),
        admin_provider.build(),  # its FSM text steps must come before the chat catch-all
        admin_settings.build(),
        admin_status.build(),
        membership.build(),
        chat.build(),
    )
    return root
