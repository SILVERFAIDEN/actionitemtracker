import io
import re

import pdfplumber

def parse_vtt(raw_text: str) -> str:
    lines = raw_text.splitlines()
    result = []

    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        match = re.match(r"<v ([^>]+)>(.*)", line)
        if match:
            speaker, text = match.groups()
            result.append(f"{speaker}: {text}")
        else:
            result.append(line)

    return "\n".join(result)


SPEAKER_LINE_RE = re.compile(r"^(?P<speaker>.+?)\s+(?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?)$")


def parse_meeting_pdf(file_bytes: bytes) -> str:
    text_lines = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_lines.extend(page_text.splitlines())

    result = []
    current_speaker = None

    for line in text_lines:
        line = line.strip()
        if not line:
            continue

        match = SPEAKER_LINE_RE.match(line)
        if match:
            current_speaker = match.group("speaker")
            continue

        if current_speaker:
            result.append(f"{current_speaker}: {line}")
        else:
            result.append(line)

    return "\n".join(result)