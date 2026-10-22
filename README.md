# crisp-bot — Crisp AI 客服 + Telegram 话题群监控台

```
访客(Crisp 网页聊天) ⇄ 本服务 ⇄ TG 话题群(你/客服)
                          ↕
                    知识库 + LLM
```

- 新会话进来 → 在 TG 监控群自动建一个话题(标题=昵称+邮箱)
- 访客消息 + AI 回答实时同步到对应话题,AI 回答附带置信度和引用条目
- 置信度低于阈值 → 不回复访客,在话题里 @你 转人工
- 你直接在话题里回复 → 以客服身份发给访客,并自动暂停该会话的 AI
- 客服在 Crisp 后台回复同样会镜像到 TG 并暂停 AI
- 人工回答自动存入 `kb_candidates` 表,定期提炼进知识库形成闭环

## 一、准备 Crisp

1. 打开 <https://marketplace.crisp.chat> → 用 Crisp 账号登录 → **New Plugin**(Private 即可)
2. 在插件的 **Tokens** 页生成生产 token,得到 `identifier` 和 `key` → 填入 `.env` 的 `CRISP_IDENTIFIER` / `CRISP_KEY`
3. 在 **Scopes** 里勾选(读+写):
   - `website:conversation:sessions`
   - `website:conversation:messages`
   - `website:conversation:states`
4. 在插件 **Web Hooks** 页填回调地址 `https://你的域名/webhook/crisp`,订阅事件:
   - `message:send`(访客发消息,必须)
   - `message:received`(客服发消息,建议订阅,用于后台回复镜像)
   - 页面上的签名密钥填入 `CRISP_WEBHOOK_SECRET`(可选)
5. 把插件安装(trusted install)到你的网站
6. `CRISP_WEBSITE_ID` 在 Crisp 后台 → 设置 → 网站设置 → 集成 里查看

> 也可以不建插件,用账号级 token(`CRISP_TIER=user`),但 webhook 仍需在 Marketplace 配置,生产环境建议走插件。

## 二、准备 Telegram

1. 找 @BotFather 建 bot,拿到 `TG_BOT_TOKEN`
2. 建一个**群组**,在群设置里开启 **话题(Topics)** 功能(开启后自动升级为超级群)
3. 把 bot 拉进群并设为**管理员**,勾选「管理话题 Manage Topics」权限
4. 获取群 id(负数,形如 `-100xxxxxxxxxx`):把群里任意消息转发给 @getidsbot,或临时用
   `https://api.telegram.org/bot<token>/getUpdates` 在群里发句话后查看

## 三、LLM

`.env` 里三个变量,走 OpenAI 协议:

- 直连官方:`LLM_BASE_URL` 留空,`LLM_API_KEY` 填 OpenAI key
- 走 new-api 等中转(支持 `/v1/chat/completions` 透传):`LLM_BASE_URL=https://你的中转域名`,`LLM_API_KEY` 填中转站的 `sk-` key
- `LLM_MODEL` 默认 `gpt-5.5`

## 四、运行

```bash
cd crisp-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填好配置
uvicorn app.main:app --host 0.0.0.0 --port 8300
```

本地调试时 Crisp 的 webhook 需要公网地址,可用 `cloudflared tunnel --url http://localhost:8300` 临时打洞;正式部署放 VPS 上,用 nginx/caddy 反代加 HTTPS。

不接 Crisp 也能先验证主链路,伪造一条访客消息:

```bash
curl -s http://localhost:8300/webhook/crisp -H 'content-type: application/json' -d '{
  "website_id": "<你的CRISP_WEBSITE_ID>",
  "event": "message:send",
  "data": {"session_id": "session_test-123", "type": "text", "from": "user", "content": "怎么充值?"}
}'
```

正常的话 TG 群会出现新话题、访客消息和 AI 回答(发 Crisp 那步对假 session 会报错,属预期)。

## 五、systemd 部署示例

```ini
# /etc/systemd/system/crisp-bot.service
[Unit]
Description=Crisp AI support bot
After=network.target

[Service]
WorkingDirectory=/opt/crisp-bot
ExecStart=/opt/crisp-bot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8300
Restart=always

[Install]
WantedBy=multi-user.target
```

## 六、日常使用

**话题内命令:**

| 操作 | 效果 |
|---|---|
| 在话题里直接发文字 | 以客服身份回复访客,自动暂停该会话 AI |
| `/ai on` / `/ai off` | 恢复 / 暂停该会话的 AI 自动回复 |
| `/close` | 会话标记已解决,并恢复 AI |
| `/info` | 查看访客资料(邮箱、国家、来源页面)和 AI 状态 |
| `/learn` | 立即触发一轮人工回答提炼 |

**打分与回答模版**(引用回复某条 🤖 AI 回答):

| 命令 | 效果 |
|---|---|
| `/good` | 好答案,巩固进知识库 |
| `/add 补充内容` | 答得对但不全:LLM 合并原答案+补充,生成新回答模版,立即生效 |
| `/bad 正确答案` | 答错了:整条替换回答模版,立即生效 |

模版存于 `kb/纠错.md`,优先级最高,AI 会按模版原文回答(只微调语气)。

**接管与恢复**:人工回复(TG 或 Crisp 后台)会暂停该会话 AI;距最后一条人工回复超过 `AI_RESUME_MINUTES` 后自动恢复,暂停期间没人回的访客消息会被主动补答(每 30 秒巡检)。

**自动学习**:人工回复 `LEARN_DEBOUNCE_MINUTES` 分钟后自动提炼进 `kb/人工回复提炼.md` 并热加载(个案与寒暄自动丢弃),TG General 里通报;另有每 `LEARN_INTERVAL_HOURS` 小时的兜底轮询。

**知识库**:`kb/` 下放 Markdown,每个 `## 标题` 是一个条目;手动改文件后重启生效(打分/学习产生的会热加载)。冷启动可从历史会话全量提炼:

```bash
.venv/bin/python scripts/distill_kb.py 0 > kb/常见问题.md   # 0=全部消息,分批提炼+合并去重
```

## 已知取舍

- 检索用字符二元组重叠度打分,零依赖、中文可用;知识库大了以后可换 embedding 检索
- 支持图片消息:访客截图会推到 TG 话题并交给视觉模型识别后回答(≤5MB);其他文件类型显示占位符并跳过 AI
- `session ↔ topic` 映射、对话历史、人工答案候选、AI 回答评分都在 SQLite,单文件即全部状态
- AI 人设为真人客服,不透露 AI 身份;LLM 走 OpenAI 协议(`/v1/chat/completions`),base_url 可指向 new-api 等中转
