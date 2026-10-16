"""
Кино-новинки — еженедельная подборка новых фильмов, сериалов и сезонов в Telegram.

Бот ищет фильмы, которые вышли в цифре (стриминг, покупка, онлайн-прокат), премьеры новых
сериалов и новые сезоны, проверяет оценки IMDb и Кинопоиска и присылает карточки с постером,
кратким сюжетом и списком сервисов, где можно посмотреть.

Код менять не нужно: всё настраивается в settings.txt.
Ключи берутся из переменных окружения: TMDB_API_KEY, KINOPOISK_API_KEY, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, для советов по запросу — ANTHROPIC_API_KEY, по желанию — OMDB_API_KEY.
"""
import datetime as dt
import json
import os
import re
import time
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Riga")
STATE_FILE = os.path.join(HERE, "state.json")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
WD_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
DAY_ALIASES = {"пн": 0, "по": 0, "вт": 1, "ср": 2, "чт": 3, "че": 3, "пт": 4, "пя": 4,
               "сб": 5, "су": 5, "вс": 6, "во": 6}

TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()
KP_KEY = os.environ.get("KINOPOISK_API_KEY", "").strip()
OMDB_KEY = os.environ.get("OMDB_API_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
FORCE_DIGEST = (os.environ.get("FORCE_DIGEST") or "").lower() == "true"

# Что произошло за запуск — показывается в конце подборки, если что-то пошло не так
DIAG = {"errors": [], "tmdb": 0, "kp": 0, "kp_cached": 0, "omdb": 0, "candidates": 0}


# ================= Настройки =================
def lines_of(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f if l.strip() and not l.lstrip().startswith("#")]


def load_settings():
    s = {}
    for l in lines_of("settings.txt"):
        if "=" in l:
            k, v = l.split("=", 1)
            s[k.strip()] = v.strip()
    defaults = {"digest_day": "пт", "digest_hour": "18", "include": "фильмы, сериалы, сезоны",
                "min_imdb": "7.5", "min_kp": "7.5", "require": "both",
                "min_imdb_votes": "1000", "min_kp_votes": "500", "lookback_days": "45",
                "watch_country": "LV", "regions": "US, GB, DE",
                "exclude_genres": "реалити-шоу, ток-шоу, новости, мыльная опера",
                "max_cards": "20", "kp_requests_per_run": "150", "show_diagnostics": "auto",
                "rec_count": "3", "rec_days": "180", "rec_min_rating": "7.0",
                "model": "claude-haiku-5-5", "monthly_budget_usd": "1.00"}
    for k, v in defaults.items():
        s.setdefault(k, v)
    s["digest_weekday"] = DAY_ALIASES.get(s["digest_day"].lower()[:2], 4)
    for k in ("digest_hour", "min_imdb_votes", "min_kp_votes", "lookback_days", "max_cards",
              "kp_requests_per_run", "rec_count", "rec_days"):
        try:
            s[k] = int(s[k])
        except ValueError:
            s[k] = int(defaults[k])
    for k in ("min_imdb", "min_kp", "rec_min_rating", "monthly_budget_usd"):
        try:
            s[k] = float(s[k].replace(",", "."))
        except ValueError:
            s[k] = float(defaults[k])
    inc = s["include"].lower()
    s["kinds"] = {k for k, w in (("movie", "фильм"), ("tv", "сериал"), ("season", "сезон"))
                  if w in inc}
    s["regions"] = [r.strip().upper() for r in s["regions"].split(",") if r.strip()]
    s["watch_country"] = s["watch_country"].strip().upper() or "LV"
    s["exclude"] = {g.strip().lower() for g in s["exclude_genres"].split(",") if g.strip()}
    s["require_any"] = s["require"].lower() in ("any", "любая", "одна", "или")
    return s


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# ================= Мелочи =================
def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def nice_date(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def short_plot(text, limit=380):
    """Сюжет коротко: целые предложения, не длиннее limit символов."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit // 3:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:—-") + "…"


def num(v):
    """'8.1', 8.1, '12,345' → число; пусто и 0 → None."""
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x or None


# ================= TMDB =================
def tmdb(path, **params):
    headers = {"accept": "application/json"}
    if TMDB_KEY.startswith("eyJ"):             # токен доступа (v4) вместо короткого ключа
        headers["Authorization"] = f"Bearer {TMDB_KEY}"
    else:
        params["api_key"] = TMDB_KEY
    err = ""
    for attempt in range(3):
        try:
            r = requests.get(f"https://api.themoviedb.org/3{path}", params=params,
                             headers=headers, timeout=30)
            DIAG["tmdb"] += 1
            if r.status_code == 429:
                time.sleep(5)
                continue
            if r.status_code == 401:
                err = "TMDB: ключ не подходит"
                break
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = f"TMDB: {str(e)[:80]}"
            time.sleep(3)
    DIAG["errors"].append(err)
    print(err, path)
    return {}


def discover(path, cfg, params, pages=3):
    out = {}
    for page in range(1, pages + 1):
        j = tmdb(path, language="ru-RU", include_adult="false", page=page, **params)
        for x in j.get("results", []):
            out.setdefault(x["id"], x)
        if page >= (j.get("total_pages") or 1):
            break
    return out


def genres_of(d):
    return [g.get("name", "").lower() for g in d.get("genres", []) if g.get("name")]


def providers_of(d, country):
    p = (d.get("watch/providers") or {}).get("results", {}).get(country) or {}
    names = lambda key: [x.get("provider_name") for x in p.get(key, []) if x.get("provider_name")]
    sub = list(dict.fromkeys(names("flatrate") + names("free") + names("ads")))
    paid = [n for n in dict.fromkeys(names("rent") + names("buy")) if n not in sub]
    return {"sub": sub[:5], "paid": paid[:4], "link": p.get("link", "")}


def by_popularity(found, limit):
    pairs = sorted(found.items(), key=lambda kv: -(kv[1].get("popularity") or 0))
    return pairs[:limit] if limit else pairs


def movie_items(cfg, start, end, extra=None, limit=None, pages=3):
    """Фильмы, которые впервые вышли в цифре (где угодно в мире) с start по end.
    extra — дополнительные условия поиска (например, жанр), limit — сколько проверять."""
    found = {}
    for region in cfg["regions"]:
        found.update(discover("/discover/movie", cfg, {
            "region": region, "with_release_type": "4", "sort_by": "popularity.desc",
            "release_date.gte": start.isoformat(), "release_date.lte": end.isoformat(),
            **(extra or {})}, pages=pages))
    items = []
    for mid, m in by_popularity(found, limit):
        if (m.get("vote_count") or 0) >= 30 and (m.get("vote_average") or 10) < 6.0:
            continue                            # у TMDB уже явно низкая оценка — не тратим запросы
        d = tmdb(f"/movie/{mid}", language="ru-RU",
                 append_to_response="release_dates,external_ids,watch/providers")
        if not d:
            continue
        digital = [parse_date(r.get("release_date"))
                   for c in (d.get("release_dates") or {}).get("results", [])
                   for r in c.get("release_dates", []) if r.get("type") == 4]
        digital = [x for x in digital if x]
        if not digital or not start <= min(digital) <= end:
            continue                            # в цифре давно (или ещё нет)
        year = (d.get("release_date") or "")[:4]
        items.append({
            "kind": "movie", "key": f"movie:{mid}", "tmdb_id": mid, "date": min(digital),
            "title": d.get("title") or m.get("title"), "orig": d.get("original_title"),
            "year": year, "genres": genres_of(d), "runtime": d.get("runtime") or 0,
            "plot": d.get("overview") or "", "imdb_id": (d.get("external_ids") or {}).get("imdb_id")
            or d.get("imdb_id"), "poster": d.get("poster_path"),
            "providers": providers_of(d, cfg["watch_country"]),
            "tmdb_url": f"https://www.themoviedb.org/movie/{mid}",
            "popularity": m.get("popularity") or 0})
    return items


def tv_items(cfg, start, end, kinds=None, extra=None, limit=None, pages=3):
    """Новые сериалы (премьера с start по end) и новые сезоны идущих сериалов."""
    kinds = cfg["kinds"] if kinds is None else kinds
    shows = {}
    if "tv" in kinds:
        shows.update(discover("/discover/tv", cfg, {
            "sort_by": "popularity.desc", "first_air_date.gte": start.isoformat(),
            "first_air_date.lte": end.isoformat(), **(extra or {})}, pages=pages))
    if "season" in kinds:
        shows.update(discover("/discover/tv", cfg, {
            "sort_by": "popularity.desc", "vote_count.gte": "50",
            "air_date.gte": start.isoformat(), "air_date.lte": end.isoformat(),
            **(extra or {})}, pages=pages + 1))
    items = []
    for sid, s in by_popularity(shows, limit):
        if (s.get("vote_count") or 0) >= 30 and (s.get("vote_average") or 10) < 6.0:
            continue
        d = tmdb(f"/tv/{sid}", language="ru-RU", append_to_response="external_ids,watch/providers")
        if not d:
            continue
        base = {"tmdb_id": sid, "title": d.get("name") or s.get("name"),
                "orig": d.get("original_name"), "year": (d.get("first_air_date") or "")[:4],
                "genres": genres_of(d), "plot": d.get("overview") or "",
                "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
                "poster": d.get("poster_path"), "providers": providers_of(d, cfg["watch_country"]),
                "networks": [n.get("name") for n in d.get("networks", []) if n.get("name")][:2],
                "tmdb_url": f"https://www.themoviedb.org/tv/{sid}",
                "popularity": s.get("popularity") or 0}
        first = parse_date(d.get("first_air_date"))
        if "tv" in kinds and first and start <= first <= end:
            items.append({**base, "kind": "tv", "key": f"tv:{sid}", "date": first,
                          "seasons": d.get("number_of_seasons") or 1})
            continue
        if "season" not in kinds:
            continue
        for se in d.get("seasons", []):
            n, sd = se.get("season_number") or 0, parse_date(se.get("air_date"))
            if n >= 2 and sd and start <= sd <= end:
                items.append({**base, "kind": "season", "key": f"season:{sid}:{n}", "date": sd,
                              "season": n, "episodes": se.get("episode_count") or 0,
                              "plot": se.get("overview") or base["plot"],
                              "poster": se.get("poster_path") or base["poster"]})
    return items


# ================= Оценки: Кинопоиск и IMDb =================
KP_BASES = ["https://api.poiskkino.dev", "https://api.kinopoisk.dev"]   # сервис переехал
KP = {"base": None, "left": 0, "off": ""}


def kp_get(path, params):
    if KP["off"] or KP["left"] <= 0:
        return None
    bases = [KP["base"]] if KP["base"] else KP_BASES
    for base in bases:
        try:
            r = requests.get(base + path, params=params, headers={"X-API-KEY": KP_KEY},
                             timeout=30)
        except Exception as e:
            print(f"[kp] {base}: {e}")
            continue
        KP["left"] -= 1
        DIAG["kp"] += 1
        if r.status_code in (401, 403):
            KP["off"] = "Кинопоиск: ключ не подходит"
            DIAG["errors"].append(KP["off"])
            return None
        if r.status_code in (402, 429):
            KP["off"] = "Кинопоиск: дневной лимит запросов исчерпан"
            DIAG["errors"].append(KP["off"])
            return None
        if r.ok:
            KP["base"] = base
            return r.json()
        print(f"[kp] {base}{path}: {r.status_code} {r.text[:150]}")
    return None


def kp_record(doc, today):
    rating, votes = doc.get("rating") or {}, doc.get("votes") or {}
    return {"checked": today.isoformat(), "kp_id": doc.get("id"), "kp": num(rating.get("kp")),
            "kp_votes": int(num(votes.get("kp")) or 0), "imdb": num(rating.get("imdb")),
            "imdb_votes": int(num(votes.get("imdb")) or 0), "name": doc.get("name") or "",
            "desc": doc.get("shortDescription") or doc.get("description") or "",
            "poster": (doc.get("poster") or {}).get("url") or ""}


def kp_lookup(it, state, today):
    """Оценки Кинопоиска (и IMDb по данным Кинопоиска). Проверяем не чаще раза в 6 дней."""
    cache = state.setdefault("ratings", {})
    ck = it.get("imdb_id") or it["key"]
    rec = cache.get(ck)
    if rec and (today - parse_date(rec["checked"])).days < 6:
        DIAG["kp_cached"] += 1
        return rec
    doc = None
    if it.get("imdb_id"):
        j = kp_get("/v1.4/movie", {"externalId.imdb": it["imdb_id"], "limit": 1})
        doc = ((j or {}).get("docs") or [None])[0]
    if not doc and not KP["off"] and KP["left"] > 0:        # нет IMDb-номера — ищем по названию
        j = kp_get("/v1.4/movie/search", {"query": it.get("orig") or it["title"], "limit": 5})
        for c in (j or {}).get("docs") or []:
            if it.get("year") and str(c.get("year")) not in (it["year"], str(int(it["year"]) + 1)):
                continue
            doc = c
            break
    if doc is None:
        return rec                              # не нашли или кончился лимит — старые данные
    rec = kp_record(doc, today)
    cache[ck] = rec
    return rec


def omdb_lookup(imdb_id, rec):
    """Свежая оценка IMDb из OMDb (если есть ключ OMDB_API_KEY)."""
    if not OMDB_KEY or not imdb_id or rec.get("omdb_checked") == rec.get("checked"):
        return
    try:
        j = requests.get("https://www.omdbapi.com/", params={"i": imdb_id, "apikey": OMDB_KEY},
                         timeout=20).json()
        DIAG["omdb"] += 1
    except Exception as e:
        DIAG["errors"].append(f"OMDb: {str(e)[:60]}")
        return
    rec["omdb_checked"] = rec.get("checked")
    if num(j.get("imdbRating")):
        rec["imdb"] = num(j["imdbRating"])
        rec["imdb_votes"] = int(num(j.get("imdbVotes")) or 0)


def passes(rec, cfg, min_imdb=None, min_kp=None):
    if not rec:
        return False
    min_imdb = cfg["min_imdb"] if min_imdb is None else min_imdb
    min_kp = cfg["min_kp"] if min_kp is None else min_kp
    ok_imdb = (rec.get("imdb") or 0) >= min_imdb and rec.get("imdb_votes", 0) >= cfg["min_imdb_votes"]
    ok_kp = (rec.get("kp") or 0) >= min_kp and rec.get("kp_votes", 0) >= cfg["min_kp_votes"]
    return (ok_imdb or ok_kp) if cfg["require_any"] else (ok_imdb and ok_kp)


# ================= Telegram =================
CHAT = {"id": TG_CHAT, "last": 0.0, "err": ""}


def tg_api(method, payload):
    if not TG_TOKEN:
        print(f"[tg] нет TELEGRAM_BOT_TOKEN — пропускаю {method}")
        return None
    for attempt in range(4):
        try:
            j = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload,
                              timeout=60).json()
        except Exception as e:
            CHAT["err"] = str(e)[:200]
            time.sleep(3)
            continue
        if j.get("ok"):
            return j.get("result")
        params = j.get("parameters") or {}
        if params.get("retry_after"):
            time.sleep(int(params["retry_after"]) + 1)
            continue
        if params.get("migrate_to_chat_id"):            # группа стала супергруппой
            CHAT["id"] = payload["chat_id"] = str(params["migrate_to_chat_id"])
            continue
        CHAT["err"] = str(j.get("description", ""))[:200]
        print(f"Telegram {method}: {CHAT['err']}")
        return None
    return None


def tg_send(text, buttons=None, photo=None, notify=False, chat=None, reply_to=None):
    """Сообщение или постер с подписью; не чаще 20 сообщений в минуту (ограничение для групп)."""
    wait = 3.1 - (time.time() - CHAT["last"])
    if wait > 0:
        time.sleep(wait)
    base = {"chat_id": chat or CHAT["id"], "parse_mode": "HTML", "disable_notification": not notify}
    if reply_to:
        base["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    rows = [[b for b in row if str(b.get("url", "")).startswith("http")] for row in (buttons or [])]
    rows = [r for r in rows if r]
    if rows:
        base["reply_markup"] = {"inline_keyboard": rows}
    res = None
    if photo:
        res = tg_api("sendPhoto", {**base, "photo": photo, "caption": text})
    if res is None:                                     # постер не загрузился — просто текстом
        res = tg_api("sendMessage", {**base, "text": text,
                                     "link_preview_options": {"is_disabled": True}})
    CHAT["last"] = time.time()
    return res


# ================= Карточки =================
KIND_ICON = {"movie": "🎬", "tv": "📺", "season": "🆕"}


def runtime_text(m):
    h, mm = divmod(int(m or 0), 60)
    return (f"{h} ч {mm} мин" if mm else f"{h} ч") if h else (f"{mm} мин" if mm else "")


def card(it, cfg, rec, why=""):
    title = rec.get("name") or it["title"]
    sub = []
    if it.get("orig") and it["orig"].lower() != title.lower():
        sub.append(it["orig"])
    if it.get("year"):
        sub.append(it["year"])
    lines = [f"{KIND_ICON[it['kind']]} <b>{esc(title)}</b>" + (f" ({esc(', '.join(sub))})" if sub else "")]
    info = [", ".join(it.get("genres", [])[:3])]
    if it["kind"] == "movie":
        info.append(runtime_text(it.get("runtime")))
    info = " · ".join(x for x in info if x)
    if info:
        lines.append(f"<i>{esc(info)}</i>")
    r = []
    if rec.get("imdb"):
        r.append(f"IMDb <b>{rec['imdb']:.1f}</b>")
    if rec.get("kp"):
        r.append(f"Кинопоиск <b>{rec['kp']:.1f}</b>")
    lines.append("⭐ " + " · ".join(r))
    net = f" · {esc(', '.join(it['networks']))}" if it.get("networks") else ""
    if it["kind"] == "movie":
        lines.append(f"📅 В цифре с {nice_date(it['date'])}")
    elif it["kind"] == "tv":
        lines.append(f"📅 Премьера сериала {nice_date(it['date'])}{net}")
    else:
        eps = (f", {it['episodes']} {plural(it['episodes'], 'серия', 'серии', 'серий')}"
               if it.get("episodes") else "")
        lines.append(f"📅 {it['season']} сезон с {nice_date(it['date'])}{eps}{net}")
    p = it.get("providers") or {}
    if p.get("sub"):
        lines.append(f"📺 По подписке: {esc(', '.join(p['sub']))}")
    if p.get("paid"):
        lines.append(f"💳 Купить или арендовать: {esc(', '.join(p['paid']))}")
    if why:
        lines.append(f"\n💡 {esc(why)}")
    head = "\n".join(lines)
    plot = it.get("plot") or rec.get("desc") or ""
    room = 1000 - len(head) - 30                        # подпись к фото — до 1024 символов
    plot = short_plot(plot, min(380, max(room, 0)))
    if plot:
        head += f"\n\n<blockquote>{esc(plot)}</blockquote>"
    return head


def card_buttons(it, rec):
    row = []
    if rec.get("kp_id"):
        row.append({"text": "Кинопоиск", "url": f"https://www.kinopoisk.ru/film/{rec['kp_id']}/"})
    if it.get("imdb_id"):
        row.append({"text": "IMDb", "url": f"https://www.imdb.com/title/{it['imdb_id']}/"})
    row.append({"text": "Где смотреть", "url": (it.get("providers") or {}).get("link")
                or it["tmdb_url"]})
    return [row]


def poster_url(it, rec):
    if it.get("poster"):
        return f"https://image.tmdb.org/t/p/w500{it['poster']}"
    return rec.get("poster") or None


def compact(it, rec):
    title = rec.get("name") or it["title"]
    r = " · ".join(x for x in (f"IMDb {rec['imdb']:.1f}" if rec.get("imdb") else "",
                               f"КП {rec['kp']:.1f}" if rec.get("kp") else "") if x)
    extra = f" · {it['season']} сезон" if it["kind"] == "season" else ""
    return f"• {KIND_ICON[it['kind']]} <b>{esc(title)}</b>{extra} — {r}"


# ================= Подборка =================
def diag_text(cfg):
    mode = cfg["show_diagnostics"].lower()
    if mode == "never" or (mode != "always" and not DIAG["errors"]):
        return ""
    t = (f"⚙️ Диагностика: кандидатов {DIAG['candidates']}, запросов к TMDB {DIAG['tmdb']}, "
         f"к Кинопоиску {DIAG['kp']} (из памяти {DIAG['kp_cached']}), к OMDb {DIAG['omdb']}")
    if DIAG["errors"]:
        t += "\nОшибки: " + "; ".join(dict.fromkeys(DIAG["errors"]))[:400]
    return t


def run_digest(state, cfg, today):
    start = today - dt.timedelta(days=cfg["lookback_days"])
    announced = state.setdefault("announced", {})
    items = []
    if "movie" in cfg["kinds"]:
        items += movie_items(cfg, start, today)
    if cfg["kinds"] & {"tv", "season"}:
        items += tv_items(cfg, start, today)
    items = [it for it in items if it["key"] not in announced
             and not (set(it.get("genres", [])) & cfg["exclude"])]
    DIAG["candidates"] = len(items)
    print(f"Кандидатов: {len(items)}")

    # Сначала самые популярные: если лимит Кинопоиска кончится, проверим главное
    if not KP_KEY:
        DIAG["errors"].append("нет ключа KINOPOISK_API_KEY")
    picked = []
    for it in sorted(items, key=lambda x: -x["popularity"]):
        rec = kp_lookup(it, state, today) if KP_KEY else None
        rec = rec or {}
        kp_ok = (rec.get("kp") or 0) >= cfg["min_kp"] and rec.get("kp_votes", 0) >= cfg["min_kp_votes"]
        # OMDb спрашиваем только когда от IMDb ещё что-то зависит — бережём его лимит
        if OMDB_KEY and it.get("imdb_id") and (kp_ok or cfg["require_any"] or not KP_KEY):
            if not rec:
                rec = {"checked": today.isoformat()}
                state.setdefault("ratings", {})[it["imdb_id"]] = rec
            omdb_lookup(it["imdb_id"], rec)
        if passes(rec, cfg):
            picked.append((it, rec))

    order = {"movie": 0, "tv": 1, "season": 2}
    picked.sort(key=lambda p: (order[p[0]["kind"]], -((p[1].get("imdb") or 0) + (p[1].get("kp") or 0))))
    n = {k: sum(1 for it, _ in picked if it["kind"] == k) for k in order}

    cover = ["🍿 <b>КИНО-НОВИНКИ</b>",
             f"<i>вышло в цифре и на стримингах · IMDb и Кинопоиск от "
             f"{min(cfg['min_imdb'], cfg['min_kp']):g}</i>", ""]
    if picked:
        if n["movie"]:
            cover.append(f"🎬 Фильмы — {n['movie']}")
        if n["tv"]:
            cover.append(f"📺 Новые сериалы — {n['tv']}")
        if n["season"]:
            cover.append(f"🆕 Новые сезоны — {n['season']}")
    else:
        cover.append("На этой неделе новинок с такими оценками не появилось.")
    cover += ["", "<i>Данные: TMDB, JustWatch, Кинопоиск, IMDb</i>"]
    tg_send("\n".join(cover), notify=True)

    overflow = []
    for k, (it, rec) in enumerate(picked):
        if k >= cfg["max_cards"]:
            overflow.append((it, rec))
            continue
        res = tg_send(card(it, cfg, rec), card_buttons(it, rec), poster_url(it, rec))
        if res:
            announced[it["key"]] = today.isoformat()
            remember_card(state, res, it, rec)
    if overflow:
        res = tg_send("📋 <b>И ЕЩЁ</b>\n" + "\n".join(compact(it, rec) for it, rec in overflow))
        if res:
            for it, _ in overflow:
                announced[it["key"]] = today.isoformat()
    diag = diag_text(cfg)
    if diag:
        tg_send(f"<i>{esc(diag)}</i>")

    # память: что уже присылали (полтора года) и оценки (только свежие)
    old = (today - dt.timedelta(days=550)).isoformat()
    state["announced"] = {k: v for k, v in announced.items() if v >= old}
    old = (today - dt.timedelta(days=cfg["lookback_days"] + 14)).isoformat()
    state["ratings"] = {k: v for k, v in state.get("ratings", {}).items()
                        if v.get("checked", "") >= old}
    print(f"Отправлено: {len(picked)}. Запросов: TMDB {DIAG['tmdb']}, Кинопоиск {DIAG['kp']}")


# ================= Вкус: опрос и оценки 👍 / 👎 =================
def remember_card(state, res, it, rec):
    """Запоминаем карточку, чтобы понять, что именно оценили."""
    mid = (res or {}).get("message_id")
    if not mid:
        return
    cards = state.setdefault("cards", {})
    cards[f"{(res.get('chat') or {}).get('id', CHAT['id'])}:{mid}"] = {
        "key": it["key"], "t": rec.get("name") or it["title"], "o": it.get("orig") or "",
        "y": it.get("year") or "", "g": ", ".join(it.get("genres", [])[:3]),
        "k": "сериал" if it["kind"] != "movie" else "фильм"}
    while len(cards) > 400:
        cards.pop(next(iter(cards)))


def load_taste(state):
    """Вкус: опрос (taste.txt) и оценки карточек в Telegram. → {love, like, nope: [строки]}"""
    out = {"love": [], "like": [], "nope": []}
    for l in lines_of("taste.txt"):
        p = [x.strip() for x in l.split("|")]
        if len(p) < 2:
            continue
        name = p[1] + (f" / {p[2]}" if len(p) > 2 and p[2] and p[2] != p[1] else "")
        name += f" ({', '.join(x for x in p[3:6] if x)})" if len(p) > 3 else ""
        mark = p[0].replace("️", "")
        key = "love" if mark in ("❤", "♥", "++", "*") else "like" if mark == "+" else \
            "nope" if mark in ("-", "−", "–") else None
        if key:
            out[key].append(name)
    for r in state.get("taste", {}).values():
        name = f"{r.get('t')}" + (f" / {r['o']}" if r.get("o") and r["o"] != r.get("t") else "") \
            + f" ({', '.join(x for x in (r.get('y'), r.get('k'), r.get('g')) if x)})"
        out["like" if r.get("v", 0) > 0 else "nope"].append(name)
    return out


def taste_known(state):
    """Названия, которые зритель уже оценил: их не советуем."""
    names = set()
    for l in lines_of("taste.txt"):
        p = [x.strip().lower() for x in l.split("|")]
        names.update(x for x in p[1:3] if x)
    for k, r in state.get("taste", {}).items():
        names.update(x.lower() for x in (r.get("t"), r.get("o")) if x)
        names.add(k)
    return names


POS = {"👍", "❤", "🔥", "🥰", "👏", "😍", "🤩", "💯", "🏆", "👌", "❤‍🔥", "🍿"}
NEG = {"👎", "💩", "🤮", "😴", "🥱", "🙈", "💔", "😐", "🤡"}
YES_WORDS = {"+", "+1", "да", "хочу", "круто", "ок", "смотрел, понравилось", "понравилось"}
NO_WORDS = {"-", "–", "—", "нет", "мимо", "не моё", "не мое", "не понравилось", "не хочу"}


def text_vote(t):
    t = str(t or "").replace("️", "").strip().lower().strip(".!)( ")
    if t in YES_WORDS:
        return 1
    if t in NO_WORDS:
        return -1
    marks = [ch for ch in t if not ch.isspace()]
    if not marks or any(ch.isalnum() for ch in marks):
        return 0
    s = "".join(marks)
    return -1 if any(e in s for e in NEG) else 1 if any(e in s for e in POS) else 0


def apply_vote(state, ck, v):
    c = state.get("cards", {}).get(ck)
    if not c:
        return False
    taste = state.setdefault("taste", {})
    if v:
        taste.pop(c["key"], None)
        taste[c["key"]] = {**{k: c[k] for k in ("t", "o", "y", "g", "k")}, "v": v}
    else:
        taste.pop(c["key"], None)
    while len(taste) > 300:
        taste.pop(next(iter(taste)))
    return True


# ================= ИИ (Claude Haiku) =================
def claude(prompt, state, cfg, max_tokens=1500):
    """Короткий вызов Claude; ответ — список JSON-объектов. Без ключа или сверх бюджета — []."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return []
    today = dt.date.today()
    u = state.setdefault("usage", {"month": f"{today:%Y-%m}", "cost": 0.0, "calls": 0})
    if u.get("month") != f"{today:%Y-%m}":
        u.update(month=f"{today:%Y-%m}", cost=0.0, calls=0)
    if u["cost"] >= cfg["monthly_budget_usd"]:
        DIAG["errors"].append("месячный бюджет ИИ исчерпан")
        return []
    model = cfg["model"]
    p_in, p_out = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0)}.get(
        next((k for k in ("haiku", "sonnet") if k in model), "haiku"))
    for attempt in range(3):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, json={
                "model": model, "max_tokens": max_tokens,
                "system": "Отвечай сразу JSON, без пояснений и markdown. Текст — по-русски.",
                "messages": [{"role": "user", "content": prompt}]},
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
        except Exception as e:
            time.sleep(5)
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(10 * (attempt + 1))
            continue
        if not r.ok:
            DIAG["errors"].append(f"Claude {r.status_code}")
            print("Claude:", r.text[:300])
            return []
        j = r.json()
        us = j.get("usage", {})
        u["cost"] += (us.get("input_tokens", 0) * p_in + us.get("output_tokens", 0) * p_out) / 1e6
        u["calls"] += 1
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        return parse_objects(text)
    DIAG["errors"].append("Claude недоступен")
    return []


