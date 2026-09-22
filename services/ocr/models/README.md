# 模型文件说明

本目录随镜像交付的 MNN 模型与字典文件。

## 来源与格式

| 项 | 说明 |
|---|---|
| 原始模型 | PaddleOCR 的 PP-OCRv5 mobile、PP-OCRv6 tiny/small 推理模型 |
| 原始仓库 | `PaddlePaddle/PP-OCRv5_mobile_*`、`PaddlePaddle/PP-OCRv6_tiny_*`、`PaddlePaddle/PP-OCRv6_small_*` |
| 转换链路 | Paddle 推理模型 → `paddle2onnx` → `mnnconvert`（MNN 官方转换工具） |
| 推理后端 | MNN，CPU，经 `ocr-rs` 封装 |
| 许可 | 上游为 Apache-2.0；本目录的文件沿用上游许可，版权归 PaddlePaddle 所有 |

## 文件清单

| 文件 | 用途 |
|---|---|
| `PP-OCRv5_mobile_det.mnn` / `PP-OCRv5_mobile_rec.mnn` | v5 检测 / 识别 |
| `PP-OCRv6_tiny_det.mnn` / `PP-OCRv6_tiny_rec.mnn` | v6tiny 检测 / 识别 |
| `PP-OCRv6_small_det.mnn` / `PP-OCRv6_small_rec.mnn` | v6small 检测 / 识别 |
| `ppocr_keys_v*.txt` | 对应识别模型的字符字典 |

## 不在目录中的模型

`PP-OCRv6_medium_det.mnn` 与 `PP-OCRv6_medium_rec.mnn` 已从仓库移除。
这两个转换产物在 MNN 中加载失败：

```
MnnError(ModelLoadFailed("Engine creation failed"))
```

该失败在同一批模型文件的原始工具链中同样可复现，属于转换产物问题。
保留它们只会让 `/ocr?model=v6medium` 返回 500，因此不交付。
原因同时记录在 `registry/ocr.yaml` 的 `excluded` 段。

## 如何重新生成

```bash
# 原始 Paddle 推理模型 -> ONNX
paddle2onnx --model_dir ./ppocrv6_small_paddle \
            --model_filename inference.json \
            --params_filename inference.pdiparams \
            --save_file ./ppocrv6_small.onnx

# ONNX -> MNN
mnnconvert -f ONNX --modelFile ./ppocrv6_small.onnx --MNNModel ./PP-OCRv6_small_det.mnn --bizCode MNN
```

字符字典直接取自上游模型仓库的 `ppocr_keys_v*.txt`。

转换完成后必须做两件事，顺序不可颠倒：

1. 把新文件放入本目录，并在 `src/config.rs` 的 `MODEL_TIERS` 中登记。
2. 执行 `make fixtures` 生成新的契约测试基线，人工确认识别结果合理后再提交。
