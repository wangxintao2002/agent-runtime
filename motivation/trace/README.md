# Agent trace 录制

这套工具保留了三类负载的原生执行链路：**Claude Code → 本地模型录制代理 → DeepSeek；工具在本机实际执行**。录完后可用本地响应回放服务替换 DeepSeek，Claude Code 和工具仍然运行。每个任务使用独立工作目录和 cgroup，不依赖每任务 Docker 容器。

录制成功表示任务正常结束、轨迹与观测完整，**不要求最终答案或补丁正确**。失败尝试也保留，不为得到正确答案重复录制。完整规则见 [COLLECTION-POLICY.md](COLLECTION-POLICY.md)。

## 目录和入口

| 位置 | 用途 |
| --- | --- |
| `common/record.py`、`replay.py` | 模型 HTTP 请求、响应字节及流式时序的录制和回放 |
| `common/audit_replay.py`、`summarize.py` | 独立一致性校验、完成状态与资源摘要 |
| `common/process_observe.py`、`async_observe.py` | 独立进程采样；慢诊断与日志写入不阻塞主要采样循环 |
| `common/enter_cgroup.py` | 先进入任务 cgroup，再启动 Claude Code；后代工具继承归属 |
| `workloads/swe/` | SWE-rebench：准备代码仓库、执行修复任务、保存修改与独立测试证据 |
| `workloads/bigcodebench/` | BigCodeBench Instruct：准备任务、生成代码、执行真实测试工具 |
| `workloads/hotpotqa/` | HotpotQA：准备语料、启动真实检索服务、执行多步问答 |
| `tools/check_environment.py` | 只读检查本机运行条件，不请求模型、不输出密钥 |
| `requirements/` | 从原录制环境提取的依赖版本快照 |

统一入口为 `python motivation/trace/trace.py <命令>`；每个命令支持 `--help`，适配器也可直接运行。以下示例均从仓库根目录执行。

默认生成目录为 `motivation/trace/runs/{swe,bigcodebench,hotpotqa}/`、`motivation/trace/data/` 和 `motivation/trace/cache/`，均已加入 `.gitignore`。可用 `AGENTOS_TRACE_RUNS`、`AGENTOS_TRACE_DATA` 覆盖输出根目录；SWE 的任务、代码和解释器位置由其 JSON spec 决定。一次录制必须用新名字，已有目录不会被覆盖。

## 运行条件

- Linux、cgroup v2、可用的 systemd 用户会话；当前用户需要已委派的 `memory`、`pids` 控制器，以及可读的 CPU 计数和 CPU/内存/I/O PSI。默认父组为 `user@UID.service`，单任务入口可用 `--cgroup-parent` 指定已有父组。
- 驱动脚本使用 Python 3.11+，安装 Claude Code、Git、`taskset`。SWE 的工具 Python 可按任务使用更旧版本，环境准备支持 `uv` 或显式配置的 Conda。
- 录制时在调用 shell 中导出 `DS_API_KEY` 或 `DEEPSEEK_API_KEY`。工具只读取环境变量，不读取 shell 配置文件；只给本地录制代理传真实密钥，Claude Code 使用本地临时令牌。回放不需要 API key。
- 默认延续原录制配置：DeepSeek Anthropic 兼容接口，模型 `deepseek-flash[1m]`。配置在 `common/launch.py`。变更模型、Claude Code 版本或工具环境后，应新建录制，不能冒充原环境复现。

```bash
python motivation/trace/trace.py check
```

基础录制、回放和观测仅使用 Python 标准库。测试还需要 NumPy；数据准备需要 PyArrow。完整负载依赖较多，分别使用环境，勿把所有包安装进系统 Python：

```bash
# Parquet 数据准备环境：与 Agent 的工具依赖分开
uv venv --python 3.13.5 motivation/trace/.venv-prepare
uv pip install --python motivation/trace/.venv-prepare/bin/python \
  -r motivation/trace/requirements/preparation-requirements.txt

# BigCodeBench 完整工具环境：原快照使用 Python 3.11.12
uv venv --python 3.11.12 motivation/trace/.venv-bcb
uv pip install --python motivation/trace/.venv-bcb/bin/python \
  -r motivation/trace/requirements/bigcodebench-full-requirements.lock.txt

# 检索环境：原快照使用 Python 3.13.5
uv venv --python 3.13.5 motivation/trace/.venv-qa
uv pip install --python motivation/trace/.venv-qa/bin/python \
  -r motivation/trace/requirements/retrieval-requirements.lock.txt
```

依赖快照来自已有环境，本次迁移未重新下载、安装全部负载依赖。部分负载另需系统库，任务可用性须通过实际准备和单任务资格检查确认。`bigcodebench-requirements.lock.txt` 是早期小集合环境，不应作为完整任务集依赖。

