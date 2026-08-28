# E:/1405_pdf_editor/core/asset_cleaner.py

import os
import re
import hashlib
import pymupdf
from PyQt6.QtCore import QThread, pyqtSignal
from core.utils import parse_page_range


def rect_iou(r1: pymupdf.Rect, r2: pymupdf.Rect) -> float:
    """Calculates Intersection over Union (IoU) of two rectangles."""
    inter = pymupdf.Rect(r1).intersect(r2)
    if inter.is_empty or inter.width <= 0 or inter.height <= 0:
        return 0.0
    inter_area = inter.width * inter.height
    union_area = (r1.width * r1.height) + (r2.width * r2.height) - inter_area
    return (inter_area / union_area) if union_area > 0 else 0.0


def deduplicate_rects(rect_list: list[pymupdf.Rect], iou_threshold: float = 0.45) -> list[pymupdf.Rect]:
    """
    Consolidates overlapping bounding boxes and retains the maximum enclosing rectangle,
    ensuring table headers, banners, and body rows remain united.
    """
    unique: list[pymupdf.Rect] = []
    for r in rect_list:
        if r.width <= 0 or r.height <= 0:
            continue
        matched_idx = -1
        for idx, u in enumerate(unique):
            inter = pymupdf.Rect(r).intersect(u)
            if not inter.is_empty and (inter.width > 0 and inter.height > 0):
                inter_area = inter.width * inter.height
                min_area = min(r.width * r.height, u.width * u.height)
                if (inter_area / min_area) > 0.35 or rect_iou(r, u) > iou_threshold:
                    matched_idx = idx
                    break
            elif (
                abs(r.x0 - u.x0) < 6.0
                and abs(r.y0 - u.y0) < 6.0
                and abs(r.x1 - u.x1) < 6.0
                and abs(r.y1 - u.y1) < 6.0
            ):
                matched_idx = idx
                break

        if matched_idx >= 0:
            u = unique[matched_idx]
            unique[matched_idx] = pymupdf.Rect(
                min(u.x0, r.x0),
                min(u.y0, r.y0),
                max(u.x1, r.x1),
                max(u.y1, r.y1)
            )
        else:
            unique.append(r)
    return unique


def expand_table_with_header(page: pymupdf.Page, table_rect: pymupdf.Rect) -> pymupdf.Rect:
    """
    Generic, language-agnostic table header expansion:
    1. Detects vector header banners directly attached to the top of the table body.
    2. Encompasses title text lines sitting directly above within table column bounds.
    """
    page_w = page.rect.width
    r = pymupdf.Rect(table_rect)

    # 1. Pure Geometric Vector Header Banner Check (Touching top of table body)
    try:
        drawings = page.get_drawings()
        for d in drawings:
            dr = d.get("rect", pymupdf.Rect())
            # Check if drawing is a banner attached directly above the table body (within 6 pt)
            if (
                dr.y1 >= r.y0 - 6.0
                and dr.y0 >= r.y0 - 65.0
                and dr.x0 <= r.x1
                and dr.x1 >= r.x0
                and dr.width >= min(80.0, r.width * 0.4)
                and dr.height <= 70.0
            ):
                r.y0 = min(r.y0, dr.y0)
                r.x0 = min(r.x0, dr.x0)
                r.x1 = max(r.x1, dr.x1)
    except Exception:
        pass

    # 2. Text Heading Check (Any title text block directly above table within column bounds)
    try:
        header_clip = pymupdf.Rect(
            max(0, r.x0 - 15),
            max(0, r.y0 - 50),
            min(page_w, r.x1 + 15),
            r.y0 + 2
        )
        header_dict = page.get_text("dict", clip=header_clip)
        for blk in header_dict.get("blocks", []):
            if blk.get("type") == 0:  # Text block
                for line in blk.get("lines", []):
                    l_bbox = pymupdf.Rect(line["bbox"])
                    # If the text line is within the table's horizontal column margins
                    if l_bbox.x0 >= r.x0 - 20 and l_bbox.x1 <= r.x1 + 20:
                        r.y0 = min(r.y0, l_bbox.y0 - 4)
                        r.x0 = min(r.x0, l_bbox.x0 - 4)
                        r.x1 = max(r.x1, l_bbox.x1 + 4)
    except Exception:
        pass

    return r


