
"""
Universal Batch PDF to .pdfedit Processor
Designed for Google Colab, GitHub Actions, and Local High-Performance Pipelines.

Converts batches of raw PDFs into unified, self-contained, and GUI-compatible
`.pdfedit` workspace bundles containing:
  - document.pdf
  - project.json (with deterministic two-way bundle linking)
  - docling_stream.json (frozen OCR/digital reading stream)
  - images_expanded.pdf (human verification slices with red bboxes)
  - tables_expanded.pdf (human verification slices with red bboxes)
"""

import os
import re
import sys
import gc
import json
import glob
import time
import hashlib
import argparse
import subprocess
from pathlib import Path

# Add project root to path for both standalone and repo execution
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymupdf
from core.project_manager import ProjectManager


# ============================================================
# GIT REPO SYNC HELPER (For Colab & CI/CD)
# ============================================================

def git_commit_and_push(file_paths: list[str], commit_message: str) -> bool:
    """Safely stages, commits, and pushes files to the current branch with rebase retry."""
    try:
        # Check if inside a git work tree
        res = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
        if res.returncode != 0:
            return False

        # Stage specific files
        for p in file_paths:
            if os.path.exists(p):
                subprocess.run(["git", "add", p], check=True, capture_output=True)

        # Check if there are changes staged
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if diff.returncode == 0:
            return True  # Nothing to commit

        # Commit
        subprocess.run(["git", "commit", "-m", commit_message], check=True, capture_output=True)

        # Pull rebase and push (handles concurrent pushes safely)
        subprocess.run(["git", "pull", "--rebase"], capture_output=True)
        push_res = subprocess.run(["git", "push"], capture_output=True, text=True)
        return push_res.returncode == 0
    except Exception as e:
        print(f"[!] Git sync warning: {e}", file=sys.stderr)
        return False


# ============================================================
# EXTRACTION & RECONCILIATION CONFIGURATION
# ============================================================

CONFIG = {
    "iou_overlap_threshold": 0.35,
    "enclosure_threshold": 0.75,
    "figure_keywords": ["FIG", "FIGURE", "EXHIBIT", "PHOTO", "IMAGE", "PLATE", "CHART", "DIAGRAM"],
    "table_keywords": ["TABLE", "TAB", "EXHIBIT", "SCHEDULE"],
    "footnote_keywords": [
        "NOTE", "NOTES", "ABBREVIATION", "ABBREVIATIONS", "ABBR",
        "SOURCE", "SOURCES", "KEY", "DATA FROM", "ADAPTED FROM",
        r"\*", r"†", r"‡", r"§"
    ],
    "max_vertical_gap": 65.0,
    "max_horizontal_drift": 35.0,
    "max_caption_continuation_gap": 15.0,
    "font_profiles": {
        "caption_labels": [],
        "caption_bodies": [],
        "regular_body": [],
        "footnote_fonts": [],
        "heading_fonts": []
    },
    "weights": {
        "explicit_title_match": 1000,
        "footnote_keyword_match": 800,
        "docling_caption_or_footnote_tag": 300,
        "caption_label_font_bonus": 120,
        "caption_body_font_bonus": 90,
        "footnote_font_bonus": 90,
        "immediate_proximity (<20pt)": 150,
        "near_proximity (<45pt)": 70,
        "horizontal_aligned": 40,
        "small_font_bonus (<=9.5pt)": 50,
        "italic_bonus": 30,
        "body_text_penalty": -150,
        "regular_body_font_penalty": -350,
        "heading_font_penalty": -500
    },
    "title_threshold": 260,
    "footnote_threshold": 220
}

FIGURE_REGEX = re.compile(rf"^\s*(?:{'|'.join(CONFIG['figure_keywords'])})\.?\s+[A-Z]?\d+(?:[-.:]\d+)*", re.IGNORECASE)
TABLE_TITLE_REGEX = re.compile(rf"^\s*(?:{'|'.join(CONFIG['table_keywords'])})\.?\s+[A-Z]?\d+(?:[-.:]\d+)*", re.IGNORECASE)
FOOTNOTE_REGEX = re.compile(rf"^\s*(?:{'|'.join(CONFIG['footnote_keywords'])})\b", re.IGNORECASE)


def clean_font_name(font_name: str) -> str:
    if not font_name:
        return ""
    if "+" in font_name:
        font_name = font_name.split("+", 1)[1]
    return font_name.strip().lower()


def font_matches_set(font_name: str, target_fonts: list) -> bool:
    if not font_name or not target_fonts:
        return False
    cleaned = clean_font_name(font_name)
    return any(clean_font_name(t) in cleaned or cleaned in clean_font_name(t) for t in target_fonts)


def to_topleft(bbox, p_height: float) -> tuple[float, float, float, float]:
    return (float(bbox.l), p_height - float(bbox.t), float(bbox.r), p_height - float(bbox.b))


def bbox_iou(b1, b2) -> tuple[float, float]:
    x1, y1 = max(b1[0], b2[0]), max(b1[1], b2[1])
    x2, y2 = min(b1[2], b2[2]), min(b1[3], b2[3])

    if x2 <= x1 or y2 <= y1:
        return 0.0, 0.0

    inter_area = (x2 - x1) * (y2 - y1)
    area1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    area2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    union_area = area1 + area2 - inter_area

    iou = inter_area / union_area if union_area > 0 else 0.0
    min_area = min(area1, area2)
    enclosure = inter_area / min_area if min_area > 0 else 0.0
    return iou, enclosure


