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

# ---------- Настройки ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]          # например @vsenovosti
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
# если основная модель недоступна или у неё кончился лимит, пробуем эти по очереди
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

# Подпись со ссылкой на ваш канал внизу каждого поста.
# Поменяйте текст на своё название.
CHANNEL_LINK_TEXT = "ВСЕ НОВОСТИ ⤵️"
if CHANNEL_ID.startswith("@"):
    CHANNEL_URL = "https://t.me/" + CHANNEL_ID[1:]
else:
    CHANNEL_URL = ""  # для закрытого канала вставьте ссылку-приглашение сюда

STATE_FILE = "posted.json"
MAX_AGE_HOURS = 24       # брать новости не старше N часов
POSTS_PER_BATCH = 3      # максимум постов за один запуск (обычно ИИ берёт 1, больше только при крупных новостях)
QUEUE_MAX_AGE_MIN = 100  # если очередь старше, она выбрасывается и собирается заново
PER_FEED = 15            # сколько последних записей брать из каждой ленты
MAX_CANDIDATES = 70      # сколько заголовков показывать ИИ

# Название -> адрес RSS. Сломанные ленты просто пропускаются.
FEEDS = {
    # Ленты только про Roblox (берутся все записи)
    "Try Hard Guides": "https://tryhardguides.com/tag/roblox/feed/",
    "Gamer Journalist": "https://gamerjournalist.com/roblox/feed",
    "Pro Game Guides": "https://progameguides.com/roblox/feed/",
    "Roblox DevForum": "https://devforum.roblox.com/c/updates/announcements/36.rss",
    "Roblox Blog": "https://blog.roblox.com/feed/",
}

# ---------- Новости про соцсети и мессенджеры ----------
# Из этих лент берутся только записи, где встречаются слова из SOCIAL_WORDS
SOCIAL_MAX_AGE_HOURS = 24
SOCIAL_FEEDS = {
    # Общие игровые и технические СМИ: берутся только записи, где упомянут Roblox
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Polygon": "https://www.polygon.com/rss/index.xml",
    "PC Gamer": "https://www.pcgamer.com/rss/",
    "GameSpot": "https://www.gamespot.com/feeds/news/",
    "IGN": "https://feeds.ign.com/ign/news",
    "Engadget": "https://www.engadget.com/rss.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "StopGame": "https://rss.stopgame.ru/rss_news.xml",
    "iXBT": "https://www.ixbt.com/export/news.rss",
    "3DNews": "https://3dnews.ru/news/rss/",
    "vc.ru": "https://vc.ru/rss",
    "Habr": "https://habr.com/ru/rss/news/",
}
SOCIAL_WORDS = r"roblox|роблокс|робукс|robux|\brbx\b"
SOCIAL_RE = re.compile(SOCIAL_WORDS, re.I)

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}


# ---------- Состояние (что уже опубликовано) ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("links", [])
    data.setdefault("titles", [])
    data.setdefault("headlines", [])
    data.setdefault("queue", [])
    data.setdefault("built_at", 0)
    return data


