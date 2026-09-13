# ADR-003：通过兼容 Chat API 接入 DeepSeek Flash

## 状态

已接受。

## 背景

PatchLoop 需要从确定性 Fake Provider 进入真实模型闭环，同时保持核心运行时不绑定单一厂商。DeepSeek Flash 支持 OpenAI 兼容的 Chat Completions、工具调用和 thinking 模式，适合当前以成本和速度优先的单 Agent 阶段。

2026-09-13，DeepSeek 官方 API 的实时 `/models` 目录已将 Flash 模型标识更新为
`deepseek-flash`。使用该标识的 Chat Completions 基础请求和工具调用请求均返回成功，响应中的
模型标识也是 `deepseek-flash`。历史报告中的 `deepseek-v4-flash` 保持原样，表示当时实际使用的
模型标识，不追溯改写。

## 决策

- 当前默认模型和新的真实 Provider 验收固定使用模型标识 `deepseek-flash`；历史验收继续保留
  `deepseek-v4-flash` 证据。
- 使用官方 `https://api.deepseek.com/chat/completions` 接口。
- 默认开启 thinking，推理强度使用 `high`，温度为 `1.0`。
- API 密钥从进程环境变量 `DEEPSEEK_API_KEY` 或兼容变量 `LLM_API_KEY` 读取，并使用 `SecretStr`
  保存；不支持命令行参数传入密钥。
- Provider 完整保存 assistant tool calls 和 tool call ID，保证多轮工具调用上下文符合兼容协议。
- HTTP 429 和 5xx 以及连接错误采用有上限的指数退避；返回给运行时的错误先移除密钥。
- 模型产生的工具参数仍由 Tool Gateway 校验；无法解析的 JSON 被归类为 `invalid_arguments`，不会使用工具默认参数执行。
- 首版使用 Python 标准库 HTTP 客户端，避免为单个 Provider 引入重量依赖。

## 后果

用户设置一个环境变量后即可运行真实 Agent，Fake Provider 测试仍保持确定性。同步 HTTP 调用暂不支持流式展示，模型价格也不在客户端硬编码；后续会在 Provider 接口稳定后增加流式事件、可配置模型目录和按服务端账单数据计算的成本指标。