def find_all_page_images(page: pymupdf.Page, min_w: float = 100.0, min_h: float = 100.0) -> list[pymupdf.Rect]:
    """
    Locates ALL raster images on the page by querying image catalog infos,
    xref image placements, and form XObject image streams.
    """
    found_rects = []

    # 1. Page image info dicts
    try:
        image_infos = page.get_image_info(xrefs=True)
        for img in image_infos:
            bbox = pymupdf.Rect(img.get("bbox", (0, 0, 0, 0)))
            if bbox.width >= min_w and bbox.height >= min_h:
                if bbox.x1 > bbox.x0 and bbox.y1 > bbox.y0:
                    found_rects.append(bbox)
    except Exception:
        pass

    # 2. XREF-based image rect placements (catches multiple instances of same image on a page)
    try:
        raw_images = page.get_images(full=True)
        for img_tuple in raw_images:
            xref = img_tuple[0]
            if xref > 0:
                rects = page.get_image_rects(xref)
                for r in rects:
                    r_rect = pymupdf.Rect(r)
                    if r_rect.width >= min_w and r_rect.height >= min_h:
                        if r_rect.x1 > r_rect.x0 and r_rect.y1 > r_rect.y0:
                            found_rects.append(r_rect)
    except Exception:
        pass

    return deduplicate_rects(found_rects)


def find_all_page_tables(page: pymupdf.Page, min_w: float = 100.0, min_h: float = 100.0) -> list[pymupdf.Rect]:
    """
    Universally locates structured tables on any PDF:
    1. Grid & line-bordered tables (forms, statements, spreadsheets, invoices).
    2. Shaded & colored background tables (textbooks, papers, modern reports).
    3. Language-agnostic geometric header banner inclusion.
    """
    found_rects = []
    page_w = page.rect.width
    page_h = page.rect.height

    def is_valid_table_rect(r: pymupdf.Rect) -> bool:
        if r.width < min_w or r.height < min_h:
            return False
        # Suppress full-page backgrounds and whole-page text flows
        if r.width >= page_w * 0.96 and r.height >= page_h * 0.92:
            return False
        return True

    # 1. Standard PyMuPDF TableFinder (General Grid Tables)
    try:
        tabs1 = page.find_tables()
        for tab in tabs1.tables:
            r = pymupdf.Rect(tab.bbox)
            if is_valid_table_rect(r):
                found_rects.append(expand_table_with_header(page, r))
    except Exception:
        pass

    # 2. Line-Bordered Strategy (Bordered & Boxed Tables)
    try:
        tabs2 = page.find_tables(strategy="lines")
        for tab in tabs2.tables:
            r = pymupdf.Rect(tab.bbox)
            if is_valid_table_rect(r):
                found_rects.append(expand_table_with_header(page, r))
    except Exception:
        pass

    # 3. Shaded & Colored Background Table Blocks (Universal Vector Fills)
    try:
        drawings = page.get_drawings()
        for d in drawings:
            d_rect = d.get("rect", pymupdf.Rect())
            fill = d.get("fill")
            color = d.get("color")
            items = d.get("items", [])

            if (fill is not None or color is not None) and len(items) >= 1:
                if is_valid_table_rect(d_rect):
                    # Ensure content exists within the shaded region
                    text_inside = page.get_text("text", clip=d_rect).strip()
                    if len(text_inside) >= 8:
                        found_rects.append(expand_table_with_header(page, d_rect))
    except Exception:
        pass

    return deduplicate_rects(found_rects)


