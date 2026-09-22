# astrbot_plugin_outputpro_split

OutputPro Split 是一个 AstrBot 输出增强插件 fork。它保留 OutputPro 的发送前管道能力，并重点重做 `split` 分段回复模块，使它更接近 `astrbot_plugin_splitter` 的体验，同时使用独立插件名与数据目录，避免和原插件冲突。

## 上游来源

- 上游主插件：[`Zhalslar/astrbot_plugin_outputpro`](https://github.com/Zhalslar/astrbot_plugin_outputpro)
- 分段模块参考：[`nuomicici/astrbot_plugin_splitter`](https://github.com/nuomicici/astrbot_plugin_splitter)

本仓库是 fork/衍生版本，不会复用 `astrbot_plugin_outputpro` 的插件标识：

- 插件名：`astrbot_plugin_outputpro_split`
- 数据目录：`data/plugins/astrbot_plugin_outputpro_split`
- 默认资源路径已同步改为新的数据目录

## 功能概览

默认管道仍由 `pipeline.steps` 控制：

| 步骤 | 配置名 | 说明 |
| --- | --- | --- |
| 图片外显 | `summary` | 纯图片消息插入外显金句（仅 aiocqhttp，不含可见文字时生效） |
| 报错处理 | `error` | 拦截或转发异常消息 |
| 消息拦截 | `block` | 超时、复读、关键词拦截 |
| 解析艾特 | `at` | 假 @ 转真实 @ |
| 文本清洗 | `clean` | 清理括号、emoji、前后缀等 |
| 文本替换 | `replace` | 敏感词或固定词替换 |
| 文转语音 | `tts` | TTS 模型或 QQ 声聊中转 |
| 文转图片 | `t2i` | 长文本渲染成图片 |
| 智能引用 | `reply` | 被插嘴时自动引用原消息 |
| 合并转发 | `forward` | 长文本转合并转发/折叠引用 |
| 自动撤回 | `recall` | 命中关键词后延迟撤回 |
| 分段回复 | `split` | 智能分段、组件策略和打字延迟 |

### 图片外显的生效范围

图片外显挂在 AstrBot 的发送前结果装饰流程上，只能处理写入 `event.result` 的消息链：

- 消息链只含图片时生效，允许同时携带 At、AtAll、Reply 和纯空白文本；含可见文字时不处理。
- 多张图片时只给第一张附加外显金句。
- 其他插件通过 `event.send()` 或 `context.send_message()` 主动发送的图片不经过本插件，无法附加外显。例如 [`astrbot_plugin_stealer`](https://github.com/nagatoquin33/astrbot_plugin_stealer) 的表情包就是主动发送的。
- 本插件分段回复对非末段同样使用主动发送，被分到非末段的图片也不会附加外显。

开启 `pipeline.debug_logging` 后，日志会记录每次跳过外显的具体原因和当前消息链摘要。

## 分段模块优化

`split` 模块在 OutputPro 原有组件级分段基础上融合了 splitter 的关键能力：

- 支持 `simple` 字符列表模式与 `regex` 正则模式。
- 智能保护括号、引号、代码块、`<think>...</think>` 和常见颜文字，减少误切。
- 支持智能均分，超过最大段数时会选择更均衡的切点。
- 支持最短触发字数、最长处理字数、最大段数和最小段长。
- 支持分段前/后文本清理，包括普通字符串清理和正则清理。
- 支持图片、表情、其他组件的归属策略：单独、跟随上段、跟随下段、嵌入。At 与字符串艾特固定绑定正文，不单独分段。
- 支持多种延迟策略：`legacy`、`linear`、`log`、`random`、`fixed`。
- 保留 OutputPro 原有 typing 状态模拟。

### 艾特与智能引用

- 艾特概率只处理有可见正文的群聊回复。空文本、空白和零宽字符组成的工具调用中间结果不会补艾特或消耗引用机会，也不会停止工具循环。
- 是否带艾特统一由 `at.at_prob` 控制；设为 0 只关闭概率控制。启用概率控制时，`reply.include_at` 不会覆盖概率结果；未启用时保留引用附带艾特的行为，且不重复插入。
- 分段模块不再提供单独的 At 策略。旧配置中的 `split.at_strategy` 会被忽略，无论旧值是单独、跟随上段还是嵌入，艾特都跟正文一起发送。有图片插在前面时优先跟随文字；确实只有图片时保留已有艾特并与图片一起发送。
- 智能引用在唤醒检查阶段记录收到的群消息，文字、图片等插嘴均计数，不要求插嘴消息触发 Bot。阈值 1 表示插入一条消息即引用；按平台连接、Bot 账号与群号隔离，同一消息 ID 重复上报不重复计数。
- 分段等待期间出现新插嘴时，下一段会重新判断引用；已引用后再有新插嘴也能再次引用。图片外显直接发送时同样先判断智能引用。
- 建议关闭 AstrBot 自带的 At、Reply 与分段回复，避免再次添加前缀或再次拆分。本插件仍不接管其他插件的主动发送和绕过结果装饰的流式发送。

排查时开启 `pipeline.debug_logging`，可查看空结果跳过、艾特概率判断，以及引用判断的 `anchor_seq`、`latest_seq`、`pushed` 和消息队列。

## 安装

将本目录放入 AstrBot 插件目录后重载插件即可。由于插件标识不同，它可以和上游 `astrbot_plugin_outputpro` 同时存在，但不建议在同一会话同时启用两套输出管道，以免重复处理同一条消息。

## 配置建议

- 若只想处理模型回复，在 `pipeline.llm_steps` 中保留 `split`。
- 若希望所有插件输出也参与分段，把 `split` 从 `pipeline.llm_steps` 中移除。
- 想避免短句被拆开，可设置 `split.min_length`。
- 想保护超长内容不被拆，可设置 `split.max_length`。
- 想更像 splitter 的均衡效果，保持 `split.balanced_split_mode = true`。
- v2.3.6 起已移除错字模拟功能及其 `jieba`、`pypinyin` 依赖；升级后旧的 `typo` 配置不会再创建或执行步骤，请从 `pipeline.steps` 和 `pipeline.llm_steps` 中移除残留项。

## 诊断日志

排查配置未生效时，可临时开启 `pipeline.debug_logging`。插件会在 AstrBot 后台以 INFO 级别记录每个管道步骤，以及文本清理的阈值、正则、命中内容和清理前后预览。日志可能包含回复正文，问题确认后建议关闭。

文本清理仅处理长度小于 `clean.text_threshold` 的文本。默认阈值为 `150`，长回复即使正则正确也会跳过；开启诊断日志后会明确显示 `len >= threshold`。

智能引用的判断以每个群消息事件自身记录的序号为锚点，统计该消息之后、当前 Bot 回复准备发送前到达的用户消息数量。这样多个 LLM 回复同时处理时不会互相消费引用队列。开启诊断日志后，可在后台看到 `anchor_seq`、`latest_seq`、`pushed`、阈值、队列和具体的触发或跳过原因。

## 致谢

感谢 `Zhalslar/astrbot_plugin_outputpro` 提供完整输出增强管道，感谢 `nuomicici/astrbot_plugin_splitter` 提供更细致的分段体验设计。
