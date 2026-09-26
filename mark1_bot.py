import asyncio
import logging
import os
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ============================================================
# SOZLAMALAR
# ============================================================
# Token va Admin ID endi kodda emas — Render'da "Environment" bo'limida
# BOT_TOKEN va ADMIN_CHAT_ID nomli maxfiy o'zgaruvchi sifatida saqlanadi.
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN muhit o'zgaruvchisi topilmadi. "
        "Render'da Environment bo'limiga BOT_TOKEN qo'shing "
        "(lokal ishga tushirish uchun .env fayl yarating)."
    )

# Lead va operator xabarlari yuboriladigan admin chat/guruh ID'lari.
# Bir nechta admin bo'lsa, Render'da ADMIN_CHAT_ID qiymatini vergul bilan
# ajratib yozing, masalan: 7090724198,7257376381
ADMIN_CHAT_IDS: list[int] = [
    int(x.strip()) for x in os.environ.get("ADMIN_CHAT_ID", "").split(",") if x.strip()
]

WEBSITE_URL = "https://mark1.uz"

# Bir xil foydalanuvchidan takroriy lead yuborilmasligi uchun
# necha soniya (cooldown) kutish kerakligi
LEAD_COOLDOWN_SECONDS = 10 * 60  # 10 daqiqa

router = Router()

# user_id -> oxirgi lead yuborilgan vaqt (deduplication uchun)
_last_lead_time: dict[int, float] = {}

# (admin_chat_id, xabar_id) -> foydalanuvchi user_id (operator javobini
# to'g'ri odamga yuborish uchun). Bot qayta ishga tushsa tozalanadi —
# katta yuklama bo'lsa buni bazaga yozish tavsiya etiladi.
_pending_replies: dict[tuple[int, int], int] = {}


def is_duplicate_lead(user_id: int) -> bool:
    last = _last_lead_time.get(user_id)
    if last is None:
        return False
    return (time.time() - last) < LEAD_COOLDOWN_SECONDS


def mark_lead_sent(user_id: int) -> None:
    _last_lead_time[user_id] = time.time()


# ============================================================
# FSM HOLATLARI
# ============================================================
class DemoForm(StatesGroup):
    business_type = State()
    points_count = State()
    phone = State()


class OperatorQuestion(StatesGroup):
    waiting_text = State()


# ============================================================
# DOIMIY (REPLY) MENYU — pastki tugmalar
# ============================================================
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Bosh sahifa"), KeyboardButton(text="🚀 Imkoniyatlar")],
            [KeyboardButton(text="💰 Narxlar"), KeyboardButton(text="🎁 Sinab ko'rish")],
            [KeyboardButton(text="❓ Savol-javob"), KeyboardButton(text="👨‍💼 Operator")],
        ],
        resize_keyboard=True,
    )


def contact_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Kontaktni yuborish", request_contact=True)],
            [KeyboardButton(text="⬅️ Bekor qilish")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ============================================================
# INLINE MENYULAR
# ============================================================
def main_inline_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 MARK1 nima?", callback_data="menu:about")
    builder.button(text="💰 Narxlar", callback_data="menu:narxlar")
    builder.button(text="🎁 Bepul sinab ko'rish", callback_data="menu:demo")
    builder.button(text="📦 Imkoniyatlar", callback_data="menu:imkoniyatlar")
    builder.button(text="❓ Ko'p so'raladigan savollar", callback_data="menu:faq")
    builder.button(text="👨‍💼 Mutaxassis bilan bog'lanish", callback_data="menu:operator")
    builder.adjust(1)
    return builder.as_markup()


def back_to_main_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="menu:main")


def faq_list_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for faq_id, item in FAQ_ITEMS.items():
        builder.button(text=item["title"], callback_data=f"faq:{faq_id}")
    builder.adjust(1)
    builder.row(back_to_main_button())
    return builder.as_markup()


