from dotenv import load_dotenv
load_dotenv()

import os
import secrets
from datetime import date, datetime

from fastapi import Depends, FastAPI, Form, UploadFile, File, Request, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select

from database import get_session
from models import Meeting, Task, User, TaskStatus, UserRole
from llm import extract_tasks, LLMExtractionError
from transcript_parser import parse_meeting_pdf
from matching import match_user
from auth import hash_password, verify_password, get_current_user

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET_KEY"])
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Вспомогательные функции ----------

def parse_due_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def build_task_context(session: Session) -> dict:
    tasks = session.exec(select(Task).where(Task.is_published == True)).all()
    all_users = session.exec(select(User).order_by(User.full_name)).all()

    grouped: dict[str, list[Task]] = {}
    done_tasks: list[Task] = []
    unresolved: list[Task] = []

    for task in tasks:
        if task.status == TaskStatus.done:
            done_tasks.append(task)
        elif task.assignee:
            grouped.setdefault(task.assignee.full_name, []).append(task)
        else:
            unresolved.append(task)

    return {
        "grouped": grouped,
        "done_tasks": done_tasks,
        "unresolved": unresolved,
        "all_users": all_users,
    }


def build_review_context(session: Session) -> dict:
    tasks = session.exec(select(Task).where(Task.is_published == False)).all()
    all_users = session.exec(select(User).order_by(User.full_name)).all()

    by_meeting: dict[str, list[Task]] = {}
    for task in tasks:
        meeting_title = task.meeting.title if task.meeting else "Без встречи"
        by_meeting.setdefault(meeting_title, []).append(task)

    return {"by_meeting": by_meeting, "all_users": all_users}


def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    expected = os.environ.get("HERMES_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий API-ключ")


# ---------- Pydantic-схемы для JSON API ----------

class ApiMeetingUpload(BaseModel):
    meeting_title: str
    transcript_text: str
    teams_meeting_id: str | None = None


class ApiTaskCreate(BaseModel):
    assignee_name: str
    description: str
    due_date: str | None = None


class ApiTaskUpdate(BaseModel):
    status: str | None = None
    description: str | None = None
    due_date: str | None = None


# ---------- Корень ----------

@app.get("/")
def root():
    return RedirectResponse(url="/tasks", status_code=303)


# ---------- Аутентификация ----------

@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    statement = select(User).where(User.email == email)
    user = session.exec(statement).first()

    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный email или пароль"}
        )

    request.session["user_id"] = user.id
    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------- Страница задач ----------

@app.get("/tasks")
def view_tasks(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    context = build_task_context(session)
    context["current_user"] = current_user
    return templates.TemplateResponse(request, "tasks.html", context)


@app.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: int, request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    task = session.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)

    is_owner = task.assignee_id == current_user.id
    if current_user.role != UserRole.manager and not is_owner:
        return RedirectResponse(url="/tasks", status_code=303)

    task.status = TaskStatus.open if task.status == TaskStatus.done else TaskStatus.done
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()

    if request.headers.get("HX-Request"):
        context = build_task_context(session)
        context["current_user"] = current_user
        return templates.TemplateResponse(request, "partials/task_board.html", context)

    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/tasks/{task_id}/edit")
def edit_task_form(task_id: int, request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    task = session.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)

    is_owner = task.assignee_id == current_user.id
    if current_user.role != UserRole.manager and not is_owner:
        return RedirectResponse(url="/tasks", status_code=303)

    all_users = session.exec(select(User).order_by(User.full_name)).all()
    return templates.TemplateResponse(
        request,
        "partials/task_edit_row.html",
        {"task": task, "all_users": all_users, "current_user": current_user},
    )


@app.get("/tasks/{task_id}/view")
def view_task_row(task_id: int, request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    task = session.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)

    return templates.TemplateResponse(
        request, "partials/task_row.html", {"task": task, "current_user": current_user}
    )


