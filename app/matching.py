from typing import Optional

from rapidfuzz import fuzz, process
from sqlmodel import select

from models import User

FUZZY_MATCH_THRESHOLD = 85


def match_user(name: str, session) -> Optional[User]:
    statement = select(User).where(User.full_name.ilike(name))
    exact_match = session.exec(statement).first()
    if exact_match:
        return exact_match

    all_users = session.exec(select(User)).all()
    if not all_users:
        return None

    choices = {user.full_name: user for user in all_users}
    result = process.extractOne(name, choices.keys(), scorer=fuzz.token_sort_ratio)

    if result and result[1] >= FUZZY_MATCH_THRESHOLD:
        return choices[result[0]]

    return None