def merge_candidates(candidates: list[dict], iou_thresh: float = 0.35, enc_thresh: float = 0.75) -> list[dict]:
    merged = []
    sorted_cands = sorted(
        candidates,
        key=lambda c: (c["bbox"][2] - c["bbox"][0]) * (c["bbox"][3] - c["bbox"][1]),
        reverse=True
    )

    for cand in sorted_cands:
        matched = False
        for m in merged:
            if m["page"] != cand["page"]:
                continue
            iou, enc = bbox_iou(m["bbox"], cand["bbox"])
            if iou >= iou_thresh or enc >= enc_thresh:
                m["bbox"] = (
                    min(m["bbox"][0], cand["bbox"][0]),
                    min(m["bbox"][1], cand["bbox"][1]),
                    max(m["bbox"][2], cand["bbox"][2]),
                    max(m["bbox"][3], cand["bbox"][3])
                )
                if cand["source"] not in m["sources"]:
                    m["sources"].append(cand["source"])
                matched = True
                break

        if not matched:
            merged.append({
                "page": cand["page"],
                "bbox": cand["bbox"],
                "sources": [cand["source"]]
            })
    return merged


def analyze_document_font_profile(page_spans_dict: dict, config: dict) -> dict:
    font_stats = {}
    total_chars = 0

    for spans in page_spans_dict.values():
        for s in spans:
            f_raw = clean_font_name(s.get("font", "unknown"))
            size = round(float(s.get("size", 0)), 1)
            txt = s.get("text", "")
            chars = len(txt)
            if not txt.strip() or chars == 0:
                continue

            total_chars += chars
            if f_raw not in font_stats:
                font_stats[f_raw] = {"chars": 0, "sizes": {}}
            font_stats[f_raw]["chars"] += chars
            font_stats[f_raw]["sizes"][size] = font_stats[f_raw]["sizes"].get(size, 0) + chars

    sorted_fonts = sorted(font_stats.items(), key=lambda x: x[1]["chars"], reverse=True)
    dominant_body_font = sorted_fonts[0][0] if sorted_fonts else ""
    body_size = 10.0
    if dominant_body_font and font_stats[dominant_body_font]["sizes"]:
        body_size = max(font_stats[dominant_body_font]["sizes"], key=font_stats[dominant_body_font]["sizes"].get)

    auto_caption_labels = set()
    auto_caption_bodies = set()
    auto_footnotes = set()
    auto_headings = set()

    for spans in page_spans_dict.values():
        for idx, s in enumerate(spans):
            txt = s.get("text", "").strip()
            f_raw = clean_font_name(s.get("font", ""))
            sz = round(float(s.get("size", 0)), 1)

            if FIGURE_REGEX.search(txt) or TABLE_TITLE_REGEX.search(txt):
                auto_caption_labels.add(f_raw)
                for neighbor in spans[max(0, idx - 1): min(len(spans), idx + 3)]:
                    n_font = clean_font_name(neighbor.get("font", ""))
                    if n_font != dominant_body_font:
                        auto_caption_bodies.add(n_font)

            if FOOTNOTE_REGEX.search(txt) or (sz <= body_size - 1.5 and sz > 0):
                auto_footnotes.add(f_raw)

            if sz >= body_size + 1.5:
                auto_headings.add(f_raw)

    profiles = config["font_profiles"]
    return {
        "dominant_body_font": dominant_body_font,
        "dominant_body_size": body_size,
        "caption_labels": profiles["caption_labels"] or list(auto_caption_labels),
        "caption_bodies": profiles["caption_bodies"] or list(auto_caption_bodies),
        "regular_body": profiles["regular_body"] or [dominant_body_font],
        "footnote_fonts": profiles["footnote_fonts"] or list(auto_footnotes),
        "heading_fonts": profiles["heading_fonts"] or list(auto_headings)
    }


