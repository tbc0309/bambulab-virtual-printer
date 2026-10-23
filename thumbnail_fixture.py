"""Synthetic ZIP fixtures for bounded thumbnail consumers; no printer data."""
import base64
import io
import zipfile
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOuoAAAAASUVORK5CYII=')
class StreamingBuffer(io.BytesIO):
    """Force ZIP data descriptors, as used by streaming ZIP writers."""
    def seek(self, *args):
        raise io.UnsupportedOperation('streaming output')

def task_archive(plate=1, compressed=False, late=False, descriptor=False):
    if not isinstance(plate,int) or not 1<=plate<=256:
        raise ValueError('plate must be between 1 and 256')
    out=StreamingBuffer() if descriptor else io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        def image():
            z.writestr(f'Metadata/plate_{plate}_small.png',PNG,compress_type=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED)
        if not late:image()
        z.writestr(f'Metadata/plate_{plate}.gcode',b'; synthetic data\n'*100000)
        if late:image()
    return out.getvalue()
