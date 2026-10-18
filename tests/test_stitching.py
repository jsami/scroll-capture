import cv2
import numpy as np
import pytest

from scroll_capture.stitching import Status, VerticalStitcher, frame_is_stable


@pytest.fixture
def source():
    return np.random.default_rng(731).integers(0, 256, (2000, 360, 3), dtype=np.uint8)


def test_exact_reconstruction_for_varied_scroll_distances(source):
    stitcher = VerticalStitcher()
    offsets = [0, 63, 230, 488, 612, 866, 1000]
    for offset in offsets:
        result = stitcher.append(source[offset:offset + 384])
        assert result.status == Status.ACCEPTED
    np.testing.assert_array_equal(stitcher.image(), source[:1384])


def test_realistic_text_page_reconstruction():
    page = np.full((2400, 600, 3), 250, np.uint8)
    rng = np.random.default_rng(81)
    for y in range(35, 2400, 37):
        text = f"Row {y}: {rng.integers(100000, 999999)} — content for scrolling"
        cv2.putText(page, text, (18, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (30, 40, 50), 1, cv2.LINE_AA)
    stitcher = VerticalStitcher()
    for offset in [0, 173, 330, 511, 740, 970]:
        assert stitcher.append(page[offset:offset + 480]).status == Status.ACCEPTED
    np.testing.assert_array_equal(stitcher.image(), page[:1450])


def test_duplicate_and_bottom_frames_do_not_duplicate_rows(source):
    stitcher = VerticalStitcher()
    frame = source[:300]
    stitcher.append(frame)
    for _ in range(4):
        assert stitcher.append(frame).status == Status.UNCHANGED
    assert stitcher.height == 300
    np.testing.assert_array_equal(stitcher.image(), frame)


@pytest.mark.parametrize("kind", ["no_overlap", "upward", "horizontal", "animation"])
def test_bad_frames_preserve_reference_and_allow_recovery(source, kind):
    stitcher = VerticalStitcher()
    stitcher.append(source[200:600])
    if kind == "no_overlap":
        bad = source[550:950]
    elif kind == "upward":
        bad = source[100:500]
    elif kind == "horizontal":
        bad = np.roll(source[300:700], 8, axis=1)
    else:
        bad = source[300:700].copy()
        bad[30:170] = 0
    assert stitcher.append(bad).status == Status.REJECTED
    assert stitcher.height == 400
    assert stitcher.append(source[300:700]).status == Status.ACCEPTED
    np.testing.assert_array_equal(stitcher.image(), source[200:700])


def test_ambiguous_repetition_is_rejected():
    pattern = np.random.default_rng(4).integers(0, 256, (32, 200, 3), np.uint8)
    page = np.tile(pattern, (30, 1, 1))
    stitcher = VerticalStitcher()
    stitcher.append(page[:320])
    assert stitcher.append(page[13:333]).status == Status.REJECTED
    assert stitcher.height == 320


def test_blank_content_does_not_invent_displacement():
    stitcher = VerticalStitcher()
    stitcher.append(np.full((320, 200, 3), 255, np.uint8))
    changed = np.full((320, 200, 3), 200, np.uint8)
    assert stitcher.append(changed).status == Status.REJECTED
    assert stitcher.height == 320


def test_small_animation_is_tolerated(source):
    stitcher = VerticalStitcher()
    stitcher.append(source[:400])
    changed = source[100:500].copy()
    changed[20:23, 20:25] = 0
    assert stitcher.append(changed).status == Status.ACCEPTED
    # Animation lies within the discarded overlap; accepted pixels are exact.
    np.testing.assert_array_equal(stitcher.image(), source[:500])


@pytest.mark.parametrize("limits", [{"max_height": 450}, {"max_pixels": 360 * 450}])
def test_limits_preserve_accepted_image(source, limits):
    stitcher = VerticalStitcher(**limits)
    stitcher.append(source[:400])
    assert stitcher.append(source[100:500]).status == Status.LIMIT
    np.testing.assert_array_equal(stitcher.image(), source[:400])


def test_invalid_frames_and_dimensions(source):
    stitcher = VerticalStitcher()
    with pytest.raises(ValueError):
        stitcher.append(np.zeros((100, 100), np.uint8))
    with pytest.raises(ValueError):
        stitcher.append(np.zeros((50, 100, 3), np.uint8))
    with pytest.raises(ValueError):
        stitcher.image()
    stitcher.append(source[:400])
    assert stitcher.append(source[:399]).status == Status.REJECTED


def test_settling_detection(source):
    assert frame_is_stable(source[:400], source[:400].copy())
    assert not frame_is_stable(source[:400], source[1:401])


def test_viewport_border_does_not_repeat_at_every_join():
    page = np.full((1500, 360, 3), 248, np.uint8)
    rng = np.random.default_rng(22)
    for y in range(30, 1490, 37):
        text = f"Item {y}   {rng.integers(100000, 999999)}"
        cv2.putText(page, text, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (25, 35, 40), 1, cv2.LINE_AA)
    stitcher = VerticalStitcher()
    for offset in (0, 120, 240, 360, 480, 600):
        frame = page[offset:offset + 400].copy()
        frame[-3:] = 230  # Fixed bottom border/shadow over the scrollable content.
        assert stitcher.append(frame).status == Status.ACCEPTED
    expected = page[:1000].copy()
    expected[-3:] = 230  # Retain the final viewport edge once.
    np.testing.assert_array_equal(stitcher.image(), expected)


def test_top_and_bottom_shadows_with_tiny_and_maximum_scroll_steps(source):
    stitcher = VerticalStitcher()
    for offset in (0, 1, 64, 352, 400, 688):
        frame = source[offset:offset + 384].copy()
        frame[:8] = 240
        frame[-8:] = 180
        assert stitcher.append(frame).status == Status.ACCEPTED
    expected = source[:1072].copy()
    expected[:8] = 240
    expected[-8:] = 180
    np.testing.assert_array_equal(stitcher.image(), expected)


def test_many_tiny_scroll_steps_keep_storage_bounded():
    source = np.random.default_rng(92).integers(0, 256, (400, 96, 3), np.uint8)
    stitcher = VerticalStitcher()
    for offset in range(120):
        assert stitcher.append(source[offset:offset + 128]).status == Status.ACCEPTED
    np.testing.assert_array_equal(stitcher.image(), source[:247])
    # Count actual owning allocations, not view lengths: a retained overlap can
    # otherwise multiply memory use for every one-pixel step.
    owners = {}
    for strip in [*stitcher._strips, stitcher._last]:
        while isinstance(strip.base, np.ndarray):
            strip = strip.base
        owners[id(strip)] = strip.nbytes
    assert sum(owners.values()) <= (stitcher.height + 128) * 96 * 3
