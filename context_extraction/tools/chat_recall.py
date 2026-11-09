"""
title: Chat Recall
description: Companion to the Context Extraction filter. Searches and reads the current chat's saved history, including tool calls and their results, so the model can recover details that Context Extraction removed from its context. Search ranks matches by their keywords in any language, and a tool call can be read in full by the ref that Context Extraction's stubs and notes name. Reads only the signed-in user's current chat. Named apart from Open WebUI's built-in search_chats and view_chat; turn off the built-in Chat History tool in the model's settings so the model uses this one instead.
version: 1.1.0
required_open_webui_version: 0.13.0
"""

import inspect
import json
import logging
import math
import re
import unicodedata
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MAX_RESULTS = 20
MAX_READ_MESSAGES = 10
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
RECALL_TOOL_RE = re.compile(r"(?:^|_)recall_(?:search|read)$")
QUALIFIED_REF_RE = re.compile(r"(\d+)/(.+)")

BM25_K1 = 1.2
BM25_B = 0.75
PREFIX_WEIGHT = 0.5
STOP_WORDS = frozenset(
    "a an and are can do for from in into is it me of on or that the this to with".split()
)
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
# Accents are dropped only after letters up to the end of the Cyrillic block.
LAST_FOLDED_BASE = 0x052F


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _parse_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _call_name(name: str, arguments: Any) -> tuple[str, Any]:
    """Name the tool that ran rather than the dispatcher that carried it."""
    arguments = _parse_arguments(arguments)
    if isinstance(arguments, dict):
        if name == "call_tool" and isinstance(arguments.get("tool_name"), str):
            return arguments["tool_name"], _parse_arguments(arguments.get("params"))
        if name == "call_tool" and isinstance(arguments.get("calls"), list):
            names = [
                entry["tool_name"]
                for entry in arguments["calls"]
                if isinstance(entry, dict) and isinstance(entry.get("tool_name"), str)
            ]
            if names:
                return ", ".join(names), arguments["calls"]
        if name == "execute_tool" and isinstance(arguments.get("tool"), str):
            return arguments["tool"], _parse_arguments(arguments.get("arguments"))
    return name, arguments


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}
    )


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def message_parts(message: dict) -> list[tuple[str, str, str, str]]:
    """Split a saved message into (kind, tool name, text, call id) parts, in order."""
    role = message.get("role") or "unknown"
    output = message.get("output")
    if role != "assistant" or not isinstance(output, list) or not output:
        text = _text_parts(message.get("content"))
        if role == "assistant":
            text = THINK_RE.sub("", text)
        return [("text", "", text.strip(), "")] if text.strip() else []

    parts = []
    names: dict[str, str] = {}
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        call_id = str(item.get("call_id") or "")
        if kind == "message":
            text = _text_parts(item.get("content")).strip()
            if text:
                parts.append(("text", "", text, ""))
        elif kind == "function_call":
            name, arguments = _call_name(str(item.get("name") or "tool"), item.get("arguments"))
            names[call_id] = name
            parts.append(("tool call", name, _dump(arguments), call_id))
        elif kind == "function_call_output":
            name = names.get(call_id, "tool")
            value = item.get("output")
            text = _text_parts(value) if isinstance(value, list) else _dump(value)
            parts.append(("tool result", name, text, call_id))
    return parts


def fold(text: str) -> tuple[str, Optional[list[int]]]:
    """Lower-case text and drop accents from Latin, Greek and Cyrillic letters.

    Returns the folded text and, unless the text is ASCII, the index in the original text of each folded character,
    because case folding and accent removal change lengths (ß becomes ss).
    """
    if text.isascii():
        return text.lower(), None
    pieces = []
    origin = []
    last_base = 0
    seen: dict[str, tuple[bool, str, int]] = {}
    for index, char in enumerate(text):
        known = seen.get(char)
        if known is None:
            known = seen[char] = _fold_char(char)
        is_mark, piece, base = known
        if is_mark:
            if last_base <= LAST_FOLDED_BASE:
                continue
        else:
            last_base = base
        pieces.append(piece)
        origin.extend([index] * len(piece))
    origin.append(len(text))
    return "".join(pieces), origin


def _fold_char(char: str) -> tuple[bool, str, int]:
    """Return whether a character is a non-spacing mark, its folded form, and the base it counts as."""
    code = ord(char)
    if code < 128:
        return False, char.lower(), code
    if unicodedata.category(char) == "Mn":
        return True, char, 0
    if code <= LAST_FOLDED_BASE or 0x1E00 <= code <= 0x1FFF:
        # Decomposing first lets a capital with a dot or accent (Turkish İ) fold to its base letter.
        decomposed = unicodedata.normalize("NFD", char)
        base = ord(decomposed[0])
        if base <= LAST_FOLDED_BASE:
            decomposed = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        return False, decomposed.casefold(), base
    # Other scripts are not decomposed, so Hangul syllables and marks that spell a sound stay whole.
    return False, char.casefold(), code


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 3 and word[-1] == "s" and word[-2] != "s" else word


