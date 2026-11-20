# price-gbm：广东现货日前小时电价 GBM 预测

LightGBM 分位回归（q10/q50/q90），预测目标日的 24 个整点日前价（元/MWh）。
与 `forecast`（TimesFM）是互补关系：本服务吃结构化特征（同小时滞后、滚动统计、
锚点日电量、日历、趋势），对锚点后多天的目标日更稳；TimesFM 是通用时序基础
模型。线上用法是两者融合（由 ems 后端取均值）；离线对比见 ems 仓库
tools/hour-price-benchmark 的回测：
生产口径（锚点滞后 5 天）GBM 19.7% / TimesFM 20.6% / 融合 19.4% MAPE（90 天重放）。

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

## 权重与训练

- `models/gbm-hour-v1/`：三个 booster 文本（各 ~2.7 MB），`sha256.json` 为摘要清单，
  启动时逐文件核对（`VERIFY_WEIGHTS`）。
- `training-data/gd-home24h.json`：官网主页 24 点分时完整日快照（公开市场数据），
  也是训练可复现性的输入。
- 重训：`python tools/train.py`（确定性：seed 钉死、单线程、无 bagging）。
  训练口径 H=5（目标日距锚点 5 天，对应官网 T+5 滞后的生产口径）。
- 权重换版本 = 打新 tag 走同一条发布链路（`registry/price-gbm.yaml` 是唯一事实来源）。

## 已知限制

- 节假日表只覆盖 2024-2026；表外目标日的节假日特征恒为 0（代码里有 shortcut 标注）。
- 分位带未做 conformal 校准，经验覆盖率约 46%（名义 80%）；融合侧由 ems 后端
  与 TimesFM 分位取平均后再看。

## 本地跑

```bash
make build SERVICE=price-gbm   # 装 venv
make run   SERVICE=price-gbm    # 127.0.0.1:8080
make test  SERVICE=price-gbm
make smoke SERVICE=price-gbm
```
