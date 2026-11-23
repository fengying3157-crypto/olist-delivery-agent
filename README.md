# Olist 履约诊断 Agent

用 DuckDB 查询 Olist 订单、客户和评价数据，通过 Streamlit 对话页面分析电商配送履约。可以查询整体晚到率、州级排行和订单评分，也可以对比两个月份，追踪晚到订单变化最大的州。

对比问题缺少月份时，应用会先追问，收到月份后继续原来的查询。回答后会显示可点击的后续问题。

## 准备数据

从 Kaggle 下载并解压 Olist 提供的巴西电商公开数据集。本项目读取以下三个文件。 [Kaggle 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)

| CSV 文件 | 用途 |
| --- | --- |
| `olist_orders_dataset.csv` | 下单、送达、预计送达和订单状态 |
| `olist_customers_dataset.csv` | 将订单关联到收货州 |
| `olist_order_reviews_dataset.csv` | 计算订单评分和分组低分率 |

默认目录是 `E:\data\olist\raw`。数据放在其他目录时，在启动和测试所用的 PowerShell 会话里设置这三个环境变量。把 `$dataDir` 改成自己的解压目录。

```powershell
$dataDir = 'D:\data\olist\raw'
$env:OLIST_ORDERS_CSV = Join-Path $dataDir 'olist_orders_dataset.csv'
$env:OLIST_CUSTOMERS_CSV = Join-Path $dataDir 'olist_customers_dataset.csv'
$env:OLIST_REVIEWS_CSV = Join-Path $dataDir 'olist_order_reviews_dataset.csv'
```

## 安装和启动

以下命令在仓库根目录的 Windows PowerShell 中运行。项目使用 Python 3.12 验证过。首次使用时创建 `.venv`，已有环境可以直接安装依赖。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

下面的命令以本地模式启动应用。打开终端显示的网页地址，本例使用 `http://127.0.0.1:8519`。 如果端口已被占用，可以把 `--server.port 8519` 改为其他空闲端口，例如 `--server.port 8520`。

```powershell
Remove-Item Env:NUAA_AI_API_KEY -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m streamlit run app.py --server.address 127.0.0.1 --server.port 8519 --browser.gatherUsageStats false
```

本地模式用规则识别问题，统计由 DuckDB 执行。需要启用可选的模型意图识别时，在启动应用的环境中配置 `NUAA_AI_API_KEY`，并根据所用服务设置 `NUAA_AI_BASE_URL` 和 `NUAA_AI_MODEL_ID`。密钥保存在本地环境变量中。

## 示例对话

### 追问月份后定位晚到增量最大的州

> **用户**　最近哪个州晚到增加最多？  
> **Agent**　请提供要对比的两个月份，并说明哪个是基准月。  
> **用户**　以 2017 年 7 月为基准，对比 8 月。  
> **Agent**　总体晚到净增 14 单。RS 州从 6 单增至 22 单，增加 16 单。RS 在 8 月的晚到组低分率为 9/22（40.91%），按时组为 22/273（8.06%）。

### 解释同日签收的判定口径

> **用户**　预计送达日期和实际签收日期是同一天，只是实际签收时间更晚，算不算晚到？  
> **Agent**　不算晚到。判定按日期进行，实际送达日期晚于预计送达日期才算晚到。当天晚上签收也算按时。

统计按下单月份分组。晚到率的分母限定为已送达且预计、实际送达日期均存在的订单。评价先按订单聚合，订单平均评分不高于 2 分计为低分，低分率的分母是有评价的订单数。

## 运行回归测试

另开一个 PowerShell 窗口并进入仓库根目录。如果使用自定义数据目录，在该窗口重新设置 CSV 环境变量。

```powershell
.\.venv\Scripts\python.exe -m pip install pytest==9.2.0
.\.venv\Scripts\python.exe -X utf8 -m pytest test_delivery_agent.py -q
```

测试用真实 CSV 核对统计值，用模拟模型响应验证意图识别和工具调用。覆盖缺少月份、超出数据范围、多轮追问和不同口径问法。基准结果为 `30 passed`。

