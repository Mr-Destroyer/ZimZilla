"""Render an image into terminal cells, for the operator's image pane.

A terminal has no pixels, so the picture is drawn with a half block: one ``▀``
cell carries two vertical samples, its foreground colour from the top sample
and its background from the bottom. Two samples per cell is what makes the
result roughly square on a normal terminal font, where a cell is about twice as
tall as it is wide.

Zoom works the way a viewer's does:

* **1.0** fits the whole image in the pane, letterboxed on the pane's
  background.
* **> 1.0** keeps the centre (plus any pan) and crops to it, so detail grows
  instead of the picture being resized into a blur.
* **< 1.0** shrinks further, leaving more background.

Pan is measured in rendered pixels, not source pixels, so one arrow press moves
the same distance whatever the zoom. The module is deliberately Textual-free:
``ImagePane`` is the widget, this is the part a test can call without a terminal.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

#: Two vertical samples per cell.
SAMPLES_PER_CELL = 2

#: A zoom outside this range is not useful: below it the image is a dot, above
#: it one source pixel fills the pane and there is nothing more to see.
MIN_ZOOM = 0.25
MAX_ZOOM = 16.0


def load(path: str | Path):
    """Open an image as RGB, honouring EXIF orientation.

    Returns a Pillow image or raises the underlying error; the caller turns it
    into a pane message. Animated GIFs are shown at their first frame — a still
    of a moving picture, which is what a cell grid can represent.
    """
    from PIL import Image, ImageOps

    image = Image.open(path)
    try:
        image.seek(0)
    except EOFError:
        pass
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        # Composite onto the pane's black background rather than letting a
        # transparent PNG arrive as a black rectangle with invisible content.
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (0, 0, 0, 255))
        image = Image.alpha_composite(background, image)
    return image.convert("RGB")


def halfblocks(image, width: int, height: int, *, zoom: float = 1.0,
               pan: tuple[float, float] = (0.0, 0.0)) -> Text:
    """Draw *image* into *width* columns and *height* rows of ``▀`` cells.

    ``pan`` is in rendered pixels. It is clamped inside the picture, so dragging
    at the edge stops rather than showing empty space beyond it.
    """
    from PIL import Image

    width = max(1, int(width))
    height = max(1, int(height))
    pixel_w = width
    pixel_h = height * SAMPLES_PER_CELL
    zoom = min(MAX_ZOOM, max(MIN_ZOOM, float(zoom or 1.0)))

    iw, ih = image.size
    if iw <= 0 or ih <= 0:
        return Text("(empty image)", style="dim")
    fit = min(pixel_w / iw, pixel_h / ih)
    scale = fit * zoom

    if zoom <= 1.0:
        new_w = max(1, round(iw * scale))
        new_h = max(1, round(ih * scale))
        canvas = Image.new("RGB", (pixel_w, pixel_h), (0, 0, 0))
        # A pan can move the letterboxed picture only within its spare room.
        max_x = max(0, (pixel_w - new_w) // 2)
        max_y = max(0, (pixel_h - new_h) // 2)
        x = (pixel_w - new_w) // 2 + int(max(-max_x, min(max_x, pan[0])))
        y = (pixel_h - new_h) // 2 + int(max(-max_y, min(max_y, pan[1])))
        resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas.paste(resized, (x, y))
    else:
        # Crop the visible source window, centred on the image centre plus the
        # pan (converted back into source pixels), then scale it to the pane.
        vis_w = min(iw, pixel_w / scale)
        vis_h = min(ih, pixel_h / scale)
        cx = iw / 2 + (pan[0] / scale)
        cy = ih / 2 + (pan[1] / scale)
        left = max(0.0, min(iw - vis_w, cx - vis_w / 2))
        top = max(0.0, min(ih - vis_h, cy - vis_h / 2))
        crop = image.crop((left, top, left + vis_w, top + vis_h))
        canvas = crop.resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)

    pixels = canvas.load()
    out = Text()
    for row in range(height):
        for col in range(width):
            top = pixels[col, row * SAMPLES_PER_CELL]
            bottom = pixels[col, row * SAMPLES_PER_CELL + 1]
            out.append(
                "▀",
                style=(f"#{top[0]:02x}{top[1]:02x}{top[2]:02x} on "
                       f"#{bottom[0]:02x}{bottom[1]:02x}{bottom[2]:02x}"),
            )
        if row < height - 1:
            out.append("\n")
    return out


def zoom_label(zoom: float) -> str:
    return f"{zoom * 100:.0f}%"
