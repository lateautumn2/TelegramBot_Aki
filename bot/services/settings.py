from dataclasses import dataclass, field, fields


@dataclass
class BotSettings:
    """Runtime settings editable by admins; persisted in the `settings` table."""

    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    max_tokens: int | None = None
    history_limit: int = 20
    context_token_budget: int = 12000
    session_timeout_min: int = 60
    stream: bool = True
    request_timeout_s: int = 120
    current_model_id: int | None = None

    # "whitelist": only listed users/groups; "open": anyone may chat.
    access_mode: str = "whitelist"
    # Let whitelisted users talk to the bot in groups that are not whitelisted.
    allow_whitelisted_user_in_any_group: bool = False
    # Leave groups the bot is added to by non-admins.
    leave_unauthorized_groups: bool = False
    # How group members address the bot: "mention", "reply" (to the bot), "command" (/ask).
    group_triggers: list[str] = field(default_factory=lambda: ["mention", "reply", "command"])

    @classmethod
    def keys(cls) -> set[str]:
        return {f.name for f in fields(cls)}
