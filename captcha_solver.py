"""
GeeTest slider captcha solver using OpenCV template matching.
Extracts puzzle piece and background images, finds offset, simulates drag.
"""
import asyncio
import base64
import io
import logging
import random
import re

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def find_slider_offset(bg_bytes: bytes, piece_bytes: bytes) -> int | None:
    """Find X offset where puzzle piece fits into background using edge matching."""
    try:
        bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)

        if bg is None or piece is None:
            logger.warning("Failed to decode captcha images")
            return None

        # Edge detection
        bg_edges = cv2.Canny(bg, 100, 200)
        piece_edges = cv2.Canny(piece, 100, 200)

        # Template matching
        result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        logger.info("Captcha match: confidence=%.3f offset=%d", max_val, max_loc[0])

        if max_val < 0.1:
            logger.warning("Low confidence captcha match: %.3f", max_val)
            return None

        return max_loc[0]

    except Exception as e:
        logger.error("Captcha image processing error: %s", e)
        return None


async def solve_geetest_on_page(page) -> bool:
    """
    Solve GeeTest slider captcha on a Playwright page.
    Returns True if solved, False otherwise.
    """
    try:
        # Wait for GeeTest widget to appear
        await page.wait_for_timeout(2000)

        # Click "Продолжить" button if present
        btn = page.locator('button:has-text("Продолжить")')
        if await btn.count() > 0:
            await btn.click()
            await page.wait_for_timeout(3000)

        # Extract captcha images from canvas elements
        # GeeTest uses canvas for background and puzzle piece
        images = await page.evaluate("""() => {
            const result = {};

            // Try GeeTest v4 selectors
            const canvases = document.querySelectorAll('canvas');
            for (const c of canvases) {
                const w = c.width, h = c.height;
                if (w > 200 && h > 100) {
                    // This is likely the background
                    if (!result.bg || w > result.bgW) {
                        result.bg = c.toDataURL('image/png');
                        result.bgW = w;
                    }
                }
                if (w > 30 && w < 120 && h > 30) {
                    // This might be the puzzle piece
                    result.piece = c.toDataURL('image/png');
                }
            }

            // Try getting images from img tags inside geetest container
            const geetestImgs = document.querySelectorAll('.geetest_canvas_img img, .geetest_item_img img, img[src*="geetest"]');
            for (const img of geetestImgs) {
                const src = img.src || img.getAttribute('data-src');
                if (src) {
                    if (img.width > 200) result.bgUrl = src;
                    else result.pieceUrl = src;
                }
            }

            // Get slider button info
            const slider = document.querySelector('.geetest_slider_button, .geetest_btn, [class*=slider]');
            if (slider) {
                const rect = slider.getBoundingClientRect();
                result.slider = { x: rect.x, y: rect.y, w: rect.width, h: rect.height };
            }

            // Get track width for scaling
            const track = document.querySelector('.geetest_slider, [class*=slider-track], [class*=sliderContainer]');
            if (track) {
                result.trackWidth = track.getBoundingClientRect().width;
            }

            return result;
        }""")

        if not images:
            logger.warning("No captcha images found on page")
            return False

        logger.info("Captcha elements found: bg=%s piece=%s slider=%s",
                     "yes" if images.get("bg") or images.get("bgUrl") else "no",
                     "yes" if images.get("piece") or images.get("pieceUrl") else "no",
                     "yes" if images.get("slider") else "no")

        # Get background image bytes
        bg_bytes = None
        if images.get("bg"):
            # Canvas data URL: data:image/png;base64,...
            b64 = images["bg"].split(",", 1)[1]
            bg_bytes = base64.b64decode(b64)
        elif images.get("bgUrl"):
            import requests
            bg_bytes = requests.get(images["bgUrl"], timeout=10).content

        # Get piece image bytes
        piece_bytes = None
        if images.get("piece"):
            b64 = images["piece"].split(",", 1)[1]
            piece_bytes = base64.b64decode(b64)
        elif images.get("pieceUrl"):
            import requests
            piece_bytes = requests.get(images["pieceUrl"], timeout=10).content

        if not bg_bytes or not piece_bytes:
            logger.warning("Could not extract captcha images (bg=%s piece=%s)",
                           len(bg_bytes) if bg_bytes else 0,
                           len(piece_bytes) if piece_bytes else 0)
            return False

        # Find offset
        offset = find_slider_offset(bg_bytes, piece_bytes)
        if offset is None:
            return False

        # Scale offset if needed (canvas may be different size than slider track)
        track_width = images.get("trackWidth", 260)
        # GeeTest background is typically 260px wide
        bg_width = images.get("bgW", 260)
        if bg_width > 0:
            scaled_offset = int(offset * track_width / bg_width)
        else:
            scaled_offset = offset

        logger.info("Captcha: raw_offset=%d scaled=%d track=%d", offset, scaled_offset, track_width)

        # Simulate human-like slider drag
        slider = images.get("slider")
        if not slider:
            logger.warning("Slider button not found")
            return False

        start_x = slider["x"] + slider["w"] / 2
        start_y = slider["y"] + slider["h"] / 2

        await page.mouse.move(start_x, start_y)
        await page.mouse.down()

        # Human-like movement: ease-out with jitter
        steps = random.randint(25, 40)
        for i in range(steps):
            progress = (i + 1) / steps
            # Ease-out cubic
            eased = 1 - (1 - progress) ** 3
            x = start_x + scaled_offset * eased + random.uniform(-1.5, 1.5)
            y = start_y + random.uniform(-2, 2)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.008, 0.035))

        # Small pause at the end (human behavior)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.up()

        # Wait for result
        await page.wait_for_timeout(3000)

        # Check if solved
        title = await page.title()
        if "проблема" not in title.lower() and "ограничен" not in title.lower():
            logger.info("Captcha SOLVED! New title: %s", title[:50])
            return True

        logger.warning("Captcha solve attempt failed (title still blocked)")
        return False

    except Exception as e:
        logger.error("Captcha solve error: %s", e)
        return False
