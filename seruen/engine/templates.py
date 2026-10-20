"""Explanations written by code from facts only — the "mode without AI" and the fallback when the LLM fails.

No digits in the text: numbers are shown on the card itself and always come from code.
"""
from __future__ import annotations

from seruen.common.models import CATEGORIES, Card, Constraints, Context, Explanation

DO = {
    "museum": ("Пройдите основную экспозицию и загляните в залы с интерактивом", "Негізгі экспозицияны аралап, интерактивті залдарға кіріңіз", "Walk the main exhibition and look for the interactive rooms"),
    "gallery": ("Посмотрите текущую выставку", "Қазіргі көрмені тамашалаңыз", "See the current exhibition"),
    "theatre": ("Проверьте афишу на вечер и посмотрите здание снаружи", "Кешкі афишаны тексеріп, ғимаратты сырттан көріңіз", "Check tonight's programme and see the building"),
    "concert": ("Проверьте афишу на сегодня", "Бүгінгі афишаны тексеріңіз", "Check today's programme"),
    "cinema": ("Выберите ближайший сеанс", "Жақын сеансты таңдаңыз", "Pick the next showing"),
    "landmark": ("Обойдите вокруг и сделайте фото с лучшей точки", "Айналып шығып, ең жақсы жерден суретке түсіріңіз", "Walk around it and take a photo from the best angle"),
    "monument": ("Остановитесь на пару минут и прочитайте табличку", "Бірнеше минут тоқтап, тақтаны оқыңыз", "Stop for a moment and read the plaque"),
    "viewpoint": ("Поднимитесь и посмотрите на город сверху", "Көтеріліп, қаланы жоғарыдан көріңіз", "Go up and look at the city from above"),
    "park": ("Пройдитесь по главной аллее", "Басты аллеямен серуендеңіз", "Take a stroll along the main alley"),
    "embankment": ("Пройдитесь вдоль набережной", "Жағалаумен жүріп өтіңіз", "Walk along the embankment"),
    "playground": ("Дайте ребёнку поиграть, а сами отдохните рядом", "Балаға ойнауға мүмкіндік беріп, жанында демалыңыз", "Let the kids play while you rest nearby"),
    "attraction": ("Купите билет на месте и начните с главной зоны", "Билетті сол жерден алып, басты аймақтан бастаңыз", "Buy a ticket on site and start with the main area"),
    "mall": ("Погрейтесь, перекусите на фудкорте и пройдитесь по галереям", "Жылынып, фудкортта тамақтанып, галереяларды аралаңыз", "Warm up, grab a bite at the food court and stroll the galleries"),
    "market": ("Попробуйте местные сладости и сухофрукты", "Жергілікті тәттілер мен кептірілген жемістерді татып көріңіз", "Try local sweets and dried fruit"),
    "cafe": ("Закажите напиток и отдохните", "Сусын алып, демалыңыз", "Order a drink and rest"),
    "coffee": ("Возьмите кофе и сядьте у окна", "Кофе алып, терезе жанына отырыңыз", "Get a coffee and sit by the window"),
    "restaurant": ("Попробуйте блюдо казахской кухни, если оно есть в меню", "Мәзірде болса, қазақ тағамын татып көріңіз", "Try a Kazakh dish if it's on the menu"),
    "fast_food": ("Быстро перекусите и идите дальше", "Тез тамақтанып, әрі қарай жүріңіз", "Grab a quick bite and move on"),
    "bar": ("Займите место и спросите местные напитки", "Орын алып, жергілікті сусындарды сұраңыз", "Take a seat and ask for local drinks"),
    "nightclub": ("Проверьте программу вечера на входе", "Кіреберісте кеш бағдарламасын тексеріңіз", "Check the night's line-up at the door"),
    "library": ("Найдите тихий стол и поработайте", "Тыныш үстел тауып, жұмыс істеңіз", "Find a quiet desk and work"),
    "coworking": ("Уточните условия на ресепшене и поработайте", "Ресепшеннен шарттарды біліп, жұмыс істеңіз", "Ask at reception and get some work done"),
    "religious": ("Зайдите внутрь, если открыто, соблюдая правила посещения", "Ашық болса, ережені сақтап ішке кіріңіз", "Go inside if open, respecting the visiting rules"),
    "sport": ("Узнайте, есть ли свободное время для посещения", "Бос уақыт бар-жоғын біліңіз", "Check if there's a free slot"),
    "other": ("Загляните и решите на месте", "Кіріп көріп, сол жерде шешіңіз", "Drop in and decide on the spot"),
}

