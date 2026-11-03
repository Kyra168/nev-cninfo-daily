# NEV 每日巨潮公告入库

为现有 NEV 股票池的 20 家公司，每天北京时间 17:00 在 GitHub Actions 云端抓取巨潮资讯公告，并保存到 SQLite 数据库。

本工程准备完成不等于任务已经启用。只有将整个目录提交到已授权的私有 GitHub 仓库默认分支，并确认 Actions 首次运行成功，定时任务才算部署完成。

## 抓取与保存范围

- 与现有系统一致的 20 家公司，名单见 `precrawler/companies.json`。
- 仅抓取巨潮公告的日期、标题和详情链接；不下载公告 PDF，也不从 PDF 提取财报数值。
- 每天回溯 180 天，分页完整读取，按原系统的 `(code,url)` 主键去重。
- 与原系统的 `companies`、`announcements`、`crawl_state` 表兼容，保留数据库中原有其他数据表。
- 每家公司单独提交；部分公司失败时保留已成功的数据，并让任务明确报错。

## 云端运行

工作流 `.github/workflows/cninfo-daily.yml` 每天 09:00 UTC（北京时间 17:00）触发，周末也运行。GitHub 排队可能造成延迟，不保证整点准时启动。

上传代码后会自动进行一次部署验证；之后也可以在 Actions 中选择 `NEV 每日巨潮公告入库` → `Run workflow` 手动补跑。

过程如下：

1. 首次从 `seed/nev_stock.db.gz` 恢复原数据库的事务一致快照；后续恢复上次保存的云端数据库。
2. 执行 20 家公司的公告抓取，按公司记录成功或失败。
3. 使用 SQLite backup 生成事务一致快照，检查数据库完整性与公司名单，并计算 SHA-256。
4. 每月创建一个 `database-YYYY-MM` Release，每次使用唯一文件名保存 ZIP 快照，不覆盖或删除旧版。
5. 另保留 7 天 Actions 备用下载包。发布失败时任务会报错，不会将失败误认为更新成功。

数据库长期状态保存在私有仓库的 Releases 中，工作流临时运行环境销毁后仍保留。不要仅依赖有保留期限的 Actions artifact 保存唯一一份数据库。

任务只使用 GitHub 自动分配的临时 `GITHUB_TOKEN`，不需要在代码或 Secrets 中填写 GitHub 密码。所需仓库权限为 `contents: write`，用途仅为上传数据库备份到同一私有仓库。

## 下载和查看数据

在仓库 Releases 中打开最近月份的 `NEV 数据库备份`，下载文件名时间最新的 `nev-state-*.zip`。ZIP 内包含：

- `nev_stock.db`：完整数据库快照；
- `last_run.json`：20 家公司的本次成功/失败明细；
- `manifest.json`：快照时间、公司数量与数据库 SHA-256。

电脑关机时，云端任务仍可执行，但不能直接修改关机电脑上的 D 盘文件。开机后需要下载并同步云端数据库，或后续将看板也迁移至云端。不要在本地爬虫或看板正在使用 SQLite WAL 文件时直接用文件覆盖原数据库。

## 本地校验

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/collect_cninfo.py --db data/nev_stock.db --companies precrawler/companies.json --summary data/last_run.json
```

本地最后一条命令会访问巨潮并写入指定 `data` 数据库，不会修改原项目目录的数据库。

## 费用与运行状态

计划使用 GitHub Free 私有仓库内含的运行和存储额度，实际用量需看首次运行结果。不要开启付费超额使用来完成本任务。若 GitHub 提示账户验证或用量限制，请由账号持有人处理。

Actions 绿色表示本次 20 家抓取与数据库保存均成功；红色需要查看运行摘要和 `last_run.json`。新公告若在当日 17:00 后发布，会在下一次运行的滚动窗口中补到。

## 官方文档

- [GitHub 定时触发及排队限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions 费用与免费额度](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub Release 文件存储说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