def collect_caption_continuations(start_block: dict, page_blocks: list, page_height: float, font_profile: dict) -> dict:
    cap_bbox = list(start_block["bbox"])
    collected_texts = [start_block["text"]]

    candidates_below = [b for b in page_blocks if b["bbox"][1] >= start_block["bbox"][1] and b != start_block]
    candidates_below.sort(key=lambda b: b["bbox"][1])

    current_y2 = cap_bbox[3]
    cur_x1, cur_x2 = cap_bbox[0], cap_bbox[2]
    body_font = font_profile["dominant_body_font"]
    body_size = font_profile["dominant_body_size"]

    for b in candidates_below:
        bx1, by1, bx2, by2 = b["bbox"]
        b_font = clean_font_name(b.get("font", ""))
        b_size = b.get("size", body_size)
        label = b.get("label", "").upper()

        gap = by1 - current_y2
        if gap < -3:
            continue
        if gap > CONFIG["max_caption_continuation_gap"]:
            break

        h_overlap = max(0, min(cur_x2, bx2) - max(cur_x1, bx1))
        if h_overlap <= 0 and abs(bx1 - cur_x1) > CONFIG["max_horizontal_drift"]:
            continue

        if FIGURE_REGEX.search(b["text"][:60]) or TABLE_TITLE_REGEX.search(b["text"][:60]):
            break

        if font_matches_set(b_font, font_profile["heading_fonts"]) and b_size >= body_size + 1.0:
            break

        is_regular_body = font_matches_set(b_font, font_profile["regular_body"]) and (b_size >= body_size - 0.5)
        if is_regular_body and "CAPTION" not in label and not b.get("is_italic"):
            break

        is_caption_style = (
            font_matches_set(b_font, font_profile["caption_bodies"]) or
            font_matches_set(b_font, font_profile["caption_labels"]) or
            font_matches_set(b_font, font_profile["footnote_fonts"]) or
            (b_size <= body_size - 1.0) or
            b.get("is_italic") or
            ("CAPTION" in label)
        )

        if not is_caption_style and is_regular_body:
            break

        collected_texts.append(b["text"])
        cap_bbox[0] = min(cap_bbox[0], bx1)
        cap_bbox[1] = min(cap_bbox[1], by1)
        cap_bbox[2] = max(cap_bbox[2], bx2)
        cap_bbox[3] = max(cap_bbox[3], by2)
        current_y2 = by2

    return {
        "text": " ".join(t.strip() for t in collected_texts if t.strip()),
        "bbox": {
            "x1": round(cap_bbox[0], 2),
            "y1": round(cap_bbox[1], 2),
            "x2": round(cap_bbox[2], 2),
            "y2": round(cap_bbox[3], 2)
        },
        "font": start_block.get("font", ""),
        "size": round(start_block.get("size", 0.0), 2)
    }


def find_figure_caption(page_number: int, image_bbox: tuple, page_docling_blocks: dict, page_heights: dict, font_profile: dict) -> dict | None:
    blocks = page_docling_blocks.get(page_number, [])
    ix1, iy1, ix2, iy2 = image_bbox
    candidates = []
    w = CONFIG["weights"]
    body_size = font_profile["dominant_body_size"]

    for block in blocks:
        text = block["text"]
        tx1, ty1, tx2, ty2 = block["bbox"]
        b_font = clean_font_name(block.get("font", ""))
        b_size = block.get("size", body_size)
        label = block.get("label", "").upper()

        if tx1 >= ix1 and tx2 <= ix2 and ty1 >= iy1 and ty2 <= iy2:
            continue

        is_below = ty1 >= (iy2 - 5)
        gap = (ty1 - iy2) if is_below else (iy1 - ty2)
        if gap < 0 or gap > CONFIG["max_vertical_gap"]:
            continue

        h_overlap = max(0, min(ix2, tx2) - max(ix1, tx1))
        if h_overlap <= 0 and abs(tx1 - ix1) > CONFIG["max_horizontal_drift"]:
            continue

        score = 0
        is_fig = bool(FIGURE_REGEX.search(text[:80].strip()))
        has_docling_tag = "CAPTION" in label

        if is_fig: score += w["explicit_title_match"]
        if has_docling_tag: score += w["docling_caption_or_footnote_tag"]

        if font_matches_set(b_font, font_profile["caption_labels"]): score += w["caption_label_font_bonus"]
        elif font_matches_set(b_font, font_profile["caption_bodies"]): score += w["caption_body_font_bonus"]
        elif font_matches_set(b_font, font_profile["footnote_fonts"]): score += w["footnote_font_bonus"]

        if font_matches_set(b_font, font_profile["heading_fonts"]) and b_size >= body_size + 1.0:
            score += w["heading_font_penalty"]

        if font_matches_set(b_font, font_profile["regular_body"]) and (b_size >= body_size - 0.5):
            if not is_fig and not has_docling_tag: score += w["regular_body_font_penalty"]

        if is_below:
            score += w["immediate_proximity (<20pt)"] if gap <= 20 else w["near_proximity (<45pt)"]
        else:
            score += 40

        if h_overlap > 0: score += w["horizontal_aligned"]
        if b_size > 0 and b_size <= 9.5: score += w["small_font_bonus (<=9.5pt)"]
        if block.get("is_italic"): score += w["italic_bonus"]
        if not is_fig and not has_docling_tag: score += w["body_text_penalty"]

        candidates.append({"block": block, "score": score, "is_fig": is_fig})

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    if best["is_fig"] or best["score"] >= CONFIG["title_threshold"]:
        return collect_caption_continuations(best["block"], blocks, page_heights[page_number], font_profile)
    return None


