"""
Auth — простая whitelist авторизация.

Администратор (ADMIN_CHAT_ID) может:
  /adduser <chat_id>   — добавить пользователя
  /removeuser <chat_id> — убрать пользователя
  /users               — список всех пользователей

Обычный пользователь:
  /start — запрос доступа (бот сообщает админу)
"""
import logging
from functools import wraps
from typing import Optional

from aiogram.types import Message

logger = logging.getLogger(__name__)


class Auth:
    def __init__(self, db, admin_chat_id: int):
        self.db = db
        self.admin_chat_id = admin_chat_id

    def is_admin(self, chat_id: int) -> bool:
        return chat_id == self.admin_chat_id

    async def is_allowed(self, chat_id: int) -> bool:
        """Проверяет: пользователь в whitelist или является админом."""
        if self.is_admin(chat_id):
            return True
        return await self.db.is_subscriber(chat_id)

    async def add_user(self, chat_id: int, added_by: int) -> bool:
        if await self.db.is_subscriber(chat_id):
            return False  # уже есть
        await self.db.add_subscriber(chat_id, approved=True)
        logger.info(f"User {chat_id} approved by {added_by}")
        return True

    async def remove_user(self, chat_id: int) -> bool:
        return await self.db.remove_subscriber(chat_id)

    async def get_all_users(self) -> list:
        return await self.db.get_subscribers()


def require_auth(auth: Auth):
    """
    Декоратор для хендлеров aiogram.
    Использование:
        @dp.message(Command("status"))
        @require_auth(auth)
        async def cmd_status(message: Message): ...
    """
    def decorator(handler):
        @wraps(handler)
        async def wrapper(message: Message, *args, **kwargs):
            chat_id = message.chat.id
            if not await auth.is_allowed(chat_id):
                await message.answer(
                    "🔒 <b>Доступ закрыт</b>\n\n"
                    "У вас нет доступа к этому боту.\n"
                    "Отправьте /request чтобы запросить доступ.\n\n"
                    "🤖 <b>MEXC Pump &amp; Dump Scanner</b>\n\n",
                    # описание фич бота
                    parse_mode="HTML"
                )
                logger.warning(f"Unauthorized access attempt from {chat_id}")
                return
            return await handler(message, *args, **kwargs)
        return wrapper
    return decorator
