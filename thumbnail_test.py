import io
import unittest
import zipfile
from thumbnail_fixture import PNG,task_archive
class ThumbnailFixtures(unittest.TestCase):
    def test_exact_plate(self):
        for plate in (1,2,8):
            for compressed in (False,True):
                with zipfile.ZipFile(io.BytesIO(task_archive(plate,compressed))) as z:
                    item=z.getinfo(f'Metadata/plate_{plate}_small.png')
                    self.assertLess(item.header_offset+item.compress_size+100,1024*1024)
                    self.assertEqual(z.read(item),PNG)
                    self.assertNotIn(f'Metadata/plate_{plate+1}_small.png',z.namelist())
    def test_late(self):
        with zipfile.ZipFile(io.BytesIO(task_archive(late=True))) as z:
            self.assertGreater(z.getinfo('Metadata/plate_1_small.png').header_offset,1024*1024)
    def test_data_descriptor(self):
        for plate in (1,2):
            for compressed in (False,True):
                with zipfile.ZipFile(io.BytesIO(task_archive(plate,compressed,descriptor=True))) as z:
                    item=z.getinfo(f'Metadata/plate_{plate}_small.png')
                    self.assertTrue(item.flag_bits & 8)
                    self.assertEqual(z.read(item),PNG)
    def test_invalid(self):
        for plate in (0,257,'1'):
            with self.assertRaises(ValueError):task_archive(plate)
if __name__=='__main__':unittest.main()