def find_table_annotations(page_number: int, table_bbox: tuple, page_docling_blocks: dict, page_heights: dict, font_profile: dict) -> tuple[dict | None, dict | None]:
    blocks = page_docling_blocks.get(page_number, [])
    tx1, ty1, tx2, ty2 = table_bbox
    w = CONFIG["weights"]
    body_size = font_profile["dominant_body_size"]

    top_candidates, bottom_candidates = [], []

    for block in blocks:
        text = block["text"]
        bx1, by1, bx2, by2 = block["bbox"]
        label = block.get("label", "").upper()
        b_font = clean_font_name(block.get("font", ""))
        b_size = block.get("size", body_size)

        if bx1 >= tx1 and bx2 <= tx2 and by1 >= ty1 and by2 <= ty2:
            continue

        h_overlap = max(0, min(tx2, bx2) - max(tx1, bx1))
        if h_overlap <= 0 and abs(bx1 - tx1) > CONFIG["max_horizontal_drift"]:
            continue

        # Top Table Title
        if by2 <= ty1 + 10:
            gap = ty1 - by2
            if -10 <= gap <= CONFIG["max_vertical_gap"]:
                score = 0
                has_title_prefix = bool(TABLE_TITLE_REGEX.search(text[:80].strip()))
                if has_title_prefix: score += w["explicit_title_match"]
                if "CAPTION" in label: score += w["docling_caption_or_footnote_tag"]

                if font_matches_set(b_font, font_profile["caption_labels"]): score += w["caption_label_font_bonus"]
                elif font_matches_set(b_font, font_profile["caption_bodies"]): score += w["caption_body_font_bonus"]

                if font_matches_set(b_font, font_profile["heading_fonts"]) and b_size >= body_size + 1.0:
                    score += w["heading_font_penalty"]

                if font_matches_set(b_font, font_profile["regular_body"]) and (b_size >= body_size - 0.5):
                    if not has_title_prefix and "CAPTION" not in label: score += w["regular_body_font_penalty"]

                score += w["immediate_proximity (<20pt)"] if gap <= 20 else w["near_proximity (<45pt)"]
                if h_overlap > 0: score += w["horizontal_aligned"]
                if not has_title_prefix and "CAPTION" not in label: score += w["body_text_penalty"]
                top_candidates.append({"block": block, "score": score, "has_prefix": has_title_prefix})

        # Bottom Footnotes / Legends
        elif by1 >= ty2 - 10:
            gap = by1 - ty2
            if -10 <= gap <= CONFIG["max_vertical_gap"]:
                score = 0
                has_footnote_kw = bool(FOOTNOTE_REGEX.search(text[:80].strip()))
                is_footnote_label = "FOOTNOTE" in label or "CAPTION" in label

                if has_footnote_kw: score += w["footnote_keyword_match"]
                if is_footnote_label: score += w["docling_caption_or_footnote_tag"]

                if font_matches_set(b_font, font_profile["footnote_fonts"]): score += w["footnote_font_bonus"]
                if font_matches_set(b_font, font_profile["regular_body"]) and (b_size >= body_size - 0.5):
                    if not has_footnote_kw and not is_footnote_label: score += w["regular_body_font_penalty"]

                score += w["immediate_proximity (<20pt)"] if gap <= 20 else w["near_proximity (<45pt)"]
                if h_overlap > 0: score += w["horizontal_aligned"]
                if b_size > 0 and b_size <= 9.5: score += w["small_font_bonus (<=9.5pt)"]
                if block.get("is_italic"): score += w["italic_bonus"]
                if not has_footnote_kw and not is_footnote_label: score += w["body_text_penalty"]
                bottom_candidates.append({"block": block, "score": score, "has_kw": has_footnote_kw})

    top_caption = None
    if top_candidates:
        top_candidates.sort(key=lambda x: x["score"], reverse=True)
        if top_candidates[0]["has_prefix"] or top_candidates[0]["score"] >= CONFIG["title_threshold"]:
            top_caption = collect_caption_continuations(top_candidates[0]["block"], blocks, page_heights[page_number], font_profile)

    bottom_notes = None
    if bottom_candidates:
        bottom_candidates.sort(key=lambda x: x["score"], reverse=True)
        if bottom_candidates[0]["has_kw"] or bottom_candidates[0]["score"] >= CONFIG["footnote_threshold"]:
            bottom_notes = collect_caption_continuations(bottom_candidates[0]["block"], blocks, page_heights[page_number], font_profile)

    return top_caption, bottom_notes


# ============================================================
# PROGRESS TRACKING & INTEGRITY CHECKING
# ============================================================

PROGRESS_MANIFEST_NAME = "progress_manifest.json"


def load_progress_tracker(output_dir: str) -> dict:
    manifest_path = os.path.join(output_dir, PROGRESS_MANIFEST_NAME)
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "version": "2.0",
        "last_updated": "",
        "completed_count": 0,
        "items": {}
    }


