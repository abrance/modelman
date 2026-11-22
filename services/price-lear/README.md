# price-lear：广东现货日前小时电价 LEAR 预测

LEAR / LASSO 线性回归（Lago et al. 2021, *Applied Energy* 293:116983），预测目标日的
24 个整点日前价（元/MWh）。与 `price-gbm`（LightGBM）和 `forecast`（TimesFM）互补：
本服务用同一套 22 维结构化特征 + L1 正则线性回归，是电价预测的**强统计基线**。

生产口径（锚点滞后 5 天）90 天 as-of 重放（ems 仓库 tools/hour-price-benchmark，
2026-08-18~11-15）：LEAR MAE 88.7 / MAPE 18.66%，反超 TimesFM（92.7）与 GBM（101.3）
——印证了 Lago et al. 2021 的结论：规范的简单统计模型在电价预测上不输深度学习。

## 契约

`POST /v1/price-forecast`

```json
{
  "days": [
    {"date": "2026-11-09", "values": [/* 24 个日前价 */], "power": [/* 24 个电量，可选 */]}
    // 升序完整日，最后一天即锚点；最多 60 天
  ],
  "target": "2026-11-20"
}
```

响应：`point`（=0.5 分位）、`quantiles`（0.1/0.5/0.9）、`horizon_days`
（target 距锚点天数，上限 14）。特征只用 ≤ 锚点的数据，不泄真实值。

## 模型与训练

- `models/lear-hour-v1/state.json`：predictor 状态（impute_medians / scaler_mean /
  scaler_scale / coef / intercept / offset_q10 / offset_q90），约 2.4 KB；`sha256.json`
  为摘要清单，启动时逐文件核对（`VERIFY_WEIGHTS`）。
- **运行期只用 numpy 做线性推理，不依赖 sklearn**：中位数填充 → 标准化 → 点积 + 截距
  → 加残差偏移，毫秒级（基线单请求约 3.8ms）。
- `training-data/gd-home24h.json`：官网主页 24 点分时完整日快照（公开市场数据），
  也是训练可复现性的输入。
- 重训：`python tools/train.py`（`SimpleImputer(median) + StandardScaler + LassoCV(
  TimeSeriesSplit(3))`，numpy/sklearn 线程数钉死保证确定性）。训练口径 H=5。
- 权重换版本 = 打新 tag 走同一条发布链路（`registry/price-lear.yaml` 是唯一事实来源）。

## 已知限制

- **分位带非模型原生**：LEAR 本体只给点预测，q10/q90 是训练残差的经验分位数作固定
  偏移（与 ems benchmark 同口径）。覆盖率可作参考，但统计含义弱于 GBM/TimesFM 的
  原生分位带，不可直接等同看待。
- 每次预测不需要重新 fit：系数已离线训练并随镜像交付（与 GBM 同款「加载即用」）。
- 节假日表只覆盖 2024-2026；表外目标日的节假日特征恒为 0（与 price-gbm 同源）。

## 本地跑

```bash
make build SERVICE=price-lear   # 装 venv（含测试/训练依赖）
make run   SERVICE=price-lear    # 127.0.0.1:8080
make test  SERVICE=price-lear
make smoke SERVICE=price-lear
```
