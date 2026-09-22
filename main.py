"""
МАФИЯ БОТ — ДАХУЯ РОЛЕЙ, ДАХУЯ ВСЕГО, ВСЁ В ОДНОМ ФАЙЛЕ
========================================================
aiogram 3.x, in-memory состояние (без БД), одна игра на чат.

ЗАПУСК:
    pip install aiogram==3.13.1
    export BOT_TOKEN="токен_от_батфазера"
    python mafia_bot.py

КОМАНДЫ:
    /newgame   — создать лобби в группе
    /join      — вступить в игру (или кнопка)
    /startgame — начать (только создатель, нужно от 6 игроков)
    /stopgame  — принудительно остановить игру (создатель/админ)
    /roles     — список ролей и их описание
    /myrole    — напомнить свою роль в личке во время игры

РОЛИ (набор зависит от кол-ва игроков):
    Мирный житель      — просто голосует
    Мафия               — ночью выбирает жертву вместе с командой
    Дон Мафии            — глава мафии, ночью может проверить, комиссар ли игрок
    Комиссар            — ночью проверяет, мафия ли игрок
    Доктор              — ночью лечит одного игрока (можно себя, но не 2 раза подряд)
    Маньяк               — ночью убивает в одиночку, играет сам за себя
    Путана               — блокирует ночное действие выбранного игрока
    Телохранитель        — защищает игрока, может словить пулю вместо него
    Журналист            — раз за игру публично палит роль случайного игрока
    Сержант (Мститель)   — если сержанта убивают ночью, он тянет за собой убийцу

Игра ведётся в группе (день/обсуждение/голосование), ночные действия — в личке с ботом
(поэтому все игроки должны хотя бы раз написать боту в ЛС /start, иначе бот не сможет
им написать — стандартное ограничение Telegram API).
"""

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from enum import Enum, auto

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mafia")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8957686843:AAG7Ahbb7kDStd2auqKDbPRaZwt9EUZxobE")

NIGHT_SECONDS = 45
DAY_DISCUSS_SECONDS = 60
VOTE_SECONDS = 30
MIN_PLAYERS = 6

router = Router()


# ---------------------------------------------------------------------------
# РОЛИ
# ---------------------------------------------------------------------------

class Role(Enum):
    CITIZEN = auto()
    MAFIA = auto()
    DON = auto()
    DETECTIVE = auto()
    DOCTOR = auto()
    MANIAC = auto()
    HOOKER = auto()      # путана
    BODYGUARD = auto()   # телохранитель
    JOURNALIST = auto()  # журналист
    SERGEANT = auto()    # мститель


ROLE_NAMES = {
    Role.CITIZEN: "🙂 Мирный житель",
    Role.MAFIA: "🔫 Мафия",
    Role.DON: "🎩 Дон Мафии",
    Role.DETECTIVE: "🕵️ Комиссар",
    Role.DOCTOR: "💉 Доктор",
    Role.MANIAC: "🔪 Маньяк",
    Role.HOOKER: "💋 Путана",
    Role.BODYGUARD: "🛡 Телохранитель",
    Role.JOURNALIST: "📰 Журналист",
    Role.SERGEANT: "🎖 Сержант (Мститель)",
}

ROLE_DESC = {
    Role.CITIZEN: "Не имеет ночных действий. Днём голосует, ищет мафию по разговору.",
    Role.MAFIA: "Ночью вместе с другими мафиози выбирает жертву.",
    Role.DON: "Глава мафии. Ночью может пробить, комиссар ли игрок. Голосует за жертву вместе с мафией.",
    Role.DETECTIVE: "Ночью проверяет одного игрока — мафия он или нет.",
    Role.DOCTOR: "Ночью лечит одного игрока. Нельзя лечить одного и того же 2 ночи подряд.",
    Role.MANIAC: "Играет сам за себя. Ночью убивает одного игрока. Побеждает, если остаётся один живой.",
    Role.HOOKER: "Ночью 'блокирует' игрока — он не сможет применить своё действие этой ночью.",
    Role.BODYGUARD: "Ночью защищает игрока от любого убийства (кроме голосования).",
    Role.JOURNALIST: "Раз за игру может опубликовать роль случайного живого игрока в чат.",
    Role.SERGEANT: "Если сержанта убивают ночью — убийца тоже погибает следующим утром.",
}