def save_state(state):
    state["links"] = state["links"][-600:]
    state["titles"] = state["titles"][-600:]
    state["headlines"] = state["headlines"][-40:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def norm(text):
    return re.sub(r"\W+", " ", text.lower()).strip()


def strip_tags(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ---------- Сбор новостей ----------
def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    blob = entry.get("summary", "") or ""
    for c in entry.get("content", []) or []:
        blob += c.get("value", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    if m:
        return m.group(1)
    return None


def og_image(url):
    try:
        r = requests.get(url, headers=UA, timeout=15)
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', r.text
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text
        )
        return html.unescape(m.group(1)) if m else None
    except requests.RequestException:
        return None


def collect(state):
    seen_links = set(state["links"])
    seen_titles = set(state["titles"])
    now = datetime.now(timezone.utc)
    world, social = [], []
    sources = [(n, u, False) for n, u in FEEDS.items()] + [
        (n, u, True) for n, u in SOCIAL_FEEDS.items()
    ]
    for name, url, is_social in sources:
        hours = SOCIAL_MAX_AGE_HOURS if is_social else MAX_AGE_HOURS
        cutoff = now - timedelta(hours=hours)
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[skip] {name}: {e}")
            continue
        count = 0
        for e in feed.entries[:PER_FEED]:
            link = e.get("link")
            title = strip_tags(e.get("title", ""))
            if not link or not title:
                continue
            if link in seen_links or norm(title) in seen_titles:
                continue
            t = entry_time(e)
            if t and t < cutoff:
                continue
            summary = strip_tags(e.get("summary", ""))[:1500]
            if is_social and not SOCIAL_RE.search(title + " " + summary):
                continue
            item = {
                "source": name,
                "title": title,
                "summary": summary,
                "link": link,
                "image": find_image(e),
                "social": is_social,
            }
            (social if is_social else world).append(item)
            count += 1
        print(f"[ok] {name}: {count} новых")
    social = spread(social, 20)
    world = spread(world, MAX_CANDIDATES - len(social))
    return world + social


def spread(items, limit):
    """Если новостей слишком много, берём поровну из каждого источника."""
    if len(items) <= limit:
        return items
    by_src = {}
    for it in items:
        by_src.setdefault(it["source"], []).append(it)
    out, i = [], 0
    while len(out) < limit:
        added = False
        for lst in by_src.values():
            if i < len(lst) and len(out) < limit:
                out.append(lst[i])
                added = True
        if not added:
            break
        i += 1
    return out


# ---------- Gemini ----------
def gemini_json(prompt):
    models = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    last = "нет ответа"
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(
                url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=90
            )
            if r.status_code == 429:
                last = f"{model}: лимит (429)"
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))
                    continue
                break
            if r.status_code in (500, 503):
                last = f"{model}: сбой {r.status_code}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 404:
                last = f"{model}: модель не найдена"
                print(last)
                break
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
        print("Переключаюсь на следующую модель:", last)
    raise RuntimeError(f"Gemini недоступен ({last})")


def choose_many(items, state, n):
    def tag(it):
        return ""

    listing = "\n".join(
        f"{i}. [{it['source']}]{tag(it)} {it['title']}" for i, it in enumerate(items)
    )
    recent = "\n".join(f"- {h}" for h in state["headlines"][-25:]) or "(пока ничего)"
    has_social = False
    social_rule = ""
    if has_social:
        social_rule = (
            "\nЗаголовки с пометкой (СОЦСЕТИ) касаются соцсетей и мессенджеров: новые функции, "
            "изменения дизайна, обновления. Среди выбранных обязательно должна быть минимум "
            "одна такая новость (самая интересная, новости про Telegram в приоритете). "
            "Её номер верни также в поле social_index.\n"
        )
    prompt = f"""Ты главный редактор Telegram-канала про игру Roblox для игроков.
Ниже пронумерованные заголовки из игровых СМИ и блогов за последние часы.

Недавно уже опубликовано:
{recent}

Выбери от 1 до {n} новостей. Обычно выбирай ОДНУ, самую важную. Две или три выбирай,
только если это действительно крупные события (большое обновление платформы, громкий ивент,
важное изменение правил или безопасности). Это должны быть РАЗНЫЕ истории:
если несколько СМИ пишут об одном событии, выбери только одну из них.
Не выбирай то, что повторяет уже опубликованное. Приоритет: обновления и новые функции платформы, события и ивенты, новые популярные игры,
изменения правил и безопасности, важные новости компании. Промокоды и гайды бери, только если
больше нет ничего важного. Избегай рекламы и пустых тем. Расположи выбранные по убыванию важности.
{social_rule}
Заголовки:
{listing}

Ответь JSON: {{"indices": [числа], "social_index": число или null}}"""
    data = gemini_json(prompt)
    out = []
    for i in data.get("indices", []):
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items) and items[i] not in out:
            out.append(items[i])
    out = out[:n]

    # гарантируем минимум одну новость про соцсети, если такие есть
    if has_social and not any(it.get("social") for it in out):
        try:
            si = int(data.get("social_index"))
            extra = items[si] if items[si].get("social") else None
        except (TypeError, ValueError, IndexError):
            extra = next((it for it in items if it.get("social")), None)
        if extra:
            if len(out) >= n:
                out[-1] = extra
            else:
                out.append(extra)
    return out


