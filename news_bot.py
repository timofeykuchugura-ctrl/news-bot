import calendar
import html
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ========================= НАСТРОЙКИ =========================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = os.environ.get("OWNER_ID", "").strip()  # ваш Telegram id (нужен для режима проверки)

# Модель можно поменять через GitHub Variables: GEMINI_MODEL.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [
    model.strip()
    for model in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-2.5-flash",
    ).split(",")
    if model.strip()
]

# Подпись под каждым постом. Текст кликабельный.
FOOTER_TEXT = "ПОДПИСАТЬСЯ: TIMNEWS↩️"
FOOTER_URL = "https://t.me/timnews_roblox"

STATE_FILE = "posted.json"
SETTINGS_FILE = "settings.json"   # режим работы (меняется из бота-пульта)
DRAFTS_FILE = "drafts.json"       # черновики, ждущие вашего решения
MAX_AGE_HOURS = 48
POSTS_PER_BATCH = 3
QUEUE_MAX_AGE_MIN = 100
PER_FEED = 15
MAX_CANDIDATES = 70

# Только новости про игры Roblox. Чтобы добавить новости самой платформы
# (DevForum, блог Roblox), поставьте True.
ALLOW_PLATFORM_NEWS = False
# Игра считается популярной, если в топах Roblox у неё не меньше столько игроков онлайн.
MIN_LIVE_PLAYERS = 5000
FILTER_VERSION = 4

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

# Ленты с новостями об играх Roblox.
GAME_FEEDS = {
    "Try Hard Guides": "https://tryhardguides.com/tag/roblox/feed/",
    "Gamer Journalist": "https://gamerjournalist.com/roblox/feed",
    "Pro Game Guides": "https://progameguides.com/roblox/feed/",
    "Bloxidex": "https://bloxidex.com/rss.xml",
}
# Ленты платформы (используются только при ALLOW_PLATFORM_NEWS = True).
PLATFORM_FEEDS = {
    "Roblox DevForum — Announcements": "https://devforum.roblox.com/c/updates/announcements/36.rss",
    "Roblox DevForum — Updates": "https://devforum.roblox.com/c/updates/45.rss",
    "Roblox Blog": "https://blog.roblox.com/feed/",
}

# Живые топ-подборки Roblox Discover.
ROBLOX_DISCOVER_URL = "https://apis.roblox.com/explore-api/v1/get-sort-content"
ROBLOX_SORTS = ("top-playing-now", "top-trending")

# Запасной список вечно популярных игр: работает, даже если Discover недоступен.
POPULAR_FALLBACK = [
    "Grow a Garden", "Blox Fruits", "Adopt Me", "Brookhaven", "Murder Mystery 2",
    "Dress to Impress", "Steal a Brainrot", "99 Nights in the Forest", "Fisch",
    "Pet Simulator 99", "Tower Defense Simulator", "Jailbreak", "Blade Ball", "Rivals",
    "Doors", "Arsenal", "Bee Swarm Simulator", "King Legacy", "Anime Vanguards",
    "Sol's RNG", "Forsaken", "Piggy", "Natural Disaster Survival", "Royale High",
    "Welcome to Bloxburg", "Toilet Tower Defense", "Plants vs Brainrots",
    "Escape Tsunami for Brainrots", "Escape Tsunami For Brainrots", "Anime Last Stand",
    "Type Soul", "Pls Donate", "Mm2", "Da Hood", "Slap Battles", "Fruit Battlegrounds",
]
# Слишком общие названия, которые нельзя считать игрой.
GENERIC_NAMES = {
    "update", "new", "event", "game", "games", "roblox", "simulator", "tycoon",
    "obby", "beta", "test", "classic", "home", "play", "world",
}

