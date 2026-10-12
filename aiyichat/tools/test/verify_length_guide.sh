#!/bin/bash
# 回复长度软引导功能验证（测试服执行）
# 验证三组场景：短消息简短自然 / 深度倾诉可展开超50字 / 不再被50字硬截断
set -e
cd "$(dirname "$0")/../.."

mongo_eval() {
  docker compose exec -T mongo mongosh --quiet aiyichat --eval "$1"
}

echo "===== 0. 运行时配置确认 ====="
docker compose exec -T app python -c "
import asyncio
from app.db.mongo import connect_db
from app.config import load_settings_from_db, settings
async def main():
    await connect_db()
    await load_settings_from_db()
    print('chat_max_tokens =', settings.chat_max_tokens)
    print('multi_message_max_count =', settings.multi_message_max_count)
asyncio.run(main())
"

echo "===== 1. 获取测试 bot / 用户 ====="
INFO=$(mongo_eval '
const svc = db.services.findOne();
const user = db.users.findOne({id: svc.user_id});
print([svc.bot_id, String(svc.account_id), user.platform_id, user.platform].join("|"));
')
IFS='|' read -r BOT_ID ACCOUNT_ID PLATFORM_ID PLATFORM <<< "$INFO"
echo "bot_id=$BOT_ID platform=$PLATFORM platform_id=$PLATFORM_ID"

send() {
  curl -sk --max-time 90 -X POST https://localhost:8848/api/conversations/chat/stream \
    -H 'Content-Type: application/json' \
    -d "{\"bot_id\":\"$BOT_ID\",\"platform\":\"$PLATFORM\",\"platform_id\":\"$PLATFORM_ID\",\"text\":\"$1\"}" \
    | sed 's/^data: //' | tr -d '\n'
}

echo ""
echo "===== 2. 场景一：短消息（嗯）→ 预期自然简短 ====="
R1=$(send "嗯")
echo "回复: $R1"
echo "长度: ${#R1}"

echo ""
echo "===== 3. 场景二：深度倾诉长消息 → 预期可自然展开（旧逻辑会截断在约50字） ====="
R2=$(send "最近真的好累，工作上一直加班，领导还不理解我，回到家也是一个人。有时候真不知道自己这么努力是为了什么，就想跟你说说话")
echo "回复: $R2"
echo "长度: ${#R2}"

echo ""
echo "===== 4. 场景三：请求详细展开（讲故事）→ 预期长回复 ====="
R3=$(send "给我详细讲讲你今天都做了些什么吧，从头到尾慢慢说，我想听长一点的")
echo "回复: $R3"
echo "长度: ${#R3}"

echo ""
echo "===== 验证完成 ====="
