# E:/1405_pdf_editor/core/project_manager.py

import os
import json
import zipfile
import tempfile
import hashlib
import hmac
import struct
import pymupdf

try:
    from PyQt6.QtCore import QPoint
except ImportError:
    QPoint = None

from core.models import BoundingBox, PdfBoxElement


class PasswordRequiredError(Exception):
    """Raised when opening an encrypted project without providing a password."""
    pass


class InvalidPasswordError(Exception):
    """Raised when the password provided fails cryptographic authentication."""
    pass


# ---------------------------------------------------------------------------
# Lightweight, Pure-Python Authenticated Encryption Engine (Zero External Deps)
# PBKDF2-HMAC-SHA256 (200k rounds) + ChaCha20 Stream Cipher + HMAC-SHA256 (Encrypt-then-MAC)
# ---------------------------------------------------------------------------
class EncryptedPackageEngine:
    MAGIC_HEADER = b"PDFEDIT_ENC_V1\x00\x01"
    PBKDF2_ROUNDS = 200_000

    @classmethod
    def is_encrypted_file(cls, file_path: str) -> bool:
        if not os.path.exists(file_path):
            return False
        try:
            with open(file_path, "rb") as f:
                header = f.read(len(cls.MAGIC_HEADER))
                return header == cls.MAGIC_HEADER
        except Exception:
            return False

    @staticmethod
    def _chacha20_core(key: bytes, nonce: bytes, counter: int = 1) -> bytes:
        """Generates ChaCha20 keystream blocks."""
        def rotl32(v, c):
            return ((v << c) & 0xFFFFFFFF) | ((v & 0xFFFFFFFF) >> (32 - c))

        def quarter_round(state, a, b, c, d):
            state[a] = (state[a] + state[b]) & 0xFFFFFFFF
            state[d] = rotl32(state[d] ^ state[a], 16)
            state[c] = (state[c] + state[d]) & 0xFFFFFFFF
            state[b] = rotl32(state[b] ^ state[c], 12)
            state[a] = (state[a] + state[b]) & 0xFFFFFFFF
            state[d] = rotl32(state[d] ^ state[a], 8)
            state[c] = (state[c] + state[d]) & 0xFFFFFFFF
            state[b] = rotl32(state[b] ^ state[c], 7)

        constants = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]
        key_words = list(struct.unpack("<8I", key))
        nonce_words = list(struct.unpack("<3I", nonce))

        state = constants + key_words + [counter] + nonce_words
        working = list(state)

        for _ in range(10):
            quarter_round(working, 0, 4, 8, 12)
            quarter_round(working, 1, 5, 9, 13)
            quarter_round(working, 2, 6, 10, 14)
            quarter_round(working, 3, 7, 11, 15)
            quarter_round(working, 0, 5, 10, 15)
            quarter_round(working, 1, 6, 11, 12)
            quarter_round(working, 2, 7, 8, 13)
            quarter_round(working, 3, 4, 9, 14)

        out_words = [(working[i] + state[i]) & 0xFFFFFFFF for i in range(16)]
        return struct.pack("<16I", *out_words)

    @classmethod
    def _chacha20_process(cls, key: bytes, nonce: bytes, data: bytes) -> bytes:
        """XORs input stream with ChaCha20 keystream."""
        out = bytearray(len(data))
        counter = 1
        block_len = 64
        for i in range(0, len(data), block_len):
            keystream = cls._chacha20_core(key, nonce, counter)
            chunk = data[i:i + block_len]
            for j in range(len(chunk)):
                out[i + j] = chunk[j] ^ keystream[j]
            counter += 1
        return bytes(out)

    @classmethod
    def encrypt_payload(cls, plaintext_bytes: bytes, password: str) -> bytes:
        salt = os.urandom(16)
        nonce = os.urandom(12)

        # Derive 64 bytes: 32 bytes for ChaCha20 key + 32 bytes for HMAC-SHA256 auth
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, cls.PBKDF2_ROUNDS, dklen=64)
        enc_key = derived[:32]
        mac_key = derived[32:]

        ciphertext = cls._chacha20_process(enc_key, nonce, plaintext_bytes)

        # Compute HMAC over Header + Salt + Nonce + Ciphertext (Encrypt-then-MAC)
        mac_payload = cls.MAGIC_HEADER + salt + nonce + ciphertext
        tag = hmac.new(mac_key, mac_payload, hashlib.sha256).digest()

        return cls.MAGIC_HEADER + salt + nonce + tag + ciphertext

    @classmethod
    def decrypt_payload(cls, encrypted_bytes: bytes, password: str) -> bytes:
        header_len = len(cls.MAGIC_HEADER)
        if len(encrypted_bytes) < header_len + 16 + 12 + 32:
            raise ValueError("Corrupted or invalid encrypted package format.")

        header = encrypted_bytes[:header_len]
        if header != cls.MAGIC_HEADER:
            raise ValueError("Unknown encryption header format.")

        salt = encrypted_bytes[header_len:header_len + 16]
        nonce = encrypted_bytes[header_len + 16:header_len + 28]
        expected_tag = encrypted_bytes[header_len + 28:header_len + 60]
        ciphertext = encrypted_bytes[header_len + 60:]

        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, cls.PBKDF2_ROUNDS, dklen=64)
        enc_key = derived[:32]
        mac_key = derived[32:]

        # Verify HMAC tag first for instant password validation
        mac_payload = header + salt + nonce + ciphertext
        computed_tag = hmac.new(mac_key, mac_payload, hashlib.sha256).digest()

        if not hmac.compare_digest(expected_tag, computed_tag):
            raise InvalidPasswordError("Incorrect password or corrupted file.")

        return cls._chacha20_process(enc_key, nonce, ciphertext)


