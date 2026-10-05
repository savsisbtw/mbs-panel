import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import chains
import db
import features
import payments
import settings
import webhooks
import xray_manager
from config import BOT_TOKEN, ADMIN_IDS, SUB_DOMAIN, SITE_DOMAIN

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mbs-bot")

db.init_db()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_bot_username: str | None = None


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def main_menu_kb(tg_id: int) -> InlineKeyboardMarkup:
    rows = []
    if settings.get_features()["trial_enabled"] and db.trial_available(tg_id):
        rows.append([InlineKeyboardButton(text="Попробовать бесплатно", callback_data="trial:start")])
    rows += [
        [InlineKeyboardButton(text="Получить VPN", callback_data="menu:get")],
        [InlineKeyboardButton(text="Моя подписка", callback_data="menu:mysub")],
        [InlineKeyboardButton(text="Промокод", callback_data="menu:promo")],
        [InlineKeyboardButton(text="Пригласить друга", callback_data="menu:referral")],
        [InlineKeyboardButton(text="О сервисе", callback_data="menu:about")],
    ]
    if is_admin(tg_id):
        rows.append([InlineKeyboardButton(text="Админка", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username


def nodes_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for n in db.list_nodes(enabled_only=True):
        rows.append([InlineKeyboardButton(text=n["label"], callback_data=f"{prefix}:{n['code']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb(prefix: str, node_code: str, tg_id: int | None = None) -> InlineKeyboardMarkup:
    payments_enabled = settings.get_payment_settings()["payments_enabled"]
    promo = db.get_pending_promo(tg_id) if tg_id else None
    rows = []
    for p in settings.get_plans():
        if payments_enabled and p["price"] > 0:
            final = db.discounted_price(p["price"], promo)
            label = f"{p['label']} — {final} ₽" if final == p["price"] else f"{p['label']} — {final} ₽ (было {p['price']})"
        else:
            label = p["label"]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{node_code}:{p['code']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:get")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


DIVIDER = "───────────────"


def sub_url_for(token: str) -> str:
    return f"https://{SUB_DOMAIN}/sub/{token}"


def connect_kb(token: str, extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Подключиться", url=sub_url_for(token))]]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_text() -> str:
    return (
        f"<b>{settings.get_brand_name()}</b>\n\n"
        "Быстрый и незаметный доступ без границ. Протокол VLESS+Reality "
        "маскируется под обычный HTTPS-трафик, ничем не палится.\n\n"
        f"{DIVIDER}\n"
        f"Сайт: {SITE_DOMAIN}"
    )


async def send_main_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_kb(message.from_user.id))


@dp.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    payload = command.args or ""
    if payload.startswith("ref_") or payload.startswith("ref-"):
        ref_code = payload[4:]
        referrer = db.get_user_by_ref_code(ref_code)
        if referrer and settings.get_referral_settings()["enabled"]:
            db.set_referred_by(message.from_user.id, referrer["tg_id"])
        return await send_main_menu(message)
    if payload.startswith("gift_") or payload.startswith("gift-"):
        code = payload[5:]
        gift, err = db.redeem_gift_code(code, message.from_user.id)
        if err == "not_found":
            await message.answer("Такого подарочного кода не существует.")
            return await send_main_menu(message)
        if err == "already_used":
            await message.answer("Этот код уже был использован.")
            return await send_main_menu(message)
        plan = settings.get_plans_by_code().get(gift["plan"])
        gift_node = db.get_node(gift["node"])
        if not plan or not gift_node:
            await message.answer("Этот подарок больше недоступен.")
            return await send_main_menu(message)
        sub = db.create_subscription(message.from_user.id, gift["node"], plan["days"], plan["code"], source="gift", )
        await asyncio.to_thread(xray_manager.add_client_to_node, gift_node, sub["uuid"], email=sub["uuid"])
        await message.answer(
            f"<b>Подарок активирован</b>\n\n"
            f"Сервер: {gift_node['label']}\n"
            f"Срок: {plan['label']}\n\n"
            f"{DIVIDER}\n"
            f"Ссылка-подписка:\n<code>{sub_url_for(user['token'])}</code>",
            reply_markup=connect_kb(user["token"]),
        )
        return await send_main_menu(message)
    await send_main_menu(message)


@dp.message(CommandStart())
async def start_plain(message: Message):
    db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Привет! Это бот {settings.get_brand_name()}.\nВыбери действие ниже.",
    )
    await send_main_menu(message)


@dp.callback_query(F.data == "menu:main")
async def cb_menu_main(cb: CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb(cb.from_user.id))
    await cb.answer()


@dp.callback_query(F.data == "menu:about")
async def cb_about(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:main")]])
    await cb.message.edit_text(about_text(), reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "menu:referral")
async def cb_referral(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:main")]])
    ref_settings = settings.get_referral_settings()
    if not ref_settings["enabled"]:
        await cb.message.edit_text("Реферальная программа сейчас отключена.", reply_markup=kb)
        return await cb.answer()
    user = db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    stats = db.referral_stats(cb.from_user.id)
    username = await get_bot_username()
    link = f"https://t.me/{username}?start=ref_{user['ref_code']}"
    days = ref_settings["bonus_days"]
    text = (
        f"<b>Пригласи друга</b>\n\n"
        f"За каждого друга, который активирует подписку по твоей ссылке, "
        f"вы <b>оба</b> получаете +{days} дн. к подписке.\n\n"
        f"{DIVIDER}\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {stats['referred_count']}\n"
    )
    if stats["bonus_days_pending"]:
        text += f"Накоплено бонусных дней (зачислятся при следующей подписке): {stats['bonus_days_pending']}\n"
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "menu:get")
async def cb_get(cb: CallbackQuery):
    await cb.message.edit_text("Выбери сервер:", reply_markup=nodes_kb("node"))
    await cb.answer()


@dp.callback_query(F.data.startswith("node:"))
async def cb_node(cb: CallbackQuery):
    node_code = cb.data.split(":")[1]
    await cb.message.edit_text("Выбери срок:", reply_markup=plans_kb("plan", node_code, cb.from_user.id))
    await cb.answer()


def providers_kb(node_code: str, plan_code: str) -> InlineKeyboardMarkup:
    rows = []
    for p in payments.available_providers():
        rows.append([InlineKeyboardButton(text=payments.PROVIDER_NAMES[p], callback_data=f"pay:{p}:{node_code}:{plan_code}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"node:{node_code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(cb: CallbackQuery):
    _, node_code, plan_code = cb.data.split(":")
    plan = settings.get_plans_by_code()[plan_code]
    db.get_or_create_user(cb.from_user.id, cb.from_user.username)

    promo = db.get_pending_promo(cb.from_user.id)
    final_price = db.discounted_price(plan["price"], promo)
    if settings.get_payment_settings()["payments_enabled"] and final_price > 0 and payments.available_providers():
        price_line = f"{final_price} ₽" if final_price == plan["price"] else f"{final_price} ₽ (скидка по промокоду {promo['code']})"
        await cb.message.edit_text(
            f"<b>{plan['label']}</b> — {price_line}\n\nВыбери способ оплаты:",
            reply_markup=providers_kb(node_code, plan_code),
        )
        return await cb.answer()
    if promo and settings.get_payment_settings()["payments_enabled"] and plan["price"] > 0 and final_price == 0:
        db.consume_promo(promo["code"], cb.from_user.id)
        db.set_promo_pending(cb.from_user.id, None)

    user = db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    sub = db.create_subscription(
        cb.from_user.id, node_code, plan["days"], plan_code, source="bot",
        traffic_limit=settings.default_traffic_limit_bytes(),
    )
    node_row = db.get_node(node_code)
    await asyncio.to_thread(xray_manager.add_client_to_node, node_row, sub["uuid"], email=sub["uuid"])
    kb = connect_kb(user["token"], extra_rows=[
        [InlineKeyboardButton(text="Моя подписка", callback_data="menu:mysub")],
        [InlineKeyboardButton(text="В меню", callback_data="menu:main")],
    ])
    await cb.message.edit_text(
        f"<b>Подписка активна</b>\n\n"
        f"Сервер: {node_row['label']}\n"
        f"Срок: {plan['label']} — до {sub['expires_at'][:10]}\n\n"
        f"{DIVIDER}\n"
        f"Ссылка-подписка:\n<code>{sub_url_for(user['token'])}</code>",
        reply_markup=kb,
    )
    await cb.answer("Подписка выдана")


@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery):
    _, provider, node_code, plan_code = cb.data.split(":")
    plan = settings.get_plans_by_code()[plan_code]
    node_row = db.get_node(node_code)
    payment_id = payments.new_payment_id()
    promo = db.get_pending_promo(cb.from_user.id)
    final_price = db.discounted_price(plan["price"], promo)
    db.create_payment(payment_id, cb.from_user.id, node_code, plan_code, provider, final_price)
    if promo and final_price != plan["price"]:
        db.set_payment_promo(payment_id, promo["code"], plan["price"])
        db.set_promo_pending(cb.from_user.id, None)
    try:
        external_id, pay_url = payments.create_payment_link(
            provider, payment_id, final_price, f"{settings.get_brand_name()} — {node_row['label']}, {plan['label']}",
        )
    except Exception:
        log.exception("payment creation failed")
        db.mark_payment_failed(payment_id)
        return await cb.answer("Не получилось создать платёж, попробуй позже", show_alert=True)
    db.set_payment_external(payment_id, external_id, pay_url)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=pay_url)],
        [InlineKeyboardButton(text="Назад", callback_data=f"plan:{node_code}:{plan_code}")],
    ])
    await cb.message.edit_text(
        f"Счёт на {final_price} ₽ создан.\nПосле оплаты подписка выдастся автоматически.",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "menu:mysub")
async def cb_mysub(cb: CallbackQuery):
    user = db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    subs = db.list_active_subscriptions(tg_id=cb.from_user.id)
    if not subs:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню", callback_data="menu:main")]])
        await cb.message.edit_text("У тебя пока нет активных подписок.", reply_markup=kb)
        return await cb.answer()
    lines = ["<b>Твои подписки</b>\n"]
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    for s in subs:
        plan = plans_by_code.get(s["plan"], {}).get("label", "Пробный" if s["plan"] == "trial" else s["plan"])
        node_info = nodes_by_code.get(s["node"])
        node = node_info["label"] if node_info else s["node"]
        lines.append(f"{node} — {plan}, до {s['expires_at'][:10]}\nТрафик: {features.traffic_text(s)}")
    lines.append(f"\n{DIVIDER}\nСсылка-подписка:\n<code>{sub_url_for(user['token'])}</code>")
    kb = connect_kb(user["token"], extra_rows=[[InlineKeyboardButton(text="В меню", callback_data="menu:main")]])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    await cb.answer()


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать гифт-ссылку", callback_data="admin:gift")],
        [InlineKeyboardButton(text="Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="Синхронизировать xray", callback_data="admin:sync")],
        [InlineKeyboardButton(text="В меню", callback_data="menu:main")],
    ])


@dp.callback_query(F.data == "menu:admin")
async def cb_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Админ-панель:", reply_markup=admin_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "admin:gift")
async def cb_admin_gift(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Для какого сервера гифт?", reply_markup=nodes_kb("admin:giftnode"))
    await cb.answer()


@dp.callback_query(F.data.startswith("admin:giftnode:"))
async def cb_admin_giftnode(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    node_code = cb.data.split(":")[2]
    await cb.message.edit_text("На какой срок?", reply_markup=plans_kb("admin:giftmake", node_code))
    await cb.answer()


@dp.callback_query(F.data.startswith("admin:giftmake:"))
async def cb_admin_giftmake(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    _, _, node_code, plan_code = cb.data.split(":")
    code = db.create_gift_code(node_code, plan_code, cb.from_user.id)
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    link = f"https://t.me/{_bot_username}?start=gift_{code}"
    plan = settings.get_plans_by_code()[plan_code]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В админку", callback_data="menu:admin")]])
    await cb.message.edit_text(
        f"Гифт-ссылка готова ({db.get_node(node_code)['label']}, {plan['label']}):\n\n"
        f"<code>{link}</code>\n\nОткрывший её (даже впервые) сразу получит подписку.",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    s = db.stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В админку", callback_data="menu:admin")]])
    await cb.message.edit_text(
        f"Пользователей: {s['users']}\n"
        f"Активных подписок: {s['active_subscriptions']}\n"
        f"Всего подписок: {s['total_subscriptions']}\n"
        f"Гифт-кодов создано: {s['gifts_created']} / использовано: {s['gifts_used']}",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "admin:sync")
async def cb_admin_sync(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    result = await asyncio.to_thread(xray_manager.sync_all)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В админку", callback_data="menu:admin")]])
    await cb.message.edit_text(
        f"Синхронизация xray выполнена.\nАктивно клиентов: {result['active_now']}\n"
        f"Убрано истёкших: {result['removed_expired']}\nБыл перезапуск: {'да' if result['reloaded'] else 'нет'}",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "trial:start")
async def cb_trial(cb: CallbackQuery):
    feats = settings.get_features()
    user = db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    if not feats["trial_enabled"] or not db.trial_available(cb.from_user.id):
        return await cb.answer("Пробный период недоступен", show_alert=True)
    node_row = features.pick_trial_node()
    if not node_row:
        return await cb.answer("Сейчас нет доступных серверов", show_alert=True)
    if not db.claim_trial(cb.from_user.id):
        return await cb.answer("Пробный период уже использован", show_alert=True)
    limit = feats["trial_traffic_gb"] * settings.GB if feats["trial_traffic_gb"] > 0 else None
    sub = db.create_subscription(
        cb.from_user.id, node_row["code"], feats["trial_days"], "trial", source="trial", traffic_limit=limit,
    )
    await asyncio.to_thread(xray_manager.add_client_to_node, node_row, sub["uuid"], email=sub["uuid"])
    traffic_line = f"\nТрафик: до {feats['trial_traffic_gb']} ГБ" if limit else ""
    kb = connect_kb(user["token"], extra_rows=[[InlineKeyboardButton(text="В меню", callback_data="menu:main")]])
    await cb.message.edit_text(
        f"<b>Пробный период активен</b>\n\n"
        f"Сервер: {node_row['label']}\n"
        f"Срок: до {sub['expires_at'][:10]}{traffic_line}\n\n"
        f"{DIVIDER}\n"
        f"Ссылка-подписка:\n<code>{sub_url_for(user['token'])}</code>",
        reply_markup=kb,
    )
    await cb.answer("Пробный период выдан")
    await asyncio.to_thread(webhooks.send, "subscription.trial", {
        "tg_id": cb.from_user.id, "node": node_row["code"], "subscription_uuid": sub["uuid"],
        "expires_at": sub["expires_at"],
    })


PROMO_ERRORS = {
    "not_found": "Такого промокода нет.",
    "expired": "Срок действия промокода закончился.",
    "exhausted": "Этот промокод уже использован максимальное число раз.",
    "already_used": "Ты уже использовал этот промокод.",
}


async def apply_promo_code(message: Message, raw_code: str):
    code = (raw_code or "").strip()
    if not code:
        return await message.answer("Напиши промокод так: /promo КОД")
    db.get_or_create_user(message.from_user.id, message.from_user.username)
    promo, err = db.validate_promo(code, message.from_user.id)
    if err:
        return await message.answer(PROMO_ERRORS.get(err, "Промокод не подошёл."))
    if promo["kind"] == "days":
        redeemed, err = db.redeem_days_promo(code, message.from_user.id)
        if err:
            return await message.answer(PROMO_ERRORS.get(err, "Промокод не подошёл."))
        return await message.answer(f"Промокод принят: +{promo['value']} дн. к подписке.")
    db.set_promo_pending(message.from_user.id, promo["code"])
    what = f"{promo['value']}%" if promo["kind"] == "percent" else f"{promo['value']} ₽"
    await message.answer(f"Промокод принят: скидка {what}. Она применится на следующей оплате, выбери срок в меню.")


@dp.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    await apply_promo_code(message, command.args or "")


@dp.callback_query(F.data == "menu:promo")
async def cb_promo_hint(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:main")]])
    await cb.message.edit_text("Отправь команду с кодом, например:\n<code>/promo КОД</code>", reply_markup=kb)
    await cb.answer()


async def notify_limit_reached(subs: list):
    for sub in subs:
        try:
            await bot.send_message(
                sub["tg_id"],
                "<b>Лимит трафика исчерпан</b>\n\nДоступ приостановлен. Продли подписку или напиши в поддержку, "
                "чтобы получить ещё трафик.",
            )
        except Exception:
            log.exception("failed to notify about traffic limit")
        await asyncio.to_thread(webhooks.send, "subscription.limit_reached", {
            "tg_id": sub["tg_id"], "subscription_uuid": sub["uuid"], "node": sub["node"],
            "limit": sub["traffic_limit"], "used": sub["traffic_used"],
        })


async def send_expiry_reminders():
    if not settings.get_features()["reminders_enabled"]:
        return
    for sub, kind, stage in features.reminders_due():
        left = "3 дня" if stage == "3d" else "сутки"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продлить", callback_data="menu:get")]])
        try:
            await bot.send_message(
                sub["tg_id"],
                f"<b>Подписка скоро закончится</b>\n\nДо конца осталось меньше чем {left} "
                f"(до {sub['expires_at'][:10]}). Продли заранее, чтобы доступ не прерывался.",
                reply_markup=kb,
            )
        except Exception:
            log.exception("failed to send expiry reminder")
        features.mark_stage_sent(sub["uuid"], sub["expires_at"])


_node_state: dict = {}


async def check_nodes_and_alert():
    if not settings.get_features()["node_alerts_enabled"]:
        return
    for node in db.list_nodes(enabled_only=True):
        if node["kind"] == "local" or node["status"] != "active" or not node.get("address"):
            continue
        samples = await asyncio.to_thread(chains.tcp_connect_ms, node["address"], node["port"], 2, 3.0)
        alive = chains.median_ms(samples) is not None
        previous = _node_state.get(node["code"])
        _node_state[node["code"]] = alive
        if previous is None or previous == alive:
            continue
        text = (
            f"Нода «{node['label']}» ({node['address']}) снова доступна."
            if alive else f"Нода «{node['label']}» ({node['address']}) не отвечает."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                log.exception("failed to send node alert")
        await asyncio.to_thread(webhooks.send, "node.up" if alive else "node.down", {
            "node": node["code"], "label": node["label"], "address": node["address"],
        })


async def reconcile_pending_payments():
    if not settings.get_payment_settings()["payments_enabled"]:
        return
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    for payment in db.list_payments():
        if payment["status"] != "pending" or not payment.get("external_id"):
            continue
        try:
            status = await asyncio.to_thread(payments.check_payment_status, payment["provider"], payment["external_id"])
        except Exception:
            continue
        if status in payments.PAID_STATUSES:
            plan = plans_by_code.get(payment["plan"])
            node_row = nodes_by_code.get(payment["node"])
            if not plan or not node_row:
                continue
            granted = db.mark_payment_paid(payment["id"])
            if not granted:
                continue
            sub = db.create_subscription(
                payment["tg_id"], payment["node"], plan["days"], payment["plan"], source="payment",
                traffic_limit=settings.default_traffic_limit_bytes(),
            )
            await asyncio.to_thread(xray_manager.add_client_to_node, node_row, sub["uuid"], email=sub["uuid"])
            user = db.get_or_create_user(payment["tg_id"], None)
            try:
                await bot.send_message(
                    payment["tg_id"],
                    f"<b>Оплата получена</b>\n\n"
                    f"Сервер: {node_row['label']}\n"
                    f"Срок: {plan['label']} — до {sub['expires_at'][:10]}\n\n"
                    f"Ссылка-подписка:\n{sub_url_for(user['token'])}",
                )
            except Exception:
                log.exception("failed to notify user about payment")
            await asyncio.to_thread(webhooks.send, "payment.paid", {
                "tg_id": payment["tg_id"],
                "amount": payment["amount"],
                "provider": payment["provider"],
                "node": payment["node"],
                "plan": payment["plan"],
                "subscription_uuid": sub["uuid"],
                "expires_at": sub["expires_at"],
            })
        elif status in payments.FAILED_STATUSES:
            db.mark_payment_failed(payment["id"])


async def periodic_sync():
    tick = 0
    while True:
        if tick % 3 == 0:
            try:
                exceeded = await asyncio.to_thread(features.update_traffic_and_find_exceeded)
                if exceeded:
                    await notify_limit_reached(exceeded)
            except Exception:
                log.exception("traffic accounting failed")
        try:
            await asyncio.to_thread(xray_manager.sync_all)
        except Exception:
            log.exception("periodic sync failed")
        if tick % 20 == 0:
            try:
                await send_expiry_reminders()
            except Exception:
                log.exception("expiry reminders failed")
        if tick % 2 == 0:
            try:
                await check_nodes_and_alert()
            except Exception:
                log.exception("node alerts failed")
        tick += 1
        try:
            await reconcile_pending_payments()
        except Exception:
            log.exception("payment reconciliation failed")
        try:
            db.delete_expired_admin_sessions()
            db.delete_expired_pending_totp()
            db.delete_old_login_attempts()
        except Exception:
            log.exception("expired admin session cleanup failed")
        await asyncio.sleep(90)


async def main():
    global _bot_username
    me = await bot.get_me()
    _bot_username = me.username
    log.info("Bot started as @%s", _bot_username)
    asyncio.create_task(periodic_sync())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
