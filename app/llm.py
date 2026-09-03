import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

HERMES_BASE_URL = os.environ["HERMES_BASE_URL"]
HERMES_MODEL = os.environ["HERMES_MODEL"]
NVIDIA_API_KEY = os.environ["NVIDIA_API_KEY"]

MAX_RETRIES = 2


class LLMExtractionError(Exception):
    """Не удалось получить валидный ответ от NIM (сеть, лимиты, невалидный JSON)."""
    pass


SYSTEM_PROMPT = """Ты помогаешь извлекать action items (задачи) из транскрипта деловой встречи \
в компании, торгующей авиационными запчастями. Внимательно читай транскрипт и находи только \
реальные задачи — конкретные действия, порученные конкретному человеку. Не путай обсуждение \
темы с назначением задачи: "мы обсудили сертификацию" — это не задача, а "Мария, подготовь \
документы по сертификации к пятнице" — задача.

ОБЯЗАТЕЛЬНО отвечай СТРОГО валидным JSON и ничем больше — без markdown-разметки, без ```
блоков кода, без пояснений до или после. Формат ответа должен быть ТОЧНО таким:

{"tasks": [{"assignee_name": "Имя Фамилия", "description": "Описание задачи", "due_date": "YYYY-MM-DD или null", "source_quote": "точная цитата из транскрипта"}]}

Если задач в транскрипте нет — верни {"tasks": []}. Указывай due_date только если в тексте \
явно и однозначно назван конкретный календарный срок — иначе строго null, не пытайся \
угадывать или приближать дату."""


def _extract_json_block(raw_text: str) -> str:
    """Модели без constrained decoding иногда оборачивают JSON в ```json ... ``` —
    вырезаем содержимое, если это произошло, иначе возвращаем текст как есть."""
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw_text, re.DOTALL)
    if match:
        return match.group(1)
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw_text[start:end + 1]
    return raw_text


def _call_nim(user_content: str) -> list[dict]:
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                f"{HERMES_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {NVIDIA_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": HERMES_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.1,
                    "chat_template_kwargs": {"thinking": False},
                },
                timeout=60,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[llm.py] Ошибка запроса к NIM: {type(e).__name__}: {e}")
            raise LLMExtractionError(
                "Не удалось связаться с NVIDIA NIM — проверьте сеть, ключ и лимиты API."
            ) from e

        raw_content = response.json()["choices"][0]["message"]["content"]

        try:
            json_text = _extract_json_block(raw_content)
            parsed = json.loads(json_text)
            tasks = parsed.get("tasks", [])
            if not isinstance(tasks, list):
                raise ValueError("Поле 'tasks' не является списком")
            return tasks
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"[llm.py] Невалидный JSON от модели (попытка {attempt + 1}): {e}")
            last_error = e
            continue

    raise LLMExtractionError(
        f"NIM вернул невалидный JSON после {MAX_RETRIES + 1} попыток. "
        "Попробуйте позже или упростите системный промпт."
    ) from last_error


def extract_tasks(transcript: str) -> list[dict]:
    return _call_nim(f"Транскрипт встречи:\n\n{transcript}")