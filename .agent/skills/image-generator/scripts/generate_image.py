#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
SUPPORTED_MODELS = (
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
)
SUPPORTED_ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")
SUPPORTED_IMAGE_SIZES = ("1K", "2K", "4K")


@dataclass(frozen=True)
class GenerationResult:
    output_path: Path
    mime_type: str | None
    text: str | None


def _import_genai():
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - environment specific
        raise RuntimeError(
            "google-genai is not installed. Install project dependencies before using this skill."
        ) from exc
    return genai, types


def _iter_response_parts(response: object) -> Iterable[object]:
    direct_parts = getattr(response, "parts", None)
    if direct_parts:
        yield from direct_parts

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        yield from parts


def _decode_inline_data(data: object) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return base64.b64decode(data)
    raise TypeError(f"Unsupported inline data type: {type(data).__name__}")


def generate_image(
    prompt: str,
    output_path: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "16:9",
    image_size: str = "1K",
) -> GenerationResult:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    genai, types = _import_genai()
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_modalities=["Image"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        ),
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    text_parts: list[str] = []
    for part in _iter_response_parts(response):
        inline_data = getattr(part, "inline_data", None)
        if inline_data is not None and getattr(inline_data, "data", None):
            payload = _decode_inline_data(inline_data.data)
            output_path.write_bytes(payload)
            mime_type = getattr(inline_data, "mime_type", None)
            text = "\n".join(text_parts).strip() or None
            return GenerationResult(
                output_path=output_path,
                mime_type=mime_type,
                text=text,
            )

        text = getattr(part, "text", None)
        if text:
            text_parts.append(str(text))

    detail = "\n".join(text_parts).strip()
    if detail:
        raise RuntimeError(f"Model returned no image data. Response text: {detail}")
    raise RuntimeError("Model returned no image data.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an image with Gemini and save it to a file."
    )
    parser.add_argument(
        "--prompt", required=True, help="Text prompt for image generation"
    )
    parser.add_argument("--output", required=True, help="Output file path")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=SUPPORTED_MODELS,
        help="Gemini image model to use",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=SUPPORTED_ASPECT_RATIOS,
        help="Output aspect ratio",
    )
    parser.add_argument(
        "--image-size",
        default="1K",
        choices=SUPPORTED_IMAGE_SIZES,
        help="Output image size",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        result = generate_image(
            prompt=args.prompt,
            output_path=args.output,
            model=args.model,
            aspect_ratio=args.aspect_ratio,
            image_size=args.image_size,
        )
    except Exception as exc:
        print(f"Image generation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Saved image to: {result.output_path}")
    if result.mime_type:
        print(f"MIME type: {result.mime_type}")
    if result.text:
        print(f"Model text: {result.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