def get_image_data_and_hash(doc: pymupdf.Document, xref: int) -> tuple[bytes | None, str, str]:
    """Extracts raw image bytes, format extension, and MD5 hash."""
    if doc is None or doc.is_closed or xref <= 0:
        return None, "png", ""
    try:
        extracted = doc.extract_image(xref)
        if extracted:
            img_bytes = extracted.get("image", b"")
            ext = extracted.get("ext", "png")
            md5_hash = hashlib.md5(img_bytes).hexdigest()
            return img_bytes, ext, md5_hash
    except Exception:
        pass
    return None, "png", ""


def is_color_similar(c1, c2, eps=0.03) -> bool:
    """Compares RGB/grayscale colors with float tolerance."""
    if c1 is None and c2 is None:
        return True
    if c1 is None or c2 is None:
        return False
    if isinstance(c1, (int, float)) and isinstance(c2, (int, float)):
        return abs(c1 - c2) < eps
    if isinstance(c1, (list, tuple)) and isinstance(c2, (list, tuple)):
        if len(c1) != len(c2):
            return False
        return all(abs(a - b) < eps for a, b in zip(c1, c2))
    return c1 == c2


def is_vector_drawing_similar(
    target_d: dict,
    candidate_d: dict,
    size_tolerance: float = 0.08,
    match_position_y: bool = False,
    y_tolerance: float = 8.0
) -> bool:
    """Evaluates whether two vector drawings share visual structure, colors, and bounds."""
    t_items = tuple(item[0] for item in target_d.get("items", []))
    c_items = tuple(item[0] for item in candidate_d.get("items", []))
    if t_items != c_items:
        return False

    if not is_color_similar(target_d.get("color"), candidate_d.get("color")):
        return False
    if not is_color_similar(target_d.get("fill"), candidate_d.get("fill")):
        return False

    t_w = target_d.get("width") or 0.0
    c_w = candidate_d.get("width") or 0.0
    if abs(t_w - c_w) > 0.5:
        return False

    t_rect = target_d.get("rect", pymupdf.Rect())
    c_rect = candidate_d.get("rect", pymupdf.Rect())

    w_diff = abs(t_rect.width - c_rect.width)
    h_diff = abs(t_rect.height - c_rect.height)
    max_w_allowed = max(1.5, t_rect.width * size_tolerance)
    max_h_allowed = max(1.5, t_rect.height * size_tolerance)

    if w_diff > max_w_allowed or h_diff > max_h_allowed:
        return False

    if match_position_y:
        if abs(t_rect.y0 - c_rect.y0) > y_tolerance:
            return False

    return True


def remove_similar_images_across_doc(
    doc: pymupdf.Document,
    target_xref: int,
    target_hash: str,
    target_dimensions: tuple[int, int],
    target_pages: list[int],
    match_mode: str = "hash"  # "xref", "hash", or "geometry"
) -> int:
    """
    Scans pages and erases all occurrences of images matching XREF, stream MD5 hash,
    or geometric dimensions/aspect ratio.
    """
    if doc is None or doc.is_closed:
        return 0

    removed_instances = 0
    t_w, t_h = target_dimensions
    t_aspect = (t_w / t_h) if t_h > 0 else 1.0

    for page_num in target_pages:
        p_idx = page_num - 1
        if not (0 <= p_idx < len(doc)):
            continue
        page = doc[p_idx]
        image_infos = page.get_image_info(xrefs=True)
        rects_to_erase = []

        for img in image_infos:
            cur_xref = img.get("xref", 0)
            cur_w = img.get("width", 0)
            cur_h = img.get("height", 0)
            bbox = pymupdf.Rect(img.get("bbox", (0, 0, 0, 0)))

            is_match = False
            if match_mode == "xref" and cur_xref == target_xref and target_xref > 0:
                is_match = True
            elif match_mode == "hash" and target_hash:
                _, _, cur_hash = get_image_data_and_hash(doc, cur_xref)
                if cur_hash == target_hash:
                    is_match = True
                elif cur_xref == target_xref and target_xref > 0:
                    is_match = True
            elif match_mode == "geometry":
                cur_aspect = (cur_w / cur_h) if cur_h > 0 else 1.0
                if abs(cur_w - t_w) <= 2 and abs(cur_h - t_h) <= 2:
                    is_match = True
                elif abs(cur_aspect - t_aspect) < 0.02 and abs(bbox.width - t_w) < 4:
                    is_match = True

            if is_match and bbox.width > 0 and bbox.height > 0:
                rects_to_erase.append(bbox)

        if rects_to_erase:
            for r in rects_to_erase:
                page.add_redact_annot(r, fill=False)
            page.apply_redactions(images=1, graphics=0, text=0)
            removed_instances += len(rects_to_erase)

    return removed_instances


