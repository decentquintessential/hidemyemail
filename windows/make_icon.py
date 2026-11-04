"""Write the app icon (the same one the window uses) as a multi-size .ico for the .exe and installer."""

import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hide_my_email import app_icon  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon.ico")
out.parent.mkdir(parents=True, exist_ok=True)

app = QGuiApplication(sys.argv[:1])
buffer = QBuffer()
buffer.open(QIODevice.WriteOnly)
app_icon().pixmap(256, 256).save(buffer, "PNG")
png = out.with_suffix(".png")
png.write_bytes(bytes(buffer.data()))
Image.open(png).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {out}")
