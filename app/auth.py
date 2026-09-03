from typing import Optional

from fastapi import Depends, Request
from passlib.context import CryptContext
from sqlmodel import Session

from database import get_session
from models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return session.get(User, user_id)