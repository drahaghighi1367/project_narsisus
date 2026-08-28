# E:/1405_pdf_editor/core/utils.py

import re
import pymupdf


def sanitize_filename(name: str) -> str:
    """Remove illegal characters for file systems."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return clean if clean else "Untitled"


def parse_page_range(range_str: str, total_pages: int) -> list[int]:
    """Parses range strings such as '1-5, 8, 11-end' into 1-based page numbers."""
    pages = set()
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            s, e = part.split("-", 1)
            s_idx = int(s.strip())
            e_idx = total_pages if e.strip().lower() == "end" else int(e.strip())
            for p in range(max(1, s_idx), min(total_pages, e_idx) + 1):
                pages.add(p)
        elif part.isdigit():
            p = int(part)
            if 1 <= p <= total_pages:
                pages.add(p)
    return sorted(pages)


def parse_interval_ranges(intervals_str: str, total_pages: int) -> list[tuple[int, int]]:
    """
    Parses comma-separated intervals/ranges like '[1, 2-4, 4-10, 12, 22-40]'
    allowing overlapping, single-page, and multi-page intervals.
    """
    cleaned = intervals_str.replace("[", "").replace("]", "").strip()
    if not cleaned:
        return []

    results = []
    for part in re.split(r'[,;]+', cleaned):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            tokens = part.split("-", 1)
            s_str = tokens[0].strip()
            e_str = tokens[1].strip().lower()

            if not s_str.isdigit():
                continue
            s_val = int(s_str)
            e_val = total_pages if e_str in ("end", "last", "max") else (int(e_str) if e_str.isdigit() else s_val)

            s_val = max(1, min(total_pages, s_val))
            e_val = max(s_val, min(total_pages, e_val))
            results.append((s_val, e_val))
        elif part.isdigit():
            val = max(1, min(total_pages, int(part)))
            results.append((val, val))

    return results


def generate_fixed_chunks(chunk_size: int, total_pages: int, overlap: int = 0) -> list[tuple[int, int]]:
    """
    Generates fixed-size page intervals (e.g. 10 pages per part) with optional overlap.
    The last part contains remaining pages.
    """
    if chunk_size <= 0 or total_pages <= 0:
        return []

    chunks = []
    start = 1
    step = max(1, chunk_size - overlap)

    while start <= total_pages:
        end = min(total_pages, start + chunk_size - 1)
        chunks.append((start, end))
        if end >= total_pages:
            break
        start += step

    return chunks


def get_best_fontname(font_name: str, flags: int) -> str:
    """Maps extracted PDF font characteristics to standard PyMuPDF base-14 fonts."""
    fn = (font_name or "").lower()
    is_bold = bool(flags & 2 ** 4) or ("bold" in fn) or ("black" in fn) or ("heavy" in fn)
    is_italic = bool(flags & 2 ** 1) or ("italic" in fn) or ("oblique" in fn)

    if "times" in fn or "serif" in fn or "roman" in fn:
        if is_bold and is_italic:
            return "times-bolditalic"
        if is_bold:
            return "times-bold"
        if is_italic:
            return "times-italic"
        return "times-roman"
    elif "courier" in fn or "mono" in fn or "code" in fn:
        if is_bold and is_italic:
            return "courier-boldoblique"
        if is_bold:
            return "courier-bold"
        if is_italic:
            return "courier-oblique"
        return "courier"
    else:
        if is_bold and is_italic:
            return "hebi"
        if is_bold:
            return "hebo"
        if is_italic:
            return "heit"
        return "helv"


def merge_connected_rects(rect_list: list[pymupdf.Rect], eps: float = 0.5) -> list[pymupdf.Rect]:
    """
    Groups touching or overlapping rectangles into connected components
    and returns the unified bounding rectangle for each component.
    """
    if not rect_list:
        return []
    if len(rect_list) == 1:
        return [rect_list[0]]

    n = len(rect_list)
    adj = {i: [] for i in range(n)}

    for i in range(n):
        r1 = rect_list[i]
        for j in range(i + 1, n):
            r2 = rect_list[j]
            ox = min(r1.x1, r2.x1) - max(r1.x0, r2.x0)
            oy = min(r1.y1, r2.y1) - max(r1.y0, r2.y0)
            if ox >= -eps and oy >= -eps:
                adj[i].append(j)
                adj[j].append(i)

    visited = set()
    merged = []

    for i in range(n):
        if i not in visited:
            component = []
            queue = [i]
            visited.add(i)
            while queue:
                curr = queue.pop(0)
                component.append(rect_list[curr])
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            x0 = min(r.x0 for r in component)
            y0 = min(r.y0 for r in component)
            x1 = max(r.x1 for r in component)
            y1 = max(r.y1 for r in component)
            merged.append(pymupdf.Rect(x0, y0, x1, y1))

    return merged