def remove_similar_vectors_across_doc(
    doc: pymupdf.Document,
    target_drawing: dict,
    target_pages: list[int],
    size_tolerance: float = 0.08,
    match_position_y: bool = False,
    y_tolerance: float = 8.0
) -> int:
    """
    Finds and redacts all matching vector drawings/lines/paths across the document.
    """
    if doc is None or doc.is_closed or not target_drawing:
        return 0

    removed_instances = 0

    for page_num in target_pages:
        p_idx = page_num - 1
        if not (0 <= p_idx < len(doc)):
            continue
        page = doc[p_idx]
        all_drawings = page.get_drawings()
        rects_to_erase = []

        for d in all_drawings:
            if is_vector_drawing_similar(
                target_drawing,
                d,
                size_tolerance=size_tolerance,
                match_position_y=match_position_y,
                y_tolerance=y_tolerance
            ):
                d_rect = d.get("rect", pymupdf.Rect())
                if d_rect.width > 0 and d_rect.height > 0:
                    rects_to_erase.append(d_rect)

        if rects_to_erase:
            for r in rects_to_erase:
                page.add_redact_annot(r, fill=False)
            page.apply_redactions(images=0, graphics=2, text=0)
            removed_instances += len(rects_to_erase)

    return removed_instances


class AutoSelectWorker(QThread):
    """Background worker for thread-safe scanning and detection of images and tables across pages."""
    progress_changed = pyqtSignal(int, int, int, int)
    finished = pyqtSignal(dict, int, int, int)
    error = pyqtSignal(str)

    def __init__(self, pdf_path: str, target_page_indices: list[int], min_w: float, min_h: float, detect_mode: str = "both"):
        super().__init__()
        self.pdf_path = pdf_path
        self.target_page_indices = target_page_indices
        self.min_w = min_w
        self.min_h = min_h
        self.detect_mode = detect_mode
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            results = {}
            total_images = 0
            total_tables = 0
            pages_affected = 0
            total_steps = len(self.target_page_indices)

            for step, p_idx in enumerate(self.target_page_indices):
                if self._is_cancelled:
                    doc.close()
                    return

                if not (0 <= p_idx < len(doc)):
                    continue

                page = doc[p_idx]
                page_rects = []

                if self.detect_mode in ("images", "both"):
                    imgs = find_all_page_images(page, min_w=self.min_w, min_h=self.min_h)
                    total_images += len(imgs)
                    page_rects.extend(imgs)

                if self.detect_mode in ("tables", "both"):
                    tables = find_all_page_tables(page, min_w=self.min_w, min_h=self.min_h)
                    total_tables += len(tables)
                    page_rects.extend(tables)

                if page_rects:
                    results[p_idx] = deduplicate_rects(page_rects)
                    pages_affected += 1

                self.progress_changed.emit(step + 1, total_steps, total_images, total_tables)

            doc.close()
            if not self._is_cancelled:
                self.finished.emit(results, total_images, total_tables, pages_affected)
        except Exception as e:
            self.error.emit(str(e))


