"""Int8-quantize the ONNX titler encoders/decoders for low-RAM CPU inference."""
import sys, os
from optimum.onnxruntime import ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig

src = sys.argv[1]
qc = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
for f in os.listdir(src):
    if f.endswith(".onnx"):
        q = ORTQuantizer.from_pretrained(src, file_name=f)
        q.quantize(save_dir=src, quantization_config=qc, file_suffix="q")
        print("quantized", f)
