"""Генератор иконок приложения «Код Мастер»."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def make_icon(size: int = 1024) -> Image.Image:
    """Создаёт квадратную PNG-иконку с логотипом."""
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bg)

    # Тёмный фон со скруглёнными углами (радиус ~22% для macOS)
    radius = int(size * 0.22)
    draw.rounded_rectangle(
        (0, 0, size, size),
        radius=radius,
        fill=(30, 30, 34, 255),
        outline=(60, 60, 70, 255),
        width=size // 256 or 1,
    )

    # Шрифт
    top_font = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", size // 6, index=1)
    bottom_font = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", size // 9, index=1)

    white = (240, 240, 245, 255)
    orange = (255, 102, 0, 255)

    def letter_widths(text: str, font: ImageFont.FreeTypeFont):
        return [font.getlength(ch) for ch in text]

    # Верхний текст: К О Д
    top_text = "КОД"
    top_ws = letter_widths(top_text, top_font)
    top_total = sum(top_ws)
    top_h = top_font.getbbox(top_text)[3]
    x = (size - top_total) // 2
    y_top = int(size * 0.28)
    for ch, w in zip(top_text, top_ws, strict=True):
        color = orange if ch == "О" else white
        draw.text((x, y_top), ch, font=top_font, fill=color)
        x += w

    # Нижний текст: МАСТЕР
    bottom_text = "МАСТЕР"
    bottom_ws = letter_widths(bottom_text, bottom_font)
    bottom_total = sum(bottom_ws)
    x = (size - bottom_total) // 2
    y_bottom = y_top + int(top_h * 1.15)
    for ch, w in zip(bottom_text, bottom_ws, strict=True):
        draw.text((x, y_bottom), ch, font=bottom_font, fill=white)
        x += w

    return bg


def make_icns(png_1024: Path, out: Path) -> None:
    """Создаёт .icns из 1024 PNG через iconutil."""
    iconset = out.parent / f"{out.stem}.iconset"
    iconset.mkdir(exist_ok=True)
    sizes = [16, 32, 128, 256, 512]
    src = Image.open(png_1024)
    for s in sizes:
        for scale in (1, 2):
            px = s * scale
            im = src.resize((px, px), Image.Resampling.LANCZOS)
            name = f"icon_{s}x{s}.png" if scale == 1 else f"icon_{s}x{s}@2x.png"
            im.save(iconset / name, "PNG")
    import subprocess
    subprocess.run(["iconutil", "-c", "icns", str(iconset)], check=True)
    # cleanup
    import shutil
    shutil.rmtree(iconset)


def make_ico(png_1024: Path, out: Path) -> None:
    """Создаёт .ico с набором размеров."""
    sizes = [16, 32, 48, 64, 128, 256]
    src = Image.open(png_1024)
    ims = [src.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
    ims[0].save(out, format="ICO", sizes=[(im.width, im.height) for im in ims], append_images=ims[1:])


def main() -> None:
    assets = Path(__file__).resolve().parent
    icon_1024 = assets / "icon.png"
    icon = make_icon(1024)
    icon.save(icon_1024, "PNG")
    print("Saved", icon_1024)

    make_ico(icon_1024, assets / "icon.ico")
    print("Saved", assets / "icon.ico")

    make_icns(icon_1024, assets / "icon.icns")
    print("Saved", assets / "icon.icns")


if __name__ == "__main__":
    main()
