import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ========================= НАСТРОЙКИ =========================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Можно изменить модель через GitHub Variables: GEMINI_MODEL.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [
    model.strip()
    for model in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if model.strip()
]

# Подпись под каждым постом. Текст кликабельный.
FOOTER_TEXT = "ПОДПИСАТЬСЯ: TIMNEWS↩️"
FOOTER_URL = "https://t.me/timnews_roblox"

STATE_FILE = "posted.json"
MAX_AGE_HOURS = 24
POSTS_PER_BATCH = 3
QUEUE_MAX_AGE_MIN = 100
PER_FEED = 15
MAX_CANDIDATES = 70

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

# Основные RSS-ленты Roblox.
FEEDS = {
    "Try Hard Guides": "https://tryhardguides.com/tag/roblox/feed/",
    "Gamer Journalist": "https://gamerjournalist.com/roblox/feed",
    "Pro Game Guides": "https://progameguides.com/roblox/feed/",
    "Roblox DevForum": "https://devforum.roblox.com/c/updates/announcements/36.rss",
    "Roblox Blog": "https://blog.roblox.com/feed/",
}

# Отслеживаемые популярные игры Roblox. Список можно расширять.
POPULAR_ROBLOX_GAMES = [
    "Grow a Garden",
    "Steal a Brainrot",
    "99 Nights in the Forest",
    "Blox Fruits",
    "Brookhaven",
    "Adopt Me",
    "Dress to Impress",
    "Murder Mystery 2",
    "Fisch",
    "DOORS",
    "RIVALS",
    "The Strongest Battlegrounds",
    "Blade Ball",
    "Tower Defense Simulator",
    "Pet Simulator 99",
    "Jailbreak",
    "Arsenal",
    "Piggy",
    "Evade",
    "Bee Swarm Simulator",
    "Royale High",
]

# Общие темы официальных новостей самой платформы Roblox.
ROBLOX_PLATFORM_TERMS = (
    "roblox", "robux", "avatar", "creator", "devforum",
    "новости платформы", "обновление платформы",
)


# ========================= СОСТОЯНИЕ =========================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            state = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    state.setdefault("links", [])
    state.setdefault("titles", [])
    state.setdefault("headlines", [])
    state.setdefault("queue", [])
    state.setdefault("built_at", 0)
    return state


def save_state(state):
    state["links"] = state["links"][-600:]
    state["titles"] = state["titles"][-600:]
    state["headlines"] = state["headlines"][-40:]
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


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


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key, []) or []:
            if media.get("url"):
                return media["url"]

    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]

    for enclosure in entry.get("enclosures", []) or []:
        if enclosure.get("type", "").startswith("image") and enclosure.get("href"):
            return enclosure["href"]

    content = entry.get("summary", "") or ""
    for part in entry.get("content", []) or []:
        content += part.get("value", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', content)
    return match.group(1) if match else None


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
        return html.unescape(match.group(1)) if match else None
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


def is_roblox_related(title, summary):
    """Оставляет новости об играх Roblox и самой платформе."""
    text = norm(f"{title} {summary}")
    game_names = [norm(name) for name in POPULAR_ROBLOX_GAMES]
    if any(name and name in text for name in game_names):
        return True
    return any(norm(term) in text for term in ROBLOX_PLATFORM_TERMS)


def collect(state):
    seen_links = set(state["links"])
    seen_titles = set(state["titles"])
    now = datetime.now(timezone.utc)
    regular_items = []

    # Используем только Roblox-источники — общие игровые СМИ отключены.
    sources = list(FEEDS.items())

    for name, url in sources:
        cutoff = now - timedelta(hours=MAX_AGE_HOURS)

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

            summary = strip_tags(entry.get("summary", ""))[:1500]
            if not is_roblox_related(title, summary):
                continue

            item = {
                "source": name,
                "title": title,
                "summary": summary,
                "link": link,
                "image": find_image(entry),
            }
            regular_items.append(item)
            count += 1

        print(f"[ok] {name}: {count} новых подходящих новостей")

    return spread(regular_items, MAX_CANDIDATES)


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
        f"{index}. {item['title']}" for index, item in enumerate(items)
    )
    recent = "\n".join(
        f"- {headline}" for headline in state["headlines"][-25:]
    ) or "(пока ничего)"

    prompt = f"""Ты главный редактор русскоязычного Telegram-канала ТОЛЬКО про Roblox.
Ниже заголовки новостей из Roblox-источников за последние часы.
Недавно уже опубликовано:
{recent}

Выбери от 1 до {count} новостей. Обычно выбирай ОДНУ, самую важную.
Две или три выбирай только при действительно крупных событиях.
Если несколько сайтов пишут об одном событии, выбери только один материал.
Не выбирай повторы уже опубликованных новостей.
Выбирай только новости о Roblox и играх внутри Roblox.
Особенно следи за этими режимами: Grow a Garden, Steal a Brainrot,
99 Nights in the Forest, Blox Fruits, Brookhaven, Adopt Me,
Dress to Impress, Murder Mystery 2, Fisch, DOORS, RIVALS,
The Strongest Battlegrounds, Blade Ball, Tower Defense Simulator,
Pet Simulator 99, Jailbreak, Arsenal, Piggy, Evade,
Bee Swarm Simulator и Royale High.
Также выбирай важные официальные обновления Roblox, изменения Robux,
аватаров, инструментов разработчиков и безопасности платформы.
Не выбирай новости о Minecraft, Fortnite, GTA или других играх вне Roblox.
Промокоды и гайды выбирай только если нет более важных новостей.
Не выбирай рекламу и пустые темы. Расположи выбранные по важности.

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
            f"[{index}] Заголовок: {item['title']}\nТекст: {item['summary']}"
        )

    prompt = f"""Перепиши новости для русскоязычного Telegram-канала только про Roblox.