class ProjectManager:
    """Handles lossless packaging, extracting, and password encryption for .pdfedit packages."""

    VERSION = "2.0"

    @staticmethod
    def serialize_rect(r: pymupdf.Rect) -> list[float]:
        return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]

    @staticmethod
    def deserialize_rect(data: list[float] | dict) -> pymupdf.Rect:
        if isinstance(data, dict):
            return BoundingBox.from_dict(data).to_rect()
        return pymupdf.Rect(data[0], data[1], data[2], data[3])

    @classmethod
    def serialize_history(cls, history: list) -> list:
        serialized = []
        for action in history:
            action_type = action[0]
            val = action[1]
            if action_type in ("add_h", "add_v"):
                serialized.append([action_type, val])
            elif action_type == "toggle_cell":
                serialized.append([action_type, cls.serialize_rect(val)])
            elif action_type == "add_rect":
                serialized.append([action_type, cls.serialize_rect(val)])
            elif action_type == "add_rects":
                serialized.append([action_type, [cls.serialize_rect(r) for r in val]])
            elif action_type == "point":
                if hasattr(val, "x") and hasattr(val, "y"):
                    serialized.append([action_type, [val.x(), val.y()]])
                elif isinstance(val, (list, tuple)):
                    serialized.append([action_type, [val[0], val[1]]])
        return serialized

    @classmethod
    def deserialize_history(cls, serialized_history: list) -> list:
        deserialized = []
        for item in serialized_history:
            action_type = item[0]
            val = item[1]
            if action_type in ("add_h", "add_v"):
                deserialized.append((action_type, float(val)))
            elif action_type == "toggle_cell":
                deserialized.append((action_type, cls.deserialize_rect(val)))
            elif action_type == "add_rect":
                deserialized.append((action_type, cls.deserialize_rect(val)))
            elif action_type == "add_rects":
                deserialized.append((action_type, [cls.deserialize_rect(r) for r in val]))
            elif action_type == "point":
                if QPoint is not None:
                    deserialized.append((action_type, QPoint(int(val[0]), int(val[1]))))
                else:
                    deserialized.append((action_type, (int(val[0]), int(val[1]))))
        return deserialized

    @classmethod
    def reconcile_elements_losslessly(
        cls,
        existing_elements: list[PdfBoxElement | dict],
        page_guidelines: dict[int, dict]
    ) -> list[dict]:
        original_map: dict[tuple[int, str], PdfBoxElement] = {}
        for item in existing_elements:
            elem = item if isinstance(item, PdfBoxElement) else PdfBoxElement.from_dict(item)
            original_map[(elem.page, elem.id)] = elem

        reconciled: list[dict] = []

        for p_idx, g_state in page_guidelines.items():
            p_num = p_idx + 1
            rects = g_state.get("selected_rects", [])
            metas = g_state.get("rect_metas", [])

            for idx, r in enumerate(rects):
                meta = metas[idx] if idx < len(metas) else {}
                elem_id = meta.get("id", f"box_{p_num}_{idx + 1}")
                elem_type = meta.get("type", "generic")

                current_bbox = BoundingBox.from_rect(r) if isinstance(r, pymupdf.Rect) else BoundingBox.from_list(r)

                original_elem = original_map.get((p_num, elem_id))
                if original_elem:
                    original_elem.combined_bbox = current_bbox
                    original_elem.type = elem_type
                    reconciled.append(original_elem.to_dict())
                else:
                    new_elem = PdfBoxElement(
                        id=elem_id,
                        type=elem_type,
                        page=p_num,
                        combined_bbox=current_bbox,
                        raw_bbox=current_bbox,
                        detection_sources=["user_drawn" if elem_type == "generic" else "gui_auto_select"]
                    )
                    reconciled.append(new_elem.to_dict())

        return reconciled

    @classmethod
    def is_encrypted(cls, project_path: str) -> bool:
        """Returns True if the .pdfedit file is encrypted with password protection."""
        return EncryptedPackageEngine.is_encrypted_file(project_path)

    @classmethod
    def generate_expanded_review_pdf_bytes(
        cls,
        doc: pymupdf.Document,
        elements: list[dict],
        element_type: str,
        padding_x: float = 50.0
    ) -> bytes | None:
        """Renders expanded full-height human review slice PDF bytes with red bounding box overlays."""
        if not doc or not elements:
            return None

        from scripts.batch_extractor2 import get_element_rect
        review_doc = pymupdf.open()
        toc = []
        count = 0
        total_pages = len(doc)

        for index, item in enumerate(elements, start=1):
            if item.get("type") != element_type:
                continue
            p_num = item.get("page")
            if p_num is None:
                continue
            p_idx = int(p_num) - 1
            if not (0 <= p_idx < total_pages):
                continue

            page = doc[p_idx]
            pw, ph = page.rect.width, page.rect.height
            try:
                rect = get_element_rect(item, element_type, pw, ph)
            except Exception:
                continue

            if rect is None or rect.width <= 2 or rect.height <= 2:
                continue

            exp_x0 = max(0.0, rect.x0 - padding_x)
            exp_x1 = min(pw, rect.x1 + padding_x)
            exp_w = exp_x1 - exp_x0
            exp_h = ph

            slice_clip = pymupdf.Rect(exp_x0, 0.0, exp_x1, ph)
            new_page = review_doc.new_page(-1, width=exp_w, height=exp_h)
            new_page.show_pdf_page(
                pymupdf.Rect(0, 0, exp_w, exp_h),
                doc,
                p_idx,
                clip=slice_clip
            )

            # Pure RED inspection overlay box
            rel_box = pymupdf.Rect(rect.x0 - exp_x0, rect.y0, rect.x1 - exp_x0, rect.y1)
            new_page.draw_rect(rel_box, color=(1.0, 0.0, 0.0), width=2.0)

            count += 1
            elem_id = item.get("id", f"{element_type}_{index}")
            toc.append([1, f"Review {element_type.capitalize()} #{elem_id} (Page {p_num})", count])

        if count > 0:
            review_doc.set_toc(toc)
            pdf_bytes = review_doc.tobytes(deflate=True, garbage=4)
            review_doc.close()
            return pdf_bytes

        review_doc.close()
        return None

    @classmethod
    def save_project(
        cls,
        project_path: str,
        doc: pymupdf.Document,
        state_data: dict,
        docling_stream: list | None = None,
        password: str | None = None,
        include_expanded_reviews: bool = True
    ):
        """Packs active state, page dimensions, expanded review PDFs, and text stream into .pdfedit package."""
        if not project_path.lower().endswith(".pdfedit"):
            project_path += ".pdfedit"

        temp_dir = tempfile.mkdtemp(prefix="pdfedit_save_")
        pdf_temp_path = os.path.join(temp_dir, "document.pdf")
        manifest_path = os.path.join(temp_dir, "project.json")
        stream_path = os.path.join(temp_dir, "docling_stream.json")
        img_exp_path = os.path.join(temp_dir, "images_expanded.pdf")
        tab_exp_path = os.path.join(temp_dir, "tables_expanded.pdf")
        temp_zip_path = os.path.join(temp_dir, "temp_package.zip")

        try:
            doc.save(pdf_temp_path, deflate=True, garbage=4)
            state_data["version"] = cls.VERSION

            # Compute PDF Hash and Two-Way Bundle Link IDs
            with open(pdf_temp_path, "rb") as pf:
                pdf_bytes_for_hash = pf.read()
                pdf_sha = hashlib.sha256(pdf_bytes_for_hash).hexdigest()

            bundle_base = os.path.basename(project_path)
            state_data.setdefault("bundle_name", bundle_base)
            state_data.setdefault("bundle_id", hashlib.md5(f"{pdf_sha}_{bundle_base}".encode()).hexdigest())
            state_data["pdf_sha256"] = pdf_sha

            # Populate page dimension metadata
            state_data["page_dimensions"] = [
                {"page": p_idx + 1, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2)}
                for p_idx, p in enumerate(doc)
            ]

            elements = state_data.get("elements", [])

            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)

            stream_to_save = docling_stream if docling_stream is not None else state_data.get("docling_stream")
            if stream_to_save:
                with open(stream_path, "w", encoding="utf-8") as f:
                    json.dump(stream_to_save, f, indent=2, ensure_ascii=False)

            # Generate and write expanded review PDFs inside package if elements present
            if include_expanded_reviews and elements:
                img_bytes = cls.generate_expanded_review_pdf_bytes(doc, elements, "image", padding_x=50.0)
                if img_bytes:
                    with open(img_exp_path, "wb") as f:
                        f.write(img_bytes)

                tab_bytes = cls.generate_expanded_review_pdf_bytes(doc, elements, "table", padding_x=50.0)
                if tab_bytes:
                    with open(tab_exp_path, "wb") as f:
                        f.write(tab_bytes)

            # Build unified ZIP archive
            with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(pdf_temp_path, arcname="document.pdf")
                zipf.write(manifest_path, arcname="project.json")
                if os.path.exists(stream_path):
                    zipf.write(stream_path, arcname="docling_stream.json")
                if os.path.exists(img_exp_path):
                    zipf.write(img_exp_path, arcname="images_expanded.pdf")
                if os.path.exists(tab_exp_path):
                    zipf.write(tab_exp_path, arcname="tables_expanded.pdf")

            with open(temp_zip_path, "rb") as f:
                zip_bytes = f.read()

            # Encrypt if password provided
            if password and password.strip():
                final_bytes = EncryptedPackageEngine.encrypt_payload(zip_bytes, password.strip())
            else:
                final_bytes = zip_bytes

            with open(project_path, "wb") as f:
                f.write(final_bytes)

        finally:
            for p in (pdf_temp_path, manifest_path, stream_path, img_exp_path, tab_exp_path, temp_zip_path):
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if os.path.exists(temp_dir):
                try: os.rmdir(temp_dir)
                except Exception: pass

    @classmethod
    def load_project(cls, project_path: str, password: str | None = None) -> tuple[str, dict, list]:
        """
        Extracts the .pdfedit archive into a temporary folder, decrypting with password if protected.
        Returns (pdf_path, state_data, docling_stream).
        """
        if not os.path.exists(project_path):
            raise FileNotFoundError(f"Project file not found: {project_path}")

        with open(project_path, "rb") as f:
            raw_bytes = f.read()

        is_enc = EncryptedPackageEngine.is_encrypted_file(project_path)
        if is_enc:
            if not password:
                raise PasswordRequiredError("This workspace project is password-protected.")
            zip_bytes = EncryptedPackageEngine.decrypt_payload(raw_bytes, password)
        else:
            zip_bytes = raw_bytes

        temp_dir = tempfile.mkdtemp(prefix="pdfedit_load_")
        temp_zip_path = os.path.join(temp_dir, "temp_package.zip")

        try:
            with open(temp_zip_path, "wb") as f:
                f.write(zip_bytes)

            with zipfile.ZipFile(temp_zip_path, "r") as zipf:
                zipf.extractall(temp_dir)
        finally:
            if os.path.exists(temp_zip_path):
                try: os.remove(temp_zip_path)
                except Exception: pass

        pdf_extracted_path = os.path.join(temp_dir, "document.pdf")
        manifest_path = os.path.join(temp_dir, "project.json")
        stream_path = os.path.join(temp_dir, "docling_stream.json")

        if not os.path.exists(pdf_extracted_path) or not os.path.exists(manifest_path):
            raise ValueError("Invalid .pdfedit project package (missing document.pdf or project.json).")

        with open(manifest_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)

        docling_stream = []
        if os.path.exists(stream_path):
            with open(stream_path, "r", encoding="utf-8") as f:
                docling_stream = json.load(f)

        return pdf_extracted_path, state_data, docling_stream

    @classmethod
    def repack_manifest_into_bundle(
        cls,
        json_manifest_path: str,
        target_bundle_path: str | None = None,
        password: str | None = None
    ) -> str:
        """
        Updates or repacks an edited JSON manifest back into its linked .pdfedit package.
        """
        with open(json_manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        dest_bundle = target_bundle_path or manifest.get("bundle_name")
        if not dest_bundle:
            dest_bundle = str(Path(json_manifest_path).with_suffix(".pdfedit"))

        if not os.path.isabs(dest_bundle):
            dest_bundle = os.path.join(os.path.dirname(json_manifest_path), dest_bundle)

        if not os.path.exists(dest_bundle):
            raise FileNotFoundError(f"Target .pdfedit bundle not found: {dest_bundle}")

        pdf_path, old_state, docling_stream = cls.load_project(dest_bundle, password=password)
        doc = pymupdf.open(pdf_path)

        # Update metadata while preserving binary elements
        manifest["version"] = cls.VERSION
        manifest["bundle_name"] = os.path.basename(dest_bundle)

        cls.save_project(
            project_path=dest_bundle,
            doc=doc,
            state_data=manifest,
            docling_stream=docling_stream,
            password=password,
            include_expanded_reviews=True
        )
        doc.close()
        return dest_bundle