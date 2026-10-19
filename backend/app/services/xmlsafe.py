"""Read XML that JMeter wrote, including character references XML 1.0 forbids.

JMeter writes control characters from recorded or received data (binary bodies, for example)
as references such as &#x10; in .jmx and XML result files. Python's parser rejects them, so
before parsing each one becomes a private-use marker that ElementTree accepts; restore()
turns the markers back into the original references when writing XML again.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_CHAR_REF = re.compile(rb"&#(?:x([0-9a-fA-F]+)|([0-9]+));")
_MARK_OPEN, _MARK_CLOSE = "", ""
_MARKED = re.compile(f"{_MARK_OPEN}([0-9a-f]+){_MARK_CLOSE}")
_MARKERS_IN_FILE = re.compile(rb"\xee\x80[\x80\x81]|&#(x0*e00[01]|5734[45]);", re.IGNORECASE)
_UNSAFE_XML = re.compile(rb"<!\s*(doctype|entity)", re.IGNORECASE)


class UnsafeXml(ValueError):
    pass


def _xml10_char(cp: int) -> bool:
    return cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


def mark_invalid_refs(raw: bytes) -> bytes:
    def mark(m: re.Match[bytes]) -> bytes:
        cp = int(m.group(1), 16) if m.group(1) else int(m.group(2))
        return m.group(0) if _xml10_char(cp) else f"&#xE000;{cp:x}&#xE001;".encode()

    marked = _CHAR_REF.sub(mark, raw)
    if marked != raw and _MARKERS_IN_FILE.search(raw):
        raise UnsafeXml("The file uses the private-use characters U+E000/U+E001, which this tool needs "
                        "internally; it cannot be read.")
    return marked


def parse(raw: bytes) -> ET.Element:
    """Parse untrusted XML: no DOCTYPE or entities; JMeter's control-character references allowed."""
    if _UNSAFE_XML.search(raw):
        raise UnsafeXml("The file contains a DOCTYPE or entity declaration, which is not allowed.")
    return ET.fromstring(mark_invalid_refs(raw))


def restore(xml: str) -> str:
    return _MARKED.sub(lambda m: f"&#x{m.group(1)};", xml)


def visible(text: str) -> str:
    """Show a marked control character as \\x10 (for labels, URLs and bodies on screen)."""
    return _MARKED.sub(lambda m: f"\\x{int(m.group(1), 16):02x}", text)
