import os
import asyncio
import random
import re
import uuid
import time
from http.server import BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DUEL_TIMEOUT = 180 
CMD_COOLDOWN = 30  
END_COOLDOWN = 180 
# ===================

# Диспетчер и память остаются глобальными
dp = Dispatcher()
duels = {}             
active_players = {}    
chat_cooldowns = {}    

def end_duel(duel_id, chat_id):
    if duel_id in duels:
        duel = duels[duel_id]
        active_players.pop(duel['challenger'].lower(), None)
        active_players.pop(duel['target'].lower(), None)
        del duels[duel_id]
    chat_cooldowns[chat_id] = time.time() + END_COOLDOWN

def check_player_status(username):
    username = username.lower()
    if username in active_players:
        player_data = active_players[username]
        if time.time() - player_data['time'] > 300:
            del active_players[username]
            return False
        return True
    return False

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == 'private':
        await message.answer("Я жив!")

@dp.message()
async def handle_messages(message: Message):
    if not message.text:
        return
    
    match = re.search(r"(?i)\bдуэль\s+@([a-zA-Z0-9_]+)", message.text)
    if not match:
        return

    chat_id = message.chat.id
    now = time.time()

    if now < chat_cooldowns.get(chat_id, 0):
        wait = int(chat_cooldowns[chat_id] - now)
        await message.reply(f"⏳ Подождите ещё {wait} сек. перед следующей дуэлью!")
        return

    target_username = match.group(1)
    challenger_username = message.from_user.username or message.from_user.first_name

    if target_username.lower() == challenger_username.lower():
        await message.reply("Нельзя вызвать на дуэль самого себя! 🤦‍♂️")
        return

    try:
        target_chat = await message.bot.get_chat(f"@{target_username}")
        if target_chat.is_bot:
            await message.reply("Нельзя вызывать бота на дуэль! 🤖")
            return
    except Exception:
        pass

    if check_player_status(target_username):
        await message.reply(f"@{challenger_username}! @{target_username} Уже играет дуэль!")
        return
    
    if check_player_status(challenger_username):
        await message.reply(f"@{challenger_username}! Вы уже участвуете в дуэли!")
        return

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

    active_players[target_username.lower()] = {'duel_id': duel_id, 'time': now}
    active_players[challenger_username.lower()] = {'duel_id': duel_id, 'time': now}
    
    chat_cooldowns[chat_id] = now + CMD_COOLDOWN


@dp.callback_query()
async def handle_duel_buttons(callback: CallbackQuery):
    try:
        data = callback.data.split(":")
        action = data[0]
        duel_id = data[1]

        if duel_id not in duels:
            await callback.answer("Дуэль устарела. Начните новую!", show_alert=True)
            return

        duel = duels[duel_id]
        user = callback.from_user.username or callback.from_user.first_name
        chat_id = callback.message.chat.id

        if action in ['play', 'ignore']:
            if user.lower() != duel['target'].lower():
                await callback.answer("Эта дуэль не для вас!", show_alert=True)
                return

        if action == "ignore":
            await callback.message.edit_text(f"@{duel['target']} Проигнорировал Дуэль От @{duel['challenger']}")
            end_duel(duel_id, chat_id)
            await callback.answer()
            return

        if action == "play":
            duel['status'] = 'active'
            active_players[duel['target'].lower()]['time'] = time.time()
            active_players[duel['challenger'].lower()]['time'] = time.time()
            
            turn_user = duel['target']
            acc = duel['t_acc']
            text = f"Дуэль Готова!\n\nОчередь @{turn_user}.\nВыбирайте действие:\n(Шанс попадания: {acc}%)"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Стрелять", callback_data=f"shoot:{duel_id}"),
                InlineKeyboardButton(text="Прицелится получше", callback_data=f"aim:{duel_id}")
            ]])
            await callback.message.edit_text(text, reply_markup=kb)
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
                turn_user = duel['target'] if duel['turn'] == 'T' else duel['challenger']
                acc = duel['t_acc'] if duel['turn'] == 'T' else duel['c_acc']
                
                text = f"🎯 @{user} прицелился получше! (+20% к шансу)\n\nОчередь @{turn_user}.\nВыбирайте действие:\n(Шанс попадания: {acc}%)"
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Стрелять", callback_data=f"shoot:{duel_id}"),
                    InlineKeyboardButton(text="Прицелится получше", callback_data=f"aim:{duel_id}")
                ]])
                await callback.message.edit_text(text, reply_markup=kb)
                await callback.answer("Вы прицелились!")
                return

            if action == "shoot":
                chance = duel['t_acc'] if duel['turn'] == 'T' else duel['c_acc']
                roll = random.randint(1, 100)
                shooter = duel['target'] if duel['turn'] == 'T' else duel['challenger']
                victim = duel['challenger'] if duel['turn'] == 'T' else duel['target']

                if roll <= chance:
                    await callback.message.edit_text(f"🔫 БАХ! @{shooter} Убил @{victim}!")
                    end_duel(duel_id, chat_id)
                else:
                    duel['turn'] = 'C' if duel['turn'] == 'T' else 'T'
                    turn_user = duel['target'] if duel['turn'] == 'T' else duel['challenger']
                    acc = duel['t_acc'] if duel['turn'] == 'T' else duel['c_acc']
                    text = f"💨 @{shooter} промахнулся! (Шанс был {chance}%)\n\nОчередь @{turn_user}.\nВыбирайте действие:\n(Шанс попадания: {acc}%)"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Стрелять", callback_data=f"shoot:{duel_id}"),
                        InlineKeyboardButton(text="Прицелится получше", callback_data=f"aim:{duel_id}")
                    ]])
                    await callback.message.edit_text(text, reply_markup=kb)
                await callback.answer()
                return
    except Exception as e:
        print("CALLBACK ERROR:", repr(e))
    finally:
        try:
            await callback.answer()
        except Exception:
            pass


# ==== ИСПРАВЛЕННАЯ ОБЕРТКА ДЛЯ VERCEL ====
async def _async_process_update(update: Update):
    # Создаем нового бота для каждого запроса, чтобы не было ошибок "Event loop is closed"
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.feed_update(bot, update)
    finally:
        # Обязательно закрываем сессию, чтобы Vercel мог спокойно уснуть
        await bot.session.close()

def _process_update(update: Update):
    try:
        # Используем asyncio.run, так как он сам создает и корректно закрывает loop
        asyncio.run(_async_process_update(update))
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
