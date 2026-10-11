"""Validate authored Markdown and local links using only the standard library."""
from __future__ import annotations

import ast
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {
    ".git", ".venv", ".pytest_cache", ".hypothesis", "__pycache__",
    "node_modules", "logs", "build", "dist",
}
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
LINK_START = re.compile(r"\[[^\]\n]*\]\(")
DEFINITION = re.compile(r"^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)", re.MULTILINE)
REFERENCE = re.compile(r"\[([^\]\n]+)\]\[([^\]\n]*)\]")
CODE_DELIMITER = re.compile(r"`+")
EXTERNAL = {"http", "https", "mailto", "tel", "data"}


def markdown_files(root: Path) -> list[Path]:
    result = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in GENERATED)
        result.extend(Path(folder) / name for name in sorted(files)
                      if name.lower().endswith(".md"))
    return sorted(result)


def outside_fences(source: str) -> tuple[str, list[int]]:
    """Blank fenced content while preserving line numbers."""
    result, errors = [], []
    active = None
    opening = 0
    for number, line in enumerate(source.splitlines(), 1):
        match = FENCE.match(line)
        if active is not None:
            if match:
                marker = match.group(1)
                if (marker[0] == active[0] and len(marker) >= len(active)
                        and not line[match.end():].strip()):
                    active = None
            result.append("")
        elif match:
            active, opening = match.group(1), number
            result.append("")
        else:
            result.append(line)
    if active is not None:
        errors.append(opening)
    return "\n".join(result), errors


def anchor_ids(source: str) -> set[str]:
    """GitHub-style IDs for the headings used in these guides."""
    result = set()
    counts: Counter[str] = Counter()
    for line in source.splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", match.group(2))
        text = text.replace("`", "").lower()
        slug = "".join(c for c in text
                       if c in "-_ " or unicodedata.category(c)[0] in {"L", "N"})
        slug = slug.replace(" ", "-")
        count = counts[slug]
        candidate = f"{slug}-{count}" if count else slug
        while candidate in result:
            count += 1
            candidate = f"{slug}-{count}"
        counts[slug] = count + 1
        result.add(candidate)
    return result


def line_at(source: str, position: int) -> int:
    return source.count("\n", 0, position) + 1


def reference_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def is_escaped(source: str, position: int) -> bool:
    count = 0
    while position > 0 and source[position - 1] == "\\":
        position -= 1
        count += 1
    return count % 2 == 1


def mask_inline_code(source: str) -> str:
    """Hide code spans but preserve line offsets and escaped literal backticks."""
    result = list(source)
    delimiters = list(CODE_DELIMITER.finditer(source))
    index = 0
    while index < len(delimiters):
        opening = delimiters[index]
        if is_escaped(source, opening.start()):
            index += 1
            continue
        closing = index + 1
        while closing < len(delimiters):
            if delimiters[closing].group() == opening.group():
                break
            closing += 1
        if closing == len(delimiters):
            index += 1
            continue
        for position in range(opening.start(), delimiters[closing].end()):
            if result[position] != "\n":
                result[position] = " "
        index = closing + 1
    return "".join(result)


def inline_links(source: str):
    """Read inline destinations, including angle brackets and balanced parentheses."""
    for match in LINK_START.finditer(source):
        if is_escaped(source, match.start()):
            continue
        start = cursor = match.end()
        if cursor < len(source) and source[cursor] == "<":
            cursor += 1
            while cursor < len(source):
                if source[cursor] == ">" and not is_escaped(source, cursor):
                    break
                cursor += 1
            if cursor == len(source):
                continue
            end = cursor + 1
        else:
            depth = 0
            while cursor < len(source):
                char = source[cursor]
                if is_escaped(source, cursor):
                    cursor += 1
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace() and depth == 0:
                    break
                cursor += 1
            end = cursor
        # A destination may be followed by an optional Markdown title.
        if re.match(r"\s*(?:\"[^\"]*\"|'[^']*'|\([^)]*\))?\s*\)", source[end:]):
            yield match.start(), source[start:end]