# Оставляем только материалы, похожие на новость, а не рекламное описание игры.
NEWS_SIGNAL = re.compile(
    r"\b(update|updated|patch|event|codes?|redeem|release|released|"
    r"shutdown|banned|maintenance|leak|new feature|change|nerf|buff|"
    r"introduc(?:es|ed|ing)?|launch(?:es|ed)?|announc(?:es|ed|ement)?|"
    r"returns?|reveals?|unveil(?:s|ed)?|available now|free item|limited|"
    r"outage|server issue|tournament|season|quest|admin abuse|new map|new mode|"
    r"new area|new item|item drop|challenge|free avatar|avatar item|"
    r"adds?|added|new pets?|new boss|new world|new weapons?|new island|drops?|"
    r"collab|crossover|rework|overhaul|expansion|live now|begins?|starts?|"
    r"добавил[иа]?|обновлени[ея]|ивент|событи[ея]|код[ыов]?|"
    r"промокод[ыов]?|вышел|вышла|запустил[иа]?|изменени[ея]|"
    r"отключени[ея]|техработ[ы]?|утечк[аи]|турнир|сезон|квест|"
    r"бесплатн[ыое]|предмет|возвращени[ея]|анонс|добавлен[ыо])\b",
    re.I,
)
PROMO_LANGUAGE = re.compile(
    r"\b(adventure|explore|become a legend|choose your side|"
    r"discover the world|start your journey|погрузись в приключени[ея]|"
    r"отправляйся в приключени[ея]|исследуй мир|стань легендой)\b",
    re.I,
)
CODE_WORD = re.compile(r"\b(codes?|коды?|кодов|промокод[ыов]?)\b", re.I)
CODE_TOKEN = re.compile(r"(?<![A-ZА-Я0-9])[A-Z0-9]{4,20}(?![A-ZА-Я0-9])", re.I)
CODE_LABEL = re.compile(r"\b(?:codes?|промокод[ыов]?)\s*[:：-]\s*", re.I)

# Картинки, которые не годятся для поста (смайлики, аватарки, иконки).
BAD_IMAGE_RE = re.compile(r"emoji|twemoji|smilies|avatar|gravatar|favicon|logo-?small", re.I)


def has_actual_codes(text):
    """Ищет код только рядом с явной подписью Codes/Промокоды."""
    for match in CODE_LABEL.finditer(text or ""):
        fragment = (text or "")[match.end():match.end() + 100]
        if CODE_TOKEN.search(fragment):
            return True
    return False


# ========================= СОСТОЯНИЕ =========================
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=1)


def load_state():
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("links", [])
    state.setdefault("titles", [])
    state.setdefault("headlines", [])
    state.setdefault("queue", [])
    state.setdefault("built_at", 0)
    if state.get("filter_version") != FILTER_VERSION:
        state["queue"] = []
        state["filter_version"] = FILTER_VERSION
        state["built_at"] = 0
    return state


def save_state(state):
    state["links"] = state["links"][-600:]
    state["titles"] = state["titles"][-600:]
    state["headlines"] = state["headlines"][-40:]
    state["filter_version"] = FILTER_VERSION
    save_json(STATE_FILE, state)


def norm(value):
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def strip_tags(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


# ========================= СБОР НОВОСТЕЙ =========================
def entry_time(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def good_image(url):
    """Отбрасываем смайлики, аватарки и иконки."""
    return bool(url) and url.startswith("http") and not BAD_IMAGE_RE.search(url)


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key, []) or []:
            if good_image(media.get("url")):
                return media["url"]

    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image") and good_image(link.get("href")):
            return link["href"]

    for enclosure in entry.get("enclosures", []) or []:
        if enclosure.get("type", "").startswith("image") and good_image(enclosure.get("href")):
            return enclosure["href"]

    content = entry.get("summary", "") or ""
    for part in entry.get("content", []) or []:
        content += part.get("value", "")
    for tag in re.findall(r"<img[^>]*>", content):
        if re.search(r"emoji|smilies|avatar", tag, re.I):
            continue
        size = re.search(r'(?:width|height)=["\']?(\d+)', tag)
        if size and int(size.group(1)) < 100:
            continue
        src = re.search(r'src=["\']([^"\']+)', tag)
        if src and good_image(src.group(1)):
            return src.group(1)
    return None


def og_image(url):
    try:
        response = requests.get(url, headers=UA, timeout=15)
        response.raise_for_status()
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            response.text,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            response.text,
        )
        found = html.unescape(match.group(1)) if match else None
        return found if good_image(found) else None
    except requests.RequestException:
        return None


def spread(items, limit):
    """Распределяет материалы между источниками, чтобы одна лента не заняла всё."""
    if len(items) <= limit:
        return items

    by_source = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)

    result = []
    index = 0
    while len(result) < limit:
        added = False
        for source_items in by_source.values():
            if index < len(source_items) and len(result) < limit:
                result.append(source_items[index])
                added = True
        if not added:
            break
        index += 1
    return result