def save_progress_tracker(output_dir: str, tracker_data: dict):
    manifest_path = os.path.join(output_dir, PROGRESS_MANIFEST_NAME)
    tracker_data["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tracker_data["completed_count"] = len([k for k, v in tracker_data.get("items", {}).items() if v.get("status") == "completed"])
    temp_path = manifest_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(tracker_data, f, indent=2, ensure_ascii=False)
    try:
        os.replace(temp_path, manifest_path)
    except Exception:
        if os.path.exists(manifest_path):
            os.remove(manifest_path)
        os.rename(temp_path, manifest_path)


def is_bundle_complete_and_valid(bundle_path: str, password: str | None = None) -> bool:
    """Verifies that an existing .pdfedit bundle is intact, uncorrupted, and decryptable."""
    if not os.path.exists(bundle_path) or os.path.getsize(bundle_path) < 100:
        return False
    try:
        pdf_path, state, _ = ProjectManager.load_project(bundle_path, password=password)
        if os.path.exists(pdf_path) and state.get("elements") is not None:
            temp_dir = os.path.dirname(pdf_path)
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            return True
    except Exception as e:
        print(f"[!] Validation check failed for {os.path.basename(bundle_path)}: {e}")
        return False
    return False


# ============================================================
# SINGLE PDF PROCESSOR TO .PDFEDIT
# ============================================================

def process_single_pdf_to_pdfedit(
    source_pdf_path: str,
    output_dir: str,
    converter=None,
    password: str | None = None,
    resume: bool = True
) -> tuple[str, bool]:
    """
    Converts one PDF into a self-contained .pdfedit package with atomic write safety.
    Returns: (output_bundle_path, was_skipped)
    """
    start_time = time.time()
    pdf_path = os.path.abspath(source_pdf_path)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    out_bundle_name = f"{base_name}.pdfedit"
    out_bundle_path = os.path.join(output_dir, out_bundle_name)
    tmp_bundle_path = os.path.join(output_dir, f"{base_name}.tmp.pdfedit")

    # 1. Resumability Check: Verify if already processed and uncorrupted
    if resume and is_bundle_complete_and_valid(out_bundle_path, password=password):
        print(f"⏩ [Skip/Resume] Valid bundle already exists: {out_bundle_name}")
        return out_bundle_path, True

    print("\n" + "=" * 75)
    print(f"📄 Processing: {os.path.basename(pdf_path)}")
    print(f"📦 Target Package: {out_bundle_name}")
    print("=" * 75)

    pdf = pymupdf.open(pdf_path)
    total_pages = len(pdf)
    page_heights = {i + 1: page.rect.height for i, page in enumerate(pdf)}
    page_widths = {i + 1: page.rect.width for i, page in enumerate(pdf)}

    # 1. PyMuPDF Native Spans & Vector Objects
    page_spans = {}
    pymupdf_tables = []
    pymupdf_images = []

    for page_num, page in enumerate(pdf, start=1):
        spans = []
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") == 0:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        spans.append({
                            "text": span.get("text", ""),
                            "bbox": span["bbox"],
                            "font": span.get("font", ""),
                            "size": float(span.get("size", 0)),
                            "flags": span.get("flags", 0)
                        })
        page_spans[page_num] = spans

        try:
            for tab in page.find_tables():
                x1, y1, x2, y2 = tab.bbox
                if (x2 - x1) > 50 and (y2 - y1) > 40:
                    pymupdf_tables.append({
                        "page": page_num,
                        "bbox": (float(x1), float(y1), float(x2), float(y2)),
                        "source": "pymupdf_native_table"
                    })
        except Exception:
            pass

        for img_info in page.get_image_info(xrefs=True):
            bx = img_info.get("bbox")
            if bx and (bx[2] - bx[0]) > 25 and (bx[3] - bx[1]) > 25:
                pymupdf_images.append({
                    "page": page_num,
                    "bbox": (float(bx[0]), float(bx[1]), float(bx[2]), float(bx[3])),
                    "source": "pymupdf_native_image"
                })

    # 2. Font Profiler
    font_profile = analyze_document_font_profile(page_spans, CONFIG)
    print(f"[*] Primary Body Font: '{font_profile['dominant_body_font']}' ({font_profile['dominant_body_size']}pt)")

    # 3. Single-Pass Docling Conversion
    print("[*] Running Docling OCR & structure analysis...")
    conversion_result = converter.convert(pdf_path)
    docling_doc = conversion_result.document

    docling_pictures = []
    docling_tables = []
    page_docling_blocks = {p: [] for p in range(1, total_pages + 1)}
    docling_text_stream = []

    for picture in docling_doc.pictures:
        if picture.prov:
            p_num = picture.prov[0].page_no
            docling_pictures.append({
                "page": p_num,
                "bbox": to_topleft(picture.prov[0].bbox, page_heights[p_num]),
                "source": "docling_picture"
            })

    for table in docling_doc.tables:
        if table.prov:
            p_num = table.prov[0].page_no
            docling_tables.append({
                "page": p_num,
                "bbox": to_topleft(table.prov[0].bbox, page_heights[p_num]),
                "source": "docling_table"
            })

    for item, _ in docling_doc.iterate_items():
        if hasattr(item, "prov") and item.prov and hasattr(item, "text"):
            txt = item.text.strip()
            if not txt:
                continue
            p_num = item.prov[0].page_no
            p_height = page_heights[p_num]
            tb = to_topleft(item.prov[0].bbox, p_height)
            label = str(getattr(item, "label", "paragraph")).lower()
            level = getattr(item, "level", None)

            spans_on_page = page_spans.get(p_num, [])
            matching_fonts = [s["font"] for s in spans_on_page if not (s["bbox"][2] < tb[0] or s["bbox"][0] > tb[2] or s["bbox"][3] < tb[1] or s["bbox"][1] > tb[3])]
            matching_sizes = [s["size"] for s in spans_on_page if not (s["bbox"][2] < tb[0] or s["bbox"][0] > tb[2] or s["bbox"][3] < tb[1] or s["bbox"][1] > tb[3]) if s["size"] > 0]

            dom_font = max(set(matching_fonts), key=matching_fonts.count) if matching_fonts else ""
            avg_size = (sum(matching_sizes) / len(matching_sizes)) if matching_sizes else 0
            font_lower = dom_font.lower()

            page_docling_blocks[p_num].append({
                "text": txt,
                "label": label,
                "bbox": tb,
                "font": dom_font,
                "size": avg_size,
                "is_bold": any(w in font_lower for w in ["bold", "black", "heavy"]),
                "is_italic": any(w in font_lower for w in ["italic", "oblique"]),
                "item_ref": item
            })

            stream_node = {
                "page": p_num,
                "label": label,
                "text": txt,
                "bbox": {
                    "x1": round(tb[0], 2),
                    "y1": round(tb[1], 2),
                    "x2": round(tb[2], 2),
                    "y2": round(tb[3], 2)
                }
            }
            if level is not None:
                stream_node["level"] = level
            docling_text_stream.append(stream_node)

    # 4. Spatial Reconciliation & Vector Background Recovery
    recovered_tables = []
    for page_num, page in enumerate(pdf, start=1):
        blocks = page_docling_blocks.get(page_num, [])
        p_height = page_heights[page_num]
        spans_on_page = page_spans.get(page_num, [])

        for b in blocks:
            text_head = b["text"][:80].strip()
            if TABLE_TITLE_REGEX.search(text_head):
                tx1, ty1, tx2, ty2 = b["bbox"]
                all_known = docling_tables + pymupdf_tables
                already_covered = any(
                    k["page"] == page_num and (
                        (k["bbox"][1] - 15 <= ty2 <= k["bbox"][3]) or
                        (abs(k["bbox"][1] - ty2) < 45 and abs(k["bbox"][0] - tx1) < 50)
                    )
                    for k in all_known
                )

                if not already_covered:
                    spans_below = [s for s in spans_on_page if s["bbox"][1] >= ty2 - 5 and s["bbox"][0] >= tx1 - 25]
                    cand_spans = [s for s in spans_below if s["bbox"][0] < tx1 + 260 and s["bbox"][1] <= p_height * 0.95]
                    if cand_spans:
                        col_x1 = max(0, tx1 - 10)
                        col_x2 = max(s["bbox"][2] for s in cand_spans) + 8.0
                        col_y1 = ty2 + 2.0
                        col_y2 = max(s["bbox"][3] for s in cand_spans) + 5.0
                        recovered_tables.append({
                            "page": page_num,
                            "bbox": (col_x1, col_y1, col_x2, col_y2),
                            "source": "column_clamped_recovery"
                        })

    final_tables = merge_candidates(docling_tables + pymupdf_tables + recovered_tables, CONFIG["iou_overlap_threshold"], CONFIG["enclosure_threshold"])
    final_images = merge_candidates(docling_pictures + pymupdf_images, CONFIG["iou_overlap_threshold"], CONFIG["enclosure_threshold"])

    # 5. Build Final Elements with Captions & Footnotes
    elements = []
    page_rects_map = {p_idx: [] for p_idx in range(total_pages)}
    page_metas_map = {p_idx: [] for p_idx in range(total_pages)}

    def parse_rect(b, pw, ph):
        return pymupdf.Rect(max(0.0, min(float(b["x1"]), pw)), max(0.0, min(float(b["y1"]), ph)),
                            max(0.0, min(float(b["x2"]), pw)), max(0.0, min(float(b["y2"]), ph)))

    for idx, img in enumerate(final_images, start=1):
        p_num = img["page"]
        pw, ph = page_widths[p_num], page_heights[p_num]
        cap = find_figure_caption(p_num, img["bbox"], page_docling_blocks, page_heights, font_profile)

        raw_box = {"x1": round(img["bbox"][0], 2), "y1": round(img["bbox"][1], 2), "x2": round(img["bbox"][2], 2), "y2": round(img["bbox"][3], 2)}
        r_list = [parse_rect(raw_box, pw, ph)]
        if cap and isinstance(cap.get("bbox"), dict):
            r_list.append(parse_rect(cap["bbox"], pw, ph))

        u_rect = r_list[0]
        for r in r_list[1:]:
            u_rect = pymupdf.Rect(min(u_rect.x0, r.x0), min(u_rect.y0, r.y0), max(u_rect.x1, r.x1), max(u_rect.y1, r.y1))

        comb_box = {"x1": round(u_rect.x0, 2), "y1": round(u_rect.y0, 2), "x2": round(u_rect.x1, 2), "y2": round(u_rect.y1, 2)}
        elem_id = f"img_{idx}"
        elements.append({
            "id": elem_id,
            "type": "image",
            "page": p_num,
            "combined_bbox": comb_box,
            "raw_bbox": raw_box,
            "caption": cap,
            "detection_sources": img["sources"]
        })
        if 0 <= p_num - 1 < total_pages:
            page_rects_map[p_num - 1].append([comb_box["x1"], comb_box["y1"], comb_box["x2"], comb_box["y2"]])
            page_metas_map[p_num - 1].append({"id": elem_id, "type": "image"})

    for idx, tab in enumerate(final_tables, start=1):
        p_num = tab["page"]
        pw, ph = page_widths[p_num], page_heights[p_num]
        top_cap, bottom_notes = find_table_annotations(p_num, tab["bbox"], page_docling_blocks, page_heights, font_profile)

        raw_box = {"x1": round(tab["bbox"][0], 2), "y1": round(tab["bbox"][1], 2), "x2": round(tab["bbox"][2], 2), "y2": round(tab["bbox"][3], 2)}
        r_list = [parse_rect(raw_box, pw, ph)]
        if top_cap and isinstance(top_cap.get("bbox"), dict):
            r_list.append(parse_rect(top_cap["bbox"], pw, ph))
        if bottom_notes and isinstance(bottom_notes.get("bbox"), dict):
            r_list.append(parse_rect(bottom_notes["bbox"], pw, ph))

        u_rect = r_list[0]
        for r in r_list[1:]:
            u_rect = pymupdf.Rect(min(u_rect.x0, r.x0), min(u_rect.y0, r.y0), max(u_rect.x1, r.x1), max(u_rect.y1, r.y1))

        comb_box = {"x1": round(u_rect.x0, 2), "y1": round(u_rect.y0, 2), "x2": round(u_rect.x1, 2), "y2": round(u_rect.y1, 2)}
        elem_id = f"tab_{idx}"
        elements.append({
            "id": elem_id,
            "type": "table",
            "page": p_num,
            "combined_bbox": comb_box,
            "raw_bbox": raw_box,
            "top_caption": top_cap,
            "bottom_notes": bottom_notes,
            "detection_sources": tab["sources"]
        })
        if 0 <= p_num - 1 < total_pages:
            page_rects_map[p_num - 1].append([comb_box["x1"], comb_box["y1"], comb_box["x2"], comb_box["y2"]])
            page_metas_map[p_num - 1].append({"id": elem_id, "type": "table"})

    # 6. Package Directly into .pdfedit Container (Zero Loose Stray Files)
    serialized_guidelines = {}
    for p_idx, r_list in page_rects_map.items():
        if r_list:
            serialized_guidelines[str(p_idx)] = {
                "h_lines": [], "v_lines": [], "selected_cells": [],
                "selected_rects": r_list,
                "rect_metas": page_metas_map.get(p_idx, []),
                "history": [["add_rect", r] for r in r_list]
            }

    # Deterministic Two-Way ID Linking
    with open(pdf_path, "rb") as f_hash:
        pdf_sha = hashlib.sha256(f_hash.read()).hexdigest()
    bundle_id = hashlib.md5(f"{pdf_sha}_{out_bundle_name}".encode()).hexdigest()

    manifest_data = {
        "version": "2.0",
        "bundle_id": bundle_id,
        "bundle_name": out_bundle_name,
        "source_original_name": os.path.basename(pdf_path),
        "pdf_sha256": pdf_sha,
        "source_pdf": "document.pdf",
        "coordinate_system": {"origin": "top-left", "unit": "pt"},
        "font_profile": font_profile,
        "elements": elements,
        "current_page_idx": 0,
        "zoom": 1.5,
        "mode": "rect",
        "page_guidelines": serialized_guidelines
    }

    # Save first to atomic .tmp path
    if os.path.exists(tmp_bundle_path):
        try: os.remove(tmp_bundle_path)
        except Exception: pass

    ProjectManager.save_project(
        project_path=tmp_bundle_path,
        doc=pdf,
        state_data=manifest_data,
        docling_stream=docling_text_stream,
        password=password,
        include_expanded_reviews=True
    )

    pdf.close()

    # Verify written package before committing atomic rename
    if not is_bundle_complete_and_valid(tmp_bundle_path, password=password):
        if os.path.exists(tmp_bundle_path):
            os.remove(tmp_bundle_path)
        raise RuntimeError(f"Package verification failed for {out_bundle_name}")

    try:
        os.replace(tmp_bundle_path, out_bundle_path)
    except Exception:
        if os.path.exists(out_bundle_path):
            os.remove(out_bundle_path)
        os.rename(tmp_bundle_path, out_bundle_path)

    gc.collect()

    elapsed = time.time() - start_time
    print(f"[✓] Successfully generated ({elapsed:.1f}s): {out_bundle_name}")
    print(f"    • Bundle ID: {bundle_id}")
    print(f"    • Elements:  {len(final_images)} images, {len(final_tables)} tables, {len(docling_text_stream)} text nodes")
    return out_bundle_path, False


