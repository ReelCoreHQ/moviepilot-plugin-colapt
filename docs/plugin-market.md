# 上架 MoviePilot 官方插件市场（流程备查）

## 机制

MoviePilot v3 不再把插件仓库清单写死在代码里，而是启动时同步官方 Wiki：

```
https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md
```

它只解析下面两个标记**之间**的每一行 `- https://github.com/owner/repo`：

```markdown
<!-- plugin-market-repos:start -->
- https://github.com/jxxghp/MoviePilot-Plugins
...
<!-- plugin-market-repos:end -->
```

同步到的清单会与本地 `PLUGIN_MARKET` 合并去重（本地顺序优先，Wiki 新增的追加在后）。设置里还有「同步 Wiki」按钮可手动触发。

## 收录步骤

1. fork `jxxghp/MoviePilot-Wiki`；
2. 编辑 `plugin.md`，在 `<!-- plugin-market-repos:end -->` 上一行追加：
   ```
   - https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
   ```
3. 提交 → Create pull request。

> 2026-11-02 已提：https://github.com/jxxghp/MoviePilot-Wiki/pull/46（fork 自 `xiaozhu5399/MoviePilot-Wiki`，分支 `add-colacrossseed-plugin`）。

## 仓库侧要求（已满足）

| 要求 | 我们的情况 |
|---|---|
| 公开仓库、`main` 分支 | ✅ |
| 根目录 `package.v2.json`（`version` / `history` / `v2: true`） | ✅ |
| 插件源码 `plugins.v2/<id小写>/__init__.py` | ✅ `plugins.v2/colacrossseed/` |
| 图标可访问 | ✅ 指向 raw.githubusercontent 的 png |
| 使用说明入口 | ✅ `plugin_readme` 指向站内 Wiki；另放 `plugins.v2/colacrossseed/README.md` 兜底 |

## 收录之前的过渡

用户在 MP 里手动加仓库即可：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

## 发版检查清单

1. 改 `plugins.v2/colacrossseed/__init__.py` 的 `plugin_version`；
2. 改 `package.v2.json` 的 `version` 与 `history`（最新在前）；
3. `python3 -m py_compile plugins.v2/colacrossseed/__init__.py`；
4. `git tag -a vX.Y.Z && git push origin main --tags`；
5. 站内 Wiki 若涉及配置变化，同步更新 `docs/wiki-crossseed.md` 与线上页面。
