# E:/pdf_cloud_pipeline/core/redactor.py

import pymupdf
from PyQt6.QtCore import QThread, pyqtSignal
from core.utils import merge_connected_rects, get_best_fontname


class RedactionWorker(QThread):
    """Background worker thread for batch redaction jobs."""
    progress_changed = pyqtSignal(int, int)
    finished = pyqtSignal(str, int, str)
    error = pyqtSignal(str)

    def __init__(self, pdf_path, out_path, target_pages, active_rules, fill_opt, gfx_opt, img_opt, text_opt, export_path=None):
        super().__init__()
        self.pdf_path = pdf_path
        self.out_path = out_path
        self.target_pages = target_pages
        self.active_rules = active_rules
        self.fill_opt = fill_opt
        self.gfx_opt = gfx_opt
        self.img_opt = img_opt
        self.text_opt = text_opt
        self.export_path = export_path
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            export_doc = pymupdf.open() if self.export_path else None
            total = len(self.target_pages)
            exported_count = 0

            for step, page_num in enumerate(self.target_pages):
                if self._is_cancelled:
                    doc.close()
                    if export_doc is not None:
                        export_doc.close()
                    return

                p_idx = page_num - 1
                page = doc[p_idx]
                is_odd = (page_num % 2 != 0)

                raw_rects = []
                for rule in self.active_rules:
                    target = rule["target"]
                    if (
                        target == "ALL"
                        or (target == "ODD" and is_odd)
                        or (target == "EVEN" and not is_odd)
                        or (isinstance(target, int) and target == page_num)
                    ):
                        raw_rects.append(rule["rect"])

                applicable_rects = merge_connected_rects(raw_rects)

                # 1. Export vector crops if requested
                if export_doc is not None and applicable_rects:
                    for r in applicable_rects:
                        if r.width > 1 and r.height > 1:
                            new_p = export_doc.new_page(-1, width=r.width, height=r.height)
                            new_p.show_pdf_page(
                                pymupdf.Rect(0, 0, r.width, r.height),
                                doc,
                                p_idx,
                                clip=r
                            )
                            exported_count += 1

                # 2. Redact / Erase
                if applicable_rects:
                    for r in applicable_rects:
                        page.add_redact_annot(r, fill=self.fill_opt)
                    page.apply_redactions(images=self.img_opt, graphics=self.gfx_opt, text=self.text_opt)

                self.progress_changed.emit(step + 1, total)

            if self._is_cancelled:
                doc.close()
                if export_doc is not None:
                    export_doc.close()
                return

            if export_doc is not None and exported_count > 0:
                export_doc.save(self.export_path, deflate=True, garbage=4)
                export_doc.close()

            doc.save(self.out_path, deflate=True, garbage=4)
            doc.close()
            self.finished.emit(self.out_path, total, self.export_path or "")

        except Exception as e:
            self.error.emit(str(e))


import os


class EraseCutWorker(QThread):
    """Background worker for batch erasing, redacting, and vector cutout export."""
    progress_changed = pyqtSignal(int, int)  # (current_step, total_steps)
    finished = pyqtSignal(str, int, int, str)  # (pdf_path, affected_pages, extracted_count, export_path)
    error = pyqtSignal(str)

    def __init__(
        self,
        pdf_path: str,
        pages_to_process: list[tuple[int, list[pymupdf.Rect]]],
        fill_opt,
        gfx_opt: int,
        img_opt: int,
        text_opt: int,
        export_path: str | None = None
    ):
        super().__init__()
        self.pdf_path = pdf_path
        self.pages_to_process = pages_to_process
        self.fill_opt = fill_opt
        self.gfx_opt = gfx_opt
        self.img_opt = img_opt
        self.text_opt = text_opt
        self.export_path = export_path
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            export_doc = pymupdf.open() if self.export_path else None
            total_steps = len(self.pages_to_process)
            affected_pages = 0
            extracted_pages_count = 0

            for step, (p_idx, p_rects) in enumerate(self.pages_to_process):
                if self._is_cancelled:
                    doc.close()
                    if export_doc is not None:
                        export_doc.close()
                    return

                if not (0 <= p_idx < len(doc)) or not p_rects:
                    continue

                page = doc[p_idx]
                merged_rects = merge_connected_rects(p_rects)

                # 1. Export vector cutouts
                if export_doc is not None:
                    for r in merged_rects:
                        if r.width > 1 and r.height > 1:
                            new_p = export_doc.new_page(-1, width=r.width, height=r.height)
                            new_p.show_pdf_page(
                                pymupdf.Rect(0, 0, r.width, r.height),
                                doc,
                                p_idx,
                                clip=r
                            )
                            extracted_pages_count += 1

                # 2. Redact content streams
                for r in merged_rects:
                    page.add_redact_annot(r, fill=self.fill_opt)
                page.apply_redactions(images=self.img_opt, graphics=self.gfx_opt, text=self.text_opt)
                affected_pages += 1

                self.progress_changed.emit(step + 1, total_steps)

            if self._is_cancelled:
                doc.close()
                if export_doc is not None:
                    export_doc.close()
                return

            if export_doc is not None and extracted_pages_count > 0:
                export_doc.save(self.export_path, deflate=True, garbage=4)
                export_doc.close()

            # Overwrite source PDF safely
            temp_path = self.pdf_path + ".tmp_erase"
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

            self.finished.emit(self.pdf_path, affected_pages, extracted_pages_count, self.export_path or "")
        except Exception as e:
            self.error.emit(str(e))