Пиши простым живым языком. Названия игр, героев и функций оставляй как в оригинале.
Для каждой новости: пиши своими словами, если текст на другом языке — переведи.
Используй только факты из предоставленного текста, ничего не выдумывай.
Нейтральный тон, 2–4 коротких предложения, максимум 600 символов.
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
    prompt = f"""Перепиши новость для русскоязычного Telegram-канала только про Roblox.
Пиши простым живым языком, названия игр, героев и функций оставляй как в оригинале.
Используй только факты из текста, ничего не выдумывай.
Нейтральный тон, 2–4 коротких предложения, максимум 600 символов.
Не добавляй ссылки, названия СМИ или упоминания источника.

Заголовок: {item['title']}
Текст: {item['summary']}

Ответь только JSON: {{"headline": "короткий заголовок до 90 символов", "body": "текст новости"}}."""
    return gemini_json(prompt)


# ========================= TELEGRAM =========================
def telegram_request(method, **kwargs):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        data=kwargs,
        timeout=60,
    )
    if not response.ok:
        print(f"Telegram error ({method}): {response.text}")
    return response


def publish(item, post):
    headline = html.escape(str(post["headline"]).strip())
    body = html.escape(str(post["body"]).strip())
    footer = (
        f'<a href="{html.escape(FOOTER_URL, quote=True)}">'
        f'{html.escape(FOOTER_TEXT)}</a>'
    )

    # Источник новости намеренно не добавляется в публикацию.
    message = f"<b>{headline}</b>\n\n{body}\n\n{footer}"
    image = item.get("image") or og_image(item["link"])

    if image and len(message) <= 1024:
        response = telegram_request(
            "sendPhoto",
            chat_id=CHANNEL_ID,
            photo=image,
            caption=message,
            parse_mode="HTML",
        )
        if response.ok:
            return True
        print("Фото не отправилось, пробую опубликовать текстом")

    if image and len(message) > 1024:
        photo_response = telegram_request(
            "sendPhoto", chat_id=CHANNEL_ID, photo=image
        )
        if not photo_response.ok:
            print("Не удалось отправить фото; пробую отправить текст")

    response = telegram_request(
        "sendMessage",
        chat_id=CHANNEL_ID,
        text=message,
        parse_mode="HTML",
        disable_web_page_preview="true",
    )
    return response.ok


# ========================= ОЧЕРЕДЬ И ЗАПУСК =========================
def build_queue(state):
    items = collect(state)
    if not items:
        print("Новых новостей нет")
        return False

    chosen = choose_many(items, state, POSTS_PER_BATCH)
    if not chosen:
        print("Gemini не выбрал новости из найденных материалов")
        return False

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

    queue = []
    for item, post in zip(chosen, posts):
        if not post:
            continue
        entry = dict(item)
        entry["headline"] = post["headline"]
        entry["body"] = post["body"]
        queue.append(entry)

    if not queue:
        print("Не удалось подготовить ни одной новости к публикации")
        return False

    state["queue"] = queue
    state["built_at"] = time.time()
    save_state(state)
    print(f"Подготовлено новостей: {len(queue)}")
    return True


def main():
    state = load_state()

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
            time.sleep(30)
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


if __name__ == "__main__":
    main()
