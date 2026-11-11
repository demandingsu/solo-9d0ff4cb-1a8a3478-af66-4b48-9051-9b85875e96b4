# 报纸扫描验收台

报纸数字化验收辅助工具。输入某一期的扫描目录 URL，程序沿**同站分页**读完目录，抓取所有扫描详情，按页核对：

- **漏扫 / 待补扫**：该页没有任何有效扫描覆盖。
- **重扫 / 冲突**：同一页被两个不同扫描详情覆盖。
- **待核实**：任一详情（或目录分页）抓取失败，整期不能判为合格。
- **合格**：每一页都恰好被一个有效扫描覆盖，且没有抓取失败。

技术栈：FastAPI + BeautifulSoup + Jinja2 服务端模板。项目自带一个可断网运行的分页样本站点。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

打开 <http://localhost:8000>，直接用页面上的三个演示按钮验收样本期。样本站也通过网页同源的 `/sample/` 路径提供，因此目录、详情和图片链接在离线环境中都能正常打开。

Compose 启动两个服务：

| 服务 | 作用 | 访问方式 |
| --- | --- | --- |
| `web` | 验收网页（FastAPI） | `http://localhost:8000` |
| `sample-site` | 本地分页样本报纸站 | 经 `web` 的 `/sample/` 反向代理访问；也可直接浏览 `http://localhost:8001` |

`web` 通过 Compose 内部网络 `http://sample-site:8000` 抓取样本站；用户看到的链接则保持在网页同源路径 `/sample/...` 下。直接在验收页输入 `http://localhost:8001/...` 不可行（容器内的 `localhost` 不等于宿主机），请使用页面上的演示按钮或 `/sample/...` 路径。

停止：

```bash
docker compose down
```

## 使用方式

1. 在首页输入该期目录 URL，例如：
   - 站内路径：`/sample/issues/clean/page/1.html`
   - 完整地址：`http://localhost:8000/sample/issues/clean/page/1.html`
   - 任意可访问的外部目录：`https://example.com/issues/2024-05-01/`
2. 程序读取目录第一页，解析期号、版次、总页数。
3. 沿同站分页链接继续读取，收集所有扫描详情链接（去重）。
4. 并发抓取每个详情，解析所属期版、页码（单页或 `2-3` 这类跨页）和图片链接。
5. 按规则生成逐页判定、扫描来源、未采用扫描和抓取失败清单。

## 判定规则

### 有效扫描

一个详情只有同时满足下列条件，才能覆盖页码：

1. 详情声明的**期号与版次**都和目录一致；
2. 详情包含**图片链接**；
3. 能从详情解析出至少一个落在 `1..总页数` 范围内的页码。

期版比较会做适度归一化（全角/半角、空白、`第3版`/`3版`/`第三版`、常见日期写法），避免排版差异造成误判。

### 页面判定

| 页状态 | 含义 |
| --- | --- |
| 已覆盖 | 恰好一个有效扫描覆盖该页 |
| 重扫冲突 | 多个**不同详情**覆盖同一页（跨页扫描与单页扫描重叠也算） |
| 待补扫 | 无有效扫描覆盖该页 |

同一详情即使重复出现在多个目录分页中，也只按一个扫描计算。

### 整期状态

- **待核实**：任一详情请求出现网络错误、超时或 HTTP ≥ 400；任一目录分页抓取失败也计入。此时即使页面看起来已全部覆盖，也**不能判为合格**。
- **不合格**：没有抓取失败，但存在重扫冲突或待补扫页。
- **合格**：没有抓取失败，且每页都只有一个有效扫描。

页面会逐页列出该页的扫描来源（扫描编号、声明页码、所属期版、详情页链接、图片链接），并单独列出未采用扫描、抓取失败与读取过的目录分页。

## 样本站点与预期结果

样本站位于 `sample_site/`，每页目录列出 3 条详情，用来演示同站分页：

| 演示期 | 目录入口 | 预期结果 | 说明 |
| --- | --- | --- | --- |
| 示例 A | `/sample/issues/clean/page/1.html` | 合格 | 6 页全部由唯一有效扫描覆盖 |
| 示例 B | `/sample/issues/conflict/page/1.html` | 不合格 | 第 3 页重扫；第 4、5 页因期号不符 / 缺图未被采用 |
| 示例 C | `/sample/issues/pending/page/1.html` | 待核实 | 一个详情页 404；另有第 4 页重扫、第 5/9/11/12 页待补扫 |

样本站路由：

```text
/                                     期目录
/issues/{slug}/page/{n}.html          分页目录（详情链接 + 上一页/下一页）
/scan/{slug}/{scan_id}.html           扫描详情（所属期版、页码、图片）
/img/{slug}-{scan_id}.svg             占位扫描图
```

## 本地开发（不使用 Docker）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 终端 1：样本站
uvicorn sample_site.main:app --port 8001

# 终端 2：验收网页（把 /sample 代理到样本站）
SAMPLE_SITE_BASE_URL=http://127.0.0.1:8001 uvicorn app.main:app --port 8000
```

打开 <http://localhost:8000>。也可以不配置代理，直接把 `http://127.0.0.1:8001/issues/clean/page/1.html` 作为完整 URL 输入。

## 测试

```bash
pip install pytest pytest-asyncio
pytest
```

测试使用 `httpx.ASGITransport` 直接驱动样本站，覆盖：

- 分页目录的完整读取；
- 跨页扫描（`2-3`）对两页的覆盖；
- 重扫冲突识别；
- 期号 / 版次不符、缺图时的忽略；
- 详情 404 将整期升级为“待核实”（即使页面覆盖情况仍可展示）；
- 页码与期版归一化。

## 主要文件

```text
app/
  main.py                  FastAPI 路由、表单、同源样本站代理
  scraper.py               目录分页、详情解析、归一化、覆盖判定
  templates/               服务端页面（首页 / 结果 / 错误）
  static/style.css
sample_site/
  main.py                  样本站 HTML、分页、详情、占位图
  data.py                  三个演示期的数据
tests/test_scraper.py
Dockerfile
docker-compose.yml
```

## 已知边界

- 目录与详情解析针对常见的语义化 HTML（`dt/dd`、表格、meta、class 命名）做了多种回退；若目标站点用纯 JavaScript 渲染，需要改为接口抓取或先渲染 HTML。
- 只检查目录中列出的详情，不递归发现目录之外的扫描件。
- 演示站点的图片是占位 SVG，不包含真实扫描图像。