def parse_objects(text):
    dec, out, i = json.JSONDecoder(), [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                out.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1


# ================= Рекомендации по запросу =================
# Жанры TMDB: (основа слова в запросе, название, жанры фильмов, жанры сериалов)
GENRES = [
    (r"ужас|хорр?ор|страшн|ужастик", "ужасы", [27], [9648, 10765]),
    (r"триллер|напряж|саспенс", "триллеры", [53], [9648, 80]),
    (r"комед|смешн|посмеят|ржать|лёгк|легк", "комедии", [35], [35]),
    (r"драм", "драмы", [18], [18]),
    (r"фантаст|sci.?fi|космос", "фантастика", [878], [10765]),
    (r"фэнтез|фентез|магия|волшеб", "фэнтези", [14], [10765]),
    (r"боевик|экшн|экшен|action", "боевики", [28], [10759]),
    (r"криминал|гангстер|мафи|ограблен", "криминал", [80], [80]),
    (r"детектив|расследов|загадк", "детективы", [9648, 80], [9648, 80]),
    (r"мелодрам|романт|любов|про отношен", "мелодрамы", [10749], [18]),
    (r"мульт|анимац|аниме", "мультфильмы", [16], [16]),
    (r"военн|войн", "военное", [10752], [10768]),
    (r"историч|истори", "историческое", [36], [10768]),
    (r"документ", "документальное", [99], [99]),
    (r"приключен", "приключения", [12], [10759]),
    (r"семейн|с детьми|для детей", "семейное", [10751], [10751]),
]

PARSE_PROMPT = """Зритель написал боту-киносоветчику: «{q}»
Определи, что он хочет посмотреть. Жанры фильмов TMDB: 28 боевик, 12 приключения, 16 мультфильм,
35 комедия, 80 криминал, 99 документальный, 18 драма, 10751 семейный, 14 фэнтези, 36 история,
27 ужасы, 10402 музыка, 9648 детектив, 10749 мелодрама, 878 фантастика, 53 триллер, 10752 военный,
37 вестерн. Жанры сериалов TMDB: 10759 боевик и приключения, 16 мультфильм, 35 комедия,
80 криминал, 99 документальный, 18 драма, 10751 семейный, 9648 детектив, 10765 фантастика и
фэнтези, 10768 война и политика, 37 вестерн.
Ответь ОДНИМ JSON-объектом:
{{"type": "movie|tv|any", "movie_genres": [id, ...], "tv_genres": [id, ...],
  "label": "что ищем, 1–3 слова по-русски во множественном числе (например «ужасы», «лёгкие комедии»)",
  "wish": "особые пожелания из запроса одной фразой или ''"}}
Если жанр не назван («что посмотреть вечером?») — пустые списки жанров."""

RANK_PROMPT = """Ты — киносоветчик. Вот вкус зрителя по его оценкам:
Любимое: {love}
Понравилось: {like}
Не понравилось: {nope}

Его запрос: «{query}»

Ниже новинки последних месяцев с высокими оценками. Выбери до {n} штук, которые лучше всего
подходят под запрос и под его вкус: похожи на любимое и понравившееся, не похожи на то, что
не понравилось. Учитывай особые пожелания из запроса. Лучше меньше, но точно.
Ответь ТОЛЬКО JSON-массивом:
[{{"n": номер, "why": "1–2 коротких предложения по-русски: почему именно ему понравится; если
уместно — сравни с конкретным фильмом или сериалом из его любимого"}}]

НОВИНКИ:
{items}"""


def parse_request(text, state, cfg):
    low = text.lower()
    kind = "tv" if re.search(r"сериал|сезон", low) else "movie" if re.search(r"фильм|кино\b|киношк", low) else "any"
    q = {"type": kind, "movie_genres": [], "tv_genres": [], "label": "", "wish": ""}
    for rx, label, mg, tg in GENRES:
        if re.search(rx, low):
            q["movie_genres"] += mg
            q["tv_genres"] += tg
            q["label"] = q["label"] or label
    if not q["movie_genres"]:                          # жанр не узнали — спросим ИИ
        res = claude(PARSE_PROMPT.format(q=text[:300]), state, cfg, max_tokens=300)
        if res:
            r = res[0]
            ints = lambda v: [int(x) for x in (v or []) if str(x).isdigit()]
            q.update(movie_genres=ints(r.get("movie_genres")), tv_genres=ints(r.get("tv_genres")),
                     label=str(r.get("label") or "")[:40], wish=str(r.get("wish") or "")[:120])
            if r.get("type") in ("movie", "tv", "any") and kind == "any":
                q["type"] = r["type"]
    q["label"] = q["label"] or "новинки"
    return q


def recommend(state, cfg, today, text):
    """Ответ на «Хочу посмотреть ужастик»: несколько новинок под вкус зрителя."""
    q = parse_request(text, state, cfg)
    start = today - dt.timedelta(days=cfg["rec_days"])
    items = []
    if q["type"] in ("movie", "any"):
        extra = {"with_genres": "|".join(map(str, q["movie_genres"]))} if q["movie_genres"] else {}
        items += movie_items(cfg, start, today, extra, limit=25 if q["type"] == "movie" else 15, pages=2)
    if q["type"] in ("tv", "any"):
        extra = {"with_genres": "|".join(map(str, q["tv_genres"]))} if q["tv_genres"] else {}
        seen, tv = set(), []
        for it in sorted(tv_items(cfg, start, today, {"tv", "season"}, extra,
                                  limit=20 if q["type"] == "tv" else 12, pages=1),
                         key=lambda x: x["date"], reverse=True):
            if it["tmdb_id"] not in seen:              # у сериала — только самый свежий сезон
                seen.add(it["tmdb_id"])
                tv.append(it)
        items += tv
    known = taste_known(state)
    items = [it for it in items if it["key"] not in known and it["title"].lower() not in known
             and str(it.get("orig") or "").lower() not in known
             and not (set(it.get("genres", [])) & cfg["exclude"])]

    good, near = [], []
    for it in sorted(items, key=lambda x: -x["popularity"]):
        rec = kp_lookup(it, state, today) or {}
        if passes(rec, cfg, cfg["rec_min_rating"], cfg["rec_min_rating"]):
            good.append((it, rec))
        elif passes(rec, cfg, cfg["rec_min_rating"] - 0.5, cfg["rec_min_rating"] - 0.5):
            near.append((it, rec))
    note = ""
    if not good and near:
        good, note = near, (f"С оценкой от {cfg['rec_min_rating']:g} ничего не нашлось — "
                            f"вот лучшее, что есть (от {cfg['rec_min_rating'] - 0.5:g}).")
    score = lambda p: -((p[1].get("imdb") or 0) + (p[1].get("kp") or 0))
    good.sort(key=score)
    good = good[:20]

    picks = []
    taste = load_taste(state)
    if len(good) > 1 and any(taste.values()):
        lines = []
        for i, (it, rec) in enumerate(good, 1):
            kind = {"movie": "фильм", "tv": "новый сериал", "season": f"{it.get('season')} сезон"}[it["kind"]]
            lines.append(f"[{i}] {rec.get('name') or it['title']} / {it.get('orig')} ({it.get('year')}, {kind}) | "
                         f"{', '.join(it.get('genres', []))} | IMDb {rec.get('imdb')} КП {rec.get('kp')} | "
                         f"{short_plot(it.get('plot') or rec.get('desc'), 250)}")
        cut = lambda xs: "; ".join(xs[-40:]) or "—"
        res = claude(RANK_PROMPT.format(love=cut(taste["love"]), like=cut(taste["like"]),
                                        nope=cut(taste["nope"]), query=text[:300],
                                        n=cfg["rec_count"], items="\n".join(lines)),
                     state, cfg, max_tokens=900)
        for r in res:
            try:
                n = int(str(r.get("n")).strip("[] "))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(good) and all(p[0] is not good[n - 1][0] for p in picks):
                picks.append((*good[n - 1], str(r.get("why") or "")))
            if len(picks) >= cfg["rec_count"]:
                break
    if not picks:                                       # без ИИ или без опроса — по оценкам
        picks = [(it, rec, "") for it, rec in good[:cfg["rec_count"]]]
    return q, picks, note, any(taste.values())


HELP = ("🍿 <b>Я советую новинки кино под ваш вкус.</b>\n\n"
        "Напишите, что хотите посмотреть, например:\n"
        "• <code>/kino ужастик</code>\n• <code>/kino лёгкую комедию на вечер</code>\n"
        "• <code>/kino сериал про расследование</code>\n\n"
        "В личной переписке со мной можно без /kino — просто текстом.\n"
        "Отвечайте на карточки «👍» или «👎» — я запомню, что вам нравится.\n"
        "<i>Отвечаю примерно через минуту.</i>")


def answer_request(state, cfg, today, chat, reply_to, text):
    q, picks, note, has_taste = recommend(state, cfg, today, text)
    months = max(1, round(cfg["rec_days"] / 30))
    if not picks and KP["off"]:
        tg_send(f"⚠️ {esc(KP['off'])}. Попробуйте завтра.", chat=chat, reply_to=reply_to)
        return
    if not picks:
        tg_send(f"😔 Не нашёл новинок ({esc(q['label'])}) за последние {months} мес. с высокими "
                "оценками. Попробуйте другой жанр.", chat=chat, reply_to=reply_to)
        return
    head = [f"🎯 <b>{esc(q['label'][:1].upper() + q['label'][1:])}</b> под ваш вкус · "
            f"новинки за {months} мес."]
    if note:
        head.append(f"<i>{esc(note)}</i>")
    if not has_taste:
        head.append("<i>Пройдите опрос (файл taste.txt) — советы станут точнее.</i>")
    tg_send("\n".join(head), chat=chat, reply_to=reply_to)
    for it, rec, why in picks:
        res = tg_send(card(it, cfg, rec, why), card_buttons(it, rec), poster_url(it, rec), chat=chat)
        remember_card(state, res, it, rec)


def allowed_chat(state, chat, user_id):
    """Группа из настроек — всегда; личка — только участникам этой группы (чтобы чужие
    не тратили лимиты). Проверку членства помним сутки."""
    if str(chat.get("id")) == str(CHAT["id"]):
        return True
    if chat.get("type") != "private" or not user_id:
        return False
    mem = state.setdefault("members", {})
    today = dt.date.today().isoformat()
    if mem.get(str(user_id), {}).get("d") != today:
        m = tg_api("getChatMember", {"chat_id": CHAT["id"], "user_id": user_id})
        mem[str(user_id)] = {"d": today, "ok": bool(m) and m.get("status") in
                             ("member", "administrator", "creator", "restricted")}
    return mem[str(user_id)]["ok"]


CMD = re.compile(r"^/(\w+)(?:@(\w+))?\s*(.*)$", re.S)


def handle_updates(state, cfg, today):
    """Новые сообщения боту: запросы рекомендаций и оценки карточек."""
    me = tg_api("getMe", {}) or {}
    botname = (me.get("username") or "").lower()
    off, asked = state.get("tg_offset", 0), 0
    for _ in range(3):
        res = tg_api("getUpdates", {"offset": off, "timeout": 0, "limit": 100,
                                    "allowed_updates": ["message", "message_reaction"]})
        if not res:
            break
        for u in res:
            off = max(off, u["update_id"] + 1)
            state["tg_offset"] = off              # даже если сообщение сломает разбор — не зациклимся
            try:
                r, m = u.get("message_reaction"), u.get("message")
                if r:
                    ck = f"{r.get('chat', {}).get('id')}:{r.get('message_id')}"
                    new = [x.get("emoji", "").replace("️", "") for x in r.get("new_reaction") or []
                           if x.get("type") == "emoji"]
                    v = -1 if any(e in NEG for e in new) else 1 if any(e in POS for e in new) else 0
                    apply_vote(state, ck, v)
                    continue
                if not m or not m.get("text"):
                    continue
                if time.time() - m.get("date", 0) > 2 * 86400:
                    continue                          # старое — не отвечаем
                chat, text = m.get("chat", {}), m["text"].strip()
                uid = (m.get("from") or {}).get("id")
                if not allowed_chat(state, chat, uid):
                    continue
                reply = m.get("reply_to_message") or {}
                ck = f"{chat.get('id')}:{reply.get('message_id')}"
                if reply and ck in state.get("cards", {}):
                    v = text_vote(text)
                    if v and apply_vote(state, ck, v):
                        tg_api("setMessageReaction", {"chat_id": chat["id"], "message_id": m["message_id"],
                                                      "reaction": [{"type": "emoji", "emoji": "👌"}]})
                        continue
                c = CMD.match(text)
                if c and c.group(2) and c.group(2).lower() != botname:
                    continue                          # команда другому боту
                if c and c.group(1).lower() in ("start", "help"):
                    tg_send(HELP, chat=chat["id"])
                    continue
                if c and c.group(1).lower() in ("kino", "hochu", "rec", "film"):
                    query = c.group(3).strip()
                elif chat.get("type") == "private" and not c:
                    query = text
                elif botname and f"@{botname}" in text.lower():
                    query = re.sub(rf"@{re.escape(botname)}", "", text, flags=re.I).strip()
                elif re.match(r"(хочу|посоветуй|что посмотреть)", text.lower()):
                    query = text                       # в группе, если у бота выключен privacy mode
                else:
                    continue
                if not query:
                    tg_send(HELP, chat=chat["id"])
                    continue
                if asked >= 3:
                    continue                          # не больше 3 запросов за запуск
                asked += 1
                answer_request(state, cfg, today, chat["id"], m["message_id"], query)
            except Exception as e:
                print("[updates] сбой:", e)
        if len(res) < 100:
            break


def main():
    cfg = load_settings()
    now = dt.datetime.now(TZ)
    today = now.date()
    state = load_state()
    if state.get("chat_id"):
        CHAT["id"] = state["chat_id"]
    first_run = not state

    if not TMDB_KEY:
        print("Нет ключа TMDB_API_KEY")
        if not state.get("warned_tmdb"):
            tg_send("⚠️ Не задан ключ TMDB_API_KEY — без него бот не может искать новинки. "
                    "Добавьте его в Settings → Secrets and variables → Actions.", notify=True)
            state["warned_tmdb"] = True
            save_state(state)
        return

    if first_run and not FORCE_DIGEST:
        wd = cfg["digest_weekday"]
        every = "каждую" if wd in (2, 4, 5) else "каждое" if wd == 6 else "каждый"
        tg_send(f"✅ Кино-новинки запущены. Подборка — {every} {WD_FULL[wd]} "
                f"в {cfg['digest_hour']}:00.", notify=True)
        state["last_digest"] = ""
        save_state(state)
        return

    # Кинопоиск: бесплатно 200 запросов в сутки — считаем, сколько уже потратили сегодня
    kd = state.setdefault("kp_day", {"date": "", "n": 0})
    if kd["date"] != today.isoformat():
        kd.update(date=today.isoformat(), n=0)
    KP["left"] = min(cfg["kp_requests_per_run"], 190 - kd["n"]) if KP_KEY else 0

    try:
        handle_updates(state, cfg, today)          # запросы «хочу посмотреть…» и оценки
    except Exception as e:
        print("[updates] сбой:", e)

    due = (today.weekday() == cfg["digest_weekday"] and now.hour >= cfg["digest_hour"]
           and state.get("last_digest") != today.isoformat())
    if due or FORCE_DIGEST:
        KP["left"] = max(0, KP["left"] - 50)       # 50 запросов к Кинопоиску оставляем для советов
        run_digest(state, cfg, today)
        state["last_digest"] = today.isoformat()
    else:
        print(f"Сегодня не день подборки ({WD_FULL[cfg['digest_weekday']]}, {cfg['digest_hour']}:00).")
    kd["n"] += DIAG["kp"]
    if CHAT["id"] != TG_CHAT:
        state["chat_id"] = CHAT["id"]
    save_state(state)


if __name__ == "__main__":
    main()
