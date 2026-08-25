from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reminders_image_input  # noqa: E402


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL5WQAAAABJRU5ErkJggg=="
)
GIF_1X1 = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")


class ReminderImageInputTests(unittest.TestCase):
    def test_accepts_a_decoded_png_even_with_a_misleading_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "capture.jpg"
            image.write_bytes(PNG_1X1)

            result = reminders_image_input.validate_image_input(image)

        self.assertEqual(result.format, "png")
        self.assertEqual((result.width, result.height), (1, 1))
        self.assertEqual(result.bytes, len(PNG_1X1))
        self.assertRegex(result.sha256, r"^[0-9a-f]{64}$")

    def test_rejects_a_relative_path(self) -> None:
        with self.assertRaises(reminders_image_input.ImageInputError) as raised:
            reminders_image_input.validate_image_input(Path("capture.png"))

        self.assertEqual(raised.exception.reason_code, "absolute_path_required")

    def test_rejects_a_symlink_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.png"
            link = root / "link.png"
            target.write_bytes(PNG_1X1)
            link.symlink_to(target)

            with self.assertRaises(reminders_image_input.ImageInputError) as raised:
                reminders_image_input.validate_image_input(link)

        self.assertEqual(raised.exception.reason_code, "symlink_not_allowed")

    def test_rejects_a_file_over_the_byte_limit_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "oversized.png"
            with image.open("wb") as handle:
                handle.truncate(reminders_image_input.MAX_IMAGE_BYTES + 1)

            with self.assertRaises(reminders_image_input.ImageInputError) as raised:
                reminders_image_input.validate_image_input(image)

        self.assertEqual(raised.exception.reason_code, "image_too_large")

    def test_rejects_a_decoded_format_outside_png_and_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "image.gif"
            image.write_bytes(GIF_1X1)

            with self.assertRaises(reminders_image_input.ImageInputError) as raised:
                reminders_image_input.validate_image_input(image)

        self.assertEqual(raised.exception.reason_code, "unsupported_image_format")

    def test_rejects_dimension_and_pixel_bombs(self) -> None:
        cases = [
            ((reminders_image_input.MAX_IMAGE_DIMENSION + 1, 1), "image_dimensions_exceeded"),
            ((10_000, 5_000), "image_pixel_count_exceeded"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "image.png"
            image.write_bytes(PNG_1X1)
            for dimensions, reason_code in cases:
                with (
                    self.subTest(dimensions=dimensions),
                    mock.patch.object(
                        reminders_image_input,
                        "_inspect_image",
                        return_value=("png", *dimensions),
                    ),
                    self.assertRaises(reminders_image_input.ImageInputError) as raised,
                ):
                    reminders_image_input.validate_image_input(image)
                self.assertEqual(raised.exception.reason_code, reason_code)


if __name__ == "__main__":
    unittest.main()