def clean_game_name(raw):
    """Убирает пометки вроде [UPDATE] и эмодзи, оставляя само название игры."""
    raw = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", raw or "")
    name = norm(raw)
    return name if len(name) >= 4 and name not in GENERIC_NAMES else ""


def fetch_live_games():
    """Названия и онлайн игр из живых топ-подборок Roblox Discover."""
    live = {}
    session_id = str(uuid.uuid4())

    def walk(value):
        if isinstance(value, dict):
            name = value.get("name") or value.get("title") or value.get("gameName")
            players = None
            for key in ("playerCount", "playing", "playersCount"):
                if isinstance(value.get(key), (int, float)):
                    players = value[key]
                    break
            if isinstance(name, str) and players is not None and players >= MIN_LIVE_PLAYERS:
                clean = clean_game_name(name)
                if clean:
                    live[clean] = max(int(players), live.get(clean, 0))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for sort_id in ROBLOX_SORTS:
        try:
            response = requests.get(
                ROBLOX_DISCOVER_URL,
                params={
                    "sessionId": session_id,
                    "sortId": sort_id,
                    "device": "computer",
                    "country": "all",
                },
                headers=UA,
                timeout=20,
            )
            response.raise_for_status()
            walk(response.json())
            print(f"[Roblox] Загружена подборка {sort_id}")
        except (requests.RequestException, ValueError) as error:
            print(f"[Roblox] Не удалось загрузить {sort_id}: {error}")
    return live


def get_popular_games():
    """Живой топ Roblox плюс запасной список. Бот работает, даже если Discover недоступен."""
    popular = {}
    for raw in POPULAR_FALLBACK:
        clean = clean_game_name(raw)
        if clean:
            popular[clean] = MIN_LIVE_PLAYERS
    live = fetch_live_games()
    for clean, players in live.items():
        popular[clean] = max(players, popular.get(clean, 0))
    print(f"[Roblox] Популярных игр в фильтре: {len(popular)} (из живого топа: {len(live)})")
    return popular


def find_popular_game(title, summary, popular):
    """Возвращает самую популярную игру из списка, которую упоминает новость."""
    text = f" {norm(title + ' ' + summary)} "
    mentions_roblox = "roblox" in text
    best, best_players = None, -1
    for name, players in popular.items():
        if f" {name} " not in text:
            continue
        # Названия из одного слова (Doors, Rivals) считаем только если упомянут Roblox.
        if " " not in name and not mentions_roblox:
            continue
        if players > best_players:
            best, best_players = name, players
    return best


def has_real_news(title, summary):
    """Отсекает рекламные описания и обещания кодов без самих кодов."""
    text = f"{title} {summary}"
    if not NEWS_SIGNAL.search(text):
        return False
    if PROMO_LANGUAGE.search(text) and not NEWS_SIGNAL.search(title):
        return False
    # Нельзя обещать новые коды, если в исходном материале нет самих кодов.
    if CODE_WORD.search(title) and not has_actual_codes(summary):
        return False
    return True


