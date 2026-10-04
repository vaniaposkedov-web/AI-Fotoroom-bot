from vkbottle import Keyboard, KeyboardButtonColor, OpenLink, Text

from bot.strings import get_string


def terms_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=True, inline=False)
    keyboard.add(Text(get_string("accept_terms", lang)), color=KeyboardButtonColor.POSITIVE)
    keyboard.row()
    keyboard.add(Text(get_string("agreement", lang)), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def main_menu_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(get_string("create_normal_gen", lang)), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("menu_market", lang)), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("buy_plan", lang)), color=KeyboardButtonColor.POSITIVE)
    keyboard.add(Text(get_string("menu_support", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("menu_profile", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text(get_string("menu_howto", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("menu_settings", lang)), color=KeyboardButtonColor.SECONDARY)
    return keyboard.get_json()


def profile_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(get_string("buy_plan", lang)), color=KeyboardButtonColor.POSITIVE)
    keyboard.row()
    keyboard.add(Text(get_string("menu_history", lang)), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def support_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def settings_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    keyboard.add(Text(get_string("select_lang", lang)), color=KeyboardButtonColor.PRIMARY)
    keyboard.row()
    keyboard.add(Text(get_string("agreement", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def marketplace_keyboard(enabled: dict[str, bool], lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)

    if enabled.get("collage", True):
        keyboard.add(Text(get_string("cat_collage", lang)), color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
    if enabled.get("random", True):
        keyboard.add(Text(get_string("cat_random", lang)), color=KeyboardButtonColor.PRIMARY)
        keyboard.row()
    if enabled.get("random_other", True):
        keyboard.add(Text(get_string("cat_random_other", lang)), color=KeyboardButtonColor.PRIMARY)
        keyboard.row()

    if enabled.get("infographic_clothing", True) or enabled.get("infographic_other", True):
        keyboard.add(Text(get_string("cat_infographics", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()

    if enabled.get("storefront", True):
        keyboard.add(Text(get_string("cat_storefront", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    if enabled.get("whitebg", True):
        keyboard.add(Text(get_string("cat_whitebg", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    if enabled.get("own", True):
        keyboard.add(Text(get_string("cat_own", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    if enabled.get("own_variant", True):
        keyboard.add(Text(get_string("cat_own_variant", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()

    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def infographics_keyboard(enabled: dict[str, bool], lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    if enabled.get("infographic_clothing", True):
        keyboard.add(Text(get_string("cat_infographic_clothing", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    if enabled.get("infographic_other", True):
        keyboard.add(Text(get_string("cat_infographic_other", lang)), color=KeyboardButtonColor.SECONDARY)
        keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def preset_gender_keyboard(enabled: dict[str, bool], lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    row_has_items = False
    if enabled.get("female", True):
        keyboard.add(Text(get_string("cat_female", lang)), color=KeyboardButtonColor.PRIMARY)
        row_has_items = True
    if enabled.get("male", True):
        keyboard.add(Text(get_string("cat_male", lang)), color=KeyboardButtonColor.PRIMARY)
        row_has_items = True
    if row_has_items:
        keyboard.row()
    row_has_items = False
    if enabled.get("boy", True):
        keyboard.add(Text(get_string("gender_boy", lang)), color=KeyboardButtonColor.SECONDARY)
        row_has_items = True
    if enabled.get("girl", True):
        keyboard.add(Text(get_string("gender_girl", lang)), color=KeyboardButtonColor.SECONDARY)
        row_has_items = True
    if row_has_items:
        keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def language_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=True, inline=False)
    keyboard.add(Text(get_string("lang_ru", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text(get_string("lang_en", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("lang_vi", lang)), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def back_to_main_keyboard(lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=True, inline=False)
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def topup_keyboard(contact_url: str | None, lang: str = "ru") -> str:
    keyboard = Keyboard(one_time=False, inline=False)
    if contact_url:
        label = "Связаться для пополнения"
        if lang == "en":
            label = "Contact admin to top up"
        elif lang == "vi":
            label = "Liên hệ quản trị để nạp tiền"
        keyboard.add(OpenLink(contact_url, label))
        keyboard.row()
    keyboard.add(Text(get_string("back_main", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()


def aspect_ratio_keyboard(lang: str = "ru") -> str:
    
    keyboard = Keyboard(one_time=True, inline=False)
    keyboard.add(Text("1:1"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("9:16"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("16:9"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("3:4"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("4:5"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("4:3"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("3:2"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("2:3"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text("5:4"), color=KeyboardButtonColor.SECONDARY)
    keyboard.add(Text("21:9"), color=KeyboardButtonColor.SECONDARY)
    keyboard.row()
    keyboard.add(Text(get_string("back", lang)), color=KeyboardButtonColor.NEGATIVE)
    return keyboard.get_json()