BigCodeBench 的工具环境快照不包含 PyArrow。准备 parquet 时使用 `.venv-prepare`（或已有的检索环境），录制和回放使用 `.venv-bcb`；不要为数据准备修改已经冻结的工具环境。

## BigCodeBench：准备、录制、自动校验

```bash
motivation/trace/.venv-prepare/bin/python motivation/trace/trace.py bcb-prepare \
  --out motivation/trace/data/bigcodebench-128 --task-limit 128

# 先录 2 个不同任务，每个完整录制随后做一次无 API 的真实工具回放
motivation/trace/.venv-bcb/bin/python motivation/trace/trace.py bcb-collect \
  --name bcb-first-collection --data motivation/trace/data/bigcodebench-128 \
  --task-start 0 --task-limit 2 --record-workers 1
```

准备器固定 BigCodeBench v0.1.4 的数据版本，按固定种子和任务 ID 排序选样，不参考正确性或资源开销。`tasks.json` 给 Agent 提供任务文本；参考代码和测试保留在 `controller-only/`。Agent 在工作目录中写 `solution.py`，通过 `check.py` 调用真实测试工具。此处是合作式本机实验，目录约定和 cgroup 不构成抵御恶意工具的安全沙箱。

`bcb-record --task-id 'BigCodeBench/…' --data … --name …` 可单独录制指定任务。批量采集最多 4 个录制 worker；这是采集并行度，不是后续性能实验的 Agent 准入策略。

## HotpotQA：真实检索与问答

```bash
motivation/trace/.venv-qa/bin/python motivation/trace/trace.py qa-model
motivation/trace/.venv-qa/bin/python motivation/trace/trace.py qa-prepare \
  --name wiki100k-hotpot128 --documents 100000 --questions 128

motivation/trace/.venv-qa/bin/python motivation/trace/trace.py qa-collect \
  --name qa-first-collection --data motivation/trace/data/wiki100k-hotpot128 \
  --task-limit 2 --record-workers 1
```

首次准备会下载约 1.55 GB 的官方 Wikipedia 摘要压缩包、固定版本问题集和嵌入模型，并实际构建 BM25/FAISS 索引。文档取归档顺序的前 10 万篇，与问题和答案独立；问题按固定哈希排序。这是 **HotpotQA + Wikipedia 子集实验**，不是完整 FullWiki 评测，也不保证每个问题的证据都在子集中。

采集器启动共享检索服务并在结束时清理服务。`search.py` 对真实文本生成 query embedding 并检索，不使用缓存答案；请求排队、检索阶段耗时和服务资源另行记录。默认不评分答案。`qa-record --service …` 支持使用已启动的服务逐个录制；服务入口为 `qa-service --data … --out … --shared-index`。

采集器管理的检索服务默认有 16 GiB 内存保护和 4 小时生命周期，大批采集应分批进行，避免服务先于采集退出。

## SWE-rebench：按任务准备本机环境

SWE 任务来自不同软件仓库，不能用一个通用虚拟环境运行全部任务。本次保留录制与环境准备器，以及 `universe` 的简短配置示例；没有将旧任务数据、构建缓存和 152 份探索性配置整批搬入新仓库。

先从已下载的 SWE-rebench parquet 或单任务 JSON 中导出指定任务。导出器会剔除参考源码补丁 `patch`，保留独立验证需要的 `test_patch`，记录输入文件哈希：

```bash
motivation/trace/.venv-prepare/bin/python motivation/trace/trace.py swe-export \
  --input /path/to/swe-rebench.parquet --instance-id jbradberry__universe-31 \
  --out motivation/trace/data/swe/universe

python motivation/trace/trace.py swe-prepare universe
python motivation/trace/trace.py swe-record \
  --name swe-universe-preflight --preflight-only \
  --task-spec motivation/trace/workloads/swe/task-specs/universe.json

# 上一步确认环境和原始缺陷能够复现后，再调用模型录制
python motivation/trace/trace.py swe-collect \
  --name swe-first-collection --tasks universe
python motivation/trace/trace.py swe-qualify \
  --collection motivation/trace/runs/swe/swe-first-collection \
  --name swe-first-qualification
```

新增任务需要审查 spec 中的源代码路径、Python 版本、测试命令和依赖。`swe-prepare /path/to/spec.json`、`swe-record --task-spec /path/to/spec.json` 支持外部 spec，相对路径以 spec 所在目录为准；本机专用配置可放入被忽略的 `local-specs/`。配置中的 venv Python 路径不会被解析成系统解释器，避免丢失虚拟环境身份。生成的构建文件如需带入新 checkout，应由 `prepared_files` 明确列出并校验哈希。

## 离线回放与结果口径

三个单任务入口均支持 `--replay-source <录制目录> --delay-scale 1`。其余任务数据、工具环境和 QA 服务需与录制一致。批量采集器自动进行的 `--delay-scale 0` 只验证可回放性，不是性能实验。

