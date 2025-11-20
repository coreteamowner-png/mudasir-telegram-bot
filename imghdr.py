# shim imghdr for environments where stdlib imghdr is missing (Py3.13+)
# minimal implementation used by telegram library: provides what(path, h=None)

from typing import Optional

try:
    from PIL import Image
except Exception:
    Image = None

def _by_magic(h: bytes) -> Optional[str]:
    if not h:
        return None
    if h.startswith(b'\xff\xd8'):
        return 'jpeg'
    if h.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if h[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if h.startswith(b'RIFF') and b'WEBP' in h[:12]:
        return 'webp'
    if h.startswith(b'BM'):
        return 'bmp'
    return None

def what(file, h=None):
    try:
        if h:
            return _by_magic(h)
    except Exception:
        pass
    try:
        if Image:
            with Image.open(file) as im:
                fmt = im.format
                return fmt.lower() if fmt else None
    except Exception:
        pass
    try:
        with open(file, 'rb') as f:
            header = f.read(32)
        return _by_magic(header)
    except Exception:
        return None
