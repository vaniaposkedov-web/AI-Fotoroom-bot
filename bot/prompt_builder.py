"""Сборка финального текстового промпта из настроек БД и ответов пользователя FSM."""
from __future__ import annotations

import logging
import re
import aiosqlite

from bot.db import Database

logger = logging.getLogger(__name__)


def _slugify_label(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", (s or "").lower())


# Подсказки: нормализованный текст внутри {…} → ключи в data (первое непустое значение)
_PLACEHOLDER_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("рукав", ("sleeve", "own_sleeve")),
    ("размер", ("size", "own_size")),
    ("рост", ("height", "height_cm")),
    ("длин", ("length", "length_cm", "own_length")),
    ("крой", ("pants_style", "own_cut")),
    ("поза", ("pose", "info_pose")),
    ("сезон", ("season", "info_season")),
    ("праздник", ("holiday", "info_holiday")),
    ("пол", ("gender", "info_gender", "rand_gender", "child_gender")),
    ("возраст", ("age",)),
    ("ракурс", ("view", "info_angle")),
    ("дистанц", ("dist", "info_dist")),
    ("стиль", ("style",)),
    ("локац", ("rand_location", "rand_location_indoor", "rand_location_outdoor", "rand_location_custom")),
    ("бренд", ("info_brand", "product_name")),
    ("товар", ("product_name", "info_brand")),
    ("язык", ("info_lang",)),
    ("нагружен", ("info_load",)),
    ("доп", ("info_extra", "extra_info")),
    ("дополнитель", ("info_extra", "extra_info")),
    ("aspect", ("aspect", "aspect_label")),
    ("формат", ("aspect", "aspect_label")),
]


_BRACE_FILL_SKIP_KEYS = frozenset({
    "category", "cloth", "index", "model_id", "prompt_id", "is_preset", "random_mode",
    "random_other_mode", "normal_gen_mode", "infographic_mode", "own_mode", "storefront_mode",
    "photos", "downloaded_paths", "last_photos_msg_id", "last_sent_prompt", "repeat_mode",
    "model_select", "model_select_label", "display_category", "current_step_id", "current_step_key",
    "current_step_index", "is_going_back", "cloth_label", "gender_label", "photo_label",
})


def fill_remaining_brace_placeholders(text: str, data: dict) -> str:
    """Подставляет оставшиеся {любой текст} из значений data по синонимам и имени ключа."""
    if not text or "{" not in text:
        return text

    def lookup_by_slug(slug: str) -> str:
        for dk, dv in data.items():
            if dk in _BRACE_FILL_SKIP_KEYS or dv is None or dv == "":
                continue
            if isinstance(dv, (dict, list)):
                continue
            if isinstance(dv, str) and dv.startswith("AgAC"):
                continue
            dks = _slugify_label(str(dk))
            if len(dks) < 2:
                continue
            if slug == dks or slug in dks or dks in slug:
                return str(dv)
        for stem, keys in _PLACEHOLDER_SYNONYMS:
            if stem not in slug:
                continue
            for k in keys:
                v = data.get(k)
                if v is None or v == "":
                    continue
                if isinstance(v, str) and v.startswith("AgAC"):
                    continue
                return str(v)
        return ""

    def repl(m: re.Match) -> str:
        inner = (m.group(1) or "").strip()
        if not inner:
            return m.group(0)
        slug = _slugify_label(inner)
        if not slug:
            return m.group(0)
        val = lookup_by_slug(slug)
        return val if val else "none"

    return re.sub(r"\{([^}]+)\}", repl, text)


def _contains_user_value(text: str, value: str) -> bool:
    haystack = re.sub(r"\s+", " ", (text or "")).strip().lower()
    needle = re.sub(r"\s+", " ", (value or "")).strip().lower()
    return bool(needle and needle in haystack)