def _keep_word(word: str, words: list[str]) -> None:
    word = _stem(word)
    if word and word not in STOP_WORDS and (len(word) > 1 or word.isdigit()):
        words.append(word)


def _paired_script(code: int) -> bool:
    """Approximate ranges of kana, CJK ideographs, Hangul syllables and Thai, which do not separate words."""
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0x0E00 <= code <= 0x0E7F
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x3134F
    )


def tokenize(text: str) -> list[str]:
    """Split text into folded words without a language model.

    Splits on anything that is not a letter or digit and on camelCase, keeps a combining mark with its letter,
    and turns runs of scripts that do not separate words into pairs of characters. Plurals and filler words are
    handled for English only.
    """
    words: list[str] = []
    if text.isascii():
        for match in ASCII_WORD_RE.finditer(text):
            for piece in CAMEL_RE.split(match.group()):
                _keep_word(piece.lower(), words)
        return words

    units: list[str] = []
    paired = False

    def flush() -> None:
        if not units:
            return
        if paired:
            if len(units) == 1:
                words.append(units[0])
            else:
                words.extend(units[i] + units[i + 1] for i in range(len(units) - 1))
        else:
            _keep_word(fold("".join(units))[0], words)
        units.clear()

    previous_lower = False
    categories: dict[str, str] = {}
    for char in text:
        category = categories.get(char)
        if category is None:
            category = categories[char] = unicodedata.category(char)
        if category[0] == "M":
            if units:
                units[-1] += char
            continue
        if category[0] != "L" and category != "Nd":
            flush()
            previous_lower = False
            continue
        is_paired = _paired_script(ord(char))
        if is_paired != paired or (not is_paired and category == "Lu" and previous_lower):
            flush()
        paired = is_paired
        units.append(char)
        previous_lower = category == "Ll"
    flush()
    return words


def _locate(folded: str, term: str) -> int:
    """Find a term loosely; a word reduced from a plural in -ies is also found in that plural."""
    position = folded.find(term)
    if position < 0 and len(term) >= 2 and term.endswith("y"):
        position = folded.find(term[:-1] + "ies")
    return position