MAFIA_TEAM = {Role.MAFIA, Role.DON}


def build_role_pool(n: int) -> list[Role]:
    """Собирает набор ролей под количество игроков. Дахуя ролей — но с головой."""
    roles: list[Role] = []

    mafia_count = max(1, n // 4)
    roles.append(Role.DON)
    roles.extend([Role.MAFIA] * (mafia_count - 1))

    roles.append(Role.DETECTIVE)
    roles.append(Role.DOCTOR)

    if n >= 7:
        roles.append(Role.MANIAC)
    if n >= 8:
        roles.append(Role.HOOKER)
    if n >= 9:
        roles.append(Role.BODYGUARD)
    if n >= 10:
        roles.append(Role.JOURNALIST)
    if n >= 11:
        roles.append(Role.SERGEANT)

    while len(roles) < n:
        roles.append(Role.CITIZEN)

    roles = roles[:n]
    random.shuffle(roles)
    return roles


# ---------------------------------------------------------------------------
# СОСТОЯНИЕ ИГРЫ
# ---------------------------------------------------------------------------

class Phase(Enum):
    LOBBY = auto()
    NIGHT = auto()
    DAY_DISCUSS = auto()
    DAY_VOTE = auto()
    FINISHED = auto()


@dataclass
class Player:
    user_id: int
    name: str
    role: Role | None = None
    alive: bool = True
    blocked: bool = False       # заблокирован путаной этой ночью
    protected: bool = False     # защищён телохранителем этой ночью
    last_doctor_target: int | None = None
    journalist_used: bool = False


@dataclass
class Game:
    chat_id: int
    host_id: int
    players: dict[int, Player] = field(default_factory=dict)
    phase: Phase = Phase.LOBBY
    day_num: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ночные действия: role -> {actor_id: target_id}
    night_actions: dict[Role, dict[int, int]] = field(default_factory=dict)
    hooker_target: int | None = None
    doctor_target: int | None = None
    bodyguard_target: int | None = None

    votes: dict[int, int] = field(default_factory=dict)  # voter -> target
    task: asyncio.Task | None = None

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_mafia(self) -> list[Player]:
        return [p for p in self.alive_players() if p.role in MAFIA_TEAM]

    def alive_non_mafia(self) -> list[Player]:
        return [p for p in self.alive_players() if p.role not in MAFIA_TEAM and p.role != Role.MANIAC]

    def get_by_role(self, role: Role) -> list[Player]:
        return [p for p in self.alive_players() if p.role == role]


games: dict[int, Game] = {}


def player_display(p: Player) -> str:
    return p.name


def targets_kb(game: Game, actor_id: int, prefix: str, allow_self: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in game.alive_players():
        if not allow_self and p.user_id == actor_id:
            continue
        kb.button(text=player_display(p), callback_data=f"{prefix}:{game.chat_id}:{p.user_id}")
    kb.adjust(1)
    return kb.as_markup()


async def safe_send(bot: Bot, user_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except Exception as e:
        log.warning(f"Не смог написать {user_id} в ЛС: {e}")
        return False


# ---------------------------------------------------------------------------
# ЛОББИ
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Йо. Я бот для игры в Мафию с кучей ролей.\n"
        "Добавь меня в группу и там пиши /newgame чтобы создать лобби.\n"
        "Обязательно напиши мне сюда /start заранее — иначе я не смогу писать тебе роль в ЛС."
    )


@router.message(Command("newgame"))
async def cmd_newgame(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эта команда только в группе.")
        return
    if message.chat.id in games and games[message.chat.id].phase != Phase.FINISHED:
        await message.answer("Игра уже создана в этом чате. /stopgame чтобы сбросить.")
        return

    game = Game(chat_id=message.chat.id, host_id=message.from_user.id)
    host = Player(user_id=message.from_user.id, name=message.from_user.full_name)
    game.players[host.user_id] = host
    games[message.chat.id] = game

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Вступить", callback_data=f"join:{message.chat.id}")
    kb.button(text="🚀 Начать игру", callback_data=f"begin:{message.chat.id}")
    kb.adjust(2)

    await message.answer(
        f"🎭 Лобби создано игроком {host.name}!\n"
        f"Нужно минимум {MIN_PLAYERS} игроков.\n"
        f"Сейчас в игре: 1 — {host.name}\n\n"
        f"Жми кнопку, чтобы вступить.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("join:"))
async def cb_join(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    game = games.get(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await call.answer("Лобби недоступно.", show_alert=True)
        return
    uid = call.from_user.id
    if uid in game.players:
        await call.answer("Ты уже в игре.")
        return
    game.players[uid] = Player(user_id=uid, name=call.from_user.full_name)

    names = ", ".join(p.name for p in game.players.values())
    await call.message.edit_text(
        f"🎭 Лобби! Игроков: {len(game.players)} (минимум {MIN_PLAYERS})\n{names}",
        reply_markup=call.message.reply_markup,
    )
    await call.answer("Вступил!")


@router.message(Command("join"))
async def cmd_join(message: Message):
    game = games.get(message.chat.id)
    if not game or game.phase != Phase.LOBBY:
        await message.answer("Нет активного лобби. Создай через /newgame.")
        return
    uid = message.from_user.id
    if uid in game.players:
        await message.answer("Ты уже в игре.")
        return
    game.players[uid] = Player(user_id=uid, name=message.from_user.full_name)
    await message.answer(f"{message.from_user.full_name} вступил! Игроков: {len(game.players)}")


@router.message(Command("startgame"))
async def cmd_startgame(message: Message, bot: Bot):
    await try_start(games.get(message.chat.id), message.from_user.id, message, bot)


@router.callback_query(F.data.startswith("begin:"))
async def cb_begin(call: CallbackQuery, bot: Bot):
    chat_id = int(call.data.split(":")[1])
    game = games.get(chat_id)
    await call.answer()
    await try_start(game, call.from_user.id, call.message, bot)


async def try_start(game: Game | None, requester_id: int, message: Message, bot: Bot):
    if not game or game.phase != Phase.LOBBY:
        await message.answer("Нет активного лобби.")
        return
    if requester_id != game.host_id:
        await message.answer("Только создатель лобби может начать игру.")
        return
    if len(game.players) < MIN_PLAYERS:
        await message.answer(f"Мало игроков. Нужно минимум {MIN_PLAYERS}, сейчас {len(game.players)}.")
        return

    roles = build_role_pool(len(game.players))
    for p, r in zip(game.players.values(), roles):
        p.role = r

    fails = []
    for p in game.players.values():
        text = (
            f"Твоя роль: <b>{ROLE_NAMES[p.role]}</b>\n{ROLE_DESC[p.role]}"
        )
        if p.role in MAFIA_TEAM:
            teammates = [t.name for t in game.players.values() if t.role in MAFIA_TEAM and t.user_id != p.user_id]
            if teammates:
                text += f"\n\nТвоя команда: {', '.join(teammates)}"
        ok = await safe_send(bot, p.user_id, text, parse_mode=ParseMode.HTML)
        if not ok:
            fails.append(p.name)

    if fails:
        await message.answer(
            "⚠️ Не смог написать в ЛС: " + ", ".join(fails) +
            "\nПусть напишут мне /start в личку, иначе роль не получат."
        )

    role_summary = "\n".join(f"— {ROLE_NAMES[r]}" for r in roles)
    await message.answer(
        f"🎬 Игра началась! Игроков: {len(game.players)}\nРоли в игре:\n{role_summary}"
    )
    await start_night(game, bot)


@router.message(Command("stopgame"))
async def cmd_stopgame(message: Message):
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Нет игры в этом чате.")
        return
    if message.from_user.id != game.host_id:
        await message.answer("Только создатель может остановить игру.")
        return
    if game.task:
        game.task.cancel()
    games.pop(message.chat.id, None)
    await message.answer("Игра остановлена.")


@router.message(Command("roles"))
async def cmd_roles(message: Message):
    text = "\n\n".join(f"{ROLE_NAMES[r]}\n{ROLE_DESC[r]}" for r in Role)
    await message.answer(text)


@router.message(Command("myrole"))
async def cmd_myrole(message: Message):
    if message.chat.type != "private":
        await message.answer("Напиши мне это в личку.")
        return
    for game in games.values():
        p = game.players.get(message.from_user.id)
        if p and p.role:
            status = "жив" if p.alive else "мёртв"
            await message.answer(f"Роль: {ROLE_NAMES[p.role]} ({status})")
            return
    await message.answer("Ты сейчас не в игре.")


# ---------------------------------------------------------------------------
# НОЧЬ
# ---------------------------------------------------------------------------

async def start_night(game: Game, bot: Bot):
    game.phase = Phase.NIGHT
    game.day_num += 1
    game.night_actions = {}
    game.hooker_target = None
    game.doctor_target = None
    game.bodyguard_target = None
    for p in game.players.values():
        p.blocked = False
        p.protected = False

    await bot.send_message(
        game.chat_id,
        f"🌙 Ночь #{game.day_num}. Город засыпает... У ночных ролей {NIGHT_SECONDS} сек на решение.\n"
        f"Проверьте личные сообщения от бота.",
    )

    # рассылаем клавиатуры ролям с ночными действиями
    mafias = game.get_by_role(Role.MAFIA) + game.get_by_role(Role.DON)
    for p in mafias:
        kb = targets_kb(game, p.user_id, "act_mafia", allow_self=False)
        await safe_send(bot, p.user_id, "🔫 Выбери, кого убить этой ночью:", reply_markup=kb)

    for p in game.get_by_role(Role.DON):
        kb = targets_kb(game, p.user_id, "act_don_check", allow_self=False)
        await safe_send(bot, p.user_id, "🎩 (доп.) Хочешь проверить, комиссар ли кто-то?", reply_markup=kb)

    for p in game.get_by_role(Role.DETECTIVE):
        kb = targets_kb(game, p.user_id, "act_detective", allow_self=False)
        await safe_send(bot, p.user_id, "🕵️ Кого проверить на мафию?", reply_markup=kb)

    for p in game.get_by_role(Role.DOCTOR):
        kb = targets_kb(game, p.user_id, "act_doctor", allow_self=True)
        await safe_send(bot, p.user_id, "💉 Кого лечить этой ночью?", reply_markup=kb)

    for p in game.get_by_role(Role.MANIAC):
        kb = targets_kb(game, p.user_id, "act_maniac", allow_self=False)
        await safe_send(bot, p.user_id, "🔪 Кого убить этой ночью?", reply_markup=kb)

    for p in game.get_by_role(Role.HOOKER):
        kb = targets_kb(game, p.user_id, "act_hooker", allow_self=False)
        await safe_send(bot, p.user_id, "💋 Кого заблокировать этой ночью?", reply_markup=kb)

    for p in game.get_by_role(Role.BODYGUARD):
        kb = targets_kb(game, p.user_id, "act_bodyguard", allow_self=True)
        await safe_send(bot, p.user_id, "🛡 Кого защищать этой ночью?", reply_markup=kb)

    game.task = asyncio.create_task(night_timer(game, bot))


async def night_timer(game: Game, bot: Bot):
    try:
        await asyncio.sleep(NIGHT_SECONDS)
        async with game.lock:
            if game.phase == Phase.NIGHT:
                await resolve_night(game, bot)
    except asyncio.CancelledError:
        pass


def register_action(game: Game, role: Role, actor_id: int, target_id: int):
    game.night_actions.setdefault(role, {})[actor_id] = target_id


@router.callback_query(F.data.startswith("act_"))
async def cb_night_action(call: CallbackQuery, bot: Bot):
    parts = call.data.split(":")
    action, chat_id, target_id = parts[0], int(parts[1]), int(parts[2])
    game = games.get(chat_id)
    if not game or game.phase != Phase.NIGHT:
        await call.answer("Ночь уже закончилась.", show_alert=True)
        return
    actor = game.players.get(call.from_user.id)
    if not actor or not actor.alive:
        await call.answer("Ты не можешь действовать.", show_alert=True)
        return

    async with game.lock:
        if action == "act_mafia" and actor.role in MAFIA_TEAM:
            register_action(game, Role.MAFIA, actor.user_id, target_id)
        elif action == "act_don_check" and actor.role == Role.DON:
            register_action(game, Role.DON, actor.user_id, target_id)
        elif action == "act_detective" and actor.role == Role.DETECTIVE:
            register_action(game, Role.DETECTIVE, actor.user_id, target_id)
        elif action == "act_doctor" and actor.role == Role.DOCTOR:
            register_action(game, Role.DOCTOR, actor.user_id, target_id)
        elif action == "act_maniac" and actor.role == Role.MANIAC:
            register_action(game, Role.MANIAC, actor.user_id, target_id)
        elif action == "act_hooker" and actor.role == Role.HOOKER:
            register_action(game, Role.HOOKER, actor.user_id, target_id)
        elif action == "act_bodyguard" and actor.role == Role.BODYGUARD:
            register_action(game, Role.BODYGUARD, actor.user_id, target_id)
        else:
            await call.answer("Это не твоё действие.", show_alert=True)
            return

    await call.answer("Принято ✅")
    await call.message.edit_text(f"{call.message.text}\n\n✅ Выбор сделан.")

    # если все ночные роли уже проголосовали — можно завершить ночь досрочно
    if all_night_actions_done(game):
        async with game.lock:
            if game.phase == Phase.NIGHT:
                if game.task:
                    game.task.cancel()
                await resolve_night(game, bot)


def all_night_actions_done(game: Game) -> bool:
    needed = []
    if game.get_by_role(Role.MAFIA) or game.get_by_role(Role.DON):
        needed.append(Role.MAFIA)
    for role in (Role.DETECTIVE, Role.DOCTOR, Role.MANIAC, Role.HOOKER, Role.BODYGUARD):
        if game.get_by_role(role):
            needed.append(role)
    for role in needed:
        alive_actors = {p.user_id for p in game.get_by_role(role)} if role != Role.MAFIA else {
            p.user_id for p in (game.get_by_role(Role.MAFIA) + game.get_by_role(Role.DON))
        }
        done = set(game.night_actions.get(role, {}).keys())
        if not alive_actors.issubset(done):
            return False
    return True


async def resolve_night(game: Game, bot: Bot):
    game.phase = Phase.DAY_DISCUSS  # промежуточно, чтобы кнопки инвалидировались

    hooker_actions = game.night_actions.get(Role.HOOKER, {})
    if hooker_actions:
        target_id = random.choice(list(hooker_actions.values()))
        game.hooker_target = target_id
        p = game.players.get(target_id)
        if p:
            p.blocked = True

    bodyguard_actions = game.night_actions.get(Role.BODYGUARD, {})
    if bodyguard_actions:
        target_id = list(bodyguard_actions.values())[0]
        game.bodyguard_target = target_id
        p = game.players.get(target_id)
        if p and not p.blocked:
            p.protected = True

    doctor_actions = game.night_actions.get(Role.DOCTOR, {})
    healed_id = None
    for doc_id, target_id in doctor_actions.items():
        doc = game.players[doc_id]
        if doc.blocked:
            continue
        if doc.last_doctor_target == target_id:
            continue  # нельзя лечить того же 2 раза подряд
        healed_id = target_id
        doc.last_doctor_target = target_id

    mafia_actions = game.night_actions.get(Role.MAFIA, {})
    mafia_kill_id = None
    if mafia_actions:
        votes = list(mafia_actions.values())
        mafia_kill_id = max(set(votes), key=votes.count)
        if game.players[mafia_actions and list(mafia_actions.keys())[0]].blocked and len(mafia_actions) == 1:
            mafia_kill_id = None

    maniac_actions = game.night_actions.get(Role.MANIAC, {})
    maniac_kill_id = None
    for man_id, target_id in maniac_actions.items():
        man = game.players[man_id]
        if not man.blocked:
            maniac_kill_id = target_id

    deaths: list[Player] = []
    revenge_needed = False

    def try_kill(target_id: int | None, killer_role: Role | None = None):
        nonlocal revenge_needed
        if target_id is None:
            return
        p = game.players.get(target_id)
        if not p or not p.alive:
            return
        if p.protected:
            return
        if healed_id == target_id:
            return
        p.alive = False
        deaths.append(p)
        if p.role == Role.SERGEANT:
            revenge_needed = True

    try_kill(mafia_kill_id, Role.MAFIA)
    try_kill(maniac_kill_id, Role.MANIAC)

    # Мститель тянет за собой убийцу (просто: убиваем случайного живого мафиози/маньяка)
    if revenge_needed:
        killers = game.alive_mafia() + game.get_by_role(Role.MANIAC)
        if killers:
            victim = random.choice(killers)
            victim.alive = False
            deaths.append(victim)

    don_checks = game.night_actions.get(Role.DON, {})
    for don_id, target_id in don_checks.items():
        don = game.players[don_id]
        if don.blocked:
            continue
        target = game.players.get(target_id)
        is_det = target and target.role == Role.DETECTIVE
        await safe_send(
            bot, don_id,
            f"🎩 Проверка: {target.name if target else '???'} — "
            f"{'КОМИССАР!' if is_det else 'не комиссар.'}"
        )

    det_checks = game.night_actions.get(Role.DETECTIVE, {})
    for det_id, target_id in det_checks.items():
        det = game.players[det_id]
        if det.blocked:
            continue
        target = game.players.get(target_id)
        is_mafia = target and target.role in MAFIA_TEAM
        await safe_send(
            bot, det_id,
            f"🕵️ Проверка: {target.name if target else '???'} — "
            f"{'МАФИЯ!' if is_mafia else 'не мафия.'}"
        )

    # утренний отчёт
    if deaths:
        names = ", ".join(f"{p.name} ({ROLE_NAMES[p.role]})" for p in deaths)
        await bot.send_message(game.chat_id, f"☀️ Утро наступило. Этой ночью погибли: {names}")
    else:
        await bot.send_message(game.chat_id, "☀️ Утро наступило. Этой ночью никто не погиб!")

    if await check_win(game, bot):
        return

    await start_day(game, bot)


# ---------------------------------------------------------------------------
# ДЕНЬ И ГОЛОСОВАНИЕ
# ---------------------------------------------------------------------------

async def start_day(game: Game, bot: Bot):
    game.phase = Phase.DAY_DISCUSS
    alive = game.alive_players()
    names = ", ".join(p.name for p in alive)
    await bot.send_message(
        game.chat_id,
        f"💬 День #{game.day_num}. Обсуждение {DAY_DISCUSS_SECONDS} сек.\n"
        f"Живые ({len(alive)}): {names}",
    )
    game.task = asyncio.create_task(day_discuss_timer(game, bot))


async def day_discuss_timer(game: Game, bot: Bot):
    try:
        await asyncio.sleep(DAY_DISCUSS_SECONDS)
        async with game.lock:
            if game.phase == Phase.DAY_DISCUSS:
                await start_vote(game, bot)
    except asyncio.CancelledError:
        pass


async def start_vote(game: Game, bot: Bot):
    game.phase = Phase.DAY_VOTE
    game.votes = {}
    kb = InlineKeyboardBuilder()
    for p in game.alive_players():
        kb.button(text=player_display(p), callback_data=f"vote:{game.chat_id}:{p.user_id}")
    kb.button(text="🤷 Воздержаться", callback_data=f"vote:{game.chat_id}:0")
    kb.adjust(1)
    await bot.send_message(
        game.chat_id,
        f"🗳 Голосование! У вас {VOTE_SECONDS} сек, чтобы выбрать, кого казнить.",
        reply_markup=kb.as_markup(),
    )
    game.task = asyncio.create_task(vote_timer(game, bot))


async def vote_timer(game: Game, bot: Bot):
    try:
        await asyncio.sleep(VOTE_SECONDS)
        async with game.lock:
            if game.phase == Phase.DAY_VOTE:
                await resolve_vote(game, bot)
    except asyncio.CancelledError:
        pass


@router.callback_query(F.data.startswith("vote:"))
async def cb_vote(call: CallbackQuery, bot: Bot):
    _, chat_id, target_id = call.data.split(":")
    chat_id, target_id = int(chat_id), int(target_id)
    game = games.get(chat_id)
    if not game or game.phase != Phase.DAY_VOTE:
        await call.answer("Голосование закрыто.", show_alert=True)
        return
    voter = game.players.get(call.from_user.id)
    if not voter or not voter.alive:
        await call.answer("Мертвые не голосуют.", show_alert=True)
        return
    async with game.lock:
        game.votes[voter.user_id] = target_id
    await call.answer("Голос принят ✅")

    if len(game.votes) >= len(game.alive_players()):
        async with game.lock:
            if game.phase == Phase.DAY_VOTE:
                if game.task:
                    game.task.cancel()
                await resolve_vote(game, bot)


async def resolve_vote(game: Game, bot: Bot):
    game.phase = Phase.FINISHED  # промежуточно
    tally: dict[int, int] = {}
    for target_id in game.votes.values():
        if target_id == 0:
            continue
        tally[target_id] = tally.get(target_id, 0) + 1

    if not tally:
        await bot.send_message(game.chat_id, "Город воздержался — никто не казнён.")
    else:
        top = max(tally.values())
        leaders = [uid for uid, c in tally.items() if c == top]
        if len(leaders) > 1:
            await bot.send_message(game.chat_id, "Голоса разделились поровну — никто не казнён.")
        else:
            victim = game.players[leaders[0]]
            victim.alive = False
            await bot.send_message(
                game.chat_id,
                f"⚖️ Город решил казнить {victim.name}. Его роль была: {ROLE_NAMES[victim.role]}",
            )

    if await check_win(game, bot):
        return

    await start_night(game, bot)


# ---------------------------------------------------------------------------
# ПРОВЕРКА ПОБЕДЫ
# ---------------------------------------------------------------------------

async def check_win(game: Game, bot: Bot) -> bool:
    alive = game.alive_players()
    mafia = [p for p in alive if p.role in MAFIA_TEAM]
    maniac = [p for p in alive if p.role == Role.MANIAC]
    others = [p for p in alive if p.role not in MAFIA_TEAM and p.role != Role.MANIAC]

    winner_text = None
    if not mafia and not maniac:
        winner_text = "🏆 Мирные жители победили! Вся мафия и маньяк устранены."
    elif maniac and len(alive) <= 1:
        winner_text = f"🏆 Маньяк ({maniac[0].name}) победил, оставшись единственным живым!"
    elif mafia and len(mafia) >= len(others) and not maniac:
        winner_text = "🏆 Мафия победила! Их количество сравнялось с мирными."
    elif not others and not maniac and mafia:
        winner_text = "🏆 Мафия победила! Город захвачен."

    if winner_text:
        game.phase = Phase.FINISHED
        reveal = "\n".join(f"{p.name} — {ROLE_NAMES[p.role]}" for p in game.players.values())
        await bot.send_message(game.chat_id, f"{winner_text}\n\nВсе роли:\n{reveal}")
        games.pop(game.chat_id, None)
        return True

    game.phase = Phase.DAY_DISCUSS  # временно, поменяется в start_night/start_day
    return False


# ---------------------------------------------------------------------------
# JOURNALIST — можно вызвать командой днём (раз за игру)
# ---------------------------------------------------------------------------

@router.message(Command("reveal"))
async def cmd_reveal(message: Message, bot: Bot):
    game = games.get(message.chat.id)
    if not game or game.phase not in (Phase.DAY_DISCUSS, Phase.DAY_VOTE):
        await message.answer("Сейчас не время для этого.")
        return
    p = game.players.get(message.from_user.id)
    if not p or p.role != Role.JOURNALIST or not p.alive:
        return
    if p.journalist_used:
        await message.answer("Ты уже использовал свою способность.")
        return
    candidates = [x for x in game.alive_players() if x.user_id != p.user_id]
    if not candidates:
        return
    target = random.choice(candidates)
    p.journalist_used = True
    await message.answer(f"📰 Журналист раскрывает: {target.name} — {ROLE_NAMES[target.role]}!")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
