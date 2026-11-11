import json
import os
import re
import time
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

from .credentials import load_credentials


TIMESTAMP_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{3})(.*)$"
)


def _parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        timestamp_index = 1 if lines[0].strip().isdigit() else 0
        if timestamp_index >= len(lines):
            continue
        match = TIMESTAMP_RE.match(lines[timestamp_index])
        if not match:
            continue
        cue_text = "\n".join(lines[timestamp_index + 1 :]).strip()
        if not cue_text:
            continue
        cues.append(
            {
                "id": len(cues),
                "index": lines[0].strip() if timestamp_index == 1 else str(len(cues) + 1),
                "timestamp": lines[timestamp_index].strip(),
                "text": cue_text,
            }
        )
    return cues


def _batches(cues: list[dict], max_items=90, max_characters=10_000):
    batch = []
    size = 0
    for cue in cues:
        cue_size = len(cue["text"]) + 30
        if batch and (len(batch) >= max_items or size + cue_size > max_characters):
            yield batch
            batch = []
            size = 0
        batch.append({"id": cue["id"], "text": cue["text"]})
        size += cue_size
    if batch:
        yield batch


def _response_text(response) -> str:
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item) for item in content
        )
    return str(content)


def _parse_json_array(value: str) -> list[dict]:
    value = value.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = value.find("[")
    end = value.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("Gemini did not return a JSON array")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("Gemini translation response is not a list")
    return parsed


def translate_english_srt_to_sinhala(
    source: str | Path,
    output: str | Path,
    progress_callback=None,
) -> Path | None:
    """Translate an English SRT with Gemini while preserving its exact timings."""
    source = Path(source).resolve()
    output = Path(output).resolve()
    cues = _parse_srt(source)
    if not cues:
        return None
    if not os.environ.get("GOOGLE_API_KEY"):
        load_credentials()
    model = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        temperature=0.15,
        client_args={"http2": False},
        timeout=120,
        max_retries=3,
    )
    batches = list(_batches(cues))
    translated = {}
    for batch_number, batch in enumerate(batches, start=1):
        if progress_callback:
            progress_callback(batch_number, len(batches))
        prompt = (
            "Translate the following English movie subtitle dialogue into natural, concise Sinhala. "
            "Preserve meaning, tone, names, HTML/formatting tags, line breaks, and speaker dashes. "
            "Do not add explanations or censor dialogue. Return ONLY a valid JSON array with the "
            "same numeric id values and translated text values, formatted as "
            '[{"id": 1, "text": "සිංහල"}]. Input:\n' + json.dumps(batch, ensure_ascii=False)
        )
        parsed = None
        for attempt in range(1, 4):
            try:
                parsed = _parse_json_array(_response_text(model.invoke(prompt)))
                break
            except Exception:
                if attempt == 3:
                    return None
                time.sleep(2**attempt)
        expected = {item["id"] for item in batch}
        for item in parsed or []:
            if not isinstance(item, dict) or item.get("id") not in expected:
                continue
            text = str(item.get("text", "")).strip()
            if text:
                translated[item["id"]] = text

    if len(translated) != len(cues):
        return None
    blocks = []
    for cue in cues:
        blocks.append(f'{cue["index"]}\n{cue["timestamp"]}\n{translated[cue["id"]]}')
    temporary = output.with_name(f"{output.stem}.translation.part.srt")
    try:
        temporary.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
        temporary.replace(output)
        return output
    except OSError:
        temporary.unlink(missing_ok=True)
        return None
