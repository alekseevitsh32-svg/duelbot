import os
import asyncio
import random
import re
import uuid
import time
from http.server import BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DUEL_TIMEOUT = 180  # 3 минуты на ответ
CMD_COOLDOWN = 30   # 30 сек кд на команду в чате
END_COOLDOWN = 180  # 3 минуты кд после окончания дуэли
# ===================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Память бота
duels = {}             # Активные дуэли
active_players = {}    # Кто сейчас занят в дуэли
chat_cooldowns = {}    # Кд на команды в чатах


async def safe_send(coro):
    try:
        await coro
    except TelegramRetryAfter as e:
        print(f"FLOOD WAIT: {e.retry_after}s")
        await asyncio.sleep(e.retry_after + 0.5)
        try: 
            await coro
        except Exception as e2:
            print("RETRY FAILED:", repr(e2))
    except TelegramAPIError as e:
        print("TELEGRAM API ERROR:", repr(e))
    except Exception as e:
        print("SEND ERROR:", repr(e))


def end_duel(duel_id, chat_id):
    """Завершает дуэль, очищает игроков и ставит кд чату на 3 минуты."""
    if duel_id in duels:
        duel = duels[duel_id]
        active_players.pop(duel['challenger'].lower(), None)
        active_players.pop(duel['target'].lower(), None)
        del duels[duel_id]
    
    chat_cooldowns[chat_id] = time.time() + END_COOLDOWN


async def check_timeouts():
    """Проверяет, не истекли ли 3 минуты ожидания."""
    now = time.time()
    for duel_id, duel in list(duels.items()):
        if duel.get('status') == 'pending' and (now - duel['created_at']) > DUEL_TIMEOUT:
            text = f"@{duel['challenger']}! Ваш вызов на дуэль @{duel['target']} Не ответил"
            try:
                await bot.edit_message_text(
                    chat_id=duel['chat_id'],
                    message_id=duel['message_id'],
                    text=text
                )
            except Exception as e:
                print("TIMEOUT EDIT ERROR:", repr(e))
            
            end_duel(duel_id, duel['chat_id'])


@dp.message()
async def handle_messages(message: Message):
    if not message.text:
        return

    await check_timeouts()
    
    match = re.search(r"(?i)\bдуэль\s+@([a-zA-Z0-9_]+)", message.text)
    if not match:
        return

    chat_id = message.chat.id
    now = time.time()

    # Кд на команду в чате (30 секунд)
    if now < chat_cooldowns.get(chat_id, 0):
        wait = int(chat_cooldowns[chat_id] - now)
        await safe_send(message.reply(f"⏳ Подождите ещё {wait} сек. перед следующей дуэлью!"))
        return

    target_username = match.group(1)
    challenger_username = message.from_user.username or message.from_user.first_name

    if target_username.lower() == challenger_username.lower():
        await safe_send(message.reply("Нельзя вызвать на дуэль самого себя! 🤦‍♂️"))
        return

    # Проверка, не бот ли цель
    try:
        target_chat = await bot.get_chat(f"@{target_username}")
        if target_chat.is_bot:
            await safe_send(message.reply("Нельзя вызывать бота на дуэль! 🤖"))
            return
    except Exception:
        pass

    # Проверка на занятость
    if target_username.lower() in active_players:
        await safe_send(message.reply(f"@{challenger_username}! @{target_username} Уже играет дуэль!"))
        return
    
    if challenger_username.lower() in active_players:
        await safe_send(message.reply(f"@{challenger_username}! Вы уже участвуете в дуэли!"))
        return

    # Создаем дуэль
    duel_id = str(uuid.uuid4())[:8]
    
    text = (f"@{target_username} Минуточку Внимания!\n"
            f"@{challenger_username} Вызвал Нас На дуэль!\n"
            f"Нажмите кнопки Снизу.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Играть", callback_data=f"play:{duel_id}"),
            InlineKeyboardButton(text="Проигнорировать", callback_data=f"ignore:{duel_id}")
        ]
    ])

    sent_msg = await message.reply(text, reply_markup=kb)
    
    duels[duel_id] = {
        'challenger': challenger_username,
        'target': target_username,
        'turn': 'T', 
        't_acc': 20,
        'c_acc': 20,
        'status': 'pending',
        'created_at': now,
        'chat_id': sent_msg.chat.id,
        'message_id': sent_msg.message_id
    }

    active_players[target_username.lower()] = duel_id
    active_players[challenger_username.lower()] = duel_id
    
    chat_cooldowns[chat_id] = now + CMD_COOLDOWN


