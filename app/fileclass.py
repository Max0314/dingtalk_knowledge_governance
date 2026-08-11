"""File classification: which assets deserve an AI review at all.

The 2026-08-05 baseline showed 12.2% of stored nodes are engineering
residue (logs, lockfiles, compiled artifacts, captures) and another ~10%
are images/media/archives no text review can score. Classifying up front
keeps the review queue meaningful and the model bill bounded. The class
is stored on the document row so dashboards can slice by it.
"""
from __future__ import annotations

CLASS_BY_EXTENSION = {
    "native_doc": ("adoc",),
    "document": ("docx", "doc", "wps", "pdf", "rtf", "odt"),
    "sheet": ("xlsx", "xls", "csv", "et", "axls", "able", "atable", "asheet"),
    "slide": ("pptx", "ppt", "odp"),
    "text": ("txt", "md", "markdown", "html", "htm"),
    "image": ("png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "tif", "tiff", "ico", "heic", "psd", "ai", "eps"),
    "media": ("mp4", "mp3", "wav", "avi", "mov", "mkv", "flac", "m4a", "wmv", "amr"),
    "archive": ("zip", "rar", "7z", "gz", "tar", "bz2", "xz", "iso", "jar", "war"),
    "engineering": ("log", "lck", "class", "dll", "so", "bin", "exe", "elf", "o", "obj",
                    "pcap", "pcapng", "stp", "eox", "eod", "asc", "dbc", "hex", "map",
                    "c", "h", "cpp", "hpp", "java", "py", "js", "ts", "css", "xml", "json",
                    "yml", "yaml", "sh", "bat", "sql", "mk", "cfg", "ini", "conf", "patch", "diff"),
}

_LOOKUP = {ext: cls for cls, exts in CLASS_BY_EXTENSION.items() for ext in exts}

DEFAULT_REVIEW_CLASSES = "native_doc,document,sheet,slide,text"


def classify(extension: str, is_folder: bool = False) -> str:
    if is_folder:
        return "folder"
    return _LOOKUP.get((extension or "").strip().lower(), "other")


def review_classes(csv_value: str) -> set[str]:
    value = csv_value or DEFAULT_REVIEW_CLASSES
    return {token.strip() for token in value.split(",") if token.strip()}