def check(root: Path) -> tuple[list[str], int, int]:
    files = markdown_files(root)
    errors, sources, anchors, graph = [], {}, {}, {}
    links = 0

    def report(path: Path, number: int, message: str) -> None:
        errors.append(f"{path.relative_to(root).as_posix()}:{number}: {message}")

    for path in files:
        source = path.read_text(encoding="utf-8-sig")
        visible, unclosed = outside_fences(source)
        sources[path], anchors[path], graph[path] = visible, anchor_ids(visible), set()
        for number in unclosed:
            report(path, number, "unclosed fenced block")
        headings = [(i, m) for i, line in enumerate(visible.splitlines(), 1)
                    if (m := HEADING.match(line))]
        if sum(len(m.group(1)) == 1 for _, m in headings) != 1:
            report(path, 1, "expected exactly one top-level heading")
        lines = source.splitlines()
        for number, _ in headings:
            if number < len(lines) and lines[number].strip():
                report(path, number, "leave a blank line after a heading")

    def validate(path: Path, number: int, raw: str, *, connect: bool = True) -> None:
        nonlocal links
        target = raw.strip("<>")
        try:
            parsed = urlsplit(target)
        except ValueError:
            report(path, number, f"malformed link: {target}")
            return
        if parsed.scheme in EXTERNAL:
            return
        if parsed.scheme or parsed.netloc:
            report(path, number, f"unsupported link scheme: {target}")
            return
        if "\\" in parsed.path:
            report(path, number, f"use forward slashes in local link: {target}")
            return
        destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        if not destination.is_relative_to(root):
            report(path, number, f"link leaves repository: {target}")
            return
        links += 1
        if not destination.exists():
            report(path, number, f"missing local target: {target}")
            return
        if destination in sources:
            if connect:
                graph[path].add(destination)
            if parsed.fragment and unquote(parsed.fragment) not in anchors[destination]:
                report(path, number, f"missing Markdown heading anchor: {target}")

    for path, source in sources.items():
        visible = mask_inline_code(source)
        definitions = {}
        for match in DEFINITION.finditer(visible):
            definitions[reference_key(match.group(1))] = match.group(2)
            validate(path, line_at(visible, match.start()), match.group(2), connect=False)
        for position, target in inline_links(visible):
            validate(path, line_at(visible, position), target)
        for match in REFERENCE.finditer(visible):
            if is_escaped(visible, match.start()):
                continue
            key = reference_key(match.group(2) or match.group(1))
            if key not in definitions:
                report(path, line_at(visible, match.start()), f"undefined reference: [{key}]")
            else:
                validate(path, line_at(visible, match.start()), definitions[key])

    reached, pending = set(), [root / "README.md"]
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        pending.extend(graph.get(path, set()) - reached)
    for path in files:
        if path not in reached and path.name != "AGENTS.md":
            report(path, 1, "Markdown is not reachable from README.md")

    # Read AST rather than importing config or loading private .env values.
    config = root / "app/config.py"
    reference = root / "docs/development/configuration.md"
    sample = root / ".env.example"
    if config.exists() and reference.exists() and sample.exists():
        tree = ast.parse(config.read_text(encoding="utf-8"))
        keys = {
            n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id.startswith("_env_") and n.args
            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
        }
        reference_text = reference.read_text(encoding="utf-8")
        sample_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=",
                                    sample.read_text(encoding="utf-8"), re.MULTILINE))
        for key in sorted(keys):
            if not re.search(rf"\b{re.escape(key)}\b", reference_text):
                report(reference, 1, f"missing configuration setting: {key}")
            if key not in sample_keys:
                report(sample, 1, f"missing example setting: {key}")
        for suffix in ("NAME", "ENABLED"):
            if f"CMD_<KEY>_{suffix}" not in reference_text:
                report(reference, 1, f"missing command pattern: CMD_<KEY>_{suffix}")
    return errors, len(files), links


def main() -> int:
    errors, count, links = check(ROOT)
    if errors:
        print("\n".join(errors))
        print(f"Documentation check failed: {len(errors)} issue(s).")
        return 1
    print(f"Documentation check passed: {count} Markdown files, {links} local links.")
    print("External URLs and semantic claims require separate review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