# ============================================================
# MAIN BATCH CLI RUNNER
# ============================================================

def load_input_pdf_list(args_inputs: list[str], list_file_path: str | None = None) -> list[str]:
    """Loads and deduplicates PDF paths from CLI arguments, JSON list files, or TXT lines."""
    pdf_files = []

    if list_file_path and os.path.exists(list_file_path):
        if list_file_path.lower().endswith(".json"):
            with open(list_file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    pdf_files.extend(loaded)
                elif isinstance(loaded, dict) and "pdfs" in loaded:
                    pdf_files.extend(loaded["pdfs"])
        else:
            with open(list_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    p = line.strip()
                    if p and not p.startswith("#"):
                        pdf_files.append(p)

    if args_inputs:
        for item in args_inputs:
            if os.path.isdir(item):
                pdf_files.extend(glob.glob(os.path.join(item, "**", "*.pdf"), recursive=True))
            elif os.path.isfile(item) and item.lower().endswith(".pdf"):
                pdf_files.append(item)
            elif "*" in item or "?" in item:
                pdf_files.extend(glob.glob(item, recursive=True))

    resolved = []
    for p in pdf_files:
        norm = os.path.normpath(p)
        if os.path.exists(norm) and norm.lower().endswith(".pdf"):
            resolved.append(norm)

    return sorted(list(dict.fromkeys(resolved)))


def main():
    parser = argparse.ArgumentParser(
        description="Universal Batch PDF to .pdfedit Converter with Resumable Tracking (Colab, GitHub Actions, Local)."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[],
        help="Input PDF paths, folder directories, or glob patterns (e.g. ./docs/*.pdf)."
    )
    parser.add_argument(
        "--list", "--pdf-list",
        dest="pdf_list",
        default=None,
        help="Path to JSON or TXT file containing a list of PDF file paths."
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="./output_bundles",
        help="Destination directory for generated .pdfedit packages (default: ./output_bundles)."
    )
    parser.add_argument(
        "-p", "--password",
        default=None,
        help="Password for package authenticated encryption (can also be passed via PDFEDIT_PASSWORD env var)."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Resume processing, skipping already-completed valid bundles (default: True)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-extraction even if valid bundle already exists."
    )
    parser.add_argument(
        "--repack-manifest",
        default=None,
        help="Repack an edited project.json back into its linked .pdfedit package."
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Disable OCR pass in Docling (faster on pure digital PDFs)."
    )
    parser.add_argument(
        "--git-push",
        action="store_true",
        help="Automatically git commit & push after each finished PDF bundle."
    )

    args = parser.parse_args()

    # Password Resolution: CLI Flag -> Environment Variable
    active_password = args.password or os.environ.get("PDFEDIT_PASSWORD")

    # Repack Mode
    if args.repack_manifest:
        print(f"[*] Repacking manifest into .pdfedit: {args.repack_manifest}")
        target = ProjectManager.repack_manifest_into_bundle(args.repack_manifest, password=active_password)
        print(f"[✓] Repack completed successfully -> {target}")
        sys.exit(0)

    # Collect Input PDF files
    pdf_files = load_input_pdf_list(args.inputs, args.pdf_list)
    if not pdf_files:
        print("Error: No valid PDF files found to process. Provide input paths or --list.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    tracker = load_progress_tracker(args.output_dir)

    print("=" * 75)
    print("🚀 Batch PDF to .pdfedit Converter (Resumable Pipeline)")
    print(f"   • Total PDFs queued:     {len(pdf_files)}")
    print(f"   • Output Directory:      {os.path.abspath(args.output_dir)}")
    print(f"   • Resumable Checkpoints: {'Enabled (skip valid)' if not args.force else 'Disabled (force overwrite)'}")
    print(f"   • Encryption:            {'🔒 Password Protected' if active_password else '🔓 Plain'}")
    print("=" * 75)

    # Initialize Docling converter once for entire batch
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, HeadingHierarchyOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_opts = PdfPipelineOptions()
    pipeline_opts.do_ocr = not args.no_ocr
    pipeline_opts.do_table_structure = True
    pipeline_opts.heading_hierarchy_options = HeadingHierarchyOptions(enabled=True)
    pipeline_opts.generate_page_images = False

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)}
    )

    new_completed = 0
    skipped_count = 0
    failed_count = 0

    for idx, pdf_p in enumerate(pdf_files, start=1):
        rel_id = os.path.basename(pdf_p)
        print(f"\n[{idx}/{len(pdf_files)}] Checking '{rel_id}'...")

        try:
            bundle_path, was_skipped = process_single_pdf_to_pdfedit(
                source_pdf_path=pdf_p,
                output_dir=args.output_dir,
                converter=converter,
                password=active_password,
                resume=(not args.force)
            )

            if was_skipped:
                skipped_count += 1
            else:
                new_completed += 1

            # Update progress tracker
            tracker.setdefault("items", {})[rel_id] = {
                "bundle_name": os.path.basename(bundle_path),
                "source_path": pdf_p,
                "status": "completed",
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            save_progress_tracker(args.output_dir, tracker)

            # Auto-sync to GitHub repository if enabled
            if args.git_push and not was_skipped:
                manifest_file = os.path.join(args.output_dir, PROGRESS_MANIFEST_NAME)
                synced = git_commit_and_push(
                    file_paths=[bundle_path, manifest_file],
                    commit_message=f"feat(bundles): add {os.path.basename(bundle_path)} [skip ci]"
                )
                if synced:
                    print(f"    ☁️ Synced to GitHub: {os.path.basename(bundle_path)}")

        except Exception as e:
            failed_count += 1
            print(f"[!] Failed processing '{pdf_p}': {e}", file=sys.stderr)
            tracker.setdefault("items", {})[rel_id] = {
                "source_path": pdf_p,
                "status": "failed",
                "error": str(e),
                "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            save_progress_tracker(args.output_dir, tracker)

    print("\n" + "=" * 75)
    print(f"✨ Batch Summary: {new_completed} newly created, {skipped_count} resumed/skipped, {failed_count} failed")
    print(f"📁 Destination:   {os.path.abspath(args.output_dir)}")
    print("=" * 75)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()