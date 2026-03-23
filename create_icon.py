"""Generate a simple .ico file for the launcher — purple lightning bolt on black."""
import struct
import zlib

def create_ico():
    size = 64
    pixels = []

    # Simple lightning bolt shape on black background
    bolt = set()
    # Lightning bolt coordinates (rough shape)
    lines = [
        (28, 4, 38, 4), (26, 8, 36, 8), (24, 12, 34, 12), (22, 16, 32, 16),
        (20, 20, 40, 20), (18, 24, 38, 24),  # top wide part
        (30, 24, 38, 24), (28, 28, 36, 28), (26, 32, 34, 32),
        (24, 36, 32, 36), (22, 40, 30, 40), (20, 44, 28, 44),
        (18, 48, 26, 48), (16, 52, 24, 52), (14, 56, 22, 56),
        (24, 20, 44, 20), (26, 24, 42, 24),  # extra width at middle
    ]

    for x1, y1, x2, y2 in lines:
        for x in range(x1, x2 + 1):
            for dy in range(-1, 2):
                bolt.add((x, y1 + dy))
                bolt.add((x, y2 + dy))
            bolt.add((x, y1))

    # Fill bolt area more solidly
    for y in range(4, 58):
        row_pixels = [x for x in range(64) if (x, y) in bolt]
        if row_pixels:
            min_x, max_x = min(row_pixels), max(row_pixels)
            for x in range(min_x, max_x + 1):
                bolt.add((x, y))

    # Generate BGRA pixel data
    for y in range(size):
        for x in range(size):
            if (x, y) in bolt:
                # Purple neon: #a855f7
                pixels.extend([0xf7, 0x55, 0xa8, 0xff])  # BGRA
            else:
                # Black
                pixels.extend([0x0a, 0x0a, 0x0a, 0xff])  # BGRA

    pixel_data = bytes(pixels)

    # BMP header for ICO (BITMAPINFOHEADER)
    bmp_header = struct.pack('<IiiHHIIiiII',
        40,          # header size
        size,        # width
        size * 2,    # height (doubled for ICO format)
        1,           # planes
        32,          # bits per pixel
        0,           # compression
        len(pixel_data),  # image size
        0, 0,        # ppm
        0, 0,        # colors
    )

    # Flip rows (BMP is bottom-up)
    row_size = size * 4
    flipped = b''
    for y in range(size - 1, -1, -1):
        flipped += pixel_data[y * row_size:(y + 1) * row_size]

    # AND mask (all zeros = fully opaque)
    and_mask = bytes(size * (size // 8))

    image_data = bmp_header + flipped + and_mask

    # ICO file header
    ico_header = struct.pack('<HHH', 0, 1, 1)  # reserved, type=ICO, count=1

    # ICO directory entry
    data_offset = 6 + 16  # header + 1 entry
    ico_entry = struct.pack('<BBBBHHII',
        size if size < 256 else 0,  # width
        size if size < 256 else 0,  # height
        0,           # color palette
        0,           # reserved
        1,           # planes
        32,          # bits per pixel
        len(image_data),  # size
        data_offset, # offset
    )

    with open('icon.ico', 'wb') as f:
        f.write(ico_header + ico_entry + image_data)

    print("icon.ico created")

if __name__ == '__main__':
    create_ico()