def faq_answer_keyboard(extra_buttons: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if extra_buttons:
        for btn in extra_buttons:
            builder.row(btn)
    builder.row(InlineKeyboardButton(text="⬅️ Savollar ro'yxati", callback_data="menu:faq"))
    builder.row(back_to_main_button())
    return builder.as_markup()


def simple_back_keyboard(extra_buttons: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if extra_buttons:
        for btn in extra_buttons:
            builder.row(btn)
    builder.row(back_to_main_button())
    return builder.as_markup()


# ============================================================
# FAQ MA'LUMOTLARI (texnik topshiriqdan)
# ============================================================
FAQ_ITEMS: dict[str, dict] = {
    "mark1_nima": {
        "title": "MARK1 nima?",
        "answer": (
            "MARK1 — do'kon va savdo bizneslari uchun boshqaruv tizimi.\n\n"
            "MARK1 orqali:\n"
            "🧾 savdolarni yuritish\n"
            "📦 mahsulot va qoldiqni nazorat qilish\n"
            "👥 mijozlarni boshqarish\n"
            "💰 qarzdorlarni kuzatish\n"
            "📊 daromad va foydani ko'rish\n"
            "⚠️ kam qolgan mahsulotlarni aniqlash mumkin.\n\n"
            "Ya'ni daftar, Excel va turli joylardagi hisoblarni bitta tizimga "
            "jamlashga yordam beradi."
        ),
    },
    "kimlar_uchun": {
        "title": "MARK1 kimlar uchun?",
        "answer": (
            "MARK1 asosan savdo bilan shug'ullanuvchi kichik va o'rta bizneslar uchun.\n\n"
            "Masalan:\n"
            "🏗 qurilish mollari do'koni\n"
            "📱 telefon va aksessuarlar\n"
            "👕 kiyim-kechak\n"
            "🛒 oziq-ovqat\n"
            "🔧 ehtiyot qismlar\n"
            "💄 kosmetika\n"
            "🏠 uy-ro'zg'or mahsulotlari\n"
            "va boshqa savdo nuqtalari."
        ),
    },
    "nima_foyda": {
        "title": "MARK1 menga nima foyda beradi?",
        "answer": (
            "MARK1 biznesingizda nima bo'layotganini aniq ko'rishga yordam beradi.\n\n"
            "Masalan:\n"
            "• bugun qancha savdo bo'ldi;\n"
            "• qancha foyda qilindi;\n"
            "• omborda nima va qancha qoldi;\n"
            "• qaysi mahsulot kamayib qoldi;\n"
            "• kimning qancha qarzi bor;\n"
            "• qaysi mahsulot yaxshi sotilyapti.\n\n"
            "Bularning barchasini bitta joydan nazorat qilasiz."
        ),
    },
    "ombor_nazorat": {
        "title": "Ombordagi mahsulotlarni nazorat qiladimi?",
        "answer": (
            "Ha ✅\n\n"
            "Mahsulotlarni tizimga kiritib, qoldiq miqdorini kuzatishingiz mumkin.\n\n"
            "Savdo qilinganda mahsulot qoldig'i yangilanadi va kam qolayotgan "
            "mahsulotlarni nazorat qilish osonlashadi."
        ),
    },
    "qarzdorlar": {
        "title": "Qarzdorlarni ham yuritsa bo'ladimi?",
        "answer": (
            "Ha ✅\n\n"
            "MARK1 orqali qarzdor mijozlarni, qarz miqdorini va to'lov "
            "muddatlarini nazorat qilish mumkin.\n\n"
            "Shu sabab kimdan qancha pul olish kerakligini alohida daftar yoki "
            "Telegram yozishmalaridan qidirib yurishga hojat kamayadi."
        ),
    },
    "daromad_foyda": {
        "title": "Daromad va foydani ko'rsatadimi?",
        "answer": (
            "Ha 📊\n\n"
            "Tizim savdo ma'lumotlari asosida biznes ko'rsatkichlarini "
            "ko'rishga yordam beradi.\n\n"
            "Dashboard orqali savdo, daromad, foyda va boshqa muhim "
            "ko'rsatkichlarni kuzatishingiz mumkin."
        ),
    },
    "xodimlar": {
        "title": "Bir nechta xodim ishlata oladimi?",
        "answer": (
            "MARK1 jamoaviy ishlashga moslashtirilmoqda.\n\n"
            "Agar do'koningizda bir nechta xodim yoki filial bo'lsa, bizga "
            "yozing. Sizning holatingiz uchun mavjud imkoniyatlarni "
            "tushuntirib beramiz."
        ),
        "extra_button": ("👨‍💼 Mutaxassis bilan gaplashish", "menu:operator"),
    },
    "bir_nechta_dokon": {
        "title": "2-3 ta do'konim bo'lsa ishlaydimi?",
        "answer": (
            "Bir nechta savdo nuqtasi bo'yicha talabingizni alohida ko'rib "
            "chiqamiz.\n\n"
            "Nechta do'koningiz borligini yozib qoldiring — jamoamiz sizga "
            "MARK1'dan qanday foydalanish mumkinligini tushuntiradi."
        ),
        "extra_button": ("👨‍💼 Mutaxassis bilan gaplashish", "menu:operator"),
    },
    "telefon_orqali": {
        "title": "Telefon orqali ishlaydimi?",
        "answer": (
            "MARK1 hozir veb tizim sifatida ishlaydi va brauzer orqali "
            "foydalaniladi.\n\n"
            "📱 Mobil versiya/ilova ustida ham ish olib borilmoqda."
        ),
        "webapp_button": ("🌐 MARK1'ni ochish", WEBSITE_URL),
    },
    "internet_kerak": {
        "title": "Internet bo'lmasa ishlaydimi?",
        "answer": (
            "Hozirgi MARK1 tizimidan foydalanish uchun internet aloqasi "
            "kerak.\n\n"
            "Offline ishlash imkoniyati hozircha mavjud emas."
        ),
    },
    "eski_mahsulot": {
        "title": "Eski mahsulotlarimni qanday kiritaman?",
        "answer": (
            "MARK1'ga o'tishda mahsulotlaringizni tizimga kiritish kerak "
            "bo'ladi.\n\n"
            "Agar mahsulotlaringiz ko'p bo'lsa, bizga yozing. "
            "Ma'lumotlaringiz qanday formatda saqlanganiga qarab eng qulay "
            "usulni tavsiya qilamiz."
        ),
        "extra_button": ("👨‍💼 Yordam olish", "menu:operator"),
    },
    "bilmasam": {
        "title": "Ishlatishni bilmasam-chi?",
        "answer": (
            "Muammo emas 😊\n\n"
            "MARK1 imkon qadar sodda ishlash uchun ishlab chiqilgan.\n\n"
            "Boshlashda tushunmagan joylaringiz bo'lsa, jamoamiz foydalanish "
            "bo'yicha yordam beradi."
        ),
    },
    "bepul_sinov": {
        "title": "Bepul sinab ko'rsam bo'ladimi?",
        "answer": (
            "Ha 🎁\n\n"
            "MARK1'ni avval sinab ko'rib, biznesingizga mos kelishini "
            "tekshirishingiz mumkin.\n\n"
            "👇 Sinab ko'rish uchun quyidagi tugmani bosing yoki demo "
            "so'rovini qoldiring."
        ),
        "webapp_button": ("🌐 mark1.uz", WEBSITE_URL),
        "extra_button": ("🎁 Demo so'rovi qoldirish", "menu:demo"),
    },
    "narxi": {
        "title": "Narxi qancha?",
        "answer": (
            "MARK1 tariflari biznes ehtiyojiga qarab taqdim etiladi.\n\n"
            "Amaldagi tariflar va mavjud takliflarni bilish uchun "
            "mutaxassis bilan bog'laning."
        ),
        "extra_button": ("👨‍💼 Mutaxassis bilan gaplashish", "menu:operator"),
    },
    "malumot_xavfsizligi": {
        "title": "Ma'lumotlarim yo'qolib ketmaydimi?",
        "answer": (
            "Biznes ma'lumotlari muhim ekanini tushunamiz.\n\n"
            "MARK1 ma'lumotlarni tizimli saqlash uchun ishlab chiqilgan. "
            "Texnik xavfsizlik, zaxiralash yoki ma'lumotlarni eksport "
            "qilish bo'yicha aniq shartlarni bilish uchun jamoamiz bilan "
            "bog'lanishingiz mumkin."
        ),
        "extra_button": ("👨‍💼 Mutaxassis bilan gaplashish", "menu:operator"),
    },
}

IMKONIYATLAR_TEXT = (
    "📦 <b>MARK1 imkoniyatlari</b>\n\n"
    "🧾 Savdolarni yuritish\n"
    "📦 Mahsulot va qoldiqni nazorat qilish\n"
    "👥 Mijozlarni boshqarish\n"
    "💰 Qarzdorlarni kuzatish\n"
    "📊 Daromad va foydani ko'rish\n"
    "⚠️ Kam qolgan mahsulotlarni aniqlash\n\n"
    "Barchasi — bitta tizimda, daftar va Excel'siz."
)

WELCOME_TEXT = (
    "<b>👋 MARK1'ga xush kelibsiz!</b>\n\n"
    "Do'kon va savdo biznesini bitta tizimdan boshqaring.\n\n"
    "Quyidagilardan birini tanlang 👇"
)


def build_faq_extra_buttons(item: dict) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if "webapp_button" in item:
        text, url = item["webapp_button"]
        buttons.append(InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url)))
    if "extra_button" in item:
        text, callback_data = item["extra_button"]
        buttons.append(InlineKeyboardButton(text=text, callback_data=callback_data))
    return buttons