@dp.callback_query()
async def handle_duel_buttons(callback: CallbackQuery):
    await check_timeouts()

    data = callback.data.split(":")
    action = data[0]
    duel_id = data[1]

    if duel_id not in duels:
        await callback.answer("Дуэль устарела или не найдена.", show_alert=True)
        return

    duel = duels[duel_id]
    user = callback.from_user.username or callback.from_user.first_name
    chat_id = callback.message.chat.id

    if action in ['play', 'ignore']:
        if user.lower() != duel['target'].lower():
            await callback.answer("Эта дуэль не для вас!", show_alert=True)
            return

    if action == "ignore":
        await safe_send(callback.message.edit_text(
            f"@{duel['target']} Проигнорировал Дуэль От @{duel['challenger']}"
        ))
        end_duel(duel_id, chat_id)
        await callback.answer()
        return

    if action == "play":
        duel['status'] = 'active'
        await update_turn_message(callback.message, duel_id, duel, prefix="Дуэль Готова!")
        await callback.answer()
        return

    if action in ['shoot', 'aim']:
        expected_user = duel['target'] if duel['turn'] == 'T' else duel['challenger']
        
        if user.lower() != expected_user.lower():
            await callback.answer("Сейчас не ваш ход!", show_alert=True)
            return

        if action == "aim":
            if duel['turn'] == 'T':
                duel['t_acc'] = min(100, duel['t_acc'] + 20)
            else:
                duel['c_acc'] = min(100, duel['c_acc'] + 20)
            
            duel['turn'] = 'C' if duel['turn'] == 'T' else 'T'
            await update_turn_message(callback.message, duel_id, duel, prefix=f"🎯 @{user} прицелился получше! (+20% к шансу)")
            await callback.answer("Вы прицелились!")
            return

        if action == "shoot":
            chance = duel['t_acc'] if duel['turn'] == 'T' else duel['c_acc']
            roll = random.randint(1, 100)

            shooter = duel['target'] if duel['turn'] == 'T' else duel['challenger']
            victim = duel['challenger'] if duel['turn'] == 'T' else duel['target']

            if roll <= chance:
                await safe_send(callback.message.edit_text(
                    f"🔫 БАХ! @{shooter} Убил @{victim}!"
                ))
                end_duel(duel_id, chat_id)
            else:
                duel['turn'] = 'C' if duel['turn'] == 'T' else 'T'
                await update_turn_message(callback.message, duel_id, duel, prefix=f"💨 @{shooter} промахнулся! (Шанс был {chance}%)")
            
            await callback.answer()
            return


async def update_turn_message(message, duel_id, duel, prefix=""):
    turn_user = duel['target'] if duel['turn'] == 'T' else duel['challenger']
    acc = duel['t_acc'] if duel['turn'] == 'T' else duel['c_acc']

    text = f"{prefix}\n\nОчередь @{turn_user}.\nВыбирайте действие:\n(Шанс попадания: {acc}%)"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Стрелять", callback_data=f"shoot:{duel_id}"),
            InlineKeyboardButton(text="Прицелится получше", callback_data=f"aim:{duel_id}")
        ]
    ])
    
    await safe_send(message.edit_text(text, reply_markup=kb))


# ==== СЕРВЕРLESS ОБЕРТКА ДЛЯ VERCEL ====
async def _async_process_update(update: Update):
    await dp.feed_update(bot, update)

def _process_update(update: Update):
    try:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_process_update(update))
    except Exception as e:
        print("PROCESS ERROR:", repr(e))

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            update = Update.model_validate_json(body)
            _process_update(update)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        except Exception as e:
            print("HANDLER ERROR:", repr(e))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Duel Bot is running")