def collect(state):
    seen_links = set(state["links"])
    seen_titles = set(state["titles"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    collected = []

    popular = get_popular_games()
    sources = dict(GAME_FEEDS)
    if ALLOW_PLATFORM_NEWS:
        sources.update(PLATFORM_FEEDS)

    for name, url in sources.items():
        platform_news = name in PLATFORM_FEEDS
        try:
            response = requests.get(url, headers=UA, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as error:
            print(f"[skip] {name}: {error}")
            continue

        count = 0
        for entry in feed.entries[:PER_FEED]:
            link = entry.get("link")
            title = strip_tags(entry.get("title", ""))
            if not link or not title:
                continue
            if link in seen_links or norm(title) in seen_titles:
                continue

            published = entry_time(entry)
            if published and published < cutoff:
                continue

            summary = strip_tags(entry.get("summary", ""))
            for part in entry.get("content", []) or []:
                summary += " " + strip_tags(part.get("value", ""))
            summary = summary[:2500]

            game = None
            if not platform_news:
                game = find_popular_game(title, summary, popular)
                if not game:
                    continue  # не про популярную игру
            if not has_real_news(title, summary):
                print(f"[skip] Не похоже на новость: {title}")
                continue

            collected.append({
                "source": name,
                "title": title,
                "summary": summary,
                "link": link,
                "image": find_image(entry),
                "game": game or "",
            })
            count += 1

        print(f"[ok] {name}: {count} подходящих новостей")

    return spread(collected, MAX_CANDIDATES)


# ========================= GEMINI =========================
def gemini_json(prompt):
    models = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
        },
    }
    last_error = "нет ответа"

    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )

        for attempt in range(3):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    timeout=90,
                )
            except requests.RequestException as error:
                last_error = f"{model}: ошибка соединения: {error}"
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                break

            if response.status_code == 429:
                last_error = f"{model}: превышен лимит (429)"
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))
                    continue
                break

            if response.status_code in (500, 502, 503, 504):
                last_error = f"{model}: временная ошибка сервера ({response.status_code})"
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                break

            if response.status_code == 404:
                last_error = f"{model}: модель не найдена (404)"
                break

            try:
                response.raise_for_status()
                result = response.json()
                answer = result["candidates"][0]["content"]["parts"][0]["text"]
                answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip(), flags=re.I)
                return json.loads(answer)
            except (requests.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
                last_error = f"{model}: не удалось обработать ответ: {error}"
                break

        print(f"Переключаюсь на следующую модель. Причина: {last_error}")

    raise RuntimeError(f"Gemini не смог обработать запрос. Последняя ошибка: {last_error}")


def choose_many(items, state, count):
    listing = "\n".join(
        f"{index}. [{item.get('game') or item['source']}] {item['title']} — "
        f"{item.get('summary', '')[:400]}"
        for index, item in enumerate(items)
    )
    recent = "\n".join(
        f"- {headline}" for headline in state["headlines"][-25:]
    ) or "(пока ничего)"

    prompt = f"""Ты главный редактор русскоязычного Telegram-канала про популярные игры Roblox.
Ниже заголовки новостей. В квадратных скобках указана игра, о которой новость.
Недавно уже опубликовано:
{recent}

Выбери от 1 до {count} новостей. Обычно выбирай ОДНУ, самую интересную.
Две или три выбирай только при действительно крупных событиях.
Если несколько сайтов пишут об одном событии, выбери только один материал.
Не выбирай повторы уже опубликованных новостей.
Подходят только новости про конкретные популярные игры Roblox: крупные обновления,
новые сезоны, карты, режимы, питомцы, боссы, предметы, ивенты, коллаборации,
турниры, квесты, бесплатные награды и рабочие коды.
Не выбирай новости про саму платформу Roblox, Roblox Studio, разработчиков, правила,
безопасность и бизнес, а также рекламные описания игр, обзоры без нового события
и пустые анонсы.
Если в заголовке обещаны коды, выбирай материал только если в исходном тексте
действительно указаны сами коды. Никогда не придумывай коды.
Предпочитай новости про самые популярные и обсуждаемые игры.
Расположи выбранные новости по важности.

Заголовки:
{listing}

Ответь только JSON такого вида: {{"indices": [0, 2]}}.
Укажи номера выбранных новостей. Не добавляй пояснения."""

    data = gemini_json(prompt)
    selected = []
    for index in data.get("indices", []):
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(items) and items[index] not in selected:
            selected.append(items[index])
    return selected[:count]


def rewrite_many(items):
    blocks = []
    for index, item in enumerate(items):
        blocks.append(
            f"[{index}] Игра: {item.get('game') or 'Roblox'}\n"
            f"Заголовок: {item['title']}\nТекст: {item['summary']}"
        )

    prompt = f"""Перепиши новости для русскоязычного Telegram-канала про игры Roblox.
Пиши простым живым языком. Названия игр, героев и функций оставляй как в оригинале.
Добавь немного смайликов: один уместный в начале заголовка и один-два в тексте
(не больше трёх на весь пост).
Для каждой новости: пиши своими словами, если текст на другом языке, переведи.
Используй только факты из предоставленного текста, ничего не выдумывай.
Не превращай рекламное описание игры в новость. Не придумывай промокоды.
Если в исходном тексте нет конкретных кодов, не пиши о новых кодах.
Нейтральный тон, 2-4 коротких предложения, максимум 600 символов.
Не добавляй ссылки, названия СМИ или упоминания источника.

Новости:
{chr(10).join(blocks)}

Ответь только JSON: {{"posts": [{{"headline": "заголовок до 90 символов", "body": "текст новости"}}]}}.
Верни ровно {len(items)} элементов в том же порядке."""

    posts = gemini_json(prompt).get("posts", [])
    if len(posts) != len(items):
        raise ValueError("Количество переписанных новостей не совпало с количеством выбранных")
    return posts


def rewrite_one(item):
    prompt = f"""Перепиши новость для русскоязычного Telegram-канала про игры Roblox.
Пиши простым живым языком, названия игр, героев и функций оставляй как в оригинале.
Добавь немного смайликов: один уместный в начале заголовка и один-два в тексте
(не больше трёх на весь пост).
Используй только факты из текста, ничего не выдумывай, не придумывай коды.
Нейтральный тон, 2-4 коротких предложения, максимум 600 символов.
Не добавляй ссылки, названия СМИ или упоминания источника.

Игра: {item.get('game') or 'Roblox'}
Заголовок: {item['title']}
Текст: {item['summary']}

Ответь только JSON: {{"headline": "короткий заголовок до 90 символов", "body": "текст новости"}}."""
    return gemini_json(prompt)


# ========================= TELEGRAM =========================
def tg(method, **kwargs):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        data=kwargs,
        timeout=60,
    )
    if not response.ok:
        print(f"Telegram error ({method}): {response.text}")
    return response