def rewrite(item):
    prompt = f"""Перепиши новость для русскоязычного Telegram-канала про Roblox для игроков.
Пиши простым живым языком, названия игр, героев и функций оставляй как в оригинале.
Правила: пиши на русском своими словами (если оригинал на другом языке, переведи),
только факты из текста ниже, ничего не выдумывай, нейтральный тон,
2-4 коротких предложения, не больше 600 символов, без ссылок и без названия источника.

Источник: {item['source']}
Заголовок: {item['title']}
Текст: {item['summary']}

Ответь JSON: {{"headline": "короткий заголовок до 90 символов", "body": "текст новости"}}"""
    return gemini_json(prompt)


def rewrite_many(items):
    """Переписывает сразу несколько новостей одним запросом (экономит лимит)."""
    blocks = []
    for i, it in enumerate(items):
        blocks.append(
            f"[{i}] Источник: {it['source']}\nЗаголовок: {it['title']}\nТекст: {it['summary']}"
        )
    prompt = f"""Перепиши новости для русскоязычного Telegram-канала про Roblox для игроков.
Пиши простым живым языком, названия игр, героев и функций оставляй как в оригинале.
Правила для каждой: пиши на русском своими словами (если оригинал на другом языке,
переведи), только факты из текста, ничего не выдумывай, нейтральный тон,
2-4 коротких предложения, не больше 600 символов, без ссылок и без названия источника.

Новости:
{chr(10).join(blocks)}

Ответь JSON: {{"posts": [{{"headline": "заголовок до 90 символов", "body": "текст"}}]}}
Ровно {len(items)} элементов, в том же порядке."""
    posts = gemini_json(prompt)["posts"]
    if len(posts) != len(items):
        raise ValueError("число новостей не совпало")
    return posts


# ---------- Telegram ----------
def tg(method, **kwargs):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", data=kwargs, timeout=60
    )
    if not r.ok:
        print("Telegram error:", r.text)
    return r


def publish(item, post):
    headline = html.escape(post["headline"].strip())
    body = html.escape(post["body"].strip())
    source = f'🔗 <a href="{html.escape(item["link"], quote=True)}">{html.escape(item["source"])}</a>'
    text = f"<b>{headline}</b>\n\n{body}\n\n{source}"
    if CHANNEL_URL:
        text += f'\n\n📲 <a href="{CHANNEL_URL}">{html.escape(CHANNEL_LINK_TEXT)}</a>'

    image = item["image"] or og_image(item["link"])
    if image and len(text) <= 1024:
        r = tg("sendPhoto", chat_id=CHANNEL_ID, photo=image, caption=text, parse_mode="HTML")
        if r.ok:
            return True
        print("Фото не прошло, отправляю без фото")
    r = tg(
        "sendMessage",
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true" if not image else "false",
    )
    return r.ok


# ---------- Главная часть ----------
def build_queue(state):
    items = collect(state)
    if not items:
        print("Новых новостей нет")
        return False
    chosen = choose_many(items, state, POSTS_PER_BATCH)
    if not chosen:
        return False
    try:
        posts = rewrite_many(chosen)
    except Exception as e:
        print("Общий пересказ не удался, переписываю по одной:", e)
        posts = []
        for it in chosen:
            try:
                posts.append(rewrite(it))
            except Exception as e2:
                print("Не удалось переписать:", it["title"], e2)
                posts.append(None)
    queue = []
    for it, post in zip(chosen, posts):
        if not post:
            continue
        entry = dict(it)
        entry["headline"] = post["headline"]
        entry["body"] = post["body"]
        queue.append(entry)
        print("В очереди:", it["source"], "|", it["title"])
    if not queue:
        return False
    state["queue"] = queue
    state["built_at"] = time.time()
    save_state(state)
    return True


def main():
    state = load_state()

    if state["queue"] and time.time() - state["built_at"] > QUEUE_MAX_AGE_MIN * 60:
        print("Очередь устарела, собираю заново")
        state["queue"] = []

    if not state["queue"]:
        if not build_queue(state):
            return

    first = True
    while state["queue"]:
        if not first:
            time.sleep(30)  # пауза между постами одной подборки
        first = False
        item = state["queue"][0]
        post = {"headline": item["headline"], "body": item["body"]}
        if not publish(item, post):
            sys.exit("Не удалось опубликовать")
        state["queue"].pop(0)
        state["links"].append(item["link"])
        state["titles"].append(norm(item["title"]))
        state["headlines"].append(post["headline"].strip())
        save_state(state)
        print("Опубликовано:", post["headline"])
    print("Готово")


if __name__ == "__main__":
    main()
