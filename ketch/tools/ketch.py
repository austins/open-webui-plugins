"""
title: Ketch
description: Searches the public web and extracts fresh page content with isolated Ketch subprocesses.
version: 0.14.2
required_open_webui_version: 0.11.3
requirements: playwright==1.60.0
"""

import asyncio
import ctypes
import hashlib
import html
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import stat
import threading
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
STATE_KEY = "_ketch_tool_runtime_v1"
USAGE_STATE_KEY = "_ketch_tool_usage_v1"
DOCUMENT_CACHE_STATE_KEY = "_ketch_tool_document_cache_v1"
USAGE_TTL_SECONDS = 15 * 60
MAX_USAGE_RECORDS_PER_USER = 100
MAX_USAGE_RECORDS_GLOBAL = 1_000
MAX_DOCUMENT_CACHE_ITEMS_PER_USER = 4
MAX_DOCUMENT_CACHE_ITEMS_GLOBAL = 100
MAX_DOCUMENT_CACHE_CHARACTERS_PER_USER = 2_000_000
MAX_DOCUMENT_CACHE_CHARACTERS_GLOBAL = 20_000_000
MAX_CONCURRENT_DOCUMENT_EXTRACTIONS = 2
MAX_DOCUMENT_HEADINGS = 64
MAX_DOCUMENT_HEADING_CHARACTERS = 160
TOOL_VERSION = "0.14.2"
KETCH_VERSION = "0.16.2"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BROWSER_ARCHIVE_BYTES = 192 * 1024 * 1024
MAX_BROWSER_EXTRACTED_BYTES = 320 * 1024 * 1024
BROWSER_INSTALL_FREE_BYTES = (
    MAX_BROWSER_ARCHIVE_BYTES + MAX_BROWSER_EXTRACTED_BYTES + 64 * 1024 * 1024
)
APT_INSTALL_FREE_BYTES = 512 * 1024 * 1024
# Must match the playwright pin in Open WebUI's backend/requirements.txt.
PLAYWRIGHT_REQUIREMENT = "playwright==1.60.0"
MAX_BINARY_BYTES = 64 * 1024 * 1024
MAX_BROWSER_BINARY_BYTES = 256 * 1024 * 1024
MAX_URL_CHARS = 2_048
MAX_REDIRECTS = 5
MAX_WARNING_ITEMS = 6
MAX_WARNING_ITEM_CHARS = 180
MAX_WARNING_CHARS = 600
MAX_SUBPROCESS_STDOUT_BYTES = 8 * 1024 * 1024
MAX_SUBPROCESS_STDERR_BYTES = 64 * 1024
MAX_SEARCH_FINGERPRINT_TERMS = 64
CONTINUATION_ID_PATTERN = re.compile(r"^kc_[A-Za-z0-9_-]{24}$")
REDIRECT_DESTINATION_RE = re.compile(
    r"Validated redirect destination:\s*(\S+?)\.\s+If needed"
)
# A URL is "offered" when Ketch itself surfaced it in this user message, as a
# search result or a validated redirect destination. Anything else is a URL the
# model composed. Composed URLs stay allowed until a host has repeatedly proven
# that the model is guessing paths on it.
MAX_TRACKED_OFFERED_URLS = 200
MAX_TRACKED_FAILED_HOSTS = 32
FABRICATED_URL_HOST_FAILURE_THRESHOLD = 2
MAX_INSPECTED_SOURCES = 24
SEARCH_QUERY_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "describe",
        "do",
        "does",
        "explain",
        "find",
        "for",
        "from",
        "give",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "search",
        "show",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "use",
        "using",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
    }
)
SEARCH_QUERY_TERM_ALIASES = {
    "architectures": "architecture",
    "directories": "directory",
    "doc": "documentation",
    "docs": "documentation",
    "documented": "documentation",
    "documenting": "documentation",
    "internals": "internal",
    "pages": "page",
    "providers": "provider",
    "repositories": "repository",
    "repos": "repository",
    "sources": "source",
    "structured": "structure",
    "structuring": "structure",
}
SEARCH_QUERY_GENERIC_TERMS = frozenset(
    {
        "documentation",
        "github",
        "official",
        "page",
        "primary",
        "project",
        "public",
        "repository",
        "site",
        "source",
        "technical",
        "text",
        "wiki",
    }
)
SPA_MARKERS = (
    'id="__next"',
    "id='__next'",
    'id="__nuxt"',
    "id='__nuxt'",
    "data-reactroot",
    "ng-version=",
    "<app-root",
    'id="___gatsby"',
    "id='___gatsby'",
    "__next_data__",
    "__nuxt__",
    "__next_f",
    'id="_r_"',
    "id='_r_'",
    "data-v-app",
    "data-svelte",
    "__sveltekit",
    "q:container",
    "astro-island",
    'id="app"',
    "id='app'",
)
JAVASCRIPT_REQUIRED_MARKERS = (
    "enable javascript",
    "requires javascript",
    "ensure javascript",
    "javascript is required",
    "javascript is disabled",
)
JAVASCRIPT_MARKER_VISIBLE_LIMIT = 2_000
JAVASCRIPT_SHELL_SCAN_CHARS = 512 * 1024
ACCESS_CHALLENGE_TITLE_MARKERS = (
    "access denied",
    "attention required",
    "checking your browser",
    "just a moment",
    "security check",
    "security verification",
)
ACCESS_CHALLENGE_TEXT_MARKERS = (
    "blocked by network security",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "verify you are human",
)
ACCESS_CHALLENGE_CHECKED_KEY = "_ketch_access_challenge_checked"
ERROR_CATEGORIES = {
    1: "internal",
    2: "validation",
    3: "not_found",
    4: "upstream",
    5: "precondition",
    6: "cancelled",
}
ARCHITECTURES = {
    "x86_64": {
        "release_arch": "x86_64",
        "archive_sha256": "e84b7667212aeb8b7bd9a92bc2a25f1c31185e2c6fba5a4cd97c2ace5a504c82",
    },
    "amd64": {
        "release_arch": "x86_64",
        "archive_sha256": "e84b7667212aeb8b7bd9a92bc2a25f1c31185e2c6fba5a4cd97c2ace5a504c82",
    },
    "aarch64": {
        "release_arch": "arm64",
        "archive_sha256": "61e0a7ef16350534c586d7e2755a490333334fc40bdeac01c092bf77bf030b8c",
    },
    "arm64": {
        "release_arch": "arm64",
        "archive_sha256": "61e0a7ef16350534c586d7e2755a490333334fc40bdeac01c092bf77bf030b8c",
    },
}
CHROMIUM_LIBRARIES = {
    "libasound.so.2": "libasound2",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libcairo.so.2": "libcairo2",
    "libcups.so.2": "libcups2",
    "libdbus-1.so.3": "libdbus-1-3",
    "libdrm.so.2": "libdrm2",
    "libexpat.so.1": "libexpat1",
    "libgbm.so.1": "libgbm1",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libnss3.so": "libnss3",
    "libnspr4.so": "libnspr4",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libX11.so.6": "libx11-6",
    "libxcb.so.1": "libxcb1",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXext.so.6": "libxext6",
    "libXfixes.so.3": "libxfixes3",
    "libXrandr.so.2": "libxrandr2",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libxshmfence.so.1": "libxshmfence1",
}
CHROMIUM_PACKAGES = tuple(dict.fromkeys(CHROMIUM_LIBRARIES.values()))
CHROME_FOR_TESTING_VERSION = "154.0.8037.57"
CHROME_FOR_TESTING_PLATFORM = "linux64"
CHROME_FOR_TESTING_ARCHITECTURES = frozenset({"x86_64", "amd64"})
CHROME_FOR_TESTING_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "154.0.8037.57/linux64/chrome-headless-shell-linux64.zip"
)
CHROME_FOR_TESTING_SHA256 = (
    "5a6979d0ab7cf952ea575d35164e7bdce4872b2ced8f8a215c8f8e8eda00ee09"
)
BROWSER_SANDBOX_CACHE = {"auto_without_no_sandbox": None}
SANDBOX_ERROR_MARKERS = (
    "no usable sandbox",
    "crbug.com/638180",
    "crbug.com/357670",
    "failed to move to new namespace",
    "setuid sandbox",
)
KEYLESS_SEARCH_BACKENDS = {
    "ddg",
    "exa",
    "firecrawl",
    "keenable",
    "parallel",
}


class KetchError(Exception):
    pass


class RedirectFailure(KetchError):
    """A redirect Ketch would not follow, or a redirect loop."""


def _browser_dependency_error(error: ImportError) -> KetchError:
    missing = getattr(error, "name", None) or str(error)
    if str(missing).split(".", 1)[0] == "playwright":
        return KetchError(
            "Browser rendering requires the Python playwright package, which is "
            "not installed (the Open WebUI main-slim image omits it). Ketch "
            f"declares {PLAYWRIGHT_REQUIREMENT} as a requirement; keep "
            "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS enabled, then restart "
            "Open WebUI or re-save the Ketch Tool."
        )
    return KetchError(
        f"Protected browser rendering dependencies are unavailable: {error}"
    )


def _require_free_space(path: Path, required_bytes: int, purpose: str) -> None:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return
    if free < required_bytes:
        mebibyte = 1024 * 1024
        raise KetchError(
            f"Not enough free disk space for {purpose}: {free // mebibyte} MiB "
            f"free at {path}, at least {required_bytes // mebibyte} MiB "
            "required. Free space on the Docker host disk, then retry."
        )


class AccessChallengeError(KetchError):
    pass


def _user_id(user: Any) -> str:
    value = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    return str(value).strip() if value is not None else ""


def _json_error(category: str, message: str, **details: Any) -> str:
    return json.dumps(
        {
            "status": "error",
            "category": category,
            "error": message,
            **details,
        },
        ensure_ascii=False,
    )


def _has_substantive_content(result: dict) -> bool:
    if result.get(ACCESS_CHALLENGE_CHECKED_KEY) is not True and _is_access_challenge(
        result
    ):
        return False
    content = result.get("markdown") or result.get("content") or result.get("text")
    if not isinstance(content, str):
        return False
    lines = content.splitlines()
    title = str(result.get("title") or "").strip().casefold()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.casefold() == title:
            continue
        if re.match(r"^#{1,6}\s", line) or re.fullmatch(r"[-=_* ]{3,}", line):
            continue
        if index + 1 < len(lines) and re.fullmatch(
            r"\s*(?:={3,}|-{3,})\s*", lines[index + 1]
        ):
            continue
        if re.fullmatch(r"(?:`{3,}|~{3,})\w*", line):
            continue
        without_links = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", line)
        if not without_links.strip(" \t-*+|/>\u00b7"):
            continue
        return True
    return False


def _scrape_status(results: list[dict], errors: list[dict]) -> str:
    has_incomplete_result = any(
        isinstance(result.get("pagination"), dict)
        and result["pagination"].get("truncated", False)
        for result in results
    )
    if results and (errors or has_incomplete_result):
        return "partial"
    if results:
        return "success"
    return "error"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _safe_install_root(raw_path: str) -> Path:
    data_root = Path("/app/backend/data").resolve()
    candidate = Path(raw_path).resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError as error:
        raise KetchError(
            "installation_root must be inside /app/backend/data."
        ) from error
    if candidate == data_root:
        raise KetchError(
            "installation_root must be a dedicated subdirectory of /app/backend/data."
        )
    return candidate


