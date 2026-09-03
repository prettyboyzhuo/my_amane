# 定制开发与上游同步

本文规定基于 Amane 开发本地功能时的仓库组织、代码边界和上游同步流程。定制功能应保持独立，使上游更新可以通过小范围变基完成。

## 仓库关系

将官方仓库配置为 `upstream`，将包含定制代码的个人仓库配置为 `origin`。`main` 分支只跟踪官方代码，定制开发使用 `custom` 及其短生命周期功能分支。

```text
main                 官方 Amane
custom               本地产品定制
custom/<feature>     单个功能开发
```

个人仓库地址确定后，可以执行：

```powershell
git remote rename origin upstream
git remote add origin <个人仓库地址>
git switch -c custom
git push -u origin custom
```

不要在 `main` 分支直接编写定制业务代码。未配置个人远程仓库时，可以只保留本地 `custom` 分支，不影响上游同步。

## 代码边界

优先使用现有扩展点，只有契约确实不足时才修改核心模块。

| 需求 | 推荐位置 | 约束 |
|------|----------|------|
| 新增刮削网站 | `plugins/sources/<id>/` | 只依赖 `amane.plugin` 契约，不访问数据库、Worker 或 API |
| 刮削批次与文件任务 | 独立 handler、repository 和 API 模块 | 通过任务协议接入现有 Worker，配置使用创建时快照 |
| 软链接目录扫描 | 媒体库扫描适配模块 | 有软链接目录时以其为扫描路径，否则使用源文件夹 |
| NFO、封面和其他资源 | `media/`、`organize/` 的扩展服务 | 下载失败记录结果，不创建伪图片文件；输出冲突必须等待确认 |
| 刮削任务页面 | `web/src/routes/` 与 `web/src/components/` 的独立目录 | 单资源和批量入口复用同一页面与 API |
| 数据库结构 | 独立 Alembic migration | 只新增表或字段，不修改既有 migration 文件 |

定制 API 使用独立路由组，例如 `/api/scrape-runs` 和 `/api/scrape-tasks`。公共模块的返回结构应保持稳定，避免让上游页面依赖定制字段。

## 提交组织

每个提交只包含一个可验证的逻辑变更。推荐的提交边界如下：

```text
custom: add scrape run persistence
custom: add profile snapshot execution
custom: add overwrite confirmation
custom: add link-first library scanner
custom: add reusable scrape run page
```

数据库迁移、后端逻辑、API 合约和前端页面可以分别提交。提交前同步相应的 `docs/dev/` 文档，并在提交说明中指出受影响的文档路径。

## 同步上游

在定制分支之外先更新 `main`：

```powershell
git fetch upstream
git switch main
git pull --ff-only upstream main
git switch custom
git rebase main
```

出现冲突时，只处理当前提交涉及的文件，完成后执行：

```powershell
git add <已解决的文件>
git rebase --continue
```

放弃本次变基使用 `git rebase --abort`。变基完成后先运行检查，再使用 `git push --force-with-lease origin custom` 更新个人远程分支。禁止使用无保护的 `--force` 覆盖其他人的提交。

## 配置与任务兼容

任务创建时必须写入实际使用的配置快照。执行阶段只读取快照，不能根据 `profile_id` 再次读取当前配置；配置删除或修改不能改变已创建任务的行为。

新增任务字段必须提供默认值，以便旧任务仍可被 Worker 反序列化。任务状态、取消标志和批次计数以数据库为准，进程内事件只用于及时唤醒执行器。

## 构建与验证

同步上游或修改定制层后，至少执行：

```powershell
just check
just build
```

涉及数据库时还需验证 migration 和空数据库初始化；涉及任务时需验证进程重启后的恢复、取消、重试和输出冲突确认；涉及前端时需验证单资源与批量入口均进入同一任务页面。

## 更新前检查清单

- `git diff main...custom` 只包含定制需求相关改动。
- 新增站点没有直接依赖 Amane 内部实现。
- 新增表和字段有独立 migration，且不会破坏已有数据。
- 任务执行使用配置快照，输出冲突不会自动覆盖。
- 文档索引和对应主题文档已同步更新。
- `just check` 与 `just build` 均通过。
