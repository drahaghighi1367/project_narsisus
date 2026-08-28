# E:/pdf_cloud_pipeline/core/toc_tree.py

import os
import pymupdf
from core.utils import sanitize_filename


class TOCNode:
    """Represents a node in the hierarchical Table of Contents tree."""
    def __init__(self, level: int, title: str, start_page: int, parent=None):
        self.level = level
        self.title = title
        self.start_page = start_page
        self.raw_end_page = start_page
        self.effective_start = start_page
        self.effective_end = start_page
        self.parent = parent
        self.children = []
        self.relative_path = ""
        self.is_file = False
        self.is_folder = False


def build_toc_tree(doc: pymupdf.Document) -> tuple[TOCNode | None, list]:
    """Parses TOC bookmarks into a nested TOCNode tree model."""
    if doc is None or doc.is_closed:
        return None, []

    toc_data = doc.get_toc()
    if not toc_data:
        return None, []

    total_pages = len(doc)
    root = TOCNode(0, "Root", 1)
    root.raw_end_page = total_pages
    root.effective_end = total_pages

    stack = [root]

    # Front-matter check
    if toc_data[0][2] > 1:
        fm_node = TOCNode(1, "00_Front_Matter", 1, parent=root)
        fm_node.raw_end_page = toc_data[0][2] - 1
        root.children.append(fm_node)

    for item in toc_data:
        lvl, title, start_p = item
        node = TOCNode(lvl, sanitize_filename(title), start_p)

        while len(stack) > 1 and stack[-1].level >= lvl:
            stack.pop()

        parent = stack[-1]
        node.parent = parent
        parent.children.append(node)
        stack.append(node)

    def compute_raw_ends(n: TOCNode, boundary: int):
        for i, child in enumerate(n.children):
            if i + 1 < len(n.children):
                child_end = n.children[i + 1].start_page - 1
            else:
                child_end = boundary
            child.raw_end_page = max(child.start_page, child_end)
            compute_raw_ends(child, child.raw_end_page)

    compute_raw_ends(root, total_pages)
    return root, toc_data


from PyQt6.QtCore import QThread, pyqtSignal


class SplitWorker(QThread):
    """Background worker for batch document splitting & restructuring."""
    progress_changed = pyqtSignal(int, int)  # (current_step, total_steps)
    finished = pyqtSignal(str, int)          # (out_root, total_exported)
    error = pyqtSignal(str)

    def __init__(self, pdf_path: str, out_root: str, jobs: list[tuple[int, int, str]], toc_data: list):
        super().__init__()
        self.pdf_path = pdf_path
        self.out_root = out_root
        self.jobs = jobs
        self.toc_data = toc_data
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            src_doc = pymupdf.open(self.pdf_path)
            total = len(self.jobs)

            for step, (start_p, end_p, rel_path) in enumerate(self.jobs):
                if self._is_cancelled:
                    src_doc.close()
                    return

                full_pdf_path = os.path.join(self.out_root, rel_path)
                os.makedirs(os.path.dirname(full_pdf_path), exist_ok=True)

                sub_doc = pymupdf.open()
                sub_doc.insert_pdf(src_doc, from_page=start_p - 1, to_page=end_p - 1)

                if self.toc_data:
                    trimmed_toc = []
                    for item in self.toc_data:
                        lvl, t_title, t_page = item
                        if start_p <= t_page <= end_p:
                            trimmed_toc.append([lvl, t_title, t_page - start_p + 1])

                    if trimmed_toc:
                        min_lvl = min(x[0] for x in trimmed_toc)
                        for item in trimmed_toc:
                            item[0] = item[0] - min_lvl + 1
                        sub_doc.set_toc(trimmed_toc)

                sub_doc.save(full_pdf_path, deflate=True, garbage=4)
                sub_doc.close()

                self.progress_changed.emit(step + 1, total)

            src_doc.close()
            if not self._is_cancelled:
                self.finished.emit(self.out_root, total)
        except Exception as e:
            self.error.emit(str(e))


def apply_rules_and_compute_paths(node: TOCNode, level_rules: dict, current_dir: str = ""):
    """Recursively computes paths and applies forward overlap with strict parent clamping."""
    for idx, child in enumerate(node.children):
        rule = level_rules.get(child.level, {"role": "File", "overlap": 0, "pattern": "{idx:02d}_{title}"})
        role = rule["role"]
        overlap = rule["overlap"]
        pattern = rule["pattern"]

        formatted_name = pattern.format(
            idx=idx + 1,
            title=child.title,
            level=child.level,
            start=child.start_page,
            end=child.raw_end_page
        )

        is_last_sibling = (idx == len(node.children) - 1)
        if overlap > 0 and not is_last_sibling:
            child.effective_end = min(node.effective_end, child.raw_end_page + overlap)
        else:
            child.effective_end = min(node.effective_end, child.raw_end_page)

        child.effective_start = child.start_page

        if role == "Folder":
            child.is_folder = True
            child.is_file = False
            child.relative_path = os.path.join(current_dir, formatted_name)
            apply_rules_and_compute_paths(child, level_rules, child.relative_path)

        elif role == "File":
            child.is_folder = False
            child.is_file = True
            child.relative_path = os.path.join(current_dir, f"{formatted_name}.pdf")

        elif role == "Hybrid":
            child.is_folder = True
            child.is_file = True
            child.relative_path = os.path.join(current_dir, formatted_name)
            apply_rules_and_compute_paths(child, level_rules, child.relative_path)

        elif role == "Skip":
            child.is_folder = False
            child.is_file = False
            child.relative_path = ""
            apply_rules_and_compute_paths(child, level_rules, current_dir)