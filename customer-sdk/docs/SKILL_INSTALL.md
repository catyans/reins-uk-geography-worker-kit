# Install the integration skill / 安装接入 Skill

The public SDK is the only Reins code installed into your application. The optional skill helps a coding assistant find one existing paid Agent call and connect it safely. It is **instructions for the assistant**, not a runtime service or a credential. / 公开 SDK 是安装进应用的唯一 Reins 代码；Skill 只帮助编程助手找到一处现有付费调用并完成接入，不是运行服务，也不包含凭证。

## Install / 安装

Clone the public SDK repository, then install the skill with Python 3.10+ / 先克隆公开 SDK 仓库：

```bash
git clone https://github.com/catyans/reins-sdk.git
cd reins-sdk
```

Then install the skill / 然后安装 Skill：

```bash
python scripts/install_skill.py --target /absolute/path/to/customer-repo --assistant codex
```

For Claude Code use `--assistant claude`; for both use `--assistant both`. The script copies only `skills/reins-integrate/` to `.agents/skills/reins-integrate/` and/or `.claude/skills/reins-integrate/` in the customer repository. It does not edit Agent code or install the SDK. Preview with `--dry-run`, remove with `--uninstall`, and use `--force` only when you intend to replace a locally edited copy. / 脚本只复制 Skill，不会修改 Agent 业务代码。可先用 `--dry-run` 预览，`--uninstall` 删除；只有确定要覆盖本地修改时才使用 `--force`。

Install the SDK separately in the customer's backend environment:

```bash
python -m pip install git+https://github.com/catyans/reins-sdk.git
```

Configure `REINS_BASE_URL` and `REINS_WORKER_TOKEN` in the backend secret environment. Obtain these from your Reins contact. **Do not use the browser pairing token**. The Reins operator first creates a hosted task and gives the customer's backend its workflow ID. / SDK 安装在客户后端；工作区地址和 worker token 从 Reins 团队私下获取，不要使用网页配对码。Reins 操作员需先创建托管任务，再将任务 ID 传给客户后端。

## Use / 使用

In the customer repository, ask your coding assistant:

> Use `$reins-integrate` to connect the `handle_order` workflow to hosted Reins. Keep the existing provider call and business output, use our existing cost and result checks, and test with a fake provider. Do not run a paid live call.

在客户代码仓库中可以说：

> 用 `$reins-integrate` 把 `handle_order` 工作流接入托管 Reins。保留现有模型调用和业务输出，沿用我们已有的费用计算和结果验收规则，用模拟 provider 测试，不运行付费线上调用。

For Claude Code, invoke `/reins-integrate`. Review the patch, provide the workflow ID at runtime, and run the focused tests before enabling it on live traffic. The assistant should leave `REINS_INTEGRATION.md` explaining what it changed and how to remove the integration. / Claude Code 使用 `/reins-integrate`。上线前检查补丁、在运行时提供任务 ID，并执行相关测试；助手会留下 `REINS_INTEGRATION.md` 说明改动与回退办法。

The skill does not grant access to the private Reins service repository. Only the customer-side SDK and the chosen calls in their own application are involved. / Skill 不授予 Reins 私有服务端仓库权限。

## Demo pairing / 演示配对

After installing the SDK, generate a code in the customer backend environment:

```bash
python -m reins_sdk.pair
```

The command generates a strong code and saves it in `~/.config/reins/demo-worker.token` with file permissions 0600. The Reins operator creates a Hosted task once, then pastes the displayed code into **Reins Settings > Pair a customer SDK** with that task ID. This first task identifies the customer account and approved deployment. Check the pairing with:

```bash
python -m reins_sdk.pair --check
```

Construct the SDK with `ReinsClient("https://47.245.114.167:8444/api", token_file="~/.config/reins/demo-worker.token")`. The IP is a demo default; use `--base-url` and your own configuration when the endpoint moves. For each new run, call `client.start_run()` and use its returned `workflow_id` for the SDK's decision, reservation and settlement calls. The SDK generates a new internal run ID automatically; the customer does not enter another task ID or pair again. If a start response is lost, retry with the **same** `run_key` rather than creating a second task. The operator can revoke the customer pairing on the Settings page.

客户先运行第一条命令生成配对码；我们只在首次配对时创建 Hosted task，并在 Settings 页面输入配对码和任务 ID。此后运行 `--check` 即可验证账号连接。每次执行由 `client.start_run()` 在服务端创建独立的运行记录，自动返回内部 ID；客户无需再次配对或手动输入 Task ID。不要将码提交到 Git。
