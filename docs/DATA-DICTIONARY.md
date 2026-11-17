# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 园区总分对账（M8，park.csv）

### 10.1 园区契约列

| 列 | 含义 | 约束 |
|---|---|---|
| `meter_id` | 表号 | 必须同时存在于 meters.csv，否则 `Q_PARK_TENANT_UNKNOWN` |
| `meter_role` | 角色 | `master`/`总表`（全园区唯一，重复报 `Q_PARK_MASTER_DUPLICATE`）或 `tenant`/`分表`/`租户`；无法识别报 `Q_PARK_ROLE_UNKNOWN` |
| `activated_at` | 分表投运时点 | 可空；带偏移，归一 UTC。空表示无投运起点 |

`park.csv` 缺失，或读数未覆盖任何园区表时，M8 阶段标记 `skipped`，其余七模块行为不变。

### 10.2 月度守恒式与裁决

每月（月桶 `[月初, 下月初)`，上海时区）：

```
园区总表用量 = 租户可证实用量合计 + 待查缺口
```

- **可证实用量**：仅由分表自身有效示数还原、且当月具备示数证据的用量。
- **待查缺口（gap_kwh）**：总表减去可证实的差值，单列展示，**永不摊进任何租户账单**，`gap_amount_yuan` 恒为 `0.00`。
- 月状态：`balanced` 守恒无缺口 / `gap` 有待查缺口 / `abnormal` 分表之和超过总表（不做负缺口抹平，`conservation_ok=false`）/ `unreconciled` 总表缺失无法对账。

### 10.3 漏抄与月中投运

| 情形 | 判定证据 | 处理 |
|---|---|---|
| 分表漏抄 | 一条区间跨越 ≥2 个月桶，被穿过的月份（末月补抄除外）当月无数码闭合 | 标记 `K_TENANT_MISSED_READ`；该月可证实用量与费用都为 0，不出账单；日权重摊入量保留在 `allocated_kwh` 供追溯 |
| 月中投运 | 契约投运时点/首读落在月中，或区间对月桶覆盖率 < 0.999 | 标记 `K_TENANT_PARTIAL_MONTH`；投运前月份为「尚未投运」，零费用且不算漏抄 |
| 缺口归因 | 有漏抄 → `K_GAP_MISSED_READ`；有投运 → `K_GAP_PARTIAL_MONTH`；都解释不了 → `K_GAP_UNVERIFIED` | 归因码随月度行与 CSV 导出 |

### 10.4 归档（park_archives）

- 归档号 `par-<批次>-<月份>-vNN`，版本号在同批次同月内从 1 递增。
- 批示数 `archive_key = 批次 + 月份 + 结果摘要 sha256`；同一批示数重复提交命中既有版本，不新增电量、费用或版本。
- 文件先写 `*.tmp` 再 `os.replace` 原子发布；中断只可能留下未发布临时文件，上一版正式文件与库记录始终完整。
- 快照冻结当月总表/分表/缺口/费用与输入摘要；后续重算产生新版本，历史版本不被覆盖（原始示数覆盖后旧版仍可回溯）。
- `unreconciled` 月份禁止归档，抛 `E_ARCHIVE_STATE`。
