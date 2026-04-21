# 用 pcd 设计 pcd

这个目录是"拿 PCDesign 自己当实验对象"的工作区。`pcd-brief.md` 是喂给
Proposer 的需求；下面的命令从这里出发跑一次完整循环。

## 准备

- `claude` 在 `$PATH` 上（下面用的是 claude 后端；想用 codex 就把所有
  `--agent claude` 去掉）
- 从仓库根目录 `pip install -e .` 过，`pcd` 命令可用
- 当前工作目录切到 **本目录** (`dev/`)：生成的 `pcd-self/` 会落在
  这里，不会污染仓库根

```bash
cd dev/
```

## 跑一次

```bash
# 1. 用 pcd-brief.md 作为初始需求，生成 v0。三个角色都走 claude，
#    v0 这一轮用 high effort 让 Proposer 多想一点。
pcd init pcd-self \
  --prompt-file pcd-brief.md \
  --agent claude \
  --reasoning max

# 2. 最多迭代 10 轮；收敛或连续两轮 must_fix 不降就自动停。
pcd run-until-stop pcd-self --max-iter 10 \
  --proposer-reasoning max \
  --critic-reasoning max \
  --judge-reasoning max

# 3. 查状态 + must_fix 走势。
pcd status pcd-self
```

## 想更细粒度地看每一轮

```bash
# 单步：一轮 critics → judge → (如需) proposer revise
pcd run-once pcd-self \
  --proposer-reasoning high \
  --critic-reasoning medium \
  --judge-reasoning high

# 单步 + 在 $EDITOR 里手改 judge 的决策包再喂给 Proposer
pcd run-once pcd-self --manual-judge
```

## 人工 escape hatch

`run-until-stop` 以 "no progress" 停下来的时候，用下面的命令记录你眼
看过之后的判断：

```bash
# 看过 design.md 后确认可以停了
pcd check pcd-self --result confirm_stop \
  --scope "full review" --note "rationale holds up, ship v0 of self-design"

# 或者"其实还没收敛，接着跑"
pcd check pcd-self --result reopen --note "rationale step 3 还有漏洞"

# 或者只记录一条笔记，不改 converged 状态
pcd check pcd-self --result advisory_only --note "三个评审的 overlap 偏高，下轮试试 --critic-reasoning low"
```

## 产出在哪

- `pcd-self/design.md` — 最终设计文档
- `pcd-self/.pcd/judgments.jsonl` — 每一轮的 critics 原始 issues + Judge 的决策包
- `pcd-self/.pcd/revisions.jsonl` — 每一轮 Proposer 做没做 revise
- `pcd-self/.pcd/human_checks.jsonl` — `pcd check` 的记录
- `pcd-self/.pcd/meta.json` — thread id、各角色的 model / agent、收敛状态

## Prompt 的几处有意为之

- **没提 Proposer / Critic / Judge 这些词**：否则 Rationale 就退化成
  "按给定结构搭积木"。让 Proposer 从零推，如果它自己收敛不到"多个专项
  评审 + 一个仲裁层"，那就是一个值得关注的信号。
- **"每一个 per-round step 都必须有一个理由"**：对 Judge 这类中间层
  最大的压力测试，也是对"三个评审并行而非一个"的压力测试。
- **"稳定性信号"和"无进展 halt"只点名为约束**：不给现成实现方式（当前
  版本用的是 must_fix 不回弹 + 连续两轮不降），让 Proposer 自己推。
- **escape hatch 的 divergence 风险**：对应当前 `--manual-judge` 和
  `pcd check` 的隐含约束（人改 Judge 包或 confirm_stop 时不能把循环搞
  乱），值得明确推一遍。

想缩短迭代次数、把 v0 从一开始就拉近现有设计，可以在 `pcd-brief.md`
里补一句硬约束，比如"设计文档应当至少清晰分离'用户需求'、'方案本身'、
'方案的第一原理论证'这三类内容"——这其实就把三段式直接告诉它，取舍看
你想压测哪一端。