@app.post("/tasks/{task_id}/edit")
def edit_task(
    task_id: int,
    request: Request,
    description: str = Form(...),
    due_date: str = Form(""),
    assignee_id: int = Form(...),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    task = session.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/tasks", status_code=303)

    is_owner = task.assignee_id == current_user.id
    if current_user.role != UserRole.manager and not is_owner:
        return RedirectResponse(url="/tasks", status_code=303)

    if current_user.role == UserRole.manager:
        assignee = session.get(User, assignee_id)
        if assignee is None:
            context = build_task_context(session)
            context["current_user"] = current_user
            context["resolve_error"] = "Выбранный сотрудник не найден."
            return templates.TemplateResponse(request, "partials/task_board.html", context)
        task.assignee_id = assignee.id

    task.description = description
    task.due_date = parse_due_date(due_date) if due_date else None
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()

    context = build_task_context(session)
    context["current_user"] = current_user
    return templates.TemplateResponse(request, "partials/task_board.html", context)


@app.post("/tasks/{task_id}/delete")
def delete_task(
    task_id: int,
    request: Request,
    return_to: str = Form("tasks"),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    task = session.get(Task, task_id)
    if task is not None:
        session.delete(task)
        session.commit()

    if request.headers.get("HX-Request"):
        if return_to == "review":
            context = build_review_context(session)
            context["current_user"] = current_user
            return templates.TemplateResponse(request, "partials/review_board.html", context)
        context = build_task_context(session)
        context["current_user"] = current_user
        return templates.TemplateResponse(request, "partials/task_board.html", context)

    return RedirectResponse(url=f"/{return_to}", status_code=303)


@app.post("/tasks/clear-done")
def clear_done_tasks(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    statement = select(Task).where(Task.status == TaskStatus.done)
    if current_user.role != UserRole.manager:
        statement = statement.where(Task.assignee_id == current_user.id)

    for task in session.exec(statement).all():
        session.delete(task)
    session.commit()

    if request.headers.get("HX-Request"):
        context = build_task_context(session)
        context["current_user"] = current_user
        return templates.TemplateResponse(request, "partials/task_board.html", context)

    return RedirectResponse(url="/tasks", status_code=303)


# ---------- Ручное создание задачи ----------

@app.get("/tasks/new")
def new_task_form(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    all_users = session.exec(select(User).order_by(User.full_name)).all()
    return templates.TemplateResponse(
        request, "task_new.html", {"current_user": current_user, "all_users": all_users}
    )


@app.post("/tasks/new")
def create_task_manual(
    request: Request,
    assignee_id: int = Form(...),
    description: str = Form(...),
    due_date: str = Form(""),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    assignee = session.get(User, assignee_id)
    if assignee is None:
        return templates.TemplateResponse(
            request, "partials/form_result.html", {"error": "Выбранный сотрудник не найден."}
        )

    task = Task(
        meeting_id=None,
        assignee_id=assignee.id,
        raw_assignee_name=assignee.full_name,
        description=description,
        due_date=parse_due_date(due_date) if due_date else None,
        status=TaskStatus.open,
        is_published=True,
    )
    session.add(task)
    session.commit()

    return templates.TemplateResponse(
        request,
        "partials/form_result.html",
        {
            "error": None,
            "message": f"Задача для {assignee.full_name} создана.",
            "redirect_url": "/tasks",
        },
    )


# ---------- Загрузка транскрипта ----------

@app.get("/upload")
def upload_form(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    return templates.TemplateResponse(
        request, "upload.html", {"current_user": current_user}
    )


@app.post("/meetings/upload")
async def upload_transcript(
    request: Request,
    meeting_title: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    is_htmx = request.headers.get("HX-Request") is not None

    if current_user is None or current_user.role != UserRole.manager:
        if is_htmx:
            return templates.TemplateResponse(
                request,
                "partials/upload_result.html",
                {"error": "Загружать транскрипты может только менеджер"},
            )
        return {"error": "Загружать транскрипты может только менеджер"}

    raw_bytes = await file.read()

    if file.filename.endswith(".pdf"):
        transcript_text = parse_meeting_pdf(raw_bytes)
    else:
        transcript_text = raw_bytes.decode("utf-8")

    try:
        tasks_data = extract_tasks(transcript_text)
    except LLMExtractionError as e:
        if is_htmx:
            return templates.TemplateResponse(
                request, "partials/upload_result.html", {"error": str(e)}
            )
        return {"error": str(e)}

    meeting = Meeting(title=meeting_title, meeting_date=datetime.utcnow())
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    created_tasks = []
    unresolved_count = 0

    for t in tasks_data:
        assignee_name = t["assignee_name"]
        matched_user = match_user(assignee_name, session)

        if matched_user is None:
            unresolved_count += 1

        task = Task(
            meeting_id=meeting.id,
            assignee_id=matched_user.id if matched_user else None,
            raw_assignee_name=assignee_name,
            description=t["description"],
            due_date=parse_due_date(t.get("due_date")),
            context_quote=t["source_quote"],
        )
        session.add(task)
        created_tasks.append(task)

    session.commit()

    if is_htmx:
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "error": None,
                "meeting_title": meeting.title,
                "tasks_created": len(created_tasks),
                "unresolved_assignees": unresolved_count,
            },
        )

    return {
        "meeting_id": meeting.id,
        "tasks_created": len(created_tasks),
        "unresolved_assignees": unresolved_count,
    }


# ---------- Ревью ----------

@app.get("/review")
def review_form(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    context = build_review_context(session)
    context["current_user"] = current_user
    return templates.TemplateResponse(request, "review.html", context)


@app.post("/tasks/{task_id}/publish")
def publish_task(
    task_id: int,
    request: Request,
    description: str = Form(...),
    due_date: str = Form(""),
    assignee_id: int = Form(...),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    task = session.get(Task, task_id)
    if task is None:
        return RedirectResponse(url="/review", status_code=303)

    assignee = session.get(User, assignee_id)
    if assignee is None:
        context = build_review_context(session)
        context["current_user"] = current_user
        context["publish_error"] = "Выбранный сотрудник не найден."
        return templates.TemplateResponse(request, "partials/review_board.html", context)

    task.description = description
    task.due_date = parse_due_date(due_date) if due_date else None
    task.assignee_id = assignee.id
    task.is_published = True
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()

    context = build_review_context(session)
    context["current_user"] = current_user
    return templates.TemplateResponse(request, "partials/review_board.html", context)


# ---------- Управление сотрудниками ----------

@app.get("/users/new")
def new_user_form(request: Request, session: Session = Depends(get_session)):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    return templates.TemplateResponse(request, "user_new.html", {"current_user": current_user})


@app.post("/users")
def create_user(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
    session: Session = Depends(get_session),
):
    current_user = get_current_user(request, session)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if current_user.role != UserRole.manager:
        return RedirectResponse(url="/tasks", status_code=303)

    final_role = UserRole(role) if current_user.is_admin else UserRole.member

    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        return templates.TemplateResponse(
            request,
            "partials/form_result.html",
            {"error": f"Пользователь с email {email} уже существует."},
        )

    new_employee = User(
        full_name=full_name,
        email=email,
        hashed_password=hash_password(password),
        role=final_role,
    )
    session.add(new_employee)
    session.commit()

    return templates.TemplateResponse(
        request,
        "partials/form_result.html",
        {
            "error": None,
            "message": f"Сотрудник «{full_name}» добавлен с email {email}.",
            "redirect_url": "/tasks",
        },
    )


# ---------- API для Hermes ----------

@app.get("/api/users")
def api_list_users(session: Session = Depends(get_session), _=Depends(verify_api_key)):
    users = session.exec(select(User).order_by(User.full_name)).all()
    return [
        {"id": u.id, "full_name": u.full_name, "email": u.email, "role": u.role.value}
        for u in users
    ]


@app.get("/api/tasks")
def api_list_tasks(
    status: str | None = None,
    unresolved: bool | None = None,
    session: Session = Depends(get_session),
    _=Depends(verify_api_key),
):
    tasks = session.exec(select(Task).where(Task.is_published == True)).all()

    result = []
    for t in tasks:
        if status and t.status.value != status:
            continue
        if unresolved is True and t.assignee_id is not None:
            continue
        if unresolved is False and t.assignee_id is None:
            continue
        result.append({
            "id": t.id,
            "assignee": t.assignee.full_name if t.assignee else None,
            "raw_assignee_name": t.raw_assignee_name,
            "description": t.description,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "status": t.status.value,
        })
    return result


@app.post("/api/tasks")
def api_create_task(
    payload: ApiTaskCreate,
    session: Session = Depends(get_session),
    _=Depends(verify_api_key),
):
    matched_user = match_user(payload.assignee_name, session)

    task = Task(
        meeting_id=None,
        assignee_id=matched_user.id if matched_user else None,
        raw_assignee_name=payload.assignee_name,
        description=payload.description,
        due_date=parse_due_date(payload.due_date) if payload.due_date else None,
        status=TaskStatus.open,
        is_published=True,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    return {
        "id": task.id,
        "assignee_matched": matched_user.full_name if matched_user else None,
        "unresolved": matched_user is None,
    }


@app.patch("/api/tasks/{task_id}")
def api_update_task(
    task_id: int,
    payload: ApiTaskUpdate,
    session: Session = Depends(get_session),
    _=Depends(verify_api_key),
):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if payload.status is not None:
        try:
            task.status = TaskStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Неверный статус: {payload.status}")
    if payload.description is not None:
        task.description = payload.description
    if payload.due_date is not None:
        task.due_date = parse_due_date(payload.due_date)

    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()
    return {"id": task.id, "status": task.status.value}


@app.get("/api/tasks/overdue")
def api_overdue_tasks(session: Session = Depends(get_session), _=Depends(verify_api_key)):
    today = date.today()
    tasks = session.exec(
        select(Task).where(
            Task.is_published == True,
            Task.status == TaskStatus.open,
            Task.due_date != None,
            Task.due_date < today,
        )
    ).all()

    result = []
    for t in tasks:
        if t.assignee is None:
            continue
        result.append({
            "id": t.id,
            "assignee_name": t.assignee.full_name,
            "assignee_email": t.assignee.email,
            "description": t.description,
            "due_date": t.due_date.isoformat(),
            "days_overdue": (today - t.due_date).days,
            "last_reminder_sent_at": t.last_reminder_sent_at.isoformat() if t.last_reminder_sent_at else None,
        })
    return result


@app.post("/api/tasks/{task_id}/reminder-sent")
def api_mark_reminder_sent(
    task_id: int,
    session: Session = Depends(get_session),
    _=Depends(verify_api_key),
):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    task.last_reminder_sent_at = datetime.utcnow()
    session.add(task)
    session.commit()
    return {"id": task.id, "last_reminder_sent_at": task.last_reminder_sent_at.isoformat()}


@app.post("/api/meetings/upload")
def api_upload_meeting(
    payload: ApiMeetingUpload,
    session: Session = Depends(get_session),
    _=Depends(verify_api_key),
):
    if payload.teams_meeting_id:
        existing = session.exec(
            select(Meeting).where(Meeting.teams_meeting_id == payload.teams_meeting_id)
        ).first()
        if existing:
            return {
                "meeting_id": existing.id,
                "duplicate": True,
                "message": "Эта встреча уже была загружена ранее.",
            }

    try:
        tasks_data = extract_tasks(payload.transcript_text)
    except LLMExtractionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    meeting = Meeting(
        title=payload.meeting_title,
        meeting_date=datetime.utcnow(),
        teams_meeting_id=payload.teams_meeting_id,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    created_tasks = []
    unresolved_count = 0

    for t in tasks_data:
        assignee_name = t["assignee_name"]
        matched_user = match_user(assignee_name, session)

        if matched_user is None:
            unresolved_count += 1

        task = Task(
            meeting_id=meeting.id,
            assignee_id=matched_user.id if matched_user else None,
            raw_assignee_name=assignee_name,
            description=t["description"],
            due_date=parse_due_date(t.get("due_date")),
            context_quote=t["source_quote"],
        )
        session.add(task)
        created_tasks.append(task)

    session.commit()

    return {
        "meeting_id": meeting.id,
        "duplicate": False,
        "tasks_created": len(created_tasks),
        "unresolved_assignees": unresolved_count,
        "review_url": "/review",
    }