"""Cross-language compatibility, not just two independent round trips."""
import ctypes as C
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'linux'))
from protocol import Frame, decode

class NativeFrame(C.Structure):
    _fields_=[('channel',C.c_uint8),('type',C.c_uint8),('epoch',C.c_uint32),
              ('session',C.c_uint32),('sequence',C.c_uint32),('length',C.c_uint16),
              ('payload',C.c_uint8*512)]

@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('cc'), 'requires Linux host C compiler')
class CrossProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='mixos-test-')
        out=Path(cls.temp.name)/'protocol.so'
        subprocess.run(['cc','-std=c11','-shared','-fPIC',str(ROOT/'firmware/esp32s3/main/mix_protocol.c'),'-o',str(out)],check=True)
        cls.lib=C.CDLL(str(out))
        cls.lib.mix_frame_encode.argtypes=[C.POINTER(NativeFrame),C.POINTER(C.c_uint8),C.c_size_t]
        cls.lib.mix_frame_encode.restype=C.c_size_t
        cls.lib.mix_frame_decode.argtypes=[C.POINTER(C.c_uint8),C.c_size_t,C.POINTER(NativeFrame)]
        cls.lib.mix_frame_decode.restype=C.c_bool
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def test_golden_and_random(self):
        rng=random.Random(12345)
        for size in [0,1,4,254,255,256,511,512]+[rng.randrange(513) for _ in range(100)]:
            data=bytes(rng.randrange(256) for _ in range(size))
            py=Frame(1,18,0x12345678,5,0xfffffffe,data)
            f=NativeFrame(1,18,py.epoch,py.session,py.sequence,len(data))
            f.payload[:len(data)]=data
            buf=(C.c_uint8*540)();n=self.lib.mix_frame_encode(C.byref(f),buf,540)
            self.assertEqual(bytes(buf[:n]),py.encode())
            self.assertEqual(decode(bytes(buf[:n-1])),py)
            result=NativeFrame()
            self.assertTrue(self.lib.mix_frame_decode(buf,n-1,C.byref(result)))
            self.assertEqual(bytes(result.payload[:result.length]),data)
    def test_bad_header(self):
        packet=bytearray(Frame(1,18,1,payload=b'hello').encode());packet[3]^=1
        buf=(C.c_uint8*len(packet)).from_buffer_copy(packet)
        self.assertFalse(self.lib.mix_frame_decode(buf,len(packet)-1,C.byref(NativeFrame())))

if __name__=='__main__':unittest.main()
