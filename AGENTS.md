# agent-wm

SWE-agent / SWE-smith 的研究实验台：跑 agent 评测、用 SWE-smith 合成 bug 与训练数据。

## 规范要求

以下为强制约束，编号递增，后续新增规范追加到本节末尾。

### 规范 1：禁止私自进行 git 操作

**任何会改变仓库状态的 git 命令，必须先向用户明确说明并获得逐次授权，不得自行执行。**

禁止未经授权执行的操作包括但不限于：

- `git add` / `git rm` / `git mv`（改变暂存区）
- `git commit` / `git commit --amend`
- `git push` / `git pull` / `git fetch`
- `git checkout` / `git switch` / `git restore`（会覆盖工作区改动）
- `git reset` / `git revert` / `git clean`
- `git branch -D` / `git rebase` / `git merge` / `git stash`
- `git tag`、`git config`、修改 `.gitignore` 或 `.git/`

无需授权、可自由执行的仅限只读命令：

- `git status` / `git log` / `git diff` / `git show`
- `git ls-files` / `git branch --list` / `git remote -v`

执行流程：

1. 先说明要执行的完整命令、影响范围、是否可逆。
2. 等待用户明确同意。
3. 执行后如实汇报结果，不隐瞒错误或跳过的文件。

即使用户此前授权过同类操作，也不构成对后续操作的默认授权 —— 每次都要问。批量操作（如 `git add -A`）尤其必须先确认，因为可能把 `.venv`、大文件等意外纳入。

## 仓库布局

| 路径 | 说明 |
| --- | --- |
| `SWE-agent/` | agent 框架本体（SWE-agent 1.1.0 + SWE-ReX 1.4.0），CLI 入口 `sweagent` |
| `SWE-smith/` | bug 合成与训练数据生成，`scripts/` 下是入口脚本，`configs/` 下是配置 |
| `.venv/` | Python 3.12.3 虚拟环境，**已被 .gitignore 排除，不提交** |
| `skills/` | 可复用的操作手册，每个 skill 一个目录（见 `skills/_template/SKILL.md`） |

两个子项目已作为普通目录纳入本仓库跟踪（它们原本各自带 `.git`，内层 `.git` 已移出，工作区文件未动）。

## 环境约定

**始终使用仓库根的 `.venv`，不要新建环境，也不要用系统 python。**

```bash
source .venv/bin/activate      # 激活
# 或者直接用绝对路径，免激活
.venv/bin/sweagent --help
```

- 已安装：`sweagent`、`swerex-remote`
- **没有安装 `uv`**。因此 `SWE-smith/AGENTS.md` 里写的 `uv run ...` 当前跑不通，需要换成 `.venv/bin/python` 或先激活 venv。

## 常用命令

SWE-agent 是子命令结构，参数以 `--help` 为准，不要凭记忆写 flag：

```bash
.venv/bin/sweagent --help                  # 总览
.venv/bin/sweagent run --help              # 单个 issue / problem statement
.venv/bin/sweagent run-batch --help        # 批量跑 benchmark
.venv/bin/sweagent inspect <trajectory>    # 终端查看轨迹
.venv/bin/sweagent inspector               # Web 查看轨迹
```

- 配置集中在 `SWE-agent/config/`：通用在 `default.yaml`，纯 bash 在 `bash_only.yaml`，benchmark 在 `config/benchmarks/`。
- 轨迹默认输出到 `SWE-agent/trajectories/`。
- SWE-smith 侧入口是 `SWE-smith/scripts/`（如 `bug_gen_modal.py`、`train.*.sh`），配置在 `SWE-smith/configs/`。

## Docker 与 GPU

- 宿主机 5 张 RTX 4090，`nvidia` runtime 已注册，容器内可用：`docker run --rm --gpus all <image>`
- **当前 shell 直接敲 `docker` 会 permission denied**：kei 已在 `docker` 组，但本次登录早于加组时间，附加组没刷新。用 `sg docker -c '...'` 或 `newgrp docker`，重登录可永久解决（也可 `sudo docker`）。
- **dockerd 本身没有代理，但已配好**：daemon 与 containerd 均通过 `/etc/systemd/system/{docker,containerd}.service.d/http-proxy.conf` 注入 `HTTP_PROXY=http://127.0.0.1:17894`，`docker pull` 正常（已实测 `hello-world`）。
  - 若 17894 端口的代理进程没了，拉取会重新失败，报错是 `registry-1.docker.io ... connection reset by peer` —— 这时先检查该端口是否还在监听，别去改 docker 配置。
  - daemon 代理不影响容器内部网络；容器内要上网需单独传 `-e HTTP_PROXY=...`。
- 本地已有镜像：`openvla:dev`、`openvla-oft:dev`、`maniflow-policy:latest`、`flowcorrect:env-ready` 等。

## 工作约定

- 改动只落在 `SWE-agent/` 和 `SWE-smith/` 的工作区文件上，不要恢复它们被移出的内层 `.git`。
- 不要提交 `.venv/`、`__pycache__/`、`*.pyc`（已在 `.gitignore`）。
- 跑长任务前先确认输出目录，避免把中间产物写进仓库根目录。
- 新增可复用操作时，在 `skills/` 下按 `_template/SKILL.md` 的格式新建一份。
