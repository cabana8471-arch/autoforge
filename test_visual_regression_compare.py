"""
Visual Regression Image Comparison Tests
========================================

Tests for VisualRegressionTester.compare_images pixel-diff logic.
Run with: python -m pytest test_visual_regression_compare.py
"""

from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")

from visual_regression import VisualRegressionTester  # noqa: E402


def _save(path: Path, changed: tuple[int, int] | None = None) -> Path:
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    if changed:
        img.putpixel(changed, (255, 255, 255))
    img.save(path)
    return path


def test_identical_images_have_no_diff(tmp_path: Path):
    tester = VisualRegressionTester(tmp_path)
    a = _save(tmp_path / "a.png")
    b = _save(tmp_path / "b.png")

    percentage, passed = tester.compare_images(a, b, tmp_path / "diff.png")

    assert percentage == 0
    assert passed is True


def test_single_changed_pixel_is_counted_and_highlighted(tmp_path: Path):
    tester = VisualRegressionTester(tmp_path)
    a = _save(tmp_path / "a.png")
    b = _save(tmp_path / "b.png", changed=(3, 7))
    diff_path = tmp_path / "diff.png"

    percentage, passed = tester.compare_images(a, b, diff_path)

    assert percentage == 1.0  # 1 of 100 pixels
    assert passed is False
    with Image.open(diff_path) as diff:
        highlighted = diff.convert("RGB").getpixel((3, 7))
        untouched = diff.convert("RGB").getpixel((0, 0))
    assert highlighted != untouched


def test_small_difference_below_threshold_is_ignored(tmp_path: Path):
    tester = VisualRegressionTester(tmp_path)
    a = _save(tmp_path / "a.png")
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    img.putpixel((5, 5), (10, 10, 10))  # summed difference 30, not above threshold
    img.save(tmp_path / "b.png")

    percentage, passed = tester.compare_images(a, tmp_path / "b.png", tmp_path / "diff.png")

    assert percentage == 0
    assert passed is True