class RecolorWorker(QThread):
    """Background worker for multi-page in-place text recoloring."""
    progress_changed = pyqtSignal(int, int)  # (current_step, total_steps)
    finished = pyqtSignal(str, int, int)     # (pdf_path, affected_pages, spans_modified)
    error = pyqtSignal(str)

    def __init__(
        self,
        pdf_path: str,
        pages_to_process: list[tuple[int, list[pymupdf.Rect]]],
        rgb_tuple: tuple[float, float, float]
    ):
        super().__init__()
        self.pdf_path = pdf_path
        self.pages_to_process = pages_to_process
        self.rgb_tuple = rgb_tuple
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            total_steps = len(self.pages_to_process)
            affected_pages = 0
            total_spans = 0

            for step, (p_idx, p_rects) in enumerate(self.pages_to_process):
                if self._is_cancelled:
                    doc.close()
                    return

                if not (0 <= p_idx < len(doc)) or not p_rects:
                    continue

                page = doc[p_idx]
                merged_rects = merge_connected_rects(p_rects)
                spans_modified = recolor_page_spans(page, merged_rects, self.rgb_tuple)
                if spans_modified > 0:
                    affected_pages += 1
                    total_spans += spans_modified

                self.progress_changed.emit(step + 1, total_steps)

            if self._is_cancelled:
                doc.close()
                return

            temp_path = self.pdf_path + ".tmp_recolor"
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

            self.finished.emit(self.pdf_path, affected_pages, total_spans)
        except Exception as e:
            self.error.emit(str(e))


def build_page_font_cache(doc: pymupdf.Document, page: pymupdf.Page) -> dict:
    """
    Extracts embedded font buffers from the page and registers them
    so recolored text retains the exact original font.
    """
    font_cache = {}
    if doc is None or doc.is_closed:
        return font_cache

    for f_info in page.get_fonts():
        # f_info: (xref, ext, type, basefont, refname, encoding)
        xref = f_info[0]
        basefont = f_info[3] or ""
        refname = f_info[4] or ""
        clean_name = basefont.split("+")[-1] if "+" in basefont else basefont

        font_id = f"font_emb_{xref}"
        registered = False

        try:
            extracted = doc.extract_font(xref)
            font_buffer = extracted[3] if isinstance(extracted, tuple) else extracted.get("buffer")
            if font_buffer and len(font_buffer) > 0:
                page.insert_font(fontname=font_id, fontbuffer=font_buffer)
                registered = True
        except Exception:
            registered = False

        if registered:
            font_cache[basefont.lower()] = font_id
            font_cache[clean_name.lower()] = font_id
            font_cache[refname.lower()] = font_id

    return font_cache


def recolor_page_spans(page: pymupdf.Page, rect_list: list[pymupdf.Rect], rgb_tuple: tuple[float, float, float]) -> int:
    """
    Extracts text spans, redacts old text in place, and re-inserts with new RGB color,
    preserving original embedded fonts, metrics, and exact bounding box widths.
    """
    spans_to_replace = []
    doc = page.parent

    # 1. Build font cache from page's embedded font resources
    embedded_fonts = build_page_font_cache(doc, page)

    # 2. Locate all text spans inside selection rects
    for clip_rect in rect_list:
        text_dict = page.get_text("dict", clip=clip_rect)
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # Text block
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "").strip()
                        if txt:
                            span_bbox = pymupdf.Rect(span["bbox"])
                            if clip_rect.intersects(span_bbox):
                                spans_to_replace.append(span)

    if not spans_to_replace:
        return 0

    # 3. Redact old text
    for span in spans_to_replace:
        page.add_redact_annot(pymupdf.Rect(span["bbox"]), fill=False)
    page.apply_redactions(images=0, graphics=0, text=0)

    # 4. Re-insert text with new color using extracted embedded font or smart fallback
    for span in spans_to_replace:
        origin = pymupdf.Point(span["origin"])
        size = span.get("size", 10.0)
        orig_font = span.get("font", "").lower()
        clean_orig_font = orig_font.split("+")[-1] if "+" in orig_font else orig_font
        flags = span.get("flags", 0)
        span_bbox = pymupdf.Rect(span["bbox"])

        # Check if we have the exact embedded font registered
        matched_fontname = embedded_fonts.get(orig_font) or embedded_fonts.get(clean_orig_font)

        if matched_fontname:
            try:
                page.insert_text(
                    origin,
                    span["text"],
                    fontsize=size,
                    fontname=matched_fontname,
                    color=rgb_tuple
                )
                continue
            except Exception:
                pass

        # Fallback to standard base-14 fonts with horizontal scaling adjustment
        best_fallback = get_best_fontname(orig_font, flags)
        try:
            # Measure expected width vs fallback font length to maintain layout
            font_obj = pymupdf.Font(fontname=best_fallback)
            rendered_len = font_obj.text_length(span["text"], fontsize=size)
            orig_width = span_bbox.width

            # If difference > 5%, apply horizontal scaling morph
            if rendered_len > 0 and orig_width > 0 and abs(rendered_len - orig_width) > (orig_width * 0.05):
                scale_x = orig_width / rendered_len
                matrix = pymupdf.Matrix(scale_x, 1.0)
                page.insert_text(
                    origin,
                    span["text"],
                    fontsize=size,
                    fontname=best_fallback,
                    color=rgb_tuple,
                    morph=(origin, matrix)
                )
            else:
                page.insert_text(
                    origin,
                    span["text"],
                    fontsize=size,
                    fontname=best_fallback,
                    color=rgb_tuple
                )
        except Exception:
            page.insert_text(
                origin,
                span["text"],
                fontsize=size,
                fontname="helv",
                color=rgb_tuple
            )

    return len(spans_to_replace)