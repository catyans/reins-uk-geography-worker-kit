# Reins Python SDK

[English](README.md)

这是给客户后端使用的轻量客户端，用于接入托管版 Reins：在付费模型调用前获取运行决策和费用预留，调用后记录实际费用，并查询任务状态。**Agent、模型调用和提供商密钥都留在你的应用中。**本仓库不包含 Reins 服务端、策略引擎、数据库、网页或部署凭证。

## 安装

需要 Python 3.10+，无运行时第三方依赖：

```bash
python -m pip install git+https://github.com/catyans/reins-sdk.git
```

独立演示时，客户运行 `python -m reins_sdk.pair` 生成私有配对码；我们用一个 Hosted task 在 Reins Settings 页面完成首次配对。以后每次运行调用 `ReinsClient.start_run()`，服务端自动创建独立记录并返回内部 ID，客户不用重复配对或输入 Task ID。步骤见 [Skill 安装与配对说明](docs/SKILL_INSTALL.md)。其他托管工作区仍由我们私下提供 worker token 和 HTTPS API 地址。凭证只放在客户后端，不放进浏览器、移动端或代码仓库。网页登录配对码不能当作 worker token。

托管 API 地址可能以 `/api` 或 `/v1` 结尾，例如 `https://reins.example.com/api`。请使用交付给你的工作区地址。

## 在现有模型调用外加一层控制

```python
import os
from reins_sdk import Charge, ReinsClient

client = ReinsClient(
    os.environ["REINS_BASE_URL"],
    token=os.environ["REINS_WORKER_TOKEN"],
)

def call_your_provider(config):
    response = your_provider.generate(model=config["model"], prompt="...")
    return Charge(
        result=response.output,
        actual_cost=response.cost_usd,
        usage={"input_tokens": response.input_tokens, "output_tokens": response.output_tokens},
        validated=your_business_check(response.output),
        validation_name="business-result-v1",
    )

outcome = client.run_paid_call(
    workflow_id="reins-中已创建的任务-id",
    execute=call_your_provider,
    max_cost="0.10",  # 美元计价的保守上限，不能只填平均费用。
)
print(outcome.result)
```

`run_paid_call` 先读取任务选定的配置和是否继续的决策，再预留费用；只有获准后才会调用你原有的模型客户端，最后登记实际费用。它**不会自动重试**付费调用。若调用已经发生、但结算没有得到确认，会抛出带有请求 ID 的 `SettlementUnknown`；应核对这次调用，不能直接重新调用模型。

## 复用已经验收的只读结果

对于**不会修改外部状态**的操作，可以把决定结果的输入和版本化验收规则交给 SDK。Reins 只在**同一次运行内**复用已经结算、验收通过的相同结果；命中时不再调用模型，也不会产生第二笔调用费用。SDK 返回前还会重新检查结果。

```python
result = client.run_paid_call(
    workflow_id,
    call_your_provider,
    max_cost="0.10",
    reuse_inputs={"document_id": document_id, "source_revision": source_revision},
    read_only=True,
    validator=lambda output: your_business_check(output),
    validation_name="business-result-v2",
)
if result.reused_from:
    print("复用了已验收的请求", result.reused_from)
```

输入应包含来源版本、提示词版本等所有会影响答案的内容；验收规则变化时更新 `validation_name`。有写入或外部副作用的操作不能启用结果复用。验收失败的调用仍需计费，但不会被缓存。任务本身被暂停或超预算时，服务器也可能拒绝复用请求。

Gemini 的**厂商上下文缓存**与上述结果复用不同。SDK 不会自动创建厂商缓存，但可用 `normalize_google_text_usage()` 和 `estimate_google_text_cost()` 按返回的缓存 token 和明确提供的当前单价计算纯文本费用，再把归一化用量放入 `Charge.usage`。这是价目表估算，账单仍需单独核对。详见 [API 说明](docs/API.md)。

多步骤 Agent 可以直接使用 `decision()`、`reserve()`、`settle()`、`uncertain()`、`progress()` 和 `status()`。详细边界见 [API 说明](docs/API.md)。要让编程助手适配现有客户代码，可安装[接入 Skill](docs/SKILL_INSTALL.md)。只有接入 SDK 的调用会受到控制。

此外可以用 `child_task()` 给子 Agent 设独立上限，用 `operation_inputs` 标记重复调用，由服务端策略识别慢循环；`run_with_fallback()` 在业务验收失败后，只在服务端批准另一个模型时追加一次调用，前一次费用仍然记录。`context_sample()` 按系统提示、历史、检索、工具定义和用户输入拆分估算 token 数，不上传原文。上述控制要由服务端配置相应策略；明细和限制见 [API 说明](docs/API.md#runtime-cost-controls)。

## 数据边界

worker API 接收任务 ID、模型/配置、费用估计及实际费用、可选 token 用量，以及你主动提交的结果。SDK 不会自动上传 prompt 或原始文档；但传给 `settle(result=...)` 的内容会保存在 Reins，请按实际协议删减敏感结果。模型提供商密钥不会发送给 Reins。

## 本地测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试只使用本地模拟 HTTP 服务，不调用付费模型。SDK 使用 Python 标准库，不关闭 TLS 证书校验，也不会通过环境配置的代理发送 worker token。

本仓库采用 Apache-2.0 许可；Reins 服务端另行部署，不在本仓库中。