# ============================================================
# ASOSIY HANDLERLAR
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_reply_keyboard())
    await message.answer("Bo'limni tanlang:", reply_markup=main_inline_menu())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Yordam kerakmi? Pastdagi menyudan bo'lim tanlang yoki "
        "'❓ Savol-javob' orqali FAQ'ni ko'ring.",
        reply_markup=main_reply_keyboard(),
    )


# ---- Doimiy reply-menyu tugmalari ----
@router.message(F.text == "🏠 Bosh sahifa")
async def reply_home(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_inline_menu())


@router.message(F.text == "🚀 Imkoniyatlar")
async def reply_imkoniyatlar(message: Message):
    await message.answer(IMKONIYATLAR_TEXT, reply_markup=simple_back_keyboard())


@router.message(F.text == "💰 Narxlar")
async def reply_narxlar(message: Message):
    item = FAQ_ITEMS["narxi"]
    await message.answer(item["answer"], reply_markup=simple_back_keyboard(build_faq_extra_buttons(item)))


@router.message(F.text == "🎁 Sinab ko'rish")
async def reply_demo(message: Message, state: FSMContext):
    await start_demo_form(message, state)


@router.message(F.text == "❓ Savol-javob")
async def reply_faq(message: Message):
    await message.answer("Sizni qiziqtirgan savolni tanlang 👇", reply_markup=faq_list_keyboard())


