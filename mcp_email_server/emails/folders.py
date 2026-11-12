"""Parse IMAP special-use folders without guessing a localized mailbox name."""

import re


def validate_mailbox(name: str) -> str:
    if not name or len(name) > 1024 or any(c in name for c in "\r\n\x00"):
        raise ValueError("Invalid mailbox name")
    return name


def listed_folders(lines) -> list[tuple[set[str], str]]:  # noqa: C901 - bounded LIST grammar
    result = []
    for index, line in enumerate(lines):
        if isinstance(line, str):
            line = line.encode("utf-8")
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'^(?:\* LIST )?\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$', line, re.I)
        if not match:
            continue
        flags = set(match[1].decode("ascii").lower().split())
        if "\\noselect" in flags or "\\nonexistent" in flags:
            continue
        raw = match[2].strip()
        if re.fullmatch(rb"\{[0-9]+\}", raw):
            if index + 1 >= len(lines) or not isinstance(lines[index + 1], (bytes, bytearray)):
                continue
            raw = bytes(lines[index + 1])
            if len(raw) != int(match[2][1:-1]):
                continue
        elif raw.startswith(b'"'):
            if not raw.endswith(b'"'):
                continue
            raw = re.sub(rb'\\(["\\])', rb"\1", raw[1:-1])
        try:
            name = validate_mailbox(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        result.append((flags, name))
    return result


async def special_folder(imap, flag: str, fallbacks=()) -> str | None:
    status, lines = await imap.list('""', "*")
    if str(status).upper() != "OK":
        raise ValueError("Mailbox folder discovery unavailable")
    folders = listed_folders(lines)
    matches = {name for flags, name in folders if flag.lower() in flags}
    if not matches:
        matches = {name for _, name in folders if name.casefold() in {f.casefold() for f in fallbacks}}
    if len(matches) > 1:
        raise ValueError("Ambiguous special-use folder; configure the mailbox folder explicitly")
    return next(iter(matches), None)