def _snippet(text: str, position: int, length: int, width: int) -> str:
    start = max(0, position - (width - length) // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    snippet = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _original_span(origin: Optional[list[int]], position: int, length: int) -> tuple[int, int]:
    if origin is None:
        return position, length
    start = origin[min(position, len(origin) - 1)]
    end = origin[min(position + length, len(origin) - 1)]
    return start, max(1, end - start)


def _is_recall_call(tool: str) -> bool:
    return bool(RECALL_TOOL_RE.search(tool))


def _clean_ref(ref: Any) -> str:
    ref = str(ref or "").strip().strip("\"'`[]").strip()
    if ref.lower().startswith("ref "):
        ref = ref[4:].strip().strip("\"'`[]").strip()
    return ref


class Tools:
    class Valves(BaseModel):
        max_read_chars: int = Field(
            default=12_000,
            ge=1_000,
            description="Maximum characters recall_read returns per call; longer reads continue with offset.",
        )
        snippet_chars: int = Field(
            default=300,
            ge=80,
            description="Characters of context shown around each recall_search match.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _load_branch(self, chat_id: Any, message_id: Any, user: Any) -> list[dict]:
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
        if not user_id:
            raise ValueError("The signed-in user is not available.")
        if not chat_id or str(chat_id).startswith("local:"):
            raise ValueError("This chat is not saved, so its history cannot be searched.")

        from open_webui.models.chats import Chats
        from open_webui.utils.misc import get_message_list

        chat = await _resolve(Chats.get_chat_by_id_and_user_id(chat_id, user_id))
        if chat is None:
            raise ValueError("The current chat was not found for this user.")

        history = (getattr(chat, "chat", None) or {}).get("history") or {}
        messages_map = None
        if hasattr(Chats, "get_messages_map_by_chat_id"):
            messages_map = await _resolve(Chats.get_messages_map_by_chat_id(chat_id))
        if not messages_map:
            messages_map = history.get("messages") or {}

        start = message_id if message_id in messages_map else history.get("currentId")
        return get_message_list(messages_map, start) if start else []

    async def _branch_or_error(self, chat_id: Any, message_id: Any, user: Any):
        try:
            return await self._load_branch(chat_id, message_id, user), None
        except Exception as error:
            if not isinstance(error, ValueError):
                log.exception("Chat Recall could not load the chat")
            return None, f"Error: {error}"

    async def recall_search(
        self,
        query: str,
        match: str = "any",
        count: int = 5,
        __user__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> str:
        """
        Search the current chat's full saved history, including tool calls and tool results, for details that are no longer in context, such as earlier messages or tool output that was compacted away. Returns message numbers and tool call refs to pass to recall_read.
        :param query: Keywords, an exact phrase, or an identifier such as a file name or error code.
        :param match: "any" (default) ranks parts by how many of the keywords they contain and how closely; "all" returns only parts that contain every word, newest first.
        :param count: Maximum number of matches to return (default 5, at most 20).
        :return: JSON with matching parts, each with its message number, role, part kind, tool name, tool call ref, and a snippet.
        """
        phrase = " ".join(fold(str(query or ""))[0].split())
        if not phrase:
            return "Error: Provide a phrase or keywords to search for."
        branch, error = await self._branch_or_error(__chat_id__, __message_id__, __user__)
        if error:
            return error

        terms = list(dict.fromkeys(tokenize(str(query))))
        mode = "all" if str(match or "").strip().lower() == "all" or not terms else "any"
        words = phrase.split()
        parts = self._searchable(branch)
        average_length = max(1.0, sum(len(part["text"]) for part in parts) / max(1, len(parts)))

        candidates = []
        for part in parts:
            folded, origin = fold(part["text"])
            at = folded.find(phrase)
            phrase_hit = at >= 0
            if mode == "all":
                positions = [(_locate(folded, word), len(word)) for word in words]
                if min(positions)[0] < 0:
                    continue
                length = len(phrase)
                if not phrase_hit:
                    at, length = min(positions)
                candidates.append({"part": part, "at": at, "length": length, "origin": origin, "phrase": phrase_hit})
                continue

            positions = [(_locate(folded, term), len(term)) for term in terms]
            positions = [position for position in positions if position[0] >= 0]
            if not positions and not phrase_hit:
                continue
            counts = [0.0] * len(terms)
            for word in tokenize(part["text"]):
                for index, term in enumerate(terms):
                    if word == term:
                        counts[index] += 1
                    elif len(term) >= 3 and word.startswith(term):
                        counts[index] += PREFIX_WEIGHT
            if not phrase_hit and not any(counts):
                continue
            length = len(phrase)
            if not phrase_hit:
                at, length = min(positions) if positions else (0, 1)
            candidates.append(
                {"part": part, "at": at, "length": length, "origin": origin, "phrase": phrase_hit, "counts": counts}
            )

        if mode == "all":
            candidates.sort(key=lambda found: (not found["phrase"], -found["part"]["number"]))
        else:
            frequency = [sum(1 for found in candidates if found["counts"][index] > 0) for index in range(len(terms))]
            for found in candidates:
                score = 0.0
                for index, term_count in enumerate(found["counts"]):
                    if term_count <= 0:
                        continue
                    idf = math.log(1 + (len(parts) - frequency[index] + 0.5) / (frequency[index] + 0.5))
                    norm = BM25_K1 * (1 - BM25_B + BM25_B * len(found["part"]["text"]) / average_length)
                    score += idf * term_count * (BM25_K1 + 1) / (term_count + norm)
                found["key"] = (
                    -sum(1 for term_count in found["counts"] if term_count > 0),
                    not found["phrase"],
                    -round(score, 1),
                    -found["part"]["number"],
                )
            candidates.sort(key=lambda found: found["key"])

        width = self.valves.snippet_chars
        limit = max(1, min(int(count or 5), MAX_RESULTS))
        results = []
        for found in candidates[:limit]:
            part = found["part"]
            start, length = _original_span(found["origin"], found["at"], found["length"])
            results.append(
                {
                    "message": part["number"],
                    "role": part["role"],
                    "part": part["kind"],
                    **({"tool": part["tool"]} if part["tool"] else {}),
                    **({"ref": part["ref"]} if part["ref"] else {}),
                    "snippet": _snippet(part["text"], start, length, width),
                }
            )
        return json.dumps(
            {
                "messages_in_chat": len(branch),
                "match": mode,
                "matches": len(candidates),
                "results": results,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _searchable(branch: list[dict]) -> list[dict]:
        """List the parts search looks in, leaving out the user's latest message and Chat Recall's own calls."""
        latest_user = max(
            (number for number, message in enumerate(branch, start=1) if message.get("role") == "user"),
            default=0,
        )
        refs = _call_refs(branch)
        parts = []
        for number, message in enumerate(branch, start=1):
            if number == latest_user:
                continue
            for kind, tool, text, call_id in message_parts(message):
                if kind != "text" and _is_recall_call(tool):
                    continue
                parts.append(
                    {
                        "number": number,
                        "role": message.get("role") or "unknown",
                        "kind": kind,
                        "tool": tool,
                        "text": text,
                        "ref": refs.get((number, call_id), "") if call_id else "",
                    }
                )
        return parts

    async def recall_read(
        self,
        message: int = 0,
        count: int = 1,
        offset: int = 0,
        ref: str = "",
        __user__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> str:
        """
        Read the current chat's saved history in full: whole messages by number, or one tool call's arguments and output by its ref. Use message numbers and refs from recall_search, or a ref named in a Context Extraction stub or note.
        :param message: Number of the first message to read; the chat's first message is 1. Leave at 0 when passing ref.
        :param count: Number of consecutive messages to read (default 1, at most 10).
        :param offset: Character offset to continue a long read from, as given at the end of a previous result.
        :param ref: A tool call ref, such as a call ID or a message-qualified ref like 12/call_abc, to read that call's arguments and output.
        :return: The messages or tool call as a transcript, with a continuation note when more text remains.
        """
        branch, error = await self._branch_or_error(__chat_id__, __message_id__, __user__)
        if error:
            return error
        offset = max(0, int(offset or 0))

        ref = _clean_ref(ref)
        if ref:
            found = _find_call(branch, ref)
            if isinstance(found, str):
                return found
            number, call_id = found
            lines = [f"## Message {number} ({branch[number - 1].get('role') or 'unknown'}), tool call {ref}"]
            for kind, tool, text, part_call_id in message_parts(branch[number - 1]):
                if part_call_id == call_id and kind != "text":
                    lines.append(f"[{kind}: {tool}]\n{text}")
            if len(lines) == 2:
                lines.append("[The result of this call was not saved.]")
            return self._page("\n\n".join(lines), offset, f'recall_read(ref="{ref}", offset={{end}})')

        first = int(message or 0)
        if first < 1 or first > len(branch):
            return f"Error: message must be between 1 and {len(branch)} in this chat, or pass a tool call ref."
        count = max(1, min(int(count or 1), MAX_READ_MESSAGES))

        sections = []
        for number in range(first, min(first + count, len(branch) + 1)):
            item = branch[number - 1]
            lines = [f"## Message {number} ({item.get('role') or 'unknown'})"]
            for kind, tool, text, _ in message_parts(item):
                if kind == "text":
                    lines.append(text)
                else:
                    lines.append(f"[{kind}: {tool}]\n{text}")
            sections.append("\n\n".join(lines))
        return self._page(
            "\n\n".join(sections), offset, f"recall_read(message={first}, count={count}, offset={{end}})"
        )

    def _page(self, transcript: str, offset: int, continuation: str) -> str:
        if offset >= len(transcript) and transcript:
            return f"Error: offset is past the end of the {len(transcript):,} characters read."
        end = offset + self.valves.max_read_chars
        result = transcript[offset:end]
        if end < len(transcript):
            result += (
                f"\n\n[{len(transcript) - end:,} more characters. Continue with "
                f"{continuation.format(end=end)}.]"
            )
        return result


def _call_refs(branch: list[dict]) -> dict[tuple[int, str], str]:
    """Give each tool call a ref: its call ID, qualified by message number when the ID is reused in the chat."""
    numbers: dict[str, list[int]] = {}
    for number, message in enumerate(branch, start=1):
        for _, _, _, call_id in message_parts(message):
            if call_id and number not in numbers.setdefault(call_id, []):
                numbers[call_id].append(number)
    return {
        (number, call_id): call_id if len(found) == 1 else f"{number}/{call_id}"
        for call_id, found in numbers.items()
        for number in found
    }


def _find_call(branch: list[dict], ref: str):
    """Find the message holding a tool call ref, or explain why it cannot be read."""
    qualified = QUALIFIED_REF_RE.fullmatch(ref)
    if qualified:
        number, call_id = int(qualified.group(1)), qualified.group(2)
        if 1 <= number <= len(branch) and any(
            part[3] == call_id for part in message_parts(branch[number - 1])
        ):
            return number, call_id
    numbers = [
        number
        for number, message in enumerate(branch, start=1)
        if any(part[3] == ref for part in message_parts(message))
    ]
    if len(numbers) == 1:
        return numbers[0], ref
    if numbers:
        choices = ", ".join(f"{number}/{ref}" for number in numbers)
        return f"Error: {len(numbers)} tool calls in this chat have the ID {ref}. Pass one of these refs instead: {choices}."
    return (
        f'Error: No saved tool call in this chat has the ref "{ref}". Tool output from the reply in progress is '
        "saved only when that reply finishes, so it cannot be read yet; otherwise look for the detail with recall_search."
    )