R = {
    "want": ("подходит под запрос: {x}", "сұранысқа сай: {x}", "matches what you asked: {x}"),
    "near": ("совсем рядом", "өте жақын", "very close"),
    "warm": ("можно погреться внутри", "ішінде жылынуға болады", "you can warm up inside"),
    "outdoor_ok": ("хорошая погода для улицы", "далаға ауа райы жақсы", "good weather to be outside"),
    "kids": ("подходит для детей", "балаларға қолайлы", "kid-friendly"),
    "popular": ("одно из известных мест города", "қаланың белгілі орындарының бірі", "one of the city's well-known places"),
    "event": ("сегодня здесь событие", "бүгін мұнда іс-шара бар", "there's an event here today"),
    "open_area": ("открытая территория, вход свободный", "ашық аумақ, кіру тегін", "open area, free to enter"),
    "accessible": ("отмечено как доступное для коляски", "арбамен кіруге болатыны белгіленген", "marked as wheelchair-accessible"),
}

WANT_LABEL = {
    "coffee": ("кофе", "кофе", "coffee"), "food": ("еда", "тамақ", "food"), "museum": ("музей", "мұражай", "museum"),
    "culture": ("культура", "мәдениет", "culture"), "park": ("прогулка", "серуен", "a walk"), "view": ("вид", "көрініс", "views"),
    "night": ("вечер", "кеш", "night out"), "unusual": ("необычное", "ерекше", "something unusual"),
    "kids": ("дети", "балалар", "kids"), "quiet": ("тишина", "тыныштық", "quiet"), "power": ("розетка и Wi‑Fi", "розетка мен Wi‑Fi", "power and Wi‑Fi"),
    "shopping": ("покупки", "сауда", "shopping"), "events": ("события", "іс-шаралар", "events"),
    "landmark": ("достопримечательность", "көрікті жер", "sights"), "attraction": ("развлечения", "ойын-сауық", "fun"),
    "sport": ("спорт", "спорт", "sport"), "religious": ("святыни", "киелі орындар", "sacred places"),
    "accessible": ("доступность", "қолжетімділік", "accessibility"),
}

_I = {"ru": 0, "kk": 1, "en": 2}


def reasons(card: Card, c: Constraints, ctx: Context, matched: list[str]) -> list[str]:
    i = _I.get(c.lang, 0)
    out = []
    if matched:
        out.append(R["want"][i].format(x=", ".join(WANT_LABEL[m][i] for m in matched[:2] if m in WANT_LABEL)))
    if card.walk_min <= 7:
        out.append(R["near"][i])
    indoor = CATEGORIES.get(card.category, {}).get("indoor", True)
    if ctx.mode != "walk" and indoor:
        out.append(R["warm"][i])
    if ctx.mode == "walk" and not indoor:
        out.append(R["outdoor_ok"][i])
    if c.kids and ("kids" in card.tags or "family" in card.tags):
        out.append(R["kids"][i])
    if card.kind == "event":
        out.append(R["event"][i])
    if card.hours.status == "open_area":
        out.append(R["open_area"][i])
    if c.mobility == "wheelchair" and card.accessibility and card.accessibility.value == "yes":
        out.append(R["accessible"][i])
    if not out:
        out.append(R["popular"][i])
    return out[:3]


def template_explanation(card: Card, c: Constraints, ctx: Context, matched: list[str]) -> Explanation:
    i = _I.get(c.lang, 0)
    rs = reasons(card, c, ctx, matched)
    why = "; ".join(rs)
    why = why[0].upper() + why[1:] + "."
    todo = DO.get(card.category, DO["other"])[i] + "."
    return Explanation(poi_id=card.poi_id, why=why, what_to_do=todo, source="poi_data", template=True)
