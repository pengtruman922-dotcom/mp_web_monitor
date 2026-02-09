"""File download tool for the Agent."""
import logging
import uuid
from pathlib import Path

import httpx

from app.config import DOWNLOADS_DIR, AGENT_MAX_FILE_SIZE_MB

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".xlsx", ".xls"}


async def download_file(url: str, filename: str) -> str:
    """Download a file from URL and save it locally.

    Returns the local file path, or an error message string.
    """
    # Sanitize filename
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"不支持的文件格式: {ext}（支持: {', '.join(ALLOWED_EXTENSIONS)}）"

    safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    save_path = DOWNLOADS_DIR / safe_name

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()

                # Check content length
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > AGENT_MAX_FILE_SIZE_MB * 1024 * 1024:
                    return f"文件过大（超过{AGENT_MAX_FILE_SIZE_MB}MB限制），跳过下载"

                total = 0
                with open(save_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        total += len(chunk)
                        if total > AGENT_MAX_FILE_SIZE_MB * 1024 * 1024:
                            f.close()
                            save_path.unlink(missing_ok=True)
                            return f"文件过大（超过{AGENT_MAX_FILE_SIZE_MB}MB限制），下载已中止"
                        f.write(chunk)

        logger.info("Downloaded %s -> %s (%d bytes)", url, save_path, total)
        return str(save_path)

    except Exception as e:
        logger.error("Failed to download %s: %s", url, e)
        save_path.unlink(missing_ok=True)
        return f"下载失败: {e}"
