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
MAX_AGE_HOURS = 48
POSTS_PER_BATCH = 3
QUEUE_MAX_AGE_MIN = 100
PER_FEED = 15
MAX_CANDIDATES = 70

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

# Основные RSS-ленты Roblox.
FEEDS = {
    "Roblox DevForum — Announcements": "https://devforum.roblox.com/c/updates/announcements/36.rss",
    "Roblox DevForum — Updates": "https://devforum.roblox.com/c/updates/45.rss",
    "Roblox Blog": "https://blog.roblox.com/feed/",
    "Try Hard Guides": "https://tryhardguides.com/tag/roblox/feed/",
    "Gamer Journalist": "https://gamerjournalist.com/roblox/feed",
    "Pro Game Guides": "https://progameguides.com/roblox/feed/",
    "Bloxidex": "https://bloxidex.com/rss.xml",
}
PLATFORM_SOURCES = {
    "Roblox DevForum — Announcements",
    "Roblox DevForum — Updates",
    "Roblox Blog",
}

# Популярность берём из актуальных подборок Roblox Discover, а не из
# вручную составленного списка. Игра должна быть в верхних подборках Roblox
# и иметь минимум 1 000 игроков онлайн на момент проверки.
ROBLOX_DISCOVER_URL = "https://apis.roblox.com/explore-api/v1/get-sort-content"
ROBLOX_SORTS = ("top-playing-now", "top-trending")
MIN_LIVE_PLAYERS = 1000
FILTER_VERSION = 3

# Оставляем только материалы, похожие на новость, а не рекламное описание игры.
NEWS_SIGNAL = re.compile(
    r"\b(update|updated|patch|event|codes?|redeem|release|released|"
    r"shutdown|banned|maintenance|leak|new feature|change|nerf|buff|"
    r"introduc(?:es|ed|ing)?|launch(?:es|ed)?|announc(?:es|ed|ement)?|"
    r"returns?|reveals?|unveil(?:s|ed)?|available now|free item|limited|"
    r"outage|server issue|tournament|season|quest|admin abuse|new map|new mode|new area|new item|item drop|challenge|free avatar|avatar item|"
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
CODE_WORD = re.compile(r"\b(codes?|промокод[ыов]?)\b", re.I)
CODE_TOKEN = re.compile(r"(?<![A-ZА-Я0-9])[A-Z0-9]{4,20}(?![A-ZА-Я0-9])", re.I)
CODE_LABEL = re.compile(r"\b(?:codes?|промокод[ыов]?)\s*[:：-]\s*", re.I)


def has_actual_codes(text):
    """Ищет код только рядом с явной подписью Codes/Промокоды."""
    for match in CODE_LABEL.finditer(text or ""):
        fragment = (text or "")[match.end():match.end() + 100]
        if CODE_TOKEN.search(fragment):
            return True
    return False


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


def fetch_popular_roblox_games():
    """Получает названия игр из живых топ-подборок Roblox Discover."""
    popular = {}
    session_id = f"timnews-{int(time.time())}"

    def walk(value):
        if isinstance(value, dict):
            name = value.get("name") or value.get("title")
            players = value.get("playerCount")
            if players is None:
                players = value.get("playing")
            if isinstance(name, str) and isinstance(players, (int, float)):
                if players >= MIN_LIVE_PLAYERS:
                    popular[norm(name)] = max(int(players), popular.get(norm(name), 0))
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

    if not popular:
        raise RuntimeError("Roblox Discover не вернул список популярных игр — публикация игр остановлена")

    print(f"[Roblox] Популярных игр для фильтра: {len(popular)}")
    return popular


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


def is_roblox_related(title, summary, popular_games):
    """Игра должна присутствовать в актуальном Roblox Discover top-листе."""
    text = norm(f"{title} {summary}")
    return any(name and name in text for name in popular_games)




def collect(state):
    seen_links = set(state["links"])
    seen_titles = set(state["titles"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    collected = []

    try:
        popular_games = fetch_popular_roblox_games()
    except Exception as error:
        print(f"[skip] Проверка популярных игр недоступна: {error}")
        print("[info] Продолжаю собирать официальные новости платформы; игровые новости пропущу")
        popular_games = {}

    for name, url in FEEDS.items():
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

            platform_news = name in PLATFORM_SOURCES
            if not platform_news and (
                not popular_games or not is_roblox_related(title, summary, popular_games)
            ):
                continue
            if not has_real_news(title, summary):
                print(f"[skip] Не похоже на новость: {title}")
                continue

            collected.append({
                "source": name,
                "title": title,
                "summary": summary,
                "link": link,
                "image": find_image(entry),
                "kind": "platform" if platform_news else "popular_game",
            })
            count += 1

        print(f"[ok] {name}: {count} новых подходящих новостей")

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
        f"{index}. {item['title']} — {item.get('summary', '')[:500]}" for index, item in enumerate(items)
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
Выбирай реальные новости Roblox всех типов: крупные события и ивенты;
обновления популярных игр, новые сезоны, карты, режимы и функции;
изменения платформы, аватары, предметы и бесплатные награды;
турниры, квесты, временные активности, рабочие коды и важные объявления.
Для отдельных игр используй только названия из актуальных подборок Roblox Discover.
Для платформенных новостей и крупных Roblox-ивентов конкретная игра не нужна.
Не выбирай рекламные описания, обзоры без нового события, пустые анонсы
и тексты в стиле «отправляйтесь в приключение».
Не выбирай новости о Minecraft, Fortnite, GTA или играх вне Roblox.
Если в заголовке обещаны коды, выбирай материал только если в исходном тексте
действительно указаны сами коды. Никогда не придумывай коды.
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
            f"[{index}] Заголовок: {item['title']}\nТекст: {item['summary']}"
        )

    prompt = f"""Перепиши новости для русскоязычного Telegram-канала только про Roblox.
Подходят новости о платформе, крупных ивентах, популярных играх, обновлениях,
сезонах, предметах, турнирах, квестах, кодах и важных изменениях.
Пиши простым живым языком. Названия игр, героев и функций оставляй как в оригинале.
Для каждой новости: пиши своими словами, если текст на другом языке — переведи.
Используй только факты из предоставленного текста, ничего не выдумывай.
Не превращай рекламное описание игры в новость. Не придумывай промокоды.
Если в исходном тексте нет конкретных кодов, не пиши о новых кодах.
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
Подходят новости о платформе, крупных ивентах, популярных играх, обновлениях,
сезонах, предметах, турнирах, квестах, кодах и важных изменениях.
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
        headline = str(post.get("headline", "")).strip()
        body = str(post.get("body", "")).strip()
        if not headline or not body:
            print(f"[skip] Пустая публикация: {item['title']}")
            continue
        if CODE_WORD.search(headline) and not has_actual_codes(item.get("summary", "")):
            print(f"[skip] Заголовок обещает коды, но кодов нет: {headline}")
            continue
        if not NEWS_SIGNAL.search(f"{headline} {body}"):
            print(f"[skip] Gemini сделал рекламный/пустой текст: {headline}")
            continue
        entry = dict(item)
        entry["headline"] = headline
        entry["body"] = body
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
