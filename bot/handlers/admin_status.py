import html
import os
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import __version__
from bot.config import Config
from bot.db.repo import Repo
from bot.services.acl import AccessControl, Kind


def app_version() -> str:
    # Docker images built by CI set APP_VERSION from the git tag.
    return os.environ.get("APP_VERSION") or __version__


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} 天" if days else "", f"{hours} 小时" if hours else "", f"{minutes} 分钟"]
    return " ".join(p for p in parts if p)


def _size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _local(utc_naive: datetime) -> datetime:
    return utc_naive.replace(tzinfo=timezone.utc).astimezone()


def today_start_utc() -> datetime:
    """Midnight in the server's timezone, as naive UTC (how timestamps are stored)."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


async def cmd_status(message: Message, repo: Repo, acl: AccessControl, config: Config, started_at: datetime) -> None:
    active = await repo.get_active_model()
    model = f"{html.escape(active.model)}（{html.escape(active.provider_name)}）" if active else "未配置"
    counts = await repo.request_counts(today_start_utc())
    total = sum(counts.values())
    db_file = config.data_dir / "bot.db"
    db_size = _size(db_file.stat().st_size) if db_file.exists() else "—"
    admins = len(acl.entries(Kind.ADMIN)) + len(acl.owner_ids)

    last = await repo.last_error()
    if last is None:
        last_error = "无"
    else:
        when = _local(last.created_at).strftime("%Y-%m-%d %H:%M")
        last_error = f"{when} · {html.escape(last.model)} · {html.escape(last.error)}"

    lines = [
        "📊 <b>运行状态</b>",
        f"版本：{html.escape(app_version())}",
        f"已运行：{_duration((datetime.now() - started_at).total_seconds())}",
        f"当前模型：{model}",
        f"今日请求：{total} 次（成功 {counts.get('ok', 0)}，停止 {counts.get('stopped', 0)}，"
        f"失败 {counts.get('error', 0)}）",
        f"白名单：用户 {len(acl.entries(Kind.USER))} 个，群组 {len(acl.entries(Kind.CHAT))} 个",
        f"管理员：{admins} 个（其中超级管理员 {len(acl.owner_ids)} 个）",
        f"数据库：{db_size}",
        f"最近一次失败：{last_error}",
    ]
    await message.answer("\n".join(lines))


def build() -> Router:
    router = Router(name="admin_status")
    router.message.register(cmd_status, Command("status"), flags={"admin": True, "private_only": True})
    return router