async def build_final_prompt(data: dict, db: Database) -> str:
    category = data.get("category")

    category_prompt_map = {
        "storefront": "storefront_prompt",
        "whitebg": "whitebg_prompt",
        "own_variant": "own_variant_prompt",
        "random": "random_prompt",
        "random_other": "random_other_prompt",
        "infographic_clothing": "infographic_clothing_prompt",
        "infographic_other": "infographic_other_prompt",
        "own": "own_prompt",
        "collage": "collage_prompt",
        "photostudio_wb": "photostudio_wb_prompt",
    }

    prompt_text = ""
    if data.get("random_mode"):

        prompt_text = ""
    elif category in ("female", "male", "child", "boy", "girl", "presets", "own") and data.get('prompt_id'):

        pid = data.get('prompt_id')
        model_prompt = await db.get_prompt_text(int(pid)) if pid else ""
        prompt_text = model_prompt

        if category == "presets":
            presets_base = await db.get_app_setting("presets_prompt") or ""
            if presets_base:
                prompt_text += "\n\n" + presets_base
        elif category == "own":
            own_base = await db.get_app_setting("own_prompt") or ""
            if own_base:
                prompt_text += "\n\n" + own_base
    elif category in category_prompt_map:

        prompt_key = category_prompt_map[category]
        prompt_text = await db.get_app_setting(prompt_key) or ""
    else:

        pid = data.get('prompt_id')
        prompt_text = await db.get_prompt_text(int(pid)) if pid else ""

    age_key = data.get('age')
    age_map = {
        "20_26": "Молодая модель возраста 20-26 лет",
        "30_38": "Взрослая модель возраста 30-38 лет",
        "40_48": "Зрелая модель возраста 40-48 лет",
        "55_60": "Пожилая модель возраста 55-60 лет",
    }
    age_text = age_map.get(age_key, age_key or "")
    # Берем данные с учетом "Своего варианта"
    def _none_if_empty(value) -> str:
        txt = str(value or "").strip()
        return txt if txt else "none"

    sleeve_text = _none_if_empty(data.get('sleeve') or data.get('own_sleeve'))
    size_text = _none_if_empty(data.get('size') or data.get('own_size'))
    cut_text = _none_if_empty(data.get('pants_style') or data.get('own_cut'))

    if size_text and str(size_text).isdigit():
        sz = int(size_text)
        size_text = f"size {sz}"

    age_key = data.get('age')
    age_map = {
        "20_26": "Молодая модель возраста 20-26 лет",
        "30_38": "Взрослая модель возраста 30-38 лет",
        "40_48": "Зрелая модель возраста 40-48 лет",
        "55_60": "Пожилая модель возраста 55-60 лет",
    }
    age_text = age_map.get(age_key, age_key or "")
    length_raw = str(data.get("length") or data.get("length_cm") or data.get("own_length") or "")
    length_en = ""
    if length_raw:
        len_en_map = {
            "Короткий топ": "short crop top length",
            "Обычный топ": "regular top length",
            "До талии": "waist length",
            "Ниже талии": "below waist length",
            "До середины ягодиц": "mid-buttocks length",
            "До середины бедра": "mid-thigh length",
            "До колен": "knee length",
            "Ниже колен": "below knee length",
            "Миди": "midi length",
            "До щиколоток": "ankle length",
            "До пола": "floor length"
        }
        length_en = len_en_map.get(length_raw, "")

    length_final = _none_if_empty(f"{length_raw} ({length_en})" if length_en else length_raw)

    view_key = data.get("view") or data.get("info_angle")
    view_word = {"close": "близкий", "far": "дальний", "medium": "средний", "back": "сзади", "front": "спереди",
                 "side": "сбоку"}.get(view_key, "спереди")

    dist_key = data.get("dist") or data.get("info_dist")
    dist_word = {"far": "дальний", "medium": "средний", "close": "близкий"}.get(dist_key, "средний")

    gender_val = data.get("gender") or data.get("info_gender") or data.get("rand_gender")
    gender_word = {"male": "мужчина", "female": "женщина", "boy": "мальчик", "girl": "девочка",
                   "unisex": "унисекс"}.get(gender_val, "")

    replacements = {
        "{размер}": size_text, "{Размер модели}": size_text, "{Размер тела модели}": size_text,
        "(ТУТ УКАЗЫВАЕМ РАЗМЕР МОДЕЛИ)": size_text, "(ТУТ УКАЗЫВАЕМ Размер модели}": size_text,

        "{рост}": str(data.get("height") or data.get("height_cm") or ""),
        "{Рост модели}": str(data.get("height") or data.get("height_cm") or ""),
        "(ТУТ УКАЗЫВАЕМ РОСТ МОДЕЛИ)": str(data.get("height") or data.get("height_cm") or ""),
        "(ТУТ УКАЗЫВАЕМ Рост модели}": str(data.get("height") or data.get("height_cm") or ""),

        "{длина изделия}": length_final,
        "{длину изделия}": length_final,
        "{Длина изделия}": length_final,
        "(ТУТ УКАЗЫВАЕМ ДЛИНУ ИЗДЕЛИЯ)": length_final,
        "(ТУТ УКАЗЫВАЕМ длину изделия}": length_final,

        "{возраст}": age_text, "{Возраст модели}": age_text,
        "(ТУТ УКАЗЫВАЕМ ВОЗРАСТ МОДЕЛИ)": age_text, "(ТУТ УКАЗЫВАЕМ Возраст модели}": age_text,

        "{длина рукав}": sleeve_text, "{Тип рукава}": sleeve_text, "{длина рукава}": sleeve_text,
        "(ТУТ УКАЗЫВАЕМ ТИП РУКАВОВ": sleeve_text, "(ТУТ УКАЗЫВАЕМ Тип рукава}": sleeve_text,

        "{сзади/спереди}": view_word, "{Угол камеры}": view_word, "{Ракурс}": view_word,
        "{Вид фотографии}": view_word,
        "(ТУТ УКАЗЫВАЕМ ВИД ФОТОГРАФИИ СПЕРЕДИ \СЗАДИ)": view_word, "(ТУТ УКАЗЫВАЕМ Угол камеры}": view_word,

        "{ракурс фотографии}": dist_word, "{Дистанция}": dist_word,
        "(ТУТ УКАЗЫВАЕМ РАКУРС ФОТОГРАФИИ)": dist_word, "(ТУТ УКАЗЫВАЕМ ракурс фотографии}": dist_word,

        "{Пол модели}": gender_word, "{пол}": gender_word, "{Пол}": gender_word,
        "(ТУТ УКАЗЫВАЕМ УКАЗЫВАЕМ ПОЛ)": gender_word, "(ТУТ УКАЗЫВАЕМ ПОВТОРНО ПОЛ МОДЕЛИ)": gender_word,
        "(ТУТ УКАЗЫВАЕМ ПОВТОРНО Пол модели}": gender_word, "(ТУТ УКАЗЫВАЕМ Пол модели)": gender_word,
        "(ТУТ УКАЗЫВАЕМ  УКАЗЫВАЕМ ПОЛ)": gender_word,

        "{Название товара}": str(data.get("product_name") or data.get("info_brand") or ""),
        "{brand}": str(data.get("info_brand") or ""),
        "{Название бренда}": str(data.get("info_brand") or ""),
        "{Укажите название брендатовара}": str(data.get("product_name") or data.get("info_brand") or ""),
        "{Укажите название бренда товара}": str(data.get("product_name") or data.get("info_brand") or ""),

        "{Нагруженность}": str(data.get("info_load") or ""),
        "{Нагруженность инфографики}": str(data.get("info_load") or ""),
        "(ТУТ УКАЗЫВАЕМ НАГРУЖЕННОСТЬ)": str(data.get("info_load") or ""),
        "(ТУТ УКАЗЫВАЕМ Нагруженность инфографики}": str(data.get("info_load") or ""),

        "{Язык}": str(data.get("info_lang") or ""),
        "{Язык инфографики}": str(data.get("info_lang") or ""),
        "(ТУТ УКАЗЫВАЕМ ЯЗЫК ИНФОГРАФИКИ)": str(data.get("info_lang") or ""),

        "{Формат фото}": str(data.get("aspect") or data.get("aspect_label") or ""),
        "{aspect}": str(data.get("aspect") or data.get("aspect_label") or "1:1"),
        "{Фото фона}": "AgAC..." if data.get("own_bg_photo_id") else "",

        "{Стиль}": str(data.get("style") or ""),
        "{Стиль локации}": str(data.get("style") or ""),
        "(ТУТ УКАЗЫВАЕМ СТИЛЬ)": str(data.get("style") or ""),

        "{Сезон}": str(data.get("season") or data.get("info_season") or ""),
        "(ТУТ УКАЗЫВАЕМ СЕЗОН)": str(data.get("season") or data.get("info_season") or ""),
        "(ТУТ УКАЗЫВАЕМ Сезон}": str(data.get("season") or data.get("info_season") or ""),

        "{Праздник}": str(data.get("holiday") or data.get("info_holiday") or ""),
        "(ТУТ УКАЗЫВАЕМ Праздник}": str(data.get("holiday") or data.get("info_holiday") or ""),

        "{Поза}": str(data.get("pose") or data.get("info_pose") or ""),
        "{Поза модели}": str(data.get("pose") or data.get("info_pose") or ""),
        "(ТУТ УКАЗЫВАЕМ ПОЗУ МОДЕЛИ)": str(data.get("pose") or data.get("info_pose") or ""),
        "(ТУТ УКАЗЫВАЕМ Поза модели}": str(data.get("pose") or data.get("info_pose") or ""),

        "{Тип кроя}": _none_if_empty(cut_text),
        "{Тип кроя штанов}": _none_if_empty(cut_text),
        "(ТУТ УКАЗЫВАЕМ ТИП КРОЯ ШТАНОВ)": _none_if_empty(data.get("pants_style") or data.get("own_cut")),
        "(ТУТ УКАЗЫВАЕМ Тип кроя штанов}": _none_if_empty(data.get("pants_style") or data.get("own_cut")),

        "{Ширина}": str(data.get("width_cm") or ""),
        "(ТУТ УКАЗЫВАЕМ ШИРИНУ)": str(data.get("width_cm") or ""),

        "{Высота}": str(data.get("height_cm") or ""),
        "(ТУТ УКАЗЫВАЕМ ВЫСОТУ)": str(data.get("height_cm") or ""),

        "{Длина}": str(data.get("length_cm") or ""),
        "(ТУТ УКАЗЫВАЕМ ДЛИНУ)": str(data.get("length_cm") or ""),

        "{Человек}": "Yes" if str(data.get("has_person")).lower() == "yes" else "No",
        "{Присутствует ли человек на фото}": "Yes" if str(data.get("has_person")).lower() == "yes" else "No",
        "(ТУТ УКАЗЫВАЕМ Присутствует ли человек на фото)": "Yes" if str(
            data.get("has_person")).lower() == "yes" else "No",

        "{Тип локации}": str(
            data.get("rand_location") or data.get("rand_location_indoor") or data.get("rand_location_outdoor") or ""),
        "(ТУТ УКАЗЫВАЕМ Тип локации}": str(
            data.get("rand_location") or data.get("rand_location_indoor") or data.get("rand_location_outdoor") or ""),
    }

    advs = [data.get("info_adv1"), data.get("info_adv2"), data.get("info_adv3")]
    replacements["{Преимущества}"] = ", ".join([a for a in advs if a])
    replacements["{Преимущество 1}"] = str(data.get("info_adv1") or "")
    replacements["{Преимущество 2}"] = str(data.get("info_adv2") or "")
    replacements["{Преимущество 3}"] = str(data.get("info_adv3") or "")
    replacements["{Топ 1 преимущества товара}"] = str(data.get("info_adv1") or "")
    replacements["{Топ 2 преимущества товара}"] = str(data.get("info_adv2") or "")
    replacements["{Топ 3 преимущества товара}"] = str(data.get("info_adv3") or "")
    replacements["(ТУТ УКАЗЫВАЕМ ПРИМУЩЕСТВО 1)"] = str(data.get("info_adv1") or "")
    replacements["(ТУТ УКАЗЫВАЕМ ПРИМУЩЕСТВО 2)"] = str(data.get("info_adv2") or "")
    replacements["(ТУТ УКАЗЫВАЕМ ПРИМУЩЕСТВО 3)"] = str(data.get("info_adv3") or "")
    # Поле "Дополнительная информация о продукте" может прийти под двумя ключами:
    #   info_extra  - устаревший FSM-обработчик waiting_info_extra (инфографика)
    #   extra_info  - динамический шаг из конструктора (library_steps), напр. в "Свой вариант модели"
    # Берём первое непустое, чтобы плейсхолдеры заполнялись в обоих сценариях.
    _extra_info_raw = str(data.get("info_extra") or data.get("extra_info") or "")
    _extra_info_value = _none_if_empty(_extra_info_raw)
    replacements["{Доп информация}"] = _extra_info_value
    replacements["{Дополнительная информация о продукте}"] = _extra_info_value
    replacements["{ℹ️ Дополнительная информация о продукте}"] = _extra_info_value
    replacements["{extra_info}"] = _extra_info_value
    replacements["{info_extra}"] = _extra_info_value
    replacements["(ТУТ УКАЗЫВАЕМ ДОП ЧТО УГОДНО О ТОВАРЕ)"] = _extra_info_value

    def apply_replacements(text: str) -> str:
        if not text: return ""
        res = text

        sorted_keys = sorted(replacements.keys(), key=len, reverse=True)
        for k in sorted_keys:
            v = _none_if_empty(replacements[k])

            if k.startswith("{") and k.endswith("}"):
                res = re.sub(re.escape(k), str(v), res, flags=re.IGNORECASE)
            else:
                res = res.replace(k, str(v))
        return res

    used_placeholders = set()
    for k in replacements.keys():
        pass

    prompt_filled = ""
    strict_admin_prompt_mode = False

    if category == "photostudio_wb":
        # Только промпт из админки + подстановка его собственных плейсхолдеров.
        # Никаких служебных дописок — в модель уходит ровно шаблон + данные + фото.
        # Общий fill_remaining_brace_placeholders здесь НЕ применяется: он подставил бы
        # в {category} внутренний ключ "photostudio_wb", а в незаполненные — "none".
        specs_bits = []
        if data.get("length"):
            specs_bits.append(f"length: {data['length']}")
        if data.get("sleeve"):
            specs_bits.append(f"sleeve: {data['sleeve']}")
        if data.get("pants_style"):
            specs_bits.append(f"cut and fit: {data['pants_style']}")
        if data.get("photostudio_details"):
            specs_bits.append(f"client notes: {data['photostudio_details']}")
        specs = "; ".join(specs_bits) or "not specified — follow the attached product photo"

        quality = str(data.get("selected_quality") or "1k").lower()
        ps_values = {
            "{type}": "see the attached product photo",
            "{category}": "marketplace product listing (Wildberries/Ozon)",
            "{specs}": specs,
            "{format}": str(data.get("aspect") or "4:5"),
            "{resolution}": "4K" if quality == "4k" else "1K",
        }
        text = prompt_text
        for ph, val in ps_values.items():
            text = text.replace(ph, val)
        return text.strip()
    if data.get("own_mode") or category == "own":

        own_from_admin = (await db.get_app_setting("own_prompt") or "").strip()
        base = own_from_admin or (await db.get_own_prompt() or "") or (await db.get_own_prompt3() or "")
        if not base.strip():
            base = """STRICT FASHION REDRESS TASK:
Generate ONE SINGLE IMAGE. NO COLLAGES. NO SIDE-BY-SIDE. NO REPETITION.

INPUT DATA:
1. [SCENE_AND_MODEL_REFERENCE_IMAGE]: The model, face, pose, and background.
2. [CLOTHING_ITEM_TO_WEAR_IMAGE]: The NEW item to put on the model.

CORE RULES:
- IDENTITY: Keep the EXACT face and body of the person from [SCENE_AND_MODEL_REFERENCE_IMAGE].
- POSE: Keep the EXACT pose from [SCENE_AND_MODEL_REFERENCE_IMAGE].
- BACKGROUND: Keep the EXACT environment from [SCENE_AND_MODEL_REFERENCE_IMAGE].
- TOTAL OUTFIT OVERHAUL: You MUST DISCARD ALL clothing items (tops, bottoms, shoes, accessories) from [SCENE_AND_MODEL_REFERENCE_IMAGE]. 
- NEW STYLING: Put the item from [CLOTHING_ITEM_TO_WEAR_IMAGE] on the person. 
- COMPLETE THE LOOK: If [CLOTHING_ITEM_TO_WEAR_IMAGE] is only a top, you MUST generate NEW matching pants/skirt and shoes that perfectly fit the style of the new item. 
- STYLISH OUTFIT: Avoid making the entire outfit monochromatic or the same color as the new item. Use complementary colors, different materials, and stylish textures for the pants and shoes to create a professional fashion-forward look. DO NOT reuse the pants from the original photo.
- FIDELITY: The new item must look 100% identical to [CLOTHING_ITEM_TO_WEAR_IMAGE] in texture and silhouette.
- HIGH-END QUALITY: The new product must look perfectly ironed, clean, and brand new. Sharp focus on fabric details, textures, and professional cinematic lighting.

FORMAT:
- Aspect Ratio: {aspect}
- Requirement: Fill the frame. ZERO BORDERS.

🎯 TARGET: A high-end, luxury marketplace photo where the model from [SCENE_AND_MODEL_REFERENCE_IMAGE] is wearing a COMPLETELY NEW OUTFIT with superior commercial quality."""
            logger.info("[PROMPT] own: default fallback used (admin prompt is empty)")
        else:
            logger.info("[PROMPT] own: using %s prompt, length=%d", "admin" if own_from_admin else "fallback-db",
                        len(base))
        prompt_filled = apply_replacements(base)

        # ВАЖНО: Включаем строгий режим. Код больше НЕ БУДЕТ приписывать
        # параметры (Additional details) и свои команды в конец вашего текста!
        strict_admin_prompt_mode = True


    elif category == "own_variant":

        own_variant_from_admin = (await db.get_app_setting("own_variant_prompt") or "").strip()

        base = own_variant_from_admin or (await db.get_own_variant_prompt() or "")

        if not base.strip():

            base = "Make a realistic fashion photography."

            logger.info("[PROMPT] own_variant: default fallback used")

        else:

            logger.info("[PROMPT] own_variant: using admin prompt")

        prompt_filled = apply_replacements(base)
        mapping_block = (
            "\n\nINPUT MAPPING (STRICT):\n"
            "PHOTO 1 = BACKGROUND REFERENCE.\n"
            "PHOTO 2 = PRODUCT REFERENCE (SOURCE OF TRUTH).\n"
            "If any optional field is skipped, treat its value as 'none'."
        )
        if "PHOTO 1" not in prompt_filled.upper() or "PHOTO 2" not in prompt_filled.upper():
            prompt_filled = (prompt_filled + mapping_block).strip()

        # ВАЖНО: Включаем строгий режим, чтобы бот не дописывал параметры в конец текста!

        strict_admin_prompt_mode = True

    elif data.get("random_other_mode"):

        base_random_other = await db.get_app_setting("random_other_prompt") or await db.get_random_other_prompt()
        if base_random_other:
            prompt_filled = apply_replacements(base_random_other)
        else:
            has_person = data.get("has_person")
            load = data.get("info_load")
            product_name = data.get("product_name")
            h_cm = data.get("height_cm");
            w_cm = data.get("width_cm");
            l_cm = data.get("length_cm")
            season = data.get("season")
            style = data.get("style")

            p_parts = ["Professional commercial product photography. High quality, ultra realistic lighting. "]
            p_parts.append(f"Product: {product_name}. ")
            if has_person:
                p_parts.append(f"A {gender_word} is in the scene with the product. ")
            else:
                p_parts.append("No people in the shot, focus strictly on the product itself. ")
            p_parts.append(f"Infographic load: {load}/10. ")
            p_parts.append(f"Camera angle: {view_word}, Distance: {dist_word}. ")
            dims = []
            if h_cm: dims.append(f"height {h_cm}cm")
            if w_cm: dims.append(f"width {w_cm}cm")
            if l_cm: dims.append(f"length {l_cm}cm")
            if dims: p_parts.append(f"Product dimensions: {', '.join(dims)}. ")
            if season: p_parts.append(f"Season/Vibe: {season}. ")
            if style: p_parts.append(f"Style: {style}. ")
            p_parts.append("8k resolution, cinematic lighting, sharp focus on product.")
            prompt_filled = "".join(p_parts)

    elif data.get("normal_gen_mode"):
        prompt_filled = data.get("prompt") or ""

    elif data.get("random_mode"):

        base_random = await db.get_app_setting("random_prompt") or await db.get_random_prompt()
        if base_random and "{" in base_random:
            prompt_filled = apply_replacements(base_random)
        else:

            p_parts = ["Professional commercial fashion photography. High quality, realistic lighting. "]
            p_parts.append(f"Model: {gender_word or 'person'}. ")
            if data.get("age"): p_parts.append(f"Age: {data.get('age')}. ")
            if size_text: p_parts.append(f"Body type: {size_text}. ")
            h = data.get("height")
            if h: p_parts.append(f"Height: {h}cm. ")
            b_type = data.get("body_type")
            if b_type: p_parts.append(f"Body density score: {b_type}/10. ")

            loc = data.get("rand_location") or data.get("rand_location_indoor") or data.get("rand_location_outdoor")
            loc_map = {
                "inside_restaurant": "внутри ресторана", "photo_studio": "в фотостудии", "coffee_shop": "в кофейне",
                "city": "в городе", "building": "у здания", "wall": "у стены", "park": "в парке",
                "coffee_shop_out": "у кофейни", "forest": "в лесу", "car": "у машины", "restaurant": "в ресторане",
                "room": "в комнате", "office": "в офисе", "mall": "в торговом центре", "cafe": "у кофейни"
            }
            if loc:
                if loc == 'custom':
                    custom = (data.get('rand_location_custom') or '').strip()
                    if custom: p_parts.append(f"Location: {custom}. ")
                else:
                    p_parts.append(f"Location: {loc_map.get(loc, loc)}. ")

            if data.get("pants_style"): p_parts.append(f"Pants cut: {data.get('pants_style')}. ")
            if data.get("sleeve"): p_parts.append(f"Sleeve type: {data.get('sleeve')}. ")
            L = (data.get("length") or "").strip()
            if L: p_parts.append(f"Garment length: {L}. ")
            if data.get("pose"): p_parts.append(f"Pose: {data.get('pose')}. ")
            p_parts.append(f"Camera distance: {dist_word}. View: {view_word}. ")
            if data.get("season"): p_parts.append(f"Season: {data.get('season')}. ")
            if data.get("holiday"): p_parts.append(f"Occasion/Holiday: {data.get('holiday')}. ")
            p_parts.append("8k resolution, cinematic lighting, professional studio look.")
            prompt_filled = ((base_random or "") + "\n\n" + "".join(p_parts)).strip()

    elif category == "whitebg":

        base_whitebg = (await db.get_app_setting("whitebg_prompt") or await db.get_whitebg_prompt() or "").strip()
        if base_whitebg:
            prompt_filled = base_whitebg
            strict_admin_prompt_mode = True
        else:
            prompt_filled = "High-end commercial product photography on a pure white background. Perfectly ironed, clean, professional studio lighting, 8k resolution, sharp focus on fabric details and texture."


    elif category == "collage" or data.get("collage_mode"):

        base_collage = (await db.get_app_setting("collage_prompt") or "").strip()
        if base_collage:
            prompt_filled = apply_replacements(base_collage)
        else:
            prompt_filled = (
                "You are a professional AI fashion photoshoot and apparel transfer model.\n\n"
                "Your task is to create ONE ultra realistic 4-frame collage from:\n"
                "1) one reference photo of a human model,\n"
                "2) product photos of a clothing or footwear item.\n\n"
                "Preserve the model exactly as in the original reference image: same face, same identity, "
                "same body shape, same proportions, same skin texture, same hairstyle, same age, same ethnicity, same anatomy.\n\n"
                "Generate EXACTLY ONE IMAGE containing EXACTLY 4 frames:\n"
                "Frame 1: full front view. Frame 2: full back view. Frame 3: side profile view. "
                "Frame 4: close-up detailed shot focusing on product details, fabric texture, stitching.\n\n"
                "The model must pose like a real professional fashion model. Dynamic posing, asymmetric posing, "
                "natural movement, premium fashion body language.\n\n"
                "Hands must remain fully visible, natural, elegant, fashion-oriented. NEVER place hands inside pockets.\n\n"
                "Style: premium ecommerce photoshoot, ultra realistic, highly detailed, clean fashion campaign aesthetic. "
                "Professional studio lighting, soft realistic shadows, clean minimal premium studio background."
            )
        strict_admin_prompt_mode = True

    elif category == "storefront":

        model_prompt = ""

        model_id = data.get("model_id")

        if model_id:

            try:

                async with aiosqlite.connect(db._db_path) as conn:

                    async with conn.execute(

                            """

                            SELECT p.text

                            FROM models m

                            JOIN prompts p ON p.id = m.prompt_id

                            WHERE m.id = ? AND m.category = 'storefront'

                            """,

                            (int(model_id),),

                    ) as cur:
                        row = await cur.fetchone()

                        if row and row[0]:
                            model_prompt = str(row[0]).strip()

            except Exception:

                model_prompt = ""

        if not model_prompt:

            model_pid = data.get("prompt_id")

            if model_pid:

                try:

                    model_prompt = (await db.get_prompt_text(int(model_pid)) or "").strip()

                except Exception:

                    model_prompt = ""

        storefront_from_admin = (await db.get_app_setting("storefront_prompt") or "").strip()

        base_storefront = model_prompt or storefront_from_admin or (await db.get_storefront_prompt() or "")

        # МАКСИМАЛЬНО ЖЕСТКИЙ И КОРОТКИЙ ПРОМТ

        # Берем промпт из базы (выбранной модели витрины или общий)
        prompt_filled = apply_replacements(base_storefront)
        strict_admin_prompt_mode = True

    elif data.get("infographic_mode"):

        if category == "infographic_clothing":
            base_info = await db.get_app_setting(
                "infographic_clothing_prompt") or await db.get_infographic_clothing_prompt()
        else:
            base_info = await db.get_app_setting("infographic_other_prompt") or await db.get_infographic_other_prompt()
        if base_info and "{" in base_info:
            prompt_filled = apply_replacements(base_info)
        else:
            p_parts = [
                "Professional commercial product photography with infographic elements. High quality, 8k resolution. "]
            brand = data.get("info_brand")
            if brand: p_parts.append(f"Product/Brand name: {brand}. ")
            load = data.get("info_load")
            if load: p_parts.append(f"Infographic design complexity level: {load}/10. ")
            lang_val = data.get("info_lang")
            if lang_val: p_parts.append(f"Text language: {lang_val}. ")
            advs_clean = [a for a in advs if a]
            if advs_clean: p_parts.append(f"Key advantages to highlight: {', '.join(advs_clean)}. ")
            extra = data.get("info_extra") or data.get("extra_info")
            if extra: p_parts.append(f"Additional text: {extra}. ")
            p_parts.append(f"Camera angle: {view_word}, Distance: {dist_word}. ")

            if data.get("has_person"):
                pose = data.get("info_pose")
                p_parts.append(
                    f"Model: {gender_word or 'person'}, Age: {age_text or 'adult'}. Pose: {pose or 'natural'}. ")
            else:
                p_parts.append("No people in the shot, focus strictly on the product. ")

            season = data.get("info_season")
            holiday = data.get("info_holiday")
            if season: p_parts.append(f"Season/Atmosphere: {season}. ")
            if holiday: p_parts.append(f"Occasion/Holiday: {holiday}. ")

            if category == "infographic_clothing":
                if size_text: p_parts.append(f"Clothing size: {size_text}. ")
                if data.get("height"): p_parts.append(f"Model height: {data.get('height')}cm. ")
                if data.get("body_type"): p_parts.append(f"Model body type score: {data.get('body_type')}/10. ")
                if data.get("pants_style"): p_parts.append(f"Pants cut: {data.get('pants_style')}. ")
                if sleeve_text: p_parts.append(f"Sleeve type: {sleeve_text}. ")
                if data.get("length"): p_parts.append(f"Garment length: {data.get('length')}. ")

            p_parts.append("Clean composition, commercial lighting, professional studio look.")
            prompt_filled = (base_info + "\n\n" + "".join(p_parts)).strip() if base_info else "".join(p_parts)
    else:

        model_id = data.get("model_id")
        if not model_id and (data.get("is_preset") or category == "presets"):

            base_presets = await db.get_app_setting("presets_prompt") or await db.get_random_prompt()
            if base_presets and "{" in base_presets:
                prompt_filled = apply_replacements(base_presets)
            else:
                p_parts = ["Professional commercial fashion photography. High quality, realistic lighting. "]
                p_parts.append(f"Model: {gender_word or 'person'}. ")
                if age_text: p_parts.append(f"Age: {age_text}. ")
                if size_text: p_parts.append(f"Body type: {size_text}. ")
                h = data.get("height")
                if h: p_parts.append(f"Height: {h}cm. ")
                if data.get("pants_style"): p_parts.append(f"Pants cut: {data.get('pants_style')}. ")
                if sleeve_text: p_parts.append(f"Sleeve type: {sleeve_text}. ")
                L = (data.get("length") or "").strip()
                if L: p_parts.append(f"Garment length: {L}. ")
                if data.get("pose"): p_parts.append(f"Pose: {data.get('pose')}. ")
                p_parts.append(f"Camera distance: {dist_word}. View: {view_word}. ")
                if data.get("season"): p_parts.append(f"Season: {data.get('season')}. ")
                p_parts.append("8k resolution, cinematic lighting, professional studio look.")
                prompt_filled = ((base_presets or "") + "\n\n" + "".join(p_parts)).strip()
        else:

            # Женские/мужские/детские и пресеты: только промпты из «Управление промптами и категориями» + подстановки {{}}
            if model_id:
                prompt_filled = apply_replacements(prompt_text)
            else:
                prompt_filled = apply_replacements(prompt_text)

            if "{Тип кроя}" not in prompt_text and data.get("pants_style"):
                prompt_filled += f" Cut of pants: {data.get('pants_style')}."
            if "{Поза}" not in prompt_text and data.get("pose"):
                prompt_filled += f" Model pose: {data.get('pose')}."
            if "{ракурс фотографии}" not in prompt_text and "{Дистанция}" not in prompt_text and dist_word:
                prompt_filled += f" Camera distance: {dist_word}."
            if "{Сезон}" not in prompt_text and data.get("season"):
                prompt_filled += f" Season: {data.get('season')}."

    is_collage = (category == "collage" or data.get("collage_mode"))

    if is_collage:
        return prompt_filled

    dynamic_parts = []

    exclude_keys = {
        "category", "cloth", "index", "model_id", "prompt_id",
        "current_step_id", "current_step_key", "current_step_index",
        "is_preset", "random_mode", "random_other_mode", "normal_gen_mode",
        "infographic_mode", "own_mode", "storefront_mode", "collage_mode",
        "own_size", "own_sleeve", "own_cut", "own_view", "own_length",
        "model_select", "model_select_label", "display_category",
        "photos", "downloaded_paths", "own_bg_photo_id", "own_product_photo_id",
        "bg_photo", "photo", "user_photo_id", "result_photo_id", "has_person", "age", "size", "height", "body_type",
        "pants_style", "sleeve", "length", "pose", "dist", "view", "season", "holiday",
        "info_gender", "info_load", "info_lang", "info_brand", "info_extra", "extra_info", "info_angle", "info_pose",
        "info_season", "info_holiday", "aspect", "prompt", "last_photos_msg_id", "last_sent_prompt",
        "gender", "cloth_label", "gender_label", "photo_label", "aspect_label", "height_cm", "width_cm", "length_cm",
        "own_length", "child_gender", "info_dist", "info_adv1", "info_adv2", "info_adv3", "repeat_mode",
        "own_ref_photo_id", "collage_model_photo", "collage_front_photo", "collage_back_photo",
        "is_going_back", "current_step_id", "current_step_key", "current_step_index", "is_preset", "own_ref_photo_id",
        "own_product_photo_id", "own_bg_photo_id", "bg_photo", "photo", "user_photo_id", "result_photo_id",
        "last_photos_msg_id", "last_sent_prompt", "is_going_back", "current_step_id", "current_step_key",
        "current_step_index",
        "photo_label", "aspect_label", "height_cm", "width_cm", "length_cm", "own_length", "child_gender",
        "info_dist", "info_adv1", "info_adv2", "info_adv3", "repeat_mode", "own_ref_photo_id"
    }

    prompt_text_lower = (prompt_text or "").lower()
    for k, v in data.items():
        if k in exclude_keys: continue
        if not v: continue
        if isinstance(v, str) and v.startswith("AgAC"): continue

        if f"{{{k.lower()}}}" in prompt_text_lower: continue

        is_replaced = False
        for r_key in replacements.keys():
            if r_key.lower() == f"{{{k.lower()}}}" and r_key.lower() in prompt_text_lower:
                is_replaced = True
                break
        if is_replaced: continue

        dynamic_parts.append(f"{k}: {v}")

    if dynamic_parts:
        if strict_admin_prompt_mode:
            # Для строгих админ-промптов тоже передаем собранный контекст,
            # чтобы пользовательские параметры не терялись между шагами FSM.
            prompt_filled = (
                prompt_filled.rstrip()
                + "\n\nREQUEST_CONTEXT:\n"
                + "\n".join([f"- {part}" for part in dynamic_parts])
            )
        else:
            sep = " " if (prompt_filled and prompt_filled.rstrip().endswith((".", "!", "?"))) else ". "
            prompt_filled = prompt_filled.rstrip() + sep + "Additional details: " + ", ".join(dynamic_parts) + "."

    if _extra_info_raw.strip() and not _contains_user_value(prompt_filled, _extra_info_raw):
        extra_line = f"Additional product information from user: {_extra_info_raw.strip()}"
        if strict_admin_prompt_mode:
            prompt_filled = prompt_filled.rstrip() + "\n\nREQUEST_CONTEXT:\n- " + extra_line
        else:
            sep = " " if (prompt_filled and prompt_filled.rstrip().endswith((".", "!", "?"))) else ". "
            prompt_filled = prompt_filled.rstrip() + sep + extra_line + "."

    if not strict_admin_prompt_mode:
        if "ONE single" not in prompt_filled:
            prompt_filled += " Produce ONE single, high-resolution, photorealistic image. No collages, no split screens, no multiple views in one image."

    if data.get("infographic_mode"):
        prompt_filled += " ABSOLUTE NEGATIVE RULE: DO NOT write 'фото загружено', 'photo uploaded', or any Telegram-style system messages on the image. ONLY include product information text. No interface elements, no chat bubbles."

    prompt_filled = fill_remaining_brace_placeholders(prompt_filled, data)

    return prompt_filled
