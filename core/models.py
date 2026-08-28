# E:/1405_pdf_editor/core/models.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import pymupdf


@dataclass
class BoundingBox:
    """
    Standardized top-left origin bounding box in PostScript points (pt).
    Coordinates: (x1, y1) = top-left, (x2, y2) = bottom-right.
    """
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self):
        # Normalize in case coordinates were reversed
        self.x1, self.x2 = min(float(self.x1), float(self.x2)), max(float(self.x1), float(self.x2))
        self.y1, self.y2 = min(float(self.y1), float(self.y2)), max(float(self.y1), float(self.y2))

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def is_valid(self) -> bool:
        return self.width > 0.0 and self.height > 0.0

    def to_rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.x1, self.y1, self.x2, self.y2)

    def to_dict(self) -> dict[str, float]:
        return {
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "x2": round(self.x2, 2),
            "y2": round(self.y2, 2)
        }

    def to_list(self) -> list[float]:
        return [round(self.x1, 2), round(self.y1, 2), round(self.x2, 2), round(self.y2, 2)]

    @classmethod
    def from_rect(cls, rect: pymupdf.Rect) -> BoundingBox:
        return cls(rect.x0, rect.y0, rect.x1, rect.y1)

    @classmethod
    def from_dict(cls, d: dict[str, Any], page_w: float | None = None, page_h: float | None = None) -> BoundingBox:
        if "x1" in d:
            x1, y1, x2, y2 = float(d["x1"]), float(d["y1"]), float(d["x2"]), float(d["y2"])
        elif "x0" in d:
            x1, y1, x2, y2 = float(d["x0"]), float(d["y0"]), float(d["x1"]), float(d["y1"])
        else:
            raise KeyError(f"Invalid bbox dict keys: {list(d.keys())}")

        if page_w is not None:
            x1 = max(0.0, min(page_w, x1))
            x2 = max(0.0, min(page_w, x2))
        if page_h is not None:
            y1 = max(0.0, min(page_h, y1))
            y2 = max(0.0, min(page_h, y2))

        return cls(x1, y1, x2, y2)

    @classmethod
    def from_list(cls, coords: list[float] | tuple[float, ...]) -> BoundingBox:
        if len(coords) != 4:
            raise ValueError(f"Expected 4 coordinate values, got {len(coords)}")
        return cls(coords[0], coords[1], coords[2], coords[3])


@dataclass
class TextAnnotation:
    """Rich caption or footnote annotation associated with an element."""
    text: str
    bbox: BoundingBox
    font: str = ""
    size: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "font": self.font,
            "size": round(self.size, 2)
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TextAnnotation:
        raw_box = d.get("bbox")
        bbox = BoundingBox.from_dict(raw_box) if isinstance(raw_box, dict) else BoundingBox.from_list(raw_box)
        return cls(
            text=d.get("text", ""),
            bbox=bbox,
            font=d.get("font", ""),
            size=float(d.get("size", 0.0))
        )


@dataclass
class PdfBoxElement:
    """
    Core semantic entity representing an image, table, or user box.
    Preserves all extraction lineage (captions, footnotes, raw detection bounding boxes, sources).
    """
    id: str
    type: str  # "image", "table", "generic"
    page: int  # 1-based page index
    combined_bbox: BoundingBox
    raw_bbox: BoundingBox | None = None
    caption: TextAnnotation | None = None
    top_caption: TextAnnotation | None = None
    bottom_notes: TextAnnotation | None = None
    detection_sources: list[str] = field(default_factory=list)
    custom_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "page": int(self.page),
            "combined_bbox": self.combined_bbox.to_dict(),
        }
        if self.raw_bbox:
            data["raw_bbox"] = self.raw_bbox.to_dict()
        if self.caption:
            data["caption"] = self.caption.to_dict()
        if self.top_caption:
            data["top_caption"] = self.top_caption.to_dict()
        if self.bottom_notes:
            data["bottom_notes"] = self.bottom_notes.to_dict()
        if self.detection_sources:
            data["detection_sources"] = list(self.detection_sources)
        if self.custom_metadata:
            data["custom_metadata"] = self.custom_metadata
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PdfBoxElement:
        c_box_raw = d.get("combined_bbox") or d.get("raw_bbox") or d.get("image_bbox") or d.get("table_bbox")
        if isinstance(c_box_raw, dict):
            c_box = BoundingBox.from_dict(c_box_raw)
        elif isinstance(c_box_raw, (list, tuple)):
            c_box = BoundingBox.from_list(c_box_raw)
        else:
            raise ValueError(f"Element {d.get('id', 'unknown')} has no valid bounding box.")

        r_box_raw = d.get("raw_bbox")
        r_box = None
        if isinstance(r_box_raw, dict):
            r_box = BoundingBox.from_dict(r_box_raw)
        elif isinstance(r_box_raw, (list, tuple)):
            r_box = BoundingBox.from_list(r_box_raw)

        caption = TextAnnotation.from_dict(d["caption"]) if isinstance(d.get("caption"), dict) else None
        top_cap = TextAnnotation.from_dict(d["top_caption"]) if isinstance(d.get("top_caption"), dict) else None
        bottom_notes = TextAnnotation.from_dict(d["bottom_notes"]) if isinstance(d.get("bottom_notes"), dict) else None

        return cls(
            id=str(d.get("id", "element")),
            type=str(d.get("type", "generic")),
            page=int(d.get("page", 1)),
            combined_bbox=c_box,
            raw_bbox=r_box,
            caption=caption,
            top_caption=top_cap,
            bottom_notes=bottom_notes,
            detection_sources=d.get("detection_sources", []),
            custom_metadata=d.get("custom_metadata", {})
        )


