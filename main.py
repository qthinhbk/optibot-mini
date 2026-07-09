"""
Daily job: scrape OptiSigns support docs and upload only Gemini deltas.

This is the part-3 entry point. It intentionally reuses scraper.py for the
Zendesk scrape and Gemini File API helpers from upload_to_gemini.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

import scraper
import upload_to_gemini

load_dotenv()

SCRAPE_MANIFEST = Path("manifest.json")
UPLOAD_MANIFEST = Path("upload_manifest.json")
ARTICLES_DIR = Path("articles")
JOB_LOG_DIR = Path("job_logs")
LATEST_JOB_LOG = JOB_LOG_DIR / "latest.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def classify_deltas(old_manifest: dict, new_manifest: dict) -> dict[str, list[dict]]:
    added: list[dict] = []
    updated: list[dict] = []
    skipped: list[dict] = []

    for article_id, new_entry in new_manifest.items():
        old_entry = old_manifest.get(article_id)
        if not old_entry:
            added.append(new_entry)
        elif old_entry.get("content_hash") != new_entry.get("content_hash"):
            updated.append(new_entry)
        else:
            skipped.append(new_entry)

    return {"added": added, "updated": updated, "skipped": skipped}


def upload_delta_files(client: genai.Client, files_to_upload: list[Path]) -> list[dict]:
    uploaded: list[dict] = []
    for index, path in enumerate(files_to_upload, 1):
        log.info("[%s/%s] Delta upload to Gemini: %s", index, len(files_to_upload), path.name)
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
    return uploaded


def merge_upload_manifest(delta_uploads: list[dict]) -> dict:
    manifest = upload_to_gemini.load_manifest()
    existing = {entry["local_file"]: entry for entry in manifest.get("files", [])}

    for entry in delta_uploads:
        existing[entry["local_file"]] = entry

    files = sorted(existing.values(), key=lambda entry: entry["local_file"])
    manifest.update(
        {
            "model": upload_to_gemini.resolve_model(manifest),
            "system_prompt": upload_to_gemini.SYSTEM_PROMPT,
            "files_uploaded": len(files),
            "estimated_chunks_embedded": len(files),
            "chunking_strategy": "document-level: one Markdown file per uploaded knowledge file",
            "files": files,
        }
    )
    upload_to_gemini.save_manifest(manifest)
    return manifest


def run_daily_job() -> dict:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or API_KEY before running the job.")

    old_scrape_manifest = read_json(SCRAPE_MANIFEST)

    log.info("=" * 60)
    log.info("Starting daily scrape + Gemini delta upload job")
    log.info("=" * 60)

    scrape_stats = scraper.scrape_articles()
    new_scrape_manifest = read_json(SCRAPE_MANIFEST)
    deltas = classify_deltas(old_scrape_manifest, new_scrape_manifest)

    upload_manifest_before = upload_to_gemini.load_manifest()
    needs_initial_upload = not upload_manifest_before.get("files")

    changed_entries = list(new_scrape_manifest.values()) if needs_initial_upload else deltas["added"] + deltas["updated"]
    changed_files = [ARTICLES_DIR / entry["filename"] for entry in changed_entries]
    changed_files = [path for path in changed_files if path.exists()]

    client = genai.Client(api_key=api_key)
    delta_uploads = upload_delta_files(client, changed_files) if changed_files else []
    if delta_uploads:
        upload_to_gemini.wait_for_files_active(client, delta_uploads)

    upload_manifest = merge_upload_manifest(delta_uploads)

    now = datetime.now(timezone.utc)
    job_log = {
        "ran_at": now.isoformat(),
        "scrape": scrape_stats,
        "delta": {
            "added": len(deltas["added"]),
            "updated": len(deltas["updated"]),
            "skipped": len(deltas["skipped"]),
            "uploaded": len(delta_uploads),
            "initial_upload": needs_initial_upload,
        },
        "gemini": {
            "model": upload_to_gemini.resolve_model(upload_manifest),
            "knowledge_files": len(upload_manifest.get("files", [])),
            "estimated_chunks_embedded": upload_manifest.get(
                "estimated_chunks_embedded",
                len(upload_manifest.get("files", [])),
            ),
        },
    }

    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamped_log = JOB_LOG_DIR / f"{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    write_json(timestamped_log, job_log)
    write_json(LATEST_JOB_LOG, job_log)

    log.info("=" * 60)
    log.info("Daily job complete")
    log.info("Added:   %s", job_log["delta"]["added"])
    log.info("Updated: %s", job_log["delta"]["updated"])
    log.info("Skipped: %s", job_log["delta"]["skipped"])
    log.info("Uploaded delta files: %s", job_log["delta"]["uploaded"])
    log.info("Latest job log: %s", LATEST_JOB_LOG)
    log.info("=" * 60)

    return job_log


def main() -> int:
    try:
        result = run_daily_job()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        log.exception("Daily job failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