def render_text(post):
    headline = html.escape(str(post["headline"]).strip())
    body = html.escape(str(post["body"]).strip())
    footer = (
        f'<a href="{html.escape(FOOTER_URL, quote=True)}">'
        f"{html.escape(FOOTER_TEXT)}</a>"
    )
    text = f"<b>{headline}</b>"
    if body:
        text += f"\n\n{body}"
    return text + f"\n\n{footer}"


def send_post(chat_id, item, post, keyboard=None):
    """Отправляет пост в чат. Возвращает (успех, id сообщения, было ли фото)."""
    text = render_text(post)
    image = item.get("image")
    extra = {"reply_markup": json.dumps(keyboard)} if keyboard else {}
    if image and len(text) <= 1024:
        response = tg("sendPhoto", chat_id=chat_id, photo=image, caption=text, parse_mode="HTML", **extra)
        if response.ok:
            return True, response.json()["result"]["message_id"], True
        print("Фото не отправилось, пробую текстом")
    response = tg(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
        **extra,
    )
    if response.ok:
        return True, response.json()["result"]["message_id"], False
    return False, None, False


def publish(item, post):
    """Публикует пост в канал. Источник новости намеренно не указывается."""
    if not item.get("image"):
        item["image"] = og_image(item["link"])
    image = item.get("image")
    if image and len(render_text(post)) > 1024:
        # Текст не помещается в подпись к фото: фото отдельно, потом текст.
        tg("sendPhoto", chat_id=CHANNEL_ID, photo=image)
        without_image = dict(item)
        without_image["image"] = None
        return send_post(CHANNEL_ID, without_image, post)[0]
    return send_post(CHANNEL_ID, item, post)[0]


# ========================= ПОДГОТОВКА ПОСТОВ =========================
def make_entries(state):
    items = collect(state)
    if not items:
        print("Новых подходящих новостей нет")
        return []

    chosen = choose_many(items, state, POSTS_PER_BATCH)
    if not chosen:
        print("Gemini не выбрал новости из найденных материалов")
        return []

    try:
        posts = rewrite_many(chosen)
    except Exception as error:
        print(f"Не удалось переписать подборку одним запросом: {error}")
        posts = []
        for item in chosen:
            try:
                posts.append(rewrite_one(item))
            except Exception as one_error:
                print(f"Не удалось переписать новость '{item['title']}': {one_error}")
                posts.append(None)

    entries = []
    for item, post in zip(chosen, posts):
        if not post:
            continue
        headline = str(post.get("headline", "")).strip()
        body = str(post.get("body", "")).strip()
        if not headline or not body:
            print(f"[skip] Пустая публикация: {item['title']}")
            continue
        if CODE_WORD.search(headline) and not has_actual_codes(item.get("summary", "")):
            print(f"[skip] Заголовок обещает коды, но кодов нет: {headline}")
            continue
        entry = dict(item)
        entry["headline"] = headline
        entry["body"] = body
        entries.append(entry)
        print(f"Подготовлено: [{item.get('game', '')}] {item['title']}")
    return entries