class AssetCleanWorker(QThread):
    """Background worker for deep similar asset removal jobs across document pages."""
    progress_changed = pyqtSignal(int, int)
    finished = pyqtSignal(str, int)
    error = pyqtSignal(str)

    def __init__(
        self,
        pdf_path: str,
        target_pages: list[int],
        asset_type: str,
        target_data: dict,
        match_mode: str = "hash",
        match_position_y: bool = False
    ):
        super().__init__()
        self.pdf_path = pdf_path
        self.target_pages = target_pages
        self.asset_type = asset_type
        self.target_data = target_data
        self.match_mode = match_mode
        self.match_position_y = match_position_y
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            total = len(self.target_pages)
            removed_instances = 0

            if self.asset_type == "image":
                target_xref = self.target_data.get("xref", 0)
                target_hash = self.target_data.get("hash", "")
                t_w = self.target_data.get("width", 0)
                t_h = self.target_data.get("height", 0)
                t_aspect = (t_w / t_h) if t_h > 0 else 1.0

                for step, page_num in enumerate(self.target_pages):
                    if self._is_cancelled:
                        doc.close()
                        return

                    p_idx = page_num - 1
                    if not (0 <= p_idx < len(doc)):
                        continue
                    page = doc[p_idx]
                    image_infos = page.get_image_info(xrefs=True)
                    rects_to_erase = []

                    for img in image_infos:
                        cur_xref = img.get("xref", 0)
                        cur_w = img.get("width", 0)
                        cur_h = img.get("height", 0)
                        bbox = pymupdf.Rect(img.get("bbox", (0, 0, 0, 0)))

                        is_match = False
                        if self.match_mode == "xref" and cur_xref == target_xref and target_xref > 0:
                            is_match = True
                        elif self.match_mode == "hash" and target_hash:
                            _, _, cur_hash = get_image_data_and_hash(doc, cur_xref)
                            if cur_hash == target_hash or (cur_xref == target_xref and target_xref > 0):
                                is_match = True
                        elif self.match_mode == "geometry":
                            cur_aspect = (cur_w / cur_h) if cur_h > 0 else 1.0
                            if abs(cur_w - t_w) <= 2 and abs(cur_h - t_h) <= 2:
                                is_match = True
                            elif abs(cur_aspect - t_aspect) < 0.02 and abs(bbox.width - t_w) < 4:
                                is_match = True

                        if is_match and bbox.width > 0 and bbox.height > 0:
                            rects_to_erase.append(bbox)

                    if rects_to_erase:
                        for r in rects_to_erase:
                            page.add_redact_annot(r, fill=False)
                        page.apply_redactions(images=1, graphics=0, text=0)
                        removed_instances += len(rects_to_erase)

                    self.progress_changed.emit(step + 1, total)

            elif self.asset_type == "vector":
                target_drawing = self.target_data.get("drawing", {})
                for step, page_num in enumerate(self.target_pages):
                    if self._is_cancelled:
                        doc.close()
                        return

                    p_idx = page_num - 1
                    if not (0 <= p_idx < len(doc)):
                        continue
                    page = doc[p_idx]
                    all_drawings = page.get_drawings()
                    rects_to_erase = []

                    for d in all_drawings:
                        if is_vector_drawing_similar(
                            target_drawing,
                            d,
                            size_tolerance=0.08,
                            match_position_y=self.match_position_y,
                            y_tolerance=8.0
                        ):
                            d_rect = d.get("rect", pymupdf.Rect())
                            if d_rect.width > 0 and d_rect.height > 0:
                                rects_to_erase.append(d_rect)

                    if rects_to_erase:
                        for r in rects_to_erase:
                            page.add_redact_annot(r, fill=False)
                        page.apply_redactions(images=0, graphics=2, text=0)
                        removed_instances += len(rects_to_erase)

                    self.progress_changed.emit(step + 1, total)

            if self._is_cancelled:
                doc.close()
                return

            temp_path = self.pdf_path + ".tmp_clean"
            doc.save(temp_path, deflate=True, garbage=4)
            doc.close()
            del doc

            try:
                os.replace(temp_path, self.pdf_path)
            except Exception:
                import time, gc
                gc.collect()
                time.sleep(0.15)
                if os.path.exists(self.pdf_path):
                    os.remove(self.pdf_path)
                os.rename(temp_path, self.pdf_path)

            self.finished.emit(self.pdf_path, removed_instances)
        except Exception as e:
            self.error.emit(str(e))