回放按录制的响应驱动真实 Claude Code，工具再次运行；检查请求、工具输入输出及文件副作用。遇到不一致立即失败，没有自动转到在线 API 的后备路径。需要等长工作目录时，使用 `common/replay_paths.py` 的命名方法；批量资格检查已内置。

```bash
python motivation/trace/trace.py audit /path/to/replay-run
python motivation/trace/trace.py summary /path/to/run
python motivation/trace/trace.py swe-analyze /path/to/swe-run
```

回放比较器有显式归一化规则，例如运行目录、日期以及部分工具输出中的耗时字段。因此“严格回放”指在这些已定义规则下的一致性，不是所有文本逐字相等；规则在 `common/replay.py` 的 `Normalize` 中。保留了旧流格式的审计兼容代码，新录制入口不启用流合并或工具调度干预。

Claude 在某些工具执行前就会拒绝请求，不一定产生工具 hook。`tool_evidence.py` 仅识别带完整输入和精确错误结果的已知拒绝（特定 Edit 错误、Write 的文件已修改提示），单独记入 `tools_rejected_without_hooks`。回放仍要求输入和错误结果一致；如果错误改变、记录缺失或原本被拒绝的调用实际执行，校验失败。这类调用没有独立执行时长，不能算作已执行的工具。

本机保护上限延续原录制设置：SWE 每任务 8 GiB，BigCodeBench/QA 每任务 4 GiB，禁止 swap，并限制进程/线程数量。这些是录制环境配置，**不是内存需求预测或调度策略，也不代表无限资源录制**。后续性能实验必须单独声明资源约束。

## 记录哪些信息

| 文件 | 内容 |
| --- | --- |
| `prompt.txt`、`metadata.json` | 实际提示、任务身份、模式、模型/Claude 版本、运行环境与资源范围 |
| `requests/NNNN/request.json` | 模型请求正文，包含消息历史和工具结果 |
| `requests/NNNN/response.bin`、`chunks.jsonl`、`meta.json` | 模型响应字节、接收时序、HTTP 状态和请求元信息；不保存认证请求头 |
| `claude.jsonl`、`events.jsonl` | Claude 输出，以及工具开始/结束、失败、启动/退出等事件 |
| `resources.jsonl`、`window.json` | Agent 及工具后代的 cgroup 资源时间线和测量起止计数 |
| `observation-quality.json` | 采样间隔、诊断缺失、写入队列、观察器异常等证据质量 |
| `workspace/`、`validation.json` | 实际文件产物和任务辅助验证；SWE 另存候选补丁 |
| `summary.json`、`replay-validation.json`、`replay-audit.json` | 录制完成情况、回放比对、独立原始证据审计（视运行阶段产生） |
| `cleanup.json` | 任务退出后 cgroup 清理结果 |

通常每 50 ms 采样主要 cgroup 计数，每 200 ms 采样进程和主机诊断。资源数据覆盖 Agent + 工具后代，录制代理和观察器在外；QA 检索服务另计。工具 hook 可用于阶段关联，但目前**没有为每次工具调用提供独立、精确的内存峰值标签**。缺失传感器和采样间隙不能用零填补；采样最大值不等于真实瞬时峰值，也不能把各进程 RSS 直接相加当成组内存。

## 检查与迁移边界

```bash
python motivation/trace/run_tests.py
python -m compileall -q motivation/trace
```

测试使用本地 HTTP 服务、临时 Git 仓库和合成工具输入，覆盖录制字节与密钥边界、回放偏离拒绝、观测线程/进程故障、真实代码测试、工具输出比较和路径可移植性。无模型 API 请求，不代表重新验证了所有历史任务或整机并发容量。

迁移说明见 [MIGRATION.md](MIGRATION.md)。旧 trace 和数据保持在原工作区；旧记录中的环境路径、脚本哈希和 Claude 版本可能与新目录不同，不能以修改元数据或放宽比较的方式宣布回放通过。新实验应在此仓库建立新录制和相应资格记录。

### 2026-11-16 本机冒烟

固定选样顺序的首个任务 `BigCodeBench/746` 已完成在线录制与离线真实工具回放：在线约 38.26 秒，6 次模型请求、5 次实际工具调用、1 次本地测试工具调用；离线回放 0 次上游 API 请求，请求、工具输入输出、测试反馈及工作目录产物全部匹配，两次资源观测和 cgroup 清理均正常。回放使用 `--delay-scale 0`，只验证功能一致性，不据此声称性能提升。

本次复用已有 Python 3.11.12 工具依赖和经过哈希校验的原始数据；录制代码与生成目录来自本仓库。此前的余额不足尝试、以及暴露 Write 拒绝兼容问题的尝试均保留。修复后 75 项离线测试通过，并重新在线录制验证；未覆盖原失败记录。原始 trace 和详细结果仅保存本机，汇总位于 `runs/smoke-20261116-retry/result.json`。