# ========================= РЕЖИМ «АВТО»: публикуем сразу =========================
def build_queue(state):
    entries = make_entries(state)
    if not entries:
        return False
    state["queue"] = entries
    state["built_at"] = time.time()
    save_state(state)
    print(f"Подготовлено новостей: {len(entries)}")
    return True


def auto_main(state):
    if state["queue"] and time.time() - state["built_at"] > QUEUE_MAX_AGE_MIN * 60:
        print("Очередь устарела, собираю заново")
        state["queue"] = []

    if not state["queue"]:
        try:
            if not build_queue(state):
                print("Публиковать нечего")
                return
        except Exception as error:
            print(f"Ошибка подготовки новостей: {error}")
            sys.exit(1)

    first = True
    while state["queue"]:
        if not first:
            time.sleep(30)  # пауза между постами одной подборки
        first = False

        item = state["queue"][0]
        post = {"headline": item["headline"], "body": item["body"]}

        if not publish(item, post):
            sys.exit("Не удалось опубликовать новость в Telegram")

        state["queue"].pop(0)
        state["links"].append(item["link"])
        state["titles"].append(norm(item["title"]))
        state["headlines"].append(post["headline"].strip())
        save_state(state)
        print("Опубликовано:", post["headline"])

    print("Готово")


# ========================= РЕЖИМ «ПРОВЕРКА»: сначала черновики вам =========================
def draft_keyboard(draft_id):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Опубликовать", "callback_data": f"ok:{draft_id}"},
                {"text": "❌ Удалить", "callback_data": f"no:{draft_id}"},
            ],
            [{"text": "✏️ Изменить", "callback_data": f"ed:{draft_id}"}],
        ]
    }


def mark_preview(draft, label):
    preview = draft.get("preview")
    if not preview or not preview.get("msg_id"):
        return
    keyboard = {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}
    tg(
        "editMessageReplyMarkup",
        chat_id=preview["chat"],
        message_id=preview["msg_id"],
        reply_markup=json.dumps(keyboard),
    )


def review_main(state, settings):
    drafts = load_json(DRAFTS_FILE, [])
    timeout = int(settings.get("timeout_min") or 0)

    # Авто-публикация черновиков, которые вы не успели проверить.
    if timeout > 0 and drafts:
        keep = []
        for draft in drafts:
            if time.time() - draft.get("created", 0) > timeout * 60 and publish(draft, draft):
                print("Опубликовано автоматически:", draft["headline"])
                mark_preview(draft, "✅ Опубликовано автоматически")
            else:
                keep.append(draft)
        drafts = keep
        save_json(DRAFTS_FILE, drafts)

    if drafts:
        print("Жду вашего решения по черновикам:", len(drafts))
        return

    if not OWNER_ID:
        sys.exit("Режим проверки включён, но не задан секрет OWNER_ID")

    entries = make_entries(state)
    if not entries:
        return
    tg(
        "sendMessage",
        chat_id=OWNER_ID,
        text=f"🆕 Новые черновики: {len(entries)}. Проверьте и нажмите кнопку под каждым.",
    )
    for index, entry in enumerate(entries):
        draft = {
            "id": f"d{int(time.time())}_{index}",
            "headline": entry["headline"],
            "body": entry["body"],
            "image": entry.get("image") or og_image(entry["link"]),
            "link": entry["link"],
            "source": entry["source"],
            "title": entry["title"],
            "created": time.time(),
        }
        ok, message_id, has_photo = send_post(OWNER_ID, draft, draft, draft_keyboard(draft["id"]))
        draft["preview"] = (
            {"chat": OWNER_ID, "msg_id": message_id, "photo": has_photo} if ok else None
        )
        drafts.append(draft)
        state["links"].append(draft["link"])
        state["titles"].append(norm(draft["title"]))
        state["headlines"].append(draft["headline"].strip())
    save_state(state)
    save_json(DRAFTS_FILE, drafts)
    print("Черновиков отправлено вам:", len(drafts))


# ========================= ЗАПУСК =========================
def main():
    state = load_state()
    settings = load_json(SETTINGS_FILE, {})
    if settings.get("mode") == "review":
        review_main(state, settings)
    else:
        auto_main(state)


if __name__ == "__main__":
    main()