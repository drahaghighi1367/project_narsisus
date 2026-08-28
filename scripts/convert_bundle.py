# E:/pdf_cloud_pipeline/scripts/convert_bundle.py

"""
High-Performance Bundle Converter for .pdfedit <-> .pdfeditlight
Allows bi-directional conversion, rehydration, and batch verification:
  - .pdfeditlight -> .pdfedit (Rehydrates light manifests with matching source PDFs)
  - .pdfedit -> .pdfeditlight (Strips embedded PDF binary for ultra-lean storage & Git commits)
"""

import os
import sys
import glob
import argparse
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.project_manager import (
    ProjectManager,
    SourcePdfNotFoundError,
    ChecksumMismatchError,
    PasswordRequiredError,
    InvalidPasswordError
)


def main():
    parser = argparse.ArgumentParser(
        description="Convert and rehydrate between .pdfedit (full) and .pdfeditlight (lean) bundles."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input .pdfedit or .pdfeditlight file paths, directories, or glob patterns."
    )
    parser.add_argument(
        "--to-light",
        action="store_true",
        help="Convert full .pdfedit bundles to lightweight .pdfeditlight (strips document.pdf)."
    )
    parser.add_argument(
        "--to-full",
        action="store_true",
        help="Convert lightweight .pdfeditlight bundles to full .pdfedit (rehydrates with PDF)."
    )
    parser.add_argument(
        "--pdf-dir",
        default=None,
        help="Directory or search pool where original PDF files are stored (for light -> full conversion)."
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Custom destination directory for converted bundles (default: alongside source)."
    )
    parser.add_argument(
        "-p", "--password",
        default=None,
        help="Password for encrypted bundles (or via PDFEDIT_PASSWORD environment variable)."
    )
    parser.add_argument(
        "--no-verify-checksum",
        action="store_true",
        help="Skip strict SHA-256 checksum verification when rehydrating with a PDF."
    )

    args = parser.parse_args()

    active_password = args.password or os.environ.get("PDFEDIT_PASSWORD")

    # Gather files
    bundle_files = []
    for item in args.inputs:
        if os.path.isdir(item):
            bundle_files.extend(glob.glob(os.path.join(item, "*.pdfedit")))
            bundle_files.extend(glob.glob(os.path.join(item, "*.pdfeditlight")))
        elif os.path.isfile(item):
            bundle_files.append(item)
        else:
            bundle_files.extend(glob.glob(item))

    bundle_files = list(dict.fromkeys(os.path.normpath(p) for p in bundle_files))

    if not bundle_files:
        print("Error: No .pdfedit or .pdfeditlight files found.", file=sys.stderr)
        sys.exit(1)

    print("=" * 75)
    print(f"📦 Bundle Converter ({len(bundle_files)} files queued)")
    print(f"   • Mode: {'To Light (.pdfeditlight)' if args.to_light else ('To Full (.pdfedit)' if args.to_full else 'Auto-Detect')}")
    if args.pdf_dir:
        print(f"   • PDF Pool: {os.path.abspath(args.pdf_dir)}")
    if args.output_dir:
        print(f"   • Destination: {os.path.abspath(args.output_dir)}")
    print("=" * 75)

    success_count = 0
    failed_count = 0

    search_dirs = [args.pdf_dir] if args.pdf_dir else ["inputs", "./inputs", "../inputs"]

    for b_path in bundle_files:
        try:
            dest_dir = args.output_dir or os.path.dirname(b_path)
            os.makedirs(dest_dir, exist_ok=True)
            stem = Path(b_path).stem

            # Determine direction
            is_light_input = b_path.lower().endswith(".pdfeditlight")

            if args.to_light or (not args.to_full and not is_light_input and not args.to_light):
                # Full -> Light
                target_out = os.path.join(dest_dir, f"{stem}.pdfeditlight")
                out = ProjectManager.convert_full_to_light(
                    full_bundle_path=b_path,
                    output_light_bundle_path=target_out,
                    password=active_password
                )
                print(f"[✓] Created Light Bundle ({os.path.getsize(out)/1024:.1f} KB): {os.path.basename(out)}")
                success_count += 1

            else:
                # Light -> Full
                target_out = os.path.join(dest_dir, f"{stem}.pdfedit")
                out = ProjectManager.convert_light_to_full(
                    light_bundle_path=b_path,
                    output_full_bundle_path=target_out,
                    search_dirs=search_dirs,
                    password=active_password,
                    verify_checksum=not args.no_verify_checksum
                )
                print(f"[✓] Rehydrated Full Bundle ({os.path.getsize(out)/(1024*1024):.2f} MB): {os.path.basename(out)}")
                success_count += 1

        except Exception as e:
            failed_count += 1
            print(f"[!] Failed '{os.path.basename(b_path)}': {e}", file=sys.stderr)

    print("\n" + "=" * 75)
    print(f"Summary: {success_count} succeeded, {failed_count} failed.")
    print("=" * 75)


if __name__ == "__main__":
    main()