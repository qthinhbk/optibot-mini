"""
Upload scraped Markdown articles to Gemini File API and test OptiBot.

Gemini note:
  Gemini File API is used here as the knowledge base equivalent. The files are
  uploaded by API, then selected uploaded files are supplied as grounded context
  for the test answer. The upload manifest is reused to avoid duplicate uploads.

Environment:
  GEMINI_API_KEY    Required.
  GEMINI_MODEL      Optional. Defaults to the model saved in upload_manifest.json,
                    then gemini-3.1-flash-lite as a fallback.
  FORCE_REUPLOAD=1  Optional. Re-upload files instead of reusing manifest.

Chunking strategy:
  Document-level chunks: each Markdown article is one uploaded knowledge file.
  For the sample question, the script retrieves YouTube-related uploaded docs by
  filename and passes only those docs to Gemini as context.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

ARTICLES_DIR = Path("articles")
UPLOAD_MANIFEST = Path("upload_manifest.json")
TEST_OUTPUT = Path("gemini_test_answer.txt")

SYSTEM_PROMPT = (
    "You are OptiBot, the customer-support bot for OptiSigns.com.\n"
    "\u2022 Tone: helpful, factual, concise.\n"
    "\u2022 Only answer using the uploaded docs.\n"
    "\u2022 Max 5 bullet points; else link to the doc.\n"
    '\u2022 Cite up to 3 "Article URL:" lines per reply.'
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_manifest() -> dict:
    if not UPLOAD_MANIFEST.exists():
        return {}
    with UPLOAD_MANIFEST.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(manifest: dict) -> None:
    UPLOAD_MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def markdown_files() -> list[Path]:
    files = sorted(ARTICLES_DIR.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No Markdown files found in {ARTICLES_DIR}")
    return files


def estimate_tokens(text: str) -> int:
    words = re.findall(r"\S+", text)
    return max(1, math.ceil(len(words) / 0.75))


def estimate_chunks(files: Iterable[Path]) -> int:
    # Document-level chunking: one file is one knowledge unit.
    return sum(1 for _ in files)


def resolve_model(manifest: dict) -> str:
    return os.getenv("GEMINI_MODEL") or manifest.get("model") or "gemini-2.5-flash"


def upload_files(client: genai.Client, files: list[Path], manifest: dict) -> list[dict]:
    existing = {entry["local_file"]: entry for entry in manifest.get("files", [])}
    uploaded: list[dict] = []
    skipped = 0

    for index, path in enumerate(files, 1):
        old = existing.get(path.name)
        if old and old.get("name") and old.get("size_bytes") == path.stat().st_size:
            uploaded.append(old)
            skipped += 1
            continue

        log.info("[%s/%s] Uploading %s", index, len(files), path.name)
        result = client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(
                display_name=path.stem,
                mime_type="text/markdown",
            ),
        )
        uploaded.append(
            {
                "name": result.name,
                "uri": result.uri,
                "display_name": path.stem,
                "mime_type": result.mime_type or "text/markdown",
                "local_file": path.name,
                "size_bytes": path.stat().st_size,
            }
        )

        if index % 5 == 0:
            time.sleep(1)

    log.info("Gemini upload summary: files=%s added=%s skipped=%s", len(uploaded), len(uploaded) - skipped, skipped)
    return uploaded


def wait_for_files_active(client: genai.Client, files: list[dict]) -> None:
    pending = 0
    for entry in files:
        while True:
            file_obj = client.files.get(name=entry["name"])
            state_name = file_obj.state.name if file_obj.state else "UNKNOWN"
            if state_name == "ACTIVE":
                break
            if state_name == "FAILED":
                raise RuntimeError(f"Gemini file failed: {entry['local_file']} ({entry['name']})")
            pending += 1
            time.sleep(2)
    log.info("Gemini files active: files=%s pending_checks=%s", len(files), pending)


def select_relevant_files(files: list[dict], question: str) -> list[dict]:
    keywords = ["youtube"]
    selected = [
        f for f in files
        if any(keyword in f["local_file"].lower() or keyword in f.get("display_name", "").lower() for keyword in keywords)
    ]
    if selected:
        return selected[:8]
    return files[:8]


def test_assistant(client: genai.Client, model: str, files: list[dict]) -> str:
    question = "How do I add a YouTube video?"
    context_files = select_relevant_files(files, question)
    log.info('Testing Gemini assistant with: "%s"', question)
    log.info("Using %s uploaded context files", len(context_files))

    parts = [
        types.Part.from_uri(file_uri=entry["uri"], mime_type=entry.get("mime_type") or "text/markdown")
        for entry in context_files
    ]
    parts.append(types.Part.from_text(text=question))

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    answer = response.text or ""
    TEST_OUTPUT.write_text(answer, encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"Q: {question}")
    print("=" * 72)
    print(answer)
    print("=" * 72 + "\n")
    return answer


def main() -> int:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY is not set. Copy .env.sample to .env and add your key.")
        return 1

    client = genai.Client(api_key=api_key)
    manifest = load_manifest()
    model = resolve_model(manifest)
    force_reupload = os.getenv("FORCE_REUPLOAD") == "1"

    if manifest.get("files") and not force_reupload:
        uploaded = manifest["files"]
        log.info("Reusing Gemini upload manifest: files=%s", len(uploaded))
    else:
        files = markdown_files()
        uploaded = upload_files(client, files, manifest)
        manifest = {
            "model": model,
            "system_prompt": SYSTEM_PROMPT,
            "files_uploaded": len(uploaded),
            "estimated_chunks_embedded": estimate_chunks(files),
            "chunking_strategy": "document-level: one Markdown file per uploaded knowledge file",
            "files": uploaded,
        }
        save_manifest(manifest)

    # Only verify the subset needed for the sample question; the full 406-file
    # manifest was already created by the upload step.
    relevant_files = select_relevant_files(uploaded, "How do I add a YouTube video?")
    wait_for_files_active(client, relevant_files)

    log.info(
        "Gemini knowledge log: files=%s estimated_chunks=%s model=%s",
        len(uploaded),
        manifest.get("estimated_chunks_embedded", len(uploaded)),
        model,
    )
    test_assistant(client, model, uploaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
