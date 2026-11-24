# 静态作品集数据与发布

静态作品集使用离线案例和经过审查的真实 AI 验收摘录，供非商业学习与作品展示。Amazon 数据出处、版权标注和许可链接见导出数据。[AWS 数据登记页](https://registry.opendata.aws/amazon-last-mile-challenges/) · [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)

## 同步展示数据

从仓库根目录执行。导出器只依赖 Python 标准库，直接读取 D1 三个案例、D4 公开 AI 证据和公开评估报告。

```powershell
.\.venv\Scripts\python.exe -B -m evals.export_portfolio_site
.\.venv\Scripts\python.exe -B -m evals.export_portfolio_site --check
```

输出为 `docs/assets/data/portfolio.json`。D1 校验器会先核对案例文件指纹、指标与差值。导出保留数值精度、空值和模拟口径，去掉案例中的本地证据路径。AI 内容沿用 D4 已脱敏字段。源文件发生变化后重新生成，并用 `--check` 校验结果。

## 本地预览

从仓库根目录启动静态文件服务，然后打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。页面通过相对路径读取 CSS、脚本及 JSON。

```powershell
.\.venv\Scripts\python.exe -B -m http.server 8000 --bind 127.0.0.1 --directory docs
```

页面沿用 `lastmile_product_v3.html` 的视觉结构。配送网络图是原型自带的流程示意，案例使用预计算结果，AI 区域展示历史验收摘要和可展开的脱敏证据。

## GitHub Pages 发布设置

发布时提交 `docs/index.html`、`docs/assets/`、`docs/.nojekyll`、导出脚本和公开证据源 `evals/portfolio_evidence.json`。仓库的 **Settings → Pages → Build and deployment** 选择 **Deploy from a branch**，分支选 `main`、目录选 `/docs`。预期项目站点位于 `https://studing-endless.github.io/ai-last-mile-delivery-assistant/`，本地验收使用了对应的仓库子路径。[GitHub Pages 官方发布说明](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
