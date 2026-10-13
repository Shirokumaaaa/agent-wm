我理解你现在要做的不是立即实现算法，而是先把整个 **SWE-smith → SWE-agent → 水印拒绝采样 → 数据筛选** 的工程闭环搭起来，形成一个后续逐步补全的项目设计文档。

我先根据你前面的思路整理成一个工程规划 Markdown。这里我会刻意区分：

- **已有组件**：SWE-smith、SWE-agent 本身负责什么。
- **需要改造部分**：终态 Response 控制、水印植入、拒绝采样。
- **暂不确定部分**：哈希设计、采样阈值等。

---

# Terminal Response Watermarking for SWE Agent Trajectory Generation

## 1. Project Overview

### 1.1 Motivation

当前 Coding Agent（如 SWE-agent）通过多轮交互完成软件工程任务：

```
Problem Statement
        ↓
Agent Response
        ↓
Tool Execution
        ↓
Observation Feedback
        ↓
...
        ↓
Final Response
```

一条完整轨迹包含大量中间交互。

本项目探索一种低侵入式 Agent 轨迹水印方案：

核心思想：

> 仅修改 Agent 轨迹中的最后一次自然语言 Response，
> 不干扰中间代码搜索、修改、测试等行为，
> 通过终态响应携带可验证水印。

---

## 2. Core Hypothesis

### H1: Terminal Response Sufficiency

虽然水印只存在于最后一次 Response：

$$
A_{T}^{W}
$$

但该 Response 处于完整任务上下文：

$$
P(A_T|U,A_1,O_1,...,A_{T-1},O_{T-1})
$$

因此可以作为整条任务轨迹的代表性载体。

### H2: Sparse Watermark Injection

假设平均轨迹长度：

$$
T=22
$$

则：

$$
\rho=\frac{1}{T}
$$

约：

$$
4.5\%
$$

的 Agent response 被修改。

目标：

- 降低水印暴露概率。
- 降低对 Agent 行为能力影响。
- 保持任务级可验证性。

---

## 3. Overall Pipeline

整体流程：

```
SWE-smith Dataset
        |
        |
        v
Task Instance Generation
        |
        |
        v
SWE-agent Execution
        |
        |
        v
Raw Trajectory τ
        |
        |
        v
Terminal Response Watermark Sampling
        |
        |
        v
Accepted Watermarked Trajectory
```

---

## 4. Module Design

### Module 1: SWE-smith Task Provider

#### Objective

提供可执行的软件工程任务。

Input:

```
SWE-smith Instance
```

包含：

```json
{
 "instance_id": "",
 "repo": "",
 "problem_statement": "",
 "image_name": "",
 "tests": []
}
```

输出：

```
Executable Task Environment
```

待确认：

- 是否直接使用 SWE-smith 官方任务。
- 是否需要重新生成任务。
- 任务规模。

---

### Module 2: SWE-agent Harness Control

#### Objective

控制 SWE-agent 执行过程。

需要完成：

1. 找到 Agent 主循环。

2. 定位：

```
Agent Step
        |
        v
LLM Response
        |
        v
Tool Execution
```

3. 捕获：

每一次：

```
request
response
tool_call
observation
```

形成：

$$
\tau_i
=
(o_{i,0},
[(r_{i,t},a_{i,t},o_{i,t})]_{t=1}^{T_i})
$$

---

#### Required Modification

新增：

```
Terminal Response Hook
```

判断：

```
current_step == final_step
```

如果：

```
response contains natural language
```

进入水印模块。

否则：

skip。

禁止：

- 修改 tool_call。
- 修改代码 patch。
- 修改中间 response。

---

### Module 3: SRO Task Representation

#### Objective

为任务生成稳定语义表示。

对于任务 q：

抽取：

$$
(S_q,R_q,O_q)
$$

例如：

```
Subject:
HTTP parser

Relation:
rejects

Object:
invalid chunk extension
```

生成：

$$
I_q=
H(K_1||S_q||R_q||O_q)
$$

作为任务索引。

---

#### Open Problem

SRO 是否稳定？

需要验证：

- 不同 LLM 提取一致性。
- 同任务不同描述稳定性。
- embedding similarity。

---

### Module 4: Watermark Generation

#### Input

任务：

$$
q
$$

SRO：

$$
S_q,R_q,O_q
$$

Secret Key:

$$
K
$$

生成：

$$
WM=b_0b_1...b_{L-1}
$$

同时：

$$
c_i=H(b_i||K||i)\mod P
$$

形成检测编码。

---

### Module 5: Watermark Rejection Sampling

#### Objective

生成满足水印约束的 Agent Response。

流程：

```
Original terminal response

        |
        v

LLM rewrite candidates

        |
        v

SRO consistency check

        |
        v

Watermark verification

        |
        v

Accept / Reject
```

---

#### Acceptance Criteria

候选 Response y：

抽取：

$$
S_y,R_y,O_y
$$

满足：

$$
H(K_2||S_y||R_y||O_y||C)
\mod N_p=0
$$

同时满足：

##### 1. Semantic Similarity

任务语义保持：

```
embedding(q,y)
```

检查：

- cosine similarity
- length difference
- tool usage consistency

---

##### 2. Harmlessness

检查：

- 不影响任务结果。
- 不改变 patch。
- 不修改测试结果。

---

##### 3. Uniqueness

检查：

- 不重复生成。
- 不同任务不会碰撞。

---

##### 4. Authenticity

检查：

轨迹：

```
real environment execution
```

工具反馈来自：

```
Docker environment
```

---

### Module 6: Dataset Construction

最终生成：

```
Watermarked SWE Dataset
```

每条数据：

```json
{
 "instance_id": "",

 "problem_statement": "",


 "trajectory": [

   {
    "step":1,
    "response":"",
    "action":"",
    "observation":""
   }

 ],


 "terminal_response_watermarked": "",


 "watermark_code": "",


 "verification_info":""

}
```

