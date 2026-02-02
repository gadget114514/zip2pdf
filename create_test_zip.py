import zipfile
import os
import base64

os.makedirs("d:\\ws\\zip2pdf\\test_gen", exist_ok=True)
zpath = "d:\\ws\\zip2pdf\\test_gen\\test.zip"

# Small 1x1 png
one_pixel_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

with zipfile.ZipFile(zpath, 'w') as zf:
    zf.writestr("page1.html", "<html><body><h1>Page 1</h1><p>Content 1</p><img src='img1.png'></body></html>")
    zf.writestr("page2.html", "<html><body><h1>Page 2</h1><p>Content 2</p><img src='img1.png'><img src='img2.jpg'></body></html>")
    zf.writestr("style.css", "body { color: red; background-image: url('img3.png'); }")
    zf.writestr("img1.png", one_pixel_png)
    zf.writestr("img2.jpg", one_pixel_png)
    zf.writestr("img3.png", one_pixel_png)

print(f"Created {zpath}")