@router.message(F.text == "👨‍💼 Operator")
async def reply_operator(message: Message, state: FSMContext):
    await start_operator_question(message, state)


# ---- Inline menyu callback'lari ----
@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(WELCOME_TEXT, reply_markup=main_inline_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:about")
async def cb_about(callback: CallbackQuery):
    item = FAQ_ITEMS["mark1_nima"]
    await callback.message.edit_text(item["answer"], reply_markup=simple_back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:narxlar")
async def cb_narxlar(callback: CallbackQuery):
    item = FAQ_ITEMS["narxi"]
    await callback.message.edit_text(
        item["answer"], reply_markup=simple_back_keyboard(build_faq_extra_buttons(item))
    )
    await callback.answer()


@router.callback_query(F.data == "menu:imkoniyatlar")
async def cb_imkoniyatlar(callback: CallbackQuery):
    await callback.message.edit_text(IMKONIYATLAR_TEXT, reply_markup=simple_back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:faq")
async def cb_faq(callback: CallbackQuery):
    await callback.message.edit_text("Sizni qiziqtirgan savolni tanlang 👇", reply_markup=faq_list_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("faq:"))
async def cb_faq_item(callback: CallbackQuery):
    faq_id = callback.data.split(":", 1)[1]
    item = FAQ_ITEMS.get(faq_id)
    if not item:
        await callback.answer("Savol topilmadi", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>{item['title']}</b>\n\n{item['answer']}",
        reply_markup=faq_answer_keyboard(build_faq_extra_buttons(item)),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:demo")
async def cb_demo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_demo_form(callback.message, state, edit=True)


@router.callback_query(F.data == "menu:operator")
async def cb_operator(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_operator_question(callback.message, state)


# ============================================================
# DEMO / SINOV LEAD FORMASI (3 ta savol)
# ============================================================
async def start_demo_form(message: Message, state: FSMContext, edit: bool = False):
    if is_duplicate_lead(message.chat.id):
        text = (
            "Siz yaqinda so'rov yuborgansiz ✅\n"
            "Jamoamiz tez orada siz bilan bog'lanadi. "
            "Qayta so'rov yuborish shart emas."
        )
        if edit:
            await message.edit_text(text, reply_markup=simple_back_keyboard())
        else:
            await message.answer(text, reply_markup=main_reply_keyboard())
        return

    await state.set_state(DemoForm.business_type)
    text = "🎁 <b>Bepul sinov so'rovi</b>\n\n1️⃣ Biznesingiz turi qanday? (masalan: kiyim do'koni)"
    if edit:
        await message.edit_text(text)
    else:
        await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(DemoForm.business_type))
async def demo_business_type(message: Message, state: FSMContext):
    await state.update_data(business_type=message.text)
    await state.set_state(DemoForm.points_count)
    await message.answer("2️⃣ Nechta savdo nuqtangiz bor?")


@router.message(StateFilter(DemoForm.points_count))
async def demo_points_count(message: Message, state: FSMContext):
    await state.update_data(points_count=message.text)
    await state.set_state(DemoForm.phone)
    await message.answer(
        "3️⃣ Telefon raqamingiz?\n\n"
        "Pastdagi tugma orqali yuborishingiz ham mumkin.",
        reply_markup=contact_request_keyboard(),
    )


@router.message(StateFilter(DemoForm.phone), F.contact)
async def demo_phone_contact(message: Message, state: FSMContext):
    await finish_demo_form(message, state, phone=message.contact.phone_number)


@router.message(StateFilter(DemoForm.phone), F.text == "⬅️ Bekor qilish")
async def demo_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_reply_keyboard())


@router.message(StateFilter(DemoForm.phone), F.text)
async def demo_phone_text(message: Message, state: FSMContext):
    await finish_demo_form(message, state, phone=message.text)


async def finish_demo_form(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    await state.clear()

    if is_duplicate_lead(message.chat.id):
        await message.answer(
            "Siz yaqinda so'rov yuborgansiz ✅ Jamoamiz tez orada bog'lanadi.",
            reply_markup=main_reply_keyboard(),
        )
        return

    mark_lead_sent(message.chat.id)

    await message.answer(
        "✅ Rahmat!\n\nSo'rovingiz qabul qilindi.\nMARK1 jamoasi siz bilan bog'lanadi.",
        reply_markup=main_reply_keyboard(),
    )

    await send_lead_notification(
        message=message,
        lead_type="🎁 Demo / sinov so'rovi",
        fields={
            "Biznes turi": data.get("business_type", "-"),
            "Savdo nuqtalari soni": data.get("points_count", "-"),
            "Telefon": phone,
        },
    )


# ============================================================
# OPERATORGA UZATISH / ERKIN SAVOL
# ============================================================
async def start_operator_question(message: Message, state: FSMContext):
    await state.set_state(OperatorQuestion.waiting_text)
    await message.answer(
        "Savolingizga javob topilmadimi?\n\n"
        "Savolingizni shu yerga yozib yuboring 👇\nJamoamiz siz bilan bog'lanadi.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(OperatorQuestion.waiting_text), F.text)
async def operator_question_received(message: Message, state: FSMContext):
    await state.clear()

    if is_duplicate_lead(message.chat.id):
        await message.answer(
            "Savolingiz allaqachon qabul qilindi ✅ Jamoamiz tez orada bog'lanadi.",
            reply_markup=main_reply_keyboard(),
        )
        return

    mark_lead_sent(message.chat.id)

    await message.answer(
        "✅ Savolingiz qabul qilindi. Jamoamiz siz bilan tez orada bog'lanadi.",
        reply_markup=main_reply_keyboard(),
    )

    await send_lead_notification(
        message=message,
        lead_type="❓ Operatorga savol",
        fields={"Savol": message.text},
    )


# ============================================================
# LEAD XABARINI ADMIN CHATGA YUBORISH
# ============================================================
async def send_lead_notification(message: Message, lead_type: str, fields: dict[str, str]):
    user = message.from_user
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "🔥 <b>YANGI MARK1 LEAD</b>",
        "",
        f"📌 Turi: {lead_type}",
        f"👤 Ism: {user.full_name}",
        f"📱 Telegram: @{user.username}" if user.username else "📱 Telegram: (username yo'q)",
        f"🆔 User ID: <code>{user.id}</code>",
    ]
    for label, value in fields.items():
        lines.append(f"{label}: {value}")
    lines.append(f"🕐 Sana/vaqt: {now}")

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if user.username:
        builder.button(text="Mijozga yozish", url=f"https://t.me/{user.username}")

    text += "\n\n✍️ <i>Javob berish uchun shu xabarga Reply qiling — javobingiz mijozga bot nomidan yuboriladi.</i>"

    if ADMIN_CHAT_IDS:
        for admin_id in ADMIN_CHAT_IDS:
            try:
                sent = await message.bot.send_message(
                    admin_id, text, reply_markup=builder.as_markup() if user.username else None
                )
                # shu xabarga reply qilinganda kimga javob yuborishni bilish uchun saqlab qo'yamiz
                _pending_replies[(admin_id, sent.message_id)] = user.id
            except Exception as e:
                logging.error("Lead xabarini %s ga yuborishda xato: %s", admin_id, e)
    else:
        logging.warning("ADMIN_CHAT_ID sozlanmagan — lead hech kimga yuborilmadi:\n%s", text)


# ============================================================
# ADMIN JAVOBI — Reply qilingan lead xabariga javob yozilsa,
# bu javob mijozga BOT NOMIDAN yuboriladi (admin profili ko'rinmaydi)
# ============================================================
@router.message(F.chat.id.in_(ADMIN_CHAT_IDS), F.reply_to_message)
async def admin_reply_relay(message: Message):
    original_id = message.reply_to_message.message_id
    target_user_id = _pending_replies.get((message.chat.id, original_id))

    if target_user_id is None:
        # Bu reply bizning lead xabarimizga emas — e'tiborsiz qoldiramiz
        return

    try:
        if message.text:
            await message.bot.send_message(target_user_id, message.text)
        elif message.photo:
            await message.bot.send_photo(
                target_user_id, message.photo[-1].file_id, caption=message.caption or ""
            )
        elif message.document:
            await message.bot.send_document(
                target_user_id, message.document.file_id, caption=message.caption or ""
            )
        elif message.voice:
            await message.bot.send_voice(target_user_id, message.voice.file_id)
        else:
            await message.answer("⚠️ Bu turdagi xabarni hozircha yubora olmayman (faqat matn/rasm/fayl/ovoz).")
            return

        await message.answer("✅ Javobingiz mijozga yuborildi (profilingiz ko'rinmadi).")
    except Exception as e:
        logging.error("Javobni yuborishda xato: %s", e)
        await message.answer(
            "❌ Javobni yuborib bo'lmadi. Ehtimol foydalanuvchi botni bloklagan."
        )


# ============================================================
# NOMA'LUM MATN — FAQ yoki operator taklif qilinadi
# ============================================================
@router.message(F.text)
async def fallback_handler(message: Message):
    await message.answer(
        "Kechirasiz, bu savolga tayyor javobim yo'q 🤔\n\n"
        "Quyidagilardan birini tanlashingiz mumkin:",
        reply_markup=simple_back_keyboard(
            [InlineKeyboardButton(text="👨‍💼 Operatorga yozish", callback_data="menu:operator")]
        ),
    )


# ============================================================
# KEEP-ALIVE VEB-SERVER (Render bepul reja shuni talab qiladi)
# ============================================================
async def health(request: web.Request) -> web.Response:
    return web.Response(text="MARK1 bot ishlayapti ✅")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Keep-alive server %s portda ishga tushdi", port)


# ============================================================
# ISHGA TUSHIRISH
# ============================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)

    await start_web_server()

    print("Bot ishga tushdi ...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())