@dataclass
class DoclingStreamNode:
    """Frozen OCR/native reading stream node for instant zero-OCR markdown compilation."""
    page: int
    label: str
    text: str
    bbox: BoundingBox
    level: int | None = None

    def to_dict(self) -> dict[str, Any]:
        node = {
            "page": self.page,
            "label": self.label,
            "text": self.text,
            "bbox": self.bbox.to_dict()
        }
        if self.level is not None:
            node["level"] = self.level
        return node

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DoclingStreamNode:
        bx = d.get("bbox")
        bbox = BoundingBox.from_dict(bx) if isinstance(bx, dict) else BoundingBox.from_list(bx)
        return cls(
            page=int(d.get("page", 1)),
            label=str(d.get("label", "paragraph")),
            text=str(d.get("text", "")),
            bbox=bbox,
            level=d.get("level")
        )


@dataclass
class ProjectManifest:
    """Complete root model for project.json inside .pdfedit packages with two-way bundle tracking."""
    version: str = "2.0"
    bundle_id: str = ""
    bundle_name: str = ""
    source_original_name: str = ""
    pdf_sha256: str = ""
    source_pdf: str = "document.pdf"
    elements: list[PdfBoxElement] = field(default_factory=list)
    current_page_idx: int = 0
    zoom: float = 1.5
    mode: str = "rect"
    page_dimensions: list[dict[str, Any]] = field(default_factory=list)
    font_profile: dict[str, Any] = field(default_factory=dict)
    page_guidelines: dict[str, Any] = field(default_factory=dict)
    redactor_tab: dict[str, Any] = field(default_factory=dict)
    splitter_tab: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bundle_id": self.bundle_id,
            "bundle_name": self.bundle_name,
            "source_original_name": self.source_original_name,
            "pdf_sha256": self.pdf_sha256,
            "source_pdf": self.source_pdf,
            "coordinate_system": {
                "origin": "top-left",
                "unit": "pt"
            },
            "page_dimensions": self.page_dimensions,
            "font_profile": self.font_profile,
            "elements": [e.to_dict() for e in self.elements],
            "current_page_idx": self.current_page_idx,
            "zoom": self.zoom,
            "mode": self.mode,
            "page_guidelines": self.page_guidelines,
            "redactor_tab": self.redactor_tab,
            "splitter_tab": self.splitter_tab
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProjectManifest:
        elements_raw = d.get("elements", [])
        elements = [PdfBoxElement.from_dict(e) for e in elements_raw if isinstance(e, dict)]
        return cls(
            version=d.get("version", "2.0"),
            bundle_id=d.get("bundle_id", ""),
            bundle_name=d.get("bundle_name", ""),
            source_original_name=d.get("source_original_name", ""),
            pdf_sha256=d.get("pdf_sha256", ""),
            source_pdf=d.get("source_pdf", "document.pdf"),
            elements=elements,
            current_page_idx=int(d.get("current_page_idx", 0)),
            zoom=float(d.get("zoom", 1.5)),
            mode=str(d.get("mode", "rect")),
            page_dimensions=d.get("page_dimensions", []),
            font_profile=d.get("font_profile", {}),
            page_guidelines=d.get("page_guidelines", {}),
            redactor_tab=d.get("redactor_tab", {}),
            splitter_tab=d.get("splitter_tab", {})
        )