def _download_release(
    destination: Path,
    release_arch: str,
    expected_sha256: str,
) -> None:
    url = (
        f"https://github.com/1broseidon/ketch/releases/download/v{KETCH_VERSION}/"
        f"ketch_{KETCH_VERSION}_linux_{release_arch}.tar.gz"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"open-webui-ketch-tool/{KETCH_VERSION}"},
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > MAX_ARCHIVE_BYTES:
            raise KetchError("The Ketch release archive exceeds the allowed size.")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise KetchError(
                        "The Ketch release archive exceeds the allowed size."
                    )
                digest.update(chunk)
                output.write(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise KetchError(
            "Ketch release checksum verification failed; the archive was not installed."
        )


def _download_verified_file(
    url: str,
    destination: Path,
    expected_sha256: str,
    maximum_bytes: int,
    label: str,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"open-webui-ketch-tool/{TOOL_VERSION}"},
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > maximum_bytes:
            raise KetchError(f"{label} exceeds the allowed size.")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise KetchError(f"{label} exceeds the allowed size.")
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != expected_sha256:
        raise KetchError(
            f"{label} checksum verification failed; the archive was not installed."
        )


def _extract_ketch(archive_path: Path, destination: Path) -> str:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        candidates = [
            member
            for member in archive.getmembers()
            if member.isfile() and Path(member.name).name == "ketch"
        ]
        if len(candidates) != 1:
            raise KetchError("The Ketch archive did not contain exactly one binary.")
        member = candidates[0]
        if member.size <= 0 or member.size > MAX_BINARY_BYTES:
            raise KetchError("The Ketch binary has an invalid size.")
        source = archive.extractfile(member)
        if source is None:
            raise KetchError("The Ketch binary could not be read from its archive.")
        with destination.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    os.chmod(destination, 0o755)
    return _sha256_file(destination)


def _extract_chrome_headless_shell(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    executable: Optional[Path] = None
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            mode = (member.external_attr >> 16) & 0o777777
            if stat.S_ISLNK(mode):
                raise KetchError("Chrome archive contains a symbolic link.")
            target = (destination / name).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise KetchError(
                    "Chrome archive contains a path outside the install directory."
                ) from error
            total += member.file_size
            if total > MAX_BROWSER_EXTRACTED_BYTES:
                raise KetchError("Chrome archive exceeds the allowed extracted size.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if Path(name).name == "chrome-headless-shell":
                executable = target
                os.chmod(target, 0o755)
            else:
                os.chmod(target, 0o755 if mode & 0o111 else 0o644)
    if executable is None:
        raise KetchError("Chrome archive did not contain chrome-headless-shell.")
    if (
        executable.stat().st_size <= 0
        or executable.stat().st_size > MAX_BROWSER_BINARY_BYTES
    ):
        raise KetchError("Chrome headless shell executable has an invalid size.")
    for directory, _, _ in os.walk(destination):
        os.chmod(directory, 0o755)
    return executable


def _safe_remove_inside_root(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    try:
        target_resolved = target.resolve()
        target_resolved.relative_to(root_resolved)
    except (OSError, ValueError) as error:
        raise KetchError(
            "Refusing to remove a browser path outside the Ketch install root."
        ) from error
    if target_resolved == root_resolved:
        raise KetchError("Refusing to remove the Ketch install root.")
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
        return
    shutil.rmtree(target)


def _legacy_browser_cleanup_root(root: Path, executable_path: Path) -> Optional[Path]:
    try:
        resolved = executable_path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    names = {"chrome", "chromium", "chrome-headless-shell"}
    candidates = [resolved if resolved.is_dir() else resolved.parent, *resolved.parents]
    matches = []
    for candidate in candidates:
        if candidate == root:
            break
        lowered = candidate.name.casefold()
        if (
            lowered.startswith("chromium")
            or lowered.startswith("chrome-")
            or lowered in names
        ):
            matches.append(candidate)
    return matches[-1] if matches else None


def _missing_libraries() -> list[dict[str, str]]:
    missing = []
    for soname, package in CHROMIUM_LIBRARIES.items():
        try:
            ctypes.CDLL(soname)
        except OSError:
            missing.append({"library": soname, "package": package})
    return missing


def _validate_url_shape(raw_url: str) -> str:
    if not isinstance(raw_url, str):
        raise KetchError("Every scrape URL must be a string.")
    url = raw_url.strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise KetchError(f"Scrape URLs must contain 1-{MAX_URL_CHARS} characters.")
    if any(character in url for character in ("\\", "\t", "\r", "\n", "\x00")):
        raise KetchError("Scrape URL contains a parser-confusing character.")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise KetchError("Only absolute HTTP and HTTPS scrape URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise KetchError("Credentials embedded in scrape URLs are not allowed.")
    try:
        parsed.hostname.encode("idna")
        _ = parsed.port
    except (UnicodeError, ValueError) as error:
        raise KetchError("Scrape URL contains an invalid hostname or port.") from error
    return url


def _canonical_url_identity(raw_url: str) -> str:
    url = _validate_url_shape(raw_url)
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    netloc = (
        host
        if port is None
        or (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
        else f"{host}:{port}"
    )
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, "")
    )


def _redirect_target(current_url: str, location: Optional[str]) -> str:
    if not location:
        raise KetchError(f"{current_url} returned a redirect without a location.")
    return _validate_url_shape(urllib.parse.urljoin(current_url, location))


def _redirect_host(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname
        return host.encode("idna").decode("ascii").lower() if host else ""
    except (UnicodeError, ValueError):
        return ""


def _may_follow_redirect(
    current_url: str,
    redirect_url: str,
    *,
    allow_all: bool,
    follow_same_host: bool,
) -> bool:
    """Scheme and port may change on a same-host hop; hosts must match exactly."""
    if allow_all:
        return True
    if not follow_same_host:
        return False
    current_host = _redirect_host(current_url)
    return bool(current_host) and current_host == _redirect_host(redirect_url)


def _unfollowed_redirect_error(
    current_url: str,
    status: int,
    redirect_url: str,
    *,
    follow_same_host: bool,
) -> RedirectFailure:
    if follow_same_host:
        reason = (
            f"{current_url} returned HTTP {status} redirecting to a different "
            f"host, which Ketch does not follow automatically."
        )
    else:
        reason = (
            f"Redirects are disabled by Open WebUI configuration; {current_url} "
            f"returned HTTP {status}."
        )
    return RedirectFailure(
        f"{reason} Validated redirect destination: {redirect_url}. If needed, "
        f"submit that exact destination as a new URL; do not retry the "
        f"original URL."
    )


def _url_host(url: str) -> str:
    """Lowercase host of an already canonical URL, or ``""`` when absent."""
    try:
        return urllib.parse.urlsplit(url).netloc.casefold()
    except ValueError:
        return ""


def _compact_ketch_warnings(raw_warnings: str) -> str:
    compact: list[str] = []
    for raw_line in raw_warnings.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        line = re.sub(r"^warnings?:\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^warn:\s*", "", line, flags=re.IGNORECASE)
        backend_match = re.match(r"([a-zA-Z][a-zA-Z0-9_-]*):\s*(.*)", line)
        backend = backend_match.group(1).lower() if backend_match else ""
        detail = backend_match.group(2) if backend_match else line
        lowered = detail.lower()
        if "rate limit" in lowered:
            reason = (
                "rate limited after retries" if "retr" in lowered else "rate limited"
            )
        elif any(
            marker in lowered
            for marker in (
                "context deadline exceeded",
                "i/o timeout",
                "timed out",
                "timeout",
            )
        ):
            reason = "timed out after retries" if "retr" in lowered else "timed out"
        else:
            reason = re.sub(
                r"https?://[^\s\"']+",
                "<url>",
                detail,
                flags=re.IGNORECASE,
            )
            reason = re.sub(
                r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b",
                "<address>",
                reason,
            )
            reason = reason[:MAX_WARNING_ITEM_CHARS].rstrip()
        item = f"{backend}: {reason}" if backend else reason
        if item and item not in compact:
            compact.append(item)
        if len(compact) >= MAX_WARNING_ITEMS:
            break
    return "; ".join(compact)[:MAX_WARNING_CHARS].rstrip()


def _likely_javascript_shell(raw_html: str) -> bool:
    sample = raw_html[:JAVASCRIPT_SHELL_SCAN_CHARS]
    lower = sample.lower()
    closing_tags_present = {
        "script": "</script" in lower,
        "style": "</style" in lower,
    }
    visible_parts: list[str] = []
    script_bytes = 0
    index = 0
    length = len(sample)
    while index < length:
        tag_start = sample.find("<", index)
        if tag_start < 0:
            visible_parts.append(sample[index:])
            break
        if tag_start > index:
            visible_parts.append(sample[index:tag_start])
        tag_end = sample.find(">", tag_start + 1)
        if tag_end < 0:
            break
        tag = lower[tag_start + 1 : tag_end].lstrip()
        name = ""
        for candidate in ("script", "style"):
            boundary = tag[len(candidate) : len(candidate) + 1]
            if tag.startswith(candidate) and (
                len(tag) == len(candidate) or boundary.isspace() or boundary == "/"
            ):
                name = candidate
                break
        if name:
            close = (
                lower.find(f"</{name}", tag_end + 1)
                if closing_tags_present[name]
                else -1
            )
            if close < 0:
                closing_tags_present[name] = False
                index = tag_end + 1
                continue
            if name == "script":
                script_bytes += max(0, close - tag_end - 1)
            close_end = sample.find(">", close + 2 + len(name))
            index = length if close_end < 0 else close_end + 1
            continue
        index = tag_end + 1
    visible = html.unescape(" ".join(visible_parts))
    visible = " ".join(visible.split())
    if len(visible) < JAVASCRIPT_MARKER_VISIBLE_LIMIT and any(
        marker in visible.lower() for marker in JAVASCRIPT_REQUIRED_MARKERS
    ):
        return True
    if len(visible) >= 200:
        return False
    if any(marker in lower for marker in SPA_MARKERS):
        return True
    return script_bytes > max(len(visible), 1) * 3


def _is_access_challenge(extracted: dict[str, Any]) -> bool:
    title = str(extracted.get("title") or "").strip().lower()
    markdown = str(
        extracted.get("markdown")
        or extracted.get("content")
        or extracted.get("text")
        or ""
    ).strip()
    lower_markdown = markdown.lower()
    short_title_challenge = len(markdown) <= 2_000 and any(
        marker in title for marker in ACCESS_CHALLENGE_TITLE_MARKERS
    )
    short_body_challenge = len(markdown) <= 1_000 and any(
        marker in lower_markdown for marker in ACCESS_CHALLENGE_TEXT_MARKERS
    )
    return short_title_challenge or short_body_challenge


def _reject_access_challenge(extracted: dict[str, Any]) -> None:
    if _is_access_challenge(extracted):
        raise AccessChallengeError(
            "The page returned an access challenge instead of the requested "
            "content. Use a different source; do not retry it with Chromium."
        )
    extracted[ACCESS_CHALLENGE_CHECKED_KEY] = True


class _SharedByteBudget:
    def __init__(self, limit: int, description: str):
        self.remaining = limit
        self.description = description
        self.exhausted = False
        self.lock = asyncio.Lock()

    async def ensure_available(self) -> None:
        async with self.lock:
            if self.remaining <= 0:
                self.exhausted = True
                raise KetchError(
                    f"{self.description} exceeded the configured combined "
                    "download limit."
                )

    async def _reserve(self, requested: int) -> int:
        async with self.lock:
            if self.remaining <= 0:
                self.exhausted = True
                raise KetchError(
                    f"{self.description} exceeded the configured combined "
                    "download limit."
                )
            reserved = min(requested, self.remaining)
            self.remaining -= reserved
            return reserved

    async def _refund(self, unused: int) -> None:
        if unused <= 0:
            return
        async with self.lock:
            self.remaining += unused

    async def read_stream(
        self,
        stream: Any,
        per_resource_limit: int,
        description: str,
    ) -> bytes:
        chunks = []
        total = 0
        while True:
            requested = min(64 * 1024, per_resource_limit - total + 1)
            reserved = await self._reserve(requested)
            try:
                chunk = await stream.read(reserved)
            except BaseException:
                await self._refund(reserved)
                raise
            await self._refund(reserved - len(chunk))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > per_resource_limit:
                raise KetchError(
                    f"{description} exceeded the configured download limit."
                )


PDF_EOF_MARKER = b"%%EOF"
PDF_EOF_SEARCH_BYTES = 1_024
PDF_TRAILING_PADDING = b"\x00\t\n\x0c\r "


def _trim_pdf_trailing_padding(body: bytes) -> bytes:
    # Ketch's PDF reader requires %%EOF to be the last non-whitespace bytes, but
    # some servers append NUL padding that other readers ignore.
    tail_start = max(0, len(body) - PDF_EOF_SEARCH_BYTES)
    marker = body.rfind(PDF_EOF_MARKER, tail_start)
    if marker < 0:
        return body
    end = marker + len(PDF_EOF_MARKER)
    trailing = body[end:]
    if not trailing or trailing.strip(PDF_TRAILING_PADDING):
        return body
    return body[:end]


class _LocalContentServer:
    def __init__(self, body: bytes, content_type: str):
        self.body = body
        self.content_type = content_type
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()
        self.server: Optional[asyncio.AbstractServer] = None

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(
            self._handle,
            host="127.0.0.1",
            port=0,
            limit=16_384,
        )
        socket_value = self.server.sockets[0].getsockname()
        return f"http://127.0.0.1:{socket_value[1]}/{self.token}/document.pdf"

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=5,
            )
            request_line = request.split(b"\r\n", 1)[0].decode(
                "ascii",
                errors="replace",
            )
            parts = request_line.split()
            allowed_path = f"/{self.token}/document.pdf"
            if len(parts) != 3 or parts[0] != "GET" or parts[1] != allowed_path:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            else:
                headers = (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Type: {self.content_type}\r\n"
                    f"Content-Length: {len(self.body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                writer.write(headers)
                writer.write(self.body)
            await writer.drain()
        except (
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class _RejectingProxy:
    def __init__(self):
        self.server: Optional[asyncio.AbstractServer] = None

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(
            self._reject,
            host="127.0.0.1",
            port=0,
            limit=16_384,
        )
        socket_value = self.server.sockets[0].getsockname()
        return f"http://127.0.0.1:{socket_value[1]}"

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _reject(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await asyncio.wait_for(reader.read(16_384), timeout=2)
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class Tools:
    class Valves(BaseModel):
        installation_root: str = Field(
            default="/app/backend/data/ketch",
            description=(
                "Persistent Ketch runtime directory. It must be a dedicated "
                "subdirectory of /app/backend/data."
            ),
        )
        search_backends: str = Field(
            default="exa,keenable,parallel",
            description=(
                "Comma-separated keyless Ketch backends used for federated search."
            ),
        )
        install_system_dependencies: bool = Field(
            default=True,
            description=(
                "Install the fixed Chromium Debian package allowlist when required."
            ),
        )
        enable_browser: bool = Field(
            default=True,
            description="Install managed Chromium and enable JS-rendered page fallback.",
        )
        browser_sandbox: str = Field(
            default="auto",
            description=(
                "Chromium sandbox policy: auto tries sandboxing and falls back "
                "to --no-sandbox only when launch fails, required fails closed, "
                "off always uses --no-sandbox."
            ),
        )
        browser_jitless: bool = Field(
            default=True,
            description="Launch Chromium with --js-flags=--jitless.",
        )
        browser_unprivileged_uid: int = Field(
            default=65534,
            ge=1,
            le=2_147_483_647,
            description="Unprivileged Linux uid used to run Chromium.",
        )
        browser_unprivileged_gid: int = Field(
            default=65534,
            ge=1,
            le=2_147_483_647,
            description="Unprivileged Linux gid used to run Chromium.",
        )
        maximum_search_results: int = Field(
            default=10,
            ge=1,
            le=20,
            description="Maximum web results allowed in one search.",
        )
        maximum_search_snippet_characters: int = Field(
            default=500,
            ge=100,
            le=2_000,
            description="Maximum snippet characters returned per search result.",
        )
        maximum_scrape_urls: int = Field(
            default=3,
            ge=1,
            le=10,
            description="Maximum URLs allowed in one scrape call.",
        )
        maximum_search_calls_per_turn: int = Field(
            default=5,
            ge=1,
            le=20,
            description="Maximum search calls allowed for one user message.",
        )
        maximum_total_calls_per_turn: int = Field(
            default=16,
            ge=3,
            le=50,
            description="Maximum combined Ketch calls allowed for one user message.",
        )
        maximum_initial_scrape_calls_per_turn: int = Field(
            default=10,
            ge=1,
            le=30,
            description="Maximum non-continuation scrape calls per user message.",
        )
        maximum_document_scrape_calls_per_turn: int = Field(
            default=2,
            ge=1,
            le=5,
            description=(
                "Maximum opt-in full-document extraction calls per user message."
            ),
        )
        maximum_continuation_calls_per_turn: int = Field(
            default=10,
            ge=1,
            le=30,
            description="Maximum continuation tool calls per user message.",
        )
        maximum_continuation_chunks_per_turn: int = Field(
            default=10,
            ge=1,
            le=30,
            description=(
                "Maximum continuation chunks requested per user message, "
                "including invalid or unavailable identifiers."
            ),
        )
        maximum_page_characters: int = Field(
            default=600_000,
            ge=1_000,
            le=1_000_000,
            description=(
                "Maximum source Markdown characters extracted only when "
                "opt-in document mode is requested."
            ),
        )
        maximum_chunk_characters: int = Field(
            default=8_000,
            ge=1_000,
            le=8_000,
            description="Maximum Markdown characters returned per page in one call; never above 8000.",
        )
        maximum_result_characters: int = Field(
            default=120_000,
            ge=5_000,
            le=1_000_000,
            description="Maximum total stdout characters accepted from Ketch.",
        )
        maximum_download_bytes: int = Field(
            default=20 * 1024 * 1024,
            ge=1 * 1024 * 1024,
            le=50 * 1024 * 1024,
            description="Maximum bytes accepted for one page or browser resource.",
        )
        maximum_browser_resource_bytes: int = Field(
            default=60 * 1024 * 1024,
            ge=5 * 1024 * 1024,
            le=250 * 1024 * 1024,
            description="Maximum combined browser resource bytes accepted per page.",
        )
        command_timeout_seconds: int = Field(
            default=90,
            ge=10,
            le=600,
            description="Maximum runtime for one search or scrape command.",
        )
        follow_same_host_redirects: bool = Field(
            default=True,
            description=(
                "Follow redirects whose destination has exactly the same host, "
                "even when Open WebUI's AIOHTTP_CLIENT_ALLOW_REDIRECTS is off. "
                "Every hop is still SSRF-validated. Redirects to another host "
                "are returned to the model as a validated destination."
            ),
        )
        bootstrap_timeout_seconds: int = Field(
            default=300,
            ge=60,
            le=900,
            description="Maximum runtime for each dependency or browser setup command.",
        )
        bootstrap_retry_seconds: int = Field(
            default=60,
            ge=0,
            le=3_600,
            description="Delay before retrying a failed runtime bootstrap.",
        )
        show_status: bool = Field(
            default=True,
            description="Show status events while Ketch initializes and runs.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _emit_status(self, event_emitter, description: str, done: bool) -> None:
        if self.valves.show_status and event_emitter is not None:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "hidden": False,
                    },
                }
            )

    async def _emit_error(self, event_emitter, content: str) -> None:
        if event_emitter is not None:
            await event_emitter(
                {
                    "type": "notification",
                    "data": {"type": "error", "content": content},
                }
            )

    def _runtime_fingerprint(self) -> str:
        value = {
            "root": self.valves.installation_root,
            "version": KETCH_VERSION,
            "browser": self.valves.enable_browser,
            "dependencies": self.valves.install_system_dependencies,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _state(self, request: Request) -> dict:
        state = getattr(request.app.state, STATE_KEY, None)
        fingerprint = self._runtime_fingerprint()
        if not isinstance(state, dict) or state.get("fingerprint") != fingerprint:
            lock = state.get("lock") if isinstance(state, dict) else None
            if lock is None or not hasattr(lock, "acquire"):
                lock = asyncio.Lock()
            state = {
                "fingerprint": fingerprint,
                "lock": lock,
                "ready": False,
                "runtime": None,
                "last_error": None,
                "failed_requirement": None,
                "failed_at": 0.0,
            }
            setattr(request.app.state, STATE_KEY, state)
        return state

    def _usage_state(self, request: Request) -> dict:
        state = getattr(request.app.state, USAGE_STATE_KEY, None)
        if not isinstance(state, dict):
            state = {
                "lock": asyncio.Lock(),
                "records": {},
            }
            setattr(request.app.state, USAGE_STATE_KEY, state)
        return state

    def _document_cache_state(self, request: Request) -> dict:
        state = getattr(request.app.state, DOCUMENT_CACHE_STATE_KEY, None)
        if not isinstance(state, dict):
            state = {
                "lock": asyncio.Lock(),
                "entries": {},
                "extraction_semaphore": asyncio.Semaphore(
                    MAX_CONCURRENT_DOCUMENT_EXTRACTIONS
                ),
            }
            setattr(request.app.state, DOCUMENT_CACHE_STATE_KEY, state)
        return state

    @staticmethod
    def _prune_document_cache(entries: dict, now: float) -> None:
        for document_id, entry in list(entries.items()):
            if (
                not isinstance(entry, dict)
                or now - entry.get("last_used", 0.0) >= USAGE_TTL_SECONDS
            ):
                entries.pop(document_id, None)

    @staticmethod
    def _document_cache_size(entry: dict) -> int:
        markdown = entry.get("markdown")
        return len(markdown) if isinstance(markdown, str) else 0

    @staticmethod
    def _merge_document_interval(
        intervals: list[tuple[int, int]],
        start: int,
        end: int,
    ) -> list[tuple[int, int]]:
        merged = []
        for current_start, current_end in sorted([*intervals, (start, end)]):
            if not merged or current_start > merged[-1][1]:
                merged.append((current_start, current_end))
            else:
                merged[-1] = (
                    merged[-1][0],
                    max(merged[-1][1], current_end),
                )
        return merged

    async def _store_document_cache(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        markdown: str,
        source: dict,
    ) -> str:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            raise KetchError("Document mode requires authenticated user-message scope.")
        user_id, message_key = usage_key
        if len(markdown) > self.valves.maximum_page_characters:
            raise KetchError(
                "Document extraction exceeded the configured page ceiling."
            )
        if (
            len(markdown) > MAX_DOCUMENT_CACHE_CHARACTERS_PER_USER
            or len(markdown) > MAX_DOCUMENT_CACHE_CHARACTERS_GLOBAL
        ):
            raise KetchError(
                "Document extraction exceeded the configured cache capacity."
            )

        state = self._document_cache_state(request)
        now = time.monotonic()
        async with state["lock"]:
            entries = state["entries"]
            self._prune_document_cache(entries, now)

            def user_entries() -> list[tuple[str, dict]]:
                return sorted(
                    (
                        (document_id, entry)
                        for document_id, entry in entries.items()
                        if entry.get("user_id") == user_id
                    ),
                    key=lambda item: item[1].get("last_used", 0.0),
                )

            current_user_entries = user_entries()
            current_user_characters = sum(
                self._document_cache_size(entry) for _, entry in current_user_entries
            )
            while current_user_entries and (
                len(current_user_entries) >= MAX_DOCUMENT_CACHE_ITEMS_PER_USER
                or current_user_characters + len(markdown)
                > MAX_DOCUMENT_CACHE_CHARACTERS_PER_USER
            ):
                oldest_id, oldest = current_user_entries.pop(0)
                current_user_characters -= self._document_cache_size(oldest)
                entries.pop(oldest_id, None)

            def global_characters() -> int:
                return sum(
                    self._document_cache_size(entry) for entry in entries.values()
                )

            while entries and (
                len(entries) >= MAX_DOCUMENT_CACHE_ITEMS_GLOBAL
                or global_characters() + len(markdown)
                > MAX_DOCUMENT_CACHE_CHARACTERS_GLOBAL
            ):
                oldest_id = min(
                    entries,
                    key=lambda document_id: entries[document_id].get("last_used", 0.0),
                )
                entries.pop(oldest_id, None)

            document_id = ""
            while not document_id or document_id in entries:
                document_id = "kd_" + secrets.token_urlsafe(18)
            entries[document_id] = {
                "user_id": user_id,
                "message_key": message_key,
                "last_used": now,
                "markdown": markdown,
                "source": dict(source),
                "read_intervals": self._merge_document_interval(
                    [],
                    0,
                    min(
                        len(markdown),
                        int(source.get("initial_characters", 0)),
                    ),
                ),
            }
        return document_id

    async def _read_document_cache(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        document_id: str,
        start_char: int,
        max_chars: int,
    ) -> Optional[dict]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, message_key = usage_key
        state = self._document_cache_state(request)
        now = time.monotonic()
        async with state["lock"]:
            entries = state["entries"]
            self._prune_document_cache(entries, now)
            entry = entries.get(document_id)
            if (
                not isinstance(entry, dict)
                or entry.get("user_id") != user_id
                or entry.get("message_key") != message_key
            ):
                return None
            entry["last_used"] = now
            markdown = entry.get("markdown")
            if not isinstance(markdown, str):
                entries.pop(document_id, None)
                return None
            end_char = min(len(markdown), start_char + max_chars)
            if not markdown[start_char:end_char].strip():
                if start_char:
                    raise KetchError(
                        f"start_char {start_char} is beyond the readable "
                        "extracted content."
                    )
                raise KetchError("Ketch extracted no readable content from the page.")
            intervals = entry.get("read_intervals")
            if not isinstance(intervals, list):
                intervals = []
            intervals = self._merge_document_interval(
                intervals,
                start_char,
                end_char,
            )
            entry["read_intervals"] = intervals
            source = dict(entry.get("source", {}))
            return {
                "markdown": markdown,
                "source": source,
                "content_complete": bool(
                    not source.get("source_truncated")
                    and intervals
                    and intervals[0][0] == 0
                    and intervals[0][1] >= len(markdown)
                ),
            }

    async def _remove_document_cache(
        self,
        request: Request,
        document_ids: list[str],
    ) -> None:
        if not document_ids:
            return
        state = self._document_cache_state(request)
        async with state["lock"]:
            for document_id in document_ids:
                state["entries"].pop(document_id, None)

    def _turn_usage_key(
        self,
        user: Any,
        metadata: Optional[dict],
    ) -> Optional[tuple[str, str]]:
        user_id = _user_id(user)
        if not user_id:
            return None
        meta = metadata if isinstance(metadata, dict) else {}
        chat_id = meta.get("chat_id") or "missing-chat"
        message_id = meta.get("message_id") or "missing-message"
        raw_key = json.dumps(
            [user_id, str(chat_id), str(message_id)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return user_id, hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _prune_usage_records(
        self,
        records: dict,
        now: float,
        user_id: str,
        current_key: str,
    ) -> None:
        expired = [
            key
            for key, record in records.items()
            if now - record.get("last_used", 0.0) >= USAGE_TTL_SECONDS
        ]
        for key in expired:
            records.pop(key, None)

        user_records = sorted(
            (
                (key, record)
                for key, record in records.items()
                if record.get("user_id") == user_id and key != current_key
            ),
            key=lambda item: item[1].get("last_used", 0.0),
        )
        while len(user_records) >= MAX_USAGE_RECORDS_PER_USER:
            key, _ = user_records.pop(0)
            records.pop(key, None)

        while len(records) >= MAX_USAGE_RECORDS_GLOBAL and current_key not in records:
            oldest_key = min(
                records,
                key=lambda key: records[key].get("last_used", 0.0),
            )
            records.pop(oldest_key, None)

    def _usage_record(
        self,
        records: dict,
        user_id: str,
        key: str,
        now: float,
    ) -> dict:
        record = records.get(key)
        if not isinstance(record, dict):
            record = {
                "user_id": user_id,
                "last_used": now,
                "total_calls": 0,
                "search_calls": 0,
                "initial_scrape_calls": 0,
                "document_scrape_calls": 0,
                "continuation_calls": 0,
                "continuation_chunks": 0,
                "search_signatures": set(),
                "search_fingerprints": [],
                "scrape_signatures": set(),
                "invalid_signatures": set(),
                "completed_chunks": set(),
                "failed_urls": set(),
                "emitted_source_urls": set(),
                "offered_urls": set(),
                "inspected_urls": set(),
                "failed_hosts": {},
                "source_event_claims": {},
            }
            records[key] = record
        for count_key in (
            "total_calls",
            "search_calls",
            "initial_scrape_calls",
            "document_scrape_calls",
            "continuation_calls",
            "continuation_chunks",
        ):
            if not isinstance(record.get(count_key), int):
                record[count_key] = 0
        for set_key in (
            "search_signatures",
            "scrape_signatures",
            "invalid_signatures",
            "completed_chunks",
            "failed_urls",
            "emitted_source_urls",
            "offered_urls",
            "inspected_urls",
        ):
            if not isinstance(record.get(set_key), set):
                record[set_key] = set()
        if not isinstance(record.get("failed_hosts"), dict):
            record["failed_hosts"] = {}
        if not isinstance(record.get("search_fingerprints"), list):
            record["search_fingerprints"] = []
        if not isinstance(record.get("continuation_requests"), dict):
            record["continuation_requests"] = {}
        if not isinstance(record.get("source_evidence_levels"), dict):
            record["source_evidence_levels"] = {}
        if not isinstance(record.get("source_event_claims"), dict):
            record["source_event_claims"] = {}
        record["user_id"] = user_id
        record["last_used"] = now
        return record

    async def _emit_source_event(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter,
        source: dict,
        level: int,
    ) -> bool:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None or event_emitter is None:
            return False
        user_id, key = usage_key
        state = self._usage_state(request)
        url = source["source"]["id"]
        async with state["lock"]:
            now = time.monotonic()
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            levels = record["source_evidence_levels"]
            previous = levels.get(url, 1 if url in record["emitted_source_urls"] else 0)
            claims = record["source_event_claims"]
            if previous >= level or claims.get(url, 0) >= level:
                return False
            claims[url] = level
        try:
            await asyncio.wait_for(
                event_emitter({"type": "source", "data": source}),
                timeout=5,
            )
        except Exception:
            log.exception("Ketch could not emit source metadata.")
            async with state["lock"]:
                record = state["records"].get(key)
                if isinstance(record, dict):
                    claims = record.get("source_event_claims")
                    if isinstance(claims, dict) and claims.get(url) == level:
                        claims.pop(url, None)
            return False
        async with state["lock"]:
            record = state["records"].get(key)
            if not isinstance(record, dict):
                return False
            levels = record["source_evidence_levels"]
            previous = levels.get(url, 1 if url in record["emitted_source_urls"] else 0)
            if previous >= level:
                claims = record.get("source_event_claims")
                if isinstance(claims, dict) and claims.get(url) == level:
                    claims.pop(url, None)
                return False
            levels[url] = level
            record["emitted_source_urls"].add(url)
            claims = record.get("source_event_claims")
            if isinstance(claims, dict) and claims.get(url) == level:
                claims.pop(url, None)
        return True

    async def _emit_scrape_sources(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter,
        results: list[dict],
    ) -> int:
        if event_emitter is None:
            return 0
        by_url = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            if not _has_substantive_content(result):
                continue
            url = result.get("resolved_url") or result.get("url")
            if not isinstance(url, str) or not url:
                continue
            by_url[url] = result
        emitted = 0
        for url, result in by_url.items():
            title = str(result.get("title") or url)[:300]
            document = str(
                result.get("markdown")
                or result.get("content")
                or result.get("text")
                or ""
            )[:1_000]
            pagination = result.get("pagination")
            content_complete = (
                bool(result.get("content_complete"))
                if "content_complete" in result
                else bool(
                    isinstance(pagination, dict)
                    and not pagination.get("truncated", False)
                )
            )
            emitted += await self._emit_source_event(
                request,
                user,
                metadata,
                event_emitter,
                {
                    "source": {"id": url, "name": title, "type": "web"},
                    "document": [document],
                    "metadata": [
                        {
                            "source": url,
                            "name": title,
                            "url": url,
                            "evidence_type": "page_extract",
                            "content_complete": content_complete,
                        }
                    ],
                },
                2 if content_complete else 1,
            )
        return emitted

    @staticmethod
    def _usage_signature(value: Any) -> str:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _invalid_usage_signature(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> Optional[str]:
        try:
            return self._usage_signature(
                {
                    "operation": operation,
                    "arguments": arguments,
                }
            )
        except (TypeError, ValueError, RecursionError):
            return None

    @staticmethod
    def _search_query_fingerprint(
        query: str,
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        terms = []
        identifier_terms = []
        raw_tokens = re.findall(
            r"[^\W_]+(?:[.+#-][^\W_]*)?",
            query,
            flags=re.UNICODE,
        )
        for raw_token in raw_tokens:
            token = raw_token.casefold()
            candidates = [token]
            if "-" in token and any(character.isalpha() for character in token):
                candidates.extend(part for part in token.split("-") if part)
            for candidate in candidates:
                normalized = SEARCH_QUERY_TERM_ALIASES.get(candidate, candidate)
                if normalized in SEARCH_QUERY_STOP_WORDS:
                    continue
                terms.append(normalized)
            has_lower_seen = False
            mixed_case_identifier = False
            for character in raw_token:
                if character.islower():
                    has_lower_seen = True
                elif character.isupper() and has_lower_seen:
                    mixed_case_identifier = True
                    break
            if len(raw_token) > 1 and (
                mixed_case_identifier
                or any(character.isdigit() for character in raw_token)
                or any(character in ".+#_" for character in raw_token)
            ):
                identifier_terms.append(SEARCH_QUERY_TERM_ALIASES.get(token, token))
        bounded = sorted(set(terms))[:MAX_SEARCH_FINGERPRINT_TERMS]
        bounded_identifiers = sorted(set(identifier_terms))[
            :MAX_SEARCH_FINGERPRINT_TERMS
        ]
        return (
            frozenset(
                hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
                for term in bounded
            ),
            frozenset(
                hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
                for term in bounded
                if term not in SEARCH_QUERY_GENERIC_TERMS
            ),
            frozenset(
                hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
                for term in bounded_identifiers
            ),
        )

    @staticmethod
    def _search_fingerprints_equivalent(
        left: Any,
        right: Any,
    ) -> bool:
        left_all, left_core, left_identifiers = left
        right_all, right_core, right_identifiers = right
        if not left_all or not right_all:
            return False
        if left_all == right_all:
            return True
        if (
            left_identifiers
            and right_identifiers
            and not left_identifiers & right_identifiers
        ):
            return False
        nonidentifier_distinct_both = (
            not left_identifiers
            and not right_identifiers
            and left_core
            and right_core
            and left_core - right_core
            and right_core - left_core
        )
        overlap = len(left_all & right_all)
        smaller = min(len(left_all), len(right_all))
        union = len(left_all | right_all)
        if (
            not nonidentifier_distinct_both
            and overlap >= 2
            and overlap / smaller >= 0.75
            and overlap / union > 0.60
        ):
            return True
        if not left_core or not right_core:
            return False
        core_overlap = len(left_core & right_core)
        if nonidentifier_distinct_both:
            return False
        return (
            core_overlap >= 3
            and core_overlap / min(len(left_core), len(right_core)) >= 0.75
            and core_overlap / len(left_core | right_core) >= 0.40
        )

    def _reserve_total_usage(self, record: dict) -> Optional[str]:
        if record["total_calls"] >= self.valves.maximum_total_calls_per_turn:
            return (
                "Total Ketch call budget exhausted for this user message. "
                "Mandatory stop: do not call any Ketch tool again. Answer now "
                "from the evidence already collected and disclose any "
                "remaining limitation."
            )
        record["total_calls"] += 1
        return None

    async def _reserve_search_usage(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        query: str,
    ) -> Optional[str]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        signature = self._usage_signature(" ".join(query.lower().split()))
        fingerprint = self._search_query_fingerprint(query)
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            total_error = self._reserve_total_usage(record)
            if total_error:
                return total_error
            record["search_calls"] += 1
            if record["search_calls"] > self.valves.maximum_search_calls_per_turn:
                return (
                    "Search call budget exhausted for this user message. Stop "
                    "using Ketch and answer from the evidence already collected. "
                    "Search coverage is non-exhaustive: do not claim that no "
                    "matches exist or that latest/top results were established."
                )
            if signature in record["search_signatures"]:
                return (
                    "This search query was already attempted in this user "
                    "message. Do not retry it; use the existing results or answer."
                )
            if any(
                self._search_fingerprints_equivalent(fingerprint, previous)
                for previous in record["search_fingerprints"]
            ):
                return (
                    "A substantially equivalent search query was already attempted "
                    "in this user message. Do not rephrase and retry it; scrape the "
                    "best existing result, use existing evidence, or answer."
                )
            record["search_signatures"].add(signature)
            record["search_fingerprints"].append(fingerprint)
            del record["search_fingerprints"][
                : -self.valves.maximum_search_calls_per_turn
            ]
        return None

    async def _reserve_invalid_usage(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        operation: str,
        continuation_chunks: int = 0,
        signature: Optional[str] = None,
    ) -> Optional[str]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        if operation == "search":
            count_key = "search_calls"
            limit = self.valves.maximum_search_calls_per_turn
            limit_message = (
                "Search call budget exhausted for this user message. Stop "
                "using Ketch and answer from the evidence already collected. "
                "Search coverage is non-exhaustive: do not claim that no "
                "matches exist or that latest/top results were established."
            )
        elif operation == "continuation":
            count_key = "continuation_calls"
            limit = self.valves.maximum_continuation_calls_per_turn
            limit_message = (
                "Continuation scrape call budget exhausted for this user "
                "message. Stop using Ketch and answer from the evidence "
                "already collected."
            )
        else:
            count_key = "initial_scrape_calls"
            limit = self.valves.maximum_initial_scrape_calls_per_turn
            limit_message = (
                "Initial scrape call budget exhausted for this user message. "
                "Stop using Ketch and answer from the evidence already collected."
            )
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            total_error = self._reserve_total_usage(record)
            if total_error:
                return total_error
            record[count_key] += 1
            if record[count_key] > limit:
                return limit_message
            if operation == "continuation":
                record["continuation_chunks"] += max(1, continuation_chunks)
                if (
                    record["continuation_chunks"]
                    > self.valves.maximum_continuation_chunks_per_turn
                ):
                    return (
                        "Continuation chunk budget exhausted for this user "
                        "message. Stop using Ketch and answer from the evidence "
                        "already collected."
                    )
            if signature is not None:
                if signature in record["invalid_signatures"]:
                    return (
                        "This exact invalid Ketch call was already rejected in "
                        "this user message. Do not retry unchanged arguments; "
                        "correct them once or stop using Ketch."
                    )
                record["invalid_signatures"].add(signature)
        return None

    async def _reserve_scrape_usage(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        urls: list[str],
        force_browser: bool,
        max_chars: int,
        document_mode: bool = False,
    ) -> tuple[Optional[str], Optional[str], list[str], list[dict]]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None, None, list(urls), []
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        canonical_urls = {url: _canonical_url_identity(url) for url in urls}
        signature = self._usage_signature(
            {
                "urls": sorted(canonical_urls.values()),
                "force_browser": force_browser,
                "max_chars": max_chars,
                "document_mode": document_mode,
            }
        )
        url_signatures = {
            url: self._usage_signature(canonical_urls[url]) for url in urls
        }
        chunk_signatures = {
            url: self._usage_signature(
                [canonical_urls[url], force_browser, 0, document_mode]
            )
            for url in urls
        }
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            total_error = self._reserve_total_usage(record)
            if total_error:
                return key, total_error, [], []
            record["initial_scrape_calls"] += 1
            if (
                record["initial_scrape_calls"]
                > self.valves.maximum_initial_scrape_calls_per_turn
            ):
                return (
                    key,
                    (
                        "Initial scrape call budget exhausted "
                        "for this user message. Stop using Ketch and answer from "
                        "the evidence already collected."
                    ),
                    [],
                    [],
                )
            if document_mode:
                record["document_scrape_calls"] += 1
                if (
                    record["document_scrape_calls"]
                    > self.valves.maximum_document_scrape_calls_per_turn
                ):
                    return (
                        key,
                        (
                            "Document scrape call budget exhausted for this user "
                            "message. Use an existing document map, perform a normal "
                            "targeted scrape, or answer from the evidence already "
                            "collected."
                        ),
                        [],
                        [],
                    )
            if signature in record["scrape_signatures"]:
                return (
                    key,
                    (
                        "This exact scrape call was already attempted in this user "
                        "message. Do not retry it; use the existing result or answer."
                    ),
                    [],
                    [],
                )
            failed = [
                url
                for url, url_signature in url_signatures.items()
                if url_signature in record["failed_urls"]
            ]
            fabricated = [
                url
                for url in urls
                if url not in failed
                and url_signatures[url] not in record["offered_urls"]
                and record["failed_hosts"].get(_url_host(canonical_urls[url]), 0)
                >= FABRICATED_URL_HOST_FAILURE_THRESHOLD
            ]
            repeated_chunks = [
                url
                for url, chunk_signature in chunk_signatures.items()
                if chunk_signature in record["completed_chunks"]
            ]
            blocked = set(failed) | set(repeated_chunks) | set(fabricated)
            eligible = [url for url in urls if url not in blocked]
            if not eligible:
                if fabricated and not repeated_chunks:
                    error = (
                        "Several composed URLs on this host already failed in "
                        "this user message, so composed paths there are now "
                        f"refused: {', '.join(fabricated + failed)}. Submit only "
                        "a URL returned by ketch_search or a validated redirect "
                        "destination, use a different source, or answer with the "
                        "evidence already collected."
                    )
                elif failed and not repeated_chunks:
                    error = (
                        "These URLs already failed in this user message and must "
                        f"not be retried: {', '.join(failed)}. Use another source "
                        "or answer with the available evidence."
                    )
                elif repeated_chunks and not failed:
                    error = (
                        "This page chunk was already retrieved for: "
                        f"{', '.join(repeated_chunks)}. Do not refetch earlier "
                        "content; use the existing result or the exact next "
                        "continuation request."
                    )
                else:
                    error = (
                        "Every submitted URL was already failed or retrieved in "
                        "this user message. Do not retry them; use existing "
                        "evidence, another source, or answer."
                    )
                return key, error, [], []
            blocked_errors = (
                [
                    {
                        "url": url,
                        "category": "policy",
                        "error": (
                            "URL already failed in this user message and was omitted "
                            "from this mixed batch. Do not retry it."
                        ),
                    }
                    for url in failed
                ]
                + [
                    {
                        "url": url,
                        "category": "policy",
                        "error": (
                            "Composed URL omitted: earlier composed paths on this "
                            "host already failed in this user message. Submit only a "
                            "URL returned by ketch_search or a validated redirect "
                            "destination."
                        ),
                    }
                    for url in fabricated
                ]
                + [
                    {
                        "url": url,
                        "category": "policy",
                        "error": (
                            "Page chunk already retrieved in this user message and was "
                            "omitted from this mixed batch. Use the existing result or "
                            "its exact continuation request."
                        ),
                    }
                    for url in repeated_chunks
                ]
            )
            record["scrape_signatures"].add(signature)
        return key, None, eligible, blocked_errors

    def _remove_pending_continuations(
        self,
        record: dict,
        url_signature: str,
    ) -> None:
        pending = record.setdefault("continuation_requests", {})
        for continuation_id, continuation in list(pending.items()):
            if (
                not isinstance(continuation, dict)
                or continuation.get("url_signature") == url_signature
            ):
                pending.pop(continuation_id, None)

    async def _reserve_continuation_usage(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        continuation_ids: list[str],
    ) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return (
                None,
                (
                    "Continuation state is unavailable without authenticated "
                    "user-message scope."
                ),
                [],
                [],
            )
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            total_error = self._reserve_total_usage(record)
            if total_error:
                return key, total_error, [], []
            record["continuation_calls"] += 1
            if (
                record["continuation_calls"]
                > self.valves.maximum_continuation_calls_per_turn
            ):
                return (
                    key,
                    (
                        "Continuation scrape call budget exhausted for this user "
                        "message. Stop using Ketch and answer from the evidence "
                        "already collected."
                    ),
                    [],
                    [],
                )
            record["continuation_chunks"] += len(continuation_ids)
            if (
                record["continuation_chunks"]
                > self.valves.maximum_continuation_chunks_per_turn
            ):
                return (
                    key,
                    (
                        "Continuation chunk budget exhausted for this user message. "
                        "Stop using Ketch and answer from the evidence already collected."
                    ),
                    [],
                    [],
                )

            pending = record["continuation_requests"]
            requests = []
            errors = []
            for continuation_id in continuation_ids:
                continuation = pending.get(continuation_id)
                if not isinstance(continuation, dict):
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "No pending continuation with this identifier "
                                "exists in this user message. Do not guess or "
                                "reuse continuation identifiers."
                            ),
                        }
                    )
                    continue
                if continuation.get("_claimed") is True:
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "This continuation identifier is already being "
                                "processed. Use existing evidence or wait for "
                                "the current result."
                            ),
                        }
                    )
                    continue
                url = continuation.get("url")
                url_signature = continuation.get("url_signature")
                start_char = continuation.get("start_char")
                max_chars = continuation.get("max_chars")
                force_browser = continuation.get("force_browser")
                document_id = continuation.get("document_id")
                valid = (
                    isinstance(url, str)
                    and isinstance(url_signature, str)
                    and isinstance(start_char, int)
                    and not isinstance(start_char, bool)
                    and 0 < start_char < self.valves.maximum_page_characters
                    and isinstance(max_chars, int)
                    and not isinstance(max_chars, bool)
                    and 1_000 <= max_chars <= self.valves.maximum_chunk_characters
                    and isinstance(force_browser, bool)
                    and (
                        document_id is None
                        or (
                            isinstance(document_id, str)
                            and document_id.startswith("kd_")
                        )
                    )
                )
                if not valid:
                    pending.pop(continuation_id, None)
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "This continuation is unavailable or was created "
                                "by an incompatible Ketch version."
                            ),
                        }
                    )
                    continue
                if url_signature in record["failed_urls"]:
                    pending.pop(continuation_id, None)
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "The continuation source already failed in this "
                                "user message and must not be retried."
                            ),
                        }
                    )
                    continue
                try:
                    canonical_url = _canonical_url_identity(url)
                except KetchError:
                    pending.pop(continuation_id, None)
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "This continuation is unavailable or was created "
                                "by an incompatible Ketch version."
                            ),
                        }
                    )
                    continue
                chunk_signature = self._usage_signature(
                    [
                        canonical_url,
                        force_browser,
                        start_char,
                        isinstance(document_id, str),
                    ]
                )
                if chunk_signature in record["completed_chunks"]:
                    pending.pop(continuation_id, None)
                    errors.append(
                        {
                            "continuation_id": continuation_id,
                            "category": "policy",
                            "error": (
                                "This continuation chunk was already retrieved. "
                                "Use the existing evidence or the newly issued "
                                "continuation identifier."
                            ),
                        }
                    )
                    continue
                continuation["_claimed"] = True
                request_spec = {
                    "url": url,
                    "url_signature": url_signature,
                    "start_char": start_char,
                    "max_chars": max_chars,
                    "force_browser": force_browser,
                    "continuation_id": continuation_id,
                }
                if isinstance(document_id, str):
                    request_spec["document_id"] = document_id
                requests.append(request_spec)
            if not requests:
                return (
                    key,
                    (
                        "None of the supplied continuation identifiers is pending "
                        "for this user message. Use existing evidence or answer with "
                        "the available sources."
                    ),
                    [],
                    errors,
                )
        return key, None, requests, errors

    async def _rollback_continuation_claims(
        self,
        request: Request,
        usage_key: Optional[str],
        request_specs: list[dict],
    ) -> None:
        if usage_key is None:
            return
        continuation_ids = [
            spec.get("continuation_id")
            for spec in request_specs
            if isinstance(spec, dict) and isinstance(spec.get("continuation_id"), str)
        ]
        if not continuation_ids:
            return
        state = self._usage_state(request)
        async with state["lock"]:
            record = state["records"].get(usage_key)
            if not isinstance(record, dict):
                return
            pending = record.get("continuation_requests")
            if not isinstance(pending, dict):
                return
            for continuation_id in continuation_ids:
                continuation = pending.get(continuation_id)
                if isinstance(continuation, dict):
                    continuation.pop("_claimed", None)

    async def _record_scrape_usage(
        self,
        request: Request,
        usage_key: Optional[str],
        results: list[dict],
        errors: list[dict],
        request_specs: list[dict],
    ) -> None:
        if usage_key is None:
            return
        state = self._usage_state(request)
        async with state["lock"]:
            record = state["records"].get(usage_key)
            if not isinstance(record, dict):
                return
            record["last_used"] = time.monotonic()
            specs_by_url = {
                spec["url"]: spec
                for spec in request_specs
                if isinstance(spec, dict) and isinstance(spec.get("url"), str)
            }
            specs_by_index = {
                index: spec
                for index, spec in enumerate(request_specs)
                if isinstance(spec, dict)
            }
            pending = record.get("continuation_requests")
            for result in results:
                requested_url = result.get("requested_url")
                if isinstance(requested_url, str):
                    try:
                        requested_identity = _canonical_url_identity(requested_url)
                    except KetchError:
                        continue
                    completed_identities = {requested_identity}
                    resolved_url = result.get("resolved_url")
                    if isinstance(resolved_url, str):
                        try:
                            completed_identities.add(
                                _canonical_url_identity(resolved_url)
                            )
                        except KetchError:
                            pass
                    spec_index = result.get("_ketch_request_index")
                    spec = (
                        specs_by_index.get(spec_index, {})
                        if isinstance(spec_index, int)
                        and not isinstance(spec_index, bool)
                        else specs_by_url.get(requested_url, {})
                    )
                    start_char = spec.get("start_char", 0)
                    requested_force_browser = spec.get("force_browser", False)
                    document_mode = bool(spec.get("document_mode")) or isinstance(
                        spec.get("document_id"),
                        str,
                    )
                    continuation_id = spec.get("continuation_id")
                    if isinstance(continuation_id, str) and isinstance(pending, dict):
                        pending.pop(continuation_id, None)
                    # A page already rendered by the browser must not be
                    # re-rendered by a later force_browser=true request.
                    force_browser_variants = {requested_force_browser}
                    if result.get("retrieval_mode") == "protected_browser":
                        force_browser_variants.add(True)
                    record["completed_chunks"].update(
                        self._usage_signature(
                            [
                                identity,
                                force_browser_variant,
                                start_char,
                                document_mode,
                            ]
                        )
                        for identity in completed_identities
                        for force_browser_variant in force_browser_variants
                    )
                    url_signature = self._usage_signature(requested_identity)
                    if start_char == 0:
                        self._remove_pending_continuations(
                            record,
                            url_signature,
                        )
                    pagination = result.get("pagination")
                    continuation = (
                        pagination.get("continuation_request")
                        if isinstance(pagination, dict)
                        else None
                    )
                    continuation_ids = (
                        continuation.get("continuation_ids")
                        if isinstance(continuation, dict)
                        else None
                    )
                    next_start_char = (
                        pagination.get("next_start_char")
                        if isinstance(pagination, dict)
                        else None
                    )
                    if (
                        isinstance(continuation_ids, list)
                        and len(continuation_ids) == 1
                        and isinstance(continuation_ids[0], str)
                        and isinstance(next_start_char, int)
                    ):
                        record["continuation_requests"][continuation_ids[0]] = {
                            "url": requested_url,
                            "url_signature": url_signature,
                            "start_char": next_start_char,
                            "max_chars": spec.get("max_chars", 8_000),
                            "force_browser": (
                                result.get("retrieval_mode") == "protected_browser"
                            ),
                            "document_id": spec.get("document_id"),
                        }
                    document_map = result.get("document_map")
                    headings = (
                        document_map.get("headings")
                        if isinstance(document_map, dict)
                        else None
                    )
                    if isinstance(headings, list):
                        for heading in headings:
                            if not isinstance(heading, dict):
                                continue
                            continuation_id = heading.get("continuation_id")
                            heading_start = heading.get("start_char")
                            if (
                                isinstance(continuation_id, str)
                                and isinstance(heading_start, int)
                                and heading_start > 0
                                and isinstance(spec.get("document_id"), str)
                            ):
                                record["continuation_requests"][continuation_id] = {
                                    "url": requested_url,
                                    "url_signature": url_signature,
                                    "start_char": heading_start,
                                    "max_chars": spec.get("max_chars", 8_000),
                                    "force_browser": (
                                        result.get("retrieval_mode")
                                        == "protected_browser"
                                    ),
                                    "document_id": spec["document_id"],
                                }
            for error in errors:
                continuation_id = error.get("continuation_id")
                if isinstance(continuation_id, str) and isinstance(pending, dict):
                    pending.pop(continuation_id, None)
                url = error.get("url")
                if isinstance(url, str):
                    try:
                        canonical_url = _canonical_url_identity(url)
                    except KetchError:
                        continue
                    failed_signature = self._usage_signature(canonical_url)
                    if failed_signature not in record["failed_urls"]:
                        host = _url_host(canonical_url)
                        failed_hosts = record["failed_hosts"]
                        if host and (
                            host in failed_hosts
                            or len(failed_hosts) < MAX_TRACKED_FAILED_HOSTS
                        ):
                            previous_failures = failed_hosts.get(host, 0)
                            if error.get("category") == "access_challenge":
                                failed_hosts[host] = max(
                                    previous_failures,
                                    FABRICATED_URL_HOST_FAILURE_THRESHOLD,
                                )
                            else:
                                failed_hosts[host] = previous_failures + 1
                    record["failed_urls"].add(failed_signature)
                    self._remove_pending_continuations(
                        record,
                        failed_signature,
                    )

    async def _usage_snapshot(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
    ) -> Optional[dict[str, int]]:
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        _, key = usage_key
        state = self._usage_state(request)
        async with state["lock"]:
            record = state["records"].get(key)
            if not isinstance(record, dict):
                counts = {
                    "total_calls": 0,
                    "search_calls": 0,
                    "initial_scrape_calls": 0,
                    "document_scrape_calls": 0,
                    "continuation_calls": 0,
                    "continuation_chunks": 0,
                }
            else:
                counts = {}
                for name in (
                    "total_calls",
                    "search_calls",
                    "initial_scrape_calls",
                    "document_scrape_calls",
                    "continuation_calls",
                    "continuation_chunks",
                ):
                    value = record.get(name, 0)
                    counts[name] = (
                        max(0, value)
                        if isinstance(value, int) and not isinstance(value, bool)
                        else 0
                    )
        return {
            **counts,
            "total_remaining": max(
                0,
                self.valves.maximum_total_calls_per_turn - counts["total_calls"],
            ),
            "document_scrape_calls_remaining": max(
                0,
                self.valves.maximum_document_scrape_calls_per_turn
                - counts["document_scrape_calls"],
            ),
            "continuation_chunks_remaining": max(
                0,
                self.valves.maximum_continuation_chunks_per_turn
                - counts["continuation_chunks"],
            ),
        }

    async def _evidence_ledger(
        self,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        offered_urls: list[str],
        inspected_urls: Optional[list[str]] = None,
        commit: bool = True,
    ) -> Optional[list[str]]:
        """Record URLs Ketch itself offered this user message and return the
        pages actually retrieved with substantive content so far. The returned
        list is the only set of URLs the model inspected through Ketch, so it
        doubles as a citation allowlist.

        The current call's own pages are recorded here rather than by source
        emission, which runs after this response is serialized. Otherwise a
        model that scrapes once and answers would be told it may cite nothing.
        """
        usage_key = self._turn_usage_key(user, metadata)
        if usage_key is None:
            return None
        user_id, key = usage_key
        state = self._usage_state(request)
        now = time.monotonic()
        canonical_offered = []
        for url in offered_urls:
            try:
                canonical_offered.append(_canonical_url_identity(url))
            except KetchError:
                continue
        async with state["lock"]:
            records = state["records"]
            self._prune_usage_records(records, now, user_id, key)
            record = self._usage_record(records, user_id, key, now)
            offered = record["offered_urls"]
            if commit:
                for canonical_url in canonical_offered:
                    if len(offered) >= MAX_TRACKED_OFFERED_URLS:
                        break
                    offered.add(self._usage_signature(canonical_url))
            inspected = record["inspected_urls"]
            preview = set(inspected)
            for url in inspected_urls or ():
                if len(preview) >= MAX_INSPECTED_SOURCES:
                    break
                preview.add(url)
            if commit:
                inspected.update(preview)
            return sorted(preview)[:MAX_INSPECTED_SOURCES]

    async def _decorate_public_response(
        self,
        response: str,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        operation: str,
        commit_ledger: bool = True,
    ) -> str:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            return response
        if not isinstance(payload, dict):
            return response

        offered_urls: list[str] = []
        inspected_now: list[str] = []
        if operation == "search" and payload.get("status") == "success":
            # Search responses carry their hits under "result"; "results" is
            # accepted defensively so a future rename cannot silently disable
            # the offered-URL exemption again.
            for results_key in ("result", "results"):
                for result in payload.get(results_key) or []:
                    if isinstance(result, dict) and isinstance(result.get("url"), str):
                        offered_urls.append(result["url"])
        for error in payload.get("errors") or []:
            if isinstance(error, dict) and isinstance(error.get("error"), str):
                destination = REDIRECT_DESTINATION_RE.search(error["error"])
                if destination:
                    offered_urls.append(destination.group(1))

        if operation == "search" and payload.get("status") == "success":
            payload.update(
                {
                    "evidence_type": "search_snippets",
                    "full_page_verified": False,
                    "evidence_notice": (
                        "Discovery snippets only. Select sources with them, "
                        "but do not claim full-page verification. Search coverage "
                        "is non-exhaustive and cannot prove no matches, complete "
                        "coverage, or a latest/top ranking. Verify required dates "
                        "from retrieved evidence."
                    ),
                    "next_action": (
                        "Select the smallest useful set of results. Before generic page "
                        "extraction, first check whether a selected platform or source "
                        "has an available source-specific structured retriever. If "
                        "availability is unknown, perform one focused capability-"
                        "discovery check. When one directly covers the selected source, "
                        "use it instead of Ketch. Only when none fits, scrape one or two "
                        "exact returned URLs together in one call. Never invent or "
                        "modify a URL path. Do not search again unless none of these "
                        "results can support a specific unmet requirement; if needed, "
                        "refine that requirement once."
                    ),
                }
            )
        elif operation == "scrape":
            results = payload.get("results")
            has_incomplete_result = False
            has_document_incomplete = False
            has_document_ceiling = False
            has_standard_incomplete = False
            if isinstance(results, list):
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    pagination = result.get("pagination")
                    result["evidence_type"] = "page_extract"
                    result["content_complete"] = (
                        bool(result.get("content_complete"))
                        if "content_complete" in result
                        else bool(
                            isinstance(pagination, dict)
                            and not pagination.get("truncated", False)
                        )
                    )
                    substantive_content = _has_substantive_content(result)
                    result.pop(ACCESS_CHALLENGE_CHECKED_KEY, None)
                    for key in tuple(result):
                        if isinstance(key, str) and key.startswith("_ketch_"):
                            result.pop(key, None)
                    result["substantive_content"] = substantive_content
                    if result["substantive_content"]:
                        inspected_url = result.get("resolved_url") or result.get("url")
                        if isinstance(inspected_url, str) and inspected_url:
                            inspected_now.append(inspected_url)
                    if not result["substantive_content"]:
                        result["evidence_notice"] = (
                            "INSUFFICIENT CONTENT: this extract contains only a title, "
                            "headings, links, or formatting. It does not establish "
                            "the requested facts or quotations. content_complete "
                            "describes extraction only, not verification."
                        )
                    if not result["content_complete"]:
                        has_incomplete_result = True
                        if result.get("document_mode") is True:
                            has_document_incomplete = True
                            if (
                                isinstance(pagination, dict)
                                and pagination.get("configured_ceiling_reached") is True
                            ):
                                has_document_ceiling = True
                        else:
                            has_standard_incomplete = True
                errors = payload.get("errors")
                payload["content_complete"] = bool(results) and not (
                    has_incomplete_result or (isinstance(errors, list) and errors)
                )
                payload["has_more_content"] = has_incomplete_result
                insufficient = [
                    result
                    for result in results
                    if isinstance(result, dict) and not result["substantive_content"]
                ]
                payload["substantive_content"] = any(
                    isinstance(result, dict) and result["substantive_content"]
                    for result in results
                )
                if has_incomplete_result:
                    if has_document_incomplete and has_standard_incomplete:
                        payload["evidence_notice"] = (
                            "INCOMPLETE MIXED PAGE EVIDENCE: document-mode "
                            "heading maps are navigation metadata, while ordinary "
                            "partial extracts cover only their returned text. "
                            "Support claims only with returned page text and do "
                            "not describe either result as fully read."
                            + (
                                " At least one document-mode extraction reached "
                                "its configured ceiling, so exhaustive coverage "
                                "of that page is impossible in this user message."
                                if has_document_ceiling
                                else ""
                            )
                        )
                        if has_document_ceiling:
                            payload["next_action"] = (
                                "A configured document-extraction ceiling has "
                                "already made exhaustive coverage of at least one "
                                "page impossible. For an exhaustive request, stop "
                                "now, report the ceiling, and disclose partial "
                                "reading; do not retry that document scrape or "
                                "consume continuations trying to make "
                                "content_complete true. For a scoped request, use "
                                "a document continuation only for a specific "
                                "evidence gap within the bounded extraction. For "
                                "ordinary results, continue only for a specific "
                                "unsupported requirement."
                            )
                        else:
                            payload["next_action"] = (
                                "For a whole-page summary or review of a document-mode "
                                "result, call ketch_scrape_continue directly with only "
                                "the exact identifiers in "
                                "pagination.continuation_request, then follow sequential "
                                "continuations until content_complete is true. Do not "
                                "use heading-map identifiers for a whole-page task. For "
                                "a document request limited to named topics or sections, "
                                "choose at most three matching heading continuation "
                                "identifiers; never pass all heading identifiers. For an "
                                "ordinary result that is one long page being summarized "
                                "or reviewed, call ketch_scrape once for only that URL "
                                "with document_mode=true instead of paging it "
                                "sequentially. For narrower ordinary research, continue "
                                "only for a specific unsupported requirement. If a "
                                "budget or error prevents completion, disclose partial "
                                "reading."
                            )
                    elif has_document_incomplete:
                        if has_document_ceiling:
                            payload["evidence_notice"] = (
                                "DOCUMENT EXTRACTION CEILING REACHED: the bounded "
                                "extraction is incomplete, so an exhaustive "
                                "whole-page review is impossible in this user "
                                "message. Support claims only with returned page "
                                "text and disclose that coverage is partial."
                            )
                            payload["next_action"] = (
                                "The configured extraction ceiling has already "
                                "made exhaustive coverage impossible. If exhaustive "
                                "coverage was requested, stop now, report the "
                                "ceiling metadata, and disclose partial reading; "
                                "do not retry the document scrape or consume "
                                "continuations trying to make content_complete "
                                "true. For highlights or a scoped request, use a "
                                "continuation only for a specific evidence gap "
                                "within the bounded extraction, then disclose "
                                "prefix-bounded coverage."
                            )
                        else:
                            payload["evidence_notice"] = (
                                "INCOMPLETE DOCUMENT EVIDENCE: the heading map covers "
                                "the bounded extraction but is navigation metadata, not "
                                "proof that every section was read. Support claims only "
                                "with returned page text and describe a section-selected "
                                "summary as targeted rather than exhaustive."
                            )
                            payload["next_action"] = (
                                "For a whole-page summary or review, call "
                                "ketch_scrape_continue directly with only the exact "
                                "identifiers in pagination.continuation_request, then "
                                "follow sequential continuations until content_complete "
                                "is true. Do not use heading-map identifiers for a "
                                "whole-page task. For requested highlights or a summary "
                                "limited to named topics or sections, choose at most "
                                "three matching opaque heading continuation identifiers; "
                                "never pass all heading identifiers, and stop when those "
                                "requirements are supported. If budgets or the "
                                "configured ceiling prevent completion, disclose partial "
                                "reading."
                            )
                    else:
                        payload["evidence_notice"] = (
                            "INCOMPLETE PAGE EXTRACT: support claims only with the "
                            "returned text. For a summary or review of one long "
                            "page, switch once to document mode or explicitly "
                            "disclose partial reading; do not imply the ordinary "
                            "extract covered the whole page."
                        )
                        payload["next_action"] = (
                            "For a requested summary or review of one long page, "
                            "call ketch_scrape once for only that URL with "
                            "document_mode=true instead of consuming ordinary "
                            "sequential continuations. Do not switch merely "
                            "because pagination.truncated is true, and do not "
                            "use document mode for a narrow multi-source "
                            "comparison or feature check. For narrower research, "
                            "answer now if the returned text supports every "
                            "explicit requirement; otherwise continue only the "
                            "page tied to a specific evidence gap. Do not run "
                            "another search for optional corroboration. If "
                            "completion becomes impossible, disclose partial "
                            "reading."
                        )
                else:
                    payload["evidence_notice"] = (
                        "Support claims only with returned page text. A complete "
                        "extract is not a guarantee that the source itself is correct."
                    )
                    if payload["substantive_content"]:
                        payload["next_action"] = (
                            "If the returned text supports every explicit requirement, "
                            "answer now. Do not search or scrape for optional "
                            "corroboration; make another call only for a specific "
                            "unsupported requirement."
                        )
                    else:
                        payload["next_action"] = (
                            "This call returned no substantive page evidence. If an "
                            "error supplies a validated redirect destination, use "
                            "that exact destination. Otherwise run at most one "
                            "focused search for an alternate source; never invent or "
                            "modify a URL path. After composed URLs repeatedly fail "
                            "on a host, Ketch refuses further composed paths there. "
                            "Then disclose any remaining failure."
                        )
                if insufficient:
                    payload["insufficient_content_count"] = len(insufficient)
                    payload["evidence_notice"] += (
                        " One or more extracts contain no substantive text. "
                        "Do not claim verification from those extracts; use another "
                        "relevant source within the remaining budget, or disclose "
                        "that verification failed."
                    )

        if "\ufffd" in response:
            payload["text_integrity_warning"] = (
                "The upstream result contains Unicode replacement characters. "
                "Do not reproduce those glyphs or quote affected text verbatim; "
                "paraphrase it with unambiguous ASCII punctuation."
            )
        usage = await self._usage_snapshot(request, user, metadata)
        if usage is not None:
            payload["usage"] = usage
        inspected = await self._evidence_ledger(
            request,
            user,
            metadata,
            offered_urls,
            inspected_now,
            commit=commit_ledger,
        )
        if inspected is not None:
            payload["inspected_sources"] = inspected
            payload["citation_rule"] = (
                "inspected_sources lists every page Ketch actually retrieved "
                "with substantive content in this user message. Cite and "
                "attribute claims only to these exact URLs. A URL that appears "
                "only in a search snippet, only in page text, or only in prior "
                "knowledge was not inspected and must not be cited."
                if inspected
                else "No page has been retrieved with substantive content in "
                "this user message yet, so no URL may be cited as inspected "
                "evidence."
            )
        return json.dumps(payload, ensure_ascii=False)

    async def _commit_response_ledger(
        self,
        response: str,
        request: Request,
        user: Any,
        metadata: Optional[dict],
        operation: str,
    ) -> None:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        offered_urls: list[str] = []
        inspected_now: list[str] = []
        if operation == "search" and payload.get("status") == "success":
            for results_key in ("result", "results"):
                for result in payload.get(results_key) or []:
                    if isinstance(result, dict) and isinstance(
                        result.get("url"),
                        str,
                    ):
                        offered_urls.append(result["url"])
        for error in payload.get("errors") or []:
            if isinstance(error, dict) and isinstance(error.get("error"), str):
                destination = REDIRECT_DESTINATION_RE.search(error["error"])
                if destination:
                    offered_urls.append(destination.group(1))
        if operation == "scrape":
            for result in payload.get("results") or []:
                if (
                    isinstance(result, dict)
                    and result.get("substantive_content") is True
                ):
                    inspected_url = result.get("resolved_url") or result.get("url")
                    if isinstance(inspected_url, str) and inspected_url:
                        inspected_now.append(inspected_url)
        await self._evidence_ledger(
            request,
            user,
            metadata,
            offered_urls,
            inspected_now,
            commit=True,
        )

    async def _run_process(
        self,
        arguments: list[str],
        *,
        environment: Optional[dict[str, str]] = None,
        timeout_seconds: int,
        input_bytes: Optional[bytes] = None,
        output_limit: Optional[int] = None,
    ) -> dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=(
                    asyncio.subprocess.PIPE
                    if input_bytes is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except OSError as error:
            raise KetchError(
                f"Could not start {Path(arguments[0]).name}: {error}"
            ) from error

        stdout_byte_limit = (
            min(MAX_SUBPROCESS_STDOUT_BYTES, max(1, output_limit) * 4)
            if output_limit is not None
            else MAX_SUBPROCESS_STDOUT_BYTES
        )

        async def read_stdout() -> bytes:
            chunks = []
            total = 0
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if total > stdout_byte_limit:
                    raise KetchError(
                        "Ketch output exceeded the configured total result limit. "
                        "Reduce the result count, URL count, or max_chars value."
                    )
                chunks.append(chunk)

        async def read_stderr() -> bytes:
            tail = bytearray()
            while True:
                chunk = await process.stderr.read(64 * 1024)
                if not chunk:
                    return bytes(tail)
                tail.extend(chunk)
                if len(tail) > MAX_SUBPROCESS_STDERR_BYTES:
                    del tail[:-MAX_SUBPROCESS_STDERR_BYTES]

        async def write_stdin() -> None:
            if process.stdin is None:
                return
            try:
                process.stdin.write(input_bytes or b"")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
            asyncio.create_task(write_stdin()),
        ]

        async def stop_and_drain() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()

        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=timeout_seconds,
            )
            await process.wait()
        except asyncio.CancelledError:
            await asyncio.shield(stop_and_drain())
            raise
        except TimeoutError as error:
            await stop_and_drain()
            raise KetchError(
                f"{Path(arguments[0]).name} timed out after {timeout_seconds} seconds."
            ) from error
        except Exception:
            await stop_and_drain()
            raise

        try:
            stdout_text = stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KetchError(
                "Ketch returned stdout that was not valid UTF-8."
            ) from error
        stderr_text = stderr.decode("utf-8", errors="backslashreplace")
        if output_limit is not None and len(stdout_text) > output_limit:
            raise KetchError(
                "Ketch output exceeded the configured total result limit. "
                "Reduce the result count, URL count, or max_chars value."
            )
        return {
            "exit_code": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text[-12_000:],
        }

    async def _ensure_dependencies(self) -> None:
        missing = await asyncio.to_thread(_missing_libraries)
        if not missing:
            return
        if not self.valves.install_system_dependencies:
            names = ", ".join(item["library"] for item in missing)
            raise KetchError(f"Chromium runtime libraries are missing: {names}")
        if (
            platform.system() != "Linux"
            or not hasattr(os, "geteuid")
            or os.geteuid() != 0
        ):
            raise KetchError(
                "Installing Chromium dependencies requires a root Linux container."
            )
        apt_get = shutil.which("apt-get")
        if not apt_get:
            raise KetchError("apt-get is unavailable in the Open WebUI container.")
        apt_state = Path("/var/lib/apt")
        _require_free_space(
            apt_state if apt_state.exists() else Path("/"),
            APT_INSTALL_FREE_BYTES,
            "Chromium dependency installation",
        )

        environment = self._base_environment()
        environment["DEBIAN_FRONTEND"] = "noninteractive"
        update = await self._run_process(
            [apt_get, "update"],
            environment=environment,
            timeout_seconds=self.valves.bootstrap_timeout_seconds,
        )
        if update["exit_code"] != 0:
            raise KetchError(
                f"apt-get update failed: {update['stderr'] or update['stdout'][-4000:]}"
            )
        install = await self._run_process(
            [
                apt_get,
                "install",
                "-y",
                "--no-install-recommends",
                *CHROMIUM_PACKAGES,
            ],
            environment=environment,
            timeout_seconds=self.valves.bootstrap_timeout_seconds,
        )
        if install["exit_code"] != 0:
            raise KetchError(
                f"Chromium dependency installation failed: "
                f"{install['stderr'] or install['stdout'][-4000:]}"
            )
        remaining = await asyncio.to_thread(_missing_libraries)
        if remaining:
            names = ", ".join(item["library"] for item in remaining)
            raise KetchError(
                f"Chromium libraries remain unavailable after installation: {names}"
            )

    async def _ensure_ketch(self, root: Path, metadata: dict) -> tuple[Path, dict]:
        architecture = platform.machine().lower()
        release = ARCHITECTURES.get(architecture)
        if platform.system() != "Linux" or release is None:
            raise KetchError(
                f"Ketch bootstrap supports Linux x86_64 and arm64, not "
                f"{platform.system()} {architecture}."
            )

        binary_path = root / "bin" / "ketch"
        expected_binary_hash = metadata.get("ketch_binary_sha256")
        valid_existing = (
            metadata.get("ketch_version") == KETCH_VERSION
            and metadata.get("architecture") == release["release_arch"]
            and isinstance(expected_binary_hash, str)
            and binary_path.is_file()
            and os.access(binary_path, os.X_OK)
        )
        if valid_existing:
            actual_hash = await asyncio.to_thread(_sha256_file, binary_path)
            valid_existing = actual_hash == expected_binary_hash
        if valid_existing:
            return binary_path, metadata

        binary_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=str(root)) as temp_dir:
            temporary_root = Path(temp_dir)
            archive_path = temporary_root / "ketch.tar.gz"
            temporary_binary = temporary_root / "ketch"
            await asyncio.to_thread(
                _download_release,
                archive_path,
                release["release_arch"],
                release["archive_sha256"],
            )
            binary_hash = await asyncio.to_thread(
                _extract_ketch,
                archive_path,
                temporary_binary,
            )
            os.replace(temporary_binary, binary_path)
            os.chmod(binary_path, 0o755)

        version = await self._run_process(
            [str(binary_path), "version"],
            timeout_seconds=30,
        )
        if version["exit_code"] != 0 or f"v{KETCH_VERSION}" not in version["stdout"]:
            binary_path.unlink(missing_ok=True)
            raise KetchError("The installed Ketch binary failed version verification.")

        metadata = {
            "ketch_version": KETCH_VERSION,
            "architecture": release["release_arch"],
            "release_archive_sha256": release["archive_sha256"],
            "ketch_binary_sha256": binary_hash,
        }
        await asyncio.to_thread(_write_json_atomic, root / "install.json", metadata)
        return binary_path, metadata

    def _base_environment(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in allowed and isinstance(value, str)
        }
        environment.update(
            {
                "PATH": environment.get(
                    "PATH",
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                ),
                "LC_ALL": "C",
                "LANG": "C.UTF-8",
            }
        )
        return environment

    def _temporary_parent(self, root: Optional[Path] = None) -> Path:
        install_root = root or _safe_install_root(self.valves.installation_root)
        parent = install_root / "browser-work"
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o711)
        self._ensure_browser_path_searchable(
            install_root,
            parent,
            "Ketch browser work directory",
            include_target=True,
        )
        return parent

    def _browser_uid_gid(self) -> tuple[int, int]:
        uid = self.valves.browser_unprivileged_uid
        gid = self.valves.browser_unprivileged_gid
        if isinstance(uid, bool) or isinstance(gid, bool) or uid <= 0 or gid <= 0:
            raise KetchError(
                "Browser unprivileged uid and gid must be positive integers."
            )
        return uid, gid

    @staticmethod
    def _running_as_root() -> bool:
        return (
            platform.system() == "Linux"
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
        )

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    @staticmethod
    def _is_sandbox_launch_error(error: Exception) -> bool:
        detail = str(error).casefold()
        return any(marker in detail for marker in SANDBOX_ERROR_MARKERS)

    def _directory_searchable_by_browser_uid(self, directory: Path) -> bool:
        if platform.system() != "Linux":
            return True
        uid, gid = self._browser_uid_gid()
        try:
            info = directory.stat()
        except OSError:
            return False
        mode = stat.S_IMODE(info.st_mode)
        return bool(
            mode & 0o001
            or (info.st_uid == uid and mode & 0o100)
            or (info.st_gid == gid and mode & 0o010)
        )

    def _ensure_browser_path_searchable(
        self,
        root: Path,
        target: Path,
        label: str,
        *,
        include_target: bool = False,
    ) -> None:
        if platform.system() != "Linux":
            return
        root_resolved = root.resolve()
        target_path = target.resolve()
        current = (
            target_path
            if include_target and target_path.is_dir()
            else target_path.parent
        )
        directories = []
        while True:
            if current.exists():
                directories.append(current)
            if current == current.parent:
                break
            current = current.parent
        for directory in reversed(directories):
            inside_root = False
            try:
                directory.relative_to(root_resolved)
                inside_root = True
            except ValueError:
                inside_root = False
            if inside_root:
                try:
                    os.chmod(
                        directory,
                        stat.S_IMODE(directory.stat().st_mode) | 0o111,
                    )
                except OSError as error:
                    raise KetchError(
                        f"Could not make {directory} searchable for browser rendering."
                    ) from error
            if not self._directory_searchable_by_browser_uid(directory):
                if inside_root:
                    raise KetchError(
                        f"{label} is not searchable by the unprivileged browser uid."
                    )
                raise KetchError(
                    f"{label} requires directory {directory} to be searchable by "
                    "the unprivileged browser uid. Configure the Ketch installation "
                    "root elsewhere or ask an administrator to chmod o+x that "
                    "directory."
                )

    def _ensure_browser_wrapper(self, root: Path, browser_path: Path) -> Path:
        wrapper_path = root / "bin" / "chrome-headless-shell-wrapper"
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_browser_path_searchable(
            root,
            browser_path,
            "Chrome headless shell executable path",
        )
        if self._running_as_root():
            setpriv = shutil.which("setpriv", path=self._base_environment().get("PATH"))
            if not setpriv:
                raise KetchError(
                    "Privilege drop is required for browser rendering, but setpriv "
                    "(util-linux) is unavailable."
                )
            uid, gid = self._browser_uid_gid()
            allowed_roots = " ".join(
                self._shell_quote(value)
                for value in ("/tmp", str(root / "browser-work"))
            )
            command = (
                "profile_dir=''\n"
                'for arg in "$@"; do\n'
                '  case "$arg" in --user-data-dir=*) profile_dir=${arg#--user-data-dir=} ;;\n'
                "  esac\n"
                "done\n"
                'if [ -n "$profile_dir" ]; then\n'
                "  case \"$profile_dir\" in /*) ;; *) echo 'Invalid Chromium profile path.' >&2; exit 126 ;;\n"
                "  esac\n"
                '  if [ ! -d "$profile_dir" ] || [ -L "$profile_dir" ]; then\n'
                "    echo 'Chromium profile path is unavailable or a symlink.' >&2; exit 126\n"
                "  fi\n"
                '  profile_real=$(CDPATH= cd -- "$profile_dir" && pwd -P) || exit 126\n'
                "  profile_ok=0\n"
                f"  for root_dir in {allowed_roots}; do\n"
                '    if [ -d "$root_dir" ] && [ ! -L "$root_dir" ]; then\n'
                '      root_real=$(CDPATH= cd -- "$root_dir" && pwd -P) || continue\n'
                '      case "$profile_real/" in "$root_real"/*) profile_ok=1 ;;\n'
                "      esac\n"
                "    fi\n"
                "  done\n"
                '  if [ "$profile_ok" != 1 ]; then\n'
                "    echo 'Chromium profile path is outside allowed temporary roots.' >&2; exit 126\n"
                "  fi\n"
                f'  chown -hR {uid}:{gid} "$profile_real" || exit 126\n'
                "fi\n"
                f"exec {self._shell_quote(setpriv)} "
                f"--reuid={uid} --regid={gid} --clear-groups --no-new-privs -- "
                f'{self._shell_quote(str(browser_path))} "$@"'
            )
        else:
            command = f'exec {self._shell_quote(str(browser_path))} "$@"'
        script = "#!/bin/sh\n" + command + "\n"
        if (
            not wrapper_path.is_file()
            or wrapper_path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
            != script
        ):
            wrapper_path.write_text(script, encoding="utf-8")
        os.chmod(wrapper_path, 0o755)
        return wrapper_path

    def _prepare_browser_profile(self, home: Path) -> None:
        for directory in (
            home,
            home / "config",
            home / "cache",
            home / "data",
            home / "tmp",
            home / "profile",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        if self._running_as_root() and hasattr(os, "chown"):
            uid, gid = self._browser_uid_gid()
            for directory, _, _ in os.walk(home):
                os.chown(directory, uid, gid)
        root = _safe_install_root(self.valves.installation_root)
        try:
            home.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            root = home
        self._ensure_browser_path_searchable(
            root,
            home,
            "Browser profile directory",
            include_target=True,
        )

    def _validate_browser_sandbox(self) -> str:
        value = str(self.valves.browser_sandbox).strip().lower()
        if value not in {"auto", "required", "off"}:
            raise KetchError("browser_sandbox must be one of: auto, required, off.")
        return value

    def _browser_launch_arguments(
        self,
        profile_dir: Optional[Path],
        no_sandbox: bool,
        *,
        include_user_data_dir: bool = False,
    ) -> list[str]:
        arguments = [
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        if include_user_data_dir and profile_dir is not None:
            arguments.append(f"--user-data-dir={profile_dir}")
        if no_sandbox:
            arguments.append("--no-sandbox")
        if self.valves.browser_jitless:
            arguments.append("--js-flags=--jitless")
        return arguments

    def _cleanup_old_browser_installs(
        self,
        root: Path,
        current_browser_root: Path,
        previous_browser_path: Path,
    ) -> None:
        root_resolved = root.resolve()
        current_resolved = current_browser_root.resolve()
        browser_parent = root / "browser"
        if browser_parent.is_dir():
            for child in browser_parent.iterdir():
                if child == current_browser_root:
                    continue
                if not child.name.startswith("chrome-headless-shell-"):
                    continue
                try:
                    child.resolve().relative_to(root_resolved)
                except (OSError, ValueError):
                    continue
                if child.resolve() != current_resolved:
                    _safe_remove_inside_root(root, child)

        legacy_root = _legacy_browser_cleanup_root(root, previous_browser_path)
        if legacy_root is None:
            return
        try:
            legacy_resolved = legacy_root.resolve()
            legacy_resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            return
        if legacy_resolved == current_resolved:
            return
        try:
            legacy_resolved.relative_to(current_resolved)
            return
        except ValueError:
            pass
        _safe_remove_inside_root(root, legacy_root)

    async def _test_browser(self, root: Path, browser_path: Path) -> None:
        wrapper_path = self._ensure_browser_wrapper(root, browser_path)
        with tempfile.TemporaryDirectory(
            prefix="ketch-browser-test-",
            dir=str(self._temporary_parent(root)),
        ) as temp_dir:
            temp_path = Path(temp_dir)
            self._prepare_browser_profile(temp_path)
            result = await self._run_process(
                [
                    str(wrapper_path),
                    "--headless",
                    *self._browser_launch_arguments(
                        temp_path / "profile",
                        no_sandbox=True,
                        include_user_data_dir=True,
                    ),
                    "--dump-dom",
                    "data:text/html,<title>ketch-runtime-ok</title>",
                ],
                environment=self._base_environment(),
                timeout_seconds=60,
                output_limit=100_000,
            )
        if result["exit_code"] != 0 or "ketch-runtime-ok" not in result["stdout"]:
            detail = result["stderr"] or result["stdout"][-4_000:]
            raise KetchError(f"Managed Chromium launch test failed: {detail}")

    async def _ensure_browser(
        self,
        root: Path,
        metadata: dict,
        required: bool,
    ) -> tuple[Optional[Path], dict]:
        if not required or not self.valves.enable_browser:
            return None, metadata
        self._validate_browser_sandbox()
        architecture = platform.machine().lower()
        if (
            platform.system() != "Linux"
            or architecture not in CHROME_FOR_TESTING_ARCHITECTURES
        ):
            raise KetchError(
                "Browser rendering requires the pinned Chrome for Testing "
                "linux64 chrome-headless-shell build; this container is "
                f"{platform.system()} {architecture}."
            )
        if self._running_as_root() and not shutil.which(
            "setpriv",
            path=self._base_environment().get("PATH"),
        ):
            raise KetchError(
                "Privilege drop is required for browser rendering, but setpriv "
                "(util-linux) is unavailable."
            )

        browser_root = (
            root
            / "browser"
            / (
                f"chrome-headless-shell-{CHROME_FOR_TESTING_VERSION}-"
                f"{CHROME_FOR_TESTING_PLATFORM}"
            )
        )
        stored_browser = metadata.get("browser_path")
        expected_binary_hash = metadata.get("browser_executable_sha256")
        browser_path = (
            Path(stored_browser) if isinstance(stored_browser, str) else Path()
        )
        valid_existing = (
            metadata.get("chrome_for_testing_version") == CHROME_FOR_TESTING_VERSION
            and metadata.get("chrome_for_testing_url") == CHROME_FOR_TESTING_URL
            and metadata.get("chrome_for_testing_archive_sha256")
            == CHROME_FOR_TESTING_SHA256
            and isinstance(expected_binary_hash, str)
        )
        if valid_existing:
            try:
                browser_path.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                valid_existing = False
            if valid_existing:
                valid_existing = browser_path.is_file() and os.access(
                    browser_path,
                    os.X_OK,
                )
            if valid_existing:
                actual_hash = await asyncio.to_thread(_sha256_file, browser_path)
                valid_existing = actual_hash == expected_binary_hash
        if valid_existing:
            await self._test_browser(root, browser_path)
            wrapper_path = self._ensure_browser_wrapper(root, browser_path)
            return wrapper_path, metadata

        previous_browser_path = (
            browser_path if isinstance(stored_browser, str) else Path()
        )
        staged_root: Optional[Path] = None
        temporary_parent = self._temporary_parent(root)
        _require_free_space(
            temporary_parent,
            BROWSER_INSTALL_FREE_BYTES,
            "the Chrome for Testing download",
        )
        with tempfile.TemporaryDirectory(
            prefix="ketch-browser-install-",
            dir=str(temporary_parent),
        ) as temp_dir:
            temporary_root = Path(temp_dir)
            archive_path = temporary_root / "chrome-headless-shell.zip"
            extracted_root = temporary_root / "extracted"
            staged_root = temporary_root / browser_root.name
            await asyncio.to_thread(
                _download_verified_file,
                CHROME_FOR_TESTING_URL,
                archive_path,
                CHROME_FOR_TESTING_SHA256,
                MAX_BROWSER_ARCHIVE_BYTES,
                "Chrome for Testing headless shell archive",
            )
            extracted_browser = await asyncio.to_thread(
                _extract_chrome_headless_shell,
                archive_path,
                extracted_root,
            )
            shutil.move(str(extracted_root), str(staged_root))
            staged_browser_path = staged_root / extracted_browser.relative_to(
                extracted_root
            )
            binary_hash = await asyncio.to_thread(_sha256_file, staged_browser_path)
            try:
                await self._test_browser(root, staged_browser_path)
            except Exception:
                await asyncio.to_thread(
                    _safe_remove_inside_root,
                    root,
                    staged_root,
                )
                raise

            browser_root.parent.mkdir(parents=True, exist_ok=True)
            if browser_root.exists() or browser_root.is_symlink():
                await asyncio.to_thread(
                    _safe_remove_inside_root,
                    root,
                    browser_root,
                )
            shutil.move(str(staged_root), str(browser_root))
            browser_path = browser_root / staged_browser_path.relative_to(staged_root)
        wrapper_path = self._ensure_browser_wrapper(root, browser_path)
        metadata = {
            **metadata,
            "browser_path": str(browser_path),
            "browser_install_root": str(browser_root),
            "browser_wrapper_path": str(wrapper_path),
            "browser_executable_sha256": binary_hash,
            "chrome_for_testing_version": CHROME_FOR_TESTING_VERSION,
            "chrome_for_testing_platform": CHROME_FOR_TESTING_PLATFORM,
            "chrome_for_testing_url": CHROME_FOR_TESTING_URL,
            "chrome_for_testing_archive_sha256": CHROME_FOR_TESTING_SHA256,
        }
        await asyncio.to_thread(_write_json_atomic, root / "install.json", metadata)
        await asyncio.to_thread(
            self._cleanup_old_browser_installs,
            root,
            browser_root,
            previous_browser_path,
        )
        return wrapper_path, metadata

    async def _ensure_runtime(
        self,
        request: Request,
        event_emitter,
        require_browser: bool = False,
    ) -> dict:
        state = self._state(request)
        browser_needed = require_browser and self.valves.enable_browser
        if (
            state["ready"]
            and isinstance(state["runtime"], dict)
            and (not browser_needed or state["runtime"].get("browser") is not None)
        ):
            return state["runtime"]

        async with state["lock"]:
            if (
                state["ready"]
                and isinstance(state["runtime"], dict)
                and (not browser_needed or state["runtime"].get("browser") is not None)
            ):
                return state["runtime"]
            if (
                state["last_error"]
                and (state.get("failed_requirement") == "base" or browser_needed)
                and time.monotonic() - state["failed_at"]
                < self.valves.bootstrap_retry_seconds
            ):
                raise KetchError(state["last_error"])

            try:
                root = _safe_install_root(self.valves.installation_root)
                root.mkdir(parents=True, exist_ok=True)
                if not os.access(root, os.W_OK | os.X_OK):
                    raise KetchError(f"The Open WebUI process cannot write to {root}.")
                await self._emit_status(
                    event_emitter,
                    "Preparing the Ketch runtime",
                    False,
                )
                if browser_needed:
                    self._require_browser_python_dependencies()
                    await self._ensure_dependencies()
                metadata = await asyncio.to_thread(
                    _read_json,
                    root / "install.json",
                )
                binary_path, metadata = await self._ensure_ketch(root, metadata)
                state.update(
                    {
                        "ready": True,
                        "runtime": {
                            "root": root,
                            "binary": binary_path,
                            "browser": None,
                        },
                    }
                )
                browser_path, metadata = await self._ensure_browser(
                    root,
                    metadata,
                    browser_needed,
                )
                runtime = {
                    "root": root,
                    "binary": binary_path,
                    "browser": browser_path,
                }
                state.update(
                    {
                        "ready": True,
                        "runtime": runtime,
                        "last_error": None,
                        "failed_requirement": None,
                        "failed_at": 0.0,
                    }
                )
                await self._emit_status(
                    event_emitter,
                    "Ketch runtime is ready",
                    True,
                )
                return runtime
            except Exception as error:
                message = str(error)
                existing_runtime = (
                    state.get("runtime")
                    if isinstance(state.get("runtime"), dict)
                    else None
                )
                state.update(
                    {
                        "ready": existing_runtime is not None,
                        "runtime": existing_runtime,
                        "last_error": message,
                        "failed_requirement": ("browser" if browser_needed else "base"),
                        "failed_at": time.monotonic(),
                    }
                )
                await self._emit_error(event_emitter, message)
                await self._emit_status(
                    event_emitter,
                    "Ketch runtime setup failed",
                    True,
                )
                if isinstance(error, KetchError):
                    raise
                log.exception("Ketch runtime bootstrap failed")
                raise KetchError(f"Ketch runtime bootstrap failed: {error}") from error

    def _require_browser_python_dependencies(self) -> None:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError as error:
            raise _browser_dependency_error(error) from error

    def _request_environment(
        self,
        temporary_home: str,
    ) -> dict[str, str]:
        environment = self._base_environment()
        temporary_path = Path(temporary_home)
        environment.update(
            {
                "HOME": temporary_home,
                "XDG_CONFIG_HOME": str(temporary_path / "config"),
                "XDG_CACHE_HOME": str(temporary_path / "cache"),
                "XDG_DATA_HOME": str(temporary_path / "data"),
                "TMPDIR": str(temporary_path / "tmp"),
                "TEMP": str(temporary_path / "tmp"),
                "TMP": str(temporary_path / "tmp"),
            }
        )
        return environment

    async def _safe_fetch(self, url: str) -> dict[str, Any]:
        try:
            from open_webui.env import (
                AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                AIOHTTP_CLIENT_SESSION_SSL,
                AIOHTTP_CLIENT_SSL_CERT_FILE,
                USER_AGENT,
            )
            from open_webui.retrieval.web.utils import (
                get_ssrf_safe_requests_session,
                validate_url,
            )
        except ImportError as error:
            raise KetchError(
                "Open WebUI SSRF-safe retrieval helpers are unavailable."
            ) from error

        follow_same_host = self.valves.follow_same_host_redirects

        def fetch(accept: str, deadline: float) -> dict[str, Any]:
            current_url = _validate_url_shape(url)
            headers = {
                "Accept": accept,
                "User-Agent": USER_AGENT or f"open-webui-ketch-tool/{TOOL_VERSION}",
                # The SSRF-safe session rejects compressed responses.
                "Accept-Encoding": "identity",
            }
            verify: Any
            if AIOHTTP_CLIENT_SESSION_SSL is False:
                verify = False
            elif AIOHTTP_CLIENT_SESSION_SSL is True:
                verify = True
            else:
                verify = AIOHTTP_CLIENT_SSL_CERT_FILE or True
            expired = threading.Event()
            response_holder: dict[str, Any] = {}
            session_holder: dict[str, Any] = {}

            def response_socket(response: Any) -> Any:
                raw = getattr(response, "raw", None)
                candidates = [
                    getattr(getattr(raw, "_connection", None), "sock", None),
                    getattr(
                        getattr(
                            getattr(getattr(raw, "_fp", None), "fp", None),
                            "raw",
                            None,
                        ),
                        "_sock",
                        None,
                    ),
                ]
                for candidate in candidates:
                    shutdown = getattr(candidate, "shutdown", None)
                    if callable(shutdown):
                        return candidate
                return None

            def abort_fetch() -> None:
                expired.set()
                response = response_holder.get("response")
                sock = response_socket(response)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                for close_target in (
                    response,
                    session_holder.get("session"),
                ):
                    close = getattr(close_target, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass

            with get_ssrf_safe_requests_session(
                trust_env=False,
                store_cookies=False,
            ) as session:
                session_holder["session"] = session
                timer = threading.Timer(
                    max(0.001, deadline - time.monotonic()),
                    abort_fetch,
                )
                timer.daemon = True
                timer.start()
                try:
                    for _ in range(MAX_REDIRECTS + 1):
                        try:
                            current_url = _validate_url_shape(current_url)
                            validate_url(current_url)
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or expired.is_set():
                                raise KetchError(
                                    f"Protected fetch timed out after "
                                    f"{self.valves.command_timeout_seconds} seconds "
                                    f"for {url}."
                                )
                            with session.get(
                                current_url,
                                headers=headers,
                                allow_redirects=False,
                                stream=True,
                                timeout=(min(10.0, remaining), min(5.0, remaining)),
                                verify=verify,
                            ) as response:
                                response_holder["response"] = response
                                if 300 <= response.status_code < 400:
                                    redirect_url = _redirect_target(
                                        current_url,
                                        response.headers.get("Location"),
                                    )
                                    validate_url(redirect_url)
                                    if not _may_follow_redirect(
                                        current_url,
                                        redirect_url,
                                        allow_all=AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                                        follow_same_host=follow_same_host,
                                    ):
                                        raise _unfollowed_redirect_error(
                                            current_url,
                                            response.status_code,
                                            redirect_url,
                                            follow_same_host=follow_same_host,
                                        )
                                    current_url = redirect_url
                                    response_holder.pop("response", None)
                                    continue
                                if response.status_code != 200:
                                    raise KetchError(
                                        f"HTTP {response.status_code} while fetching "
                                        f"{current_url}."
                                    )
                                body = bytearray()
                                total = 0
                                for chunk in response.iter_content(
                                    chunk_size=16 * 1024
                                ):
                                    if time.monotonic() > deadline or expired.is_set():
                                        raise KetchError(
                                            f"Protected fetch timed out after "
                                            f"{self.valves.command_timeout_seconds} "
                                            f"seconds for {url}."
                                        )
                                    if not chunk:
                                        continue
                                    total += len(chunk)
                                    if total > self.valves.maximum_download_bytes:
                                        raise KetchError(
                                            f"Response from {current_url} exceeds the "
                                            f"{self.valves.maximum_download_bytes}-byte "
                                            f"limit."
                                        )
                                    body.extend(chunk)
                                response_holder.pop("response", None)
                                return {
                                    "url": current_url,
                                    "content_type": response.headers.get(
                                        "Content-Type",
                                        "",
                                    ),
                                    "body": bytes(body),
                                }
                        except KetchError:
                            raise
                        except Exception as error:
                            if expired.is_set() or time.monotonic() > deadline:
                                raise KetchError(
                                    f"Protected fetch timed out after "
                                    f"{self.valves.command_timeout_seconds} seconds "
                                    f"for {url}."
                                ) from error
                            raise KetchError(
                                f"Protected fetch failed for {current_url}: {error}"
                            ) from error
                        finally:
                            response_holder.pop("response", None)
                finally:
                    timer.cancel()
            raise RedirectFailure(f"Too many redirects while fetching {url}.")

        async def fetch_with_fallback() -> dict[str, Any]:
            deadline = time.monotonic() + self.valves.command_timeout_seconds
            try:
                return await asyncio.to_thread(
                    fetch,
                    "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                    deadline,
                )
            except RedirectFailure as error:
                # Some sites start a cookie-based silent login only for explicit
                # HTML requests; retry the same URL once under identical rules.
                try:
                    return await asyncio.to_thread(fetch, "*/*", deadline)
                except KetchError:
                    raise error from None

        try:
            async with asyncio.timeout(self.valves.command_timeout_seconds):
                return await fetch_with_fallback()
        except TimeoutError as error:
            raise KetchError(
                f"Protected fetch timed out after "
                f"{self.valves.command_timeout_seconds} seconds for {url}."
            ) from error

    @asynccontextmanager
    async def _protected_browser(
        self,
        browser_path: Path,
        temporary_home: str,
    ):
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise _browser_dependency_error(error) from error

        sandbox = self._validate_browser_sandbox()
        home_path = Path(temporary_home)
        self._prepare_browser_profile(home_path)
        environment = self._request_environment(temporary_home)
        async with _RejectingProxy() as rejecting_proxy:
            async with async_playwright() as playwright:

                async def launch(no_sandbox: bool):
                    return await playwright.chromium.launch(
                        executable_path=str(browser_path),
                        headless=True,
                        proxy={
                            "server": rejecting_proxy,
                            "bypass": "",
                        },
                        env=environment,
                        args=self._browser_launch_arguments(
                            None,
                            no_sandbox=no_sandbox,
                        ),
                        chromium_sandbox=not no_sandbox,
                    )

                browser = None
                if sandbox == "off":
                    browser = await launch(no_sandbox=True)
                elif sandbox == "required":
                    try:
                        browser = await launch(no_sandbox=False)
                    except Exception as error:
                        raise KetchError(
                            "Chromium sandbox launch failed and browser_sandbox "
                            "is required."
                        ) from error
                else:
                    cached = BROWSER_SANDBOX_CACHE.get("auto_without_no_sandbox")
                    if cached is False:
                        browser = await launch(no_sandbox=True)
                    else:
                        try:
                            browser = await launch(no_sandbox=False)
                            BROWSER_SANDBOX_CACHE["auto_without_no_sandbox"] = True
                        except Exception as error:
                            if not self._is_sandbox_launch_error(error):
                                raise
                            BROWSER_SANDBOX_CACHE["auto_without_no_sandbox"] = False
                            browser = await launch(no_sandbox=True)
                try:
                    yield browser
                finally:
                    await browser.close()

    async def _safe_render_html(
        self,
        url: str,
        browser: Any,
    ) -> dict[str, str]:
        try:
            import aiohttp
            from open_webui.env import (
                AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                AIOHTTP_CLIENT_SESSION_SSL,
            )
            from open_webui.retrieval.web.utils import (
                _forwardable_request_headers,
                _fulfillable_response_headers,
                get_ssrf_safe_session,
                validate_url,
            )
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        except ImportError as error:
            raise _browser_dependency_error(error) from error

        resource_budget = _SharedByteBudget(
            self.valves.maximum_browser_resource_bytes,
            "Browser resources",
        )
        follow_same_host = self.valves.follow_same_host_redirects
        page = None
        navigation_failure: Optional[tuple[int, str]] = None
        navigation_redirect_failure: Optional[tuple[int, str, str]] = None

        async def intercept(route, session) -> None:
            nonlocal navigation_failure, navigation_redirect_failure
            request = route.request
            current_url = request.url
            try:
                await resource_budget.ensure_available()
                current_url = _validate_url_shape(current_url)
                await asyncio.to_thread(validate_url, current_url)
                headers = _forwardable_request_headers(await request.all_headers())
                post_data = request.post_data_buffer
                async with session.request(
                    request.method,
                    current_url,
                    headers=headers,
                    data=post_data,
                    allow_redirects=False,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    timeout=aiohttp.ClientTimeout(
                        total=self.valves.command_timeout_seconds
                    ),
                ) as response:
                    header_pairs = list(response.headers.items())
                    if 300 <= response.status < 400:
                        redirect_url = _redirect_target(
                            current_url,
                            response.headers.get("Location"),
                        )
                        await asyncio.to_thread(validate_url, redirect_url)
                        if not _may_follow_redirect(
                            current_url,
                            redirect_url,
                            allow_all=AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                            follow_same_host=follow_same_host,
                        ):
                            if (
                                page is not None
                                and request.is_navigation_request()
                                and request.frame == page.main_frame
                            ):
                                navigation_redirect_failure = (
                                    response.status,
                                    current_url,
                                    redirect_url,
                                )
                            raise _unfollowed_redirect_error(
                                current_url,
                                response.status,
                                redirect_url,
                                follow_same_host=follow_same_host,
                            )
                        body = await resource_budget.read_stream(
                            response.content,
                            self.valves.maximum_download_bytes,
                            f"Browser resource {current_url}",
                        )
                        await route.fulfill(
                            status=response.status,
                            headers=_fulfillable_response_headers(header_pairs),
                            body=body,
                        )
                        return
                    if (
                        page is not None
                        and request.is_navigation_request()
                        and request.frame == page.main_frame
                        and not 200 <= response.status < 300
                    ):
                        navigation_failure = (response.status, current_url)
                    body = await resource_budget.read_stream(
                        response.content,
                        self.valves.maximum_download_bytes,
                        f"Browser resource {current_url}",
                    )
                    await route.fulfill(
                        status=response.status,
                        headers=_fulfillable_response_headers(header_pairs),
                        body=body,
                    )
            except Exception as error:
                log.warning(
                    "Protected browser request blocked or failed for %s: %s",
                    current_url,
                    error,
                )
                await route.abort()

        await asyncio.to_thread(validate_url, url)
        context = None
        try:
            context = await browser.new_context(
                service_workers="block",
            )
            async with get_ssrf_safe_session(
                trust_env=False,
                store_cookies=False,
            ) as session:
                await context.route(
                    "**/*",
                    lambda route: intercept(route, session),
                )
                await context.route_web_socket(
                    "**/*",
                    lambda websocket: websocket.close(),
                )
                page = await context.new_page()
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.valves.command_timeout_seconds * 1_000,
                )
                if response is None:
                    raise KetchError(
                        f"Browser navigation produced no response for {url}."
                    )
                if not 200 <= response.status < 300:
                    raise KetchError(
                        f"HTTP {response.status} while rendering {page.url}."
                    )
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=5_000,
                    )
                except PlaywrightTimeoutError:
                    pass
                await page.wait_for_timeout(1_000)
                if resource_budget.exhausted:
                    raise KetchError(
                        "Browser resources exceeded the configured "
                        "combined download limit."
                    )
                final_url = _validate_url_shape(page.url)
                await asyncio.to_thread(validate_url, final_url)
                try:
                    rendered_html = await page.evaluate(
                        """(limit) => {
                            const element = document.documentElement;
                            const html = element ? element.outerHTML : "";
                            if (html.length > limit) {
                                return {too_large: true, length: html.length};
                            }
                            return html;
                        }""",
                        max(1, self.valves.maximum_download_bytes // 3),
                    )
                except Exception as error:
                    raise KetchError(
                        f"Could not retrieve rendered DOM for {final_url}: {error}"
                    ) from error
                if isinstance(rendered_html, dict) and rendered_html.get("too_large"):
                    length = rendered_html.get("length")
                    raise KetchError(
                        f"Rendered DOM from {final_url} exceeds the configured "
                        f"character limit ({length})."
                    )
                if not isinstance(rendered_html, str):
                    raise KetchError(f"Rendered DOM from {final_url} was unavailable.")
                try:
                    rendered_bytes = rendered_html.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise KetchError(
                        "Rendered browser content is not valid Unicode and cannot be extracted."
                    ) from error
                if len(rendered_bytes) > self.valves.maximum_download_bytes:
                    raise KetchError(
                        f"Rendered DOM from {final_url} exceeds the "
                        f"{self.valves.maximum_download_bytes}-byte limit."
                    )
                return {
                    "url": final_url,
                    "html": rendered_html,
                }
        except KetchError:
            raise
        except Exception as error:
            if resource_budget.exhausted:
                raise KetchError(
                    "Browser resources exceeded the configured "
                    "combined download limit."
                ) from error
            if navigation_redirect_failure is not None:
                status, failed_url, redirect_url = navigation_redirect_failure
                raise _unfollowed_redirect_error(
                    failed_url,
                    status,
                    redirect_url,
                    follow_same_host=follow_same_host,
                ) from error
            if navigation_failure is not None:
                status, failed_url = navigation_failure
                raise KetchError(
                    f"HTTP {status} while rendering {failed_url}."
                ) from error
            raise KetchError(
                f"Protected browser rendering failed for {url}: {error}"
            ) from error
        finally:
            if context is not None:
                await context.close()

    @staticmethod
    def _html_to_utf8_bytes(rendered: dict[str, str]) -> bytes:
        html_text = rendered.get("html")
        if not isinstance(html_text, str):
            raise KetchError("Rendered browser content did not contain HTML text.")
        try:
            return html_text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise KetchError(
                "Rendered browser content is not valid Unicode and cannot be extracted."
            ) from error

    async def _extract_html(
        self,
        runtime: dict,
        environment: dict[str, str],
        source_url: str,
        body: bytes,
        max_chars: int,
    ) -> dict[str, Any]:
        result = await self._run_process(
            [
                str(runtime["binary"]),
                "extract",
                "--url",
                source_url,
                "--max-chars",
                str(max_chars),
                "--json",
            ],
            environment=environment,
            timeout_seconds=self.valves.command_timeout_seconds,
            input_bytes=body,
            output_limit=min(
                MAX_SUBPROCESS_STDOUT_BYTES,
                max_chars * 6 + 50_000,
            ),
        )
        if result["exit_code"] != 0:
            category = ERROR_CATEGORIES.get(result["exit_code"], "internal")
            detail = (result["stderr"] or result["stdout"]).strip()
            raise KetchError(f"Ketch {category} error: {detail}")
        try:
            output = json.loads(result["stdout"])
        except json.JSONDecodeError as error:
            raise KetchError(
                "Ketch returned invalid JSON while extracting HTML."
            ) from error
        if not isinstance(output, dict):
            raise KetchError("Ketch returned an unexpected HTML extraction result.")
        return output

    async def _extract_pdf(
        self,
        runtime: dict,
        environment: dict[str, str],
        source_url: str,
        body: bytes,
        content_type: str,
        max_chars: int,
    ) -> dict[str, Any]:
        body = _trim_pdf_trailing_padding(body)
        async with _LocalContentServer(body, content_type) as local_url:
            result = await self._run_process(
                [
                    str(runtime["binary"]),
                    "scrape",
                    local_url,
                    "--no-cache",
                    "--cookie-file",
                    "",
                    "--max-chars",
                    str(max_chars),
                    "--json",
                ],
                environment=environment,
                timeout_seconds=self.valves.command_timeout_seconds,
                output_limit=min(
                    MAX_SUBPROCESS_STDOUT_BYTES,
                    max_chars * 6 + 50_000,
                ),
            )
        if result["exit_code"] != 0:
            category = ERROR_CATEGORIES.get(result["exit_code"], "internal")
            detail = (result["stderr"] or result["stdout"]).strip()
            raise KetchError(f"Ketch PDF {category} error: {detail}")
        try:
            output = json.loads(result["stdout"])
        except json.JSONDecodeError as error:
            raise KetchError(
                "Ketch returned invalid JSON while extracting a PDF."
            ) from error
        if isinstance(output, list) and len(output) == 1:
            output = output[0]
        if not isinstance(output, dict):
            raise KetchError("Ketch returned an unexpected PDF extraction result.")
        output["url"] = source_url
        output.pop("fetched_url", None)
        return output

    @staticmethod
    def _extraction_markdown(
        extracted: dict[str, Any],
        extraction_limit: int,
    ) -> tuple[str, bool]:
        markdown = extracted.get("markdown")
        if not isinstance(markdown, str):
            raise KetchError("Ketch extraction did not contain Markdown text.")
        truncation_marker = "\n\n[truncated]"
        ketch_truncated = (
            markdown.endswith(truncation_marker)
            and len(markdown) - len(truncation_marker) == extraction_limit
        )
        if ketch_truncated:
            markdown = markdown[: -len(truncation_marker)]
        return markdown, ketch_truncated

    def _paginate_markdown(
        self,
        extracted: dict[str, Any],
        markdown: str,
        source_truncated: bool,
        start_char: int,
        max_chars: int,
    ) -> None:
        page_end = start_char + max_chars
        has_more = source_truncated or len(markdown) > page_end
        ceiling_reached = (
            len(markdown) >= self.valves.maximum_page_characters and source_truncated
        )
        chunk = markdown[start_char:page_end]
        if not chunk.strip():
            if start_char:
                raise KetchError(
                    f"start_char {start_char} is beyond the readable extracted content."
                )
            raise KetchError("Ketch extracted no readable content from the page.")
        extracted["markdown"] = chunk
        extracted["words"] = len(chunk.split())
        extracted["pagination"] = {
            "start_char": start_char,
            "returned_characters": len(chunk),
            "next_start_char": (
                page_end
                if has_more and page_end < self.valves.maximum_page_characters
                else None
            ),
            "truncated": has_more or ceiling_reached,
            "configured_ceiling_reached": ceiling_reached,
        }

    def _paginate_extraction(
        self,
        extracted: dict[str, Any],
        start_char: int,
        max_chars: int,
        extraction_limit: int,
    ) -> None:
        markdown, source_truncated = self._extraction_markdown(
            extracted,
            extraction_limit,
        )
        self._paginate_markdown(
            extracted,
            markdown,
            source_truncated,
            start_char,
            max_chars,
        )

    @staticmethod
    def _document_headings(markdown: str) -> tuple[list[dict], bool]:
        headings = []
        fence_character = None
        fence_length = 0
        offset = 0
        for line in markdown.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            if fence_character is not None:
                closing_fence = re.fullmatch(
                    rf" {{0,3}}{re.escape(fence_character)}"
                    rf"{{{fence_length},}}[ \t]*",
                    content,
                )
                if closing_fence:
                    fence_character = None
                    fence_length = 0
                offset += len(line)
                continue

            opening_fence = re.match(
                r"^ {0,3}(`{3,}|~{3,})",
                content,
            )
            if opening_fence:
                marker = opening_fence.group(1)
                fence_character = marker[0]
                fence_length = len(marker)
                offset += len(line)
                continue

            match = re.fullmatch(
                r"(#{1,6})[ \t]+(.+?)[ \t]*",
                content,
            )
            if match is None:
                offset += len(line)
                continue
            title = re.sub(r"\s+", " ", match.group(2)).strip()
            if not title:
                offset += len(line)
                continue
            headings.append(
                {
                    "level": len(match.group(1)),
                    "title": title[:MAX_DOCUMENT_HEADING_CHARACTERS],
                    "start_char": offset,
                }
            )
            offset += len(line)
        preferred = [heading for heading in headings if heading["level"] <= 3]
        if len(preferred) < MAX_DOCUMENT_HEADINGS:
            selected_offsets = {heading["start_char"] for heading in preferred}
            preferred.extend(
                heading
                for heading in headings
                if heading["start_char"] not in selected_offsets
            )
        selected = sorted(
            preferred[:MAX_DOCUMENT_HEADINGS],
            key=lambda heading: heading["start_char"],
        )
        return selected, len(headings) > len(selected)

    def _compact_search_results(self, response: str) -> str:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            return response
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "success"
            or not isinstance(payload.get("result"), list)
        ):
            return response

        compact = []
        canonical_indexes = {}
        for result in payload["result"]:
            if not isinstance(result, dict):
                continue
            title = str(result.get("title") or "").strip()
            url = str(result.get("url") or "").strip()
            description = str(result.get("description") or "").strip()
            content = str(result.get("content") or "").strip()
            if not description or description.lower() in {
                "published: n/a",
                "published n/a",
            }:
                description = content
            lines = []
            for line in description.splitlines():
                line = " ".join(line.split())
                if (
                    not line
                    or line == "..."
                    or line.lower() in {"published: n/a", "published n/a"}
                    or line == title
                ):
                    continue
                lines.append(line)
            snippet = " ".join(lines)
            limit = self.valves.maximum_search_snippet_characters
            if len(snippet) > limit:
                snippet = snippet[: max(0, limit - 3)].rstrip() + "..."
            item = {
                "title": title,
                "url": url,
                "snippet": snippet,
                "retrieval_status": "discovery_only",
                "claim_eligible": False,
            }
            backends = result.get("backends")
            if isinstance(backends, list):
                item["backends"] = backends
            canonical_url = None
            if url:
                try:
                    canonical_url = _canonical_url_identity(url)
                except KetchError:
                    pass
            if canonical_url in canonical_indexes:
                existing = compact[canonical_indexes[canonical_url]]
                if not existing["title"] and title:
                    existing["title"] = title
                if not existing["snippet"] and snippet:
                    existing["snippet"] = snippet
                if isinstance(backends, list):
                    existing_backends = existing.setdefault("backends", [])
                    existing_backends.extend(
                        backend
                        for backend in backends
                        if backend not in existing_backends
                    )
                continue
            if canonical_url is not None:
                canonical_indexes[canonical_url] = len(compact)
            compact.append(item)
        payload["result"] = compact
        return json.dumps(payload, ensure_ascii=False)

    def _search_backends(self) -> tuple[str, ...]:
        values = tuple(
            dict.fromkeys(
                item.strip().lower()
                for item in self.valves.search_backends.split(",")
                if item.strip()
            )
        )
        if not values:
            raise KetchError("At least one Ketch search backend is required.")
        unsupported = sorted(set(values) - KEYLESS_SEARCH_BACKENDS)
        if unsupported:
            raise KetchError(
                "Unsupported or non-keyless search backends: " + ", ".join(unsupported)
            )
        return values

    async def _execute_ketch(
        self,
        request: Request,
        user: Any,
        event_emitter,
        arguments: list[str],
        operation: str,
    ) -> str:
        if not _user_id(user):
            return _json_error(
                "authentication",
                "Ketch requires an authenticated Open WebUI user.",
            )
        runtime = await self._ensure_runtime(request, event_emitter)
        await self._emit_status(event_emitter, f"Running Ketch {operation}", False)
        with tempfile.TemporaryDirectory(
            prefix="open-webui-ketch-",
            dir=str(self._temporary_parent(runtime.get("root"))),
        ) as home:
            (Path(home) / "tmp").mkdir(mode=0o700)
            environment = self._request_environment(home)
            result = await self._run_process(
                [str(runtime["binary"]), *arguments],
                environment=environment,
                timeout_seconds=self.valves.command_timeout_seconds,
                output_limit=self.valves.maximum_result_characters,
            )
        await self._emit_status(event_emitter, f"Ketch {operation} finished", True)

        if result["exit_code"] != 0:
            category = ERROR_CATEGORIES.get(result["exit_code"], "internal")
            message = (result["stderr"] or result["stdout"]).strip()
            if not message:
                message = f"Ketch exited with code {result['exit_code']}."
            return _json_error(
                category,
                message,
                exit_code=result["exit_code"],
            )
        try:
            output = json.loads(result["stdout"])
        except json.JSONDecodeError:
            output = result["stdout"]
        response = {
            "status": "success",
            "operation": operation,
            "result": output,
        }
        warnings = _compact_ketch_warnings(result["stderr"])
        if warnings:
            response["warnings"] = warnings
        return json.dumps(response, ensure_ascii=False)

    async def ketch_search(
        self,
        query: str,
        limit: int = 5,
        __request__: Optional[Request] = None,
        __user__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        DIRECT OPEN WEBUI TOOL: call ketch_search by name. Never route it
        through execute_tool or search for it with search_tools.

        Search the current public web for candidate sources. Results contain
        titles, URLs, and discovery snippets, not verified page evidence.
        Search coverage is non-exhaustive: results cannot prove no matches,
        complete coverage, or a latest/top ranking. Verify required dates from
        retrieved evidence.
        Before generic extraction, first check whether a selected platform or
        source has an available source-specific structured retriever; if
        availability is unknown, perform one focused capability-discovery
        check. Use ketch_scrape for the smallest relevant set, normally one or
        two pages, only when no source-specific retriever fits. Never repeat an
        equivalent query; refine once only for a specific unresolved fact,
        then use available evidence or disclose the limitation. Cite only
        pages actually retrieved.

        :param query: Specific web search query.
        :param limit: Number of ranked results, from 1 to the configured maximum.
        :return: JSON containing federated Ketch search results or an explicit error.
        """
        if __request__ is None:
            return _json_error(
                "request_context",
                "Ketch cannot run because Open WebUI request context is unavailable.",
            )

        async def finish(response: str) -> str:
            return await self._decorate_public_response(
                response,
                __request__,
                __user__,
                __metadata__,
                "search",
            )

        async def reject(response: str) -> str:
            usage_error = await self._reserve_invalid_usage(
                __request__,
                __user__,
                __metadata__,
                "search",
                signature=self._invalid_usage_signature(
                    "search",
                    {
                        "query": query,
                        "limit": limit,
                    },
                ),
            )
            return await finish(
                _json_error("policy", usage_error) if usage_error else response
            )

        if not _user_id(__user__):
            return await finish(
                _json_error(
                    "authentication",
                    "Ketch requires an authenticated Open WebUI user.",
                )
            )
        if not isinstance(query, str) or not query.strip():
            return await reject(
                _json_error("validation", "Search query must not be empty.")
            )
        if len(query) > 500:
            return await reject(
                _json_error(
                    "validation",
                    "Search query must not exceed 500 characters.",
                )
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.valves.maximum_search_results
        ):
            return await reject(
                _json_error(
                    "validation",
                    f"Search limit must be between 1 and "
                    f"{self.valves.maximum_search_results}.",
                )
            )
        usage_error = await self._reserve_search_usage(
            __request__,
            __user__,
            __metadata__,
            query,
        )
        if usage_error:
            return await finish(_json_error("policy", usage_error))
        try:
            backends = self._search_backends()
            return await finish(
                self._compact_search_results(
                    await self._execute_ketch(
                        __request__,
                        __user__,
                        __event_emitter__,
                        [
                            "search",
                            f"--multi={','.join(backends)}",
                            "--limit",
                            str(limit),
                            "--json",
                            "--",
                            query.strip(),
                        ],
                        "search",
                    )
                )
            )
        except KetchError as error:
            await self._emit_error(__event_emitter__, str(error))
            return await finish(_json_error("runtime", str(error)))

    async def _run_scrape_requests(
        self,
        request_specs: list[dict],
        preflight_errors: list[dict],
        usage_key: Optional[str],
        request: Request,
        user: Any,
        metadata: Optional[dict],
        event_emitter,
    ) -> str:
        async def finish(response: str, *, commit_ledger: bool = True) -> str:
            return await self._decorate_public_response(
                response,
                request,
                user,
                metadata,
                "scrape",
                commit_ledger=commit_ledger,
            )

        created_document_ids = []
        try:
            requires_extraction = any(
                not isinstance(spec.get("document_id"), str) for spec in request_specs
            )
            runtime = {}
            if requires_extraction:
                runtime = await self._ensure_runtime(
                    request,
                    event_emitter,
                    require_browser=False,
                )
            await self._emit_status(
                event_emitter,
                (
                    "Fetching content for Ketch extraction"
                    if requires_extraction
                    else "Reading cached Ketch document content"
                ),
                False,
            )
            results = []
            runtime_errors = []
            issued_ids = set()

            def issue_continuation_id() -> str:
                continuation_id = ""
                while not continuation_id or continuation_id in issued_ids:
                    continuation_id = "kc_" + secrets.token_urlsafe(18)
                issued_ids.add(continuation_id)
                return continuation_id

            def attach_navigation(
                extracted: dict,
                document_info: Optional[dict] = None,
            ) -> None:
                offset_tokens = {}

                def token_for(offset: int) -> str:
                    token = offset_tokens.get(offset)
                    if token is None:
                        token = issue_continuation_id()
                        offset_tokens[offset] = token
                    return token

                pagination = extracted["pagination"]
                next_start_char = pagination["next_start_char"]
                pagination["continuation_request"] = (
                    {
                        "continuation_ids": [token_for(next_start_char)],
                    }
                    if next_start_char is not None
                    else None
                )
                if not isinstance(document_info, dict):
                    return
                returned_end = (
                    pagination["start_char"] + pagination["returned_characters"]
                )
                headings = []
                for heading in document_info["headings"]:
                    item = dict(heading)
                    if item["start_char"] >= returned_end:
                        item["continuation_id"] = token_for(item["start_char"])
                    headings.append(item)
                extracted["document_map"] = {
                    "source_characters": document_info["source_characters"],
                    "extraction_complete": not document_info["source_truncated"],
                    "configured_ceiling_reached": document_info["source_truncated"],
                    "headings_truncated": document_info["headings_truncated"],
                    "headings": headings,
                    "navigation_notice": (
                        "Use a heading continuation_id to inspect only a "
                        "relevant section. A document map is navigation "
                        "metadata, not evidence that the whole document was read."
                    ),
                }

            scrape_root = (
                runtime.get("root")
                if isinstance(runtime.get("root"), Path)
                else _safe_install_root(self.valves.installation_root)
            )
            with tempfile.TemporaryDirectory(
                prefix="open-webui-ketch-scrape-",
                dir=str(self._temporary_parent(scrape_root)),
            ) as home:
                home_path = Path(home)
                for directory in ("config", "cache", "data", "tmp"):
                    (home_path / directory).mkdir(mode=0o700)
                environment = (
                    self._request_environment(home)
                    if requires_extraction
                    else {}
                )

                async with AsyncExitStack() as browser_stack:
                    browser = None

                    async def render(url: str) -> dict[str, str]:
                        nonlocal browser, runtime
                        if browser is None:
                            runtime = await self._ensure_runtime(
                                request,
                                event_emitter,
                                require_browser=True,
                            )
                            browser_path = runtime.get("browser")
                            if not isinstance(browser_path, Path):
                                raise KetchError("Managed Chromium is unavailable.")
                            browser = await browser_stack.enter_async_context(
                                self._protected_browser(browser_path, home)
                            )
                        return await self._safe_render_html(url, browser)

                    for spec_index, spec in enumerate(request_specs):
                        submitted_url = spec["url"]
                        force_browser = spec["force_browser"]
                        max_chars = spec["max_chars"]
                        start_char = spec["start_char"]
                        document_id = spec.get("document_id")
                        document_mode = bool(spec.get("document_mode"))
                        extraction_limit = min(
                            self.valves.maximum_page_characters,
                            (
                                self.valves.maximum_page_characters
                                if document_mode
                                else start_char + max_chars + 1
                            ),
                        )
                        try:
                            if isinstance(document_id, str):
                                cached = await self._read_document_cache(
                                    request,
                                    user,
                                    metadata,
                                    document_id,
                                    start_char,
                                    max_chars,
                                )
                                if cached is None:
                                    runtime_errors.append(
                                        {
                                            "continuation_id": spec.get(
                                                "continuation_id"
                                            ),
                                            "category": "policy",
                                            "error": (
                                                "The document cache entry is "
                                                "unavailable or expired. Do not "
                                                "refetch the page in this message; "
                                                "use existing evidence and disclose "
                                                "incomplete reading."
                                            ),
                                        }
                                    )
                                    continue
                                source = cached["source"]
                                markdown = cached["markdown"]
                                extracted = dict(source["extracted"])
                                self._paginate_markdown(
                                    extracted,
                                    markdown,
                                    bool(source["source_truncated"]),
                                    start_char,
                                    max_chars,
                                )
                                extracted["requested_url"] = submitted_url
                                extracted["resolved_url"] = source["resolved_url"]
                                extracted["retrieval_mode"] = source["retrieval_mode"]
                                extracted["document_mode"] = True
                                extracted["content_complete"] = cached[
                                    "content_complete"
                                ]
                                attach_navigation(extracted)
                                extracted["_ketch_request_index"] = spec_index
                                results.append(extracted)
                                continue

                            async def retrieve_and_extract():
                                path = urllib.parse.urlsplit(submitted_url).path.lower()
                                if force_browser and not path.endswith(".pdf"):
                                    rendered = await render(submitted_url)
                                    return (
                                        await self._extract_html(
                                            runtime,
                                            environment,
                                            rendered["url"],
                                            self._html_to_utf8_bytes(rendered),
                                            extraction_limit,
                                        ),
                                        rendered["url"],
                                        "protected_browser",
                                    )

                                fetched = await self._safe_fetch(submitted_url)
                                final_url = fetched["url"]
                                content_type = fetched["content_type"]
                                body = fetched["body"]
                                media_type = (
                                    content_type.split(";", 1)[0].strip().lower()
                                )
                                if (
                                    body.startswith(b"%PDF-")
                                    or media_type == "application/pdf"
                                ):
                                    return (
                                        await self._extract_pdf(
                                            runtime,
                                            environment,
                                            final_url,
                                            body,
                                            content_type or "application/pdf",
                                            extraction_limit,
                                        ),
                                        final_url,
                                        "protected_pdf",
                                    )

                                decoded = body.decode("utf-8", errors="ignore")
                                should_render = (
                                    self.valves.enable_browser
                                    and await asyncio.to_thread(
                                        _likely_javascript_shell,
                                        decoded,
                                    )
                                )
                                if should_render:
                                    rendered = await render(final_url)
                                    final_url = rendered["url"]
                                    body = self._html_to_utf8_bytes(rendered)
                                    retrieval_mode = "protected_browser"
                                else:
                                    retrieval_mode = "protected_http"
                                return (
                                    await self._extract_html(
                                        runtime,
                                        environment,
                                        final_url,
                                        body,
                                        extraction_limit,
                                    ),
                                    final_url,
                                    retrieval_mode,
                                )

                            if document_mode:
                                document_state = self._document_cache_state(request)
                                async with document_state["extraction_semaphore"]:
                                    (
                                        extracted,
                                        final_url,
                                        retrieval_mode,
                                    ) = await retrieve_and_extract()
                            else:
                                (
                                    extracted,
                                    final_url,
                                    retrieval_mode,
                                ) = await retrieve_and_extract()
                            _reject_access_challenge(extracted)
                            document_info = None
                            if document_mode:
                                markdown, source_truncated = self._extraction_markdown(
                                    extracted,
                                    extraction_limit,
                                )
                                headings, headings_truncated = self._document_headings(
                                    markdown
                                )
                                source = {
                                    "extracted": {
                                        "title": str(
                                            extracted.get("title") or final_url
                                        )[:300],
                                        ACCESS_CHALLENGE_CHECKED_KEY: True,
                                    },
                                    "resolved_url": final_url,
                                    "retrieval_mode": retrieval_mode,
                                    "source_truncated": source_truncated,
                                    "initial_characters": min(
                                        len(markdown),
                                        max_chars,
                                    ),
                                }
                                document_id = await self._store_document_cache(
                                    request,
                                    user,
                                    metadata,
                                    markdown,
                                    source,
                                )
                                created_document_ids.append(document_id)
                                spec["document_id"] = document_id
                                self._paginate_markdown(
                                    extracted,
                                    markdown,
                                    source_truncated,
                                    start_char,
                                    max_chars,
                                )
                                extracted["content_complete"] = bool(
                                    not source_truncated and len(markdown) <= max_chars
                                )
                                extracted["document_mode"] = True
                                document_info = {
                                    "source_characters": len(markdown),
                                    "source_truncated": source_truncated,
                                    "headings": headings,
                                    "headings_truncated": headings_truncated,
                                }
                            else:
                                self._paginate_extraction(
                                    extracted,
                                    start_char,
                                    max_chars,
                                    extraction_limit,
                                )
                            extracted["requested_url"] = submitted_url
                            extracted["resolved_url"] = final_url
                            extracted["retrieval_mode"] = retrieval_mode
                            attach_navigation(
                                extracted,
                                document_info,
                            )
                            extracted["_ketch_request_index"] = spec_index
                            results.append(extracted)
                        except asyncio.CancelledError:
                            raise
                        except KetchError as error:
                            if document_id in created_document_ids:
                                await self._remove_document_cache(
                                    request,
                                    [document_id],
                                )
                                created_document_ids.remove(document_id)
                            runtime_error = {
                                "url": submitted_url,
                                "error": str(error),
                            }
                            if isinstance(spec.get("continuation_id"), str):
                                runtime_error["continuation_id"] = spec[
                                    "continuation_id"
                                ]
                            if isinstance(error, AccessChallengeError):
                                runtime_error["category"] = "access_challenge"
                            runtime_errors.append(runtime_error)

            errors = preflight_errors + runtime_errors
            status = _scrape_status(results, errors)
            await self._emit_status(
                event_emitter,
                f"Ketch extraction finished with status: {status}",
                True,
            )
            response = {
                "status": status,
                "operation": "scrape",
                "results": results,
                "errors": errors,
            }
            serialized = await finish(
                json.dumps(response, ensure_ascii=False),
                commit_ledger=False,
            )
            if len(serialized) > self.valves.maximum_result_characters:
                await self._rollback_continuation_claims(
                    request,
                    usage_key,
                    request_specs,
                )
                raise KetchError(
                    "Combined Ketch output exceeded the configured total result limit."
                )
            await self._record_scrape_usage(
                request,
                usage_key,
                results,
                runtime_errors,
                request_specs,
            )
            await self._commit_response_ledger(
                serialized,
                request,
                user,
                metadata,
                "scrape",
            )
            await self._emit_scrape_sources(
                request,
                user,
                metadata,
                event_emitter,
                results,
            )
            return serialized
        except KetchError as error:
            await self._rollback_continuation_claims(
                request,
                usage_key,
                request_specs,
            )
            await self._remove_document_cache(
                request,
                created_document_ids,
            )
            await self._emit_error(event_emitter, str(error))
            await self._emit_status(
                event_emitter,
                "Ketch extraction failed",
                True,
            )
            return await finish(_json_error("runtime", str(error)))
        except asyncio.CancelledError:
            await self._rollback_continuation_claims(
                request,
                usage_key,
                request_specs,
            )
            await self._remove_document_cache(
                request,
                created_document_ids,
            )
            raise
        except Exception as error:
            await self._rollback_continuation_claims(
                request,
                usage_key,
                request_specs,
            )
            await self._remove_document_cache(
                request,
                created_document_ids,
            )
            log.exception("Unexpected Ketch extraction failure")
            await self._emit_error(
                event_emitter,
                "Ketch extraction failed unexpectedly.",
            )
            await self._emit_status(
                event_emitter,
                "Ketch extraction failed",
                True,
            )
            return await finish(
                _json_error(
                    "internal",
                    f"Ketch extraction failed unexpectedly: {error}",
                )
            )

    async def ketch_scrape(
        self,
        urls: list[str],
        force_browser: bool = False,
        max_chars: int = 8_000,
        document_mode: bool = False,
        __request__: Optional[Request] = None,
        __user__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        DIRECT OPEN WEBUI TOOL: call ketch_scrape by name. Never route it
        through execute_tool or search for it with search_tools. Continue a
        partial whole-page task with the directly available
        ketch_scrape_continue function.

        Fetch one to three public URLs and extract clean Markdown. Runtime
        network and process isolation is enforced internally. Search first
        when the user did not supply a URL, then scrape only the smallest
        relevant set and cite only inspected pages.

        Normal calls retrieve only initial page chunks and do not retain page
        bodies. document_mode=true accepts exactly one URL and is only for a
        requested summary, review, highlights, or section navigation on that
        one long page. Do not enable it merely because an ordinary result is
        truncated, or for a narrow multi-source comparison or feature check.
        It returns a bounded cached extraction and a heading map; content
        beyond its stated ceiling remains incomplete.
        For a whole-page summary or review in document mode, use only the exact
        identifiers in pagination.continuation_request and follow sequential
        continuations until content_complete is true. Do not use heading-map
        identifiers for a whole-page task. For requested highlights or a
        summary limited to named topics or sections, choose at most three
        matching heading identifiers and describe the result as targeted, not
        exhaustive; never pass all heading identifiers. If the call budget,
        context budget, configured ceiling, or an error prevents completion,
        explicitly state that the source was only partially read.

        Never repeat the same URL with the same parameters. Use force_browser
        once only after a successful static result is clearly an incomplete
        JavaScript shell; never use it after an HTTP, redirect, timeout,
        validation, or transport error. After two different candidates fail
        for the same fact, stop and disclose the limitation. After a terminal
        budget error, do not call Ketch again.

        :param urls: Public HTTP(S) URLs, up to the configured per-call limit of three by default; document_mode=true requires exactly one URL. Split larger normal selections across calls.
        :param force_browser: Render with Chromium only for one URL whose successful static result was an incomplete JavaScript shell; never use it to retry HTTP or redirect errors, or after a result whose retrieval_mode is already protected_browser.
        :param max_chars: Markdown characters per page, from 1000 through 8000. Never exceed 8000.
        :param document_mode: Opt into one bounded full extraction and message-scoped document map only for requested highlights, a summary, review, or section lookup on exactly one long page; never use it merely because a normal extract is truncated or for narrow multi-source research.
        :return: JSON containing extracted page content or an explicit error.
        """
        if __request__ is None:
            return _json_error(
                "request_context",
                "Ketch cannot run because Open WebUI request context is unavailable.",
            )

        async def finish(response: str) -> str:
            return await self._decorate_public_response(
                response,
                __request__,
                __user__,
                __metadata__,
                "scrape",
            )

        async def reject(response: str) -> str:
            usage_error = await self._reserve_invalid_usage(
                __request__,
                __user__,
                __metadata__,
                "initial",
                signature=self._invalid_usage_signature(
                    "scrape",
                    {
                        "urls": urls,
                        "force_browser": force_browser,
                        "max_chars": max_chars,
                        "document_mode": document_mode,
                    },
                ),
            )
            return await finish(
                _json_error("policy", usage_error) if usage_error else response
            )

        if not _user_id(__user__):
            return await finish(
                _json_error(
                    "authentication",
                    "Ketch requires an authenticated Open WebUI user.",
                )
            )
        if not isinstance(urls, list) or not urls:
            return await reject(
                _json_error(
                    "validation",
                    "urls must be a non-empty list.",
                )
            )
        if len(urls) > self.valves.maximum_scrape_urls:
            return await reject(
                _json_error(
                    "validation",
                    f"At most {self.valves.maximum_scrape_urls} URLs may be "
                    "scraped at once. Split larger selections across calls.",
                )
            )
        if not isinstance(document_mode, bool):
            return await reject(
                _json_error(
                    "validation",
                    "document_mode must be true or false.",
                )
            )
        if not isinstance(force_browser, bool):
            return await reject(
                _json_error(
                    "validation",
                    "force_browser must be true or false.",
                )
            )
        if document_mode and len(urls) != 1:
            return await reject(
                _json_error(
                    "validation",
                    "document_mode accepts exactly one URL per call to bound "
                    "CPU and memory use. Retry with one selected URL only for "
                    "a requested whole-page summary, review, highlights, or "
                    "section lookup; otherwise leave document_mode false.",
                )
            )
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 1_000 <= max_chars <= self.valves.maximum_chunk_characters
        ):
            return await reject(
                _json_error(
                    "validation",
                    f"max_chars must be between 1000 and "
                    f"{self.valves.maximum_chunk_characters}.",
                )
            )
        if force_browser and not self.valves.enable_browser:
            return await reject(
                _json_error(
                    "precondition",
                    "Browser rendering is disabled in the Ketch Tool valves.",
                )
            )

        try:
            validated = [_validate_url_shape(url) for url in urls]
            canonical_urls = set()
            deduplicated = []
            for url in validated:
                canonical_url = _canonical_url_identity(url)
                if canonical_url in canonical_urls:
                    continue
                canonical_urls.add(canonical_url)
                deduplicated.append(url)
            validated = deduplicated
        except KetchError as error:
            await self._emit_error(__event_emitter__, str(error))
            return await reject(_json_error("validation", str(error)))

        (
            usage_key,
            usage_error,
            eligible_urls,
            preflight_errors,
        ) = await self._reserve_scrape_usage(
            __request__,
            __user__,
            __metadata__,
            validated,
            force_browser,
            max_chars,
            document_mode,
        )
        if usage_error:
            return await finish(_json_error("policy", usage_error))
        validated = eligible_urls

        request_specs = [
            {
                "url": url,
                "force_browser": force_browser,
                "max_chars": max_chars,
                "start_char": 0,
                "document_mode": document_mode,
            }
            for url in validated
        ]
        return await self._run_scrape_requests(
            request_specs,
            preflight_errors,
            usage_key,
            __request__,
            __user__,
            __metadata__,
            __event_emitter__,
        )

    async def ketch_scrape_continue(
        self,
        continuation_ids: list[str],
        __request__: Optional[Request] = None,
        __user__: Any = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> str:
        """
        DIRECT OPEN WEBUI TOOL: call ketch_scrape_continue by name. Never route
        it through execute_tool or search for it with search_tools.

        Continue partial Ketch page extracts using one to three exact opaque
        identifiers from the most recent result. Pass identifiers unchanged;
        they are single-use and already encode the URL, offset, chunk size, and
        browser mode.

        For a whole-page document summary or review, use only
        pagination.continuation_request and continue sequentially until
        content_complete is true. Do not use heading-map identifiers for a
        whole-page task. For named topics or sections, choose at most three
        matching heading identifiers; never pass all headings. For an ordinary
        long-page summary, switch once to ketch_scrape with document_mode=true
        instead of paging ordinary chunks. Otherwise continue only for a
        specific evidence gap. If an identifier fails or expires, do not
        restart the page; use existing evidence and disclose incomplete
        reading.

        :param continuation_ids: One to three opaque continuation identifiers returned by Ketch; combine independent pending identifiers when useful.
        :return: JSON containing subsequent page extracts or explicit per-identifier errors.
        """
        if __request__ is None:
            return _json_error(
                "request_context",
                "Ketch cannot run because Open WebUI request context is unavailable.",
            )

        async def finish(response: str) -> str:
            return await self._decorate_public_response(
                response,
                __request__,
                __user__,
                __metadata__,
                "scrape",
            )

        async def reject(response: str) -> str:
            usage_error = await self._reserve_invalid_usage(
                __request__,
                __user__,
                __metadata__,
                "continuation",
                (len(continuation_ids) if isinstance(continuation_ids, list) else 1),
                signature=self._invalid_usage_signature(
                    "continuation",
                    {
                        "continuation_ids": continuation_ids,
                    },
                ),
            )
            return await finish(
                _json_error("policy", usage_error) if usage_error else response
            )

        if not _user_id(__user__):
            return await finish(
                _json_error(
                    "authentication",
                    "Ketch requires an authenticated Open WebUI user.",
                )
            )
        if not isinstance(continuation_ids, list) or not continuation_ids:
            return await reject(
                _json_error(
                    "validation",
                    "continuation_ids must be a non-empty list.",
                )
            )
        if len(continuation_ids) > self.valves.maximum_scrape_urls:
            return await reject(
                _json_error(
                    "validation",
                    f"At most {self.valves.maximum_scrape_urls} continuation "
                    "identifiers may be used at once. Split larger selections "
                    "across calls.",
                )
            )
        if any(
            not isinstance(continuation_id, str)
            or not CONTINUATION_ID_PATTERN.fullmatch(continuation_id)
            for continuation_id in continuation_ids
        ) or len(set(continuation_ids)) != len(continuation_ids):
            return await reject(
                _json_error(
                    "validation",
                    "Each continuation identifier must be a unique, unchanged "
                    "Ketch identifier from pagination.continuation_request.",
                )
            )
        (
            usage_key,
            usage_error,
            request_specs,
            preflight_errors,
        ) = await self._reserve_continuation_usage(
            __request__,
            __user__,
            __metadata__,
            continuation_ids,
        )
        if usage_error:
            response = _json_error("policy", usage_error)
            if preflight_errors:
                payload = json.loads(response)
                payload["errors"] = preflight_errors
                response = json.dumps(payload, ensure_ascii=False)
            return await finish(response)
        return await self._run_scrape_requests(
            request_specs,
            preflight_errors,
            usage_key,
            __request__,
            __user__,
            __metadata__,
            __event_emitter__,
        )
