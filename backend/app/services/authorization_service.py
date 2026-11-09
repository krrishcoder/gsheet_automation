"""Who may do what.

* ADMIN: everything, including submitting reports for any loader.
* USER: submits reports only for loaders explicitly granted with /grant_loader.
* Administrators listed in ADMIN_TELEGRAM_IDS are (re)created as active admins at startup.
* Reports are accepted in private chats, and in group chats only when group submissions are
  enabled and the group was trusted with /trust_group. Per-loader rights apply everywhere.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import Loader, User, UserRole
from app.database.repositories import TrustedChatRepository, UserRepository, audit
from app.schemas.telegram import TgChat, TgUser


class AuthorizationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def bootstrap_admins(self, session: Session) -> None:
        users = UserRepository(session)
        for telegram_id in self.settings.admin_ids:
            user, created = users.get_or_create(telegram_id, role=UserRole.ADMIN)
            if created or user.role != UserRole.ADMIN or not user.is_active:
                user.role = UserRole.ADMIN
                user.is_active = True
                audit(session, "admin_bootstrapped", actor=None, entity_type="user", entity_id=telegram_id)

    def resolve_user(self, session: Session, tg_user: TgUser | None) -> User | None:
        """Return the active registered user (refreshing their display details), else None."""
        if tg_user is None or tg_user.is_bot:
            return None
        user = UserRepository(session).get_by_telegram_id(tg_user.id)
        if user is None or not user.is_active:
            return None
        if tg_user.username != user.username or (tg_user.full_name or None) != user.full_name:
            user.username = tg_user.username
            user.full_name = tg_user.full_name or None
        return user

    def is_admin(self, user: User | None) -> bool:
        if user is None or not user.is_active:
            return False
        return user.role == UserRole.ADMIN or user.telegram_user_id in self.settings.admin_ids

    def can_submit_for(self, session: Session, user: User | None, loader: Loader) -> bool:
        if user is None or not user.is_active:
            return False
        if self.is_admin(user):
            return True
        return UserRepository(session).has_loader(user, loader.id)

    def chat_accepts_reports(self, session: Session, chat: TgChat) -> bool:
        if chat.is_private:
            return True
        if chat.is_group and self.settings.allow_group_submissions:
            return TrustedChatRepository(session).is_trusted(chat.id)
        return False
