# 闲鱼同城助手

在 Windows 本机运行的闲鱼商品采集与整理工具。前端为单文件 HTML/CSS/JavaScript，后端使用 Flask、Playwright 和本地 JSON 存储，通过本机 Edge 中的网页搜索、筛选与翻页采集。

## 主要功能

- 按城市、关键词和滚动时间范围采集：最近 **1、2、3、12 小时**及 **1、3、7、15、30 天**；手动与定时任务均支持。
- 商品信息池支持搜索、品类/城市/地区/价格筛选、排序、商品与卖家视图、收藏及 CSV 导出。池内时间筛选目前为“最近24小时上架”。
- 按启发式规则识别个人卖家与疑似商家；人工标记优先于自动评分。
- 品类、城市、地区标签右侧的 **×** 可删除对应入库商品及收藏，操作前确认范围。采集期间需先停止并完成保存；删除不会阻止未来重新采到这些商品。
- 可选壁纸、背景蒙层和内容透明度。内容透明度 0% 为不透明、100% 为完全透出壁纸，文字与图片保持清晰；设置保存在当前浏览器中。

## 安装与运行

需要 **Windows、Python 3.12 或更新版本、已安装的 Microsoft Edge**。安装 Python 时勾选加入 PATH。在项目根目录打开 PowerShell，运行：

```powershell
python -m pip install -r backend/requirements.txt
python backend/app.py
```

浏览器访问 [http://localhost:8000](http://localhost:8000)。也可双击 `start.bat`；启动器检查到 8000 端口已有服务时仅打开网页，不会重启后端。更新后端源码后，应先关闭原服务，再重新启动。

Playwright 直接使用本机 Edge，无需另行执行 `playwright install` 下载浏览器。服务默认仅监听 `127.0.0.1:8000`。

## 登录、采集与停止

首次使用点击“登录闲鱼”，在弹出的工具专属 Edge 窗口中扫码。之后会优先复用有效会话，只有网站要求重新登录时才需要扫码。

- 浏览器配置保存在 `data/browser-profile/`，最新会话快照保存在 `data/browser-session.json`。旧版 `data/session.json` 仅用于兼容迁移并保留原件。
- 商品、收藏、人工标记和设置等运行数据保存在 `data/`。该目录包含登录凭据与个人数据，已由 `.gitignore` 排除，不应上传或分享。
- 正常翻页间隔 5—7 秒，每累计 20 页在下一页前停留 10—13 秒。任务没有固定页数上限，但有 **20 分钟总时限**，包含暂停、登录及验证等待。实际数量取决于网站返回范围、翻页速度与触发的停止条件。
- 出现验证时停止主动搜索与翻页，等待用户在原窗口手动完成，最长 5 分钟；若页面没有自动恢复，可点击已有的“验证完成，继续”。收到当前查询的有效结果后才继续。
- 同一任务再次验证、验证超时或明确限流时，保存已采结果并进入 **30 分钟冷却**。冷却在本机保存，重启不会清除；瞬时网络错误最多重试一次。
- 城市、关键词或排序条件失效时结束任务；连续两页满足已超出时间范围的判定条件时提前结束。停止、超时等提前结束会保存部分结果，并在无法确认取尽时提示“可能不完整”。

采集依赖网站当前页面及账号状态，不能保证完整获取平台上的所有商品，也不能保证不会触发验证。请按照平台规则使用并保持低频；工具不自动破解验证码。

## 可选壁纸

本次公开源码 **不分发壁纸素材**；缺少壁纸时仍可正常运行，其余功能不依赖壁纸。

已有兼容皮肤包时，可导入包含 `skins-index.json` 的 `art/incoming` 目录：

```powershell
python backend/import_skins.py "<皮肤包目录>/art/incoming" "static/skins"
```

导入脚本复制 WebP 图片和缩略图，并生成 `static/skins/skins.json`。导入后在右上角“壁纸”中选择；素材目录默认不纳入 Git。

## 项目结构

```text
index.html                 前端页面
start.bat                  Windows 启动器
backend/app.py             Flask 接口、任务与调度
backend/scraper.py          浏览器会话与商品解析
backend/browser_flow.py     网页采集、验证与边界控制
backend/item_time.py        发布时间与滚动窗口
backend/seller_filter.py    卖家识别规则
backend/storage.py          JSON 存储与恢复
backend/import_skins.py     可选壁纸导入
backend/requirements.txt    Python 依赖
backend/tests/              单元测试与离线浏览器回归
static/skins/               可选本机壁纸（不分发）
data/                      自动生成的本机运行数据（不分发）
```

## 离线回归

在项目根目录执行。测试使用临时数据与假网络响应，不执行真实商品采集。浏览器回归需要已安装 Edge。

```powershell
python -B -m unittest discover -s backend/tests -v
python -B backend/tests/frontend_regression.py
python -B backend/tests/browser_controls_regression.py
python -B backend/tests/sandbox_regression.py
python -B backend/tests/login_regression.py
python -B backend/tests/session_reuse_regression.py
python -B backend/tests/transparency_regression.py
```

测试结果只能覆盖对应场景，真实网站行为仍受页面变化与账号状态影响。

## 开发记录

[REVIEW.md](REVIEW.md) 保留历次评审与处理记录，[VALIDATION.md](VALIDATION.md) 记录验收证据及限制，[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) 记录开发进度与回退点。这些文档包含历史方案与当时的本机验证状态；同一事项以最后更新的条目为准，不能将历史通过记录视为任意新环境的验证结果。
