from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class UserRole(str, Enum):
    member = "member"
    manager = "manager"


class TaskStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str
    email: str = Field(unique=True, index=True)
    hashed_password: str
    teams_user_id: Optional[str] = Field(default=None, index=True)
    role: UserRole = Field(default=UserRole.member)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    tasks: list["Task"] = Relationship(back_populates="assignee")
    meetings_organized: list["Meeting"] = Relationship(back_populates="organizer")


class Meeting(SQLModel, table=True):
    __tablename__ = "meetings"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    meeting_date: datetime
    teams_meeting_id: Optional[str] = Field(default=None, index=True)
    organizer_id: Optional[int] = Field(default=None, foreign_key="users.id")
    transcript_source: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    organizer: Optional[User] = Relationship(back_populates="meetings_organized")
    tasks: list["Task"] = Relationship(back_populates="meeting")


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    meeting_id: Optional[int] = Field(default=None, foreign_key="meetings.id")
    assignee_id: Optional[int] = Field(default=None, foreign_key="users.id")
    raw_assignee_name: str
    description: str
    context_quote: Optional[str] = None
    due_date: Optional[date] = None
    status: TaskStatus = Field(default=TaskStatus.open)
    is_published: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_reminder_sent_at: Optional[datetime] = Field(default=None)

    meeting: Optional[Meeting] = Relationship(back_populates="tasks")
    assignee: Optional[User] = Relationship(back_populates="tasks")

